# Local recovery operation receipts

This preview adds correlation and a latest-operation receipt to local root
enrollment/lockdown commands. It does not enable worker mutations or expose an
operation-history HTTP API. Production administration remains disabled.

Each verified mutation receives an independently generated 64-character hex
operation ID. It is metadata, not an authentication token or session generation.
The command prints it on completion and includes it in uncertain/partial-failure
errors. Existing `console-status` / `--auth-status` returns `last_operation`:

```json
{"id":"<64 hex characters>","operation":"lockdown","outcome":"applied"}
```

The receipt is serialized in the same atomic file replacement as the changed
enrollment state. It is not a second file whose commit could disagree with
the state. Status remains root-only and directory-independent. A receipt
describes the latest state transition, not a complete historical journal.

When local auditing is configured, records include typed `operation_id`,
`operation`, and optional `outcome` fields alongside root/GUID attribution:

| Event | Meaning |
| --- | --- |
| `local_enrollment_authorized` | Verified authorization synced before attempting the state commit; no completion claim |
| `local_enrollment_applied` | State persistence succeeded; same ID, outcome `applied` |
| `local_console_locked` | Lock persistence succeeded; same ID, outcome `applied` |
| `local_operation_uncertain` | State persistence reported an error; outcome `unknown`, never inferred as definitely unapplied |

If enrollment authorization cannot be recorded, state is unchanged. If the
state commits but completion logging fails, access may already be applied;
the command explicitly says so, identifies the operation, and returns an error.
Inspect local status rather than assuming a nonzero exit means nothing changed.
Lockdown still writes its state/receipt before attempting auditing, so an audit
failure cannot prevent access removal.

A process crash can leave a durable authorization record with no completion
record. If the current state's receipt matches that operation ID, status
establishes that the operation is reflected in the observed state. No matching
latest receipt is not proof of failure: another operation may have superseded
it. Do not automatically retry or invent a completed result from a begin event.
Parent-directory sync failures can also leave an uncertain result even when
the replacement is visible; surviving state must be inspected after recovery.

New mutations write enrollment schema v2. The reader accepts unchanged legacy
v1 state and strict v2 receipts; malformed IDs, unknown outcomes, or a receipt
inconsistent with the lock state fail closed. Old binaries do not understand
v2 and will reject it. Production upgrade/rollback compatibility and recovery
remain M6 gates; never use an older binary to rewrite new receipt-bearing state.

No usernames, passwords, paths, arbitrary error strings, or session generations
are added to audit records. HTTP authentication event shape is unchanged.
Known-ID historical lookup is available as described below. Comprehensive
operation journaling, retention, privileged worker operations, and authenticated
UI/CI integration remain unfinished. This local preview must not be described
as those completed gates.

Validation on 2026-11-09: 64 host tests passed (one manual browser fixture
ignored), plus Clippy and formatting checks. Native root audit acceptance
passed with the rebuilt static x86_64 binary and schema-v2 state. Four separate
operation checks verified enrollment correlation, independent-process receipt
recovery, unique lockdown IDs, and partial-failure status after corrupt history.

The native crash test set child-only `RLIMIT_FSIZE` and disabled core dumps.
The authorization line was synced and state committed before the process was
killed by `SIGXFSZ` while writing the completion event. A fresh status process
read the applied receipt matching the authorization ID despite a missing
completion record and partial log tail. A subsequent lockdown saved its own
receipt even though appending to that damaged log failed. No filesystem was
filled and no service-wide resource limit was changed by this crash test.

The final receipt then survived a real VM reboot with an unchanged operation
ID and changed kernel boot ID. Status remained directory-independent. The
rebuilt binary also read unmodified legacy v1 state before that reboot.
Private evidence, including both IDs and boot IDs, is retained in
`/var/lib/smolofis-identity-machine-lab/operation-receipts-512c5fed8f/`.
Reproduce the process/crash checks after rebuilding/staging the Linux binary
and recreating the local audit fixture:

```sh
python3 scripts/dev/identity/vm/lab.py check-local-audit
python3 scripts/dev/identity/vm/lab.py check-operation-receipts
```

The receipt test stores its state in private persistent lab storage, not `/run`.
Kernel reboot verification is a separate explicit lab action; the helper does
not reboot automatically. Appliance persistence/upgrade/rollback wiring remains
an M6 gate, and actual storage sync-error injection remains separate acceptance.

The rebuilt schema-v2 binary also passed the nine root-command, seven web-audit,
and nine actual-unit sandbox regressions after reboot. Their private reports
are `local-audit-150fe8da03`, `web-audit-c9681b1398`, and
`service-audit-32e5471ff4` under the guest lab directory. These checks include
live enrollment replacement and permanent rejection of old browser tokens.

Root operators can now query a specific older ID:

```sh
sudo env SMOLOFIS_LOCAL_AUDIT_FILE=/var/lib/smolofis/local-audit/events.jsonl \
  smolofis-admin console-operation-status OPERATION_ID
```

