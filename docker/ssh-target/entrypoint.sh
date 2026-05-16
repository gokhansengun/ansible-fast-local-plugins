#!/bin/bash
set -e

# Generate SSH host keys
ssh-keygen -A

# Generate ansible user key pair into shared volume (once)
mkdir -p /ssh-keys
if [ ! -f /ssh-keys/id_ed25519 ]; then
    ssh-keygen -t ed25519 -f /ssh-keys/id_ed25519 -N "" -q
fi

# Install public key for ansible user
cp /ssh-keys/id_ed25519.pub /home/ansible/.ssh/authorized_keys
chown -R ansible:ansible /home/ansible/.ssh
chmod 600 /home/ansible/.ssh/authorized_keys

exec /usr/sbin/sshd -D -e
