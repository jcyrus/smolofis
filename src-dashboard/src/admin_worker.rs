//! Restricted local administration protocol. No mutation operations are enabled yet.
use anyhow::{bail, Context};
use serde::{Deserialize, Serialize};
use std::{
    path::{Path, PathBuf},
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::{UnixListener, UnixStream},
    sync::Semaphore,
};

const MAX_REQUEST_BYTES: usize = 1024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(2);
const MAX_CONNECTIONS: usize = 16;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Request {
    version: u8,
    operation: Operation,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    actor_guid: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    operation_id: Option<String>,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Operation {
    ProtocolStatus,
    IdentityCheck,
    JobStatus,
    JobList,
}

#[derive(Serialize)]
struct Reply {
    version: u8,
    status: &'static str,
    mutations_enabled: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    data: Option<serde_json::Value>,
}

fn request(frame: &[u8]) -> Result<Request, &'static str> {
    let request: Request = serde_json::from_slice(frame).map_err(|_| "invalid_request")?;
    if request.version != 1 {
        return Err("unsupported_version");
    }
    let actor = request
        .actor_guid
        .as_deref()
        .is_some_and(crate::operations::valid_guid);
    let valid = match request.operation {
        Operation::ProtocolStatus => request.actor_guid.is_none() && request.operation_id.is_none(),
        Operation::IdentityCheck | Operation::JobList => actor && request.operation_id.is_none(),
        Operation::JobStatus => {
            actor
                && request.operation_id.as_deref().is_some_and(|id| {
                    id.len() == 64 && id.bytes().all(|byte| byte.is_ascii_hexdigit())
                })
        }
    };
    if !valid {
        return Err("invalid_request");
    }
    Ok(request)
}

fn response(status: &'static str, data: Option<serde_json::Value>) -> Reply {
    Reply {
        version: 1,
        status,
        mutations_enabled: false,
        data,
    }
}

#[cfg(test)]
fn reply(frame: &[u8]) -> Reply {
    match request(frame) {
        Ok(Request {
            operation: Operation::ProtocolStatus,
            ..
        }) => response("ok", None),
        Ok(_) => response("operations_disabled", None),
        Err(status) => response(status, None),
    }
}

async fn dispatch(
    frame: &[u8],
    base: Option<&Path>,
    uid: u32,
    slots: std::sync::Arc<Semaphore>,
) -> Reply {
    let request = match request(frame) {
        Ok(request) => request,
        Err(status) => return response(status, None),
    };
    if matches!(request.operation, Operation::ProtocolStatus) {
        return response("ok", None);
    }
    let Some(base) = base else {
        return response("operations_disabled", None);
    };
    let permit = match slots.try_acquire_owned() {
        Ok(permit) => permit,
        Err(_) => return response("unavailable", None),
    };
    let base = base.to_owned();
    let result = tokio::task::spawn_blocking(move || {
        // A timed-out socket must not release execution capacity while its
        // blocking journal commit is still running.
        let _permit = permit;
        match request.operation {
            Operation::IdentityCheck => crate::operations::worker_run(
                &base,
                request.actor_guid.as_deref().expect("validated actor"),
                uid,
            ),
            Operation::JobStatus => {
                crate::operations::worker_read(&base, request.operation_id.as_deref())
            }
            Operation::JobList => crate::operations::worker_read(&base, None),
            Operation::ProtocolStatus => unreachable!(),
        }
    })
    .await;
    match result {
        Ok(Ok(data)) => response("ok", Some(data)),
        _ => response("unavailable", None),
    }
}

#[cfg(test)]
async fn handle(stream: UnixStream, allowed_uid: u32) -> anyhow::Result<()> {
    handle_with_journal(
        stream,
        allowed_uid,
        None,
        std::sync::Arc::new(Semaphore::new(1)),
    )
    .await
}

async fn handle_with_journal(
    stream: UnixStream,
    allowed_uid: u32,
    base: Option<&Path>,
    slots: std::sync::Arc<Semaphore>,
) -> anyhow::Result<()> {
    // Socket filesystem permissions are the first boundary; peer credentials
    // independently restrict requests to the configured panel account.
    anyhow::ensure!(
        stream.peer_cred()?.uid() == allowed_uid,
        "unauthorized peer"
    );
    let mut reader = BufReader::new(stream);
    let mut frame = Vec::new();
    loop {
        let available = reader.fill_buf().await?;
        if available.is_empty() {
            bail!("incomplete request");
        }
        let end = available.iter().position(|&byte| byte == b'\n');
        let consumed = end.map_or(available.len(), |offset| offset + 1);
        anyhow::ensure!(
            frame.len() + consumed <= MAX_REQUEST_BYTES,
            "request too large"
        );
        frame.extend_from_slice(&available[..consumed]);
        reader.consume(consumed);
        if end.is_some() {
            break;
        }
    }
    let mut response = serde_json::to_vec(&dispatch(&frame, base, allowed_uid, slots).await)?;
    response.push(b'\n');
    reader.get_mut().write_all(&response).await?;
    reader.get_mut().shutdown().await?;
    Ok(())
}

