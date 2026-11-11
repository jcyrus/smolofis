# Administration UI implementation tracker

Build a responsive, umbrelOS-inspired administration console on the existing
Rust/Axum/Askama panel. Primary tasks: people and groups, shared folders, and
service diagnosis. Central identities apply to hosted services; employee
computer sign-in is deferred.

Current production status (2026-11-11): `192.168.1.17` runs dev.82 on active
system-a. Administration remains disabled; system-b/dev.80 is retained.

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

## R1 — Deliver validated audit/identity groundwork with administration disabled

- [x] PR #22 merged as `1e38c1033676eac4482d098c8179450c4bdc4a9e` on 2026-11-09.
- [x] Main [CI run 42592333876](https://github.com/jcyrus/smolofis/actions/runs/42592333876) passed compilation, ISO assembly, smoke, BIOS/UEFI installation/update/rollback, and release publication.
- [x] Published nightly `0.1.0-dev.78`; downloaded manifest signature verifies against `config/release/allowed_signers`. Signed ISO SHA256 `2a91dfa81d4683123c3487194a6a323294b3449df6ef27214b3a674449555313` matches GitHub asset metadata. Full ISO download/checksum verification remains part of server staging.
- [x] Upgrade `192.168.1.17` to dev.78 through its signed updater with pinned manifest/ISO SHA256 and fresh backup `20261109T094858Z`. Backup tar archives and database dump parsed successfully. Reboot verified dev.78 on promoted system-a, boot ID `917cebe9-f650-4e12-b742-eddd54f4a511`, phase ready, no failed units, healthy core containers, Gitea 28.2.0, persisted Coolify user, working operator session and public dashboard. Durable machine ID initialized and matches runtime; system-b/dev.73 retained. Console/login/assets return 404, candidate admin units remain absent and no admin flags are configured (2026-11-09).

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
- [x] Prove group-based read-only/read-write/denied SMB access from native Windows and macOS. Windows 23H2 acceptance completed with user-reported operations and server-correlated authentication/authorization (2026-11-09).
- [x] Native Windows 11 23H2 writer acceptance: user confirmed an OK mapping as Alice, retained evidence-file read, probe write/read and successful deletion through the restricted temporary lab relay (2026-11-09). Reader and outsider acceptance subsequently passed below.
- [x] Native Windows reader acceptance: user confirmed an OK mapping, retained evidence-file read and Access denied on probe creation as Reader (2026-11-09). Outsider acceptance subsequently passed below.
- [x] Native Windows outsider acceptance: user received Windows System Error 5; server trace confirms M0\outsider authenticated with NTLMv2 and was explicitly denied projects. Credential-free evidence retained at `windows-acceptance-bd98aa8abf`; temporary proxy/listeners/firewall/timers/SSH masters cleaned up and normal lab logging restored (2026-11-09).
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
- [x] Finalize v1 topology and full-VM/native-client evidence: native Samba AD on a full-kernel appliance OS, separate domain-member file service, and explicit Gitea LDAP/account mapping. M0 feasibility accepted for the tested scope; production packaging, restricted operations, HTTPS/mobile and lifecycle integration retain M1/M3–M6 gates (2026-11-09).
- [x] Consolidate [M0 evidence and conditional topology recommendation](identity-m0-evidence.md). Native Windows evidence completed the remaining M0 client gate on 2026-11-09.

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
- [ ] Complete private HTTPS production integration. V1 is restricted to the owner account/devices; distinct employee/admin tailnet policy testing is deferred by user decision (2026-11-10), not proven. See [real-tailnet evidence](admin-private-access.md#real-personal-tailnet-acceptance--2026-11-09).
- [x] Personal-tailnet full-VM acceptance: nine real HTTPS/authentication peer checks and user-confirmed phone cellular HTTPS/sign-in passed (2026-11-09).
- [x] Certificate issuance and controlled renewal/rotation passed without exporting private keys or changing the clock. Natural scheduled renewal remains unobserved (2026-11-09).
- [x] Six actual HTTPS/Serve lifecycle checks passed, including stop, process crash, backend dependency, direct-backend isolation and ordinary reboot. Lab accounts/runtime units removed; node logged out and tailscaled stopped/disabled (2026-11-09).
- [x] Actual backend/worker systemd execution: nine native VM checks passed for the single-file audit write exception, explicit read-only `/run`, home isolation, capabilities/seccomp, fixed loopback/authentication, atomic recovery, kernel peer UID, bounded typed protocol, AF_UNIX-only worker, and cleanup (2026-11-09). Personal-tailnet HTTPS/Serve/mobile acceptance subsequently passed; distinct employee network-policy isolation remains pending.
- [ ] Separate privileged service with restricted Unix socket and typed, allowlisted operations.
- [ ] CSRF protection, login throttling, secure sessions, and secret-free auditing.
- [x] Root-only historical operation lookup by ID: current receipt or bounded read-only history; coherent completion required, nonblocking busy-log behavior, partial-tail reporting, and unknown outcomes preserved. 67 host tests and eight native Linux history checks passed (2026-11-09). Listing, archive discovery/retention, superseded crash reconciliation, and the general journal remain pending.
- [x] Candidate root-only bounded audit archival and writer descriptor guard implemented; 69 host tests passed, one manual fixture ignored, and Clippy passed (2026-11-09). Exact bytes and SHA256 manifest retained before replacement; no automatic deletion.
- [x] Native Linux archival acceptance: eight checks passed for root ownership/modes/links, exact bytes/hash, busy and concurrent writers, capacity refusal, real SIGXFSZ snapshot interruption/retry, stale enrollment descriptor refusal, and fresh-writer recovery. Nine local audit regressions also passed (2026-11-09).
- [x] Archival sync-failure and abrupt-VM recovery: inject EIO into all five Linux fsync calls; terminate the owned QEMU container after rename but before parent-directory fsync and verify the synced archive, valid live log, and changed boot ID after restart (2026-11-09). This does not simulate host/device cache power loss.
- [x] Add packaged root `console-audit-init` and `console-audit-archive` workflow: non-destructive initialization, automatic standard-log selection, safe override handling, missing-log enrollment refusal and lockdown recovery. Seven native packaged-wrapper checks and nine local audit regressions passed; 69 host tests, Clippy and ShellCheck passed (2026-11-09). Existing image packaging includes the wrapper/binary without enabling services.
- [x] Wire audit/enrollment persistence assertions into the existing signed A/B ISO install test after update promotion and corrupted-slot fallback, including administration-disabled checks. Three native fixture component checks passed; complete ISO execution is still pending (2026-11-09).
- [x] Open draft [PR #22](https://github.com/jcyrus/smolofis/pull/22) on isolated branch `admin/audit-lifecycle-validation` and start full [CI run 42577895520](https://github.com/jcyrus/smolofis/actions/runs/42577895520). Initial run passed compilation, ISO assembly and smoke tests; BIOS/UEFI install jobs stopped at an overly narrow QEMU-only fixture guard. The helper now accepts QEMU/KVM with unsupported-VM refusal covered by four native component checks; full rerun, publication and server upgrade remain pending.
- [x] Fix installed-image machine-ID persistence exposed by CI run 42582007436: restore a per-device durable ID in initramfs before systemd, keep enrollment binding strict, and hash both ID files in the lifecycle gate. Eight native checks (including real initramfs packaging) and four lifecycle fixture checks passed; full image rerun remains pending.
- [x] Full BIOS and UEFI ISO install tests passed in run 42585992507: locked enrollment, both machine-ID files, live log/archive/manifest and historical lookup survive signed A/B promotion and corrupted-slot fallback; administration stays disabled. The run's separate smoke fixture failed on runner-owned persistence; its offline ownership repair passed native checks and awaits CI rerun.
- [x] Complete [PR #22 CI run 42589005839](https://github.com/jcyrus/smolofis/actions/runs/42589005839) passed on `2d1a0df`: compilation/lint/advisories/unit tests, ISO assembly, smoke boot, BIOS and UEFI installation, signed test-bundle A/B upgrade and corrupted-slot rollback. Runtime/saved machine IDs, locked enrollment and audit history survive; administration stays disabled. Release job skipped for the draft PR; no nightly publication or production upgrade occurred (2026-11-09).
- [x] Publish and validate production-signed dev.78 with administration disabled, then complete the backed-up server rollout and post-checks under R1 (2026-11-09).
- [ ] Assess physical storage power-loss durability before claiming hardware-level guarantees; VM termination and syscall fault tests do not establish device-cache power-loss behavior.
- [x] Local operation receipts/correlation: schema-v2 state atomically records the latest applied operation ID; audit authorization/completion/uncertainty are correlated. Host regressions, native completion-write process crash, legacy-v1 read, and real VM-reboot receipt recovery passed (2026-11-09). Known-ID history lookup is recorded above; general operation journaling and appliance upgrade/rollback integration remain pending. See [operation receipts](admin-operations.md).
- [x] Audit crash-tail hardening: preserve incomplete records and refuse new appends; sign-in issues no session/cookie on tail/capacity failure. Four-process append and write-error regressions pass on macOS (2026-11-09). Complete operation/recovery auditing remains pending; personal-tailnet HTTPS/Serve acceptance subsequently passed.
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


- Live production status rechecked after starting image validation (2026-11-09):
  `192.168.1.17` reports dev.73, `smolofis-panel` active, and `/console` HTTP 404.
  No `SMOLOFIS_CONSOLE_ENABLED` service environment entry was present.
  Administration remains disabled; no production configuration was changed.

### Account-only private access follow-up — 2026-11-10

- [x] Record user-approved v1 owner-account/devices-only scope. Separate employee network-policy testing is deferred and must precede adding other users or sharing administration.
- [x] Clean up reactivated HTTPS trial `135d5487`: temporary accounts/configuration and foreground Serve removed; lab logged out and tailscaled stopped/disabled.
- [x] Configure production Tailscale `accept-dns=false` to preserve NetworkManager DNS; resolver hash unchanged and external DNS lookup passed. Add the same default to source connection/autoconnect helper.
- [ ] Validate DNS health after an authorized production tailnet connection. The existing resolve1 warning remains in stopped-node status; configuration alone does not establish connected acceptance.

### Operation discovery follow-up — 2026-11-10

- [x] Implement root-only selected-file `console-operation-list` preview: protected bounded snapshot, 4096-operation cap, consistent completion validation, lexical IDs, unknown outcomes and explicit partial-tail status.
- [x] Validate operation listing through the packaged wrapper on native Linux: eight checks passed for standard-log discovery, read-only archive overrides, retained real crash/unknown outcomes, busy locks, ownership/mode/links, malformed/orphan records, size/missing/empty history, non-root/argument denial and unchanged bytes. Private evidence: `operation-listing-0ec98ed3f7` (2026-11-10).
- [x] Review and publish operation-listing and DNS-helper changes through PR #23/main CI and dev.80 rollout, recorded below. Archive discovery/pagination and complete operation journaling remain open.

- [x] Open draft [PR #23](https://github.com/jcyrus/smolofis/pull/23) at `278cf0c` for operation listing, DNS-preserving connection defaults and retained lab evidence. Local validation: 71 Rust tests passed, one manual fixture ignored; Clippy/formatting/ShellCheck/Python checks passed. Full [CI run 42759101013](https://github.com/jcyrus/smolofis/actions/runs/42759101013) was queued on 2026-11-10.
- [x] Verify [PR #23 CI run 42759101013](https://github.com/jcyrus/smolofis/actions/runs/42759101013) completed successfully on the latest PR head `278cf0c8e3c9d95b8123cf37c3637004995a628d`: compile/lint/advisories/unit tests, ISO assembly, QEMU smoke, BIOS and UEFI install/reboot acceptance all passed. CodeRabbit status is successful. Release publication was skipped for the draft PR (2026-11-10).
- [x] Mark PR #23 ready and merge the tested head as `bdc0e9f5bf2aa5a7f7c8557b9380187bcbcdda10` (2026-11-10).
- [x] Verify [main CI/publication run 42767153841](https://github.com/jcyrus/smolofis/actions/runs/42767153841) passed all six jobs, including BIOS/UEFI installation and release publication. Signed nightly `0.1.0-dev.80` published from merge `bdc0e9f5bf2aa5a7f7c8557b9380187bcbcdda10`; nightly tag matches that commit (2026-11-10).
- [x] Download dev.80 manifest/signature/checksum sidecar and verify against `config/release/allowed_signers`. Signed ISO SHA256 `94ac377cf64fe94fd555088884aef2d3fc899ddbd887b9c5062b8842b711bd14` matches the sidecar and GitHub asset digest; ISO size 962904064 bytes. Full ISO download/hash remains part of server staging.
- [x] Upgrade production to dev.80 using the pinned signed ISO, full downloaded SHA256 verification and fresh readable backup `20261110T072254Z`. System-b promoted after ready; system-a/dev.78 retained. Boot ID `fba54147-5879-4e24-9aa5-9fa60256568c`; both machine-ID hashes unchanged, no failed units, healthy core services, persisted Coolify user, public dashboard 200 and admin/login/assets 404. Candidate admin units and enrollment remain absent. Staging/evidence: `/var/lib/smolofis/dev80-stage.VIug5A/` (2026-11-10).
- [ ] Validate Tailscale DNS preference durability after actual enrollment: the unjoined node reset `CorpDNS` to true across reboot despite a persistent state directory. Reapplied false live; installed dev.80 manual/autoconnect helper explicitly passes `--accept-dns=false`. Connected acceptance remains pending; node still `NeedsLogin`, Serve empty.

### Durable receipt journal follow-up — 2026-11-10

- [x] Implement typed root `local_receipt_preserved` checkpoints from validated and synced current state. Enrollment checkpoints before replacing access; lockdown commits access removal first and reports any subsequent journal failure. Busy checkpoints fail without waiting. Known-ID lookup and listing retain preserved applied outcomes after superseding actions.
- [x] Host validation: 73 tests passed, one manual browser fixture ignored; Clippy, formatting, Python compilation and whitespace checks passed.
- [x] Eight native full-kernel VM checks passed: real SIGXFSZ completion crash, explicit tail archival and superseded receipt recovery, repeated preservation, busy/corrupt journal lockdown behavior, real LDAPS enrollment refusal/recovery ordering, foreign-state/non-root refusal and synthetic-account cleanup. Evidence: `receipt-journal-a7dbfc18d1` (2026-11-10).
- [x] Native lockdown checkpoint fault/reader acceptance: 16 checks passed (baseline sync order, EIO at all six fsync/fdatasync calls, eight SIGKILL boundaries, and read-only pre-checkpoint binary compatibility). Evidence: `receipt-faults-13f87514ee`. State/parent and audit flush calls were actually traced; older history lookup reports unknown/unavailable and preserves bytes (2026-11-10).
- [x] Native enrollment fault acceptance: 19 checks passed using real verified LDAPS (seven-sync baseline, EIO at all seven calls, eleven SIGKILL boundaries), preserving locked state before replacement and honest uncertainty afterward. Synthetic directory user/group removed; enrollment password supplied by pipe. Evidence: `enrollment-faults-c028bd2b2b` (2026-11-10).
- [x] Shared lab-only tracer regression: all 16 lockdown/older-reader checks passed again after extraction to `receipt_fault_tools.py`; evidence `receipt-faults-1d0423355e` (2026-11-10).
- [x] Complete checkpoint-bearing appliance signed A/B upgrade/fallback acceptance through PR #24 and main CI, and publish dev.82, recorded below. Physical storage durability remains pending under M1. Emergency lockdown retains the documented interval between access removal and prior-receipt checkpointing.
- [ ] General queued/running operation journal/executor, privileged worker mutations, HTTP integration and archive discovery remain unfinished. This checkpoint work does not complete M1.

### Checkpoint appliance lifecycle fixture — 2026-11-10

- [x] Extend installed-image acceptance with protected synthetic checkpoint-only history and its hash. Previous-nightly preparation verifies legacy history/bytes; upgraded candidate must resolve the superseded ID and list the checkpoint. Existing signed-update promotion and corrupted-slot rollback checks both enforce it; administration stays locked/disabled.
- [x] Five native lifecycle fixture component checks passed: previous-reader preparation/new-reader verification, QEMU/KVM guard with unsupported-VM refusal, overwrite refusal, changed state detection and changed checkpoint detection. Evidence: `audit-workflow-3e920e666c`. Private mounts/configuration cleaned on namespace exit; actual A/B boots remain CI work.
- [x] Open draft [PR #24](https://github.com/jcyrus/smolofis/pull/24) at `9369bd314eddc867acb24e8b3c648944c3c1e973` with checkpoint implementation and native/installed-image acceptance. Local validation: 73 host tests passed, one ignored; Clippy, formatting, ShellCheck, Python syntax and staged whitespace checks passed (2026-11-10).
- [x] Verify [PR #24 CI run 42800140945](https://github.com/jcyrus/smolofis/actions/runs/42800140945) passed on latest head `9369bd314eddc867acb24e8b3c648944c3c1e973`: compile/lint/advisories/unit/Python checks, ISO assembly, smoke, BIOS and UEFI installation. Both firmware logs explicitly confirm checkpoint-only history readability after signed update promotion and corrupted-slot fallback. Publication skipped for draft PR (2026-11-10).
- [x] PR #24 merged as `a2f3da08afcebdb5b4be4345a6c3f588dc747f29` on 2026-11-10; merge confirmed via GitHub.
- [x] Verify [main CI/publication run 42840650291](https://github.com/jcyrus/smolofis/actions/runs/42840650291) passed all six jobs on merge `a2f3da08afcebdb5b4be4345a6c3f588dc747f29`, including BIOS/UEFI tests and release publication. Nightly dev.82 published; downloaded manifest signature verifies against the release signer. Signed ISO hash `d7e4435b0eb2e2cbc845f2267982180f32147ce8a86c61dc8d300266509288c1` matches the checksum sidecar and GitHub asset digest. Full ISO download/hash verification remains part of staging. Older-version writers and hardware power-loss durability retain separate limits.
- [x] Deploy dev.82 on 2026-11-11 using the installed updater, trusted manifest signature and full downloaded ISO hash `d7e4435b0eb2e2cbc845f2267982180f32147ce8a86c61dc8d300266509288c1`. Fresh backup `20261111T032720Z` tar archives and PostgreSQL dump listed successfully. System-a promoted after ready; system-b/dev.80 retained. Boot ID `f2183338-7718-412d-8f62-2339ccfac5f7`, machine-ID hashes unchanged, no failed units, healthy core containers and persisted Coolify user. Console/login/assets return 404 and candidate admin units/configuration remain absent. Tailscale `NeedsLogin`, Serve empty. Evidence `/var/lib/smolofis/dev82-stage.UNJR3R/`.

### General operation journal preview — 2026-11-11

- [x] Implement root-command journal initialization, listing, status and execution with machine binding, protected files, serialized access and atomic synced snapshots. Records carry queued/running/succeeded/failed/unknown transitions and typed root attribution. First executable kind checks runtime/saved machine identity.
- [x] Explicit recovery marks queued/running work unknown/interrupted, preserves terminal results and allows a new operation ID afterward. Terminal rewrites and new work during unfinished operations are refused. Store is bounded to 256 operations and 1 MiB.
- [x] Host validation: 76 tests passed, one manual fixture ignored. Clippy, formatting, ShellCheck and Python syntax checks passed. Final native acceptance passed 19 checks for packaged commands/listing, real identity success/failure, concurrency/root/ID guards, foreign/corrupt/unsafe stores, EIO at six sync calls and nine process-crash boundaries. Evidence: `operation-journal-56088655dc`.
- [ ] Review/publish the journal preview through CI and validate its own installed-image persistence/rollback. Worker/API integration, directory actor attribution, additional mutation kinds, retention/export and UI integration remain pending. Production remains dev.82 with administration disabled.

### General journal lifecycle and review — 2026-11-11

- [x] Extend the installed-image fixture with synthetic completed/running journal records and protected hashes. After signed promotion, verify persistence, recover the interrupted record, preserve the completed result, and execute a fresh identity check. Verify the resulting journal after failed-slot rollback.
- [x] Seven native lifecycle component checks passed using previous-binary preparation and candidate verification, with changed-journal detection, recovery/execution and fixture overwrite guards. Evidence: `audit-workflow-c13c76d582`. Actual A/B boots await full ISO CI.
- [ ] Submit the reviewed journal and lifecycle changes and pass full ISO CI before merge/publication. Production remains dev.82 with administration disabled.
