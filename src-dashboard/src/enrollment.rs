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

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Record {
    version: u8,
    device_id: String,
    machine_id_digest: String,
    generation: String,
    config_digest: String,
    first_admin_guid: String,
    locked: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    last_operation: Option<crate::audit::OperationReceipt>,
}

impl Record {
    fn valid_schema(&self) -> bool {
        (self.version == 1 && self.last_operation.is_none())
            || (self.version == 2
                && self.last_operation.as_ref().is_some_and(|receipt| {
                    receipt.valid()
                        && self.locked
                            == (receipt.operation == crate::audit::LocalOperationKind::Lockdown)
                }))
    }
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
        record.valid_schema()
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

fn local_audit(state: &Path, config: Option<&Path>) -> Result<Option<crate::audit::Audit>> {
    use std::os::unix::fs::MetadataExt;
    std::env::var_os("SMOLOFIS_LOCAL_AUDIT_FILE")
        .map(|path| {
            let path = Path::new(&path);
            let metadata = fs::symlink_metadata(path)?;
            for protected in std::iter::once(state).chain(config) {
                if let Ok(other) = fs::metadata(protected) {
                    anyhow::ensure!(metadata.dev() != other.dev() || metadata.ino() != other.ino(),
                        "local audit must be separate from enrollment state and directory configuration");
                }
            }
            crate::audit::Audit::open(path)
        })
        .transpose()
}

fn persist_enrollment(
    path: &Path,
    record: &Record,
    identity: &crate::directory::Identity,
    audit: Option<&crate::audit::Audit>,
) -> Result<crate::audit::OperationReceipt> {
    use crate::audit::{Event, LocalOperationKind, LocalOutcome, OperationReceipt};
    let receipt = OperationReceipt {
        id: random_id()?,
        operation: LocalOperationKind::Enrollment,
        outcome: LocalOutcome::Applied,
    };
    let mut next = record.clone();
    next.version = 2;
    next.last_operation = Some(receipt.clone());
    if let Some(audit) = audit {
        // Write-ahead authorization, not a claim that the state commit succeeded.
        audit.record_operation(
            Event::LocalEnrollmentAuthorized,
            Some(identity),
            &receipt,
            None,
        ).with_context(|| format!("Operation {} not applied: authorization audit failed; enrollment state unchanged", receipt.id))?;
    }
    if let Err(error) = write_record(path, &next) {
        if let Some(audit) = audit {
            let _ = audit.record_operation(
                Event::LocalOperationUncertain,
                Some(identity),
                &receipt,
                Some(LocalOutcome::Unknown),
            );
        }
        return Err(error.context(format!(
            "Operation {}: enrollment outcome uncertain; inspect local status",
            receipt.id
        )));
    }
    if let Some(audit) = audit {
        audit.record_operation(Event::LocalEnrollmentApplied, Some(identity), &receipt, Some(LocalOutcome::Applied))
            .with_context(|| format!("Operation {} applied to enrollment state, but completion audit failed; inspect local status", receipt.id))?;
    }
    Ok(receipt)
}

fn persist_lockdown(
    path: &Path,
    record: &Record,
    audit: Result<Option<crate::audit::Audit>>,
) -> Result<crate::audit::OperationReceipt> {
    use crate::audit::{Event, LocalOperationKind, LocalOutcome, OperationReceipt};
    let receipt = OperationReceipt {
        id: random_id()?,
        operation: LocalOperationKind::Lockdown,
        outcome: LocalOutcome::Applied,
    };
    let mut next = record.clone();
    next.version = 2;
    next.last_operation = Some(receipt.clone());
    // Access removal must remain possible even with a broken/full audit file.
    if let Err(error) = write_record(path, &next) {
        if let Ok(Some(audit)) = &audit {
            let _ = audit.record_operation(
                Event::LocalOperationUncertain,
                None,
                &receipt,
                Some(LocalOutcome::Unknown),
            );
        }
        return Err(error.context(format!(
            "Operation {}: lockdown outcome uncertain; inspect local status",
            receipt.id
        )));
    }
    (|| -> Result<()> {
        if let Some(audit) = audit? {
            audit.record_operation(Event::LocalConsoleLocked, None, &receipt, Some(LocalOutcome::Applied))?;
        }
        Ok(())
    })().with_context(|| format!("Console is locked (operation {}), but local audit recording failed; inspect local status and archive/repair the audit log", receipt.id))?;
    Ok(receipt)
}

pub async fn command(args: &[String]) -> Result<()> {
    require_root()?;
    match args {
        [operation, config, state, username] if operation == "--auth-enroll" => {
            let path = Path::new(state);
            let _lock = command_lock(path)?;
            let audit = local_audit(path, Some(Path::new(config)))?;
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
                generation: random_id()?, config_digest: digest, first_admin_guid: hex(&identity.0), locked: false, last_operation: None };
            let receipt = persist_enrollment(path, &record, &identity, audit.as_ref())?;
            println!("Operation {} applied.", receipt.id);
            println!("Enrollment saved. Set SMOLOFIS_AUTH_STATE to this file when starting the panel. Prior sessions using this enrollment are invalid. Restart if configuration changed.");
        },
        [operation, state] if operation == "--auth-lock" => {
            let path = Path::new(state);
            let _lock = command_lock(path)?;
            let mut record = read_record(path)?;
            let audit = local_audit(path, None);
            record.locked = true;
            record.generation = random_id()?;
            let receipt = persist_lockdown(path, &record, audit)?;
            println!("Operation {} applied.", receipt.id);
            println!("Enrollment locked. Panels configured with this state will deny access. Re-enroll a verified directory administrator locally to reopen it.");
        },
        [operation, state] if operation == "--auth-status" => {
            let record = read_record(Path::new(state))?;
            println!("{}", serde_json::to_string_pretty(&serde_json::json!({"version":record.version,"device_id":record.device_id,"locked":record.locked,"first_admin_guid":record.first_admin_guid,"last_operation":record.last_operation,"directory_checked":false}))?);
        },
        [operation, base] if operation == "--auth-audit-init" => {
            println!("{}", serde_json::to_string_pretty(&crate::audit_archive::initialize(Path::new(base))?)?);
        },
        [operation, source, destination] if operation == "--auth-audit-archive" => {
            let result = crate::audit_archive::archive(Path::new(source), Path::new(destination))?;
            println!("{}", serde_json::to_string_pretty(&result)?);
        },
        [operation, state, id] if operation == "--auth-operation-status" => {
            anyhow::ensure!(valid_hex(id, 64), "operation ID must contain 64 hex characters");
            let path = Path::new(state);
            require_trusted_directory(path.parent().context("state needs a parent")?)?;
            let current = match fs::symlink_metadata(path) {
                Ok(_) => Some(read_record(path)?),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
                Err(error) => return Err(error.into()),
            };
            let mut response = serde_json::json!({"version":1,"operation_id":id.to_ascii_lowercase(),
                "operation":null,"outcome":"unknown","source":"unknown","audit_status":"not_configured",
                "found":false,"directory_checked":false});
            if let Some(receipt) = current.and_then(|record| record.last_operation)
                .filter(|receipt| receipt.id.eq_ignore_ascii_case(id)) {
                response["operation"] = serde_json::to_value(receipt.operation)?;
                response["outcome"] = serde_json::json!("applied");
                response["source"] = serde_json::json!("state_receipt");
                response["audit_status"] = serde_json::json!("not_checked");
                response["found"] = serde_json::json!(true);
            } else if let Some(log) = std::env::var_os("SMOLOFIS_LOCAL_AUDIT_FILE") {
                match crate::audit::operation_history(Path::new(&log), id) {
                    Ok(history) => {
                        response["operation"] = serde_json::to_value(history.operation)?;
                        response["outcome"] = serde_json::to_value(history.outcome)?;
                        response["source"] = serde_json::json!("audit_history");
                        response["audit_status"] = serde_json::json!(if history.incomplete_tail {"incomplete_tail"} else {"complete"});
                        response["found"] = serde_json::json!(history.found);
                    }
                    Err(_) => response["audit_status"] = serde_json::json!("unavailable"),
                }
            }
            println!("{}", serde_json::to_string_pretty(&response)?);
        },
        _ => anyhow::bail!("usage: --auth-enroll /absolute/config /absolute/state username | --auth-lock /absolute/state | --auth-status /absolute/state | --auth-operation-status /absolute/state operation-id | --auth-audit-archive /absolute/log /absolute/archive-directory | --auth-audit-init /absolute/directory"),
    }
    Ok(())
}

