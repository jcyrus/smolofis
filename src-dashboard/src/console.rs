//! Experimental, read-only console. Registration is opt-in and the main
//! configuration validator requires a loopback bind until release acceptance.

use std::sync::Arc;

use askama::Template;
use axum::extract::{OriginalUri, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{Html, IntoResponse, Response};
use axum::routing::get;
use axum::Router;
use tracing::error;

use crate::handlers::{fmt_gib, fmt_uptime, pct};
use crate::system::{AppState, BootPhase, ServiceHealth};

const CSS: &[u8] = include_bytes!("../assets/console.css");
const JS: &[u8] = include_bytes!("../assets/console.js");
const PAGES: &[&str] = &[
    "/console",
    "/console/",
    "/console/people",
    "/console/shares",
    "/console/services",
    "/console/settings",
];

pub fn routes(enabled: bool) -> Router<Arc<AppState>> {
    let mut router = Router::new();
    if enabled {
        for path in PAGES {
            router = router.route(path, get(page));
        }
        router = router
            .route("/assets/console.css", get(css))
            .route("/assets/console.js", get(js));
    }
    router
}

struct ServiceView {
    id: &'static str,
    name: &'static str,
    role: &'static str,
    health: &'static str,
    label: &'static str,
    detail: String,
    latency: String,
    impact: &'static str,
    url: Option<String>,
}

#[derive(Template)]
#[template(path = "console.html")]
struct ConsoleTemplate {
    signed_in: bool,
    page: &'static str,
    title: &'static str,
    description: &'static str,
    hostname: String,
    phase: &'static str,
    phase_title: &'static str,
    phase_detail: &'static str,
    phase_label: &'static str,
    online: usize,
    total: usize,
    cpu: String,
    cpu_pct: u8,
    memory: String,
    mem_pct: u8,
    disk: String,
    disk_pct: u8,
    uptime: String,
    image_version: String,
    boot_mode: String,
    tailscale_ip: String,
    services: Vec<ServiceView>,
}

async fn page(
    State(state): State<Arc<AppState>>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
    auth: Option<axum::Extension<Arc<crate::auth::Auth>>>,
) -> Response {
    let (page, title, description) = match uri.path().trim_end_matches('/') {
        "/console" => (
            "home",
            "Office overview",
            "The essentials for keeping your office running.",
        ),
        "/console/shares" => (
            "shares",
            "Shared folders",
            "A home for the files your team works with.",
        ),
        "/console/people" => (
            "people",
            "People & access",
            "Office accounts and the access that comes with them.",
        ),
        "/console/services" => (
            "services",
            "Service health",
            "See what is running and understand what needs attention.",
        ),
        "/console/settings" => (
            "settings",
            "Settings",
            "Your appliance, connections, and appearance.",
        ),
        _ => return StatusCode::NOT_FOUND.into_response(),
    };
    let snap = state.snapshot().await;
    let m = &snap.metrics;
    let sampled = !m.hostname.is_empty();
    let (phase_title, phase_detail, phase_label) = match snap.phase {
        BootPhase::Ready => (
            "Core services are running",
            "Git hosting and deployment tools are responding.",
            "Online",
        ),
        BootPhase::Initializing => (
            "Getting the office ready",
            "Core services are starting. Their status will update automatically.",
            "Starting",
        ),
        BootPhase::Degraded => (
            "A service needs attention",
            "One or more core services are not responding. Check their details to find out more.",
            "Needs attention",
        ),
        BootPhase::Live => (
            "You're running from USB",
            "Install SmolOfis to a disk to start Git hosting and deployment tools.",
            "Live mode",
        ),
    };
    let services: Vec<_> = snap.services.iter().map(|service| {
        let port = match service.id {
            "gitea" => state.config.gitea_public_port,
            "coolify" => state.config.coolify_public_port,
            _ => 0,
        };
        ServiceView {
            id: service.id,
            name: service.name,
            role: service.role,
            health: service.health.as_str(),
            label: match service.health {
                ServiceHealth::Online => "Online",
                ServiceHealth::Pending => "Starting",
                ServiceHealth::Offline => "Unavailable",
            },
            detail: service.detail.clone(),
            latency: service.latency_ms.map(|ms| format!("{ms} ms")).unwrap_or_else(|| "Not measured".into()),
            impact: match service.id {
                "docker" => "The container engine runs Git hosting and deployment services. If it is unavailable, those tools may be unavailable too.",
                "gitea" => "Gitea hosts repositories and the interface for Git workflows. If it is unavailable, your team may not be able to reach its repositories.",
                _ => "Coolify manages application deployments. If it is unavailable, its management interface may not respond; deployed applications have their own health checks.",
            },
            url: (port != 0).then(|| app_url(&headers, port)),
        }
    }).collect();
    let template = ConsoleTemplate {
        signed_in: auth.is_some(),
        page,
        title,
        description,
        hostname: if sampled {
            m.hostname.clone()
        } else {
            "SmolOfis".into()
        },
        phase: snap.phase.as_str(),
        phase_title,
        phase_detail,
        phase_label,
        online: services.iter().filter(|s| s.health == "online").count(),
        total: services.len(),
        cpu: if sampled {
            format!("{:.1}%", m.cpu_percent)
        } else {
            "Checking…".into()
        },
        cpu_pct: if m.cpu_percent.is_finite() {
            m.cpu_percent.clamp(0.0, 100.0).round() as u8
        } else {
            0
        },
        memory: capacity(m.mem_used, m.mem_total),
        mem_pct: pct(m.mem_used, m.mem_total),
        disk: capacity(m.disk_used, m.disk_total),
        disk_pct: pct(m.disk_used, m.disk_total),
        uptime: if sampled {
            fmt_uptime(m.uptime_secs)
        } else {
            "Checking…".into()
        },
        image_version: state.config.image_version.clone(),
        boot_mode: state.config.boot_slot.clone().unwrap_or_else(|| {
            if snap.phase == BootPhase::Live {
                "USB live image".into()
            } else {
                "Development / live image".into()
            }
        }),
        tailscale_ip: m
            .tailscale_ip
            .clone()
            .unwrap_or_else(|| "Not detected".into()),
        services,
    };
    match template.render() {
        Ok(html) => (
            [
                (header::CACHE_CONTROL, "no-store"),
                (header::CONTENT_SECURITY_POLICY, "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
                (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
                (header::REFERRER_POLICY, "same-origin"),
            ],
            Html(html),
        ).into_response(),
        Err(err) => {
            error!(%err, "console template render failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "console render error").into_response()
        }
    }
}

fn capacity(used: u64, total: u64) -> String {
    if total == 0 {
        "Checking…".into()
    } else {
        format!("{} / {} GiB", fmt_gib(used), fmt_gib(total))
    }
}

fn app_url(headers: &HeaderMap, port: u16) -> String {
    let authority = headers
        .get(header::HOST)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<axum::http::uri::Authority>().ok());
    let host = authority
        .as_ref()
        .map(|value| value.host())
        .unwrap_or("localhost");
    let host = if host.contains(':') && !host.starts_with('[') {
        format!("[{host}]")
    } else {
        host.to_string()
    };
    format!("http://{host}:{port}/")
}

async fn css() -> impl IntoResponse {
    (
        [
            (header::CONTENT_TYPE, "text/css; charset=utf-8"),
            (header::CACHE_CONTROL, "no-cache"),
            (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
        ],
        CSS,
    )
}

async fn js() -> impl IntoResponse {
    (
        [
            (header::CONTENT_TYPE, "text/javascript; charset=utf-8"),
            (header::CACHE_CONTROL, "no-cache"),
            (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
        ],
        JS,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn service_links_replace_ports_and_handle_ipv6() {
        for (host, expected) in [
            ("127.0.0.1:8090", "http://127.0.0.1:3000/"),
            ("smolofis.local", "http://smolofis.local:3000/"),
            ("[::1]:8090", "http://[::1]:3000/"),
            ("bad host", "http://localhost:3000/"),
        ] {
            let mut headers = HeaderMap::new();
            headers.insert(header::HOST, host.parse().unwrap());
            assert_eq!(app_url(&headers, 3000), expected);
        }
    }

    #[tokio::test]
    async fn preview_routes_are_absent_by_default_and_render_when_enabled() {
        for enabled in [false, true] {
            let mut config = crate::config::Config::from_env();
            config.console_enabled = enabled;
            config.bind_addr = "127.0.0.1:0".parse().unwrap();
            let state = Arc::new(AppState::new(config));
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let address = listener.local_addr().unwrap();
            let app = routes(enabled).with_state(state);
            let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
            let client = reqwest::Client::new();
            for path in PAGES
                .iter()
                .copied()
                .chain(["/assets/console.css", "/assets/console.js"])
            {
                let response = client
                    .get(format!("http://{address}{path}"))
                    .send()
                    .await
                    .unwrap();
                assert_eq!(
                    response.status(),
                    if enabled {
                        StatusCode::OK
                    } else {
                        StatusCode::NOT_FOUND
                    },
                    "{path}"
                );
                if enabled && path.starts_with("/console") {
                    assert!(response
                        .headers()
                        .contains_key(header::CONTENT_SECURITY_POLICY));
                    let body = response.text().await.unwrap();
                    assert!(body.contains("Read-only preview"));
                    assert!(body.contains("/assets/console.js"));
                }
            }
            server.abort();
        }
    }
}
