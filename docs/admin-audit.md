# Authentication audit preview

Set `SMOLOFIS_AUDIT_FILE` alongside the existing authentication configuration to
opt into durable authentication events. The file must already exist, be owned
by the panel process user, have mode 0600 and one hard link, and sit under a
root-owned directory with no group/world write permission or symlink ancestors.
The application does not create directories or relax permissions automatically.
For an isolated Linux trial, a root operator can provision it with:

```sh
sudo install -d -o root -g root -m 0755 /var/lib/smolofis/panel-audit
sudo install -o smolofis-panel -g smolofis-panel -m 0600 /dev/null /var/lib/smolofis/panel-audit/auth.jsonl
```

Run the existing loopback authentication preview as the panel account with
`SMOLOFIS_AUDIT_FILE=/var/lib/smolofis/panel-audit/auth.jsonl`. The optional
private-console service candidate does not set this variable or grant write
access automatically. A separately opt-in single-file audit drop-in now has
native sandbox acceptance; see [service integration](admin-private-access.md).
No production unit or appliance image has been enabled by this work.

Each JSONL record contains schema version, wall-clock milliseconds since the
Unix epoch, a typed event, and an optional raw directory GUID encoded as hex.
Events currently cover successful credential verification, denied directory
sign-in, explicit sign-out, and session revocation after directory denial.
No password, username, cookie, session token, request body, URL, or arbitrary
error/message string is accepted by the event writer. Failed sign-ins have no
verified actor and use a null GUID. A separate root-owned local audit preview
records enrollment authorization and completed lockdown with optional `actor_uid`
attribution; see [local command semantics](admin-enrollment.md). Malformed
request denials, throttling events, and general operation auditing remain unfinished.

Local recovery events now include correlated operation IDs and typed outcomes;
the latest applied receipt is committed atomically with enrollment state. See
[operation semantics and crash recovery](admin-operations.md). In particular,
an authorization record is not a completion record, and completion-log failure
can coexist with an applied state receipt. Normal HTTP event shape is unchanged.

Writes use an in-process mutex plus an OS file lock across writers, append a
complete JSON line, and sync data before reporting success. A 16 MiB limit
bounds storage. Under the same file lock, the writer checks that a nonempty
log ends with a newline. An incomplete crash tail blocks later appends without
altering the existing bytes; the writer never silently repairs history.
On incomplete-tail/append/sync/capacity failure, a verified sign-in does not
issue a new session; a sign-out or directory revocation still removes the local
session, with an error response when its event cannot be persisted. Successful
credential verification is recorded before session allocation, so capacity or
concurrent state changes can prevent the final session even after that event.

The log is durable local history, not tamper-proof evidence: the panel process
can write it, root can alter it, crashes can leave a partial final line, and no
hash chain, remote collector, automatic repair, rotation, or retention scheduler
exists yet, apart from the explicit root-only archival command described below. The newline check detects incomplete tails, not arbitrary corruption
or tampering in earlier records. Wall-clock correctness follows the appliance clock. The descriptor
stays open for the process lifetime. To archive at capacity, stop every writer,
copy the log to protected archival storage, provision a new empty file, and
restart. Use the same procedure for an incomplete crash tail, retaining the
original bytes for diagnosis. Do not truncate a log or replace it manually while writers run. Writers built with the descriptor identity guard reject appends after replacement or permission changes; older binaries lack this protection. Do not rerun the
provisioning command over an existing log; it would erase that history.

This is a preliminary authentication audit step. Durable actor-attributed
privileged-operation records, local recovery auditing, disk-full fault injection,
concurrent process stress, Linux ownership checks, and deployment acceptance
remain release gates.

Local regression evidence (2026-11-09): partial crash tails are retained and
block appends; HTTP sign-in issues no session/cookie when the log has an
incomplete tail or is at capacity. Four independent test processes append 64
parseable records, and a read-only descriptor reports failure without altering
the file. This macOS check does not substitute for Linux ownership checks,
disk-full/sync fault injection, or production deployment acceptance.

Subsequent full-VM Linux evidence: nine local-command audit acceptance checks
passed with real LDAPS, root ownership, UID 65534 rejection, unsafe-file
rejection, a real tiny-tmpfs ENOSPC fault, and independent root CLI writers.
Enrollment failed closed and lockdown persisted despite audit failures. See
[local recovery audit evidence](admin-enrollment.md). This validates the local
command preview; web-process Linux audit ownership is covered below. Sync-error
injection, service hardening, archival, and operation-result correlation remain pending.

Full-VM web-process audit acceptance subsequently passed on 2026-11-09:
seven checks ran the static x86_64 binary as UID/GID 65534 with no root
supplementary group or effective capabilities, using real LDAPS and a
root-controlled enrollment. Linux startup rejected missing, root-owned,
world-readable, symlink and hard-link audit files, and writable parent paths.
A healthy panel-owned 0600 file recorded the verified actor GUID without
usernames/passwords or a root-operator attribution.

