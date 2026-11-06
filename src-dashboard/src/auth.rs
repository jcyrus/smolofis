//! Opt-in directory sessions. No management operations are exposed.
use crate::{
    directory::{AdDirectory, Directory, DirectoryConfig, Identity},
    system::AppState,
};
use anyhow::Context;
use askama::Template;
use axum::{
    extract::{DefaultBodyLimit, Extension, Request},
    http::{header, HeaderMap, Method, StatusCode},
    middleware::Next,
    response::{Html, IntoResponse, Redirect, Response},
    routing::{get, post},
    Form, Router,
};
use rand::RngCore;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    path::Path,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use zeroize::Zeroizing;

const SESSION_COOKIE: &str = "smolofis_session";
const LOGIN_COOKIE: &str = "smolofis_login";
const IDLE: Duration = Duration::from_secs(1800);
const ABSOLUTE: Duration = Duration::from_secs(28800);
const DIRECTORY_TIMEOUT: Duration = Duration::from_secs(5);
const CAPACITY: usize = 256;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FileConfig {
    origin: String,
    directory: DirectoryConfig,
}

#[derive(Clone)]
struct Session {
    generation: Option<String>,
    identity: Identity,
    csrf: String,
    created: Instant,
    last_used: Instant,
}
struct Attempt {
    start: Instant,
    count: u32,
}
struct Store {
    sessions: HashMap<[u8; 32], Session>,
    attempts: HashMap<String, Attempt>,
    global: Attempt,
}

pub struct Auth {
    audit: Option<crate::audit::Audit>,
    enrollment: Option<crate::enrollment::Gate>,
    origin: String,
    authority: String,
    secure: bool,
    directory: Arc<dyn Directory>,
    store: Mutex<Store>,
    slots: tokio::sync::Semaphore,
}

fn token() -> Result<String, StatusCode> {
    let mut bytes = [0; 32];
    rand::rngs::OsRng
        .try_fill_bytes(&mut bytes)
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}
fn key(token: &str) -> [u8; 32] {
    Sha256::digest(token.as_bytes()).into()
}
fn valid_token(token: &str) -> bool {
    token.len() == 64 && token.bytes().all(|c| c.is_ascii_hexdigit())
}

// Ambiguous duplicate cookies are rejected rather than choosing an attacker-
// controlled ordering. Request headers never reach logs.
fn cookie(headers: &HeaderMap, name: &str) -> Option<String> {
    let mut found = None;
    for value in headers.get_all(header::COOKIE) {
        for pair in value.to_str().ok()?.split(';') {
            if let Some((key, value)) = pair.trim().split_once('=') {
                if key == name {
                    if found.is_some() || !valid_token(value) {
                        return None;
                    }
                    found = Some(value.to_owned());
                }
            }
        }
    }
    found
}

impl Auth {
    pub fn load(path: &Path) -> anyhow::Result<Arc<Self>> {
        Self::load_with_enrollment(path, None)
    }

    pub fn load_with_enrollment(path: &Path, state: Option<&Path>) -> anyhow::Result<Arc<Self>> {
        let bytes = if state.is_some() {
            crate::enrollment::read_configuration(path)?
        } else {
            std::fs::read(path).context("read authentication configuration")?
        };
        let config: FileConfig =
            serde_json::from_slice(&bytes).context("invalid authentication configuration")?;
        if state.is_some() {
            crate::enrollment::require_trusted_file(&config.directory.ca_file)?;
            crate::enrollment::require_trusted_file(&config.directory.reader_password_file)?;
        }
        let directory = Arc::new(AdDirectory::new(config.directory)?);
        let mut auth = Self::new(config.origin, directory)?;
        if let Some(path) = std::env::var_os("SMOLOFIS_AUDIT_FILE") {
            auth.audit = Some(crate::audit::Audit::open(Path::new(&path))?);
        }
        auth.enrollment = state
            .map(|state| crate::enrollment::Gate::load(state, path, &bytes))
            .transpose()?;
        Ok(Arc::new(auth))
    }

