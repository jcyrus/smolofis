# Administration UI implementation tracker

Build a responsive, umbrelOS-inspired administration console on the existing
Rust/Axum/Askama panel. Primary tasks: people and groups, shared folders, and
service diagnosis. Central identities apply to hosted services; employee
computer sign-in is deferred.

Update this file with evidence as work completes. A checked milestone means
its acceptance criteria have passed, not merely that code exists.

## R0 — Nightly rollout with administration disabled

- [x] Review and commit the implementation on current main, preserving disabled console/authentication defaults and excluding candidate service units from image installation. PR #21 merged as 27198a6.
- [x] Pass CI lint/advisory/unit tests, ISO boot/install checks, and upgrade from the previous published nightly. Candidate run 42165212246 and main publication run 42167447823 passed.
- [x] Publish signed nightly 0.1.0-dev.73 from commit 27198a61bebc31f12190207b20f5edeedea5db8e; ISO SHA256 d62362bcb949aeb6028c28bf73440da0b30e6c6a186d5ff5e85eb3d5779c377b.
- [x] Inspect 192.168.1.17: 0.1.0-dev.68, system-a active, release signers present, recent backups, ample free space, healthy core containers; administration flags absent.
- [x] Resolve pre-existing user@1000.service failure: libpam-systemd is installed after upgrade; the user manager is active with XDG_RUNTIME_DIR=/run/user/1000. CI and server checks passed.
- [x] Stage signed dev.73 through the existing updater with fresh backup 20261107T081623Z, reboot, and verify version, active slot, services, and administration still disabled. Server 192.168.1.17 now boots and is active on system-b; system-a is retained.

Rollout completed 2026-11-07. Main run 42167447823 published the signed image.
The server verified the manifest signature and ISO checksum before staging.
The update verifier exited successfully and promoted system-b after phase ready.
Docker, Gitea, Coolify, database/Redis, proxy, and runner recovered; Coolify's
persisted users table is accessible. No failed systemd units remain. Console,
login, and console asset routes return 404; candidate worker/private-console
units report not-found; no administration flags are configured. The fresh
backup and previous system-a image remain available. This verifies the disabled
feature rollout, not identity/share management acceptance or a backup restore.

R0 permits code delivery with features disabled; it does not authorize enabling
identity administration or replace M0/M1/M6 acceptance.

## M0 — Identity feasibility gate (acceptance tasks)

