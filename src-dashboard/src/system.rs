//! State engine for the SmolOfis panel.
//!
//! Owns the boot-phase state machine (`Initializing` -> `Ready`, with
//! `Degraded` if a previously healthy service drops), host telemetry via
//! `sysinfo`, and the health probes for the Docker engine, Gitea, and
//! Coolify. A single background task refreshes everything on a fixed
//! cadence; HTTP handlers only ever read the latest immutable snapshot.

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use sysinfo::{CpuRefreshKind, Disks, MemoryRefreshKind, RefreshKind, System};
use tokio::sync::RwLock;
use tracing::{info, warn};

use crate::config::Config;

/// Where the appliance is in its boot lifecycle.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum BootPhase {
    /// Cold boot: core services have not all come online yet.
    Initializing,
    /// Every core service is healthy.
    Ready,
    /// The appliance was `Ready` at some point, but a core service dropped.
    Degraded,
}

impl BootPhase {
    pub fn as_str(self) -> &'static str {
        match self {
            BootPhase::Initializing => "initializing",
            BootPhase::Ready => "ready",
            BootPhase::Degraded => "degraded",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            BootPhase::Initializing => "INITIALIZING",
            BootPhase::Ready => "OPERATIONAL",
            BootPhase::Degraded => "DEGRADED",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ServiceHealth {
    /// Not probed successfully yet (normal during early boot).
    Pending,
    Online,
    Offline,
}

impl ServiceHealth {
    pub fn as_str(self) -> &'static str {
        match self {
            ServiceHealth::Pending => "pending",
            ServiceHealth::Online => "online",
            ServiceHealth::Offline => "offline",
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ServiceStatus {
    pub id: &'static str,
    pub name: &'static str,
    pub role: &'static str,
    pub health: ServiceHealth,
    pub detail: String,
    pub latency_ms: Option<u64>,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct Metrics {
    pub cpu_percent: f32,
    pub mem_used: u64,
    pub mem_total: u64,
    pub disk_used: u64,
    pub disk_total: u64,
    pub uptime_secs: u64,
    pub load_one: f64,
    pub hostname: String,
}

/// Immutable view of the whole appliance, swapped atomically each cycle.
#[derive(Clone, Debug, Serialize)]
pub struct Snapshot {
    pub phase: BootPhase,
    pub services: Vec<ServiceStatus>,
    pub metrics: Metrics,
    pub updated_at: u64,
}

impl Snapshot {
    fn boot_default() -> Self {
        let pending = |id, name, role| ServiceStatus {
            id,
            name,
            role,
            health: ServiceHealth::Pending,
            detail: "waiting for first probe".to_string(),
            latency_ms: None,
        };
        Self {
            phase: BootPhase::Initializing,
            services: vec![
                pending("docker", "Docker Engine", "Container runtime"),
                pending("gitea", "Gitea", "Git hosting & CI/CD"),
                pending("coolify", "Coolify", "PaaS & deployments"),
            ],
            metrics: Metrics::default(),
            updated_at: epoch_secs(),
        }
    }
}

/// Wraps the (synchronous) `sysinfo` handles so the async poller can take a
/// short blocking lock and read fresh telemetry.
struct Monitor {
    sys: System,
}

impl Monitor {
    fn new() -> Self {
        let sys = System::new_with_specifics(
            RefreshKind::nothing()
                .with_cpu(CpuRefreshKind::nothing().with_cpu_usage())
                .with_memory(MemoryRefreshKind::nothing().with_ram()),
        );
        Self { sys }
    }

    fn sample(&mut self) -> Metrics {
        self.sys.refresh_cpu_usage();
        self.sys.refresh_memory();

        // Sum physical disks; skip pseudo-filesystems sysinfo already hides.
        let disks = Disks::new_with_refreshed_list();
        let (disk_total, disk_avail) = disks.list().iter().fold((0u64, 0u64), |(t, a), d| {
            (t + d.total_space(), a + d.available_space())
        });

        Metrics {
            cpu_percent: self.sys.global_cpu_usage(),
            mem_used: self.sys.used_memory(),
            mem_total: self.sys.total_memory(),
            disk_used: disk_total.saturating_sub(disk_avail),
            disk_total,
            uptime_secs: System::uptime(),
            load_one: System::load_average().one,
            hostname: System::host_name().unwrap_or_else(|| "smolofis".to_string()),
        }
    }
}

pub struct AppState {
    pub config: Config,
    http: reqwest::Client,
    snapshot: RwLock<Snapshot>,
    monitor: Mutex<Monitor>,
    /// Latched once all services have been healthy simultaneously; used to
    /// distinguish `Initializing` (never ready) from `Degraded` (was ready).
    ever_ready: AtomicBool,
}

impl AppState {
    pub fn new(config: Config) -> Self {
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(2))
            .timeout(Duration::from_secs(4))
            .user_agent(concat!("smolofis-panel/", env!("CARGO_PKG_VERSION")))
            .build()
            .expect("reqwest client construction cannot fail with static config");
        Self {
            config,
            http,
            snapshot: RwLock::new(Snapshot::boot_default()),
            monitor: Mutex::new(Monitor::new()),
            ever_ready: AtomicBool::new(false),
        }
    }

    pub async fn snapshot(&self) -> Snapshot {
        self.snapshot.read().await.clone()
    }

    /// One full refresh cycle: telemetry + health probes + phase transition.
    async fn refresh(&self) {
        let metrics = {
            let mut monitor = self.monitor.lock().expect("monitor lock poisoned");
            monitor.sample()
        };

        let (docker, gitea, coolify) = tokio::join!(
            self.probe_docker(),
            self.probe_http(
                "gitea",
                "Gitea",
                "Git hosting & CI/CD",
                &self.config.gitea_url,
                &self.config.gitea_health_path,
            ),
            self.probe_http(
                "coolify",
                "Coolify",
                "PaaS & deployments",
                &self.config.coolify_url,
                &self.config.coolify_health_path,
            ),
        );
        let services = vec![docker, gitea, coolify];

        let all_online = services.iter().all(|s| s.health == ServiceHealth::Online);
        let phase = decide_phase(all_online, &self.ever_ready);

        let mut snap = self.snapshot.write().await;
        if snap.phase != phase {
            info!(
                from = snap.phase.as_str(),
                to = phase.as_str(),
                "boot phase transition"
            );
        }
        *snap = Snapshot {
            phase,
            services,
            metrics,
            updated_at: epoch_secs(),
        };
    }

    /// Pings the Docker engine over its unix control socket with a raw
    /// `GET /_ping` — no HTTP client supports unix sockets out of the box,
    /// and the exchange is trivial enough to speak by hand.
    async fn probe_docker(&self) -> ServiceStatus {
        let started = Instant::now();
        let (health, detail) = match ping_docker_socket(&self.config.docker_socket).await {
            Ok(()) => (
                ServiceHealth::Online,
                format!(
                    "engine responding on {}",
                    self.config.docker_socket.display()
                ),
            ),
            Err(reason) => (ServiceHealth::Offline, reason),
        };
        ServiceStatus {
            id: "docker",
            name: "Docker Engine",
            role: "Container runtime",
            latency_ms: (health == ServiceHealth::Online)
                .then(|| started.elapsed().as_millis() as u64),
            health,
            detail,
        }
    }

    async fn probe_http(
        &self,
        id: &'static str,
        name: &'static str,
        role: &'static str,
        base: &str,
        path: &str,
    ) -> ServiceStatus {
        let url = format!("{}{}", base.trim_end_matches('/'), path);
        let started = Instant::now();
        let (health, detail, latency_ms) = match self.http.get(&url).send().await {
            Ok(resp) if resp.status().is_success() => {
                let ms = started.elapsed().as_millis() as u64;
                (
                    ServiceHealth::Online,
                    format!("HTTP {} in {ms}ms", resp.status().as_u16()),
                    Some(ms),
                )
            }
            Ok(resp) => (
                ServiceHealth::Offline,
                format!("unexpected HTTP {} from {url}", resp.status().as_u16()),
                None,
            ),
            Err(err) => (ServiceHealth::Offline, probe_error_summary(&err), None),
        };
        if health == ServiceHealth::Offline {
            warn!(service = id, %url, %detail, "health probe failed");
        }
        ServiceStatus {
            id,
            name,
            role,
            health,
            detail,
            latency_ms,
        }
    }
}

/// Launches the background refresh loop. Runs for the lifetime of the
/// process; an immediate first tick populates the snapshot right away.
pub fn spawn_poller(state: Arc<AppState>) {
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(state.config.poll_interval);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tick.tick().await;
            state.refresh().await;
        }
    });
}

/// Pure boot-phase transition rule, factored out of [`AppState::refresh`] so
/// it can be exercised in isolation. `ever_ready` latches the first instant
/// every core service is simultaneously online — that latch is what separates
/// a cold boot still coming up (`Initializing`) from a healthy appliance that
/// later lost a service (`Degraded`).
fn decide_phase(all_online: bool, ever_ready: &AtomicBool) -> BootPhase {
    if all_online {
        ever_ready.store(true, Ordering::Relaxed);
        BootPhase::Ready
    } else if ever_ready.load(Ordering::Relaxed) {
        BootPhase::Degraded
    } else {
        BootPhase::Initializing
    }
}

async fn ping_docker_socket(socket: &Path) -> Result<(), String> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    if !socket.exists() {
        return Err(format!("socket {} does not exist yet", socket.display()));
    }
    let connect = tokio::net::UnixStream::connect(socket);
    let mut stream = tokio::time::timeout(Duration::from_secs(2), connect)
        .await
        .map_err(|_| "timed out connecting to docker socket".to_string())?
        .map_err(|e| format!("cannot connect to docker socket: {e}"))?;