    fn new(origin: String, directory: Arc<dyn Directory>) -> anyhow::Result<Self> {
        let url = url::Url::parse(&origin)?;
        anyhow::ensure!(
            url.origin().ascii_serialization() == origin
                && matches!(url.scheme(), "https" | "http"),
            "auth origin must be a canonical HTTP(S) origin without a trailing slash"
        );
        let local = url.host_str().is_some_and(|host| {
            host == "localhost"
                || host
                    .trim_matches(['[', ']'])
                    .parse::<std::net::IpAddr>()
                    .is_ok_and(|ip| ip.is_loopback())
        });
        anyhow::ensure!(
            url.scheme() == "https" || local,
            "HTTP auth is restricted to local development"
        );
        let authority = origin
            .split_once("://")
            .context("missing auth origin scheme")?
            .1
            .to_owned();
        Ok(Self {
            audit: None,
            enrollment: None,
            secure: url.scheme() == "https",
            origin,
            authority,
            directory,
            store: Mutex::new(Store {
                sessions: HashMap::new(),
                attempts: HashMap::new(),
                global: Attempt {
                    start: Instant::now(),
                    count: 0,
                },
            }),
            slots: tokio::sync::Semaphore::new(8),
        })
    }

    fn set_cookie(&self, response: &mut Response, name: &str, value: &str, age: u32) {
        let secure = if self.secure { "; Secure" } else { "" };
        let value =
            format!("{name}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}{secure}");
        response.headers_mut().append(
            header::SET_COOKIE,
            value.parse().expect("generated cookie is ASCII"),
        );
    }

    fn same_origin(&self, headers: &HeaderMap) -> bool {
        headers.get(header::ORIGIN).and_then(|h| h.to_str().ok()) == Some(&self.origin)
    }

    fn allow_attempt(&self, username: &str) -> bool {
        let now = Instant::now();
        let mut store = self.store.lock().unwrap_or_else(|e| e.into_inner());
        store
            .attempts
            .retain(|_, attempt| now.duration_since(attempt.start) < Duration::from_secs(60));
        if now.duration_since(store.global.start) >= Duration::from_secs(60) {
            store.global = Attempt {
                start: now,
                count: 0,
            };
        }
        if store.global.count >= 20 || store.attempts.len() >= CAPACITY {
            return false;
        }
        store.global.count += 1;
        let attempt = store
            .attempts
            .entry(username.to_ascii_lowercase())
            .or_insert(Attempt {
                start: now,
                count: 0,
            });
        attempt.count += 1;
        attempt.count <= 5
    }

    async fn session(&self, headers: &HeaderMap) -> Result<(String, Session), StatusCode> {
        let generation = self.generation()?;
        let token = cookie(headers, SESSION_COOKIE).ok_or(StatusCode::UNAUTHORIZED)?;
        let digest = key(&token);
        let now = Instant::now();
        let session = {
            let mut store = self.store.lock().unwrap_or_else(|e| e.into_inner());
            store.sessions.retain(|_, session| {
                session.generation == generation
                    && now.duration_since(session.last_used) < IDLE
                    && now.duration_since(session.created) < ABSOLUTE
            });
            store
                .sessions
                .get(&digest)
                .cloned()
                .ok_or(StatusCode::UNAUTHORIZED)?
        };
        if session.generation != generation {
            self.store
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .sessions
                .remove(&digest);
            return Err(StatusCode::UNAUTHORIZED);
        }
        let _slot = self
            .slots
            .try_acquire()
            .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
        match tokio::time::timeout(
            DIRECTORY_TIMEOUT,
            self.directory.authorized(&session.identity),
        )
        .await
        {
            Ok(Ok(true)) => (),
            Ok(Ok(false)) => {
                self.store
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .sessions
                    .remove(&digest);
                tracing::info!(
                    event = "session_revoked",
                    "administrator access no longer authorized"
                );
                self.record(crate::audit::Event::SessionRevoked, Some(&session.identity))?;
                return Err(StatusCode::UNAUTHORIZED);
            }
            _ => return Err(StatusCode::SERVICE_UNAVAILABLE),
        }
        if self.generation()? != generation {
            return Err(StatusCode::UNAUTHORIZED);
        }
        // Concurrent logout must win over a request that was checking LDAP.
        let mut store = self.store.lock().unwrap_or_else(|e| e.into_inner());
        let current = store
            .sessions
            .get_mut(&digest)
            .ok_or(StatusCode::UNAUTHORIZED)?;
        if current.created.elapsed() >= ABSOLUTE || current.last_used.elapsed() >= IDLE {
            return Err(StatusCode::UNAUTHORIZED);
        }
        current.last_used = Instant::now();
        Ok((token, session))
    }