- [x] Inspect current panel, persistence, service privilege boundaries, and local development tooling.
- [x] Add and run an isolated container integration harness for preliminary identity testing.
- [x] Prove certificate-verified LDAPS, Gitea directory login, and SMB permissions in the container lab.
- [x] Test password rotation, fresh-login denial when disabled, and GUID/SID stability after service restarts in the container lab.
- [x] Migrate an existing local Gitea account through its supported API, preserving account ID, repository ownership/history, working SSH key, and API token.
- [x] Demonstrate directory-disable limitations and explicitly block Gitea access; permanently revoke synthetic API tokens and SSH keys.
- [x] Restore a stopped, consistent container snapshot into fresh volumes; verify identity, Gitea mapping/history, file contents, numeric ownership, ACLs, and extended attributes.
- [x] Prove permanent browser-session revocation in the container lab, including re-enabling and replay of valid legacy remembered-login cookies, using the approved office-wide sign-out policy.
- [x] Revoke an already-authenticated SMB connection after account disabling or group removal, preserving another user's live connection and the file-server master process in the container lab.
- [x] Provision an isolated Debian 13 VM with native Samba AD and a separate domain-member SMB file service (2026-11-07).
- [x] Full-kernel x86 Debian 13 VM: certificate-verified LDAPS and SYSVOL read with native `security.NTACL` (2026-11-07).
- [x] Prove directory authentication against shipped Gitea 28.2.0 on the full VM; directory login grants no automatic administrator rights.
- [x] Guest SMB baseline: writer/read-only/denied groups, shared content read, and healthy domain join. Native Windows/macOS acceptance remains below.
- [ ] Prove group-based read-only/read-write SMB access from Windows and macOS.
- [x] Native macOS mounts: writer read/write/delete and reader read with denied write; temporary mounts and loopback tunnel cleaned up (2026-11-07).
- [x] Validate native macOS outsider denial with correlated Samba trace: authenticated Outsider explicitly rejected from `projects` with `NT_STATUS_ACCESS_DENIED` (2026-11-07). macOS presents a generic timeout over the lab tunnel; preserve this diagnostic limitation. Windows remains pending.
- [x] Verify password changes, account disabling, stable ownership, and existing Gitea account migration on the full VM (2026-11-07).
- [x] Full-VM fresh-login lifecycle: old password denied after rotation, new password accepted, disabled account denied across LDAPS/SMB/Gitea, and original synthetic credentials restored. Requires explicit zero-minute old-password grace policy (2026-11-07).
- [x] Full-VM existing Gitea account migration: stable account ID/username, repository ownership/history, existing SSH key/API token continuity, and rejected former local password. Synthetic key/token removed after proof (2026-11-07).
- [x] Verify DNS, time, kernel reboot persistence, and directory backup/restore on the full VM (2026-11-07); default SYSVOL ACL scope and recovery steps documented.
- [x] DNS/NTP service outage and recovery: authoritative DNS blocked then restored with resolver cache/backoff reset; disabled NTP observed and synchronization restored (2026-11-07). Guest/host clocks unchanged; Kerberos skew coverage is recorded below.
- [x] Kerberos clock-skew failure/recovery: normal login succeeds, process-local +10-minute skew returns explicit clock-skew error, and real-time client login recovers without changing guest/host clocks (2026-11-07).
- [x] Full-VM kernel reboot: changed boot ID, stable GUID/SID, automatic directory/file/Gitea startup, directory login, and guest SMB group permissions (2026-11-07). DNS/time failure acceptance is recorded separately above.
- [x] Cold whole-VM backup/restore into a separate guest disk: directory IDs, Gitea account ID, SMB contents, numeric ownership/mode, ACLs, xattrs, and group access preserved (2026-11-07). Application-level directory restore remains separate.
- [x] Supported offline directory backup/restore into a replacement DC: restored TLS material, native SYSVOL ACL reset/check, stable GUID/SID, trusted login, existing member/Gitea access, and retained file metadata/mappings (2026-11-07). Custom GPO ACLs are outside this default-SYSVOL acceptance.
- [ ] Finalize deployment topology and the full-VM evidence report.
- [x] Consolidate [M0 evidence and conditional topology recommendation](identity-m0-evidence.md). Final acceptance awaits native Windows evidence.

Release gate: do not enable or release identity administration until this
prototype passes. The user approved continuing console implementation behind
a flag disabled by default while retaining full VM/native-client acceptance
as release gates. Revise the architecture explicitly if the prototype fails.
See [prototype protocol](identity-prototype.md).

## M1 — Secure administration

