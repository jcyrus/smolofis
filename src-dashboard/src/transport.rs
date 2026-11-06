//! Read-only prerequisite checks for the opt-in Tailscale HTTPS preview unit.
use anyhow::{Context, Result};
use axum::http::header;
use serde_json::Value;
use std::{path::Path, process::Stdio, time::Duration};
use tokio::io::AsyncReadExt;

const BACKEND: &str = "http://127.0.0.1:8091";
const COMMAND_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_OUTPUT: u64 = 1024 * 1024;

pub(crate) fn private_host(origin: &str) -> Result<String> {
    let url = url::Url::parse(origin)?;
    let host = url.host_str().context("HTTPS origin needs a hostname")?;
    anyhow::ensure!(
        url.scheme() == "https"
            && url.port_or_known_default() == Some(443)
            && url.origin().ascii_serialization() == origin,
        "private console requires a canonical HTTPS origin on port 443"
    );
    let labels: Vec<_> = host.split('.').collect();
    anyhow::ensure!(
        host.ends_with(".ts.net")
            && labels.len() >= 4
            && host.len() <= 253
            && labels.iter().all(|label| !label.is_empty()
                && label.len() <= 63
                && !label.starts_with('-')
                && !label.ends_with('-')
                && label
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-')),
        "private console requires this node's MagicDNS name under ts.net"
    );
    Ok(host.to_owned())
}

fn validate_tailnet(node: &Value, serve: &Value, host: &str) -> Result<()> {
    anyhow::ensure!(
        node.get("BackendState").and_then(Value::as_str) == Some("Running")
            && node.pointer("/Self/Online").and_then(Value::as_bool) == Some(true),
        "Tailscale node is not running and online"
    );
    let dns = node
        .pointer("/Self/DNSName")
        .and_then(Value::as_str)
        .context("Tailscale node has no MagicDNS name")?;
    anyhow::ensure!(
        dns.trim_end_matches('.').eq_ignore_ascii_case(host),
        "authentication origin does not match this Tailscale node"
    );
    if serve.is_null() {
        return Ok(());
    }
    let fields = serve
        .as_object()
        .context("unsupported Tailscale Serve status format")?;
    for (name, value) in fields {
        anyhow::ensure!(
            ["TCP", "Web", "AllowFunnel", "Foreground", "Services"].contains(&name.as_str()),
            "unknown Tailscale Serve configuration field; inspect it manually"
        );
        anyhow::ensure!(
            value.is_null() || value.as_object().is_some_and(|object| object.is_empty()),
            "existing Serve/Funnel configuration found; refusing to share or replace it"
        );
    }
    Ok(())
}

async fn tailscale_json(args: &[&str]) -> Result<Value> {
    tokio::time::timeout(COMMAND_TIMEOUT, async {
        let mut child = tokio::process::Command::new("/usr/bin/tailscale")
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .context("start local Tailscale status command")?;
        let mut bytes = Vec::new();
        child
            .stdout
            .take()
            .context("missing command stdout")?
            .take(MAX_OUTPUT + 1)
            .read_to_end(&mut bytes)
            .await?;
        anyhow::ensure!(
            bytes.len() as u64 <= MAX_OUTPUT,
            "Tailscale status exceeds size limit"
        );
        anyhow::ensure!(
            child.wait().await?.success(),
            "Tailscale status command failed"
        );
        serde_json::from_slice(&bytes).context("invalid Tailscale status JSON")
    })
    .await
    .context("Tailscale status command timed out")?
}

async fn validate_backend(backend: &str, host: &str) -> Result<()> {
    // Only the fixed loopback target is passed by production callers. A custom
    // endpoint here exists solely so socket tests can bind an ephemeral port.
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(3))
        .build()?;
    // Type=exec systemd ordering guarantees exec, not listener readiness.
    // Retry connection refusal briefly; an insecure responding backend fails
    // immediately instead of waiting for a different process to replace it.
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    let telemetry = loop {
        match client
            .get(format!("{backend}/api/state"))
            .header(header::HOST, host)
            .send()
            .await
        {
            Ok(response) => break response,
            Err(error) if error.is_connect() && tokio::time::Instant::now() < deadline => {
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            Err(error) => return Err(error.into()),
        }
    };
    anyhow::ensure!(
        telemetry.status() == reqwest::StatusCode::UNAUTHORIZED,
        "loopback backend must deny unauthenticated telemetry"
    );
    let login = client
        .get(format!("{backend}/auth/login"))
        .header(header::HOST, host)
        .send()
        .await?;
    anyhow::ensure!(
        login.status().is_success()
            && login
                .headers()
                .get(header::CACHE_CONTROL)
                .is_some_and(|v| v == "no-store"),
        "loopback backend must serve the configured HTTPS authentication layer"
    );
    let csrf_cookie = login
        .headers()
        .get_all(header::SET_COOKIE)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .any(|value| {
            value.starts_with("smolofis_login=")
                && ["Secure", "HttpOnly", "SameSite=Strict", "Path=/"]
                    .iter()
                    .all(|flag| value.split(';').skip(1).any(|part| part.trim() == *flag))
        });
    anyhow::ensure!(
        csrf_cookie,
        "HTTPS login must issue a Secure, HttpOnly, SameSite=Strict host cookie"
    );
    Ok(())
}

pub async fn check(config: &Path, state: &Path) -> Result<()> {
    crate::enrollment::require_root()?;
    let auth = crate::auth::Auth::load_with_enrollment(config, Some(state))?;
    let origin = auth.private_transport_origin()?;
    let host = private_host(origin)?;
    let (node, serve) = tokio::try_join!(
        tailscale_json(&["status", "--json"]),
        tailscale_json(&["serve", "status", "--json"])
    )?;
    validate_tailnet(&node, &serve, &host)?;
    validate_backend(BACKEND, &host).await?;
    println!(
        "{}",
        serde_json::json!({"origin":origin,"backend":BACKEND,"enrollment":"verified and unlocked","existing_serve":"empty","check":"preview prerequisites only; tailnet/client acceptance still required"})
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn origin_is_https_and_bound_to_the_online_node() {
        let host = "office.tail123.ts.net";
        let node =
            json!({"BackendState":"Running","Self":{"Online":true,"DNSName":format!("{host}.")}});
        assert_eq!(private_host(&format!("https://{host}")).unwrap(), host);
        assert!(validate_tailnet(&node, &Value::Null, host).is_ok());
        assert!(validate_tailnet(&node, &json!({}), host).is_ok());
        for origin in [
            "http://office.tail123.ts.net",
            "https://office.tail123.ts.net:8443",
            "https://office.example.com",
            "https://office.ts.net",
            "https://office.tail123.ts.net/",
            "https://office.tail123.ts.net/path",
        ] {
            assert!(private_host(origin).is_err(), "{origin}");
        }
        assert!(validate_tailnet(&node, &json!({}), "other.tail123.ts.net").is_err());
        assert!(validate_tailnet(&json!({"BackendState":"Stopped"}), &json!({}), host).is_err());
    }
    #[test]
    fn never_shares_or_replaces_existing_serve_or_funnel_configuration() {
        let host = "office.tail123.ts.net";
        let node = json!({"BackendState":"Running","Self":{"Online":true,"DNSName":host}});
        for serve in [
            json!({"AllowFunnel":{"office.tail123.ts.net:443":true}}),
            json!({"TCP":{"443":{"HTTPS":true}}}),
            json!({"Web":{"other:443":{}}}),
            json!({"Foreground":{"session":{}}}),
            json!({"Services":{"svc:other":{}}}),
            json!({"Unknown":{}}),
            json!([]),
        ] {
            assert!(validate_tailnet(&node, &serve, host).is_err());
        }
        assert!(validate_tailnet(&node, &json!({"TCP":{},"Web":null}), host).is_ok());
    }
    #[tokio::test]
    async fn backend_must_require_authentication_and_issue_secure_cookies() {
        use axum::{routing::get, Router};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let backend = format!("http://{}", listener.local_addr().unwrap());
        let app = Router::new()
            .route("/api/state", get(|| async { "public telemetry" }))
            .route("/auth/login", get(|| async { "public login" }));
        let task = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        assert!(validate_backend(&backend, "office.tail123.ts.net")
            .await
            .is_err());
        task.abort();
    }
}
