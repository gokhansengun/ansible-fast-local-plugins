#!/bin/sh
set -e

echo "==> Seeding Vault with test secrets..."

# Vault dev mode mounts 'secret' as KV v2 — enable a dedicated KV v1 mount.
vault secrets enable -path=kv1-test -version=1 kv || true
vault write kv1-test/test-kv1 username=admin password=s3cr3t

# Enable KV v2 at 'secretv2'
vault secrets enable -path=secretv2 -version=2 kv || true
vault kv put secretv2/test-kv2 api_key=my-api-key-value db_host=postgres.internal

echo "==> Vault seeding complete."
