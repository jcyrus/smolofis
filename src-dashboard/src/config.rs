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

#[cfg(test)]
mod tests {
    use super::*;

    // Each test uses a uniquely-named env var so process-global env state can't
    // race across the parallel test runner.

    #[test]
    fn env_or_falls_back_when_unset() {
        assert_eq!(env_or("SMOLOFIS_TEST_OR_UNSET", "fallback"), "fallback");
    }

    #[test]
    fn env_or_falls_back_on_blank() {
        std::env::set_var("SMOLOFIS_TEST_OR_BLANK", "   ");
        assert_eq!(env_or("SMOLOFIS_TEST_OR_BLANK", "fallback"), "fallback");
        std::env::remove_var("SMOLOFIS_TEST_OR_BLANK");
    }

    #[test]
    fn env_or_reads_value_when_set() {
        std::env::set_var("SMOLOFIS_TEST_OR_SET", "custom");
        assert_eq!(env_or("SMOLOFIS_TEST_OR_SET", "fallback"), "custom");
        std::env::remove_var("SMOLOFIS_TEST_OR_SET");
    }

    #[test]
    fn env_parsed_reads_and_trims_value() {
        std::env::set_var("SMOLOFIS_TEST_PARSE_OK", "  42  ");
        assert_eq!(env_parsed::<u16>("SMOLOFIS_TEST_PARSE_OK", 7), 42);
        std::env::remove_var("SMOLOFIS_TEST_PARSE_OK");
    }

    #[test]
    fn env_parsed_falls_back_on_garbage() {
        std::env::set_var("SMOLOFIS_TEST_PARSE_BAD", "not-a-number");
        assert_eq!(env_parsed::<u16>("SMOLOFIS_TEST_PARSE_BAD", 7), 7);
        std::env::remove_var("SMOLOFIS_TEST_PARSE_BAD");
    }

    #[test]
    fn env_parsed_falls_back_when_unset() {
        assert_eq!(env_parsed::<u64>("SMOLOFIS_TEST_PARSE_UNSET", 3), 3);
    }

    #[test]
    fn from_env_uses_appliance_defaults() {
        // Clear the vars this assertion depends on so a polluted environment
        // can't make the defaults test flaky.
        for key in [
            "SMOLOFIS_GITEA_URL",
            "SMOLOFIS_GITEA_HEALTH_PATH",
            "SMOLOFIS_DOCKER_SOCKET",
            "SMOLOFIS_POLL_INTERVAL_SECS",
            "SMOLOFIS_GITEA_PUBLIC_PORT",
        ] {
            std::env::remove_var(key);
        }

        let cfg = Config::from_env();
        assert_eq!(cfg.gitea_url, "http://127.0.0.1:3000");
        assert_eq!(cfg.gitea_health_path, "/api/healthz");
        assert_eq!(cfg.docker_socket, PathBuf::from("/var/run/docker.sock"));
        assert_eq!(cfg.poll_interval, Duration::from_secs(3));
        assert_eq!(cfg.gitea_public_port, 3000);
    }
}
