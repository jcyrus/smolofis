# Private HTTPS console preview

This adds an opt-in transport candidate for the enrolled read-only console.
It has not been activated on a real tailnet, and image construction does not
install or enable the new units. Full VM, actual Tailscale certificate issuance,
access policy, remote mobile, boot lifecycle, and security acceptance remain
release gates. Durable actor-attributed auditing is also unfinished.

Subsequent 2026-11-09 evidence covers actual backend/worker unit execution in
the isolated full-kernel Debian VM; see sandbox acceptance below. It does not
cover Tailscale Serve, real certificates, network policy, or phone access.

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

Real tailnet/certificate/policy/mobile/Serve lifecycle acceptance remains open.
