# Local console enrollment and recovery preview

Enrollment binds an already-configured directory administrator to this appliance.
It creates no directory accounts or groups. The first administrator must already
belong to the dedicated group in the [sign-in configuration](admin-sign-in.md).
Root access through the local console or SSH is the ownership check.

This is opt-in preview tooling. It does not enable production administration,
remove the loopback restriction, configure HTTPS, or repair a failed directory.
The image's default panel service is unchanged.

## First enrollment

Prepare `/etc/smolofis/panel-auth.json` using the sign-in guide. Configuration,
CA, reader-password files, and all their parent directories must be root-owned,
with no group/world write permission or symlinks. The unprivileged panel account
needs read access and directory traversal. Keep the reader password private,
for example mode 0640 with the panel group; the config and CA contain no password.
The enrollment directory must already exist. The appliance must have an initialized,
stable `/etc/machine-id`, as provided by systemd on the installed appliance.

From the local console or an authenticated SSH session:

```sh
sudo -v
systemd-ask-password 'Office administrator password:' | sudo smolofis-admin console-enroll alice
sudo smolofis-admin console-status
```

The password prompt is hidden. The helper reads the password from stdin; it
refuses an interactive stdin so terminal echo cannot expose it. Passwords are
never command-line arguments. A final CR/LF is ignored, and the input buffer
is zeroized when dropped. Canceling the prompt or entering invalid credentials
does not create a valid enrollment.

The helper verifies the password over certificate-checked LDAPS, confirms enabled
administrator membership, and records the immutable directory GUID. It writes
`/etc/smolofis/panel-enrollment.json` atomically with a random device identifier,
a binding to the local machine ID, an exact configuration digest, and a random
session generation. The record contains no passwords or browser tokens. Its
mode is 0644 so the unprivileged panel can read it; it remains root-writable only.
Concurrent modifying commands are serialized with an OS file lock.

Start a loopback preview with both configuration and enrollment selected:

```sh
sudo -u smolofis-panel env \
  SMOLOFIS_CONSOLE_ENABLED=true SMOLOFIS_BIND=127.0.0.1:18091 \
  SMOLOFIS_AUTH_CONFIG=/etc/smolofis/panel-auth.json \
  SMOLOFIS_AUTH_STATE=/etc/smolofis/panel-enrollment.json \
  /usr/local/bin/smolofis-panel
```

The configured origin must match this address. Do not run this as root: use the
existing panel account with read access to the files. This is a manual preview
invocation, not an instruction to change the production systemd unit.

Both environment variables are necessary. A panel started without
`SMOLOFIS_AUTH_STATE` remains the earlier directory-sign-in development preview
and does not honor enrollment locks. A supplied state with no auth config fails
startup. A missing, foreign, or mismatched supplied state also fails startup;
there is no automatic fallback to unauthenticated access. A valid locked record
allows public liveness and the sign-in page, but denies protected requests and
sign-in submissions in this development mode. The stricter `--private-console`
transport candidate requires unlocked enrollment at startup and refuses to start
while locked.

## Local recovery when the directory is unavailable

```sh
sudo smolofis-admin console-status
sudo smolofis-admin console-lock
```

These commands inspect local enrollment and lock its access without any directory
connection. Status reports `directory_checked: false`: it does not claim the
directory is healthy. Locking rotates the session generation and blocks the next
protected request on panels using that state. GET `/healthz` remains public.
Lockdown survives a panel restart. Existing account forms can still sign out
locally. No emergency web password or directory-authentication bypass is created.

Restore directory connectivity using the appliance's operator procedures, then
repeat `console-enroll` with a working administrator. Re-enrollment keeps the
same device identifier, verifies credentials again, unlocks access, and rotates
the session generation. Previously issued sessions cannot resume, even if no
browser request happened between lockdown and re-enrollment. Failed directory
verification leaves the existing enrollment untouched.

When changing configuration, lock access first, edit the root-owned config,
re-enroll against that exact file, and restart the panel. Even whitespace edits
change the digest. A running process remains bound to its loaded configuration;
it cannot switch to a newly enrolled configuration without restart. Reader-secret
and CA file rotations also require restart to reload those files.

Never copy an enrollment record between appliances: its machine-ID binding will
be rejected. Full backup/restore and native boot acceptance remain release gates.
If the record is missing, malformed, or insecurely permissioned, access fails
closed. A local root operator must restore the correct record or quarantine the
invalid file before fresh enrollment, then restart the panel to load the new
enrollment identity; no network endpoint repairs it. The tool
refuses symlinks and foreign state rather than silently replacing them.

## Direct binary commands and testing

