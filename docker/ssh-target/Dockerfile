FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash ansible \
    && mkdir -p /var/run/sshd \
    && mkdir -p /home/ansible/.ssh \
    && chmod 700 /home/ansible/.ssh

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 22
HEALTHCHECK --interval=3s --timeout=5s --retries=20 \
    CMD test -f /ssh-keys/id_ed25519 && ssh-keyscan -p 22 localhost >/dev/null 2>&1

ENTRYPOINT ["/entrypoint.sh"]
