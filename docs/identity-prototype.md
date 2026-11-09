# M0 identity prototype protocol

Run only on an isolated Debian 13 VM with disposable storage and an isolated
network. Do not provision a domain on an existing office network for this test.
Use synthetic users and fresh test passwords. Record exact package versions,
commands, results, and resource usage in the implementation tracker.

## Local container integration pass

On a machine with Docker Compose, run:

```bash
bash scripts/identity-lab.sh test
bash scripts/identity-lab.sh migration
bash scripts/identity-lab.sh sessions
bash scripts/identity-lab.sh live-smb
bash scripts/identity-lab.sh backup
bash scripts/identity-lab.sh status
bash scripts/identity-lab.sh stop
```

The runner builds a Debian 13 image, provisions a synthetic directory, joins a
separate file server, and configures the repository's pinned Gitea release for
LDAPS. It verifies baseline access, rotates Alice's password, denies fresh
authentication while Alice is disabled, re-enables her, restarts all services,
and checks that her directory GUID and SID survive. A failed disable test still
attempts to re-enable Alice so the lab can be rerun.

`migration` creates a fresh pair of synthetic accounts for each run: a local
Gitea user with a private repository, browser session, SSH key, and scoped API
token, and a directory user with a different login name. It changes the Gitea
authentication source through the supported admin API and verifies continuity.
It then measures access after directory disablement, explicitly prohibits
Gitea login, deletes the user's tokens and SSH keys through the API, and checks
that those credentials stay revoked after re-enabling. The metadata-only
report is stored in the lab's `evidence` volume; credentials stay in the
disposable lab volumes.

`sessions` implements the approved v1 office-wide browser sign-out policy in
the lab. It first enables 31-day remembered logins to create a real legacy
cookie and proves that cookie can recreate a session. It then prohibits the
target user, removes their API tokens/SSH keys, stops Gitea, and expires all
remembered-login tokens in a version/schema-checked offline SQLite transaction.
Old file-session storage is cleared while stopped; normal sessions use Gitea's
memory provider. Gitea restarts with zero-day remembered logins.

After re-enabling the target account, the test replays dormant session cookies,
the valid replacement remember cookie issued by the positive control, and a
remember-cookie-only request. All must remain denied. It also verifies another
user's browser is signed out while that user's API token remains usable, the
target's deleted credentials still fail, and fresh password login works.

The offline cleanup is **lab-only**, restricted to the pinned Gitea 28.2.0
SQLite layout and a known prohibited test user. The runner checks that Gitea
has stopped before giving the network-isolated maintenance container access
to its data volume. Refusal tests prove mismatched accounts and unsupported
database engines leave row counts unchanged. It does not support PostgreSQL,
replicas, arbitrary configuration paths, or a running Gitea writer. It must not
be exposed as a production endpoint without the M1 privilege and operation
controls. The temporary cookie/token fixture is private and removed on
successful verification; the final `session-report.json` contains metadata.

For v1, any Gitea restart ends browser sessions. Ordinary Git SSH and personal
API credentials are preserved except for the account explicitly offboarded.
Restoration must invalidate browser sessions and remembered logins before
ingress opens, so an old backup cannot revive a revoked remembered login.

`live-smb` keeps Alice and a read-only user's SMB clients authenticated at the
same time. It disables Alice, checks the old connection, and closes Alice's
server worker through a pinned process descriptor. It verifies Alice can no
longer download the synthetic file while the existing reader still lists the
share. The second scenario removes Alice from Engineering and repeats those
checks after clearing Samba's identity cache. Account and group state are
restored, then the baseline permission suite runs again.

The server control is fixed to the synthetic Alice UID and Samba 4.22.11's
standalone session JSON. It refuses the master process or a worker hosting a
different user, validates the kernel parent/process name and Samba process
unique ID, and signals via pidfd to avoid targeting a reused numeric PID.
The metadata-only `live-smb-report.json` records whether each old session was
usable before disconnection. This is Linux-client session evidence; native
client behavior, durable handles, and interruption of in-progress file writes
still require the platform acceptance tests.

