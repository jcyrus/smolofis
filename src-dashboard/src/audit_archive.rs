//! Explicit local-root archival. Never deletes retained history.
use anyhow::{Context, Result};
use rand::RngCore;
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::Path,
};

const MAX_BYTES: u64 = 16 * 1024 * 1024;
const MAX_ENTRIES: usize = 64;

/// Explicit opt-in provisioning. Existing history is validated, never truncated.
pub fn initialize(base: &Path) -> Result<serde_json::Value> {
    use std::os::unix::fs::DirBuilderExt;
    crate::enrollment::require_root()?;
    anyhow::ensure!(base.is_absolute(), "absolute audit directory required");
    crate::enrollment::require_trusted_directory(base.parent().context("audit needs a parent")?)?;
    for directory in [base.to_path_buf(), base.join("archives")] {
        match fs::DirBuilder::new().mode(0o700).create(&directory) {
            Ok(()) => (),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
            Err(error) => return Err(error.into()),
        }
        crate::enrollment::require_trusted_directory(&directory)?;
        let metadata = fs::symlink_metadata(&directory)?;
        anyhow::ensure!(
            metadata.mode() & 0o777 == 0o700,
            "local audit directories require mode 0700"
        );
        File::open(&directory)?.sync_all()?;
    }
    let path = base.join("events.jsonl");
    match OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
    {
        Ok(file) => file.sync_all()?,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(error) => return Err(error.into()),
    }
    // Reuse the writer's ownership, inode, hard-link and permission guards.
    let _audit = crate::audit::Audit::open(&path)?;
    File::open(base)?.sync_all()?;
    File::open(base.parent().context("audit needs a parent")?)?.sync_all()?;
    Ok(
        serde_json::json!({"version":1,"audit":path,"archives":base.join("archives"),"history_preserved":true}),
    )
}

pub fn archive(source: &Path, destination: &Path) -> Result<serde_json::Value> {
    crate::enrollment::require_root()?;
    anyhow::ensure!(
        source.is_absolute() && destination.is_absolute(),
        "absolute paths required"
    );
    crate::enrollment::require_trusted_directory(source.parent().context("log needs a parent")?)?;
    crate::enrollment::require_trusted_directory(destination)?;
    archive_for_uid(source, destination, 0)
}

fn archive_for_uid(source: &Path, destination: &Path, uid: u32) -> Result<serde_json::Value> {
    let metadata = fs::symlink_metadata(source)?;
    anyhow::ensure!(
        metadata.is_file()
            && metadata.uid() == uid
            && metadata.nlink() == 1
            && metadata.mode() & 0o777 == 0o600,
        "source must be a root-owned regular file with one link and mode 0600"
    );
    let mut file = OpenOptions::new().read(true).write(true).open(source)?;
    anyhow::ensure!(
        file.try_lock().is_ok(),
        "audit log busy; retry after writers stop"
    );
    let opened = file.metadata()?;
    anyhow::ensure!(
        opened.dev() == metadata.dev() && opened.ino() == metadata.ino(),
        "audit changed while opening"
    );
    let result = archive_locked(&mut file, source, destination, &opened);
    let unlock = file.unlock();
    let value = result?;
    unlock?;
    Ok(value)
}

fn archive_locked(
    file: &mut File,
    source: &Path,
    destination: &Path,
    metadata: &fs::Metadata,
) -> Result<serde_json::Value> {
    anyhow::ensure!(metadata.len() <= MAX_BYTES, "audit exceeds archival bound");
    // Each archive has a data file and manifest. Unknown entries count too.
    let entries = fs::read_dir(destination)?
        .take(MAX_ENTRIES + 1)
        .collect::<std::io::Result<Vec<_>>>()?;
    anyhow::ensure!(
        entries.len() <= MAX_ENTRIES - 2,
        "archive directory capacity reached; retain or export history before retrying"
    );
    let mut bytes = Vec::new();
    file.take(MAX_BYTES + 1).read_to_end(&mut bytes)?;
    anyhow::ensure!(
        bytes.len() as u64 == metadata.len(),
        "audit changed during snapshot"
    );
    let mut random = [0u8; 32];
    rand::rngs::OsRng
        .try_fill_bytes(&mut random)
        .context("archive ID entropy")?;
    let id: String = random.iter().map(|b| format!("{b:02x}")).collect();
    let archived = destination.join(format!("audit-{id}.jsonl"));
    let manifest = destination.join(format!("audit-{id}.manifest.json"));
    let digest = format!("{:x}", Sha256::digest(&bytes));
    let mut snapshot = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o400)
        .open(&archived)?;
    snapshot.write_all(&bytes)?;
    snapshot.sync_all()?;
    // This manifest describes the durable snapshot, not a successful replacement.
    let description = serde_json::json!({"version":1,"archive_id":id,"archive":archived,"bytes":bytes.len(),"sha256":digest,"status":"snapshot_saved"});
    let mut manifest_file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o400)
        .open(manifest)?;
    manifest_file.write_all(&serde_json::to_vec_pretty(&description)?)?;
    manifest_file.sync_all()?;
    File::open(destination)?.sync_all()?;
    let parent = source.parent().context("log needs a parent")?;
    let temporary = parent.join(format!(".audit-{id}.new"));
    let replacement = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    replacement.sync_all()?;
    let current = fs::symlink_metadata(source)?;
    anyhow::ensure!(
        current.dev() == metadata.dev()
            && current.ino() == metadata.ino()
            && current.nlink() == 1
            && current.uid() == metadata.uid()
            && current.mode() & 0o777 == 0o600,
        "audit changed before replacement; snapshot retained"
    );
    fs::rename(&temporary, source).context("replace audit log; snapshot retained")?;
    File::open(parent)?
        .sync_all()
        .context("audit replaced but directory sync failed; inspect locally")?;
    Ok(
        serde_json::json!({"version":1,"archive_id":id,"archive":archived,"bytes":bytes.len(),"sha256":digest,"current_log_replaced":true}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn archival_preserves_partial_bytes_and_refuses_busy_or_full_directory() {
        let directory = std::env::temp_dir().join(format!(
            "archive-test-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        fs::create_dir(&directory).unwrap();
        let destination = directory.join("archives");
        fs::create_dir(&destination).unwrap();
        let source = directory.join("events.jsonl");
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&source)
            .unwrap();
        file.write_all(b"{\"partial\":").unwrap();
        let uid = file.metadata().unwrap().uid();
        file.lock().unwrap();
        assert!(archive_for_uid(&source, &destination, uid).is_err());
        file.unlock().unwrap();
        let result = archive_for_uid(&source, &destination, uid).unwrap();
        let archived = Path::new(result["archive"].as_str().unwrap());
        assert_eq!(fs::read(archived).unwrap(), b"{\"partial\":");
        assert_eq!(fs::metadata(archived).unwrap().mode() & 0o777, 0o400);
        assert!(fs::read(&source).unwrap().is_empty());
        assert_eq!(fs::metadata(&source).unwrap().mode() & 0o777, 0o600);
        for index in 0..MAX_ENTRIES - 2 {
            File::create(destination.join(format!("retained-{index}"))).unwrap();
        }
        assert!(archive_for_uid(&source, &destination, uid).is_err());
        assert_eq!(fs::read(archived).unwrap(), b"{\"partial\":");
        for entry in fs::read_dir(&destination).unwrap() {
            fs::remove_file(entry.unwrap().path()).unwrap();
        }
        fs::remove_file(source).unwrap();
        fs::remove_dir(destination).unwrap();
        fs::remove_dir(directory).unwrap();
    }
}
