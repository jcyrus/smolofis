//! Read-only prerequisite inventory. This command never provisions or enables services.
use crate::{
    directory::{AdDirectory, DirectoryConfig},
    enrollment,
};
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::{fs, os::unix::fs::MetadataExt, path::Path, process::Stdio, time::Duration};

const CONFIG: &str = "/etc/smolofis/panel-auth.json";
const STATE: &str = "/etc/smolofis/panel-enrollment.json";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Configuration {
    origin: String,
    directory: DirectoryConfig,
}

#[derive(Serialize)]
struct Check {
    id: &'static str,
    passed: bool,
    next_step: &'static str,
}

#[derive(Serialize)]
struct Report {
    schema: u8,
    scope: &'static str,
    prerequisites_passed: bool,
    administration_enabled_by_check: bool,
    checks: Vec<Check>,
}

impl Report {
    fn new() -> Self {
        Self {
            schema: 1,
            scope: "read_only_pre_start_prerequisites",
            prerequisites_passed: false,
            administration_enabled_by_check: false,
            checks: vec![],
        }
    }
    fn add(&mut self, id: &'static str, passed: bool, next_step: &'static str) {
        self.checks.push(Check {
            id,
            passed,
            next_step: if passed { "" } else { next_step },
        });
    }
    fn finish(&mut self) {
        self.prerequisites_passed = self.checks.iter().all(|check| check.passed);
    }
}

fn account(passwd: &str, group: &str) -> Option<(u32, u32)> {
    let entries: Vec<_> = passwd
        .lines()
        .filter(|line| line.starts_with("smolofis-panel:"))
        .collect();
    if entries.len() != 1 {
        return None;
    }
    let fields: Vec<_> = entries[0].split(':').collect();
    if fields.len() != 7 {
        return None;
    }
    let uid = fields[2].parse::<u32>().ok()?;
    let gid = fields[3].parse::<u32>().ok()?;
    let groups: Vec<_> = group
        .lines()
        .filter(|line| line.starts_with("smolofis-panel:"))
        .collect();
    if uid == 0 || gid == 0 || groups.len() != 1 {
        return None;
    }
    let fields: Vec<_> = groups[0].split(':').collect();
    let uid_count = passwd
        .lines()
        .filter(|line| {
            line.split(':')
                .nth(2)
                .and_then(|value| value.parse::<u32>().ok())
                == Some(uid)
        })
        .count();
    let gid_count = group
        .lines()
        .filter(|line| {
            line.split(':')
                .nth(2)
                .and_then(|value| value.parse::<u32>().ok())
                == Some(gid)
        })
        .count();
    (fields.len() == 4
        && fields[2].parse::<u32>().ok()? == gid
        && fields[3].is_empty()
        && uid_count == 1
        && gid_count == 1)
        .then_some((uid, gid))
}

fn panel_can_read(path: &Path, gid: u32) -> Result<()> {
    enrollment::require_trusted_file(path)?;
    let metadata = fs::symlink_metadata(path)?;
    anyhow::ensure!(
        metadata.mode() & 0o004 != 0 || (metadata.gid() == gid && metadata.mode() & 0o040 != 0),
        "panel cannot read file"
    );
    for ancestor in path.parent().into_iter().flat_map(Path::ancestors) {
        let metadata = fs::symlink_metadata(ancestor)?;
        anyhow::ensure!(
            metadata.mode() & 0o001 != 0 || (metadata.gid() == gid && metadata.mode() & 0o010 != 0),
            "panel cannot traverse directory"
        );
    }
    Ok(())
}

fn reader_secret(path: &Path, gid: u32) -> Result<()> {
    panel_can_read(path, gid)?;
    let metadata = fs::symlink_metadata(path)?;
    anyhow::ensure!(
        metadata.nlink() == 1 && metadata.mode() & 0o007 == 0 && metadata.size() <= 65536,
        "unsafe reader secret"
    );
    Ok(())
}

fn root_log(path: &Path) -> Result<()> {
    enrollment::require_trusted_file(path)?;
    let metadata = fs::symlink_metadata(path)?;
    anyhow::ensure!(
        metadata.mode() & 0o777 == 0o600 && metadata.nlink() == 1,
        "unsafe root log"
    );
    Ok(())
}

