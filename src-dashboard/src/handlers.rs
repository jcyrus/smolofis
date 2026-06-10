//! HTTP handlers: the Askama-rendered dashboard, the JSON state API the
//! frontend polls, embedded static assets (stylesheet + fonts, so the
//! appliance stays fully styled offline), and a liveness endpoint for
//! systemd/uptime checks.

use std::sync::Arc;

use askama::Template;
use axum::extract::{Path, State};
use axum::http::{header, StatusCode};
use axum::response::{Html, IntoResponse, Json, Response};
use serde_json::json;
use tracing::error;

use crate::system::{AppState, Metrics, ServiceStatus, Snapshot};

const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Stylesheet (compiled Tailwind + @font-face) baked into the binary so the
/// appliance needs no internet and no loose files on disk.
const APP_CSS: &[u8] = include_bytes!("../assets/app.css");

/// Vendored woff2 font files (latin subset), also baked in.
const FONTS: &[(&str, &[u8])] = &[
    (
        "chakra-petch-500.woff2",
        include_bytes!("../assets/fonts/chakra-petch-500.woff2"),
    ),
    (
        "chakra-petch-600.woff2",
        include_bytes!("../assets/fonts/chakra-petch-600.woff2"),
    ),
    (
        "chakra-petch-700.woff2",
        include_bytes!("../assets/fonts/chakra-petch-700.woff2"),
    ),
    (
        "ibm-plex-mono-400.woff2",
        include_bytes!("../assets/fonts/ibm-plex-mono-400.woff2"),
    ),
    (
        "ibm-plex-mono-500.woff2",
        include_bytes!("../assets/fonts/ibm-plex-mono-500.woff2"),
    ),
    (
        "ibm-plex-mono-600.woff2",
        include_bytes!("../assets/fonts/ibm-plex-mono-600.woff2"),
    ),
];

/// Assets are immutable for a given binary; let browsers cache them hard.
const CACHE_FOREVER: &str = "public, max-age=31536000, immutable";

/// Pre-formatted, template-friendly view of one core service.
struct ServiceCard {
    id: &'static str,
    name: &'static str,
    role: &'static str,
    health: &'static str,
    badge: &'static str,
    detail: String,
    /// Host-mapped port for the quick link; 0 means "no external UI".
    port: u16,
}

#[derive(Template)]
#[template(path = "dashboard.html")]
struct DashboardTemplate {
    phase: &'static str,
    phase_label: &'static str,
    hostname: String,
    version: &'static str,
    cpu: String,
    mem_used: String,
    mem_total: String,
    mem_pct: u8,
    disk_used: String,
    disk_total: String,
    disk_pct: u8,
    uptime: String,
    load_one: String,
    services: Vec<ServiceCard>,
}

/// GET / — server-renders the full dashboard with current values so the
/// first paint is real data; the embedded script keeps it live afterwards.
pub async fn dashboard(State(state): State<Arc<AppState>>) -> Response {
    let snap = state.snapshot().await;
    let m = &snap.metrics;
    let template = DashboardTemplate {
        phase: snap.phase.as_str(),
        phase_label: snap.phase.label(),
        hostname: m.hostname.clone(),
        version: VERSION,
        cpu: format!("{:.1}", m.cpu_percent),
        mem_used: fmt_gib(m.mem_used),
        mem_total: fmt_gib(m.mem_total),
        mem_pct: pct(m.mem_used, m.mem_total),
        disk_used: fmt_gib(m.disk_used),
        disk_total: fmt_gib(m.disk_total),
        disk_pct: pct(m.disk_used, m.disk_total),
        uptime: fmt_uptime(m.uptime_secs),
        load_one: format!("{:.2}", m.load_one),
        services: service_cards(&state, &snap),
    };
    match template.render() {
        Ok(html) => Html(html).into_response(),
        Err(err) => {
            error!(%err, "dashboard template render failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "template render error").into_response()
        }
    }
}

/// GET /api/state — full machine-readable snapshot, polled by the frontend.
pub async fn api_state(State(state): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let snap = state.snapshot().await;
    Json(json!({
        "phase": snap.phase,
        "phase_label": snap.phase.label(),
        "services": snap.services,
        "metrics": api_metrics(&snap.metrics),
        "updated_at": snap.updated_at,
        "panel": { "version": VERSION },
        "links": {
            "gitea_port": state.config.gitea_public_port,
            "coolify_port": state.config.coolify_public_port,
        },
    }))
}

/// GET /healthz — liveness for systemd watchdogs and external monitors.
pub async fn healthz() -> &'static str {
    "ok"
}

/// GET /assets/app.css — the embedded stylesheet.
pub async fn asset_css() -> impl IntoResponse {
    (
        [
            (header::CONTENT_TYPE, "text/css; charset=utf-8"),
            (header::CACHE_CONTROL, CACHE_FOREVER),
        ],
        APP_CSS,
    )
}

/// GET /assets/fonts/{file} — embedded woff2 fonts.
pub async fn asset_font(Path(file): Path<String>) -> Response {
    match FONTS.iter().find(|(name, _)| *name == file) {
        Some((_, bytes)) => (
            [
                (header::CONTENT_TYPE, "font/woff2"),
                (header::CACHE_CONTROL, CACHE_FOREVER),
            ],
            *bytes,
        )
            .into_response(),
        None => (StatusCode::NOT_FOUND, "unknown font asset").into_response(),
    }
}

fn api_metrics(m: &Metrics) -> serde_json::Value {
    json!({
        "cpu_percent": m.cpu_percent,
        "mem_used": m.mem_used,
        "mem_total": m.mem_total,
        "mem_pct": pct(m.mem_used, m.mem_total),
        "mem_used_h": fmt_gib(m.mem_used),
        "mem_total_h": fmt_gib(m.mem_total),
        "disk_used": m.disk_used,
        "disk_total": m.disk_total,
        "disk_pct": pct(m.disk_used, m.disk_total),
        "disk_used_h": fmt_gib(m.disk_used),
        "disk_total_h": fmt_gib(m.disk_total),
        "uptime_secs": m.uptime_secs,
        "uptime_h": fmt_uptime(m.uptime_secs),
        "load_one": m.load_one,
        "hostname": m.hostname,
    })
}

fn service_cards(state: &AppState, snap: &Snapshot) -> Vec<ServiceCard> {
    snap.services
        .iter()
        .map(|s: &ServiceStatus| ServiceCard {
            id: s.id,
            name: s.name,
            role: s.role,
            health: s.health.as_str(),
            badge: match s.health.as_str() {
                "online" => "ONLINE",
                "offline" => "OFFLINE",
                _ => "STARTING",
            },
            detail: s.detail.clone(),
            port: match s.id {
                "gitea" => state.config.gitea_public_port,
                "coolify" => state.config.coolify_public_port,
                _ => 0,
            },
        })
        .collect()
}

fn pct(used: u64, total: u64) -> u8 {
    if total == 0 {
        return 0;
    }
    ((used as f64 / total as f64) * 100.0)
        .round()
        .clamp(0.0, 100.0) as u8
}

fn fmt_gib(bytes: u64) -> String {
    format!("{:.1}", bytes as f64 / 1_073_741_824.0)
}

fn fmt_uptime(secs: u64) -> String {
    let days = secs / 86_400;
    let hours = (secs % 86_400) / 3_600;
    let mins = (secs % 3_600) / 60;
    if days > 0 {
        format!("{days}d {hours}h {mins}m")
    } else if hours > 0 {
        format!("{hours}h {mins}m")
    } else {
        format!("{mins}m {}s", secs % 60)
    }
}
