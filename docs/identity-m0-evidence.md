# M0 identity feasibility evidence

Status on 2026-11-07: native directory feasibility demonstrated; M0 remains
open for native Windows SMB acceptance and final topology acceptance. Production
`192.168.1.17` was not modified. Identity administration remains disabled by
default and must also pass the M1/M6 release gates before production enablement.

The accepted implementation direction remains a native Samba AD directory in
a full-kernel appliance VM, a separate domain-member file service, and Gitea
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
| Native Windows | Pending; Linux/macOS results do not substitute |
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
[VM harness](../scripts/dev/identity/vm/lab.py). Complete native Windows access
testing on an isolated route before finalizing the deployment decision; do not
expose this lab or enable production identity administration to bypass that gate.
