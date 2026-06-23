# kubernetes.core (fast local plugins)

Fast `kubectl`/`helm`-based action plugins for local Ansible connections.

These override the matching `kubernetes.core` modules: on a local connection
(and without `become`) they shell out to `kubectl`/`helm` in-process; otherwise
they transparently fall back to the genuine `kubernetes.core` collection.

Provided overrides:

- `helm_repository` — `helm repo add`
- `helm_pull` — `helm pull`
- `helm_info` — `helm status` / `helm get values`
- `k8s` — `kubectl apply`/`delete`
- `k8s_info` — `kubectl get`