With an incomplete tail, sign-in returned 503 without a session cookie or a
modified log. Sign-out returned 503 for the audit failure but deleted the
cookie and removed its session; raw-token replay returned 401. A real ENOSPC
fault on a disposable 64 KiB tmpfs also blocked session creation. To force the
next append to allocate a page, the test aligned the log using a clearly
synthetic typed JSON event plus whitespace before filling the filesystem.

During ENOSPC, directory-group removal revoked the observed session even
though writing the revocation event failed: the first request returned 503,
later replay returned 401, and re-adding membership did not revive it. A
separate still-authorized session remained usable until local lockdown.
Lockdown with a broken local audit channel persisted the access lock while
the web log was also full. Protected requests returned the intentional 503
locked-service response; after re-enrollment, old-token replay returned 401.
This confirms removal/revocation remains effective when recording fails; it
does not claim every previously issued token was revoked by group removal
without an observed request or explicit lifecycle action.

All writer processes were stopped and the tmpfs unmounted. A freshly
provisioned file and restarted writer restored sign-in. Recovery never
truncated or replaced a live writer's log. This web-process check alone is not
systemd sandbox acceptance; native unit evidence follows below. Sync-error
injection, automatic retention, and a complete operation/result journal remain pending.

Subsequent native systemd evidence is recorded in the
[private backend sandbox guide](admin-private-access.md): actual candidate
backend/worker units passed nine checks, including the required single-file
audit exception, explicit read-only `/run`, protected home paths, active seccomp,
zero effective capabilities, fixed loopback binding, atomic lockdown visibility,
and kernel peer-UID enforcement. Unit/worker cleanup passed. Real Serve/HTTPS
transport execution, sync faults, retention, and correlated operation results
remain pending.

Private evidence, including event archives and binary SHA256, is retained at
`/var/lib/smolofis-identity-machine-lab/web-audit-303159e9c4/` in the guest.
To reproduce after building/staging the reviewed Linux binary and passing the
[local audit fixture](admin-enrollment.md):

```sh
python3 scripts/dev/identity/vm/lab.py check-web-audit
```

The runner uses only the restored VM's pinned loopback SSH port; HTTP binds
guest loopback port 18094 without host publication. The harness refuses
containers or an installed appliance. A reboot removes `/run` fixtures, so
rerun `check-local-audit` first. The web test verifies its binary hash matches
that accepted local fixture. Production services and flags are unchanged.


### Explicit local-root archival (candidate)

`smolofis-panel --auth-audit-archive /absolute/events.jsonl /absolute/archive-directory`
preserves a bounded local root-owned log, including incomplete trailing bytes,
and replaces it with a fresh empty log. Provision the archive directory with
root ownership and mode 0700 first; all ancestors must satisfy the normal trusted
directory checks. The source must be root-owned, regular, mode 0600, and have
one hard link. Panel-owned HTTP logs are outside this command's scope.

Before first use, stop all writers, install the binary containing the descriptor
identity guard, and restart writers after archival. Each running writer keeps
its descriptor and must restart to use the new file. Do not use this command
with older writer binaries. The command refuses a busy log rather than waiting.
It saves exact bytes and a SHA256 manifest as separate mode-0400 files and syncs
them before replacing the live path. The manifest says `snapshot_saved`, which
is not evidence that replacement completed. Only successful command output
reports `current_log_replaced: true`. Failures can leave retained snapshots or
a temporary replacement file; inspect locally before retrying. Nothing is
removed automatically.

The source limit is 16 MiB. The dedicated archive directory permits at most 64
entries (normally 32 snapshot/manifest pairs); unknown entries count toward this
limit. Export retained history deliberately before capacity is exhausted. There
is no automatic archive discovery, deletion policy, scheduler, or browser API.
Known-ID history lookup can inspect a saved snapshot by setting
`SMOLOFIS_LOCAL_AUDIT_FILE` to its absolute path.

Host tests cover byte-preserving archival of a crash tail, protected file modes,
busy-log refusal, archive capacity refusal, and stale-descriptor write refusal.
Native Linux acceptance passed eight checks on 2026-11-09: root ownership,
protected modes, link and size refusal, busy and concurrent archival, capacity
refusal, real SIGXFSZ interruption during snapshot writing and successful retry,
stale enrollment descriptor refusal before state mutation, and fresh-writer
recovery. Nine local audit regression checks passed against the same binary.
Private evidence is retained in the isolated guest at
`/var/lib/smolofis-identity-machine-lab/audit-archive-c396da14f8/`, with binary
SHA256 and retained snapshots. Run `lab.py check-local-audit` followed by
`lab.py check-audit-archive` after staging a rebuilt Linux binary to reproduce.
Five Linux syscall fault checks also passed: a lab-only ptrace harness skipped
each real `fsync` in turn and returned EIO. Failures at snapshot, manifest,
archive-directory, and replacement-file sync preserved the original live log;
failure at the final source-directory sync left an empty replacement and reported
that replacement had already occurred. Recoverable snapshot bytes remained in
all five cases. Evidence:
`/var/lib/smolofis-identity-machine-lab/archive-sync-9df356b9a2/`.

