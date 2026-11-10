# M0 identity feasibility evidence

Status on 2026-11-09: M0 feasibility acceptance is complete for the tested v1
topology, including native Windows and macOS permissions. Production identity,
share-management, security and lifecycle integration remain M1/M3–M6 gates.
The temporary Windows lab relay on `192.168.1.17` was removed after acceptance;
no production Samba/directory/share service was installed or enabled. The server
remains on dev.78, ready, with administration disabled and no SMB listener.

The accepted implementation direction remains a native Samba AD directory in
a full-kernel appliance OS (validated in the lab VM), a separate domain-member file service, and Gitea
using certificate-verified LDAP. Directory users/groups are the identity source;
share authorization and repository authorization remain separate. An explicit
Gitea account association preserves existing ownership/history. A share is not
automatically a Git repository. Keep directory state, shared data, and Gitea
state separately managed and consistently backed up.

The lab boots an x86_64 Debian 13 guest through QEMU on the ARM workstation.
The outer runner is unprivileged; the guest owns its kernel, ext4 filesystem,
and systemd Samba service. Samba is 4.22.11; Gitea is appliance-pinned 28.2.0.
The member/Gitea containers run inside the guest. Only loopback SSH is exposed;
native macOS tests use a temporary loopback tunnel. The guest blocks office
and other private-network egress. No production secrets or real users are used.

| Acceptance | Evidence/result |
| --- | --- |
| Native directory | Standard native provision, certificate-verified LDAPS, SYSVOL read, native `security.NTACL`; no container ACL workaround |
| Gitea identity | Directory login works without automatic admin rights |
| Guest share authorization | Engineering writes; Readers read but cannot write; Outsiders cannot access |
| Native macOS | Writer read/write/delete and read-only access passed; correlated server trace confirms Outsider tree-connect denied |
| Native Windows | Windows 11 23H2: Alice read/write/delete, Reader read/write-denial, Outsider authenticated but tree-connect denied; user results correlated with server audit |
| Password reset/disable | Old password rejected after rotation; new password accepted; disabled fresh logins denied by LDAPS, SMB, and Gitea; original synthetic credentials restored |
| Identity/ownership | Stable GUID/SID and retained numeric ownership, mode, ACLs, xattrs, and contents across lifecycle checks |
| Gitea migration | Same account ID/username, private repository ownership/history, SSH access, and API-token mapping; former local password rejected; synthetic key/token removed after proof |
| Restart/reboot | New service invocation and changed kernel boot ID; automatic service startup and working authentication/shares |
| Cold VM restore | Independent flattened backup and separate writable guest disk; identities, mappings, share data/metadata and permissions retained |
| Directory application restore | Supported offline backup restored into replacement DC `dc-r0`; actual restored DB, recovered server TLS files, trusted login, SYSVOL, member/Gitea, and data/mappings passed |
| DNS outage/recovery | Injected authoritative DNS outage detected; rule removal, server-backoff reset and cache flushing restore lookup |
| NTP outage/recovery | Disabled synchronization service observable; re-enabled and synchronized afterward |
| Kerberos skew/recovery | Normal login, explicit error with process-local +10-minute client skew, normal-time recovery; no system clock changed |

Retain these deployment requirements:

- Set the old-password grace period explicitly to zero for immediate-reset
  behavior. The native default accepted the former password during initial
  rotation testing. Fresh-login denial does not revoke existing sessions,
  Gitea tokens, or SSH credentials; the M2/M6 offboarding path must fence and
  revoke those separately. The approved office-wide Gitea browser sign-out
  remains the v1 policy, with preliminary container evidence already recorded.
- Route the AD DNS zone to the directory and preserve working external DNS
  forwarding. Cached resolver failure state needs explicit recovery handling.
- Keep local recovery access independent of directory availability. Directory
  membership must not imply console administrator or Gitea administrator rights.
- Keep restored runtime paths short enough for Unix sockets. Application
  restore requires `tdb-tools`, creation of runtime cache directories, private
  database protection, SYSVOL traversal, recovered TLS configuration, and
  validated SYSVOL ACL repair.
- This restore tested default SYSVOL/GPO ACLs using `sysvolreset`/`sysvolcheck`.
  Custom GPO ACL preservation needs separate acceptance. The lab Compose files
  and broad synthetic credential mounts are test fixtures, not production
  privilege boundaries or an operator restore implementation.

macOS presents a generic timeout for the denied Outsider mount over this lab
tunnel. Config-level tracing captured successful authentication followed by
`NT_STATUS_ACCESS_DENIED` on `projects`, proving authorization denial. The
native helper deliberately fails closed on a timeout without that correlation;
do not turn generic connection failures into automatic permission-test passes.

The successful replacement directory is active in the disposable restored
guest; the original guest is stopped. Backups, private diagnostics, and failed
attempts are retained in ignored lab storage. The Debian base image used an
official HTTPS SHA512 checksum; detached-signature verification was not done.

See the [persistent checklist](admin-ui-plan.md),
[detailed protocol and recovery findings](identity-prototype.md), and
[VM harness](../scripts/dev/identity/vm/lab.py). Native Windows acceptance is
recorded below. This is a feasibility decision, not permission to enable
unfinished production administration or expose the lab permanently.


## Windows acceptance connection prepared — 2026-11-09

A temporary two-hour SSH relay is available for a Windows 11 24H2-or-newer
client. The workstation forwards its loopback 11445 to the isolated restored
VM's loopback 1445. A reverse SSH forward makes it reachable only at production
server loopback 127.0.0.1:11445. Windows creates its own SSH local forward through
the existing server SSH service and uses SMB's alternative TCP-port mapping.
The server has no port-445 listener or Samba installation; no LAN SMB port,
firewall change, tailnet change, or administration enablement was introduced.

