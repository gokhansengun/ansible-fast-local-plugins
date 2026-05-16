#!/bin/bash
set -e

export KUBECONFIG=/kubeconfig/config

echo "==> Removing any leftover cluster from a previous run..."
kind delete cluster --name aflp-test 2>/dev/null || true

echo "==> Creating kind cluster (talking to DinD at ${DOCKER_HOST})..."
kind create cluster --name aflp-test --config /kind-cluster.yaml

echo "==> Exporting kubeconfig..."
kind get kubeconfig --name aflp-test > "${KUBECONFIG}"

echo "==> Patching kubeconfig: any-host:6443 -> dind:6443, disabling TLS verify..."
# kind sets apiServerAddress as the server URL; 0.0.0.0 is a bind address —
# replace it with the DinD service hostname reachable on the compose network.
sed -i 's|server: https://[^:]*:6443|server: https://dind:6443|g' "${KUBECONFIG}"
sed -i '/certificate-authority-data:/d' "${KUBECONFIG}"
# Insert insecure-skip-tls-verify on the line after the server line
sed -i 's|server: https://dind:6443|server: https://dind:6443\n    insecure-skip-tls-verify: true|' "${KUBECONFIG}"

echo "==> Waiting for node to be Ready..."
kubectl wait --for=condition=Ready nodes --all --timeout=120s

echo "==> Creating test namespace and Helm release secrets..."
kubectl create namespace test-ns

# Two deployed secrets so the plugin must pick the highest version (3)
for ver in 2 3; do
    kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: sh.helm.release.v1.test-release.v${ver}
  namespace: test-ns
  labels:
    name: test-release
    owner: helm
    status: deployed
    version: "${ver}"
type: helm.sh/release.v1
data:
  release: dGVzdA==
EOF
done

# Release with no existing secrets (for next_version=1 test)
kubectl create namespace test-ns-empty

echo "==> Kind cluster ready."
