# QEMU is a process here; the Debian guest boots its own x86 kernel and ext4
# disk. No privileged container, KVM access, host network or Docker socket.
FROM debian:trixie
RUN apt-get update && apt-get install -y --no-install-recommends \
    qemu-system-x86 qemu-utils cloud-image-utils openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /lab
ENTRYPOINT ["qemu-system-x86_64"]
