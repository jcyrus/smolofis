# Production console setup prerequisites

Production `192.168.1.17` runs dev.88 with administration disabled. It has no
production office directory, console directory configuration or enrollment yet.
The existing Linux operator and Gitea/Coolify application administrators do not
automatically become office directory accounts or console administrators.

The selected server-hosted directory uses a private local namespace:

| Setting | Planned value |
|---|---|
| DNS domain | `office.smolofis.internal` |
| AD realm | `OFFICE.SMOLOFIS.INTERNAL` |
| Short domain | `SMOLOFIS` |

ICANN reserved `.internal` for private-use applications in its
[29 July 2024 resolution](https://www.icann.org/en/board-activities-and-meetings/materials/preliminary-report-special-meeting-of-the-icann-board-29-07-2024-en).
This directory namespace is separate from the existing `smolofis.local` LAN
name and the production node's eventual Tailscale HTTPS name. No domain purchase
or workstation domain-join setup is required for this hosted-service scope.

## Read-only setup check

The candidate command is:

```sh
sudo smolofis-admin console-setup-check
```

It emits one JSON report with schema, scope, `prerequisites_passed`, and 14
checks containing stable IDs, pass/fail values and fixed next-step guidance.
It exits nonzero when prerequisites are incomplete. Missing configuration does
not abort the inventory early or cause public access to be enabled. The report
contains no passwords, tokens, cookie values, directory contents or raw errors.
Only root can invoke it. It accepts no alternate filesystem paths or shell
commands.

| Check | What it inspects |
|---|---|
| Installed appliance | Trusted installed-release marker |
| Saved machine identity | Trusted initialized runtime identity matches its saved copy |
| Local directory service | Native Samba AD service is active |
| Panel account | Dedicated non-root local account and matching primary group |
| Directory configuration | Bounded, root-owned configuration with strict schema |
| HTTPS origin | Canonical HTTPS hostname under `ts.net` |
| Directory material | Trusted valid CA, private reader secret and panel read/traverse access |
| Reader and group | Real certificate-verified LDAPS bind, matching naming context and existing administrator group |
| Enrollment | Device/config binding is valid, unlocked and panel-readable |
| Local audit | Root-owned 0600 single-link local audit log |
| Panel audit | Panel-owned 0600 single-link log with trusted traversable parent directories |
| Operation journal | Protected, machine-bound journal is readable and not busy |
| Service files | Reviewed-unit/drop-in file presence/ownership and exact panel UID setting |
| Private transport | Intended node online, matching HTTPS certificate domain and empty existing Serve/Funnel configuration |

The command does not create files, initialize or recover journals, provision
accounts, modify DNS, join a tailnet, install units, enable listeners, or start
services. The LDAPS probe performs a reader bind and searches only; it makes no
directory changes. Filesystem ownership/mode inspection and service-file presence
do not replace actual service-hardening or extended-ACL acceptance. A successful
inventory is a pre-start prerequisite report, not production enablement approval.
The existing `console-access-check` is still required against a running enrolled
loopback backend before starting the private HTTPS listener.

## Remaining provisioning work

1. Deliver native Samba AD and its persistent state/configuration through the
   appliance image and validate DNS, address stability, LDAPS trust and renewal.
2. Create the directory reader and dedicated console administrator group; enroll
   an actual office administrator using a protected password prompt.
3. Provision protected audit/journal paths and the reviewed backend, worker and
   HTTPS units with this appliance's actual panel UID.
4. Validate backup/restore, reboot, signed upgrade/fallback, local lockdown and
   connected production DNS before enabling console access.

Native client access, identity lifecycle and private HTTPS have lab acceptance
recorded elsewhere. Those checks do not imply that production provisioning is
already installed or enabled. Never point production at the disposable lab
realm or reuse its users, certificate authority or test passwords.

## Acceptance — 2026-11-11

Host validation passed 82 tests with one manual fixture ignored, plus Clippy,
formatting, ShellCheck and Python syntax checks. Nine native VM acceptance groups
passed with real LDAPS, reader/context/group failures, unsafe/inaccessible
secrets and logs, incorrect worker UID, locked enrollment, machine mismatch,
non-root denial and unchanged protected input inventories. Native evidence is
`production-preflight-819d0c829a`.

The same validated Linux candidate was staged separately on production, SHA256
`99ebacde9d5e7c5211c307ee55ad4235ec641b11648d6cf5cf49a1eb298696a9`.
Three prerequisites passed: installed appliance, saved machine identity and panel
account. The other 11 remain blocked, starting with the missing office directory.
Configuration hashes were unchanged after the check, core services remained
ready, candidate units remained absent and administration endpoints stayed 404.
Evidence: `/var/lib/smolofis/console-preflight-preview.LUlYNZ/`. The installed
panel was not replaced and remains dev.88.

Installed-image acceptance is wired to check the packaged command after signed
BIOS/UEFI upgrade and fallback; its full CI result remains pending.
