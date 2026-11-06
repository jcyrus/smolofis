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
access automatically; its systemd integration remains pending. No production
unit or appliance image has been enabled by this work.

Each JSONL record contains schema version, wall-clock milliseconds since the
Unix epoch, a typed event, and an optional raw directory GUID encoded as hex.
Events currently cover successful credential verification, denied directory
sign-in, explicit sign-out, and session revocation after directory denial.
No password, username, cookie, session token, request body, URL, or arbitrary
error/message string is accepted by the event writer. Failed sign-ins have no
verified actor and use a null GUID. Local enrollment/lockdown events, malformed
request denials, throttling events, and operation auditing remain unfinished.

Writes use an in-process mutex plus an OS file lock across writers, append a
complete JSON line, and sync data before reporting success. A 16 MiB limit
bounds storage. On append/sync/capacity failure, a verified sign-in does not
issue a new session; a sign-out or directory revocation still removes the local
session, with an error response when its event cannot be persisted. Successful
credential verification is recorded before session allocation, so capacity or
concurrent state changes can prevent the final session even after that event.

The log is durable local history, not tamper-proof evidence: the panel process
can write it, root can alter it, crashes can leave a partial final line, and no
hash chain, remote collector, automatic repair, rotation, or retention scheduler
exists yet. Wall-clock correctness follows the appliance clock. The descriptor
stays open for the process lifetime. To archive at capacity, stop every writer,
copy the log to protected archival storage, provision a new empty file, and
restart. Do not truncate or replace a live writer's file. Do not rerun the
provisioning command over an existing log; it would erase that history.

This is a preliminary authentication audit step. Durable actor-attributed
privileged-operation records, local recovery auditing, disk-full fault injection,
concurrent process stress, Linux ownership checks, and deployment acceptance
remain release gates.