    fn record(
        &self,
        event: crate::audit::Event,
        identity: Option<&Identity>,
    ) -> Result<(), StatusCode> {
        if let Some(audit) = &self.audit {
            audit
                .record(event, identity)
                .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
        }
        Ok(())
    }
    fn generation(&self) -> Result<Option<String>, StatusCode> {
        self.enrollment
            .as_ref()
            .map(|gate| gate.generation())
            .transpose()
            .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)
    }

    pub fn private_transport_origin(&self) -> anyhow::Result<&str> {
        anyhow::ensure!(
            self.secure && self.enrollment.is_some(),
            "private HTTPS requires secure authentication and local enrollment"
        );
        self.generation()
            .map_err(|_| anyhow::anyhow!("local enrollment is locked or unavailable"))?;
        Ok(&self.origin)
    }
}

pub async fn verify_first_admin(
    path: &Path,
    username: &str,
    password: &str,
) -> anyhow::Result<Identity> {
    let config: FileConfig = serde_json::from_slice(&crate::enrollment::read_configuration(path)?)?;
    crate::enrollment::require_trusted_file(&config.directory.ca_file)?;
    crate::enrollment::require_trusted_file(&config.directory.reader_password_file)?;
    let auth = Auth::load(path)?;
    tokio::time::timeout(
        DIRECTORY_TIMEOUT,
        auth.directory.authenticate(username, password),
    )
    .await
    .map_err(|_| anyhow::anyhow!("directory verification timed out"))?
    .map_err(|_| anyhow::anyhow!("directory administrator verification failed"))
}

pub fn routes() -> Router<Arc<AppState>> {
    Router::new()
        .route("/auth/login", get(login_page).post(login))
        .route("/auth/account", get(account))
        .route("/auth/logout", post(logout))
        .layer(DefaultBodyLimit::max(8192))
}

pub async fn guard(
    Extension(auth): Extension<Arc<Auth>>,
    request: Request,
    next: Next,
) -> Response {
    if request.uri().path() == "/healthz" && request.method() == Method::GET {
        return next.run(request).await;
    }
    if request
        .headers()
        .get(header::HOST)
        .and_then(|h| h.to_str().ok())
        != Some(&auth.authority)
    {
        return StatusCode::MISDIRECTED_REQUEST.into_response();
    }
    let path = request.uri().path();
    let public = path == "/auth/login" || path == "/auth/logout" || path.starts_with("/assets/");
    let mut response = if public {
        next.run(request).await
    } else {
        match auth.session(request.headers()).await {
            Ok(_) => next.run(request).await,
            Err(StatusCode::UNAUTHORIZED)
                if request.method() == Method::GET && !path.starts_with("/api/") =>
            {
                Redirect::to("/auth/login").into_response()
            }
            Err(status) => (
                status,
                if status == StatusCode::SERVICE_UNAVAILABLE {
                    "Administrator verification is unavailable or locked locally. Contact your appliance operator."
                } else {
                    "Administrator sign-in required."
                },
            )
                .into_response(),
        }
    };
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, "no-store".parse().unwrap());
    response
        .headers_mut()
        .insert(header::REFERRER_POLICY, "same-origin".parse().unwrap());
    response
}

