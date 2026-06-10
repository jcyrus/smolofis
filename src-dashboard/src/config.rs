//! Runtime configuration for the SmolOfis panel.
//!
//! Everything is sourced from environment variables with appliance-correct
//! defaults, so the binary runs with zero configuration on a flashed image
//! and can be repointed at mocks during local development (see
//! `scripts/dev-mock.sh`).

use std::net::SocketAddr;
use std::path::PathBuf;
use std::time::Duration;

#[derive(Clone, Debug)]
pub struct Config {
    /// Address the dashboard listens on. Port 80 on the appliance.
    pub bind_addr: SocketAddr,
    /// Internal base URL used to health-check Gitea.
    pub gitea_url: String,
    /// Health endpoint path on the Gitea instance.
    pub gitea_health_path: String,
    /// Internal base URL used to health-check Coolify.
    pub coolify_url: String,
    /// Health endpoint path on the Coolify instance.
    pub coolify_health_path: String,
    /// Docker engine control socket, pinged to verify the engine is up.
    pub docker_socket: PathBuf,
    /// Interval between health/metric refresh cycles.
    pub poll_interval: Duration,
    /// Port the browser should use to reach Gitea (host-mapped).
    pub gitea_public_port: u16,
    /// Port the browser should use to reach Coolify (host-mapped).
    pub coolify_public_port: u16,
}

impl Config {
    pub fn from_env() -> Self {
        Self {
            bind_addr: env_parsed("SMOLOFIS_BIND", SocketAddr::from(([0, 0, 0, 0], 80))),
            gitea_url: env_or("SMOLOFIS_GITEA_URL", "http://127.0.0.1:3000"),
            gitea_health_path: env_or("SMOLOFIS_GITEA_HEALTH_PATH", "/api/healthz"),
            coolify_url: env_or("SMOLOFIS_COOLIFY_URL", "http://127.0.0.1:8000"),
            coolify_health_path: env_or("SMOLOFIS_COOLIFY_HEALTH_PATH", "/api/health"),
            docker_socket: PathBuf::from(env_or("SMOLOFIS_DOCKER_SOCKET", "/var/run/docker.sock")),
            poll_interval: Duration::from_secs(env_parsed("SMOLOFIS_POLL_INTERVAL_SECS", 3u64)),
            gitea_public_port: env_parsed("SMOLOFIS_GITEA_PUBLIC_PORT", 3000u16),
            coolify_public_port: env_parsed("SMOLOFIS_COOLIFY_PUBLIC_PORT", 8000u16),
        }
    }
}

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key)
        .ok()
        .filter(|v| !v.trim().is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn env_parsed<T: std::str::FromStr>(key: &str, default: T) -> T {
    match std::env::var(key) {
        Ok(raw) => raw.trim().parse().unwrap_or_else(|_| {
            tracing::warn!(%key, value = %raw, "unparseable env var, falling back to default");
            default
        }),
        Err(_) => default,
    }
}
