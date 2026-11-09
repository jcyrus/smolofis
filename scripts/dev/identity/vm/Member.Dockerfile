FROM debian:trixie
RUN apt-get update && apt-get install -y --no-install-recommends \
    samba winbind libnss-winbind smbclient acl python3 && rm -rf /var/lib/apt/lists/*
COPY member.sh /usr/local/bin/m0-member
ENTRYPOINT ["bash", "/usr/local/bin/m0-member"]