- [x] Add an isolated protocol-status-only Unix worker scaffold with peer-UID checks, bounded requests, strict typed decoding, and a candidate systemd unit excluded from image construction.
- [x] Implement opt-in directory-backed panel sessions and a dedicated administrator group; pass preliminary container acceptance. Production deployment remains gated below and by M0.
- [x] Add preview sign-in/sign-out forms, CSRF validation, bounded in-memory throttling, session expiry, and fail-closed directory revalidation.
- [x] Preview device-specific first-admin enrollment through local console/SSH, verifying an existing directory administrator and binding its GUID/configuration to the appliance machine ID.
- [x] Preview directory-independent local status/lock controls and verified re-enrollment after connectivity returns. Directory repair and full restore acceptance remain M0/M6 gates.
- [x] Add an opt-in private HTTPS transport candidate: fixed loopback backend, required HTTPS enrollment, read-only Tailscale/backend preflight, and separate foreground-Serve service units. Preliminary local TLS acceptance passes.
- [ ] Validate HTTPS/private Tailscale access on the full VM and real tailnet: certificate issuance/renewal, policy isolation, phone access, foreground cleanup, and actual service sandbox execution.
- [x] Actual backend/worker systemd execution: nine native VM checks passed for the single-file audit write exception, explicit read-only `/run`, home isolation, capabilities/seccomp, fixed loopback/authentication, atomic recovery, kernel peer UID, bounded typed protocol, AF_UNIX-only worker, and cleanup (2026-11-09). Real HTTPS/Serve/tailnet/mobile acceptance remains pending.
- [ ] Separate privileged service with restricted Unix socket and typed, allowlisted operations.
- [ ] CSRF protection, login throttling, secure sessions, and secret-free auditing.
- [x] Root-only historical operation lookup by ID: current receipt or bounded read-only history; coherent completion required, nonblocking busy-log behavior, partial-tail reporting, and unknown outcomes preserved. 67 host tests and eight native Linux history checks passed (2026-11-09). Listing, archive discovery/retention, superseded crash reconciliation, and the general journal remain pending.
- [x] Candidate root-only bounded audit archival and writer descriptor guard implemented; 69 host tests passed, one manual fixture ignored, and Clippy passed (2026-11-09). Exact bytes and SHA256 manifest retained before replacement; no automatic deletion.
- [x] Native Linux archival acceptance: eight checks passed for root ownership/modes/links, exact bytes/hash, busy and concurrent writers, capacity refusal, real SIGXFSZ snapshot interruption/retry, stale enrollment descriptor refusal, and fresh-writer recovery. Nine local audit regressions also passed (2026-11-09).
- [x] Archival sync-failure and abrupt-VM recovery: inject EIO into all five Linux fsync calls; terminate the owned QEMU container after rename but before parent-directory fsync and verify the synced archive, valid live log, and changed boot ID after restart (2026-11-09). This does not simulate host/device cache power loss.
- [x] Add packaged root `console-audit-init` and `console-audit-archive` workflow: non-destructive initialization, automatic standard-log selection, safe override handling, missing-log enrollment refusal and lockdown recovery. Seven native packaged-wrapper checks and nine local audit regressions passed; 69 host tests, Clippy and ShellCheck passed (2026-11-09). Existing image packaging includes the wrapper/binary without enabling services.
- [x] Wire audit/enrollment persistence assertions into the existing signed A/B ISO install test after update promotion and corrupted-slot fallback, including administration-disabled checks. Three native fixture component checks passed; complete ISO execution is still pending (2026-11-09).
- [ ] Validate the archival workflow in a signed nightly image and upgrade/rollback path before release; assess physical storage power-loss durability before claiming hardware-level guarantees.
- [x] Local operation receipts/correlation: schema-v2 state atomically records the latest applied operation ID; audit authorization/completion/uncertainty are correlated. Host regressions, native completion-write process crash, legacy-v1 read, and real VM-reboot receipt recovery passed (2026-11-09). Known-ID history lookup is recorded above; general operation journaling and appliance upgrade/rollback integration remain pending. See [operation receipts](admin-operations.md).
- [x] Audit crash-tail hardening: preserve incomplete records and refuse new appends; sign-in issues no session/cookie on tail/capacity failure. Four-process append and write-error regressions pass on macOS (2026-11-09). Complete operation/recovery auditing and real HTTPS/Serve acceptance remain pending.
- [x] Local recovery audit preview: root-attributed enrollment authorization precedes access changes; lockdown persists even if audit open/append fails, with explicit partial-failure reporting. Ordering/failure regressions pass locally (2026-11-09). Linux/root acceptance is recorded below; a complete correlated operation journal remains pending.
- [x] Full-kernel Linux local-audit acceptance: nine checks passed for real LDAPS enrollment, root/file ownership, UID 65534 rejection, invalid audit files, crash tails, protected-file aliases, real ENOSPC, lockdown recovery, and independent CLI writers (2026-11-09). Disposable tmpfs unmounted; web acceptance is recorded below. Operation-result correlation remains pending.
- [x] Full-kernel Linux web-audit acceptance: seven checks passed with UID/GID 65534 and zero effective capabilities; startup ownership/path checks, real LDAPS attribution, crash-tail/ENOSPC login denial, logout and group-revocation token invalidation, combined audit-failure lockdown, and recovered logging (2026-11-09). Writers stopped and tmpfs unmounted. Backend/worker sandbox acceptance is recorded above; sync faults, retention, and operation-result correlation remain pending.
- [ ] Authenticate telemetry/management APIs and update CI; preserve public liveness.