pub async fn serve(path: &Path, allowed_uid: u32) -> anyhow::Result<()> {
    serve_configured(path, allowed_uid, None).await
}

pub async fn serve_configured(
    path: &Path,
    allowed_uid: u32,
    journal: Option<PathBuf>,
) -> anyhow::Result<()> {
    if let Some(base) = &journal {
        crate::enrollment::require_root()?;
        anyhow::ensure!(
            allowed_uid != 0,
            "journal worker requires an unprivileged panel UID"
        );
        crate::operations::worker_ready(base)?;
    }
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    anyhow::ensure!(path.is_absolute(), "worker socket path must be absolute");
    let parent = path
        .parent()
        .context("worker socket requires a parent directory")?;
    let metadata = std::fs::symlink_metadata(parent).context("read worker runtime directory")?;
    anyhow::ensure!(
        metadata.is_dir(),
        "runtime directory must be a real directory"
    );
    anyhow::ensure!(metadata.uid() == 0, "runtime directory must be root-owned");
    anyhow::ensure!(
        metadata.mode() & 0o022 == 0,
        "runtime directory must not be group/world writable"
    );
    // Never unlink an existing socket or follow a symlink supplied at startup.
    // systemd RuntimeDirectory handles cleanup after the worker stops.
    let listener =
        UnixListener::bind(path).context("bind worker socket (refusing existing paths)")?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o660))?;
    tracing::info!("administration worker ready; mutation operations disabled");
    let permits = std::sync::Arc::new(Semaphore::new(MAX_CONNECTIONS));
    let operation_slots = std::sync::Arc::new(Semaphore::new(1));
    loop {
        let (stream, _) = listener.accept().await?;
        let Ok(permit) = permits.clone().try_acquire_owned() else {
            continue;
        };
        let journal = journal.clone();
        let operation_slots = operation_slots.clone();
        tokio::spawn(async move {
            let _permit = permit;
            // Never log request bodies, credentials, or malformed input.
            match tokio::time::timeout(
                REQUEST_TIMEOUT,
                handle_with_journal(stream, allowed_uid, journal.as_deref(), operation_slots),
            )
            .await
            {
                Ok(Ok(())) => (),
                _ => tracing::warn!("administration connection rejected or incomplete"),
            }
        });
    }
}