    stream
        .write_all(b"GET /_ping HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n")
        .await
        .map_err(|e| format!("write to docker socket failed: {e}"))?;

    let mut response = Vec::with_capacity(256);
    let read = stream.read_to_end(&mut response);
    tokio::time::timeout(Duration::from_secs(2), read)
        .await
        .map_err(|_| "timed out reading docker ping response".to_string())?
        .map_err(|e| format!("read from docker socket failed: {e}"))?;

    let head = String::from_utf8_lossy(&response);
    if head.starts_with("HTTP/1.1 200") || head.starts_with("HTTP/1.0 200") {
        Ok(())
    } else {
        Err(format!(
            "docker ping returned: {}",
            head.lines().next().unwrap_or("<empty response>")
        ))
    }
}

fn probe_error_summary(err: &reqwest::Error) -> String {
    if err.is_connect() {
        "connection refused (service not listening yet)".to_string()
    } else if err.is_timeout() {
        "probe timed out".to_string()
    } else {
        format!("probe failed: {err}")
    }
}

fn epoch_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::StatusCode;
    use axum::routing::get;
    use axum::Router;
    use std::net::SocketAddr;
    use std::path::PathBuf;

    fn base_config() -> Config {
        Config {
            bind_addr: "127.0.0.1:0".parse().unwrap(),
            gitea_url: "http://127.0.0.1:0".to_string(),
            gitea_health_path: "/api/healthz".to_string(),
            coolify_url: "http://127.0.0.1:0".to_string(),
            coolify_health_path: "/api/health".to_string(),
            docker_socket: PathBuf::from("/nonexistent/smolofis-docker.sock"),
            poll_interval: Duration::from_secs(3),
            gitea_public_port: 3000,
            coolify_public_port: 8000,
        }
    }

    // ---- boot-phase state machine -----------------------------------------

    #[test]
    fn phase_starts_initializing_before_first_ready() {
        let latch = AtomicBool::new(false);
        assert_eq!(decide_phase(false, &latch), BootPhase::Initializing);
        assert!(!latch.load(Ordering::Relaxed));
    }

    #[test]
    fn phase_becomes_ready_and_latches() {
        let latch = AtomicBool::new(false);
        assert_eq!(decide_phase(true, &latch), BootPhase::Ready);
        assert!(latch.load(Ordering::Relaxed), "ready must latch ever_ready");
    }

    #[test]
    fn phase_degrades_only_after_having_been_ready() {
        let latch = AtomicBool::new(false);
        assert_eq!(decide_phase(true, &latch), BootPhase::Ready);
        // A service drops after the appliance was healthy -> Degraded, not back
        // to Initializing.
        assert_eq!(decide_phase(false, &latch), BootPhase::Degraded);
    }

    #[test]
    fn phase_recovers_from_degraded_to_ready() {
        let latch = AtomicBool::new(false);
        decide_phase(true, &latch);
        assert_eq!(decide_phase(false, &latch), BootPhase::Degraded);
        assert_eq!(decide_phase(true, &latch), BootPhase::Ready);
    }

    // ---- serde / string contract the frontend + JSON API depend on --------

    #[test]
    fn boot_phase_json_matches_as_str() {
        for phase in [
            BootPhase::Initializing,
            BootPhase::Ready,
            BootPhase::Degraded,
        ] {
            assert_eq!(
                serde_json::to_value(phase).unwrap(),
                serde_json::Value::String(phase.as_str().to_string()),
            );
        }
    }

    #[test]
    fn service_health_json_matches_as_str() {
        for health in [
            ServiceHealth::Pending,
            ServiceHealth::Online,
            ServiceHealth::Offline,
        ] {
            assert_eq!(
                serde_json::to_value(health).unwrap(),
                serde_json::Value::String(health.as_str().to_string()),
            );
        }
    }

    #[test]
    fn boot_default_snapshot_lists_three_pending_services() {
        let snap = Snapshot::boot_default();
        assert_eq!(snap.phase, BootPhase::Initializing);
        assert_eq!(snap.services.len(), 3);
        assert!(snap
            .services
            .iter()
            .all(|s| s.health == ServiceHealth::Pending));
        let ids: Vec<_> = snap.services.iter().map(|s| s.id).collect();
        assert_eq!(ids, ["docker", "gitea", "coolify"]);
    }

    // ---- HTTP health probe ------------------------------------------------

    async fn spawn_status_server(code: StatusCode) -> SocketAddr {
        let app = Router::new().route("/health", get(move || async move { code }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        addr
    }

    #[tokio::test]
    async fn probe_http_reports_online_on_success() {
        let addr = spawn_status_server(StatusCode::OK).await;
        let mut cfg = base_config();
        cfg.gitea_url = format!("http://{addr}");
        let state = AppState::new(cfg);

        let status = state
            .probe_http(
                "gitea",
                "Gitea",
                "Git hosting",
                &state.config.gitea_url,
                "/health",
            )
            .await;

        assert_eq!(status.health, ServiceHealth::Online);
        assert!(status.latency_ms.is_some());
    }

    #[tokio::test]
    async fn probe_http_reports_offline_on_5xx() {
        let addr = spawn_status_server(StatusCode::INTERNAL_SERVER_ERROR).await;
        let mut cfg = base_config();
        cfg.gitea_url = format!("http://{addr}");
        let state = AppState::new(cfg);

        let status = state
            .probe_http(
                "gitea",
                "Gitea",
                "Git hosting",
                &state.config.gitea_url,
                "/health",
            )
            .await;

        assert_eq!(status.health, ServiceHealth::Offline);
        assert!(status.latency_ms.is_none());
        assert!(status.detail.contains("500"));
    }

    #[tokio::test]
    async fn probe_http_reports_offline_when_nothing_listens() {
        // Bind to grab a free port, then drop it so the connection is refused.
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let mut cfg = base_config();
        cfg.gitea_url = format!("http://{addr}");
        let state = AppState::new(cfg);

        let status = state
            .probe_http(
                "gitea",
                "Gitea",
                "Git hosting",
                &state.config.gitea_url,
                "/health",
            )
            .await;

        assert_eq!(status.health, ServiceHealth::Offline);
    }

    // ---- Docker unix-socket ping ------------------------------------------

    fn temp_socket_path(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!("smolofis-{}-{}.sock", tag, std::process::id()))
    }

    /// Accepts exactly one connection and replies with the given raw HTTP
    /// status line, mimicking the Docker engine's `/_ping` endpoint.
    fn spawn_socket_responder(path: PathBuf, response: &'static [u8]) {
        let listener = tokio::net::UnixListener::bind(&path).unwrap();
        tokio::spawn(async move {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            if let Ok((mut stream, _)) = listener.accept().await {
                let mut scratch = [0u8; 256];
                let _ = stream.read(&mut scratch).await;
                let _ = stream.write_all(response).await;
            }
        });
    }

    #[tokio::test]
    async fn docker_ping_ok_on_200() {
        let path = temp_socket_path("ping-ok");
        let _ = std::fs::remove_file(&path);
        spawn_socket_responder(
            path.clone(),
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK",
        );

        let result = ping_docker_socket(&path).await;
        std::fs::remove_file(&path).ok();
        assert!(result.is_ok(), "expected Ok, got {result:?}");
    }

    #[tokio::test]
    async fn docker_ping_errors_on_non_200() {
        let path = temp_socket_path("ping-500");
        let _ = std::fs::remove_file(&path);
        spawn_socket_responder(path.clone(), b"HTTP/1.1 500 Internal Server Error\r\n\r\n");

        let result = ping_docker_socket(&path).await;
        std::fs::remove_file(&path).ok();
        let err = result.expect_err("non-200 must be an error");
        assert!(
            err.contains("500"),
            "error should surface the status: {err}"
        );
    }

    #[tokio::test]
    async fn docker_ping_errors_when_socket_missing() {
        let result = ping_docker_socket(Path::new("/nonexistent/smolofis-missing.sock")).await;
        let err = result.expect_err("missing socket must be an error");
        assert!(err.contains("does not exist"), "got: {err}");
    }
}