pub fn require_trusted_directory(path: &Path) -> Result<()> {
    trusted(path, true, 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn audit_fixture() -> (PathBuf, Record, crate::audit::Audit) {
        let directory =
            std::env::temp_dir().join(format!("smolofis-local-audit-{}", random_id().unwrap()));
        fs::create_dir(&directory).unwrap();
        let file = OpenOptions::new()
            .read(true)
            .append(true)
            .create_new(true)
            .open(directory.join("local.jsonl"))
            .unwrap();
        let record = Record {
            version: 1,
            device_id: "01".repeat(32),
            machine_id_digest: "02".repeat(32),
            generation: "03".repeat(32),
            config_digest: "04".repeat(32),
            first_admin_guid: "07".repeat(16),
            locked: false,
            last_operation: None,
        };
        (directory, record, crate::audit::Audit::from_test_file(file))
    }

    #[test]
    fn enrollment_audit_failure_preserves_existing_lock_and_generation() {
        let (directory, mut record, audit) = audit_fixture();
        let path = directory.join("state.json");
        record.locked = true;
        write_record(&path, &record).unwrap();
        let original = fs::read(&path).unwrap();
        fs::write(directory.join("local.jsonl"), b"partial").unwrap();
        record.locked = false;
        record.generation = "05".repeat(32);
        assert!(persist_enrollment(
            &path,
            &record,
            &crate::directory::Identity([7; 16]),
            Some(&audit)
        )
        .is_err());
        assert_eq!(fs::read(&path).unwrap(), original);
        assert_eq!(fs::read(directory.join("local.jsonl")).unwrap(), b"partial");
        drop(audit);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn local_authorization_identifies_root_and_verified_directory_actor() {
        let (directory, record, audit) = audit_fixture();
        persist_enrollment(
            &directory.join("state.json"),
            &record,
            &crate::directory::Identity([7; 16]),
            Some(&audit),
        )
        .unwrap();
        let events: Vec<serde_json::Value> = fs::read_to_string(directory.join("local.jsonl"))
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(events.len(), 2);
        let event = &events[0];
        assert_eq!(event["event"], "local_enrollment_authorized");
        assert_eq!(event["actor_uid"], 0);
        assert_eq!(event["actor_guid"], "07".repeat(16));
        assert!(event.get("username").is_none());
        assert!(event.get("password").is_none());
        assert_eq!(events[1]["event"], "local_enrollment_applied");
        assert_eq!(events[1]["operation_id"], event["operation_id"]);
        assert_eq!(events[1]["outcome"], "applied");
        let saved: Record =
            serde_json::from_slice(&fs::read(directory.join("state.json")).unwrap()).unwrap();
        assert_eq!(saved.version, 2);
        assert_eq!(
            saved.last_operation.unwrap().id,
            event["operation_id"].as_str().unwrap()
        );
        drop(audit);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn lockdown_succeeds_despite_audit_open_or_append_failure() {
        for open_failure in [false, true] {
            let (directory, mut record, audit) = audit_fixture();
            let path = directory.join("state.json");
            write_record(&path, &record).unwrap();
            fs::write(directory.join("local.jsonl"), b"partial").unwrap();
            record.locked = true;
            record.generation = "05".repeat(32);
            let audit = if open_failure {
                drop(audit);
                Err(anyhow::anyhow!("audit unavailable"))
            } else {
                Ok(Some(audit))
            };
            let error = persist_lockdown(&path, &record, audit).unwrap_err();
            assert!(error.to_string().contains("Console is locked"));
            let saved: Record = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
            assert!(saved.locked);
            assert_eq!(saved.generation, "05".repeat(32));
            assert!(error
                .to_string()
                .contains(&saved.last_operation.as_ref().unwrap().id));
            assert!(saved.last_operation.as_ref().unwrap().valid());
            assert_eq!(fs::read(directory.join("local.jsonl")).unwrap(), b"partial");
            fs::remove_dir_all(directory).unwrap();
        }
    }

    #[test]
    fn completed_lockdown_records_root_without_claiming_a_directory_actor() {
        let (directory, mut record, audit) = audit_fixture();
        record.locked = true;
        persist_lockdown(&directory.join("state.json"), &record, Ok(Some(audit))).unwrap();
        let event: serde_json::Value =
            serde_json::from_slice(&fs::read(directory.join("local.jsonl")).unwrap()).unwrap();
        assert_eq!(event["event"], "local_console_locked");
        assert_eq!(event["actor_uid"], 0);
        assert!(event["actor_guid"].is_null());
        let saved: Record =
            serde_json::from_slice(&fs::read(directory.join("state.json")).unwrap()).unwrap();
        assert!(saved.locked);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn failed_state_commit_records_authorization_without_claiming_completion() {
        let (directory, record, audit) = audit_fixture();
        assert!(persist_enrollment(
            &directory.join("missing/state.json"),
            &record,
            &crate::directory::Identity([7; 16]),
            Some(&audit)
        )
        .is_err());
        let events: Vec<serde_json::Value> = fs::read_to_string(directory.join("local.jsonl"))
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(events.len(), 2);
        assert_eq!(events[0]["event"], "local_enrollment_authorized");
        assert_eq!(events[1]["event"], "local_operation_uncertain");
        assert_eq!(events[1]["operation_id"], events[0]["operation_id"]);
        assert_eq!(events[1]["outcome"], "unknown");
        assert!(!directory.join("missing/state.json").exists());
        drop(audit);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn completion_audit_failure_leaves_an_applied_receipt_in_committed_state() {
        use std::os::unix::fs::FileExt;
        let (directory, record, audit) = audit_fixture();
        let path = directory.join("local.jsonl");
        let file = OpenOptions::new().write(true).open(&path).unwrap();
        let length = 16 * 1024 * 1024 - 350;
        file.set_len(length).unwrap();
        file.write_at(b"\n", length - 1).unwrap();
        let state = directory.join("state.json");
        let error = persist_enrollment(
            &state,
            &record,
            &crate::directory::Identity([7; 16]),
            Some(&audit),
        )
        .unwrap_err();
        let saved: Record = serde_json::from_slice(&fs::read(state).unwrap()).unwrap();
        let receipt = saved.last_operation.unwrap();
        assert!(receipt.valid());
        assert!(!saved.locked);
        assert!(error.to_string().contains("applied to enrollment state"));
        assert!(error.to_string().contains(&receipt.id));
        drop(audit);
        fs::remove_dir_all(directory).unwrap();
    }
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
    #[test]
    fn receipt_schema_accepts_legacy_and_rejects_inconsistent_or_unknown_outcomes() {
        use crate::audit::{LocalOperationKind, LocalOutcome, OperationReceipt};
        let (directory, mut record, audit) = audit_fixture();
        assert!(record.valid_schema());
        record.version = 2;
        assert!(!record.valid_schema());
        record.last_operation = Some(OperationReceipt {
            id: "ab".repeat(32),
            operation: LocalOperationKind::Enrollment,
            outcome: LocalOutcome::Applied,
        });
        assert!(record.valid_schema());
        record.locked = true;
        assert!(!record.valid_schema());
        record.last_operation.as_mut().unwrap().operation = LocalOperationKind::Lockdown;
        assert!(record.valid_schema());
        record.last_operation.as_mut().unwrap().outcome = LocalOutcome::Unknown;
        assert!(!record.valid_schema());
        record.last_operation.as_mut().unwrap().id = "invalid".into();
        assert!(!record.valid_schema());
        record.version = 1;
        assert!(!record.valid_schema());
        drop(audit);
        fs::remove_dir_all(directory).unwrap();
    }
    #[cfg(unix)]
    #[test]
    fn public_and_symlink_paths_are_not_trusted() {
        assert!(trusted(Path::new("relative"), false, 0).is_err());
        assert!(trusted(Path::new("/tmp"), true, 0).is_err());
    }
}
