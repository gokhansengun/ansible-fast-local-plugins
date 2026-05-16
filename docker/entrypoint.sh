#!/bin/bash
set -e

echo "==> Waiting for SSH keys from ssh-target..."
timeout=60
elapsed=0
while [ ! -f /ssh-keys/id_ed25519 ]; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [ $elapsed -ge $timeout ]; then
        echo "ERROR: SSH keys not available after ${timeout}s" >&2
        exit 1
    fi
done

# The ssh-keys volume is read-only; copy to a writable location SSH will accept.
mkdir -p /tmp/ssh-keys
cp /ssh-keys/id_ed25519 /tmp/ssh-keys/id_ed25519
chmod 600 /tmp/ssh-keys/id_ed25519
echo "==> SSH keys ready at /tmp/ssh-keys/id_ed25519."

exec "$@"
