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
