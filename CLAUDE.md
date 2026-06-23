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

### Global kill-switch (`AFLP_DISABLE`)

`_is_local()` first consults `_fast_disabled()` (also in `_action_utils.py`), which returns True when the `AFLP_DISABLE` env var is truthy (`1`/`true`/`yes`/`on`). When set, `_is_local()` returns False for *every* connection, so all 8 standalone plugins delegate to stock — an operator can turn the whole fast path off (debugging "is a fast plugin the culprit?", or A/B parity checks) without touching config or playbooks. The env is read at gate time, so it can change between runs. Because the fast path never runs under the switch, results are unmarked, so the summary callback reports `fast=0 fallback=N` (a built-in confirmation the switch took effect). The five `kubernetes.core` overrides live in the separate `aflp.kubernetes_core` collection (see *kubernetes.core overrides* below) and can't import `_action_utils`, so each carries the same inline `AFLP_DISABLE` check at the top of its own `_is_local()` — keep those in sync with the shared one. Under the switch they behave like the standalone plugins but delegate to the **genuine `kubernetes.core` collection** (a real installed dependency) rather than to ansible-core: the task still runs, unmarked, via the upstream plugin. (Only if the genuine collection is absent does a non-local task fail with an actionable message instead.) So unlike the old directory-shadow design, the parity set's `AFLP_DISABLE` stock baseline is now meaningful for these too, though they remain out of the parity set because their output is cluster/service-bound, not deterministic.

### Strict mode (`AFLP_STRICT`)

`strict_guard(connection, play_context, task=None)` in `_action_utils.py` raises `AnsibleActionFail` (with a best-effort reason — non-local / become / check_mode / async / "unsupported arguments") when `AFLP_STRICT` is truthy, so an *unintended* fallback fails the task instead of silently running slow. It is **called at the top of every plugin's fallback branch**, right before the delegate (the `_execute_module(...)` or `_Standard = _load_builtin_action(...)` line) — all 14 standalone plugins import and call it. It is a no-op when `AFLP_STRICT` is unset (so it never affects normal runs or existing tests), and `_fast_disabled()` suppresses it (AFLP_DISABLE is an intentional fallback; the two are not meant to be combined). The five `kubernetes.core` overrides define a self-contained `_strict_guard()` (alongside their own `_is_local()`) and call it at their delegation points — keep these in sync with the shared one. When adding a new plugin, add a `strict_guard(conn, self._play_context, self._task)` call to each of its fallback branches.

### Atomic writes

