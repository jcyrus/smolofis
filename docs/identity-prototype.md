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
**empty, isolated Debian 13 lab machine only**. They provision a separate
`M0.SMOLOFIS.TEST` realm, install Samba as a real systemd service, and would
verify LDAPS, built-in SYSVOL read access, and stable GUID/SID values across
service activations. They do not replace the separate domain-member share
server tested by the container suite. The provisioner requires root and
`SMOLOFIS_IDENTITY_MACHINE_LAB=1`, refuses unowned or partial domains, and
expects both files under `/opt/smolofis-identity-lab/` in the lab machine.

Attempted on an isolated OrbStack Debian 13 amd64 machine named
`smolofis-identity-m0`, with file integration disabled, network isolation,
3 GiB memory/2 CPU/12 GiB disk limits. It reports `lxc` under
`/run/systemd/container`. Standard provisioning failed while setting SYSVOL
NT ACLs with `NT_STATUS_ACCESS_DENIED`; no native authentication or lifecycle
check is counted as passing. The scripts deliberately do not apply the
container-only `user.NTACL` workaround. A full-kernel VM is still required.
The failed machine is retained for diagnostics; do not rerun provisioning on
its partial directory. Use a fresh full VM for the next attempt.

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