fn panel_log(path: &Path, uid: u32, gid: u32) -> Result<()> {
    enrollment::require_trusted_directory(
        path.parent()
            .ok_or_else(|| anyhow::anyhow!("missing parent"))?,
    )?;
    let metadata = fs::symlink_metadata(path)?;
    anyhow::ensure!(
        metadata.is_file()
            && metadata.uid() == uid
            && metadata.gid() == gid
            && metadata.mode() & 0o777 == 0o600
            && metadata.nlink() == 1,
        "unsafe panel log"
    );
    for ancestor in path.parent().into_iter().flat_map(Path::ancestors) {
        let metadata = fs::symlink_metadata(ancestor)?;
        anyhow::ensure!(
            metadata.mode() & 0o001 != 0 || (metadata.gid() == gid && metadata.mode() & 0o010 != 0),
            "panel cannot traverse audit directory"
        );
    }
    Ok(())
}

fn saved_identity() -> Result<()> {
    let path = Path::new("/etc/smolofis/identity/machine-id");
    enrollment::require_trusted_file(path)?;
    anyhow::ensure!(fs::metadata(path)?.len() <= 129, "saved identity too large");
    let saved = fs::read_to_string(path)?;
    let runtime = fs::read_to_string("/etc/machine-id")?;
    enrollment::machine_id_digest()?;
    anyhow::ensure!(saved.trim() == runtime.trim(), "saved identity differs");
    Ok(())
}

