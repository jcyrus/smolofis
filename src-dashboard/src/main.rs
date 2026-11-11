//! smolofis-panel — the SmolOfis appliance control plane.
//!
//! Launched by systemd as early as possible in the boot sequence so the
//! browser sees an "Initializing system…" page within seconds of power-on,
//! then flips to the live dashboard once Docker, Gitea and Coolify report
//! healthy.

#[cfg(unix)]
mod admin_worker;
mod audit;
mod audit_archive;
mod auth;
mod config;
mod console;
mod directory;
mod enrollment;
mod handlers;
mod operations;
mod system;
mod transport;

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

    let args: Vec<String> = std::env::args().skip(1).collect();
    let private_preview = args.as_slice() == ["--private-console"];
    if !args.is_empty() && !private_preview {
        if args.len() == 3 && args[0] == "--console-access-check" {
            return transport::check(
                std::path::Path::new(&args[1]),
                std::path::Path::new(&args[2]),
            )
            .await;
        }
        if args[0].starts_with("--operation-") {
            return operations::command(&args);
        }
        if args[0].starts_with("--auth-") {
            return enrollment::command(&args).await;
        }
        #[cfg(unix)]
        if args.len() == 3 && args[0] == "--admin-worker" {
            let uid = args[2]
                .parse::<u32>()
                .context("panel UID must be numeric")?;
            return admin_worker::serve(std::path::Path::new(&args[1]), uid).await;
        }
        anyhow::bail!("usage: smolofis-panel [--admin-worker /absolute/socket/path panel-uid]");
    }

    let mut config = config::Config::from_env();
    if private_preview {
        // The separate private service cannot be widened by environment overrides.
        config.console_enabled = true;
        config.bind_addr = "127.0.0.1:8091".parse().expect("fixed loopback address");
    }
    config.validate()?;
    info!(
        version = env!("CARGO_PKG_VERSION"),
        bind = %config.bind_addr,
        "smolofis-panel starting"
    );

    let state = Arc::new(system::AppState::new(config.clone()));
    system::spawn_poller(Arc::clone(&state));

    let mut app = Router::new()
        .route("/", get(handlers::dashboard))
        .route("/api/state", get(handlers::api_state))
        .route("/healthz", get(handlers::healthz))
        .route("/assets/app.css", get(handlers::asset_css))
        .route("/assets/fonts/{file}", get(handlers::asset_font))
        .merge(console::routes(config.console_enabled));
    let auth_config = if private_preview {
        Some(std::ffi::OsString::from("/etc/smolofis/panel-auth.json"))
    } else {
        std::env::var_os("SMOLOFIS_AUTH_CONFIG")
    };
    let auth_state = if private_preview {
        Some(std::ffi::OsString::from(
            "/etc/smolofis/panel-enrollment.json",
        ))
    } else {
        std::env::var_os("SMOLOFIS_AUTH_STATE")
    };
    anyhow::ensure!(
        auth_state.is_none() || auth_config.is_some(),
        "enrollment state requires authentication configuration"
    );
    if let Some(path) = auth_config {
        anyhow::ensure!(
            config.console_enabled,
            "authentication preview requires the console flag"
        );
        let auth = auth::Auth::load_with_enrollment(
            std::path::Path::new(&path),
            auth_state.as_deref().map(std::path::Path::new),
        )?;
        if private_preview {
            transport::private_host(auth.private_transport_origin()?)?;
        }
        app = app
            .merge(auth::routes())
            .layer(axum::middleware::from_fn(auth::guard))
            .layer(axum::Extension(auth));
    }
    let app = app.with_state(state);

    let listener = tokio::net::TcpListener::bind(config.bind_addr)
        .await
        .with_context(|| {
            format!(
                "failed to bind {} (set SMOLOFIS_BIND to use another address)",
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
