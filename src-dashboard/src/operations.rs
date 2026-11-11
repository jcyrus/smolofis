//! Durable local-root operation execution and conservative interrupted-work recovery.
use anyhow::{Context, Result};
use rand::RngCore;
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

const MAX_BYTES: u64 = 1024 * 1024;
const MAX_OPERATIONS: usize = 256;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum Phase {
    Queued,
    Running,
    Succeeded,
    Failed,
    Unknown,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum Kind {
    MachineIdentityCheck,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum Outcome {
    IdentityVerified,
    IdentityUnavailable,
    IdentityMismatch,
    Interrupted,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Transition {
    phase: Phase,
    timestamp_ms: u128,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Operation {
    id: String,
    kind: Kind,
    actor_uid: u32,
    transitions: Vec<Transition>,
    outcome: Option<Outcome>,
}

impl Operation {
    fn phase(&self) -> Phase {
        self.transitions.last().expect("validated operation").phase
    }

    fn validate(&self) -> Result<()> {
        anyhow::ensure!(
            valid_id(&self.id) && self.actor_uid == 0,
            "invalid operation identity or actor"
        );
        let phases: Vec<_> = self.transitions.iter().map(|entry| entry.phase).collect();
        anyhow::ensure!(
            matches!(
                phases.as_slice(),
                [Phase::Queued]
                    | [Phase::Queued, Phase::Running]
                    | [Phase::Queued, Phase::Unknown]
                    | [
                        Phase::Queued,
                        Phase::Running,
                        Phase::Succeeded | Phase::Failed | Phase::Unknown
                    ]
            ),
            "invalid operation transition sequence"
        );
        anyhow::ensure!(
            matches!(
                (self.phase(), self.outcome),
                (Phase::Queued | Phase::Running, None)
                    | (Phase::Succeeded, Some(Outcome::IdentityVerified))
                    | (
                        Phase::Failed,
                        Some(Outcome::IdentityUnavailable | Outcome::IdentityMismatch)
                    )
                    | (Phase::Unknown, Some(Outcome::Interrupted))
            ),
            "inconsistent operation outcome"
        );
        Ok(())
    }

    fn transition(&mut self, phase: Phase, outcome: Option<Outcome>) -> Result<()> {
        let mut next = self.clone();
        next.transitions.push(Transition {
            phase,
            timestamp_ms: timestamp()?,
        });
        next.outcome = outcome;
        next.validate()?;
        *self = next;
        Ok(())
    }
}

#[derive(Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Journal {
    version: u8,
    machine_id_digest: String,
    operations: Vec<Operation>,
}

impl Journal {
    fn validate(&self) -> Result<()> {
        anyhow::ensure!(
            self.version == 1
                && valid_id(&self.machine_id_digest)
                && self.operations.len() <= MAX_OPERATIONS,
            "unsupported or oversized operation journal"
        );
        let mut ids = std::collections::HashSet::new();
        for operation in &self.operations {
            operation.validate()?;
            anyhow::ensure!(
                ids.insert(operation.id.to_ascii_lowercase()),
                "duplicate operation ID"
            );
        }
        Ok(())
    }
}

fn valid_id(id: &str) -> bool {
    id.len() == 64 && id.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn timestamp() -> Result<u128> {
    Ok(SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis())
}

fn random_id() -> Result<String> {
    let mut bytes = [0; 32];
    rand::rngs::OsRng
        .try_fill_bytes(&mut bytes)
        .map_err(|_| anyhow::anyhow!("OS randomness unavailable"))?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn protected_file(path: &Path, uid: u32) -> Result<fs::Metadata> {
    let metadata = fs::symlink_metadata(path)?;
    anyhow::ensure!(
        metadata.is_file()
            && metadata.uid() == uid
            && metadata.nlink() == 1
            && metadata.mode() & 0o777 == 0o600,
        "unsafe operation journal file"
    );
    Ok(metadata)
}

struct Store {
    base: PathBuf,
    _lock: File,
    journal: Journal,
}

impl Store {
    fn open(base: &Path) -> Result<Self> {
        crate::enrollment::require_trusted_directory(base)?;
        let store = Self::open_checked(base, 0)?;
        anyhow::ensure!(
            store.journal.machine_id_digest == crate::enrollment::machine_id_digest()?,
            "operation journal belongs to a different appliance"
        );
        Ok(store)
    }

    // Production checks the complete ancestor chain before this helper. Tests
    // exercise descriptor/transition behavior in a caller-owned private fixture.
    fn open_checked(base: &Path, uid: u32) -> Result<Self> {
        let metadata = fs::symlink_metadata(base)?;
        anyhow::ensure!(
            metadata.is_dir() && metadata.uid() == uid && metadata.mode() & 0o777 == 0o700,
            "operation directory requires owner-only access"
        );
        let path = base.join("journal.lock");
        let metadata = protected_file(&path, uid)?;
        let lock = OpenOptions::new().read(true).write(true).open(&path)?;
        let opened = lock.metadata()?;
        anyhow::ensure!(
            metadata.ino() == opened.ino() && metadata.dev() == opened.dev(),
            "operation lock changed while opening"
        );
        lock.try_lock()
            .map_err(|_| anyhow::anyhow!("another operation or recovery is active"))?;
        let path = base.join("journal.json");
        let metadata = protected_file(&path, uid)?;
        anyhow::ensure!(
            metadata.len() <= MAX_BYTES,
            "operation journal exceeds capacity"
        );
        let file = File::open(&path)?;
        let opened = file.metadata()?;
        anyhow::ensure!(
            metadata.ino() == opened.ino() && metadata.dev() == opened.dev(),
            "operation journal changed while opening"
        );
        let mut bytes = Vec::new();
        file.take(MAX_BYTES + 1).read_to_end(&mut bytes)?;
        anyhow::ensure!(
            bytes.len() as u64 <= MAX_BYTES,
            "operation journal exceeds capacity"
        );
        let journal: Journal =
            serde_json::from_slice(&bytes).context("invalid operation journal")?;
        journal.validate()?;
        Ok(Self {
            base: base.to_owned(),
            _lock: lock,
            journal,
        })
    }

    fn save(&self) -> Result<()> {
        self.journal.validate()?;
        let bytes = serde_json::to_vec_pretty(&self.journal)?;
        anyhow::ensure!(
            bytes.len() as u64 <= MAX_BYTES,
            "operation journal exceeds capacity"
        );
        let temporary = self.base.join(format!(".journal-{}", random_id()?));
        let result = (|| -> Result<()> {
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&temporary)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            fs::rename(&temporary, self.base.join("journal.json"))?;
            File::open(&self.base)?.sync_all()?;
            Ok(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(temporary);
        }
        result.context("journal commit uncertain; inspect operation status before retrying")
    }

    fn enqueue(&mut self) -> Result<String> {
        anyhow::ensure!(
            self.journal.operations.len() < MAX_OPERATIONS,
            "operation journal capacity reached"
        );
        anyhow::ensure!(
            !self
                .journal
                .operations
                .iter()
                .any(|operation| matches!(operation.phase(), Phase::Queued | Phase::Running)),
            "unfinished operation requires recovery"
        );
        let id = random_id()?;
        self.journal.operations.push(Operation {
            id: id.clone(),
            kind: Kind::MachineIdentityCheck,
            actor_uid: 0,
            transitions: vec![Transition {
                phase: Phase::Queued,
                timestamp_ms: timestamp()?,
            }],
            outcome: None,
        });
        self.save().with_context(|| format!("Operation {id}"))?;
        Ok(id)
    }

    fn advance(&mut self, id: &str, phase: Phase, outcome: Option<Outcome>) -> Result<()> {
        let operation = self
            .journal
            .operations
            .iter_mut()
            .find(|operation| operation.id.eq_ignore_ascii_case(id))
            .context("unknown operation ID")?;
        operation.transition(phase, outcome)?;
        self.save().with_context(|| format!("Operation {id}"))
    }

    fn recover(&mut self) -> Result<usize> {
        let mut recovered = 0;
        for operation in &mut self.journal.operations {
            if matches!(operation.phase(), Phase::Queued | Phase::Running) {
                operation.transition(Phase::Unknown, Some(Outcome::Interrupted))?;
                recovered += 1;
            }
        }
        if recovered != 0 {
            self.save()?;
        }
        Ok(recovered)
    }
}

fn initialize(base: &Path) -> Result<()> {
    crate::enrollment::require_trusted_directory(base.parent().context("journal needs a parent")?)?;
    match fs::DirBuilder::new().mode(0o700).create(base) {
        Ok(()) => (),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(error) => return Err(error.into()),
    }
    crate::enrollment::require_trusted_directory(base)?;
    anyhow::ensure!(
        fs::metadata(base)?.mode() & 0o777 == 0o700,
        "operation directory requires mode 0700"
    );
    let lock_path = base.join("journal.lock");
    match OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&lock_path)
    {
        Ok(file) => file.sync_all()?,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(error) => return Err(error.into()),
    }
    let metadata = protected_file(&lock_path, 0)?;
    let lock = OpenOptions::new().read(true).write(true).open(&lock_path)?;
    anyhow::ensure!(
        lock.metadata()?.ino() == metadata.ino() && lock.metadata()?.dev() == metadata.dev(),
        "operation lock changed"
    );
    lock.try_lock()
        .map_err(|_| anyhow::anyhow!("operation journal busy"))?;
    let path = base.join("journal.json");
    match OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
    {
        Ok(mut file) => {
            let journal = Journal {
                version: 1,
                machine_id_digest: crate::enrollment::machine_id_digest()?,
                operations: Vec::new(),
            };
            file.write_all(&serde_json::to_vec(&journal)?)?;
            file.sync_all()?;
        }
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(error) => return Err(error.into()),
    }
    File::open(base)?.sync_all()?;
    File::open(base.parent().context("journal needs a parent")?)?.sync_all()?;
    drop(lock);
    let _store = Store::open(base)?;
    Ok(())
}

fn check_identity() -> Outcome {
    let check = || -> Result<bool> {
        let runtime = crate::enrollment::machine_id_digest()?;
        let saved = Path::new("/etc/smolofis/identity/machine-id");
        crate::enrollment::require_trusted_file(saved)?;
        let mut bytes = String::new();
        File::open(saved)?.take(129).read_to_string(&mut bytes)?;
        let id = bytes.trim();
        anyhow::ensure!(
            id.len() == 32
                && id.bytes().all(|byte| byte.is_ascii_hexdigit())
                && id != "0".repeat(32),
            "invalid saved identity"
        );
        use sha2::{Digest, Sha256};
        let digest: String = Sha256::digest(id.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        Ok(runtime == digest)
    };
    match check() {
        Ok(true) => Outcome::IdentityVerified,
        Ok(false) => Outcome::IdentityMismatch,
        Err(_) => Outcome::IdentityUnavailable,
    }
}

pub fn command(args: &[String]) -> Result<()> {
    crate::enrollment::require_root()?;
    match args {
        [action, base] if action == "--operation-init" => {
            initialize(Path::new(base))?;
            println!("{{\"version\":1,\"initialized\":true}}");
        }
        [action, base] if action == "--operation-run" => {
            let mut store = Store::open(Path::new(base))?;
            let id = store.enqueue()?;
            store.advance(&id, Phase::Running, None)?;
            let outcome = check_identity();
            let phase = if outcome == Outcome::IdentityVerified { Phase::Succeeded } else { Phase::Failed };
            store.advance(&id, phase, Some(outcome))?;
            let operation = store.journal.operations.last().context("operation missing")?;
            println!("{}", serde_json::to_string_pretty(operation)?);
            anyhow::ensure!(phase == Phase::Succeeded, "Operation {id} failed; inspect its recorded outcome");
        }
        [action, base] if action == "--operation-recover" => {
            let mut store = Store::open(Path::new(base))?;
            let recovered = store.recover()?;
            println!("{}", serde_json::json!({"version":1,"recovered":recovered}));
        }
        [action, base] if action == "--operation-list" => {
            let store = Store::open(Path::new(base))?;
            println!("{}", serde_json::to_string_pretty(&store.journal)?);
        }
        [action, base, id] if action == "--operation-status" => {
            anyhow::ensure!(valid_id(id), "operation ID requires 64 hex characters");
            let store = Store::open(Path::new(base))?;
            let operation = store.journal.operations.iter().find(|operation| operation.id.eq_ignore_ascii_case(id)).context("unknown operation ID")?;
            println!("{}", serde_json::to_string_pretty(operation)?);
        }
        _ => anyhow::bail!("usage: --operation-init|--operation-run|--operation-recover|--operation-list /absolute/journal-directory | --operation-status /absolute/journal-directory operation-id"),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    struct Fixture(PathBuf);
    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.0).unwrap();
        }
    }
    fn fixture() -> (Fixture, u32) {
        let base =
            std::env::temp_dir().join(format!("smolofis-operations-{}", random_id().unwrap()));
        fs::create_dir(&base).unwrap();
        fs::set_permissions(&base, fs::Permissions::from_mode(0o700)).unwrap();
        let journal = Journal {
            version: 1,
            machine_id_digest: "ab".repeat(32),
            operations: Vec::new(),
        };
        fs::write(
            base.join("journal.json"),
            serde_json::to_vec(&journal).unwrap(),
        )
        .unwrap();
        fs::write(base.join("journal.lock"), b"").unwrap();
        for name in ["journal.json", "journal.lock"] {
            fs::set_permissions(base.join(name), fs::Permissions::from_mode(0o600)).unwrap();
        }
        let uid = fs::metadata(&base).unwrap().uid();
        (Fixture(base), uid)
    }

    #[test]
    fn restart_recovers_unfinished_work_without_replaying_or_changing_completed_results() {
        let (fixture, uid) = fixture();
        let base = &fixture.0;
        let mut store = Store::open_checked(base, uid).unwrap();
        let first = store.enqueue().unwrap();
        store.advance(&first, Phase::Running, None).unwrap();
        store
            .advance(&first, Phase::Succeeded, Some(Outcome::IdentityVerified))
            .unwrap();
        let completed = serde_json::to_vec(&store.journal.operations[0]).unwrap();
        let second = store.enqueue().unwrap();
        store.advance(&second, Phase::Running, None).unwrap();
        drop(store);
        let mut reopened = Store::open_checked(base, uid).unwrap();
        assert!(reopened.enqueue().is_err());
        assert_eq!(reopened.recover().unwrap(), 1);
        assert_eq!(
            serde_json::to_vec(&reopened.journal.operations[0]).unwrap(),
            completed
        );
        assert_eq!(reopened.journal.operations[1].phase(), Phase::Unknown);
        let bytes = fs::read(base.join("journal.json")).unwrap();
        assert_eq!(reopened.recover().unwrap(), 0);
        assert_eq!(fs::read(base.join("journal.json")).unwrap(), bytes);
        assert!(reopened.advance(&second, Phase::Running, None).is_err());
        let third = reopened.enqueue().unwrap();
        drop(reopened);
        let mut reopened = Store::open_checked(base, uid).unwrap();
        assert_eq!(reopened.recover().unwrap(), 1);
        assert_eq!(reopened.journal.operations.last().unwrap().id, third);
        assert_eq!(
            reopened.journal.operations.last().unwrap().phase(),
            Phase::Unknown
        );
    }

    #[test]
    fn rejects_unpaired_outcomes_and_terminal_rewrites() {
        let (fixture, uid) = fixture();
        let mut store = Store::open_checked(&fixture.0, uid).unwrap();
        let id = store.enqueue().unwrap();
        assert!(store
            .advance(&id, Phase::Succeeded, Some(Outcome::IdentityVerified))
            .is_err());
        store.advance(&id, Phase::Running, None).unwrap();
        assert!(store
            .advance(&id, Phase::Failed, Some(Outcome::IdentityVerified))
            .is_err());
        store
            .advance(&id, Phase::Failed, Some(Outcome::IdentityMismatch))
            .unwrap();
        assert!(store
            .advance(&id, Phase::Succeeded, Some(Outcome::IdentityVerified))
            .is_err());
        assert!(store
            .advance(&"cd".repeat(32), Phase::Running, None)
            .is_err());
    }

    #[test]
    fn serialization_permissions_and_capacity_fail_closed() {
        let (fixture, uid) = fixture();
        let base = &fixture.0;
        let held = Store::open_checked(base, uid).unwrap();
        assert!(Store::open_checked(base, uid).is_err());
        drop(held);
        let path = base.join("journal.json");
        let good = fs::read(&path).unwrap();
        for bad in [b"partial".as_slice(), b"{\"version\":99}"] {
            fs::write(&path, bad).unwrap();
            assert!(Store::open_checked(base, uid).is_err());
            assert_eq!(fs::read(&path).unwrap(), bad);
        }
        fs::write(&path, &good).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(Store::open_checked(base, uid).is_err());
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        let hard = base.join("hardlink");
        fs::hard_link(&path, &hard).unwrap();
        assert!(Store::open_checked(base, uid).is_err());
        fs::remove_file(hard).unwrap();
        fs::remove_file(&path).unwrap();
        std::os::unix::fs::symlink(base.join("journal.lock"), &path).unwrap();
        assert!(Store::open_checked(base, uid).is_err());
        fs::remove_file(&path).unwrap();
        fs::write(&path, &good).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        let mut store = Store::open_checked(base, uid).unwrap();
        let id = store.enqueue().unwrap();
        store.advance(&id, Phase::Running, None).unwrap();
        store
            .advance(&id, Phase::Succeeded, Some(Outcome::IdentityVerified))
            .unwrap();
        let first = store.journal.operations[0].clone();
        store.journal.operations = (0..MAX_OPERATIONS)
            .map(|index| {
                let mut operation = first.clone();
                operation.id = format!("{index:064x}");
                operation
            })
            .collect();
        store.save().unwrap();
        let bytes = fs::read(&path).unwrap();
        assert!(store.enqueue().is_err());
        assert_eq!(fs::read(&path).unwrap(), bytes);
        store.journal.operations[1].id = store.journal.operations[0].id.clone();
        assert!(store.save().is_err());
        assert_eq!(fs::read(&path).unwrap(), bytes);
    }
}