async fn directory_service_active() -> bool {
    tokio::time::timeout(Duration::from_secs(5), async {
        tokio::process::Command::new("/usr/bin/systemctl")
            .args(["is-active", "--quiet", "samba-ad-dc.service"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .status()
            .await
            .is_ok_and(|status| status.success())
    })
    .await
    .unwrap_or(false)
}

pub async fn command() -> Result<()> {
    enrollment::require_root()?;
    let mut report = Report::new();
    report.add(
        "installed_appliance",
        enrollment::require_trusted_file(Path::new("/etc/smolofis-release")).is_ok(),
        "Run production setup on an installed SmolOfis appliance.",
    );
    report.add(
        "saved_machine_identity",
        saved_identity().is_ok(),
        "Initialize or repair the persistent machine identity before enrollment.",
    );
    report.add(
        "local_directory_service",
        directory_service_active().await,
        "Provision and validate the server-hosted Samba AD directory.",
    );
    let panel = fs::read_to_string("/etc/passwd")
        .ok()
        .zip(fs::read_to_string("/etc/group").ok())
        .and_then(|(passwd, group)| account(&passwd, &group));
    report.add(
        "panel_account",
        panel.is_some(),
        "Create the dedicated non-root smolofis-panel account and matching primary group.",
    );
    let bytes = enrollment::read_configuration(Path::new(CONFIG)).ok();
    let configuration = bytes
        .as_ref()
        .and_then(|bytes| serde_json::from_slice::<Configuration>(bytes).ok());
    report.add(
        "directory_configuration",
        configuration.is_some(),
        "Install a root-owned, bounded panel-auth.json with the office directory settings.",
    );
    let mut origin = None;
    let mut directory = None;
    let mut readable = false;
    if let Some(config) = configuration {
        origin = crate::transport::private_host(&config.origin).ok();
        if let Some((_, gid)) = panel {
            readable = panel_can_read(Path::new(CONFIG), gid).is_ok()
                && panel_can_read(&config.directory.ca_file, gid).is_ok()
                && reader_secret(&config.directory.reader_password_file, gid).is_ok();
        }
        // Never read credential material until all its trusted-path/access checks pass.
        if readable {
            directory = AdDirectory::new(config.directory).ok();
        }
    }
    report.add(
        "private_https_origin",
        origin.is_some(),
        "Set the canonical HTTPS origin to the production node's MagicDNS hostname under ts.net.",
    );
    report.add("directory_material", readable && directory.is_some(), "Install a valid trusted CA and a private reader secret readable by the panel account; reject links or untrusted paths.");
    let connected = if let Some(directory) = directory {
        tokio::time::timeout(Duration::from_secs(5), directory.probe())
            .await
            .is_ok_and(|result| result.is_ok())
    } else {
        false
    };
    report.add("directory_reader_and_group", connected, "Verify LDAPS, reader credentials, the configured directory naming context and administrator group.");
    let enrolled = bytes.as_ref().is_some_and(|bytes| {
        enrollment::Gate::load(Path::new(STATE), Path::new(CONFIG), bytes)
            .and_then(|gate| gate.generation())
            .is_ok()
    }) && panel
        .is_some_and(|(_, gid)| panel_can_read(Path::new(STATE), gid).is_ok());
    report.add("device_enrollment", enrolled, "Enroll an existing office directory administrator and verify that enrollment is unlocked and readable by the panel.");
    report.add(
        "local_audit",
        root_log(Path::new("/var/lib/smolofis/local-audit/events.jsonl")).is_ok(),
        "Initialize the protected root local audit log.",
    );
    report.add("panel_audit", panel.is_some_and(|(uid, gid)| panel_log(Path::new("/var/lib/smolofis/panel-audit/auth.jsonl"), uid, gid).is_ok()), "Provision the single panel-owned 0600 authentication log under a trusted root-owned directory.");
    report.add("operation_journal", crate::operations::worker_read(Path::new("/var/lib/smolofis/operations"), None).is_ok(), "Initialize or inspect the protected operation journal; this check never creates or recovers jobs.");
    let units = [
        "smolofis-console-preview.service",
        "smolofis-console-https-preview.service",
        "smolofis-admin-worker.service",
        "smolofis-console-preview.service.d/audit.conf",
        "smolofis-console-preview.service.d/management.conf",
        "smolofis-admin-worker.service.d/journal.conf",
    ];
    report.add("service_files", units.iter().all(|unit| enrollment::require_trusted_file(&Path::new("/etc/systemd/system").join(unit)).is_ok())
        && panel.is_some_and(|(uid, _)| worker_uid(uid).is_ok()), "Install the reviewed candidate units/drop-ins and root-owned admin-worker.env with the exact panel UID; file trust alone does not validate service contents.");
    let tailnet = if let Some(host) = origin {
        match tokio::try_join!(
            crate::transport::tailscale_json(&["status", "--json"]),
            crate::transport::tailscale_json(&["serve", "status", "--json"])
        ) {
            Ok((node, serve)) => {
                crate::transport::validate_tailnet(&node, &serve, &host).is_ok()
                    && node["CertDomains"].as_array().is_some_and(|domains| {
                        domains
                            .iter()
                            .any(|domain| domain.as_str() == Some(host.as_str()))
                    })
            }
            Err(_) => false,
        }
    } else {
        false
    };
    report.add("private_transport_prerequisites", tailnet, "Join the intended production tailnet, enable HTTPS certificates, and leave existing Serve/Funnel configuration untouched and empty.");
    report.finish();
    println!("{}", serde_json::to_string(&report)?);
    anyhow::ensure!(
        report.prerequisites_passed,
        "production console prerequisites are incomplete; no changes made"
    );
    Ok(())
}

fn worker_uid(uid: u32) -> Result<()> {
    let path = Path::new("/etc/smolofis/admin-worker.env");
    let bytes = enrollment::read_configuration(path)?;
    anyhow::ensure!(
        bytes == format!("PANEL_UID={uid}\n").as_bytes(),
        "worker UID differs"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn account_rejects_root_mismatched_and_duplicate_identities() {
        let valid = "smolofis-panel:x:995:994::/nonexistent:/usr/sbin/nologin\n";
        assert_eq!(account(valid, "smolofis-panel:x:994:\n"), Some((995, 994)));
        for passwd in [
            valid.replace(":995:", ":0:"),
            valid.repeat(2),
            "smolofis-panel:x:bad:994::/:/bin/false".into(),
        ] {
            assert!(account(&passwd, "smolofis-panel:x:994:\n").is_none());
        }
        assert!(account(valid, "smolofis-panel:x:0:\n").is_none());
        assert!(account(
            &format!("{valid}alias:x:995:994::/:/bin/false\n"),
            "smolofis-panel:x:994:\n"
        )
        .is_none());
        assert!(account(valid, "smolofis-panel:x:994:other\n").is_none());
        assert!(account(valid, "smolofis-panel:x:994:\nalias:x:994:\n").is_none());
        assert!(account(valid, "smolofis-panel:x:994:\nsmolofis-panel:x:994:\n").is_none());
    }
    #[test]
    fn report_never_claims_readiness_with_a_failed_prerequisite_or_enables_services() {
        let mut report = Report::new();
        report.add("identity", true, "repair identity");
        report.add("directory", false, "provision directory");
        report.finish();
        assert!(!report.prerequisites_passed);
        assert!(!report.administration_enabled_by_check);
        let value = serde_json::to_value(report).unwrap();
        assert_eq!(value["checks"][0]["next_step"], "");
        assert_eq!(value["checks"][1]["next_step"], "provision directory");
    }
}