`backup` repeats the migration checks and snapshots a test file's hash,
numeric ownership, mode, ACLs, and extended attributes. It stops **all** lab
writers before archiving eight volumes with ACL/xattr preservation, restores
them into fresh volumes under a uniquely named Compose project, and reruns
identity, Gitea, and SMB checks. The original containers/network are removed
to release the isolated subnet; original volumes are retained. Restored
containers/network are removed after testing, including on failure. Backup
and restored volumes remain for inspection, named with the
`smolofis-identity-restore-` prefix printed by the runner. Do not run lab commands
concurrently with a backup.

This is a **same-DC cold snapshot** test on the same Samba release. It is not
proof of Samba online domain backup, domain-controller replacement,
cross-version restoration, or the appliance's production backup integration.

The network is internal, with **no published host ports or host filesystem
mounts**. It uses `10.83.0.0/24`; Docker will reject creation if this overlaps an
existing Docker network. Credentials are random, synthetic, and kept in lab
volumes. Gitea trusts the disposable lab CA; TLS verification stays enabled.
The lab is not a production deployment: it shares a synthetic bind credential
with Gitea and uses a short-lived test CA. Do not reuse its credentials or
certificate in an appliance image.

`stop` retains volumes and identities for another run. To explicitly destroy
this lab's data when finished:

```bash
docker compose -f scripts/dev/identity/compose.yml down --volumes
```

These checks use Linux `smbclient` and container process restarts. They do not
prove Windows/macOS client behavior, a VM reboot, x86 operation, production
backup integration, old-directory-password invalidation, or native-client
durable-handle behavior. Production session-revocation integration also remains to
be implemented. The full M0 gate stays
open until that evidence is collected.

## Deployment to validate

Evaluate Samba AD as the identity authority and a separate domain-member SMB
service for shares. Keep directory state separate from file data. Connect the
existing Gitea release using LDAP with certificate validation. The directory
realm and DNS configuration are explicit setup inputs; the appliance's current
`.local` mDNS address is not a substitute for AD DNS.

## Separate native-service probe

`scripts/dev/identity/provision-machine.sh` and `verify-machine.py` are for an
**empty, isolated Debian 13 full-kernel lab VM only**. They provision a separate
`M0.SMOLOFIS.TEST` realm, install Samba as a real systemd service, and would
verify LDAPS, built-in SYSVOL read access, and stable GUID/SID values across
service activations. They do not replace the separate domain-member share
server tested by the container suite. The provisioner requires root and
`SMOLOFIS_IDENTITY_MACHINE_LAB=1`, refuses unowned or partial domains, and
expects both files under `/opt/smolofis-identity-lab/` in the lab machine.
It also requires the owned VM marker, completed firewall bootstrap, and a
full-VM virtualization check; an appliance installation is explicitly refused.

Attempted on an isolated OrbStack Debian 13 amd64 machine named
`smolofis-identity-m0`, with file integration disabled, network isolation,
3 GiB memory/2 CPU/12 GiB disk limits. It reports `lxc` under
`/run/systemd/container`. Standard provisioning failed while setting SYSVOL
NT ACLs with `NT_STATUS_ACCESS_DENIED`; no native authentication or lifecycle
check is counted as passing. The scripts deliberately do not apply the
container-only `user.NTACL` workaround. A full-kernel VM is still required.
The failed machine is retained for diagnostics; do not rerun provisioning on
its partial directory. Use a fresh full VM for the next attempt.

### Full-kernel VM evidence (2026-11-07)

`scripts/dev/identity/vm/lab.py` runs QEMU inside an unprivileged Docker
runner. The Debian guest has its own x86_64 kernel and ext4 root filesystem;
Samba runs under the guest's systemd. This is distinct from the failed LXC
probe above. Only loopback SSH port 12222 is published. Guest firewall rules
block office/private-network egress while allowing public package downloads.
The host SSH key is pinned and no SSH agent is forwarded. Production is untouched.

