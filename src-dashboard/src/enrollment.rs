//! Root-controlled enrollment and offline console lockdown. No recovery web login.
use anyhow::{Context, Result};
use rand::RngCore;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{IsTerminal, Read, Write},
    path::{Path, PathBuf},
};
use zeroize::Zeroizing;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Record {
    version: u8,
    device_id: String,
    machine_id_digest: String,
    generation: String,
    config_digest: String,
    first_admin_guid: String,
    locked: bool,
}

fn random_id() -> Result<String> {
    let mut bytes = [0; 32];
    rand::rngs::OsRng
        .try_fill_bytes(&mut bytes)
        .map_err(|_| anyhow::anyhow!("OS randomness unavailable"))?;
    Ok(hex(&bytes))
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn valid_hex(value: &str, len: usize) -> bool {
    value.len() == len && value.bytes().all(|b| b.is_ascii_hexdigit())
}

#[cfg(unix)]
fn trusted(path: &Path, directory: bool, owner: u32) -> Result<()> {
    use std::os::unix::fs::MetadataExt;
    anyhow::ensure!(path.is_absolute(), "enrollment paths must be absolute");
    // Reject symlinks and untrusted ancestors, not just an apparently safe leaf.
    for ancestor in path.ancestors() {
        let metadata = fs::symlink_metadata(ancestor).context("read trusted path metadata")?;
        let is_directory = ancestor != path || directory;
        anyhow::ensure!(
            if is_directory {
                metadata.is_dir()
            } else {
                metadata.is_file()
            },
            "enrollment paths must be regular files and real directories"
        );
        // Tests may use a non-root leaf owner; production always requires UID 0.
        anyhow::ensure!(
            metadata.uid() == owner || (ancestor != path && metadata.uid() == 0),
            "enrollment path must be root-owned"
        );
        anyhow::ensure!(
            metadata.mode() & 0o022 == 0,
            "enrollment path must not be group/world writable"
        );
    }
    Ok(())
}

fn read_record(path: &Path) -> Result<Record> {
    trusted(path, false, 0)?;
    let mut bytes = Vec::new();
    File::open(path)?.take(4097).read_to_end(&mut bytes)?;
    anyhow::ensure!(bytes.len() <= 4096, "enrollment state is too large");
    let record: Record = serde_json::from_slice(&bytes).context("invalid enrollment state")?;
    anyhow::ensure!(
        record.version == 1
            && valid_hex(&record.device_id, 64)
            && valid_hex(&record.machine_id_digest, 64)
            && valid_hex(&record.generation, 64)
            && valid_hex(&record.config_digest, 64)
            && valid_hex(&record.first_admin_guid, 32),
        "invalid enrollment schema or identifiers"
    );
    anyhow::ensure!(
        record.machine_id_digest == machine_id_digest()?,
        "enrollment belongs to a different appliance machine-id"
    );
    Ok(record)
}

fn machine_id_digest() -> Result<String> {
    let path = Path::new("/etc/machine-id");
    trusted(path, false, 0)?;
    let id = fs::read_to_string(path).context("read appliance machine-id")?;
    anyhow::ensure!(
        valid_hex(id.trim(), 32) && id.trim() != "00000000000000000000000000000000",
        "appliance needs an initialized machine-id before enrollment"
    );
    Ok(hex(&Sha256::digest(id.trim().as_bytes())))
}

fn config_digest(path: &Path) -> Result<String> {
    Ok(hex(&Sha256::digest(read_configuration(path)?)))
}

pub fn require_trusted_file(path: &Path) -> Result<()> {
    trusted(path, false, 0)
}

pub fn read_configuration(path: &Path) -> Result<Vec<u8>> {
    trusted(path, false, 0)?;
    let mut bytes = Vec::new();
    File::open(path)?.take(65537).read_to_end(&mut bytes)?;
    anyhow::ensure!(
        bytes.len() <= 65536,
        "authentication configuration is too large"
    );
    Ok(bytes)
}

pub struct Gate {
    path: PathBuf,
    config: PathBuf,
    device: String,
    digest: String,
}
impl Gate {
    pub fn load(path: &Path, config: &Path, bytes: &[u8]) -> Result<Self> {
        let record = read_record(path)?;
        let digest = hex(&Sha256::digest(bytes));
        anyhow::ensure!(
            record.config_digest == digest && config_digest(config)? == digest,
            "configuration does not match enrollment; re-enroll locally"
        );
        Ok(Self {
            path: path.to_owned(),
            config: config.to_owned(),
            device: record.device_id,
            digest,
        })
    }
    pub fn generation(&self) -> Result<String> {
        let record = read_record(&self.path)?;
        anyhow::ensure!(!record.locked, "console access is locked locally");
        anyhow::ensure!(
            record.device_id == self.device
                && record.config_digest == self.digest
                && config_digest(&self.config)? == self.digest,
            "enrollment binding changed; restart after local enrollment"
        );
        Ok(record.generation)
    }
}

fn command_lock(path: &Path) -> Result<File> {
    use std::os::unix::fs::OpenOptionsExt;
    let parent = path.parent().context("state path needs a parent")?;
    trusted(parent, true, 0)?;
    let lock_path = parent.join(".panel-enrollment.lock");
    if fs::symlink_metadata(&lock_path).is_ok() {
        trusted(&lock_path, false, 0)?;
    }
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(&lock_path)?;
    file.try_lock()
        .map_err(|_| anyhow::anyhow!("another enrollment command is active"))?;
    Ok(file)
}

fn write_record(path: &Path, record: &Record) -> Result<()> {
    use std::os::unix::fs::OpenOptionsExt;
    let parent = path.parent().context("state path needs a parent")?;
    let temporary = parent.join(format!(".panel-enrollment-{}", random_id()?));
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o644)
            .open(&temporary)?;
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(fs::Permissions::from_mode(0o644))?;
        file.write_all(&serde_json::to_vec_pretty(record)?)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        File::open(parent)?.sync_all()?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(temporary);
    }
    result
}

