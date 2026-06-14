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
    use std::sync::{Mutex, MutexGuard, OnceLock};

    // Process environment is global, so these tests both serialize behind a
    // shared lock and snapshot/restore every key they touch. That keeps them
    // from racing each other or leaking state into any other test that reads
    // `Config::from_env()` under the parallel runner.

    /// Serializes all env-mutating tests against one another. Recovers from a
    /// poisoned lock so one panicking test doesn't wedge the rest.
    fn env_lock() -> MutexGuard<'static, ()> {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(()))
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// RAII snapshot of a set of env vars; restores each to its prior value
    /// (set or unset) on drop, so even a panicking assertion can't leak state.
    struct EnvSnapshot(Vec<(&'static str, Option<String>)>);

    impl EnvSnapshot {
        fn capture(keys: &[&'static str]) -> Self {
            Self(keys.iter().map(|&k| (k, std::env::var(k).ok())).collect())
        }
    }

    impl Drop for EnvSnapshot {
        fn drop(&mut self) {
            for (key, value) in &self.0 {
                match value {
                    Some(v) => std::env::set_var(key, v),
                    None => std::env::remove_var(key),
                }
            }
        }
    }

    #[test]
    fn env_or_falls_back_when_unset() {
        let _lock = env_lock();
        let _env = EnvSnapshot::capture(&["SMOLOFIS_TEST_OR_UNSET"]);
        std::env::remove_var("SMOLOFIS_TEST_OR_UNSET");
        assert_eq!(env_or("SMOLOFIS_TEST_OR_UNSET", "fallback"), "fallback");
    }

    #[test]
    fn env_or_falls_back_on_blank() {
        let _lock = env_lock();
        let _env = EnvSnapshot::capture(&["SMOLOFIS_TEST_OR_BLANK"]);
        std::env::set_var("SMOLOFIS_TEST_OR_BLANK", "   ");
        assert_eq!(env_or("SMOLOFIS_TEST_OR_BLANK", "fallback"), "fallback");
    }

    #[test]
    fn env_or_reads_value_when_set() {
        let _lock = env_lock();
        let _env = EnvSnapshot::capture(&["SMOLOFIS_TEST_OR_SET"]);
        std::env::set_var("SMOLOFIS_TEST_OR_SET", "custom");
        assert_eq!(env_or("SMOLOFIS_TEST_OR_SET", "fallback"), "custom");
    }

    #[test]
    fn env_parsed_reads_and_trims_value() {
        let _lock = env_lock();
        let _env = EnvSnapshot::capture(&["SMOLOFIS_TEST_PARSE_OK"]);
        std::env::set_var("SMOLOFIS_TEST_PARSE_OK", "  42  ");
        assert_eq!(env_parsed::<u16>("SMOLOFIS_TEST_PARSE_OK", 7), 42);
    }

    #[test]
    fn env_parsed_falls_back_on_garbage() {
        let _lock = env_lock();
        let _env = EnvSnapshot::capture(&["SMOLOFIS_TEST_PARSE_BAD"]);
        std::env::set_var("SMOLOFIS_TEST_PARSE_BAD", "not-a-number");
        assert_eq!(env_parsed::<u16>("SMOLOFIS_TEST_PARSE_BAD", 7), 7);
    }

    #[test]
    fn env_parsed_falls_back_when_unset() {
        let _lock = env_lock();
        let _env = EnvSnapshot::capture(&["SMOLOFIS_TEST_PARSE_UNSET"]);
        std::env::remove_var("SMOLOFIS_TEST_PARSE_UNSET");
        assert_eq!(env_parsed::<u64>("SMOLOFIS_TEST_PARSE_UNSET", 3), 3);
    }

    #[test]
    fn from_env_uses_appliance_defaults() {
        let _lock = env_lock();
        let keys = [
            "SMOLOFIS_GITEA_URL",
            "SMOLOFIS_GITEA_HEALTH_PATH",
            "SMOLOFIS_DOCKER_SOCKET",
            "SMOLOFIS_POLL_INTERVAL_SECS",
            "SMOLOFIS_GITEA_PUBLIC_PORT",
        ];
        // Snapshot first, then clear so a polluted environment can't make the
        // defaults assertion flaky; the snapshot restores everything on drop.
        let _env = EnvSnapshot::capture(&keys);
        for key in keys {
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
