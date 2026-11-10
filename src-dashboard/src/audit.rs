//! Bounded durable JSONL events. No arbitrary message or request fields.
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::{
    fs::{File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    sync::Mutex,
    time::{SystemTime, UNIX_EPOCH},
};

const MAX_BYTES: u64 = 16 * 1024 * 1024;
#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Event {
    LoginAllowed,
    LoginDenied,
    Logout,
    SessionRevoked,
    LocalEnrollmentAuthorized,
    LocalConsoleLocked,
    LocalEnrollmentApplied,
    LocalOperationUncertain,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LocalOperationKind {
    Enrollment,
    Lockdown,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LocalOutcome {
    Applied,
    Unknown,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OperationReceipt {
    pub id: String,
    pub operation: LocalOperationKind,
    pub outcome: LocalOutcome,
}
impl OperationReceipt {
    pub fn valid(&self) -> bool {
        self.id.len() == 64
            && self.id.bytes().all(|byte| byte.is_ascii_hexdigit())
            && self.outcome == LocalOutcome::Applied
    }
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Record {
    version: u8,
    timestamp_ms: u128,
    event: Event,
    actor_guid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    actor_uid: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    operation_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    operation: Option<LocalOperationKind>,
    #[serde(skip_serializing_if = "Option::is_none")]
    outcome: Option<LocalOutcome>,
}

pub struct OperationHistory {
    pub operation: Option<LocalOperationKind>,
    pub outcome: LocalOutcome,
    pub found: bool,
    pub incomplete_tail: bool,
}

fn parse_history(bytes: &[u8], id: &str) -> Result<OperationHistory> {
    anyhow::ensure!(
        bytes.len() as u64 <= MAX_BYTES,
        "audit exceeds history limit"
    );
    let complete = bytes
        .iter()
        .rposition(|byte| *byte == b'\n')
        .map_or(0, |index| index + 1);
    let records = bytes[..complete]
        .split_inclusive(|byte| *byte == b'\n')
        .map(|line| serde_json::from_slice::<Record>(line).context("invalid audit history record"))
        .collect::<Result<Vec<_>>>()?;
    parse_operation_records(records.iter(), id, complete != bytes.len())
}

fn parse_operation_records<'a>(
    records: impl IntoIterator<Item = &'a Record>,
    id: &str,
    incomplete_tail: bool,
) -> Result<OperationHistory> {
    let mut history = OperationHistory {
        operation: None,
        outcome: LocalOutcome::Unknown,
        found: false,
        incomplete_tail,
    };
    let mut authorized = false;
    let mut applied = false;
    let mut uncertain = false;
    let mut actor: Option<String> = None;
    for record in records {
        anyhow::ensure!(record.version == 1, "unsupported audit history schema");
        if !record
            .operation_id
            .as_deref()
            .is_some_and(|value| value.eq_ignore_ascii_case(id))
        {
            continue;
        }
        anyhow::ensure!(
            record.actor_uid == Some(0),
            "untrusted operation attribution"
        );
        let kind = record.operation.context("missing operation kind")?;
        anyhow::ensure!(
            history.operation.is_none_or(|previous| previous == kind),
            "conflicting operation kinds"
        );
        history.operation = Some(kind);
        history.found = true;
        match (record.event, kind, record.outcome) {
            (Event::LocalEnrollmentAuthorized, LocalOperationKind::Enrollment, None) => {
                anyhow::ensure!(
                    !applied && !uncertain,
                    "authorization follows terminal outcome"
                );
                let guid = record
                    .actor_guid
                    .clone()
                    .context("missing verified actor")?;
                anyhow::ensure!(
                    guid.len() == 32 && guid.bytes().all(|byte| byte.is_ascii_hexdigit()),
                    "invalid verified actor"
                );
                anyhow::ensure!(
                    actor.as_ref().is_none_or(|previous| previous == &guid),
                    "conflicting verified actors"
                );
                actor = Some(guid);
                authorized = true;
            }
            (
                Event::LocalEnrollmentApplied,
                LocalOperationKind::Enrollment,
                Some(LocalOutcome::Applied),
            ) => {
                anyhow::ensure!(
                    authorized && !uncertain && record.actor_guid == actor,
                    "unpaired or conflicting enrollment outcome"
                );
                applied = true;
            }
            (
                Event::LocalConsoleLocked,
                LocalOperationKind::Lockdown,
                Some(LocalOutcome::Applied),
            ) => {
                anyhow::ensure!(
                    !uncertain && record.actor_guid.is_none(),
                    "conflicting lockdown outcome"
                );
                applied = true;
            }
            (Event::LocalOperationUncertain, _, Some(LocalOutcome::Unknown)) => {
                anyhow::ensure!(!applied, "uncertainty conflicts with applied outcome");
                uncertain = true;
            }
            _ => anyhow::bail!("invalid operation history event"),
        }
    }
    if applied {
        history.outcome = LocalOutcome::Applied;
    }
    Ok(history)
}

fn history_snapshot(file: &File) -> Result<Vec<u8>> {
    use std::os::unix::fs::FileExt;
    file.try_lock_shared()
        .map_err(|_| anyhow::anyhow!("audit history is busy"))?;
    let result = (|| {
        let length = file.metadata()?.len();
        anyhow::ensure!(length <= MAX_BYTES, "audit exceeds history limit");
        let mut bytes = vec![0; length as usize];
        file.read_exact_at(&mut bytes, 0)?;
        Ok(bytes)
    })();
    let unlock = file.unlock();
    let bytes = result?;
    unlock?;
    Ok(bytes)
}

fn read_history(path: &Path) -> Result<Vec<u8>> {
    use std::os::unix::fs::MetadataExt;
    anyhow::ensure!(path.is_absolute(), "audit history path must be absolute");
    crate::enrollment::require_trusted_directory(path.parent().context("audit needs a parent")?)?;
    let metadata = std::fs::symlink_metadata(path)?;
    let (_, peer) = tokio::net::UnixStream::pair()?;
    anyhow::ensure!(
        metadata.is_file()
            && metadata.uid() == peer.peer_cred()?.uid()
            && metadata.nlink() == 1
            && metadata.mode() & 0o077 == 0,
        "unsafe audit history file"
    );
    // No append/write descriptor: archived read-only logs remain queryable.
    let file = OpenOptions::new().read(true).open(path)?;
    let opened = file.metadata()?;
    anyhow::ensure!(
        opened.ino() == metadata.ino() && opened.dev() == metadata.dev(),
        "audit history changed while opening"
    );
    history_snapshot(&file)
}

pub fn operation_history(path: &Path, id: &str) -> Result<OperationHistory> {
    parse_history(&read_history(path)?, id)
}

#[derive(Serialize)]
pub struct OperationListing {
    pub version: u8,
    pub operations: Vec<OperationReceipt>,
    pub incomplete_tail: bool,
    pub source: &'static str,
}

fn parse_listing(bytes: &[u8]) -> Result<OperationListing> {
    anyhow::ensure!(
        bytes.len() as u64 <= MAX_BYTES,
        "audit exceeds history limit"
    );
    let complete = bytes
        .iter()
        .rposition(|byte| *byte == b'\n')
        .map_or(0, |index| index + 1);
    let mut grouped = std::collections::BTreeMap::<String, Vec<Record>>::new();
    for line in bytes[..complete].split_inclusive(|byte| *byte == b'\n') {
        let record: Record =
            serde_json::from_slice(line).context("invalid audit history record")?;
        anyhow::ensure!(record.version == 1, "unsupported audit history schema");
        if let Some(id) = &record.operation_id {
            anyhow::ensure!(
                id.len() == 64 && id.bytes().all(|byte| byte.is_ascii_hexdigit()),
                "invalid operation ID"
            );
            let id = id.to_ascii_lowercase();
            anyhow::ensure!(
                grouped.contains_key(&id) || grouped.len() < 4096,
                "too many operations; select a smaller archive"
            );
            grouped.entry(id).or_default().push(record);
        } else {
            anyhow::ensure!(
                !matches!(
                    record.event,
                    Event::LocalEnrollmentAuthorized
                        | Event::LocalEnrollmentApplied
                        | Event::LocalConsoleLocked
                        | Event::LocalOperationUncertain
                ),
                "operation record lacks ID"
            );
        }
    }
    let mut operations = Vec::with_capacity(grouped.len());
    for (id, records) in grouped {
        let history = parse_operation_records(records.iter(), &id, complete != bytes.len())?;
        operations.push(OperationReceipt {
            id,
            operation: history.operation.context("missing operation kind")?,
            outcome: history.outcome,
        });
    }
    Ok(OperationListing {
        version: 1,
        operations,
        incomplete_tail: complete != bytes.len(),
        source: "selected_audit_file",
    })
}

pub fn operation_listing(path: &Path) -> Result<OperationListing> {
    parse_listing(&read_history(path)?)
}

struct AuditIdentity {
    path: PathBuf,
    device: u64,
    inode: u64,
    uid: u32,
}

impl AuditIdentity {
    fn verify(&self, file: &File) -> Result<()> {
        use std::os::unix::fs::MetadataExt;
        let current = std::fs::symlink_metadata(&self.path)?;
        let opened = file.metadata()?;
        for metadata in [&current, &opened] {
            anyhow::ensure!(
                metadata.is_file()
                    && metadata.dev() == self.device
                    && metadata.ino() == self.inode
                    && metadata.uid() == self.uid
                    && metadata.nlink() == 1
                    && metadata.mode() & 0o777 == 0o600,
                "audit log replaced or permissions changed; restart writer"
            );
        }
        Ok(())
    }
}

pub struct Audit(Mutex<File>, Option<AuditIdentity>);
impl Audit {
    #[cfg(test)]
    pub fn from_test_file(file: File) -> Self {
        Self(Mutex::new(file), None)
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
            metadata.nlink() == 1 && metadata.mode() & 0o777 == 0o600,
            "audit file must have one link and no world access (mode 0600)"
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
        Ok(Self(
            Mutex::new(file),
            Some(AuditIdentity {
                path: path.to_path_buf(),
                device: opened.dev(),
                inode: opened.ino(),
                uid: opened.uid(),
            }),
        ))
    }
    pub fn record(
        &self,
        event: Event,
        identity: Option<&crate::directory::Identity>,
    ) -> Result<()> {
        self.append(event, identity, None, None)
    }

    // Only the local command dispatcher calls this after require_root(). The
    // UID identifies the root operator, not the human behind sudo/SSH.
    pub fn record_operation(
        &self,
        event: Event,
        identity: Option<&crate::directory::Identity>,
        receipt: &OperationReceipt,
        outcome: Option<LocalOutcome>,
    ) -> Result<()> {
        anyhow::ensure!(receipt.valid(), "invalid operation receipt");
        anyhow::ensure!(
            matches!(
                (event, receipt.operation, outcome),
                (
                    Event::LocalEnrollmentAuthorized,
                    LocalOperationKind::Enrollment,
                    None
                ) | (
                    Event::LocalEnrollmentApplied,
                    LocalOperationKind::Enrollment,
                    Some(LocalOutcome::Applied),
                ) | (
                    Event::LocalConsoleLocked,
                    LocalOperationKind::Lockdown,
                    Some(LocalOutcome::Applied),
                ) | (
                    Event::LocalOperationUncertain,
                    _,
                    Some(LocalOutcome::Unknown)
                )
            ),
            "invalid local audit event"
        );
        self.append(event, identity, Some(0), Some((receipt, outcome)))
    }

    fn append(
        &self,
        event: Event,
        identity: Option<&crate::directory::Identity>,
        actor_uid: Option<u32>,
        operation: Option<(&OperationReceipt, Option<LocalOutcome>)>,
    ) -> Result<()> {
        let record = Record {
            version: 1,
            timestamp_ms: SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis(),
            event,
            actor_guid: identity.map(|id| id.0.iter().map(|b| format!("{b:02x}")).collect()),
            actor_uid,
            operation_id: operation.map(|(receipt, _)| receipt.id.clone()),
            operation: operation.map(|(receipt, _)| receipt.operation),
            outcome: operation.and_then(|(_, outcome)| outcome),
        };
        let mut bytes = serde_json::to_vec(&record)?;
        bytes.push(b'\n');
        let mut file = self
            .0
            .lock()
            .map_err(|_| anyhow::anyhow!("audit lock poisoned"))?;
        file.lock()?;
        let result = (|| -> Result<()> {
            if let Some(identity) = &self.1 {
                identity.verify(&file)?;
            }
            // A crash or failed write can leave a partial final record. Never
            // concatenate a later event onto it or silently repair history.
            use std::os::unix::fs::FileExt;
            let length = file.metadata()?.len();
            if length != 0 {
                let mut tail = [0];
                anyhow::ensure!(
                    file.read_at(&mut tail, length - 1)? == 1 && tail[0] == b'\n',
                    "audit has an incomplete final record; local archival required"
                );
            }
            anyhow::ensure!(
                length + bytes.len() as u64 <= MAX_BYTES,
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
    fn writer_rejects_replacement_and_permission_changes() {
        use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
        let directory = std::env::temp_dir().join(format!(
            "audit-identity-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        std::fs::create_dir(&directory).unwrap();
        let path = directory.join("events.jsonl");
        let archive = directory.join("archive.jsonl");
        let file = OpenOptions::new()
            .create_new(true)
            .read(true)
            .append(true)
            .mode(0o600)
            .open(&path)
            .unwrap();
        let metadata = file.metadata().unwrap();
        let audit = Audit(
            Mutex::new(file),
            Some(AuditIdentity {
                path: path.clone(),
                device: metadata.dev(),
                inode: metadata.ino(),
                uid: metadata.uid(),
            }),
        );
        audit.record(Event::LoginDenied, None).unwrap();
        let original = std::fs::read(&path).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o400)).unwrap();
        assert!(audit.record(Event::LoginDenied, None).is_err());
        assert_eq!(std::fs::read(&path).unwrap(), original);
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        std::fs::rename(&path, &archive).unwrap();
        OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&path)
            .unwrap();
        assert!(audit.record(Event::LoginDenied, None).is_err());
        assert_eq!(std::fs::read(&archive).unwrap(), original);
        assert!(std::fs::read(&path).unwrap().is_empty());
        std::fs::remove_file(path).unwrap();
        std::fs::remove_file(archive).unwrap();
        std::fs::remove_dir(directory).unwrap();
    }

    #[test]
    fn listing_preserves_unknown_and_rejects_orphan_conflicting_or_malformed_history() {
        let begin = history_event("local_enrollment_authorized", "enrollment", None);
        let mut bytes = begin.clone();
        let second = String::from_utf8(history_event(
            "local_console_locked",
            "lockdown",
            Some("applied"),
        ))
        .unwrap()
        .replace(&"ab".repeat(32), &"cd".repeat(32));
        bytes.extend(second.as_bytes());
        bytes.extend(b"partial");
        let listing = parse_listing(&bytes).unwrap();
        assert!(listing.incomplete_tail);
        assert_eq!(listing.operations.len(), 2);
        assert_eq!(listing.operations[0].outcome, LocalOutcome::Unknown);
        assert_eq!(listing.operations[1].outcome, LocalOutcome::Applied);
        assert!(parse_listing(&history_event(
            "local_enrollment_applied",
            "enrollment",
            Some("applied")
        ))
        .is_err());
        assert!(parse_listing(
            &[
                begin,
                history_event("local_console_locked", "lockdown", Some("applied"))
            ]
            .concat()
        )
        .is_err());
        assert!(parse_listing(b"{}\n").is_err());
        assert!(parse_listing(&vec![b'x'; MAX_BYTES as usize + 1]).is_err());
    }

    #[test]
    fn listing_bounds_distinct_ids_normalizes_case_and_requires_root_attribution() {
        let line = String::from_utf8(history_event(
            "local_console_locked",
            "lockdown",
            Some("applied"),
        ))
        .unwrap();
        let upper = line.replace(&"ab".repeat(32), &"AB".repeat(32));
        let listing =
            parse_listing([line.as_bytes(), upper.as_bytes()].concat().as_slice()).unwrap();
        assert_eq!(listing.operations.len(), 1);
        assert_eq!(listing.operations[0].id, "ab".repeat(32));
        let mut row: serde_json::Value = serde_json::from_str(&line).unwrap();
        row["actor_uid"] = serde_json::json!(1000);
        assert!(parse_listing(format!("{}\n", row).as_bytes()).is_err());
        row["actor_uid"] = serde_json::json!(0);
        row.as_object_mut().unwrap().remove("operation_id");
        assert!(parse_listing(format!("{}\n", row).as_bytes()).is_err());
        let mut bytes = Vec::new();
        for index in 0..4096 {
            bytes.extend(
                line.replace(&"ab".repeat(32), &format!("{index:064x}"))
                    .as_bytes(),
            );
        }
        assert_eq!(parse_listing(&bytes).unwrap().operations.len(), 4096);
        bytes.extend(
            line.replace(&"ab".repeat(32), &format!("{:064x}", 4096))
                .as_bytes(),
        );
        assert!(parse_listing(&bytes).is_err());
    }

    fn history_event(event: &str, kind: &str, outcome: Option<&str>) -> Vec<u8> {
        let mut row = serde_json::json!({"version":1,"timestamp_ms":1,"event":event,
            "actor_uid":0,"actor_guid":if kind == "enrollment" {Some("07".repeat(16))} else {None},
            "operation_id":"ab".repeat(32),"operation":kind});
        if let Some(outcome) = outcome {
            row["outcome"] = serde_json::json!(outcome);
        }
        let mut bytes = serde_json::to_vec(&row).unwrap();
        bytes.push(b'\n');
        bytes
    }

    #[test]
    fn history_recovers_applied_outcome_but_never_promotes_a_begin_or_partial_completion() {
        let id = "ab".repeat(32);
        let begin = history_event("local_enrollment_authorized", "enrollment", None);
        let finish = history_event("local_enrollment_applied", "enrollment", Some("applied"));
        let pending = parse_history(&begin, &id).unwrap();
        assert!(pending.found);
        assert_eq!(pending.outcome, LocalOutcome::Unknown);
        let mut partial = begin.clone();
        partial.extend_from_slice(&finish[..finish.len() - 1]);
        let result = parse_history(&partial, &id).unwrap();
        assert!(result.incomplete_tail);
        assert_eq!(result.outcome, LocalOutcome::Unknown);
        partial.push(b'\n');
        let result = parse_history(&partial, &id.to_ascii_uppercase()).unwrap();
        assert_eq!(result.outcome, LocalOutcome::Applied);
        partial.extend_from_slice(b"{\"partial\":");
        let result = parse_history(&partial, &id).unwrap();
        assert_eq!(result.outcome, LocalOutcome::Applied);
        assert!(result.incomplete_tail);
        assert!(!parse_history(&partial, &"cd".repeat(32)).unwrap().found);
    }

    #[test]
    fn history_rejects_conflicts_orphan_completions_and_corrupt_complete_records() {
        let id = "ab".repeat(32);
        let begin = history_event("local_enrollment_authorized", "enrollment", None);
        let finish = history_event("local_enrollment_applied", "enrollment", Some("applied"));
        assert!(parse_history(&finish, &id).is_err());
        let mut valid = begin.clone();
        valid.extend_from_slice(&finish);
        for extra in [
            history_event("local_console_locked", "lockdown", Some("applied")),
            history_event("local_operation_uncertain", "enrollment", Some("unknown")),
            b"corrupt\n".to_vec(),
            b"{\"version\":99}\n".to_vec(),
        ] {
            let mut bytes = valid.clone();
            bytes.extend_from_slice(&extra);
            assert!(parse_history(&bytes, &id).is_err());
        }
        let lock = history_event("local_console_locked", "lockdown", Some("applied"));
        assert_eq!(
            parse_history(&lock, &id).unwrap().outcome,
            LocalOutcome::Applied
        );
        let mut wrong_actor: serde_json::Value = serde_json::from_slice(&lock).unwrap();
        wrong_actor["actor_uid"] = serde_json::json!(65534);
        let mut bytes = serde_json::to_vec(&wrong_actor).unwrap();
        bytes.push(b'\n');
        assert!(parse_history(&bytes, &id).is_err());
    }

    #[test]
    fn history_snapshot_is_readonly_bounded_and_does_not_wait_for_a_writer() {
        let path = std::env::temp_dir().join(format!("smolofis-history-{}", rand::random::<u64>()));
        std::fs::write(&path, b"retained bytes\n").unwrap();
        let reader = File::open(&path).unwrap();
        let writer = OpenOptions::new().write(true).open(&path).unwrap();
        writer.lock().unwrap();
        let start = std::time::Instant::now();
        assert!(history_snapshot(&reader).is_err());
        assert!(start.elapsed() < std::time::Duration::from_secs(1));
        writer.unlock().unwrap();
        assert_eq!(history_snapshot(&reader).unwrap(), b"retained bytes\n");
        writer.set_len(MAX_BYTES + 1).unwrap();
        assert!(history_snapshot(&reader).is_err());
        drop(reader);
        drop(writer);
        std::fs::remove_file(path).unwrap();
    }
    #[test]
    fn records_are_typed_and_exclude_credentials() {
        let record = Record {
            version: 1,
            timestamp_ms: 1,
            event: Event::LoginAllowed,
            actor_guid: Some("07".repeat(16)),
            actor_uid: None,
            operation_id: None,
            operation: None,
            outcome: None,
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
        let audit = Audit(Mutex::new(file), None);
        audit.record(Event::LoginDenied, None).unwrap();
        let line = std::fs::read_to_string(&path).unwrap();
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&line).unwrap()["actor_guid"],
            serde_json::Value::Null
        );
        audit.0.lock().unwrap().set_len(MAX_BYTES).unwrap();
        use std::os::unix::fs::FileExt;
        OpenOptions::new()
            .write(true)
            .open(&path)
            .unwrap()
            .write_at(b"\n", MAX_BYTES - 1)
            .unwrap();
        assert!(audit
            .record(Event::LoginDenied, None)
            .unwrap_err()
            .to_string()
            .contains("capacity"));
        assert_eq!(std::fs::metadata(&path).unwrap().len(), MAX_BYTES);
        drop(audit);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn partial_crash_tail_is_preserved_and_blocks_later_events() {
        let path = std::env::temp_dir().join(format!(
            "smolofis-audit-tail-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        let mut file = OpenOptions::new()
            .create_new(true)
            .read(true)
            .append(true)
            .open(&path)
            .unwrap();
        file.write_all(b"{\"version\":1").unwrap();
        let before = std::fs::read(&path).unwrap();
        let audit = Audit(Mutex::new(file), None);
        assert!(audit.record(Event::LoginAllowed, None).is_err());
        assert_eq!(std::fs::read(&path).unwrap(), before);
        drop(audit);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn independent_writers_preserve_complete_records() {
        const CHILD_PATH: &str = "SMOLOFIS_AUDIT_TEST_CHILD_PATH";
        if let Some(path) = std::env::var_os(CHILD_PATH) {
            let file = OpenOptions::new()
                .read(true)
                .append(true)
                .open(path)
                .unwrap();
            let audit = Audit(Mutex::new(file), None);
            for _ in 0..16 {
                audit.record(Event::LoginDenied, None).unwrap();
            }
            return;
        }
        let path = std::env::temp_dir().join(format!(
            "smolofis-audit-writers-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&path)
            .unwrap();
        let handles: Vec<_> = (0..4)
            .map(|_| {
                std::process::Command::new(std::env::current_exe().unwrap())
                    .args([
                        "--exact",
                        "audit::tests::independent_writers_preserve_complete_records",
                    ])
                    .env(CHILD_PATH, &path)
                    .stdout(std::process::Stdio::null())
                    .spawn()
                    .unwrap()
            })
            .collect();
        for mut handle in handles {
            assert!(handle.wait().unwrap().success());
        }
        let text = std::fs::read_to_string(&path).unwrap();
        assert_eq!(text.lines().count(), 64);
        for line in text.lines() {
            let value: serde_json::Value = serde_json::from_str(line).unwrap();
            assert_eq!(value["event"], "login_denied");
        }
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn write_failure_is_reported() {
        let path =
            std::env::temp_dir().join(format!("smolofis-audit-readonly-{}", rand::random::<u64>()));
        std::fs::write(&path, b"").unwrap();
        let file = OpenOptions::new().read(true).open(&path).unwrap();
        let audit = Audit(Mutex::new(file), None);
        assert!(audit.record(Event::LoginAllowed, None).is_err());
        assert!(std::fs::read(&path).unwrap().is_empty());
        drop(audit);
        std::fs::remove_file(path).unwrap();
    }
}