Replace `OPERATION_ID` with the command's actual 64-character hex ID. The direct
binary equivalent is `--auth-operation-status /absolute/state.json OPERATION_ID`.
This command needs no directory configuration/connection. It does not mutate
state, repair history, create files, or append audit events. The current state
must still pass ownership/schema/machine binding checks; an absent state is
allowed for inspecting a never-completed enrollment's history.

The JSON reply distinguishes evidence sources:

| Field/value | Meaning |
| --- | --- |
| `source: state_receipt` | Current validated state contains this applied ID; audit is `not_checked` |
| `source: audit_history` | Selected root-owned history was read; `found` refers only to complete records |
| `outcome: applied` from history | Matching, coherent completion records exist; not a claim that later actions have not changed access |
| `outcome: unknown` | No complete trustworthy outcome is available; never inferred as failed |
| `audit_status: incomplete_tail` | Only complete newline-terminated records were considered; the writer still requires archival/recovery |
| `audit_status: unavailable` | Busy, unsafe, corrupt, unsupported, conflicting, or oversized history cannot establish a result |
| `audit_status: not_configured` | No local history path was selected and no matching current receipt exists |

Snapshot reads are limited to 16 MiB, use a read-only descriptor and a
nonblocking shared file lock, and accept protected read-only archives such as
mode 0400. Root ownership, no group/world access, one hard link, trusted
ancestors, and stable inode/device are required. A current matching receipt
takes precedence even if history is unavailable. Other queries remain unknown
when a writer holds the lock rather than waiting indefinitely.

The reader rejects malformed complete records, unsupported schemas, conflicting
kinds/outcomes, invalid attribution, and orphan enrollment completions. An
enrollment begin is never promoted to completion. An incomplete final fragment
is ignored, with its degraded status exposed. Results describe only the selected
file; automatic archive discovery, operation listing/pagination, tamper-proof
provenance, and reconciliation of superseded crash receipts are not implemented.

Validation: 67 host tests passed (one manual fixture ignored), Clippy passed,
and eight native Linux checks passed against retained real crash archives.
They cover older applied enrollment/lockdown, a superseded interrupted operation,
receipt fallback, absent IDs, busy writers, bad/conflicting/orphan records,
permissions/links/size limits, non-root denial, invalid IDs, and unchanged bytes.
Private evidence is retained under
`/var/lib/smolofis-identity-machine-lab/operation-history-f415566672/`.
After staging the rebuilt Linux binary and retaining operation-receipt evidence:

```sh
python3 scripts/dev/identity/vm/lab.py check-operation-history
```


## Local audit maintenance

The packaged root workflow is `sudo smolofis-admin console-audit-init` followed,
when maintenance is needed, by `sudo smolofis-admin console-audit-archive`.
Initialization preserves existing history. Once initialized, root enrollment,
lockdown, and operation lookup automatically select the standard local log.
A missing opted-in file blocks enrollment; lockdown continues to close access
and reports audit failure. Explicit root environment overrides remain available
for inspecting saved archives.

See [maintenance and interrupted-command recovery](admin-audit.md) for storage
requirements, writer restart procedure, archive bounds, and partial failures.
The archive command saves a snapshot and manifest before replacing the live log;
a saved manifest alone never proves that replacement completed. No command
automatically removes archives or enables browser administration.


## Signed-image lifecycle gate

The existing `scripts/install-test-iso.sh` now prepares a locked synthetic
schema-v1 enrollment, converts it through real root lockdown commands to
schema v2, archives two correlated operations, and records exact file hashes.
After signed A/B update promotion and after fallback from a corrupted candidate
slot, it checks the persistent enrollment/live-log/archive/manifest bytes,
protected modes and ownership, locked status, historical operation lookup,
and the administration preview remaining disabled. The synthetic fixture has
no directory configuration and never grants console access. It is created only
in the QEMU install-test guest and refuses existing enrollment/audit storage.

The fixture helper `scripts/dev/identity/verify-audit-lifecycle.sh` passed three
native Linux component checks: preparation/verification, refusing existing
fixtures, and detecting changed state bytes. Evidence:
`/var/lib/smolofis-identity-machine-lab/audit-workflow-42dbf72453/`.
Reproduce component acceptance with `lab.py check-audit-lifecycle-gate`.

Complete execution of the changed ISO test remains pending; the local workspace
has no ISO built from these changes. The CI install-test jobs already gate
publication for both BIOS and UEFI; UEFI first installs the previous nightly.
The A/B bundle uses an ephemeral test signing key and explicit test trust file,
not the production release key. This verifies updater signature handling when
run, not production-key publication. Corrupted-slot fallback returns to a
working slot containing the candidate build. It does not prove that a previous
release can understand schema-v2 enrollment. Older-reader compatibility and
upgrade/rollback deployment policy remain release gates; preserve schema-v2
state/history instead of rewriting it to appease an older binary.