`atomic_write()` in `_action_utils.py` writes to a same-directory temp file then `shutil.move()`s it. Used by `copy.py`, `template.py`, `lineinfile.py`, and `get_url.py`. Mode handling matches stock ansible's `atomic_move`: an explicit `mode` is applied; otherwise an *existing* file keeps its perms and a *new* file gets the umask default (`0666 & ~umask`) — **not** mkstemp's restrictive `0600`. This umask default lives only here, so `copy`/`template`/`lineinfile`/`get_url` all inherit it (don't re-implement it per plugin).

### Copy vs Template fallback style

`copy.py` and `template.py` instantiate the standard action plugin class directly (`from ansible.plugins.action.copy import ActionModule as _Standard`). `stat.py`, `tempfile.py`, `hashivault_read.py`, and `find_next_helm_release_number.py` call `self._execute_module()`.

`copy.py` fast-paths **two** shapes to a local `dest`: inline `content`, and a single local-file `src` (resolved via `self._find_needle('files', ...)`, read on the controller, written with `atomic_write`; a directory dest receives the file by basename, matching stock). It falls back for `remote_src`, a directory source, `mode: preserve`, `both/neither content+src`, and the usual ownership/backup/validate args. Because these copy-specific triggers are invisible to `strict_guard`'s generic detection (non-local / become / check_mode / async), `copy.py` passes them as `extra_reasons=[...]` so `AFLP_STRICT` names the real cause (e.g. `remote_src`, `directory source`) instead of the catch-all "unsupported arguments". A new override that falls back for plugin-specific reasons should do the same.

### Fast-path marker

Every plugin stamps a successful in-process result with `__produced_by_fast_plugin: True` so tests can prove the fast path ran rather than the standard fallback (whose result never carries it). The standalone plugins import `mark_fast_result` / `FAST_PLUGIN_MARKER` from `_action_utils.py`; the five `kubernetes.core` overrides define a local `mark_fast_result` (they live in the separate `aflp.kubernetes_core` collection and cannot cleanly import `_action_utils` without breaking the file-path unit-test loader, so they stay self-contained). The marker is applied centrally: each `run()` resolves the fallback/delegate gate, then returns `mark_fast_result(self._run_local(...))`. Only non-failed dicts are marked (`setdefault`, so an explicit value is never clobbered), so failures and delegated results stay unmarked. ansible-core only strips `_ansible_`-prefixed keys, so the marker survives into the registered result; read it in playbooks via subscript (`r['__produced_by_fast_plugin']`), not dotted access, which the templating sandbox blocks for `_`-prefixed names.

### FQCN redirect (`ansible.builtin.*` → fast plugins)

Local action plugins live in the *legacy* namespace, so bare `template:` and `ansible.legacy.template:` hit the fast plugin, but `ansible.builtin.template:` does **not** (that FQCN is hard-bound to stock ansible-core). To let role authors keep `ansible.builtin.*` names and still get the fast path *without editing roles*, a callback plugin rewrites the name at resolution time.

`ansible/plugins/callback_plugins/aflp_builtin_redirect.py` wraps `ansible.plugins.loader.action_loader.get` so that `ansible.builtin.<name>` becomes `ansible.legacy.<name>` whenever a `<name>.py` exists in the `action_plugins` path. It is enabled via `callback_plugins` in `ansible.cfg` and auto-loads (`CALLBACK_NEEDS_ENABLED = False`); `load_callbacks()` runs before the worker fork, so the patch is inherited by every task worker. Overrides are auto-discovered — adding a new action plugin needs no change here. The patch is **fail-open**: any error installing it or rewriting a name falls back to stock behaviour.

> Why a callback and not a strategy plugin: custom strategy plugins (the other interception point) are deprecated in ansible-core 2.19 and slated for removal (~2.21) — see ansible/ansible#84725. The patched symbol (`action_loader.get`) is verified against the 2.19 / 2.20 / 2.21 matrix; re-check on a matrix bump.

### kubernetes.core overrides (`aflp.kubernetes_core` + redirect + genuine fallback)

The five k8s/helm overrides (`k8s`, `k8s_info`, `helm_repository`, `helm_pull`, `helm_info`) are **not** standalone `action_plugins`; they are a real collection, `aflp.kubernetes_core`, under `ansible/collections/ansible_collections/aflp/kubernetes_core/` (loaded from source via `collections_path`). They used to shadow `kubernetes.core` by occupying its collection directory — but that made their fallback import (`from ansible_collections.kubernetes.core...`) resolve back to themselves, so they could never delegate (only fail or, with a self-shadow guard, avoid infinite recursion). The current design fixes that:

- **Separate name.** Living under `aflp.kubernetes_core` means the delegation import `from ansible_collections.kubernetes.core.plugins.action.<name> import ActionModule` resolves to the **genuine** upstream collection, which is installed as a real dependency (`requirements.yml`; Dockerfile `ansible-galaxy collection install`, plus the `kubernetes` pip client). `_load_standard_action()` is now a plain import wrapped in `try/except ImportError` (returns `None` only if genuine is absent). There is no `_FAST_LOCAL_OVERRIDE` marker and no recursion guard any more — the self-import is structurally impossible.
- **Transparency via redirect.** `ansible/plugins/callback_plugins/aflp_kubernetes_redirect.py` mirrors `aflp_builtin_redirect`: it wraps `action_loader.get` so `kubernetes.core.<name>` → `aflp.kubernetes_core.<name>` for *only* the overridden names (auto-discovered from the collection's `action/` dir; every other `kubernetes.core.*` action passes through to genuine). Roles keep their `kubernetes.core.*` names and still get the fast path. Crucially the redirect patches `action_loader.get`, **not** Python imports, so the in-plugin delegation import still reaches genuine `kubernetes.core`. It also patches `action_loader.has_plugin` to report our overridden names as present: ansible-core's `TaskExecutor._get_action_handler_with_module_context` only routes a task through `action_loader.get(<fqcn>)` when `has_plugin(<fqcn>)` is True — otherwise it runs the genuine *module* via the `normal` action and bypasses the rewrite. This matters because some genuine plugins are **module-only** (e.g. `helm_pull` has a module but no action plugin in `kubernetes.core` 5.x).
- **helm_pull is the module-only exception.** Because genuine `kubernetes.core.helm_pull` has no action plugin to instantiate, its override delegates via `self._execute_module(task_vars=...)` (module_name defaults to `self._task.action`, resolved by `module_loader`, untouched by the redirect) instead of importing a standard action class. It therefore has no `_load_standard_action`; the other four delegate by instantiating the genuine action class.
- **Real fallback.** Non-local / `become` / unsupported-arg tasks now genuinely delegate to upstream `kubernetes.core` (unmarked result), instead of failing. `collections_path` in `ansible.cfg` is `./ansible/collections:~/.ansible/collections` so both the source `aflp.kubernetes_core` and the galaxy-installed genuine `kubernetes.core` resolve.

When adding a sixth override: drop `<name>.py` in the collection's `action/` dir (the redirect auto-discovers it), keep the self-contained `_is_local`/`_strict_guard`/`mark_fast_result`/`_load_standard_action` helpers, and add `tests/unit/test_<name>.py` (point `PLUGIN_DIR` at `COLLECTION_PLUGIN_DIRS['aflp.kubernetes_core']`).

### Fast-path summary callback (`aflp_fast_path_summary`)

`ansible/plugins/callback_plugins/aflp_fast_path_summary.py` tallies, per action, fast (in-process) vs fallback (stock) results — classified by the `__produced_by_fast_plugin` marker — and prints a table at `v2_playbook_on_stats`. It exists to surface *silent* fallbacks (a task that should be fast but hit become / non-local / an unsupported arg). Unlike the redirect callback it is **opt-in** (`CALLBACK_NEEDS_ENABLED = True`), enabled via `callbacks_enabled = aflp_fast_path_summary` in `ansible.cfg` — the convention for informational callbacks (profile_tasks/timer). It discovers which actions to report the same way the redirect callback does (sibling `action_plugins` dir + config paths), counts loop results per item (skipped items excluded), ignores failures/skips (the marker is absent on failures regardless of path), and is fail-open.

> Gotcha: because this callback is *enabled*, ansible parses its `DOCUMENTATION` block as YAML at load. A bare `: ` (colon-space) inside prose there aborts the **whole run** with a YAML scan error pointing at a `<unicode string>` — not at your playbook. Keep colons out of description/notes text (the redirect callback, being auto-loaded but never doc-parsed the same way, was less sensitive). The fast-path tests catch this.

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

### Output-parity tests (`test_parity`)

`tests/integration/playbooks/parity.yml` + the `test_parity` parametrisation in `test_integration.py` prove each deterministic local plugin (copy, template, lineinfile, stat, slurp, fetch, get_url, command, shell) produces output equivalent to stock ansible-core. For each plugin the runner runs `parity.yml` twice — stock via `AFLP_DISABLE=1` (the #4 kill-switch), then fast — and compares the two dumped results. Two checks: **side-effect parity** (the produced file's checksum+mode match) and **result parity** (`_parity_diffs` recurses and compares only keys present in *both* dumps, since fast plugins intentionally return a minimal subset, skipping a volatile `_PARITY_DENYLIST`). Validity asserts the fast dump carries the marker and the stock dump does not, so it can never silently compare fast-vs-fast.

> Two real divergences this surfaced: (1) stock copy/template report `src` as an internal AnsiballZ tmp staging path while the fast plugins report the real source — denylisted as an implementation detail. (2) fast copy/template created a *new* file at `0600` (mkstemp) whereas stock uses the umask default `0644` — **fixed** by giving `atomic_write` the umask default for new files (see Atomic writes above), which also let get_url/lineinfile drop their own per-plugin umask code. `parity.yml` therefore sets no mode, so the sidecar verifies the default-perms path too.

---

## Test matrix

Defined at the top of `Makefile`:

```makefile
MATRIX := 3.14:2.20.5:5.6.0  3.12:2.19.3:5.4.0
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
- `helm` — installed in the controller image; used by the `helm_*` fast plugins via `subprocess.run`.
- `kubernetes.core` (genuine, upstream) — installed via `requirements.yml` (`ansible-galaxy collection install`); the `aflp.kubernetes_core` overrides delegate to it for non-local / `become` tasks. Pinned to one version that spans the ansible-core matrix.
- `kubernetes` / `jsonpatch` (pip) — Python clients the genuine `kubernetes.core` modules need when a delegated task actually runs.
