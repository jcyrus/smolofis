# Directory administrator sign-in preview

This is an opt-in authentication layer for the read-only console. It is not
installed or enabled by image construction. It does not add user/share editing,
service restarts or production HTTPS setup. An opt-in [private HTTPS candidate](admin-private-access.md)
now has preliminary local TLS acceptance; live tailnet acceptance remains open.
Optional [local enrollment and
recovery controls](admin-enrollment.md) add device binding and offline lockdown.
The console still requires a loopback bind even when authentication is enabled.

## Configuration

Create a dedicated directory group for console administrators. Ordinary office
users and Gitea administrators are not automatically console administrators.
The configured group may contain nested groups. The directory reader account
needs permission to read users' immutable IDs, enabled state, and membership;
it does not need directory write privileges.

Create a local JSON file with these keys, replacing the example domain and
paths with your isolated test directory configuration:

```json
{
  "origin": "http://127.0.0.1:18091",
  "directory": {
    "url": "ldaps://dc.office.example",
    "realm": "OFFICE.EXAMPLE",
    "base_dn": "DC=office,DC=example",
    "admin_group_dn": "CN=Console-Administrators,CN=Users,DC=office,DC=example",
    "ca_file": "/etc/smolofis/directory-ca.pem",
    "reader_dn": "CN=panel-reader,CN=Users,DC=office,DC=example",
    "reader_password_file": "/etc/smolofis/panel-reader-password"
  }
}
```

Keep the configuration and CA protected from untrusted writes. Keep the reader
password file readable only by the panel account and its trusted operators.
Passwords are loaded from a file, never from CLI arguments or JSON values.
A final newline in that password file is ignored. The reader secret remains in
memory for directory checks and is zeroized when the directory client drops.
The panel performs no automatic directory/group provisioning.

Set `SMOLOFIS_AUTH_STATE` to a locally enrolled state file to opt into the
device binding and local lockdown controls. Without it, this remains the
earlier sign-in-only development preview and does not honor enrollment locks.

```sh
SMOLOFIS_CONSOLE_ENABLED=true SMOLOFIS_BIND=127.0.0.1:18091 \
  SMOLOFIS_AUTH_CONFIG=/absolute/path/panel-auth.json \
  cargo run --locked --manifest-path src-dashboard/Cargo.toml
```

`origin` must exactly match the browser's canonical origin, with no trailing
slash. HTTP is permitted only for loopback development origins; HTTPS origins
set Secure cookies. HTTPS termination and remote access are still release
gates. Forwarded headers are not trusted. Except for public liveness, the Host
header must match the configured origin. Invalid or unreadable supplied
configuration or CA material fails startup rather than falling back to public access.

Without `SMOLOFIS_AUTH_CONFIG`, the original read-only dashboard and loopback
console preview retain their existing behavior. Setting this variable requires
the console feature flag. It is not a production authentication toggle yet.

## Session behavior

- `/auth/login` is a server-rendered, JavaScript-independent form. Usernames
  contain up to 64 ASCII letters/digits, dots, underscores, or hyphens, without
  the domain. Passwords may contain up to 1024 UTF-8 bytes and cannot be empty.
- User passwords bind directly to the configured realm over LDAPS. Certificate
  chain and hostname verification are mandatory. No plaintext LDAP fallback or
  insecure certificate option exists.
- Sign-in requires an enabled AD user with membership in the configured group.
  Sessions use the raw 16-byte `objectGUID`, never email or display-name matching.
- Each protected HTTP request rechecks enabled state and administrator membership
  using the reader account. A denied check deletes that session. Re-enabling
  the account does not restore an already-revoked session. Changes undone before
  any check are not observed: explicit lifecycle-triggered revocation remains
  part of M3. Password changes alone do not revoke panel sessions yet.
- Directory errors deny protected access with HTTP 503. They do not silently
  authorize cached sessions. A temporary outage does not delete the session.
- Sessions live only in process memory and expire after 30 minutes without a
  protected request or eight hours total. Polling counts as activity. Restarting
  the panel signs everyone out. There is no remember-me option.
- With enrollment selected, a local lock or re-enrollment rotates the session
  generation. Prior sessions cannot resume after re-enrollment. Missing,
  mismatched, malformed, or insecure state blocks access; no fallback bypass exists.
- Session tokens use 256 bits of OS randomness; only SHA-256 token digests are
  kept in the session store. Cookies are host-only, HttpOnly, SameSite=Strict,
  and Secure for HTTPS origins. Successful sign-in rotates the current browser
  session. At most 256 sessions are held.
- Native form POSTs require the configured Origin and a CSRF token. Sign-out
  deletes the local session and remains possible from an already-open account
  form during a directory outage. It does not sign out Gitea or SMB.
- Login attempts are limited to five per normalized username per minute and
  twenty globally per minute, including successful attempts. The in-memory
  limits reset on restart; distributed throttling is not implemented.
- Up to eight directory checks run concurrently, each with a five-second
  deadline. Login bodies are limited to 8192 bytes. Logs record fixed sign-in,
  sign-out, and revocation events without usernames, passwords, cookies, or
  request bodies. Durable actor-attributed auditing is still outstanding.

When configured, authentication covers the classic panel, console pages, and
`/api/state`. Assets, sign-in, and GET `/healthz` stay public. CI automation
credentials and deployment wiring remain unimplemented, so the production
telemetry-authentication milestone remains open.

## Validation

Optional [durable authentication auditing](admin-audit.md) is available through
`SMOLOFIS_AUDIT_FILE`. It records verified directory GUIDs and typed events;
local recovery and privileged-operation auditing remain separate work.

`cargo test --locked --manifest-path src-dashboard/Cargo.toml` exercises HTTP
session behavior with a synthetic directory. The ignored `browser_fixture`
test runs an explicitly selected, loopback-only synthetic-directory UI fixture;
it is not part of the appliance binary or the regular test suite.

For the actual Samba integration, compile a Linux binary and run:

```sh
bash scripts/identity-lab.sh panel-auth /absolute/path/to/linux/smolofis-panel
```

The runner uses the existing isolated Docker network, publishes no host ports,
and mounts only the selected binary read-only in addition to the synthetic
credential volume. It creates dedicated synthetic panel users and a group,
checks real LDAPS sign-in, denies non-admins/wrong passwords, exercises group
removal/account disabling/session revocation and sign-out, and rejects an
untrusted CA and certificate hostname mismatch. Fixture account state is
restored afterward. The directory container is left running for inspection;
`bash scripts/identity-lab.sh stop` stops the lab without deleting volumes.

This acceptance is preliminary container evidence. Full Linux VM, native
clients, full recovery/restore acceptance, HTTPS, lifecycle revocation,
security review, and deployment acceptance remain required before release.
