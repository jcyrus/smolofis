FROM debian:trixie

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    samba-ad-dc samba-ad-provision samba-vfs-modules samba winbind libnss-winbind smbclient ldap-utils \
    krb5-user openssl ca-certificates curl python3 sqlite3 acl procps git openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/identity-lab
COPY verify.py /usr/local/bin/verify-identity
COPY control.py /usr/local/bin/lab-control
COPY migration.py /usr/local/bin/lab-migration
COPY storage.py /usr/local/bin/lab-storage
COPY revoke-sessions.py /usr/local/bin/lab-revoke-sessions
COPY check-session-guards.py /usr/local/bin/lab-check-session-guards
COPY live-smb.py /usr/local/bin/lab-live-smb
COPY panel-auth.py /usr/local/bin/lab-panel-auth
COPY private-https.py /usr/local/bin/lab-private-https
RUN chmod 0755 /usr/local/bin/identity-lab
ENTRYPOINT ["/usr/local/bin/identity-lab"]
