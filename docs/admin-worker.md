# Local administration boundary

The panel remains an unprivileged web process. An experimental second mode of
its binary establishes the local Unix-socket boundary for future administration:

```text
smolofis-panel --admin-worker /run/smolofis-admin/control.sock PANEL_NUMERIC_UID
```

The three-argument worker mode supports protocol status. It cannot change accounts,
shares, files, service state, or directory data. It is not connected to HTTP
routes and is not installed or enabled in appliance images. Console access
remains loopback-only; this work does not satisfy the M1 authentication gate.

The candidate unit is `config/systemd/smolofis-admin-worker.service`. For a
future isolated Linux test, install it alongside the binary and create a
root-owned `/etc/smolofis/admin-worker.env` containing `PANEL_UID=` followed by
the actual numeric UID of `smolofis-panel`. Do not assume identical UIDs across
appliances. The unit uses the panel group for socket access and a root-owned
runtime directory with mode 0750. No capability or mutable system directory is
granted to the worker at this stage. The unit has no automatic enable target.

## Protocol version 1

Each connection accepts one newline-terminated JSON request, at most 1024 bytes
including the newline. The entire request/response has a two-second deadline;
only 16 handlers may run concurrently. Additional connections are closed when
capacity is exhausted. OS backlog limits also apply.

```json
{"version":1,"operation":"protocol_status"}
```

```json
{"version":1,"status":"ok","mutations_enabled":false}
```

An unsupported version returns `unsupported_version`. Unknown operations,
unknown fields, duplicate fields, and malformed JSON return `invalid_request`.
Unauthorized peers, incomplete requests, and oversized requests are closed.
There are no arbitrary command, path, systemd-unit, or environment parameters.
Trailing bytes after the first newline are never executed as another request.

The worker checks the OS-provided peer UID in addition to filesystem socket
permissions. It refuses relative socket paths, symlink runtime directories,
non-root-owned runtime directories, writable group/world runtime directories,
and any existing socket path. It never unlinks an existing entry. systemd
cleans the runtime directory when the unit stops. Startup and rejection logs
contain no request bodies; operation auditing is still to be implemented.

## Remaining integration

Add directory-backed administrator verification and secure web sessions before
connecting management routes. Design each mutation as a typed operation with
server-side authorization, CSRF checks, a durable operation record, explicit
failure handling, and a bounded executor. Validate the worker unit and Unix
peer checks on the target Linux VM; local macOS tests do not establish Linux
systemd or production security acceptance. Add the unit to image construction
only after these release gates pass.

## Journal API integration preview

An optional fourth root CLI argument enables the existing protected journal:

```text
smolofis-panel --admin-worker /run/smolofis-admin/control.sock PANEL_UID /var/lib/smolofis/operations
```

The journal must be initialized locally first. This mode requires root and a
non-root panel UID, validates the journal before binding, and accepts only that
kernel peer UID. The panel client requires a trusted root-owned socket parent,
a root-owned Unix socket without world access, and a root peer after connecting.
The configured panel process is trusted to assert the session's administrator
GUID. The HTTP guard revalidates that administrator through the directory.
Worker-only LDAP verification is outside this AF_UNIX-only service boundary.

Additional version-1 request operations are `identity_check`, `job_list` and
`job_status`. They require `actor_guid` as 32 hexadecimal characters. Only
`job_status` accepts `operation_id`, as 64 hexadecimal characters. Unknown fields,
including commands, paths and caller-supplied UIDs, are rejected. The actual peer
UID is recorded by the worker. Identity execution records root UID, directory
GUID and requester UID in the journal. Legacy root records omit the new optional
fields and remain readable. Older journal binaries reject attributed records
before mutation because their schemas reject unsupported fields.

Requests remain bounded to 1024 bytes. The client bounds replies to 2 MiB and
checks response version/status. One journal execution/read may run at a time,
with a permit retained by the blocking task through completion even if its
socket times out. Other journal requests report unavailable promptly. A timeout
can leave a durable result; inspect listing/status before retrying.

Candidate drop-ins are `smolofis-admin-worker.service.d/journal.conf` for the
persistent write exception and `smolofis-console-preview.service.d/management.conf`
for the opt-in API flag. They are excluded from image installation. Actual
sandbox execution with these drop-ins and production integration remain pending.

Native integration passed nine checks in `management-api-a4bace1931`, including
real LDAPS sessions, root/unprivileged peer boundaries, correct administrator
GUID and kernel requester UID, forged request rejection, revocation, outage and
older-writer refusal. Eighty host tests passed with one manual fixture ignored.
Actual execution of the candidate systemd drop-ins and new-route public HTTPS
acceptance remain pending. The AF_UNIX worker trusts the configured panel UID
for directory-session assertions and enforces the action allowlist independently.
