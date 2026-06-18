# CLAUDE.md — project guide for Claude Code

## What this project is

Performance-optimised Ansible **action plugins** for a local-connection-only controller. Every plugin checks whether the connection is local (`_is_local()`); if so it executes in-process (no SSH, no subprocess spawning a new Python interpreter). If not, it falls back transparently to the equivalent standard Ansible module or action plugin.

Plugins live in `ansible/plugins/action_plugins/`. They are not an Ansible collection — they are standalone `.py` files loaded via `action_plugins` in `ansible.cfg`.

---

## Key patterns

### The local/fallback gate

Every plugin's `run()` starts with this:

```python
if not _is_local(conn) or self._play_context.become:
    return self._execute_module(task_vars=task_vars, ...)  # or instantiate _Standard
```

`_is_local()` is in `_action_utils.py`. It checks `connection.transport`, `connection._load_name`, and `type(connection).__module__`. Setting `conn.transport = 'local'` in tests is sufficient to trigger the fast path.

### Atomic writes

`atomic_write()` in `_action_utils.py` writes to a same-directory temp file then `shutil.move()`s it. Used by `copy.py` and `template.py`.

### Copy vs Template fallback style

`copy.py` and `template.py` instantiate the standard action plugin class directly (`from ansible.plugins.action.copy import ActionModule as _Standard`). `stat.py`, `tempfile.py`, `hashivault_read.py`, and `find_next_helm_release_number.py` call `self._execute_module()`.

### Fast-path marker

Every plugin stamps a successful in-process result with `__produced_by_fast_plugin: True` so tests can prove the fast path ran rather than the standard fallback (whose result never carries it). The standalone plugins import `mark_fast_result` / `FAST_PLUGIN_MARKER` from `_action_utils.py`; the three `kubernetes.core` overrides define a local `mark_fast_result` (they must stay self-contained — they shadow a real collection and cannot import `_action_utils`). The marker is applied centrally: each `run()` resolves the fallback/delegate gate, then returns `mark_fast_result(self._run_local(...))`. Only non-failed dicts are marked (`setdefault`, so an explicit value is never clobbered), so failures and delegated results stay unmarked. ansible-core only strips `_ansible_`-prefixed keys, so the marker survives into the registered result; read it in playbooks via subscript (`r['__produced_by_fast_plugin']`), not dotted access, which the templating sandbox blocks for `_`-prefixed names.

### FQCN redirect (`ansible.builtin.*` → fast plugins)

Local action plugins live in the *legacy* namespace, so bare `template:` and `ansible.legacy.template:` hit the fast plugin, but `ansible.builtin.template:` does **not** (that FQCN is hard-bound to stock ansible-core). To let role authors keep `ansible.builtin.*` names and still get the fast path *without editing roles*, a callback plugin rewrites the name at resolution time.

`ansible/plugins/callback_plugins/aflp_builtin_redirect.py` wraps `ansible.plugins.loader.action_loader.get` so that `ansible.builtin.<name>` becomes `ansible.legacy.<name>` whenever a `<name>.py` exists in the `action_plugins` path. It is enabled via `callback_plugins` in `ansible.cfg` and auto-loads (`CALLBACK_NEEDS_ENABLED = False`); `load_callbacks()` runs before the worker fork, so the patch is inherited by every task worker. Overrides are auto-discovered — adding a new action plugin needs no change here. The patch is **fail-open**: any error installing it or rewriting a name falls back to stock behaviour.

> Why a callback and not a strategy plugin: custom strategy plugins (the other interception point) are deprecated in ansible-core 2.19 and slated for removal (~2.21) — see ansible/ansible#84725. The patched symbol (`action_loader.get`) is verified against the 2.19 / 2.20 matrix; re-check on a matrix bump.

---

## Test architecture

### Unit tests (`tests/unit/`)

Use `make_action(plugin_name, args, **kwargs)` from `tests/conftest.py`. It wires a real `DataLoader` + `Templar` with `MagicMock` task/connection/play-context so `ActionModule.__init__` and `ActionBase.run()` succeed without a real Ansible inventory.

To test the **fast path**: `make_action('stat', {...})` — defaults to `local=True`.  
To test the **fallback gate**: `make_action('stat', {...}, local=False)`, then assert `_execute_module` was called via `patch.object`.

Unit tests do not require any Docker service. Run with:

```
make test-unit
```

### Integration tests (`tests/integration/`)

`test_integration.py` is a thin pytest wrapper that invokes `ansible-playbook` as a subprocess and fails the test if the exit code is non-zero. The playbooks contain the actual assertions via Ansible's `assert` module.

Integration tests require the full Docker Compose stack (vault, kind, ssh-target). Run with:

```
make test-int   # or: make test  (unit + integration)
```

---

## Test matrix

Defined at the top of `Makefile`:

```makefile
MATRIX := 3.12:2.19.3:5.4.0  3.14:2.20.5:5.6.0
#          ^Python  ^ansible-core  ^ansible-modules-hashivault
```

