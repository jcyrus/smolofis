# Private HTTPS console preview

This adds an opt-in transport candidate for the enrolled read-only console.
The isolated full-kernel VM passed a real personal-tailnet HTTPS/mobile trial
on 2026-11-09 (details below). Image construction does not install or enable
the candidate units. Office network-policy isolation, production integration,
and complete actor-attributed operation auditing remain release gates.


## Transport layout

```text
Office browser / phone on the tailnet
    HTTPS https://appliance.tailnet-name.ts.net:443
    Tailscale Serve (foreground process managed by an opt-in unit)
    HTTP 127.0.0.1:8091 (local host only)
    Enrolled SmolOfis console as the unprivileged panel account
    Certificate-verified LDAPS to the appliance directory
```

[Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)
shares a service within the tailnet. This candidate uses foreground Serve;
stopping its systemd unit ends that process's temporary Serve session. It does
not configure Funnel, use persistent `--bg`, reset global Serve configuration,
join a tailnet, change policy, or manage other applications.

The existing dashboard/proxy on port 8090 is unchanged. The private backend runs
as a separate process with separate in-memory sessions; local enrollment locks
apply to every process configured with that state. Git/Coolify launchers still
reference the existing application ports. This step does not publish or add
HTTPS to those applications; their access policies remain separate.

## Prerequisites before an isolated VM trial

- The server is already joined to the intended tailnet, with a stable MagicDNS
  name, and the trial browser/phone has Tailscale connectivity.
