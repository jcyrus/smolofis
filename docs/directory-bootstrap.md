# Offline native office directory bootstrap

`sudo smolofis-admin directory-prepare` prepares an isolated native Samba AD
data tree on an installed appliance. `sudo smolofis-admin directory-status`
reports the offline bootstrap state. The image ships the bootstrap and Samba
provisioning packages with the distribution Samba units masked. Preparation
leaves the host resolver and Samba service state untouched. Use a disposable
installed test appliance until the lifecycle and recovery gates pass.

The selected namespace is `office.smolofis.internal`, realm
`OFFICE.SMOLOFIS.INTERNAL`, short domain `SMOLOFIS`. The directory controller's
internal host name is `smolofis-dc`; its name must differ from the short domain.
This does not rename the appliance's existing LAN/mDNS identity.

## Fixed inputs and storage

The public maintenance commands are `directory-prepare` and `directory-status`.
Bootstrap requires local root, a
trusted installed-appliance marker, a mounted ext4/XFS/Btrfs data filesystem and
an explicit root-owned plan at `/etc/smolofis/directory-plan.json`:

```json
{
  "version": 1,
  "dns_domain": "office.smolofis.internal",
  "short_domain": "SMOLOFIS",
  "hostname": "smolofis-dc",
  "address": "192.168.1.17",
  "forwarder": null
}
```

The address above is the current production address, not a generic default to
copy onto another appliance. Address assignment/stability and DNS forwarding
must be validated before runtime integration. `null` does not configure a
public DNS forwarder. Plans reject ambiguous/unknown fields, invalid names,
mDNS suffixes, public/loopback DC addresses and invalid/self-referential
forwarders. No default plan is copied into production.

Data stays under `/var/lib/smolofis/directory`, mode 0700. The controller's
configuration and databases are created beneath `domain/`; target paths are
stable rather than renamed after provision. The break-glass Administrator
password is generated in-process and retained in the root-only
`bootstrap-admin.secret`. It is passed directly to Samba's Python API, never
through command arguments, environment variables or public output. Provisioning
output is redirected to a private log. The candidate requires the tested Samba
4.22 API, with native SYSVOL ACLs and RFC2307 support explicitly selected.

The Samba operation is based on its supported
[domain-provision and SYSVOL verification tools](https://www.samba.org/samba/docs/current/man-html/samba-tool.8.html)
and the installed native provisioning API. This candidate intentionally has a
version guard; an API upgrade needs renewed acceptance.

## State and interruption behavior

A persistent owner-only lock serializes preparation/status. A synced state file
records an operation ID, plan and machine digests, and `preparing`, `failed` or
`prepared`. Generated files/directories are synced before the prepared state
is committed. Domain GUID/SID metadata and required prepared artifacts are
checked before reusing an existing result.

Identical successful preparation returns the existing result without rewriting
its secret or directory identity. A changed plan, foreign machine identity,
unsafe control file, unknown existing directory, missing artifact or interrupted
attempt is refused. Failure retains private partial evidence. After SIGKILL,
status reports `interrupted` without rewriting the underlying `preparing`
record. No operation is automatically replayed, no data is automatically deleted,
and no recovery or archive command is implemented yet.

The summary's `service_started_by_command` and `dns_changed_by_command` describe
the command's effects. They are not live service-health claims. A prepared data
tree is not an operating production directory or permission to expose it.

## Validation and next integration

Run the standard-library plan checks on the host:

```sh
python3 -m unittest discover -s scripts/directory -p 'test_*.py'
```

The native harness uses the existing guarded full-kernel VM, real ext4 storage,
an isolated mount namespace, a temporary appliance marker and a separate new
AD tree. Native filesystem acceptance currently covers ext4; the other accepted
filesystem types need their own acceptance before production use. Existing global Samba configuration/data are bound read-only during
the test. It checks real provisioning, SYSVOL native ACLs, protected credentials,
unchanged host configuration, idempotence, foreign/changed inputs, missing or
unsafe artifacts, failure retention and SIGKILL/no-replay behavior:

```sh
python3 scripts/dev/identity/vm/lab.py check-office-bootstrap
```

The BIOS and UEFI installed-image jobs prepare a disposable directory and check
its identity and SYSVOL ACLs after signed upgrade and failed-slot fallback. The
jobs also exercise explicit service start and stop using a temporary address and
private CA. A passing run is required to accept these changes.

## Explicit service startup

The image ships `smolofis-directory.service` disabled. On an installed test
appliance, prepare the directory, assign its planned address, and create
`/etc/smolofis/directory-tls` as root with mode 0700. Put the private trust CA in
`ca.pem`, the server certificate in `cert.pem`, and its matching unencrypted key
in `key.pem`. Each file is root-owned with mode 0600. Issue the certificate for
TLS server use with the DC's full internal name in its DNS subject alternative
name. Keep the CA signing key outside the appliance.

Run `sudo systemctl start smolofis-directory.service`. Startup checks the
prepared state, assigned address, and certificate before launching Samba. The
start job succeeds after a verified TLS handshake on port 636 and the directory
returns its LDAP SRV and planned IPv4 records over UDP and TCP DNS. The
service does not change the host resolver. For trial clients, direct queries for
the office zone to the DC and trust the CA separately. To stop the directory,
run `sudo systemctl stop smolofis-directory.service`.

For a trial renewal, stage the replacement files with the same ownership and
modes, stop the service, replace the three files together, and start the service
again. Retain the previous still-valid set until the restart passes. Automated
renewal, reader and administrator accounts, backup/restore, local recovery, and
production console enablement remain separate acceptance work.

## Acceptance — 2026-11-12

Five host plan-validation tests passed. The final candidate passed 12 native
checks with real Samba 4.22 provisioning and native SYSVOL ACL verification,
protected credentials, unchanged global configuration, idempotence, invalid
plans/machine state/artifacts/secret modes, retained failures and SIGKILL
interruption with no replay. Evidence:
`/var/lib/smolofis-identity-machine-lab/office-bootstrap-39ae454a07/report.json`.
The harness removed its temporary mount namespace/appliance marker; the existing
M0 directory remains running. Production was not touched.