## M2 — Responsive console

- [x] Add a read-only console preview behind `SMOLOFIS_CONSOLE_ENABLED=false` by default, restricted to loopback until security and deployment acceptance pass.
- [x] Implement overview, service diagnostics/app launchers, appearance controls, and explicit not-connected states for account/share administration.
- [x] Verify flag-off routes/assets return 404 and non-loopback preview configuration is rejected.
- [x] Read-only home with health, problem guidance, and administration destinations.
- [x] People & Groups and Shared Folders destination pages with explicit not-connected states; Service Health and Gitea/Coolify launchers. Working management remains in M3/M4.
- [x] Offline embedded assets, server-rendered initial data, and a small JavaScript module.
- [x] Mobile and keyboard navigation, reduced motion, and accessible controls verified in the preview.
- [x] Preview states for Live USB, initializing, degraded, disconnected, and unconnected directory management. Live directory availability requires M1/M3 integration.

## M3 — People and groups

- [ ] Create users, reset passwords, disable accounts, and edit groups.
- [ ] Immutable directory IDs and explicit service mappings.
- [ ] Separate application access from administrator privileges.
- [ ] Gitea directory login; repository authorization remains in Gitea.
- [ ] Reviewed migration of existing Gitea accounts preserving ownership/history.
- [ ] Explicit revocation of SMB/panel sessions and Gitea tokens/SSH access.
- [ ] Integrate the tested office-wide Gitea browser sign-out operation with version/schema checks, service downtime notice, and partial-failure reporting.

## M4 — Shared folders

- [ ] Managed persistent share root and validated names/path containment.
- [ ] Group-based no-access/read-only/read-write permissions and effective access display.
- [ ] Atomic validated configuration changes with rollback on failure.
- [ ] Windows/macOS connection instructions using reachable addresses.
- [ ] Remove share definitions while retaining files; no permanent file deletion.

## M5 — Service recovery

- [ ] Directory, SMB, Docker, Gitea, runner, and Coolify health with dependencies.
- [ ] Plain-language impact and bounded sanitized diagnostics.
- [ ] Explicit targeted SMB/application restarts with disruption warnings.
- [ ] Directory/Docker-wide recovery remains diagnostic-only initially.
- [ ] Concurrency control, durable operation results, audit trail, and post-action checks.

## M6 — Appliance lifecycle

- [ ] Persist identities, shares, permissions, and service mappings across reboot/upgrades.
- [ ] Consistent backup/restore of directory, shared files/ACLs, and Gitea.
- [ ] Invalidate restored browser sessions and remembered logins before exposing restored Gitea to clients.
- [ ] Sanitized support bundles and operator documentation.
- [ ] VM install, restore, signed A/B upgrade, and rollback tests.
- [ ] Windows/macOS client acceptance and remote mobile administration.

## Interface rules

Retain Rust and Askama. Use authenticated resources for users, groups, shares,
diagnostics, and operations. Long-running changes return durable operation IDs.
Never accept arbitrary shell commands, paths, or systemd units through the
privileged API. Do not merge accounts automatically by email. Record partial
failures and never log credentials.

## Evidence and decisions

- Initial inspection: panel is read-only and unauthenticated, runs as
  `smolofis-panel` with systemd hardening, and has no Docker socket access.
- No Samba configuration or directory service exists in the repository.
- Persistence currently covers `/etc/smolofis` and `/var/lib/smolofis`, among
  other paths; directory state and ACL preservation need explicit integration.
- Local tooling: Docker client available; QEMU executable not found on PATH.
- No production identity or UI milestone has passed yet.
- Preliminary M0 container checks passed on 2026-11-04. Commands, exact
  versions, results, and remaining limitations are in the prototype protocol.
  This is not VM/client/lifecycle acceptance; M0 remains open.
- Extended container tests passed: `bash scripts/identity-lab.sh migration` and
  `bash scripts/identity-lab.sh backup`. The latter also repeats migration and
  verifies the restored system before removing its disposable containers.