The official Debian 13 genericcloud AMD64 image was checked against its
official HTTPS SHA512SUMS file; its detached signature has not been verified.
Image checksum evidence, VM disks, pinned SSH keys, seed, and console output
are retained under ignored `work/identity-m0-vm/` with private permissions.
Do not commit or publish that directory.

Passed on the guest: Samba 4.22.11 native domain provision,
certificate-verified LDAPS for the synthetic Alice account, built-in SYSVOL
read, and a 320-byte native `security.NTACL` attribute. The guest reports
`qemu`, x86_64, ext4, and no container runtime marker. The LDAP SRV query
resolves the DC, time synchronization is reported active, and systemd reports
no failed units. These are positive checks, not DNS/time failure acceptance.
One Samba systemd restart also passed LDAPS, SYSVOL, and unchanged GUID/SID
comparison after the SMB listener became ready. The initial immediate SYSVOL
probe failed; the verifier now allows a bounded SMB readiness window.
DNS update logs contain duplicate-record errors (exit 110); these remain
an investigation item even though the positive SRV lookup works.
The machine verifier stores GUID/SID and service invocation evidence in
`/var/lib/smolofis-identity-machine-lab/baseline.json` inside the guest.

After preparing the verified base image and building the runner with
`docker build -t smolofis-m0-qemu:local scripts/dev/identity/vm`, use:

```sh
python3 scripts/dev/identity/vm/lab.py up
# Wait for cloud-init and /var/lib/m0-bootstrap-ready in the guest.
python3 scripts/dev/identity/vm/lab.py provision
python3 scripts/dev/identity/vm/lab.py status
python3 scripts/dev/identity/vm/lab.py stop
```

The adjacent Compose configuration now runs a separate domain-member file
service and appliance-pinned Gitea 28.2.0 inside the guest. Gitea's trusted
LDAPS login passes for Alice without administrator privileges. The member
reports a healthy domain join. Guest `smbclient` checks pass: Alice lists and
writes, Reader lists and downloads Alice's file but cannot write, and Outsider
cannot list or write. Synthetic probe files are removed after the check.

`lab.py services` stages the reviewed lab files, creates synthetic users and
groups, installs Docker, and builds/starts the services. The member image
build uses the isolated guest's network; Docker's default build bridge is
blocked by the guest firewall. Runtime services use the dedicated 10.85.0.0/24
bridge. Service ports bind only the guest's loopback. `lab.py check-services`
configures Gitea's CA/auth source and executes the guest permission checks.
For checks without configuration changes, run `configure-gitea.py --verify-only`
and `verify-services.py` under `/opt/smolofis-identity-lab/vm/` as guest root.

Full kernel reboot passed: `compare-reboot` verified a changed kernel boot ID
and stable directory GUID/SID in a new service invocation. Gitea directory
login and all guest share permission checks passed after automatic startup,
without rerunning configuration or manually restarting the services.

Native Windows permission checks and final
topology acceptance remain open. Guest CLI checks do not replace native clients.