pub(crate) async fn call(
    operation: Operation,
    actor_guid: String,
    operation_id: Option<String>,
) -> anyhow::Result<serde_json::Value> {
    use std::os::unix::fs::{FileTypeExt, MetadataExt};
    use tokio::io::AsyncReadExt;
    const SOCKET: &str = "/run/smolofis-admin/control.sock";
    const MAX_REPLY: usize = 2 * 1024 * 1024;
    let frame = serde_json::to_vec(&Request {
        version: 1,
        operation,
        actor_guid: Some(actor_guid),
        operation_id,
    })?;
    request(&frame).map_err(|_| anyhow::anyhow!("invalid worker request"))?;
    anyhow::ensure!(frame.len() < MAX_REQUEST_BYTES, "worker request too large");
    tokio::time::timeout(REQUEST_TIMEOUT, async {
        crate::enrollment::require_trusted_directory(Path::new("/run/smolofis-admin"))?;
        let metadata = std::fs::symlink_metadata(SOCKET)?;
        anyhow::ensure!(
            metadata.file_type().is_socket() && metadata.uid() == 0 && metadata.mode() & 0o007 == 0,
            "unsafe worker socket"
        );
        let mut stream = UnixStream::connect(SOCKET).await?;
        anyhow::ensure!(stream.peer_cred()?.uid() == 0, "worker must run as root");
        stream.write_all(&frame).await?;
        stream.write_all(b"\n").await?;
        let mut bytes = Vec::new();
        BufReader::new(stream.take((MAX_REPLY + 1) as u64))
            .read_until(b'\n', &mut bytes)
            .await?;
        anyhow::ensure!(
            bytes.len() <= MAX_REPLY && bytes.last() == Some(&b'\n'),
            "invalid worker response frame"
        );
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct WireReply {
            version: u8,
            status: String,
            mutations_enabled: bool,
            data: serde_json::Value,
        }
        let reply: WireReply = serde_json::from_slice(&bytes)?;
        anyhow::ensure!(
            reply.version == 1 && reply.status == "ok" && !reply.mutations_enabled,
            "worker unavailable"
        );
        Ok(reply.data)
    })
    .await
    .context("worker request timed out")?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn protocol_only_allows_versioned_known_operations() {
        assert_eq!(
            reply(br#"{"version":1,"operation":"protocol_status"}"#).status,
            "ok"
        );
        assert_eq!(
            reply(br#"{"version":2,"operation":"protocol_status"}"#).status,
            "unsupported_version"
        );
        for frame in [
            br#"{"version":1,"operation":"restart","unit":"sshd"}"#.as_slice(),
            br#"{"version":1,"operation":"protocol_status","command":"id"}"#,
            br#"{"version":1,"operation":"protocol_status","version":1}"#,
            br#"{"version":1,"operation":"protocol_status"} {}"#,
            b"not json",
        ] {
            assert_eq!(reply(frame).status, "invalid_request");
        }
    }

    #[test]
    fn journal_protocol_rejects_identity_and_operation_parameter_forgery() {
        let valid = serde_json::json!({"version":1,"operation":"identity_check","actor_guid":"07".repeat(16)});
        assert_eq!(
            reply(&serde_json::to_vec(&valid).unwrap()).status,
            "operations_disabled"
        );
        for (field, value) in [
            ("actor_guid", serde_json::json!("bad")),
            ("operation_id", serde_json::json!("ab".repeat(32))),
            ("requester_uid", serde_json::json!(65534)),
            ("command", serde_json::json!("id")),
        ] {
            let mut input = valid.clone();
            input[field] = value;
            assert_eq!(
                reply(&serde_json::to_vec(&input).unwrap()).status,
                "invalid_request"
            );
        }
        assert_eq!(reply(br#"{"version":1,"operation":"protocol_status","actor_guid":"07070707070707070707070707070707"}"#).status,"invalid_request");
    }

    #[tokio::test]
    async fn busy_execution_capacity_rejects_work_without_waiting() {
        let slots = std::sync::Arc::new(Semaphore::new(1));
        let held = slots.clone().try_acquire_owned().unwrap();
        let frame = serde_json::json!({"version":1,"operation":"identity_check","actor_guid":"07".repeat(16)});
        let reply = dispatch(
            &serde_json::to_vec(&frame).unwrap(),
            Some(Path::new("/unused")),
            65534,
            slots.clone(),
        )
        .await;
        assert_eq!(reply.status, "unavailable");
        assert_eq!(slots.available_permits(), 0);
        drop(held);
        assert_eq!(slots.available_permits(), 1);
    }

    #[tokio::test]
    async fn socket_checks_peer_and_bounds_input() {
        let (mut client, server) = UnixStream::pair().unwrap();
        let uid = server.peer_cred().unwrap().uid();
        let worker = tokio::spawn(handle(server, uid));
        client
            .write_all(b"{\"version\":1,\"operation\":\"protocol_status\"}\n")
            .await
            .unwrap();
        let mut response = String::new();
        BufReader::new(client)
            .read_line(&mut response)
            .await
            .unwrap();
        assert!(response.contains("\"status\":\"ok\""));
        assert!(response.contains("\"mutations_enabled\":false"));
        worker.await.unwrap().unwrap();

        let (_client, server) = UnixStream::pair().unwrap();
        assert!(handle(server, uid.wrapping_add(1)).await.is_err());
        let (mut client, server) = UnixStream::pair().unwrap();
        let worker = tokio::spawn(handle(server, uid));
        client
            .write_all(&vec![b'x'; MAX_REQUEST_BYTES + 1])
            .await
            .unwrap();
        assert!(worker.await.unwrap().is_err());
        let (client, server) = UnixStream::pair().unwrap();
        drop(client);
        assert!(handle(server, uid).await.is_err());
    }

    #[tokio::test]
    async fn incomplete_request_hits_deadline() {
        let (mut client, server) = UnixStream::pair().unwrap();
        let uid = server.peer_cred().unwrap().uid();
        client.write_all(b"{\"version\":1").await.unwrap();
        assert!(tokio::time::timeout(REQUEST_TIMEOUT, handle(server, uid))
            .await
            .is_err());
    }

    #[tokio::test]
    async fn startup_rejects_relative_or_public_runtime_paths() {
        assert!(serve(Path::new("control.sock"), 0).await.is_err());
        // /tmp is writable by other users (or a symlink on macOS). Neither is
        // an acceptable parent for a privileged control socket.
        assert!(
            serve(Path::new("/tmp/smolofis-worker-must-not-bind.sock"), 0)
                .await
                .is_err()
        );
    }
}