Local audit preview is separately opt-in through `SMOLOFIS_LOCAL_AUDIT_FILE`.
Use a dedicated, pre-existing root-owned 0600 file with one hard link under a
trusted root-owned directory; do not share the panel-owned authentication log,
enrollment state, or directory configuration file. For a new isolated lab log:

```sh
sudo install -d -o root -g root -m 0755 /var/lib/smolofis/local-audit
sudo sh -c 'umask 077; set -C; : > /var/lib/smolofis/local-audit/events.jsonl'
sudo env SMOLOFIS_LOCAL_AUDIT_FILE=/var/lib/smolofis/local-audit/events.jsonl \
  smolofis-admin console-lock
```

The noclobber creation intentionally fails if the file already exists. Never
truncate an existing log. Enrollment can use the same `sudo env` prefix after
the protected password pipe. Setting the panel's `SMOLOFIS_AUDIT_FILE` does not
enable local-command auditing, and the local variable does not configure the
panel. Defaults remain unchanged when the local variable is absent.

After verified administrator authentication, `local_enrollment_authorized` is
synced before enrollment state changes. Failure recording that authorization
prevents enrollment and leaves existing access/lock/generation intact. This is
not a completed-operation claim. New [operation receipts](admin-operations.md)
correlate authorization and completion with an independent ID and atomically
persist the latest applied result in schema-v2 state. If completion logging
fails after state commits, the command explicitly reports that partial result;
inspect `console-status` rather than assuming an error means nothing changed.

Lockdown persists the locked state and rotated generation first, then writes
`local_console_locked`. An unavailable/full/broken audit log cannot prevent
locking access. If recording fails after persistence, the command returns an
explicit error saying the console is locked and the audit needs repair; use
`console-status` to inspect it. Status does not require an audit log or directory.

Local events include `actor_uid: 0`, identifying the root command process rather
than a human behind sudo/SSH. Enrollment also includes the verified administrator
GUID; lockdown does not attribute the root operator to that directory user.
No username, password, path, session generation, or arbitrary error text enters
the event. Unit tests alone do not establish Linux/root permission acceptance;
full-VM evidence is recorded below. Known-ID historical lookup is now available
through `console-operation-status`; see the operation guide. Denied-command
auditing and a complete durable operation journal remain pending.

The admin wrapper uses these fixed paths; the binary also accepts explicit
absolute paths for isolated tests:

```text
smolofis-panel --auth-enroll /absolute/config.json /absolute/state.json username
smolofis-panel --auth-status /absolute/state.json
smolofis-panel --auth-lock /absolute/state.json
```

All three require root, including status. Enrollment/recovery is a Linux
appliance feature using `/etc/machine-id`; the normal unenrolled development
preview continues to run on macOS.

```sh
bash scripts/identity-lab.sh panel-enrollment /absolute/path/to/linux/smolofis-panel
```

The isolated Docker test uses synthetic directory accounts, a unique container
machine ID, root local commands, and an unprivileged panel process. It publishes
no host ports. This proves preliminary behavior, not full VM, recovery-from-backup,
native client, production deployment, or durable auditing acceptance.

Full-VM local-audit acceptance passed on 2026-11-09 with the static x86_64
Linux binary and isolated Debian 13 guest, including real LDAPS verification.
Nine checks cover denied non-admin/wrong credentials, root/GUID attribution,
UID 65534 command rejection, generation rotation, incomplete-tail handling,
missing/wrong-owner/wrong-mode/symlink/hard-link audit files, protected-file
alias rejection, real ENOSPC, and concurrent root writers. Lockdown remains
available and status remains local throughout audit failures. A disposable
64 KiB tmpfs was filled to produce ENOSPC and unmounted in `finally`; the
guest root filesystem and office server were not filled by that fault.

The private report and synthetic credentials are retained under
`/var/lib/smolofis-identity-machine-lab/local-audit-3ecff7d1cd/` in the guest.
The report records the binary SHA256, kernel, architecture, checks, runtime
path, and tmpfs cleanup. Runtime fixtures under `/run` disappear at reboot;
this is not appliance persistence or service sandbox acceptance.

To reproduce, build from context `src-dashboard` with the test-only
`scripts/dev/identity/vm/Panel.Dockerfile` using `--platform linux/amd64`.
It uses locked Cargo dependencies; the accepted build used Rust 1.98.1.
Extract `/smolofis-panel` from the stopped artifact image into ignored
`work/identity-m0-vm/panel-audit`, then run:

```sh
python3 scripts/dev/identity/vm/lab.py check-local-audit
```

The runner stages only the reviewed binary/script, pins SSH to the restored
guest's loopback port, and refuses a non-x86_64 ELF artifact. The test itself
requires the owned full-kernel VM markers and refuses an installed appliance
or container. Production wiring, service sandbox behavior, archive retention,
and a complete correlated operation journal remain pending.
