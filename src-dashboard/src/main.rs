//! smolofis-panel — the SmolOfis appliance control plane.
//!
//! Launched by systemd as early as possible in the boot sequence so the
//! browser sees an "Initializing system…" page within seconds of power-on,
//! then flips to the live dashboard once Docker, Gitea and Coolify report
//! healthy.

mod config;
mod handlers;
mod system;

use std::sync::Arc;

use anyhow::Context;
use axum::routing::get;
use axum::Router;
use tracing::info;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .compact()
        .init();

    let config = config::Config::from_env();
    info!(
        version = env!("CARGO_PKG_VERSION"),
        bind = %config.bind_addr,
        "smolofis-panel starting"
    );

    let state = Arc::new(system::AppState::new(config.clone()));
    system::spawn_poller(Arc::clone(&state));

    let app = Router::new()
        .route("/", get(handlers::dashboard))
        .route("/api/state", get(handlers::api_state))
        .route("/healthz", get(handlers::healthz))
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(config.bind_addr)
        .await
        .with_context(|| {
            format!(
                "failed to bind {} (port 80 needs CAP_NET_BIND_SERVICE; \
                 set SMOLOFIS_BIND=127.0.0.1:8080 for local development)",
                config.bind_addr
            )
        })?;
    info!("dashboard listening on http://{}", config.bind_addr);

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .context("http server terminated unexpectedly")?;

    info!("smolofis-panel shut down cleanly");
    Ok(())
}

/// Resolves on SIGINT or SIGTERM so systemd stops are graceful.
async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("failed to install SIGINT handler");
    };
    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => info!("received SIGINT, shutting down"),
        _ = terminate => info!("received SIGTERM, shutting down"),
    }
}