- The tailnet owner has enabled the HTTPS certificate prerequisite described
  in [Tailscale Serve setup](https://tailscale.com/docs/features/tailscale-serve).
- The operator has reviewed network grants for the device and tested an allowed
  administrator and a denied ordinary employee. The directory administrator
  group is still required after network access is granted.
- `/etc/smolofis/panel-auth.json`, its CA, and reader-secret paths satisfy the
  root ownership/permissions requirements in the [enrollment guide](admin-enrollment.md).
- The `origin` is exactly `https://<this-node>.<tailnet-name>.ts.net`, with no
  trailing slash or nonstandard port, and local enrollment matches that file.
  HTTP origins and enrollment from another machine are refused in private mode.
- This node has no other Serve, Funnel, foreground, or service configuration.
  The preflight refuses to share or replace existing configuration.

Changing a previously enrolled HTTP origin requires local lockdown, editing
the root-owned config, re-enrolling the administrator, and restarting the new
backend. Do not reuse the old digest without re-enrollment.

## Candidate services and read-only preflight

`config/systemd/smolofis-console-preview.service` runs:

```text
/usr/local/bin/smolofis-panel --private-console
```

This mode fixes the listener to **127.0.0.1:8091**, enables the console, and
requires `/etc/smolofis/panel-auth.json` plus
`/etc/smolofis/panel-enrollment.json`. Environment overrides cannot widen that
listener or turn off its authentication/enrollment requirements. Its origin
must be HTTPS under the node's MagicDNS domain. The service runs as
`smolofis-panel` and retains filesystem/capability hardening.

The candidate now explicitly mounts `/run` read-only. A native probe observed
its separate tmpfs writable in the lab namespace with `ProtectSystem=strict`
alone; the explicit rule passed write-denial probes. The worker retains only
its own runtime-directory exception. Private temporary directories remain
writable by design. These settings do not claim protection against every
later host mount propagation; see [systemd filesystem restrictions](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html).

Audit writing is separately opt-in. After provisioning the trusted directory
and panel-owned 0600 file in the [audit guide](admin-audit.md), review
`config/systemd/smolofis-console-preview.service.d/audit.conf`. It selects
the fixed audit file and grants only that file a `ReadWritePaths` exception.
The unit and drop-in remain excluded from image installation. Missing files
or unsafe permissions fail closed; do not make the whole persistent data tree
writable to make auditing work.

`config/systemd/smolofis-console-https-preview.service` depends on that backend
and `tailscaled`. Its root preflight runs:

```sh
sudo smolofis-admin console-access-check
```

The equivalent explicit-path command is:

```text
smolofis-panel --console-access-check /absolute/config.json /absolute/enrollment.json
```

The check loads trusted, unlocked enrollment; reads local `tailscale status
--json` and `tailscale serve status --json`; matches the online node's DNS name;
rejects every existing Serve/Funnel/service configuration; and probes the fixed
loopback backend. Unauthenticated telemetry must return 401, and sign-in must
provide no-store and Secure/HttpOnly/SameSite=Strict cookies. Backend
connection startup is retried briefly; an insecure responding backend fails
immediately. Command duration and output size are bounded. This preflight only
reads status; it never activates a listener or proves actual policy/client access.

After the check succeeds, the HTTPS candidate runs:

```text
/usr/bin/tailscale serve --https=443 http://127.0.0.1:8091
```

It uses foreground session ownership and no automatic restart. The application
checks the original Host and form Origin; it does not authenticate with
Tailscale identity headers or trust forwarded headers. HSTS is deferred while
HTTP-only application launchers still share the node hostname: its policy would
upgrade their other ports too, breaking those links. This follows the host-wide
port mapping in [RFC 6797 section 8.3](https://www.rfc-editor.org/rfc/rfc6797#section-8.3).
The console still requires HTTPS and Secure cookies; application HTTPS routing
needs a separate design before adding a host-wide policy.

Neither unit has an automatic enable target. For a separately approved VM trial,
a root operator installs the two unit files under `/etc/systemd/system`, runs
`systemd-analyze verify` on both installed units, runs `systemctl daemon-reload`,
starts `smolofis-console-preview.service`, runs the
preflight, and then starts `smolofis-console-https-preview.service`. These steps
are deliberately not part of the image build or first-boot scripts.

Stop the HTTPS candidate with:

```sh
sudo systemctl stop smolofis-console-https-preview.service
```

Then inspect `tailscale serve status --json` to confirm that its foreground
session disappeared. Stop `smolofis-console-preview.service` to close its local
backend too. Do not use a global Serve reset as cleanup. Root can also run
`smolofis-admin console-lock` independently of directory connectivity. After a
restart or dependency failure, recheck configuration before starting HTTPS again.

## Example policy fragment for review

This illustrative fragment needs the real office identities and an assigned
device tag; it has not been applied to a tailnet:

```json
{
  "groups": {
    "group:smolofis-admins": ["office-owner@example.com"]
  },
  "tagOwners": {
    "tag:smolofis-console": ["group:smolofis-admins"]
  },
  "grants": [
    {
      "src": ["group:smolofis-admins"],
      "dst": ["tag:smolofis-console"],
      "ip": ["tcp:443"]
    }
  ],
  "tests": [
    {"src": "office-owner@example.com", "accept": ["tag:smolofis-console:443"]},
    {"src": "ordinary-employee@example.com", "deny": ["tag:smolofis-console:443"]}
  ]
}
```

Review the fragment against the existing policy rather than treating it as a
replacement. Other broad grants/ACLs can still permit access: the negative test
must pass in the final policy. See the official [grants syntax](https://tailscale.com/docs/reference/syntax/grants)
and [policy test syntax](https://tailscale.com/docs/reference/syntax/policy-file).
The preflight cannot establish policy isolation or prevent later out-of-band
changes by another tailnet administrator.

## Verification performed and still required

Run the preliminary acceptance using a Linux binary:

```sh
bash scripts/identity-lab.sh panel-private-https /absolute/path/to/linux/smolofis-panel
```

It starts the private mode as an unprivileged user on an internal Docker network,
checks the kernel loopback binding despite hostile environment overrides, and
runs root preflight checks against controlled read-only Tailscale status fixtures.
A real local TLS proxy uses a disposable CA with certificate verification fully
enabled. Real Samba LDAPS sign-in, HTTPS cookies, protected console/telemetry,
Host spoof refusal, certificate/hostname rejection, and local lockdown are tested.
No host ports, live tailnet, external DNS, public certificates, or real access
policy are involved. No TLS checks are disabled to make the fixture work.

Real-node acceptance still needs certificate provisioning/renewal, correct
Serve Host/Origin forwarding, foreground cleanup after stop/crash/reboot,
allowed-admin/denied-employee policy tests, LAN bypass checks, phone sign-in,
network outages, and HTTPS/Serve unit sandbox execution on the full target VM.

Actual backend/worker sandbox acceptance passed on 2026-11-09 using the
isolated Debian VM and reviewed static Linux binary. Nine checks covered:

- Fail-closed startup when the audit file lacked a write exception, despite
  otherwise correct Unix ownership/permissions.
- Actual candidate backend operation with the single-file audit drop-in,
  real LDAPS login, Secure cookie attributes, and protected telemetry.
- `ExecStartPost` probes under the unit's hardening: writes outside the
  allowlist returned EROFS, a normally readable synthetic home file was
  hidden, effective capabilities were zero, and no-new-privileges was active.
- The actual main process ran with the panel UID and seccomp active. Hostile
  environment overrides could not widen the listener or bypass enrollment;
  the kernel listener remained only 127.0.0.1:8091.
- Atomic local lockdown became visible to the running service; re-enrollment
  did not revive the prior browser token. The test bound the configuration
  directory, not individual file inodes, so atomic replacements stayed visible.
- The root worker's Unix socket admitted the configured panel UID while
  rejecting root and another UID even when Unix permissions allowed connection.
- Unknown/arbitrary/unsupported/oversized/incomplete worker requests were
  rejected. Mutation support remained false. A worker-context probe could
  create Unix sockets and its runtime file but could not create an IPv4 socket.
- Stopping the units removed the worker runtime/socket and backend listener.

The actual unit files were loaded from temporary `/run/systemd/system` entries.
Lab overrides changed the binary path, disabled automatic restart, bound private
fixture configuration, supplied negative-test environment values, and added
`ExecStartPost` probes; they did not remove hardening. A synthetic ts.net-shaped
origin tested backend behavior using local proxy-style HTTP requests, not a
real HTTPS connection or real tailnet identity. The HTTPS/Serve unit was not
started. Unit entries, fixture configuration, and the temporary service account
were cleaned up. No candidate unit was enabled or installed into an image.

Private evidence is retained at
`/var/lib/smolofis-identity-machine-lab/service-audit-592b57d646/`, including
the systemd version, binary hash, event archives, journals, and declared lab
overrides. Reproduce after preparing the [local audit fixture](admin-enrollment.md):

```sh
python3 scripts/dev/identity/vm/lab.py check-service-sandbox
```

The subsequent real-tailnet trial below covers certificates, mobile access and
Serve lifecycle. Distinct employee network-policy denial remains open.

## Real personal-tailnet acceptance — 2026-11-09

The owner approved only `smolofis-m0-private` in their existing personal tailnet,
confirmed that only their devices use it, and enabled HTTPS certificates after
being informed about certificate-transparency publication. Production was not
joined or enrolled. Tailscale 1.104.5 ran in the restored full-kernel Debian VM
with DNS acceptance, route acceptance and Tailscale SSH disabled, and netfilter
mode off to preserve the existing AD DNS and lab firewall.

The exact dev.78 panel binary (SHA256
`e961c429b5738786c9283e7f1b3fc6ac1ff1dbfea3497cf1310b62c556b2cc0d`)
ran under the candidate backend and foreground-Serve systemd units. Temporary
entries in `/run/systemd/system` supplied lab paths and accounts; neither unit
was enabled or installed into an image. The backend retained its fixed loopback
listener and service hardening.

- Nine real peer checks passed: publicly trusted certificate/hostname,
  unauthenticated telemetry rejection, forged identity-header rejection, Host
  validation, secure cookie attributes, cross-origin and CSRF rejection,
  non-administrator directory-account rejection, administrator sign-in and
  protected access, and logout/session invalidation.
- The user confirmed HTTPS and sign-in on a phone over cellular with Wi-Fi off.
- One controlled certificate renewal passed using `tailscale cert --min-validity`:
  serial and expiry changed, and the existing HTTPS service presented the new
  certificate. Only public certificates were exported; no private key or system
  clock change was required. Natural scheduled renewal was not observed.
- Six lifecycle checks passed: renewed certificate presentation; inaccessible
  direct backend; normal Serve stop; Serve process crash cleanup; backend/HTTPS
  dependency stop and explicit restart; and ordinary VM reboot. After reboot,
  tailnet connectivity and the renewed certificate cache persisted, while the
  temporary console units and HTTPS session stayed off. This does not establish
  future production autostart behavior or hardware power-loss durability.

Evidence is retained privately under
`/var/lib/smolofis-identity-machine-lab/private-https-398db315/`, including
`peer-report.json`, `renewal.json`, `lifecycle-report.json`, typed audit logs,
unit journals and `trial-record.json`. Helpers are
`scripts/dev/identity/vm/private-https-trial.py`,
`verify-private-https-peer.py` and `check-private-https-lifecycle.py` in that
same source directory. Passwords are supplied through protected local input,
not stored in reports or source.

Cleanup completed: foreground Serve configuration empty, temporary directory
accounts/group and backend account removed, runtime configuration/unit entries
removed, lab node logged out, and lab tailscaled disabled and stopped. No console
backend or HTTPS listener remains. The issued public certificate remains in
certificate-transparency history.

Remaining gates: demonstrate allowed administrator and denied employee network
access using distinct tailnet identities; finish production service/deployment
integration and renewal monitoring. Directory non-administrator login denial
passed but does not prove network-policy isolation. Production Tailscale was
previously `NeedsLogin`, with a missing `org.freedesktop.resolve1` DNS integration
warning; DNS handling needs an explicit production decision before joining.
Production was rechecked read-only on 2026-11-10: SSH reachable, dev.78,
`/console` returned 404, no backend listener on 8091, no failed systemd units,
Tailscale `NeedsLogin` and Serve configuration empty. The DNS integration warning
remains. This trial made no production configuration changes.

### Reactivated access-policy trial — 2026-11-10

At the owner's request, the isolated lab was reapproved and reactivated at the
same HTTPS hostname. Nine real-peer HTTPS/authentication checks passed again.
The temporary console and foreground Serve are currently active for user testing;
production configuration remains unchanged. The earlier cleanup above applies
to the completed `398db315` trial, not this active trial.
Current private evidence directory:
`/var/lib/smolofis-identity-machine-lab/private-https-135d5487/`.
Distinct employee tailnet identity/policy acceptance is still pending; denial
of a directory non-administrator at sign-in is already verified separately.

### Owner-only v1 scope and cleanup — 2026-11-10

The user elected to use only their own account/devices for v1 and defer the
separate employee tailnet-policy test. The second Mac was connected to a
different tailnet; its DNS failure does not prove same-tailnet employee denial.
Revisit policy isolation before adding users or sharing the administration node.
The `135d5487` trial is now cleaned up: temporary accounts/runtime units removed,
foreground Serve empty, node logged out and lab tailscaled stopped/disabled.

Production NetworkManager owns `/etc/resolv.conf`; systemd-resolved is inactive.
Applied `tailscale set --accept-dns=false` without joining a tailnet. Resolver
SHA256 remained unchanged and `getent ahostsv4 github.com` succeeded. Console
remains 404 and Tailscale remains `NeedsLogin`. The stopped-node status still
contains the earlier resolve1 warning; connected DNS-health acceptance remains
pending. Source connection/autoconnect defaults now preserve local DNS as well.
This disables automatic tailnet DNS consumption on the server; client devices
may continue using MagicDNS. Roll back the preference with
`sudo tailscale set --accept-dns=true` only after selecting a working resolver
integration. See [Tailscale DNS configuration](https://tailscale.com/docs/reference/faq/dns-resolv-conf).
