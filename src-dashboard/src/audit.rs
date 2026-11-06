//! Bounded durable JSONL events. No arbitrary message or request fields.
use anyhow::{Context, Result};
use serde::Serialize;
use std::{
    fs::{File, OpenOptions},
    io::Write,
    path::Path,
    sync::Mutex,
    time::{SystemTime, UNIX_EPOCH},
};

const MAX_BYTES: u64 = 16 * 1024 * 1024;
#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Event {
    LoginAllowed,
    LoginDenied,
    Logout,
    SessionRevoked,
}
#[derive(Serialize)]
struct Record {
    version: u8,
    timestamp_ms: u128,
    event: Event,
    actor_guid: Option<String>,
}

pub struct Audit(Mutex<File>);
impl Audit {
    #[cfg(test)]
    pub fn from_test_file(file: File) -> Self {
        Self(Mutex::new(file))
    }
    pub fn open(path: &Path) -> Result<Self> {
        use std::os::unix::fs::MetadataExt;
        anyhow::ensure!(path.is_absolute(), "audit path must be absolute");
        crate::enrollment::require_trusted_directory(
            path.parent().context("audit needs a parent")?,
        )?;
        let metadata = std::fs::symlink_metadata(path)?;
        let (_, peer) = tokio::net::UnixStream::pair()?;
        anyhow::ensure!(
            metadata.is_file() && metadata.uid() == peer.peer_cred()?.uid(),
            "audit file must be regular and caller-owned"
        );
        anyhow::ensure!(
            metadata.nlink() == 1 && metadata.mode() & 0o077 == 0,
            "audit file must have one link and no world access"
        );
        let file = OpenOptions::new()
            .append(true)
            .read(true)
            .open(path)
            .context("open provisioned audit log")?;
        let opened = file.metadata()?;
        anyhow::ensure!(
            opened.ino() == metadata.ino() && opened.dev() == metadata.dev(),
            "audit file changed while opening"
        );
        Ok(Self(Mutex::new(file)))
    }
    pub fn record(
        &self,
        event: Event,
        identity: Option<&crate::directory::Identity>,
    ) -> Result<()> {
        let record = Record {
            version: 1,
            timestamp_ms: SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis(),
            event,
            actor_guid: identity.map(|id| id.0.iter().map(|b| format!("{b:02x}")).collect()),
        };
        let mut bytes = serde_json::to_vec(&record)?;
        bytes.push(b'\n');
        let mut file = self
            .0
            .lock()
            .map_err(|_| anyhow::anyhow!("audit lock poisoned"))?;
        file.lock()?;
        let result = (|| -> Result<()> {
            anyhow::ensure!(
                file.metadata()?.len() + bytes.len() as u64 <= MAX_BYTES,
                "audit capacity reached; local archival required"
            );
            file.write_all(&bytes)?;
            file.sync_data()?;
            Ok(())
        })();
        let unlock = file.unlock();
        result?;
        unlock?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn records_are_typed_and_exclude_credentials() {
        let record = Record {
            version: 1,
            timestamp_ms: 1,
            event: Event::LoginAllowed,
            actor_guid: Some("07".repeat(16)),
        };
        let value = serde_json::to_value(record).unwrap();
        assert_eq!(value.as_object().unwrap().len(), 4);
        assert_eq!(value["event"], "login_allowed");
        assert!(value.get("password").is_none());
    }
    #[test]
    fn writes_survive_reopen_and_capacity_is_enforced() {
        let path = std::env::temp_dir().join(format!(
            "smolofis-audit-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        let file = OpenOptions::new()
            .create_new(true)
            .read(true)
            .append(true)
            .open(&path)
            .unwrap();
        let audit = Audit(Mutex::new(file));
        audit.record(Event::LoginDenied, None).unwrap();
        let line = std::fs::read_to_string(&path).unwrap();
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&line).unwrap()["actor_guid"],
            serde_json::Value::Null
        );
        audit.0.lock().unwrap().set_len(MAX_BYTES).unwrap();
        assert!(audit.record(Event::LoginDenied, None).is_err());
        assert_eq!(std::fs::metadata(&path).unwrap().len(), MAX_BYTES);
        drop(audit);
        std::fs::remove_file(path).unwrap();
    }
}