fn stdin_password() -> Result<Zeroizing<String>> {
    anyhow::ensure!(!std::io::stdin().is_terminal(), "read the password through protected stdin, for example systemd-ask-password piped to this command");
    let mut password = Zeroizing::new(String::new());
    std::io::stdin().take(1026).read_to_string(&mut password)?;
    let length = password.trim_end_matches(['\r', '\n']).len();
    password.truncate(length);
    anyhow::ensure!(
        !password.is_empty() && password.len() <= 1024,
        "password input must contain 1–1024 bytes"
    );
    Ok(password)
}

pub fn require_root() -> Result<()> {
    let (_, peer) = tokio::net::UnixStream::pair()?;
    anyhow::ensure!(
        peer.peer_cred()?.uid() == 0,
        "enrollment/recovery commands require root through local console or SSH"
    );
    Ok(())
}

pub async fn command(args: &[String]) -> Result<()> {
    require_root()?;
    match args {
        [operation, config, state, username] if operation == "--auth-enroll" => {
            let path = Path::new(state);
            let _lock = command_lock(path)?;
            let previous = match fs::symlink_metadata(path) {
                Ok(_) => Some(read_record(path)?),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
                Err(error) => return Err(error.into()),
            };
            let digest = config_digest(Path::new(config))?;
            let machine = machine_id_digest()?;
            let password = stdin_password()?;
            let identity = crate::auth::verify_first_admin(Path::new(config), username, &password).await?;
            anyhow::ensure!(config_digest(Path::new(config))? == digest, "configuration changed while verifying; retry enrollment");
            anyhow::ensure!(machine_id_digest()? == machine, "appliance identity changed during enrollment");
            let record = Record { version: 1, device_id: match previous { Some(record) => record.device_id, None => random_id()? }, machine_id_digest: machine,
                generation: random_id()?, config_digest: digest, first_admin_guid: hex(&identity.0), locked: false };
            write_record(path, &record)?;
            println!("Enrollment saved. Set SMOLOFIS_AUTH_STATE to this file when starting the panel. Prior sessions using this enrollment are invalid. Restart if configuration changed.");
        },
        [operation, state] if operation == "--auth-lock" => {
            let path = Path::new(state);
            let _lock = command_lock(path)?;
            let mut record = read_record(path)?;
            record.locked = true;
            record.generation = random_id()?;
            write_record(path, &record)?;
            println!("Enrollment locked. Panels configured with this state will deny access. Re-enroll a verified directory administrator locally to reopen it.");
        },
        [operation, state] if operation == "--auth-status" => {
            let record = read_record(Path::new(state))?;
            println!("{}", serde_json::to_string_pretty(&serde_json::json!({"version":record.version,"device_id":record.device_id,"locked":record.locked,"first_admin_guid":record.first_admin_guid,"directory_checked":false}))?);
        },
        _ => anyhow::bail!("usage: --auth-enroll /absolute/config /absolute/state username | --auth-lock /absolute/state | --auth-status /absolute/state"),
    }
    Ok(())
}

pub fn require_trusted_directory(path: &Path) -> Result<()> {
    trusted(path, true, 0)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn identifiers_are_unpredictable_and_state_shape_is_strict() {
        let a = random_id().unwrap();
        assert!(valid_hex(&a, 64));
        assert_ne!(a, random_id().unwrap());
        assert!(serde_json::from_str::<Record>(
            r#"{"version":1,"locked":false,"unknown":"field"}"#
        )
        .is_err());
    }
    #[cfg(unix)]
    #[test]
    fn public_and_symlink_paths_are_not_trusted() {
        assert!(trusted(Path::new("relative"), false, 0).is_err());
        assert!(trusted(Path::new("/tmp"), true, 0).is_err());
    }
}
