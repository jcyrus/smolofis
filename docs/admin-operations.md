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