Password-reset policy finding: the native DC defaults to a 60-minute
`old password allowed period`. The first immediate-revocation test failed
because Alice's old password still bound successfully after rotation; the
test restored her original credentials. Samba documents this grace period in
the [4.22 configuration reference](https://www.samba.org/samba/docs/4.22/man-html/smb.conf.5.html#OLDPASSWORDALLOWEDPERIOD).
The lab now evaluates an explicit zero-minute period so an admin reset can
invalidate old credentials immediately. New lab provisioning sets this
option; `strict-password-policy.py` updates an existing owned lab directory.
This is a candidate deployment policy requiring inclusion in the final M0
architecture decision, not a production change. Existing-session revocation
is a separate requirement even when fresh logins reject the old password.
With the zero-minute lab policy, fresh-login lifecycle tests passed across
LDAPS, member SMB, and Gitea: old credentials denied, rotated credentials
accepted, disabled account denied, and restored original credentials accepted.
The test uses `finally` to re-enable/reset only synthetic Alice; no production
identity is involved. The native directory comparison passed after the policy
restart with unchanged GUID/SID values.

Cold whole-VM restore also passed. `verify-checkpoint.py snapshot` wrote a
retained SMB file as Alice and recorded its hash, numeric ownership, mode,
POSIX ACL, xattrs, and Gitea account ID. After guest clean poweroff and QEMU
exit, `lab.py restore` flattened the stopped disk into an independent
read-only `checkpoint.qcow2`, recorded its SHA256, and booted a new writable
overlay backed by that checkpoint. The original guest remains stopped.
The restored guest uses loopback SSH port 12223 with the pinned cloned host
key (`lab.py restored-ssh '<command>'`). It passed actual GUID/SID comparison,
a changed kernel boot ID, Gitea directory login, all guest SMB group checks,
and exact checkpoint metadata/content comparison. No service configuration
was reapplied to make the restored services start. The verifier needed a
bounded LDAP startup timeout retry on the emulated guest.

This is a consistent whole-machine backup/restore proof, not a substitute for
the future operator-facing application backup/restore workflow. The checkpoint
contains synthetic credentials and must remain private in ignored lab storage.
`restore` refuses a running original guest or an existing checkpoint; it never
overwrites evidence. Do not run `provision` against the retained failed LXC lab.

In the restored guest, `verify-migration.py` passed migration of a synthetic
local Gitea account through supported APIs. It preserved the account ID and
username, private repository ID/ownership and commit SHAs, working SSH access,
and existing API token mapping. The former local password was rejected.
The source ID was resolved from verified Alice's account, not assumed.
The synthetic SSH key and token were removed after the checks. SSH host
verification used the public host key read from the owned Gitea container.
A secret-free migration report remains inside the guest lab state directory.

`verify-macos.py` exercised the restored guest using the workstation's
native SMB filesystem driver through loopback port 11445 forwarded to the
guest's loopback share port. Engineering reads/writes/deletes, Reader reads
but cannot write. Outsider cannot mount but reports a generic timeout; this
case required explicit share-denial evidence. Each connection forces a new
session. The helper suppresses credential output, unmounts the temporary
shares, closes its tunnel, and never recursively removes mount directories.
No SMB mounts remained after acceptance. Windows acceptance remains open;
macOS and Linux CLI results do not stand in for it. The initial helper treated
any outsider mount failure as denial; a stricter check exposed the generic
timeout, so that earlier inference is not counted as acceptance. Temporary
server debug logging confirmed Outsider authenticates with NTLMv2, but did
not initially establish an explicit share denial; normal logging was restored.
Subsequent config-level debug tracing captured the native Mac attempt at
2026-11-07 09:48:40 UTC: Outsider authenticated successfully, followed by
`create_connection_session_info` rejecting that same user from `projects`
and SMB2 tree-connect returning `NT_STATUS_ACCESS_DENIED`. This establishes
server authorization denial rather than inferring it from the timeout.
Normal config-level and runtime logging were restored afterward. The native
helper still fails closed on a generic timeout; it needs correlated server
tracing for this case. The misleading tunneled-client error remains a
diagnostic limitation, not a permissions bypass.

DNS registration warnings were subsequently resolved in the restored guest.
The default systemd resolver routed AD-zone lookups upstream rather than to
the native DC, so the updater attempted to add records already present.
An explicit resolved routing domain sends `m0.smolofis.test` to 172.31.83.10;
Samba forwards external queries to the guest's 10.0.2.3 upstream. Normal
system-resolver SRV lookup and `samba_dnsupdate --verbose` then passed with
exit zero. New provisioning includes this routing; `configure-dns.py` applies
it to existing owned lab guests. The DNS routing and successful automatic
registration also persisted across a subsequent kernel reboot. DNS/time
failure scenarios remain open.

`verify-faults.py` subsequently passed bounded outage/recovery acceptance in
the restored VM. A temporary nftables table blocked DNS requests to the AD
server; normal resolver SRV lookup failed as expected. Removing the table,
resetting resolver server features/backoff, and flushing caches restored lookup
within bounded retries. The first immediate recovery check failed because the
resolver retained its failure state; this established the required recovery
steps rather than assuming rule removal alone was sufficient. Fault rules are
removed in `finally` and existing tables are never overwritten.

The same test disabled NTP, observed `NTP=no`, re-enabled it in `finally`, and
verified synchronized status returned. Cached `NTPSynchronized=yes` by itself
is not evidence of a running synchronization service. No guest or host clock
was changed. Kerberos clock-skew behavior is not covered by this service-outage
test. A secret-free `fault-report.json` remains inside the guest lab state.

`verify-clock-skew.py` subsequently passed the separate Kerberos skew case:
normal-time Alice login succeeded, a process-local +10-minute clock produced
an explicit clock-skew error, and normal-time login succeeded again. It uses
`krb5-user` and `libfaketime` (install only in the disposable lab), a private
temporary credential cache/config, and `kdc_timesync=0` so client-side time
correction cannot mask the injected skew. No system clock, global Kerberos
configuration, or real user credential cache changes. See
[libfaketime](https://github.com/wolfcw/libfaketime) and the
[MIT Kerberos configuration reference](https://web.mit.edu/Kerberos/www/krb5-current/doc/admin/conf_files/krb5_conf.html).
The secret-free result is `clock-skew-report.json` inside guest lab state.

Application-level directory restore subsequently passed using
`verify-domain-restore.py`. It stopped lab clients and the original DC, created
a supported offline backup, restored into a fresh tree as replacement DC
`dc-r0`, and started Samba against that restored configuration/database.
Trusted LDAP, SYSVOL, actual restored GUID/SID values, existing Gitea login,
member share permissions, retained file contents/numeric ownership/mode/ACLs/
xattrs, and Gitea account mapping all passed. Server TLS keys/certificates were
recovered from the backup; client CA trust was retained. The machine verifier
reads the active config's private/SYSVOL paths so an untouched old database
cannot produce a false restore pass. Failed attempts and original databases
are retained; failed validation restores the original config and services.

Recovery requirements found by this test:

- Install `tdb-tools` for the supported offline backup command.
- Create the cache directory emitted by restored configuration before validation.
- Use short runtime paths (here `/var/lib/m0-ad/<id>`): the first long path
  exceeded Winbind's Unix socket path limit and prevented the DC from staying up.
- Keep backup/private trees root-only while allowing traversal to SYSVOL.
- Explicitly retain the tested zero-minute password grace policy and point
  server TLS configuration at backup-recovered material.
- Run supported `samba-tool ntacl sysvolreset` and `sysvolcheck` before
  acceptance: the restored SYSVOL root initially lacked native `security.NTACL`.
  This proves default SYSVOL/GPO permissions only; custom GPO ACL preservation
  requires its own migration/restore acceptance.

The successful attempt resumed retained ID `99392ee495` after its backup and
fresh-tree restore already passed, applying the SYSVOL repair before repeating
all functional checks. Fresh runs include those repair/check steps directly.
`domain-restore-report.json` records the archive hash, replacement DC, active
private directory, and passed checks. The successful replacement remains the
active DC in the disposable restored guest; the original QEMU guest is stopped.

The retained checkpoint file's contents, numeric ownership/mode, ACL and xattrs
also remained unchanged across another complete password rotation and account
disable/re-enable cycle in the restored guest. Gitea's mapped account ID
remained unchanged; Alice's original synthetic password was restored afterward.

## Acceptance matrix

| Scenario | Required result |
| --- | --- |
| Create Alice and Engineering | Stable directory IDs; no automatic admin access |
| Engineering writable share | Alice can create/read/update files |
| Read-only group | Read succeeds; writes fail |
| Unrelated user | Access denied |
| Password change | New password works for fresh SMB and Gitea authentication |
| Disable Alice | New logins fail; existing sessions and credentials tested separately |
| Existing Gitea user migration | Same account, repositories, SSH keys, and history retained |
| Remove group access | Access revoked, including behavior of existing SMB sessions |
| Restart directory | Clear availability behavior; no account duplication |
| Reboot VM | Directory identities and file ownership unchanged |
| Backup and restore | Directory IDs, share ACLs, and Gitea mappings remain consistent |
| Windows and macOS clients | Share access without requiring domain enrollment |
| DNS/time failure | Diagnosable failure with documented recovery |

Do not infer offboarding success from a failed password login alone: explicitly
test Gitea API tokens, Git SSH keys, existing browser sessions, and SMB sessions.
Directory disablement does not itself prove those credentials are revoked.

## Gate report

Record pass/fail evidence for each row, tested topology, minimum resource
usage, domain naming rules, backup consistency procedure, and any remaining
operational requirements. The user approved continuing M1/M2 development behind
a flag disabled by default while this report remains an enabling/release gate.
Do not expose privileged administration before the security and acceptance
checks pass. Record blockers and revise the architecture explicitly if needed.

### Container evidence — 2026-11-04

- Host: ARM64 Docker engine 29.8.2; Debian 13 containers.
- Samba: `4.22.11-Debian-4.22.11+dfsg-0+deb13u1`.
- Gitea: `28.2.0`, matching the existing appliance stack.
- Passed: certificate-verified LDAPS binds; Gitea directory login without
  administrator elevation; SMB writer upload, reader download with exact
  content match, denied reader writes, and denied unrelated-user access.
- Passed: new password authenticates across LDAP/Gitea/SMB; disabling Alice
  blocks fresh authentication on all three; re-enabling restores access.
- Passed: directory GUID/SID unchanged after service restarts; authentication
  and share permission checks pass again after restart.
- Idle memory sample: directory 153.6 MiB, file server 13.43 MiB, Gitea
  98.41 MiB. These are single idle samples, not minimum hardware requirements.
- Debian packaging finding: explicitly install `samba-ad-provision` and
  `samba-vfs-modules` alongside the AD/DC packages when recommendations are
  disabled.
- Container-only accommodation: provisioning stores its NT ACL xattr in
  `user.NTACL`. Validate native filesystem ACL behavior on the VM separately.
- Passed extended migration: same Gitea account ID and username, repository
  ID/owner and commit SHAs, existing working SSH key, and scoped API token.
  The former local password no longer authenticates after directory linking.
- Passed explicit Gitea credential revocation: prohibit login, enumerate and
  delete personal API tokens and SSH keys through admin-authorized API calls,
  then re-enable the account and confirm the removed credentials still fail.
- Observed: disabling only the directory user leaves the Gitea token, SSH key,
  and browser session usable. Gitea prohibition blocks access, including its
  HTTP-200 prohibited-account page, but the browser session resumes when the
  account is re-enabled. This observation is the positive control for the
  office-wide revocation operation below; prohibition alone is insufficient.
- Passed same-DC cold restore into fresh volumes: Alice's GUID/SID, directory
  authentication, migrated Gitea account/source mapping and repository history,
  SMB permissions, and file hash/UID/GID/mode/ACLs/xattrs remain consistent.
  Disposable restore project: `smolofis-identity-restore-1793765901-4716`.
- API finding: Gitea 28.2.0's admin edit endpoint treats `source_id: 0` as no
  source change. It must not be used as a migration rollback to local auth.
  Tests create fresh account pairs rather than silently relying on that reset.
- Browser testing follows the pinned release's same-origin login mechanism;
  it does not disable Gitea's cross-origin protections.
- Passed office-wide permanent browser-session revocation: old dormant and
  remembered sessions, including remember-cookie-only replay, fail after the
  target account is re-enabled. Another user's browser is also signed out,
  while their API token continues working. Target tokens/SSH keys stay revoked,
  and fresh directory-backed browser login works.
- Passed maintenance refusal checks: target mismatch and unsupported database
  engine do not change authentication, user, or repository row counts.
- Zero-day remember tokens can remain valid until the current second ends;
  the test waits past that boundary before checking that a new remembered
  cookie cannot recreate a missing session. Existing long-lived tokens require
  explicit offline cleanup; changing the setting alone does not revoke them.
- Passed live SMB revocation for account disabling and Engineering-group
  removal. In both cases the old authenticated connection could still read
  before targeted disconnection; afterward it could not. The already-connected
  unrelated reader and file-server master were preserved. The baseline
  authentication and share-permission suite passed after restoring the test
  user's account and membership.