An abrupt QEMU termination test passed after rename and before the final directory
sync. The host killed only the verified task-owned restored VM container with
SIGKILL, restarted it, and confirmed a changed kernel boot ID, intact synced
snapshot/manifest, valid protected live log, and matching SHA256. Evidence:
`/var/lib/smolofis-identity-machine-lab/archive-power-4f35a32908/` and the matching
host record in `work/identity-m0-vm/`. Reproduce using `lab.py check-archive-sync`
and `python3 scripts/dev/identity/vm/check-archive-power.py`; the latter requires
the owned restored lab VM and interrupts its running services. The tracer is
lab-only and adds no fault-injection interface to the panel binary.

This models process/VM interruption and syscall errors. Host/device storage
caches remain powered; physical power-loss durability is not established.
This candidate is not a production rollout.


### Recovering an interrupted archival command

Keep writers stopped while inspecting the live path and retained files. A saved
manifest describes snapshot bytes; it does not prove successful replacement.
Never delete a partial snapshot or temporary replacement merely because the
command returned an error.

| Observed state | Recovery action |
| --- | --- |
| Original live log still present | Preserve partial evidence; resolve the storage error, then retry archival into available protected capacity. |
| Empty live log and complete snapshot/manifest | Verify snapshot length and SHA256 against the manifest, keep the archive, then restart writers with the guarded binary. |
| Directory-sync error after replacement | Inspect both live log and snapshot before restart; a reboot may expose either the original or replacement live inode. |
| Missing live log, hash mismatch, or unexpected bytes | Keep enrollment closed and preserve all files for local diagnosis; do not manufacture a completion or overwrite history. |

These steps are a root maintenance procedure. Archive browsing/export workflow and automatic retention are still pending;
packaged root maintenance commands are described below.


### Packaged local maintenance commands

The root admin wrapper includes these explicit opt-in commands:

```sh
sudo smolofis-admin console-audit-init
sudo smolofis-admin console-audit-archive
```

Initialization requires an existing trusted `/var/lib/smolofis` parent. It creates
`/var/lib/smolofis/local-audit` and its `archives` subdirectory with mode 0700,
and `events.jsonl` with mode 0600. Existing files are validated without truncation,
replacement, or tail repair. Existing directories must be trusted and mode 0700.
If provisioning fails partway through, inspect and correct the reported problem;
rerunning initialization preserves existing history.

Once the base directory exists, `console-enroll`, `console-lock`, and
`console-operation-status` automatically select its log unless root explicitly
sets `SMOLOFIS_LOCAL_AUDIT_FILE`. A missing file in an opted-in directory remains
an audit error: enrollment fails closed, while lockdown persists and reports the
audit failure. An absent base directory preserves the previous opt-in behavior.
Direct panel invocations still require the environment variable explicitly.

Before maintenance, stop long-lived root writers and allow in-flight enrollment
commands to finish. Archive only after all writers use the descriptor guard.
The archive command uses fixed standard paths and does not stop services, reopen
descriptors, delete archives, or grant browser access. New root commands naturally
open the replacement log. Restart any long-lived writer before using it again.
An environment override selecting another log is rejected for maintenance to
avoid archiving a different log from the one the operator intended. Use the
root panel CLI explicitly for custom paths. Historical inspection may select an
archive through the environment override described above.

The image builder already packages `smolofis-admin` and the panel binary; these
commands need no new enabled unit or scheduled job. Local root history and the
HTTP panel user's audit log remain separate. Image publication and production
upgrade acceptance are separate release gates.


Packaged workflow acceptance passed seven native Linux checks on 2026-11-09,
using the actual wrapper and binary at their installed paths inside a private
mount namespace. Coverage includes pre-opt-in behavior, private provisioning,
repeat initialization preserving bytes and inode, automatic log selection,
archival and fresh enrollment, explicit archived-ID lookup, root/argument/override
guards, missing-log enrollment refusal with successful lockdown, unsafe symlink
refusal, and crash-tail preservation. Nine local audit regression checks also
passed against the same binary. Private reports:
`/var/lib/smolofis-identity-machine-lab/audit-workflow-c727bd2dc4/` and
`/var/lib/smolofis-identity-machine-lab/local-audit-3c5a54ee22/`.
Reproduce with `lab.py check-local-audit` and `lab.py check-audit-workflow`
after staging a rebuilt Linux artifact. Test mounts disappear with the namespace;
existing guest configuration and workstation/production installations are not
replaced. Signed-image, upgrade, and rollback acceptance remain pending.
