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
        let phase = if all_online {
            self.ever_ready.store(true, Ordering::Relaxed);
            BootPhase::Ready
        } else if self.ever_ready.load(Ordering::Relaxed) {
            BootPhase::Degraded
        } else {
            BootPhase::Initializing
        };

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
