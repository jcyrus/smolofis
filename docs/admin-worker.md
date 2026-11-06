# Local administration boundary

The panel remains an unprivileged web process. An experimental second mode of
its binary establishes the local Unix-socket boundary for future administration:

```text
smolofis-panel --admin-worker /run/smolofis-admin/control.sock PANEL_NUMERIC_UID
```

This worker currently supports only protocol status. It cannot change accounts,
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