- Offboarding finding: directory disablement alone leaves Gitea API tokens,
  SSH keys, and browser sessions usable. Gitea login prohibition blocks them;
  deleting tokens/keys keeps those credentials revoked after re-enabling.
  Browser sessions can resume after re-enabling if no further action is taken.
- Approved v1 policy: office-wide Gitea browser sign-out is acceptable when
  disabling an account. The container lab now uses memory sessions and zero-day
  remembered logins. A Gitea restart plus a version-checked offline cleanup of
  persisted remembered-login tokens prevents old sessions from resuming.
- `bash scripts/identity-lab.sh sessions` passed, including a working
  remember-cookie-only positive control, post-revocation replay denial,
  preservation of another user's API token, and non-mutating refusal of
  mismatched targets/unsupported database engines. This remains lab-only;
  production integration belongs to M1/M3.
- Native-machine attempt: an isolated Debian 13 amd64 OrbStack machine reports
  an LXC runtime. Standard Samba provisioning fails with
  `NT_STATUS_ACCESS_DENIED` when setting SYSVOL NT ACLs. This is not a full VM
  and does not satisfy the native filesystem/boot gate. No ACL workaround was
  applied. The provisioner now refuses to reuse a partially created domain.
- `bash scripts/identity-lab.sh live-smb` passed for both account disabling and
  group removal. Targeted worker revocation preserves the master and a live
  unrelated reader; the account and group membership are restored afterward.
- Approved decision: continue M1/M2 behind a disabled feature flag. Full VM,
  native-client, security, and lifecycle checks remain release gates. The
  current preview is read-only and loopback-only; M1 authentication and
  privileged management remain unfinished.
- The user chose direct code implementation followed by screenshot review.
- Read-only console validation: 34 Rust tests pass; Clippy with warnings denied,
  formatting, and JavaScript syntax checks pass. Chrome automation passed 23
  checks covering all five pages at desktop/mobile widths, 320px layout,
  appearance persistence, diagnostics copying, connection loss/recovery,
  malformed data, safe text rendering, and reduced motion.
- Independent UI review identified a squeezed mobile health summary; the count
  now occupies its own row, verified at 390px and 320px. Screenshots and browser
  reports are local ignored artifacts under `.impeccable/review/`.
- Console design guidance is recorded in `DESIGN.md` and
  `.impeccable/design.json`. M1 secure administration remains in progress;
  the preview does not manage
  accounts, permissions, shares, or service restarts.
- M1 boundary groundwork: `--admin-worker` accepts only protocol status over a
  filesystem-restricted Unix socket and checks the OS peer UID independently.
  Unknown fields/operations, oversized requests, incomplete requests, and
  unsafe runtime directories are rejected; handler concurrency and duration
  are bounded. There are no mutation operations or HTTP integration. See
  [worker protocol](admin-worker.md). The candidate unit is not installed or
  enabled by image construction. Linux systemd acceptance, authorization,
  durable operation auditing, and actual privileged operations remain open.
- Worker validation: 38 Rust tests pass, including caller mismatch, oversized
  and incomplete input, request timeout, protocol rejection, and unsafe startup
  paths. Clippy with warnings denied and formatting checks pass. These tests
  ran on macOS; the Linux unit has not been exercised.
- Authentication preview: real Samba LDAPS acceptance passed for administrator
  sign-in, denial of non-admins and wrong passwords, group removal/account
  disabling with observed-session revocation, CSRF-protected sign-out, and
  rejection of an untrusted CA and certificate hostname mismatch. The Linux
  binary runs on the internal lab network without published host ports.
- The authentication configuration is opt-in; when configured it protects
  console/classic pages and telemetry, with public assets/sign-in/liveness.
  The console still requires a loopback bind. At that checkpoint enrollment,
  recovery, HTTPS,
  durable actor-attributed auditing, CI credentials, explicit lifecycle
  revocation, privileged operations, and production wiring remain unfinished.
  See [sign-in configuration and limits](admin-sign-in.md).