`python3 scripts/dev/identity/vm/windows-access.py start|status|stop` manages
only these task-owned SSH masters. Auto-expiry and manual cleanup are supported.
Private session metadata and synthetic credentials stay in ignored lab storage.
Do not commit account passwords. An authenticated native macOS read through the
complete production-loopback relay returned the retained lab evidence file;
the validation mount and its extra local forward were cleaned up afterward.
This proves relay connectivity, not native Windows permission acceptance.

Windows tests remain pending: M0\alice read/write/delete, M0\reader read but
write denied, and M0\outsider share connection denied. Disconnect the test
mapping between users and preserve exact error codes if credentials conflict.
The lab share is `projects`; production web-login credentials are not SMB test
credentials. No production share is being tested or exposed.


Windows version probing reported 23H2/build 22631.6199 and no TcpPort cmdlet
parameter. Alternative-port instructions therefore do not apply. A temporary
runtime-only socket proxy now listens on production IPv4 192.168.1.17:445 and
forwards to the existing loopback-only lab relay. A dedicated nftables input
chain permits only Windows client 192.168.1.58 (plus local validation) and drops
other sources for that port. No Samba installation, production share, permanent
firewall rule, or enabled-on-boot service was added. The proxy runs as nobody
with hardening and no capabilities. Runtime files are under
`/run/smolofis-m0-windows-relay/` and `/run/systemd/system/`; cleanup timer
`smolofis-m0-windows-relay-cleanup.timer` stops proxy/socket and deletes its own
nftables table at 18:59:30 +07 on 2026-11-09, before the SSH relay expires.
Manual stop: `sudo /bin/sh /run/smolofis-m0-windows-relay/stop.sh` on the server,
or the workstation helper's `stop` action while its SSH master is available.

Authenticated native SMB read through the standard-port proxy succeeded.
A non-allowlisted workstation connection was blocked, confirmed by nftables
counters; packets from the Windows client reached the allow rule. These are
connectivity checks only; Windows read/write/read-only/denied role results are
still pending. The user can now map `\\192.168.1.17\projects` using normal SMB
syntax and the synthetic M0 credentials. Activation status does not add the
missing alternative-port feature to Windows 23H2. Administration remains off.


Windows writer acceptance passed on 2026-11-09: the user reported an OK Z:
mapping to the lab projects share, read the retained cold-restore evidence,
wrote/read the Windows probe and deleted it without an error. The client is
Windows 11 23H2/build 22631.6199 and uses the source-IP-restricted standard-port
relay. Prior failed attempts included the local ADMIN-PC account; the explicit
lab mapping was used for the successful test. Reader read/write-denial and
outsider authorization denial remain pending, so the overall Windows gate
is still open. Lab-only passwords were simplified by user request and verified
with fresh SMB authentication; credential values are not recorded here.


Windows read-only acceptance passed on 2026-11-09: the user mapped Z: as
M0\reader, read the retained cold-restore evidence, and received Access denied
when creating `windows-reader-check.txt`. This is the expected read-only result.
Outsider authentication-plus-share-denial correlation remains pending.


## Final native Windows acceptance and cleanup — 2026-11-09

| Account | Native Windows result | Correlation |
| --- | --- | --- |
| M0\alice | OK mapping, retained-file read, probe write/read/delete | NTLMv2 status OK from ADMIN-PC; probe absent after deletion |
| M0\reader | OK mapping and retained-file read; probe creation Access denied | NTLMv2 status OK from ADMIN-PC; denied write created no file |
| M0\outsider | Mapping failed with Windows System Error 5, Access denied | NTLMv2 status OK, then explicit projects tree-connect NT_STATUS_ACCESS_DENIED |

Credential-free auth metadata, exact Outsider denial excerpt, client build and
user-confirmed outcomes are retained privately in the restored lab guest at
`/var/lib/smolofis-identity-machine-lab/windows-acceptance-bd98aa8abf/`.
The tests used Windows 11 23H2/build 22631.6199 and the native Windows SMB client;
no SMB1/guest access or weaker authentication was enabled for acceptance.
The client already reported a nondefault insecure-guest setting; it was not
used to grant access and was not changed by these tests. All successful named
account sessions used NTLMv2. Earlier failures included local ADMIN-PC\Admin;
explicit lab credentials and a copy-safe UNC path were used for acceptance.

The member's pre-diagnostic configuration was restored and DC verbosity reset.
Both SSH masters stopped, standard-port proxy/socket stopped, its runtime unit
files removed, cleanup timer stopped, and only its dedicated nftables table
was deleted. Post-cleanup production checks show no 445 or 11445 listener,
no failed units, phase ready and /console HTTP 404. The lab is isolated again.

The v1 topology decision is native Samba AD with real kernel/filesystem ACL
support, a separate ADS-member file service, and Gitea using verified LDAP plus
reviewed explicit account associations. It does not require putting the
production appliance inside another VM: the full-kernel VM was the validation
environment. Shared files and repositories remain distinct resources with
separate authorization, persistence and backup scopes. Tested default SYSVOL
restore does not establish custom GPO ACL restoration. Production secret mounts,
service packaging, memory/storage sizing, restricted mutations and consistent
operator restore remain implementation/lifecycle work; the lab's broad synthetic
mounts must not be copied into production. M0 completion does not enable the
admin UI or the production directory/share services.