The first complete CI attempt, run 42577895520 on PR #22, passed compilation,
ISO assembly and smoke tests but both install jobs stopped before fixture
preparation because the helper accepted only the `qemu` virtualization label.
The CI guest uses KVM acceleration, which systemd can report as `kvm`. The
helper now accepts both labels and reports unsupported detected values. Four
native component checks passed, including simulated `qemu`/`kvm` acceptance and
`vmware` refusal, fixture preservation, and changed-state detection. Evidence:
`/var/lib/smolofis-identity-machine-lab/audit-workflow-17e925debf/`.
Full signed A/B and rollback execution remains pending the corrected CI run.


The second CI attempt (42582007436) passed the KVM guard, prepared the fixture,
and promoted the signed update. It then correctly rejected enrollment because
the volatile root supplied a different `/etc/machine-id` after reboot. An early
initramfs step now restores durable device identity before systemd starts;
[enrollment identity notes](admin-enrollment.md) cover migration and safety
constraints. The lifecycle gate now checks both runtime and saved machine-ID
bytes as well as enrollment/audit history. Full BIOS/UEFI acceptance remains
pending the corrected image run.


Run 42585992507 passed both complete BIOS and UEFI install jobs, including
signed A/B promotion and corrupted-slot fallback, with unchanged enrollment,
runtime/saved machine ID and audit files; administration stayed disabled.
Its standalone smoke job refused a test persistence directory copied with the
runner UID by `mkfs.ext4 -d`. The scratch-image builder now normalizes and
verifies the required inode owners offline using debugfs, without host chown or
sudo. The boot ownership guard remains strict. Nine native checks passed,
including reproduction and repair of non-root copied inode ownership:
`/var/lib/smolofis-identity-machine-lab/machine-id-4ae1dc0101/`.
All-green CI remains pending the smoke fixture correction.


