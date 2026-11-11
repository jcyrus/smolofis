//! Opt-in authenticated administration API for journaled identity diagnostics.
use crate::{
    admin_worker::{self, Operation},
    auth::{Auth, VerifiedAdmin},
    system::AppState,
};
use axum::{
    extract::{DefaultBodyLimit, Extension, Path},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use std::sync::Arc;

pub(crate) fn routes() -> Router<Arc<AppState>> {
    Router::new()
        .route("/api/operations", get(list))
        .route("/api/operations/{operation_id}", get(status))
        .route("/api/operations/identity-check", post(identity_check))
        .layer(DefaultBodyLimit::max(1024))
}

async fn result(
    operation: Operation,
    admin: VerifiedAdmin,
    id: Option<String>,
    created: bool,
) -> Response {
    match admin_worker::call(operation, admin.guid(), id).await {
        Ok(data) if data.is_null() => StatusCode::NOT_FOUND.into_response(),
        Ok(data) => (if created { StatusCode::CREATED } else { StatusCode::OK }, Json(data)).into_response(),
        Err(_) => (StatusCode::SERVICE_UNAVAILABLE, "Operation service unavailable. Inspect operation history before retrying a submitted check.").into_response(),
    }
}

async fn list(Extension(admin): Extension<VerifiedAdmin>) -> Response {
    result(Operation::JobList, admin, None, false).await
}

async fn status(Extension(admin): Extension<VerifiedAdmin>, Path(id): Path<String>) -> Response {
    if id.len() != 64 || !id.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return StatusCode::BAD_REQUEST.into_response();
    }
    result(Operation::JobStatus, admin, Some(id), false).await
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CheckInput {
    csrf: String,
}

async fn identity_check(
    Extension(auth): Extension<Arc<Auth>>,
    Extension(admin): Extension<VerifiedAdmin>,
    headers: HeaderMap,
    Json(input): Json<CheckInput>,
) -> Response {
    if let Err(status) = auth.authorize_operation(&headers, &admin, &input.csrf) {
        return status.into_response();
    }
    result(Operation::IdentityCheck, admin, None, true).await
}
