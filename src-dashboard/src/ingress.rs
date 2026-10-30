//! Ingress routing layer for the SmolOfis appliance.
//!
//! Owns port 80 traffic dispatch: requests intended for the appliance panel
//! (`smolofis.local`, IP addresses, `localhost`) are served by the local Axum
//! dashboard routes, while any other domain or subdomain (e.g. `app.smolofis.local`,
//! `myproject.org`) is dynamically forwarded to the upstream application proxy
//! (Coolify's Traefik on port 8088).

use std::sync::Arc;

use axum::extract::{Request, State};
use axum::http::{header, HeaderName, HeaderValue, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use tracing::warn;

use crate::system::AppState;

/// Determines whether an incoming request's `Host` header belongs to the
/// SmolOfis control panel or an application deployed in the ecosystem.
pub fn is_panel_host(host_header: Option<&str>, appliance_hostname: &str) -> bool {
    let host = match host_header {
        Some(h) => h.split(':').next().unwrap_or("").trim().to_lowercase(),
        None => return true, // Default to panel if Host header is missing
    };

    if host.is_empty()
        || host == "localhost"
        || host == "127.0.0.1"
        || host == "::1"
        || host == "smolofis"
        || host == "smolofis.local"
        || host == appliance_hostname.to_lowercase()
        || host == format!("{}.local", appliance_hostname.to_lowercase())
    {
        return true;
    }

    // Direct IP address access always lands on the management cockpit
    if host.parse::<std::net::IpAddr>().is_ok() {
        return true;
    }

    false
}

/// Axum middleware layer that dispatches traffic between the local panel
/// and the upstream container application proxy.
pub async fn ingress_dispatch(
    State(state): State<Arc<AppState>>,
    req: Request,
    next: Next,
) -> Response {
    let host_header = req
        .headers()
        .get(header::HOST)
        .and_then(|v| v.to_str().ok());

    let snap = state.snapshot().await;
    let hostname = &snap.metrics.hostname;

    if let Some(target_base) = &state.config.app_proxy_url {
        if !is_panel_host(host_header, hostname) {
            return forward_to_proxy(&state.http, target_base, req).await;
        }
    }

    next.run(req).await
}

/// Forwards an HTTP request to the upstream application proxy (Coolify/Traefik).
async fn forward_to_proxy(client: &reqwest::Client, target_base: &str, req: Request) -> Response {
    let method = req.method().clone();
    let uri = req.uri().clone();
    let path_and_query = uri.path_and_query().map(|pq| pq.as_str()).unwrap_or("/");
    let target_url = format!("{}{}", target_base.trim_end_matches('/'), path_and_query);

    let reqwest_method = match reqwest::Method::from_bytes(method.as_str().as_bytes()) {
        Ok(m) => m,
        Err(_) => {
            return (StatusCode::METHOD_NOT_ALLOWED, "unsupported HTTP method").into_response();
        }
    };

    let mut req_builder = client.request(reqwest_method, &target_url);

    // Forward original request headers, preserving Host and routing metadata
    for (name, value) in req.headers() {
        if name != header::CONNECTION && name != header::UPGRADE {
            if let Ok(v) = reqwest::header::HeaderValue::from_bytes(value.as_bytes()) {
                req_builder = req_builder.header(name.as_str(), v);
            }
        }
    }

    req_builder = req_builder.header("X-Forwarded-Proto", "http");

    // Read body (up to 16 MiB payload limit)
    let body_bytes = match axum::body::to_bytes(req.into_body(), 16 * 1024 * 1024).await {
        Ok(bytes) => bytes,
        Err(err) => {
            return (
                StatusCode::BAD_REQUEST,
                format!("Failed to read request payload: {err}"),
            )
                .into_response();
        }
    };

    if !body_bytes.is_empty() {
        req_builder = req_builder.body(body_bytes);
    }

    match req_builder.send().await {
        Ok(resp) => {
            let status = StatusCode::from_u16(resp.status().as_u16())
                .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
            let mut response_builder = Response::builder().status(status);

            for (name, value) in resp.headers() {
                if name != reqwest::header::TRANSFER_ENCODING {
                    if let Ok(k) = HeaderName::from_bytes(name.as_str().as_bytes()) {
                        if let Ok(v) = HeaderValue::from_bytes(value.as_bytes()) {
                            response_builder = response_builder.header(k, v);
                        }
                    }
                }
            }

            match resp.bytes().await {
                Ok(bytes) => response_builder
                    .body(axum::body::Body::from(bytes))
                    .unwrap_or_else(|_| {
                        (
                            StatusCode::INTERNAL_SERVER_ERROR,
                            "failed to assemble upstream proxy response",
                        )
                            .into_response()
                    }),
                Err(err) => (
                    StatusCode::BAD_GATEWAY,
                    format!("failed reading response from application proxy: {err}"),
                )
                    .into_response(),
            }
        }
        Err(err) => {
            warn!(%target_url, %err, "upstream application proxy unreachable");
            (
                StatusCode::BAD_GATEWAY,
                format!(
                    "SmolOfis Ingress Gateway: Cannot connect to upstream application proxy at {target_base}.\n\
                     Reason: {err}\n\n\
                     If you are deploying applications in Coolify, ensure your proxy is active on port 8088."
                ),
            )
                .into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use axum::routing::get;
    use axum::Router;
    use tower::ServiceExt;

    #[test]
    fn is_panel_host_accepts_appliance_identities() {
        let cases = [
            (Some("smolofis.local"), true),
            (Some("smolofis.local:80"), true),
            (Some("SMOLOFIS.LOCAL:8080"), true),
            (Some("smolofis"), true),
            (Some("localhost"), true),
            (Some("localhost:3000"), true),
            (Some("127.0.0.1"), true),
            (Some("192.168.1.150"), true),
            (Some("10.0.0.5:80"), true),
            (Some("my-nuc.local"), true),
            (None, true),
        ];

        for (host, expected) in cases {
            assert_eq!(
                is_panel_host(host, "my-nuc"),
                expected,
                "failed for host: {host:?}"
            );
        }
    }

    #[test]
    fn is_panel_host_rejects_application_domains() {
        let cases = [
            "app.smolofis.local",
            "api.smolofis.local:80",
            "coolify.my-nuc.local",
            "blog.example.com",
            "custom-app.internal",
        ];

        for host in cases {
            assert!(
                !is_panel_host(Some(host), "my-nuc"),
                "should have rejected application host: {host}"
            );
        }
    }

    #[tokio::test]
    async fn ingress_dispatches_to_panel_routes_for_panel_host() {
        let app = Router::new()
            .route("/", get(|| async { "welcome to panel" }))
            .layer(axum::middleware::from_fn_with_state(
                Arc::new(AppState::new(crate::config::Config {
                    bind_addr: "127.0.0.1:0".parse().unwrap(),
                    gitea_url: "http://127.0.0.1:0".to_string(),
                    gitea_health_path: "/api/healthz".to_string(),
                    coolify_url: "http://127.0.0.1:0".to_string(),
                    coolify_health_path: "/api/health".to_string(),
                    docker_socket: std::path::PathBuf::from("/nonexistent.sock"),
                    poll_interval: std::time::Duration::from_secs(3),
                    gitea_public_port: 3000,
                    coolify_public_port: 8000,
                    app_proxy_url: Some("http://127.0.0.1:8088".to_string()),
                })),
                ingress_dispatch,
            ));

        let req = Request::builder()
            .uri("/")
            .header("Host", "smolofis.local")
            .body(Body::empty())
            .unwrap();

        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 1024).await.unwrap();
        assert_eq!(&body[..], b"welcome to panel");
    }

    #[tokio::test]
    async fn ingress_forwards_application_domains_to_upstream_proxy() {
        let upstream = Router::new().route("/hello", get(|| async { "proxied from coolify" }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let upstream_addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, upstream).await.unwrap();
        });

        let app = Router::new()
            .route("/", get(|| async { "panel dashboard" }))
            .layer(axum::middleware::from_fn_with_state(
                Arc::new(AppState::new(crate::config::Config {
                    bind_addr: "127.0.0.1:0".parse().unwrap(),
                    gitea_url: "http://127.0.0.1:0".to_string(),
                    gitea_health_path: "/api/healthz".to_string(),
                    coolify_url: "http://127.0.0.1:0".to_string(),
                    coolify_health_path: "/api/health".to_string(),
                    docker_socket: std::path::PathBuf::from("/nonexistent.sock"),
                    poll_interval: std::time::Duration::from_secs(3),
                    gitea_public_port: 3000,
                    coolify_public_port: 8000,
                    app_proxy_url: Some(format!("http://{upstream_addr}")),
                })),
                ingress_dispatch,
            ));

        let req = Request::builder()
            .uri("/hello")
            .header("Host", "cool-app.smolofis.local")
            .body(Body::empty())
            .unwrap();

        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 1024).await.unwrap();
        assert_eq!(&body[..], b"proxied from coolify");
    }
}