Final CI run [42589005839](https://github.com/jcyrus/smolofis/actions/runs/42589005839)
passed on commit `2d1a0df` (2026-11-09). Compilation, lint/advisory/unit checks,
ISO assembly, smoke boot, and both complete BIOS/UEFI installation jobs are green.
Both firmware jobs verify identity/enrollment/audit persistence after signed
A/B promotion and corrupted-slot fallback, and confirm administration stays
disabled. This is acceptance of the candidate image and test-signed update bundle;
the release job was skipped for the draft PR. No production signing/publication,
main merge, or server upgrade occurred. Physical storage power-loss durability,
older-reader policy, and administration enablement retain their separate gates.


## Production dev.78 rollout — 2026-11-09

The authorized upgrade of `192.168.1.17` completed using the installed signed
updater. Its manifest was verified against the server's trusted signers and
pinned to dev.78; the downloaded ISO checksum matched
`2a91dfa81d4683123c3487194a6a323294b3449df6ef27214b3a674449555313`.
Updater backup `20261109T094858Z` completed before writing inactive system-a;
all backup tar archives parsed and the PostgreSQL dump passed `pg_restore --list`.
This checks archive readability, not a production restore exercise. The staging
bundle and apply log remain under `/var/lib/smolofis/dev78-stage.xHwDNv/`.

After reboot, dev.78 reached ready and its verifier promoted system-a with
`Result=success`, `ExecMainStatus=0`, and an empty next_slot. The previous
system-b/dev.73 kernel, initrd and squashfs remain available. Boot ID changed
from `5854865d-67fd-4ecd-bc7b-2a006ae2bec8` to
`917cebe9-f650-4e12-b742-eddd54f4a511`. No failed units remained. Docker, panel,
infrastructure and SSH were active; core containers were healthy, Gitea reported
28.2.0, the persisted Coolify users table contained one user, the dashboard
returned 200, and the operator had an active user manager and runtime directory.

The new early-boot step initialized durable identity on this previously
unenrolled server. Its runtime machine-ID file matches the protected saved copy
(mode 0644 runtime, 0600 saved under root-owned mode-0700 identity storage).
This one-time adoption changes the former volatile ID. A second hardware
reboot was not performed; repeated-boot/update identity persistence has separate
full-ISO BIOS/UEFI CI acceptance. No enrollment binding was weakened or rewritten.

Administration remains disabled: console, sign-in and console asset routes
returned 404; worker/private-console units were not found; no console/auth
service flags were configured. No identity/share management was enabled.
Physical power-loss, Windows client, real tailnet/mobile and remaining
management/lifecycle acceptance gates remain open.

## Root-only operation listing preview

`sudo smolofis-admin console-operation-list` lists operation IDs, kinds and
outcomes from the selected local audit file. The standard log is selected by
the wrapper; a root `SMOLOFIS_LOCAL_AUDIT_FILE` override can select one retained
archive. The direct binary command is `--auth-operation-list`.

The command requires root and uses the same trusted-file, read-only descriptor,
nonblocking shared-lock and 16 MiB snapshot checks as known-ID lookup. It emits
JSON with `version`, `source: selected_audit_file`, `operations`, and
`incomplete_tail`. IDs are normalized and sorted lexically, not chronologically.
At most 4096 distinct operations are accepted; excess history returns an error,
not a silently truncated result. Only complete newline-terminated records are
considered. An incomplete final fragment is flagged; a begin without completion
retains `unknown`. Orphan or conflicting outcomes, bad attribution, missing or
malformed operation IDs, and unsupported records refuse the listing.

Listing does not consult current enrollment receipts, discover archives, merge
files, reconcile superseded crashes, paginate, or mutate history. Empty history
is valid. An error is not an empty successful listing. No passwords, session
material, arbitrary request fields or directory lookups are included.
Host tests and lint checks cover this preview. Eight native Linux wrapper
checks passed on 2026-11-10: standard-log discovery; root archive override;
retained real crash/unknown outcomes; prompt busy-lock refusal and recovery;
unsafe ownership/mode/symlink/hardlink refusal; corrupt/unsupported/orphan
records; oversized/missing/empty history; non-root and extra-argument denial,
with standard/read-only/crash file bytes preserved. The test ran in a private
mount namespace with the exact shipped wrapper and candidate static x86_64
binary. Temporary mounts disappeared with namespace exit; no production
configuration or installed VM binaries were replaced.

Private report:
`/var/lib/smolofis-identity-machine-lab/operation-listing-0ec98ed3f7/report.json`.
Reproduce after building the Linux artifact into
`work/identity-m0-vm/panel-listing`:

```sh
python3 scripts/dev/identity/vm/lab.py check-operation-listing
```

Release CI/publication remains pending. Production binary is unchanged.

## Operation-listing release — 2026-11-10

PR #23 merged as `bdc0e9f5bf2aa5a7f7c8557b9380187bcbcdda10`.
[Main run 42767153841](https://github.com/jcyrus/smolofis/actions/runs/42767153841)
passed compilation/lint/advisories/unit tests, ISO assembly, smoke, BIOS/UEFI
installation and publication. Nightly dev.80 contains the root listing command,
DNS-preserving connection defaults and retained acceptance helpers/documentation.
The nightly tag matches the merge commit.

The downloaded manifest verifies against the repository release signer. ISO hash
`94ac377cf64fe94fd555088884aef2d3fc899ddbd887b9c5062b8842b711bd14`
agrees across the signed manifest, checksum sidecar and GitHub asset digest.
This metadata verification does not replace downloading/hashing the complete ISO
before staging an upgrade. No production upgrade was performed; production
remains dev.78 with administration disabled.

## Production dev.80 rollout — 2026-11-10

The authorized rollout downloaded the full pinned ISO to
`/var/lib/smolofis/dev80-stage.VIug5A/`, verified its signature against the
server's trusted signer and matched SHA256
`94ac377cf64fe94fd555088884aef2d3fc899ddbd887b9c5062b8842b711bd14`.
The installed updater created backup `20261110T072254Z`; every tar archive
listed successfully and the PostgreSQL dump passed `pg_restore --list`.
This establishes readability, not restore acceptance.

Updater staging succeeded, followed by an authorized reboot. Dev.80 reached
ready and system-b was promoted; system-a/dev.78 is retained. Kernel boot ID
changed to `fba54147-5879-4e24-9aa5-9fa60256568c`. Runtime and saved machine-ID
hashes match their pre-upgrade values. No failed units remain; Docker, panel,
infrastructure and SSH are active. Core containers recovered, Gitea reports
28.2.0, Coolify retains one user and the public dashboard returns 200.

Console/login and actual `/assets/console.css` and `/assets/console.js` routes
return 404. Candidate worker/backend/HTTPS units, auth configuration and
enrollment remain absent; no 8091 or SMB 445 listener is present. The newly
installed listing CLI correctly refuses an unconfigured audit file.

Tailscale remains unjoined (`NeedsLogin`) and Serve is empty. Its state directory
is on persistent ext4, but the disconnected node's live `CorpDNS=false` preference
reset on reboot; it was reapplied without joining. The installed dev.80
connection/autoconnect helper explicitly supplies `--accept-dns=false`.
Do not claim durable connected DNS acceptance from this unjoined-node change;
verify after enrollment. The earlier resolve1 warning remains in stopped-node
health. Production administration was not enabled.

## Preserving superseded receipts — implementation preview

Before a configured-audit enrollment replaces the current state, it validates
and syncs the previous state/parent and appends a synced
`local_receipt_preserved` event for that state's applied receipt. This is root
attribution of observed state, not a reconstructed directory authorization.
Known-ID lookup and operation listing recognize this checkpoint as applied,
including when the original completion is missing or its log was archived.
No begin event alone is promoted, and no arbitrary operation ID is accepted
from a caller for preservation. Existing command serialization covers the state
observation, checkpoint and subsequent mutation.

Enrollment fails before changing access when prior-receipt verification or
checkpoint persistence fails. Lockdown keeps its access-removal priority:
it captures the validated previous receipt, commits the locked state first,
then checkpoints that receipt and writes its own completion. A busy checkpoint
fails without waiting on the audit lock. Journal failures are reported after
lockdown rather than preventing access removal. If the process crashes after
lockdown commits but before checkpoint persistence, the earlier outcome can
still remain unknown; the new lockdown receipt remains in state. This exception
is deliberate and must not be described as eliminating all crash windows.

A partial log tail still requires explicit archival; checkpointing never repairs
or truncates it. When auditing is not configured, legacy local commands retain
their latest-receipt behavior without historical guarantees. No worker mutations,
HTTP history API, general queued/running operation executor, automatic archive
search, or restored-device reconciliation is introduced by this preview.

Older binaries do not understand the new typed event and can report audit
history unavailable. Keep the new reader for checkpoint-bearing histories;
validate upgrade/rollback handling before enabling production administration.
Production remains dev.80 and has not received this implementation.

Validation: 73 host tests passed (one manual browser fixture ignored) and Clippy,
formatting/Python/whitespace checks passed. Eight native Linux checks passed,
including a real SIGXFSZ completion-write crash, explicit partial-tail archival,
superseded receipt lookup, repeated preservation, busy/damaged journal lockdown,
real LDAPS enrollment refusal and recovery ordering, foreign-state/non-root
refusal, and cleanup of the synthetic directory user/group. Report:
`/var/lib/smolofis-identity-machine-lab/receipt-journal-a7dbfc18d1/report.json`.
Reproduce with the reviewed Linux candidate in
`work/identity-m0-vm/panel-journal`:

```sh
python3 scripts/dev/identity/vm/lab.py check-receipt-preservation
```

Checkpoint fsync fault injection, individual checkpoint crash boundaries,
appliance upgrade/rollback compatibility and release CI remain pending.
No production configuration or binary changed during these tests.

### Native checkpoint fault and older-reader acceptance — 2026-11-10

Sixteen checks passed using the existing candidate binary in the owned x86_64
full-kernel VM. The tracer observed this lockdown sequence:

1. `fsync` of the validated previous state.
2. `fsync` of its parent directory.
3. `fsync` of the new locked-state temporary file.
4. `fsync` of the parent after state replacement.
5. `fdatasync` of the previous-receipt checkpoint.
6. `fdatasync` of lockdown completion.

Real `EIO` was injected into each sync syscall. Failures before the new state
file sync preserved the old state or allowed lockdown with explicit partial
failure, depending on the phase. A state-directory sync failure left a visible
replacement with an uncertain command result. Checkpoint/completion sync failures
returned an error while preserving the already-committed locked state. Complete
records can remain visible after an injected flush failure; visibility is not a
claim of durability under physical power loss.

Child processes were killed before all six sync calls and after checkpoint and
completion sync. Fresh processes could inspect the resulting state. After a
successful checkpoint sync, the older applied receipt remained discoverable
from journal history even after its current-state receipt was superseded.
The known lockdown interval between state commit and checkpoint remains explicit;
these tests neither erase that interval nor invent historical completion.

The pre-checkpoint candidate's SHA256 was matched to its retained native-listing
report before testing a private executable copy. It read current schema-v2
receipts, but unsupported checkpoint history returned `unknown` with audit status
`unavailable`; state and log bytes were unchanged. No old mutation command ran.
This is reader compatibility acceptance, not complete appliance rollback
acceptance, restored-session acceptance, or permission to run older writers on
checkpoint-bearing history.

Private report:
`/var/lib/smolofis-identity-machine-lab/receipt-faults-13f87514ee/report.json`.
The initial baseline harness omitted `fdatasync`; the final run covers both
sync syscalls. No product fault-injection hook was added. Reproduce after
receipt-preservation setup:

```sh
python3 scripts/dev/identity/vm/lab.py check-receipt-faults
```

Full A/B upgrade/rollback, enrollment-specific sync/crash coverage and hardware
power-loss durability remain separate acceptance. Production was unchanged.

### Enrollment-specific sync/crash acceptance — 2026-11-10

Nineteen checks passed in the full-kernel x86_64 VM using real
certificate-verified Samba LDAPS and a disposable dedicated administrator.
The baseline established seven sync calls in this order:

1. Previous state `fsync`.
2. Previous state parent `fsync`.
3. Prior-receipt checkpoint `fdatasync`.
4. New enrollment authorization `fdatasync`.
5. Replacement state temporary-file `fsync`.
6. Replacement state parent-directory `fsync`.
7. Completion event `fdatasync`.

Injected `EIO` at each call preserved the original locked state through phase 5.
After state replacement, phase 6 returned explicit uncertainty; phase 7 reported
that state was applied but completion auditing failed. Fresh history listing
kept authorization-only/uncertain operations unknown rather than inventing
completion. No success message was accepted on error. LDAP connection-close
warnings may appear on stdout independently of command success; the harness
checks results and state rather than assuming every warning is a failure.

The child was killed before each of the seven sync calls and after phases 3,
4, 6 and 7. Original locked state remained until replacement; visible replacement
state had its own receipt. When later access was applied, the prior checkpoint
retained the old lockdown outcome. These are process-level tests on a running
kernel, not device-cache or host power-loss proofs.

Enrollment passwords flowed through a pipe to the child and were absent from
enrollment argv and reports. The synthetic directory user/group and private
reader-password fixture were removed. Source configuration in the private
evidence directory references that removed fixture and is not a running panel.
No production state or binary changed.

Evidence:
`/var/lib/smolofis-identity-machine-lab/enrollment-faults-c028bd2b2b/report.json`.
The shared fault tracer is a lab-only Python module, not product code. Its
refactor passed the complete 16-check lockdown/older-reader matrix again at
`receipt-faults-1d0423355e`. Reproduce after staging the journal candidate:

```sh
python3 scripts/dev/identity/vm/lab.py check-enrollment-faults
python3 scripts/dev/identity/vm/lab.py check-receipt-faults
```

Full appliance A/B upgrade/rollback with the checkpoint event and release CI
remain pending. Production remains dev.80 with administration disabled.

### Installed-image checkpoint fixture — 2026-11-10

The existing ISO install test now carries a mode-0400 synthetic checkpoint-only
history alongside its real archived lockdown history. The checkpoint's ID was
superseded in current state, so candidate lookup must use the selected history.
Its bytes are included in the persisted SHA256 set. Previous-nightly preparation
checks legacy history and hashes only; candidate verification additionally
requires checkpoint lookup and listing. Both post-promotion and corrupted-slot
fallback verification enforce the new reader. The fixture is synthetic and
locked; it does not configure a directory or expose administration. Actual
checkpoint emission and fault handling have their separate native acceptance.

Five component checks passed in a private native-VM mount namespace, using the
verified pre-checkpoint binary for preparation and the new binary for reading.
They include QEMU/KVM/unsupported-runtime checks, overwrite refusal, and changed
state/checkpoint byte detection. Evidence:
`/var/lib/smolofis-identity-machine-lab/audit-workflow-3e920e666c/report.json`.
Fixture mounts/configuration disappeared with namespace exit. This component
run is not complete A/B boot acceptance; full ISO CI remains pending.

### Full ISO checkpoint lifecycle acceptance — 2026-11-10

[PR #24 run 42800140945](https://github.com/jcyrus/smolofis/actions/runs/42800140945)
passed on latest head `9369bd314eddc867acb24e8b3c648944c3c1e973`.
Compilation/lint/advisories/unit/Python checks, ISO assembly and QEMU smoke passed.
Both BIOS and UEFI logs explicitly confirm checkpoint-only history is readable
and hashed state/history survives signed A/B promotion and corrupted-slot fallback.
Administration remains disabled throughout the test. The PR is open, draft and
mergeable; release publication was skipped. No production upgrade occurred.
This closes the installed-image checkpoint persistence/fallback gate for the
candidate, not hardware power-loss durability or arbitrary older-writer safety.

## Production dev.82 rollout — 2026-11-11

The installed updater verified the trusted signed manifest and the complete ISO
SHA256 `d7e4435b0eb2e2cbc845f2267982180f32147ce8a86c61dc8d300266509288c1`.
Staging and logs are retained under `/var/lib/smolofis/dev82-stage.UNJR3R/`.
Backup `20261111T032720Z` completed before slot replacement. Its tar archives
and PostgreSQL dump listed successfully.

After reboot, dev.82 reached ready and the verifier promoted system-a with
`Result=success` and `ExecMainStatus=0`. System-b/dev.80 is retained. Kernel boot
ID changed to `f2183338-7718-412d-8f62-2339ccfac5f7`. Runtime and saved machine-ID
hashes match their pre-upgrade values. Core units are active, no failed systemd
units remain, and core containers recovered healthy. Gitea reports 28.2.0,
Coolify retains one user and the public dashboard returns 200.

Console, login and console assets return 404. Candidate administration units,
auth configuration and enrollment are absent. Ports 8091 and 445 have no
listeners. The operation-list CLI refuses an unconfigured audit file as expected.
Tailscale remains `NeedsLogin` with empty Serve configuration. Post-check result
is retained in `postcheck-result.txt` in the staging directory.

## General operation journal preview

A root command now executes a machine-identity check through persisted queued,
running and terminal transitions. The check compares the validated runtime
machine ID against the saved appliance identity. The journal is bound to that
appliance identity and stored in `/var/lib/smolofis/operations/journal.json`.

```sh
sudo smolofis-admin console-job-init
sudo smolofis-admin console-job-run
sudo smolofis-admin console-job-list
sudo smolofis-admin console-job-status OPERATION_ID
sudo smolofis-admin console-job-recover
```

Initialization preserves an existing valid store. Each operation has a random
64-character hex ID, an allowlisted kind, root UID attribution, ordered timestamped
transitions and a typed outcome. The current executor supports
`machine_identity_check`, with `identity_verified`, `identity_unavailable` or
`identity_mismatch` results. A failed check records `failed` and returns an error.
Status/listing allow discovery after a disconnected client or interrupted process.

The store serializes an entire execution or recovery under a protected,
nonblocking file lock. Every transition atomically replaces the bounded JSON
snapshot after syncing its temporary file, then syncs the parent directory.
The directory requires root ownership and mode 0700. Data and lock files require
mode 0600, one hard link and regular-file identity checks. Trusted ancestors,
appliance binding, schema, unique IDs, allowed transitions and outcomes are
validated before execution. Store limits are 256 operations and 1 MiB.

A command refuses new work while any record is queued or running. Explicit root
recovery changes those records to terminal `unknown` with outcome `interrupted`.
Completed records remain intact. Recovery is idempotent and performs no executor
action. A fresh invocation after recovery receives a new operation ID. Capacity
refusal preserves history. Retention/export, web actor attribution, worker/API
integration, additional operation kinds and image lifecycle acceptance remain
pending.

Snapshot sync/rename errors return an uncertain-commit error with the operation
ID. Inspect listing/status before retrying. A complete replacement can be visible
after a directory-sync failure. Current-state inspection and process-crash tests
establish observed state, while physical power-loss durability requires separate
storage acceptance.

Validation on 2026-11-11 passed 76 host tests (one manual browser fixture ignored),
Clippy, formatting, ShellCheck and Python compilation. The final Linux candidate
passed 19 native checks through the packaged command interface in a private
mount namespace. They include listing/status across processes, preserved history
on initialization, root/ID/concurrency guards, actual identity mismatch, foreign
machine binding, corrupt/unsafe files, six injected sync failures and nine
process-death boundaries. Recovery preserved completed records and marked
unfinished work unknown before a new operation was admitted. Namespace fixtures
were cleaned on exit. Report:
`/var/lib/smolofis-identity-machine-lab/operation-journal-56088655dc/report.json`.

Reproduce with the Linux artifact staged as
`work/identity-m0-vm/panel-operation-journal`:

```sh
python3 scripts/dev/identity/vm/lab.py check-operation-journal
```

This preview awaits release CI and installed-image lifecycle acceptance.
Production runs dev.82 with administration disabled.

### General journal installed-image fixture — 2026-11-11

The ISO lifecycle fixture now prepares protected synthetic completed and running
operations, including an appliance identity binding. Separate hashes cover the
journal snapshot and immutable fixture files. The previous nightly can prepare
these bytes. After candidate promotion, CI verifies both records, performs
explicit interrupted-work recovery, checks the completed result is intact, and
runs a fresh machine-identity check. Updated journal bytes/results are then
verified after failed-slot fallback. Native writer/crash tests remain the evidence
for genuine queue transitions.

Seven component checks passed in a private VM mount namespace, including
previous-reader preparation/new-reader verification, QEMU/KVM guards, overwrite
refusal, changed state/checkpoint/journal detection, and recovery/execution.
Evidence:
`/var/lib/smolofis-identity-machine-lab/audit-workflow-c13c76d582/report.json`.
Full installed-image A/B execution awaits CI.

### Journal release acceptance — 2026-11-11

PR #25 passed full CI on `ffa6df535f6ab06ce6e360f4f551e8a8272d0852`
and merged as `2976c61e17deb43535d642de2c6ae690206a3326`.
[Main run 42961750775](https://github.com/jcyrus/smolofis/actions/runs/42961750775)
passed compile, ISO assembly, smoke, BIOS/UEFI installation and publication.
Both firmware tests exercised journal persistence, interrupted-work recovery,
fresh identity execution and failed-slot fallback.

Nightly dev.84 was published and its tag matches the merge commit. The downloaded
manifest verifies against the trusted release signer. Signed ISO hash
`87602f9ce170a9e591674712734b24fbfee9b8e3a772a80574f14a8826ac60d0`
matches the checksum sidecar and GitHub asset digest. Full downloaded-image
verification belongs to server staging. Production remains dev.82 with
administration disabled. Worker/API and directory actor integration remain next.

## Authenticated journal API preview

`SMOLOFIS_MANAGEMENT_ENABLED=true` enables three administration routes after
authentication configuration, device enrollment and an HTTPS ts.net origin are
validated at startup:

| Route | Behavior |
| --- | --- |
| `GET /api/operations` | Read the bounded operation journal through the worker |
| `GET /api/operations/{operation_id}` | Read one operation; malformed IDs return 400 and absent IDs return 404 |
| `POST /api/operations/identity-check` | Accept JSON containing only the session CSRF token; return the recorded operation with 201 |

The authentication guard supplies an internal verified administrator identity.
POST also verifies Origin, CSRF and current enrollment generation before worker
submission. Actor GUIDs and peer UIDs are derived server-side. Public request
bodies cannot select them. The CSRF value is available to the signed-in user
in the existing account form. Responses have no-store caching policy.

The worker authenticates the Unix peer and accepts typed operations against its
root-configured journal. Journal records retain execution UID 0 and additionally
store the administrator's immutable GUID and actual panel UID. Recovery remains
a local root command. Worker outage, busy execution, unsafe sockets and invalid
responses yield 503 with guidance to inspect submitted operations before retrying.
The prototype executes the identity diagnostic synchronously. Service/account
mutation kinds, management UI and production wiring remain further integration.

Validation on 2026-11-11 passed 80 host tests (one manual browser fixture ignored),
Clippy, formatting and Python checks. Nine native checks passed with real
certificate-verified Samba LDAPS, an unprivileged UID-65534 backend and a UID-0
worker. They cover default-off routes, required configuration, non-admin denial,
CSRF/Origin/actor forgery, typed durable results with the real administrator GUID
and kernel requester UID, malformed/unknown IDs, socket caller/path restrictions,
directory revocation, worker outage and older-reader/writer refusal with unchanged
attributed history. Synthetic directory accounts, processes and namespace fixtures
were cleaned up.

The backend transport test used private loopback HTTP with an HTTPS origin and
manually supplied Secure cookies, modeling the existing HTTPS proxy boundary.
Actual public TLS for these new API routes subsequently passed in the exact-artifact acceptance recorded below. The
actual hardened systemd units and new management/journal drop-ins passed 11
checks, including attributed execution, successful synced transitions, worker
restart persistence, outage preservation and denial of direct backend access to
the root journal. Report: `management-sandbox-ec9f20a22a/report.json`. Report:
`/var/lib/smolofis-identity-machine-lab/management-api-a4bace1931/report.json`.
Reproduce with the candidate in `work/identity-m0-vm/panel-management-api`:

```sh
python3 scripts/dev/identity/vm/lab.py check-management-api
python3 scripts/dev/identity/vm/lab.py check-management-sandbox
```

The journal uses optional actor fields to retain legacy root-record readability.
The verified pre-attribution binary refuses new records on read/run/recover,
preserving the original bytes. A socket timeout may occur while its blocking
commit finishes; an execution permit remains held until that work ends. No
automatic retry is performed by the API. Production runs dev.82 with
administration disabled.

### Console identity checks

With the management preview enabled, the Service health page includes Server
checks and Recent operations. Run identity check uses the authenticated session's
CSRF token. Expand a history row for the result, operation reference and
administrator ID. Root-run records appear as Local administrator.

The browser retains previous history if the worker becomes unavailable and
disables submissions until history loads successfully. Queued/running records
block new submissions. After a submission loses its response, the result remains
uncertain: refresh history to inspect it before reloading the page for another
check. Refresh never resubmits the POST. Interrupted records require the local
recovery workflow above; a history refresh does not recover or replay work.

Browser validation uses the ignored synthetic directory fixture and mocked
operation responses; it does not replace native worker or TLS acceptance. To
reproduce, install Playwright in a temporary directory and run the fixture:

```sh
cd src-dashboard
SMOLOFIS_AUTH_BROWSER_ORIGIN_FILE=/private/tmp/smolofis-operation-browser-origin \
  cargo test --locked auth::tests::browser_fixture -- --ignored --nocapture
```

In another terminal, from the repository root:

```sh
SMOLOFIS_PLAYWRIGHT_MODULE=/path/to/node_modules/playwright \
  node scripts/dev/check-operation-ui.cjs
```

Use `SMOLOFIS_BROWSER_EXECUTABLE` to select an installed Chromium executable.
Stop the fixture with Ctrl-C after testing. Screenshots and the scoped report
are written to `.impeccable/review/operations/`.

### Exact-artifact HTTPS acceptance — 2026-11-11

PR #26 head `38a6a3875f5e09a4d3bb9ff8f40e76bb2f4a291c` passed CI run
`42982628507`, including the ISO, smoke and BIOS/UEFI install/upgrade checks.
The CI artifact SHA256
`fa81062af29562f8dc58b69979d6a2c3e4272d428621911e9e2701db0b287a8e`
was staged in the isolated lab with its hardened console, root journal worker
and foreground Tailscale Serve. Fifteen actual HTTPS peer checks passed with
normal CA and hostname verification, real LDAPS, real worker execution and
protected journal attribution. The user confirmed the phone/browser certificate,
sign-in, identity check, saved result after refresh and sign-out steps all worked.

Evidence: `/var/lib/smolofis-identity-machine-lab/private-https-409cb462/`,
including `https-peer-report.json`, `ci-artifact.json`, the archived journal and
final acceptance record. This clears PR #26's HTTPS publication gate; production
administration remains disabled and requires its own integration acceptance.

## Production dev.88 rollout — 2026-11-11

The installed updater verified dev.88's trusted manifest signature and complete
ISO SHA256 `9651bfbdbff47e29c4adbe3e53da94bfbf568bd045cb7788aaf21a7741c25447`.
Backup `20261111T101057Z` completed before replacing system-b. All backup tar
archives parsed, and the PostgreSQL dump table of contents listed successfully.
The update was staged without reboot so backup inspection completed first.

After reboot, dev.88 reached ready and the verifier promoted system-b with
Result=success and ExecMainStatus=0. System-a/dev.82 remains available as the
previous slot. Boot ID changed to `58389532-c3b6-4071-8449-10c00d779e7f`. Runtime and saved
machine-ID hashes, repository paths and the Coolify user count (1) match
the baseline. All prior containers returned running with healthy status wherever
a health check exists; no failed systemd units remain. Gitea reports 28.2.0.

Public dashboard and liveness return 200. Console, sign-in, operations API and
console assets return 404; candidate administration units/auth/enrollment remain
absent. No listeners exist on 8091 or 445. Tailscale remains NeedsLogin and Serve
configuration is empty. Production administration is still disabled.

Protected staging, baseline, backup reference, apply log and post-check results
are retained under `/var/lib/smolofis/dev88-stage.hLzRfm/`.