- Final authentication evidence: 10 real Samba-container acceptance checks and
  10 Chrome checks passed, including nested administrator membership and native
  no-JavaScript form rendering. The independent UI reviewer returned `ship`
  for the authentication UI/account link only; design documentation review
  preserved the incumbent system. All 45 Rust regression tests pass (one
  manual browser fixture is ignored by default). Clippy with warnings
  denied, formatting, and dependency advisory checks pass. The Linux ARM64
  test binary is statically linked. Browser fixtures and the directory lab were
  stopped after verification; volumes were retained.
- Local enrollment/recovery preview: root-only CLI commands verify a directory
  administrator through protected stdin, atomically persist a machine/config
  binding, and rotate session generations. Offline status/lock commands do not
  contact the directory. Re-enrollment preserves device identity and permanently
  invalidates prior sessions, including sessions not observed during lockdown.
- Ten preliminary Samba-container enrollment/recovery checks passed with an
  unprivileged panel process: invalid enrollments leave no state, root-only
  ownership checks work, unsafe/malformed/symlink/foreign state fails closed,
  concurrent commands are serialized, and lockdown survives process restart.
  The preview remains opt-in through `SMOLOFIS_AUTH_STATE`; existing sign-in-only
  development mode is unchanged. See [local enrollment guide](admin-enrollment.md).
- Next M1 work: deployment transport/access and durable audited operations.
  Full VM, kernel reboot, native-client, directory repair/restore, and production
  security acceptance remain release gates.
- Enrollment regression validation: 48 Rust tests pass (one manual browser
  fixture is ignored by default), including stale-generation cleanup at the
  session capacity limit. Clippy with warnings denied, formatting, shell syntax,
  Python compilation, and whitespace checks pass. No frontend layout changed.
  The updated Linux binary passed all ten enrollment/recovery checks again;
  the disposable lab was stopped afterward and its volumes retained.
- Private HTTPS groundwork: `--private-console` fixes 127.0.0.1:8091 and the
  root-owned enrollment/configuration paths, ignoring environment attempts to
  widen or disable its security requirements. The candidate units are excluded
  from image installation/enabling. The legacy panel/proxy is unchanged.
- `console-access-check` validates unlocked enrollment, HTTPS node identity,
  online local Tailscale status, empty Serve/Funnel/service configuration,
  protected backend telemetry and Secure cookies. It only invokes local
  read-only status commands; foreground Serve activation is a separate opt-in
  unit start. No real tailnet or policy was modified.
- HTTPS evidence: 51 Rust tests pass (one manual browser fixture ignored),
  Clippy with warnings denied and formatting/syntax checks pass. Nine internal
  lab checks passed with an unprivileged backend, real TLS/certificate checking,
  Samba LDAPS sign-in, Secure-cookie/Host checks, local lockdown, and controlled
  Tailscale status fixtures. Static systemd verification passed in a disposable
  Debian 13 tools container with a stub Tailscale dependency; this does not prove
  real service execution, live Serve, certificate issuance, or tailnet policy.
  See [private access guide](admin-private-access.md). Next implementation work
  is durable actor-attributed auditing; real-node transport acceptance remains open.
- Transport decision: defer HSTS while HTTP application launchers share the
  node hostname. A host-wide upgrade policy would break those application ports;
  HTTPS and Secure cookies remain required for the private console.
- Final transport rerun passed all nine checks with an unprivileged UID/GID
  and no inherited root groups. No HSTS header is emitted while shared HTTP
  app links remain. The disposable lab was stopped and its volumes retained.

- Audit groundwork: optional `SMOLOFIS_AUDIT_FILE` adds bounded, synced JSONL
  authentication events with immutable directory GUID attribution. Sign-in fails
  closed on audit errors; sign-out/revocation still invalidate local sessions.
  Local enrollment/lockdown events, privileged-operation auditing, log archival
  integration, Linux file-ownership acceptance and fault injection remain open.
  See [audit preview](admin-audit.md). No production service wiring changed.
- Audit validation: 54 Rust tests pass (one manual fixture ignored), including
  HTTP login denial without cookie/session issuance at audit capacity. Clippy
  with warnings denied, formatting and whitespace checks pass. The complete
  durable-auditing milestone remains open; this step covers authentication only.
