# aflp.kubernetes_core (fast local plugins)

Fast `kubectl`/`helm`-based action plugins that override `kubernetes.core` for
local Ansible connections.

These plugins are routed onto the `kubernetes.core.<name>` actions by the
`aflp_kubernetes_redirect` callback, so roles keep their `kubernetes.core.*`
names unchanged. On a local connection (without `become`) they shell out to
`kubectl`/`helm` in-process. For a non-local connection, `become`, or an
unsupported argument they delegate to the **genuine** upstream `kubernetes.core`
collection (a required dependency, installed separately), so behaviour stays
correct — the task simply runs via the upstream plugin instead of the fast path.

Because this is a distinctly named collection (`aflp.kubernetes_core`, not a
shadow of `kubernetes.core`), the delegation import resolves to the genuine
collection rather than back to these overrides — there is no self-recursion and
no fail-fast: real fallback just works.

Provided overrides:

- `helm_repository` — `helm repo add`
- `helm_pull` — `helm pull`
- `helm_info` — `helm status` / `helm get values`
- `k8s` — `kubectl get`/`create`/`patch`/`replace`/`delete` (`apply` only for `apply: true`)
- `k8s_info` — `kubectl get`