The third slot exists because `ansible-modules-hashivault` versions are tied to `ansible-core`. Adding a new pair means appending one entry here (and in `.github/workflows/ci.yml`).

---

## Docker Compose services

| Service | Image | Purpose |
|---|---|---|
| `dind` | `docker:24-dind` | Docker daemon for kind to create containers in |
| `kind-setup` | custom Alpine | Creates a 1-node kind cluster inside DinD; exposes API at `dind:6443` |
| `vault` | `hashicorp/vault:1.17` | Dev-mode Vault; root token `root` |
| `vault-seed` | same vault image | One-shot: seeds KV v1 at `secret/test-kv1`, KV v2 at `secretv2/test-kv2` |
| `ssh-target` | custom Debian | openssh-server; generates ED25519 key pair into `ssh-keys` volume |
| `controller` | custom Python | Runs pytest; mounts repo read-only, kubeconfig and ssh-keys volumes |

### Kind networking

Kind runs inside DinD. `kind-cluster.yaml` sets `apiServerAddress: 0.0.0.0` and `apiServerPort: 6443`, and maps `containerPort 6443 → hostPort 6443` on the DinD container. `setup.sh` patches the exported kubeconfig so `server:` points to `https://dind:6443` with `insecure-skip-tls-verify: true` (the TLS cert is issued for `127.0.0.1`, not `dind`).

Pre-seeded Kubernetes objects:
- Namespace `test-ns` with Helm release secrets for `test-release` at versions 2 and 3 (both `status=deployed`), so `current_version=3, next_version=4`.
- Namespace `test-ns-empty` — no secrets, so `current_version=0, next_version=1`.

### SSH target

The `ssh-target` container generates a fresh ED25519 key pair into the `ssh-keys` named volume on every cold start. The controller's `entrypoint.sh` waits for `/ssh-keys/id_ed25519` before running pytest. The SSH inventory at `tests/integration/inventory/ssh.ini` references that path.

---

## Adding a new plugin

1. Drop `myplugin.py` in `ansible/plugins/action_plugins/`. Follow the local-gate pattern.
2. Add `tests/unit/test_myplugin.py` with fast-path and fallback classes.
3. Add `tests/integration/playbooks/test_myplugin.yml` with `assert` tasks.
4. Add a `test_myplugin()` function in `tests/integration/test_integration.py`.
5. If the plugin uses external services, add them to `docker/docker-compose.yml` and seed data if needed.

## Benchmark harness (`bench/`)

`make bench` quantifies the fast-path speedup vs stock ansible-core. It runs the controller image with `--no-deps` (like `make test-unit`) — no external services. `bench/benchmark.py` drives `bench/playbooks/bench.yml` once per plugin per mode:

- **fast** → `ANSIBLE_CONFIG=./ansible.cfg` (the repo config, fast plugins loaded)
- **stock** → `ANSIBLE_CONFIG=bench/ansible_stock.cfg` (a deliberately stripped config with *no* `action_plugins`/`callback_plugins`, so bare names resolve to ansible-core modules)

`bench.yml` is parametrised by `bench_plugin` (selects one timed block via `when:`) and `bench_iterations`. The reported figure is `time(N iterations) − time(0 iterations)`: the zero-iteration run captures ansible startup + per-run setup so the runner subtracts it, isolating the plugin's per-invocation cost. The N>0 run is repeated (`--repeat`, default 3) and the **median** is reported.

Each block captures the `__produced_by_fast_plugin` marker from its *own* fresh `register` into the `bench_marker_present` fact, then one assert at the end checks it matches the mode (present in fast, absent in stock). This is load-bearing: all `TIMED <plugin>` tasks would otherwise share `register: bench_probe`, and a **skipped** task still overwrites its register with a skip result — so the marker must be captured per-block (`set_fact`) while the register is fresh, never read from a single shared `bench_probe` after the fact. The assert makes an accidentally-invalid comparison (both modes running the same code) fail loudly instead of silently reporting ~1x.

Adding a plugin to the benchmark: add a `when: bench_plugin == '<name>'` block in `bench.yml` (timed task named `TIMED <name>` + the `Capture marker` `set_fact`) and append the name to `ALL_PLUGINS` in `benchmark.py`. Only pure-local plugins belong here; the hashivault/k8s plugins are network/service-bound, not CPU-bound local wins.

## Adding a new matrix pair

1. Append `PY:AC:HV` to `MATRIX` in `Makefile`.
2. Add a matching `include` entry in `.github/workflows/ci.yml`.

---

## Dependency notes

- `hvac` — Python client used by `hashivault_read.py` in the fast path.
- `ansible-modules-hashivault` — provides the `hashivault_read` *module* used in the fallback path; version is coupled to `ansible-core` (see matrix).
- `kubectl` — installed in the controller image; used by `find_next_helm_release_number.py` via `subprocess.run`.