#[derive(Template)]
#[template(path = "auth.html")]
struct AuthTemplate<'a> {
    csrf: &'a str,
    error: &'a str,
    account: bool,
}
fn form(csrf: &str, error: &str, account: bool, status: StatusCode) -> Response {
    let mut response = match (AuthTemplate {
        csrf,
        error,
        account,
    })
    .render()
    {
        Ok(html) => (status, Html(html)).into_response(),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    response.headers_mut().insert(header::CONTENT_SECURITY_POLICY, "default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'".parse().unwrap());
    response
        .headers_mut()
        .insert(header::X_CONTENT_TYPE_OPTIONS, "nosniff".parse().unwrap());
    response
}

async fn login_page(Extension(auth): Extension<Arc<Auth>>) -> Response {
    match token() {
        Ok(csrf) => {
            let mut response = form(&csrf, "", false, StatusCode::OK);
            auth.set_cookie(&mut response, LOGIN_COOKIE, &csrf, 600);
            response
        }
        Err(status) => status.into_response(),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Login {
    username: String,
    password: String,
    csrf: String,
}
async fn login(
    Extension(auth): Extension<Arc<Auth>>,
    headers: HeaderMap,
    Form(input): Form<Login>,
) -> Response {
    let password = Zeroizing::new(input.password);
    if !auth.same_origin(&headers) || cookie(&headers, LOGIN_COOKIE).as_deref() != Some(&input.csrf)
    {
        return (
            StatusCode::FORBIDDEN,
            "Sign-in form expired. Reload the sign-in page and try again.",
        )
            .into_response();
    }
    if !crate::directory::valid_username(&input.username)
        || password.is_empty()
        || password.len() > 1024
    {
        return form(
            &input.csrf,
            "Sign-in failed. Check your account and administrator access.",
            false,
            StatusCode::UNAUTHORIZED,
        );
    }
    if !auth.allow_attempt(&input.username) {
        let mut response = form(
            &input.csrf,
            "Too many sign-in attempts. Wait one minute and try again.",
            false,
            StatusCode::TOO_MANY_REQUESTS,
        );
        response
            .headers_mut()
            .insert(header::RETRY_AFTER, "60".parse().unwrap());
        return response;
    }
    let generation = match auth.generation() {
        Ok(generation) => generation,
        Err(status) => return (status, "Console access is locked or enrollment is unavailable. Contact your appliance operator.").into_response(),
    };
    let Ok(_slot) = auth.slots.try_acquire() else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    let identity = match tokio::time::timeout(
        DIRECTORY_TIMEOUT,
        auth.directory.authenticate(&input.username, &password),
    )
    .await
    {
        Ok(Ok(identity)) => identity,
        _ => {
            tracing::info!(event = "login_denied", "administrator sign-in failed");
            if let Err(status) = auth.record(crate::audit::Event::LoginDenied, None) {
                return status.into_response();
            }
            return form(&input.csrf, "Sign-in failed. Check your account and administrator access. If the directory is unavailable, try again shortly.", false, StatusCode::UNAUTHORIZED);
        }
    };
    let (Ok(session_token), Ok(csrf)) = (token(), token()) else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    if auth.generation().ok() != Some(generation.clone()) {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    let now = Instant::now();
    if let Err(status) = auth.record(crate::audit::Event::LoginAllowed, Some(&identity)) {
        return status.into_response();
    }
    {
        let mut store = auth.store.lock().unwrap_or_else(|e| e.into_inner());
        store.sessions.retain(|_, session| {
            session.generation == generation
                && now.duration_since(session.last_used) < IDLE
                && now.duration_since(session.created) < ABSOLUTE
        });
        if let Some(old) = cookie(&headers, SESSION_COOKIE) {
            store.sessions.remove(&key(&old));
        }
        if store.sessions.len() >= CAPACITY {
            return StatusCode::SERVICE_UNAVAILABLE.into_response();
        }
        store.sessions.insert(
            key(&session_token),
            Session {
                generation,
                identity,
                csrf,
                created: now,
                last_used: now,
            },
        );
    }
    tracing::info!(event = "login_allowed", "administrator signed in");
    let mut response = Redirect::to("/console").into_response();
    auth.set_cookie(
        &mut response,
        SESSION_COOKIE,
        &session_token,
        ABSOLUTE.as_secs() as u32,
    );
    auth.set_cookie(&mut response, LOGIN_COOKIE, "", 0);
    response
}

async fn account(Extension(auth): Extension<Arc<Auth>>, headers: HeaderMap) -> Response {
    match auth.session(&headers).await {
        Ok((_, session)) => form(&session.csrf, "", true, StatusCode::OK),
        Err(status) => status.into_response(),
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Logout {
    csrf: String,
}
async fn logout(
    Extension(auth): Extension<Arc<Auth>>,
    headers: HeaderMap,
    Form(input): Form<Logout>,
) -> Response {
    // Logout remains possible when the directory is offline: revoke locally.
    if !auth.same_origin(&headers) {
        return StatusCode::FORBIDDEN.into_response();
    }
    let Some(token) = cookie(&headers, SESSION_COOKIE) else {
        return StatusCode::UNAUTHORIZED.into_response();
    };
    let mut store = auth.store.lock().unwrap_or_else(|e| e.into_inner());
    let Some(session) = store.sessions.get(&key(&token)) else {
        return StatusCode::UNAUTHORIZED.into_response();
    };
    if !valid_token(&input.csrf) || session.csrf != input.csrf {
        return StatusCode::FORBIDDEN.into_response();
    }
    let identity = session.identity.clone();
    store.sessions.remove(&key(&token));
    let audit_result = auth.record(crate::audit::Event::Logout, Some(&identity));
    tracing::info!(event = "logout", "administrator signed out");
    let mut response = Redirect::to("/auth/login").into_response();
    auth.set_cookie(&mut response, SESSION_COOKIE, "", 0);
    if audit_result.is_err() {
        *response.status_mut() = StatusCode::SERVICE_UNAVAILABLE;
    }
    response
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU8, Ordering};
    struct FakeDirectory(AtomicU8);
    impl Directory for FakeDirectory {
        fn authenticate<'a>(
            &'a self,
            username: &'a str,
            password: &'a str,
        ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Identity, ()>> + Send + 'a>>
        {
            Box::pin(async move {
                if username == "admin"
                    && password == "correct"
                    && self.0.load(Ordering::SeqCst) == 1
                {
                    Ok(Identity([7; 16]))
                } else {
                    Err(())
                }
            })
        }
        fn authorized<'a>(
            &'a self,
            _: &'a Identity,
        ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<bool, ()>> + Send + 'a>>
        {
            Box::pin(async move {
                match self.0.load(Ordering::SeqCst) {
                    1 => Ok(true),
                    0 => Ok(false),
                    _ => Err(()),
                }
            })
        }
    }
    struct Server {
        auth: Arc<Auth>,
        directory: Arc<FakeDirectory>,
        client: reqwest::Client,
        origin: String,
        task: tokio::task::JoinHandle<()>,
    }
    impl Drop for Server {
        fn drop(&mut self) {
            self.task.abort();
        }
    }
    impl Server {
        async fn start() -> Self {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let origin = format!("http://{}", listener.local_addr().unwrap());
            let directory = Arc::new(FakeDirectory(AtomicU8::new(1)));
            let auth = Arc::new(Auth::new(origin.clone(), directory.clone()).unwrap());
            let app = Router::new()
                .route("/", get(|| async { "private" }))
                .route("/api/state", get(crate::handlers::api_state))
                .route("/healthz", get(|| async { "ok" }))
                .merge(crate::console::routes(true))
                .merge(routes())
                .layer(axum::middleware::from_fn(guard))
                .layer(Extension(auth.clone()))
                .with_state(Arc::new(AppState::new(crate::config::Config::from_env())));
            let task = tokio::spawn(async move {
                axum::serve(listener, app).await.unwrap();
            });
            Self {
                auth,
                directory,
                client: reqwest::Client::builder()
                    .redirect(reqwest::redirect::Policy::none())
                    .build()
                    .unwrap(),
                origin,
                task,
            }
        }
        async fn request(&self, path: &str, cookie: &str) -> reqwest::Response {
            self.client
                .get(format!("{}{path}", self.origin))
                .header(header::COOKIE, cookie)
                .send()
                .await
                .unwrap()
        }
        async fn login(&self) -> String {
            self.login_replacing("").await
        }
        async fn login_replacing(&self, existing: &str) -> String {
            let page = self.request("/auth/login", "").await;
            // Native form POSTs need this policy to preserve their Origin.
            assert_eq!(page.headers()[header::REFERRER_POLICY], "same-origin");
            let cookie = page.headers()[header::SET_COOKIE]
                .to_str()
                .unwrap()
                .split(';')
                .next()
                .unwrap()
                .to_owned();
            let csrf = cookie.split_once('=').unwrap().1;
            let body = url::form_urlencoded::Serializer::new(String::new())
                .append_pair("username", "admin")
                .append_pair("password", "correct")
                .append_pair("csrf", csrf)
                .finish();
            let response = self
                .client
                .post(format!("{}/auth/login", self.origin))
                .header(header::ORIGIN, &self.origin)
                .header(header::COOKIE, format!("{cookie}; {existing}"))
                .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
                .body(body)
                .send()
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::SEE_OTHER);
            let cookie = response
                .headers()
                .get_all(header::SET_COOKIE)
                .iter()
                .find(|h| h.to_str().unwrap().starts_with(SESSION_COOKIE))
                .unwrap()
                .to_str()
                .unwrap();
            assert!(cookie.contains("HttpOnly") && cookie.contains("SameSite=Strict"));
            cookie.split(';').next().unwrap().to_owned()
        }
    }

    #[tokio::test]
    async fn audit_failure_prevents_session_issuance() {
        use std::fs::OpenOptions;
        let path =
            std::env::temp_dir().join(format!("smolofis-auth-audit-{}", rand::random::<u64>()));
        let file = OpenOptions::new()
            .create_new(true)
            .read(true)
            .append(true)
            .open(&path)
            .unwrap();
        file.set_len(16 * 1024 * 1024).unwrap();
        let mut auth = Auth::new(
            "http://127.0.0.1:8090".into(),
            Arc::new(FakeDirectory(AtomicU8::new(1))),
        )
        .unwrap();
        auth.audit = Some(crate::audit::Audit::from_test_file(file));
        let auth = Arc::new(auth);
        let csrf = "a".repeat(64);
        let mut headers = HeaderMap::new();
        headers.insert(header::ORIGIN, "http://127.0.0.1:8090".parse().unwrap());
        headers.insert(
            header::COOKIE,
            format!("{LOGIN_COOKIE}={csrf}").parse().unwrap(),
        );
        let response = login(
            Extension(auth.clone()),
            headers,
            Form(Login {
                username: "admin".into(),
                password: "correct".into(),
                csrf,
            }),
        )
        .await;
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert!(response.headers().get(header::SET_COOKIE).is_none());
        assert!(auth.store.lock().unwrap().sessions.is_empty());
        std::fs::remove_file(path).unwrap();
    }

    #[tokio::test]
    async fn reauthentication_rotates_and_invalidates_the_previous_token() {
        let server = Server::start().await;
        let old = server.login().await;
        let new = server.login_replacing(&old).await;
        assert_ne!(old, new);
        assert_eq!(
            server.request("/api/state", &old).await.status(),
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            server.request("/api/state", &new).await.status(),
            StatusCode::OK
        );
        assert_eq!(server.auth.store.lock().unwrap().sessions.len(), 1);
    }

    #[tokio::test]
    async fn stale_enrollment_sessions_do_not_exhaust_new_login_capacity() {
        let server = Server::start().await;
        {
            let mut store = server.auth.store.lock().unwrap();
            for index in 0..CAPACITY {
                store.sessions.insert(
                    key(&format!("old-session-{index}")),
                    Session {
                        generation: Some("previous enrollment".into()),
                        identity: Identity([7; 16]),
                        csrf: "old csrf".into(),
                        created: Instant::now(),
                        last_used: Instant::now(),
                    },
                );
            }
        }
        let cookie = server.login().await;
        assert_eq!(
            server.request("/api/state", &cookie).await.status(),
            StatusCode::OK
        );
        assert_eq!(server.auth.store.lock().unwrap().sessions.len(), 1);
    }

    #[tokio::test]
    async fn protects_telemetry_and_validates_login_origin_and_host() {
        let server = Server::start().await;
        assert_eq!(
            server.request("/api/state", "").await.status(),
            StatusCode::UNAUTHORIZED
        );
        let response = server.request("/", "").await;
        assert_eq!(response.status(), StatusCode::SEE_OTHER);
        assert_eq!(response.headers()[header::LOCATION], "/auth/login");
        assert_eq!(
            server
                .client
                .get(format!("{}/healthz", server.origin))
                .header(header::HOST, "untrusted.test")
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::OK
        );
        assert_eq!(
            server
                .client
                .get(format!("{}/auth/login", server.origin))
                .header(header::HOST, "untrusted.test")
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::MISDIRECTED_REQUEST
        );
        let response = server
            .client
            .post(format!("{}/auth/login", server.origin))
            .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
            .body("username=admin&password=correct&csrf=wrong")
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
        assert_eq!(
            server
                .client
                .post(format!("{}/auth/login", server.origin))
                .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
                .body("x".repeat(9000))
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::PAYLOAD_TOO_LARGE
        );
        let cookie = server.login().await;
        let response = server.request("/api/state", &cookie).await;
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response.headers()[header::CACHE_CONTROL], "no-store");
    }

    #[tokio::test]
    async fn directory_changes_revoke_sessions_without_resurrecting_them() {
        let server = Server::start().await;
        let cookie = server.login().await;
        server.directory.0.store(2, Ordering::SeqCst);
        assert_eq!(
            server.request("/api/state", &cookie).await.status(),
            StatusCode::SERVICE_UNAVAILABLE
        );
        server.directory.0.store(1, Ordering::SeqCst);
        assert_eq!(
            server.request("/api/state", &cookie).await.status(),
            StatusCode::OK
        );
        server.directory.0.store(0, Ordering::SeqCst);
        assert_eq!(
            server.request("/api/state", &cookie).await.status(),
            StatusCode::UNAUTHORIZED
        );
        server.directory.0.store(1, Ordering::SeqCst);
        assert_eq!(
            server.request("/api/state", &cookie).await.status(),
            StatusCode::UNAUTHORIZED
        );
    }

    #[tokio::test]
    async fn logout_requires_csrf_and_works_during_directory_outage() {
        let server = Server::start().await;
        let cookie = server.login().await;
        let account = server
            .request("/auth/account", &cookie)
            .await
            .text()
            .await
            .unwrap();
        let csrf = account
            .split("name=\"csrf\" value=\"")
            .nth(1)
            .unwrap()
            .split('"')
            .next()
            .unwrap();
        let post = |csrf: &str| {
            server
                .client
                .post(format!("{}/auth/logout", server.origin))
                .header(header::ORIGIN, &server.origin)
                .header(header::COOKIE, &cookie)
                .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
                .body(format!("csrf={csrf}"))
        };
        assert_eq!(
            post("wrong").send().await.unwrap().status(),
            StatusCode::FORBIDDEN
        );
        server.directory.0.store(2, Ordering::SeqCst);
        let response = post(csrf).send().await.unwrap();
        assert_eq!(response.status(), StatusCode::SEE_OTHER);
        assert!(response.headers()[header::SET_COOKIE]
            .to_str()
            .unwrap()
            .contains("Max-Age=0"));
        server.directory.0.store(1, Ordering::SeqCst);
        assert_eq!(
            server.request("/api/state", &cookie).await.status(),
            StatusCode::UNAUTHORIZED
        );
    }

    #[tokio::test]
    async fn sessions_expire_and_reject_ambiguous_cookies() {
        let server = Server::start().await;
        let cookie = server.login().await;
        assert_eq!(
            server
                .request("/api/state", &format!("{cookie}; {cookie}"))
                .await
                .status(),
            StatusCode::UNAUTHORIZED
        );
        server
            .auth
            .store
            .lock()
            .unwrap()
            .sessions
            .values_mut()
            .next()
            .unwrap()
            .last_used = Instant::now() - IDLE;
        assert_eq!(
            server.request("/api/state", &cookie).await.status(),
            StatusCode::UNAUTHORIZED
        );
        let cookie = server.login().await;
        server
            .auth
            .store
            .lock()
            .unwrap()
            .sessions
            .values_mut()
            .next()
            .unwrap()
            .created = Instant::now() - ABSOLUTE;
        assert_eq!(
            server.request("/api/state", &cookie).await.status(),
            StatusCode::UNAUTHORIZED
        );
    }

    #[test]
    fn origin_and_throttling_are_bounded() {
        let directory = Arc::new(FakeDirectory(AtomicU8::new(1)));
        for origin in [
            "http://office.test",
            "https://office.test/",
            "https://user@office.test",
            "https://office.test/path",
        ] {
            assert!(Auth::new(origin.into(), directory.clone()).is_err());
        }
        let auth = Auth::new("https://office.test".into(), directory).unwrap();
        for _ in 0..5 {
            assert!(auth.allow_attempt("Admin"));
        }
        assert!(!auth.allow_attempt("admin"));
        for i in 0..14 {
            assert!(auth.allow_attempt(&format!("u{i}")));
        }
        assert!(!auth.allow_attempt("another"));
        let mut response = StatusCode::OK.into_response();
        auth.set_cookie(&mut response, SESSION_COOKIE, "", 0);
        assert!(response.headers()[header::SET_COOKIE]
            .to_str()
            .unwrap()
            .contains("; Secure"));
    }

    /// Explicit browser fixture only; never compiled into the appliance binary.
    #[tokio::test]
    #[ignore = "manual browser fixture with a synthetic directory"]
    async fn browser_fixture() {
        let server = Server::start().await;
        let path = std::env::var("SMOLOFIS_AUTH_BROWSER_ORIGIN_FILE")
            .expect("set an output path for the local browser fixture origin");
        std::fs::write(path, &server.origin).unwrap();
        println!("Synthetic directory browser fixture ready");
        tokio::signal::ctrl_c().await.unwrap();
    }
}
