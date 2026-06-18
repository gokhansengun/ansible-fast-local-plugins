# ansible-fast-local-plugins

Performance-optimised Ansible action plugins for a controller that provisions entirely over a **local connection** — no SSH to remote hosts.

Each plugin checks whether the connection is local. If it is, the plugin executes in-process: no subprocess, no SSH, no remote Python interpreter. If the connection is anything else, the plugin falls back transparently to the standard Ansible module or action plugin, so behaviour is always correct even when `become`, `check_mode`, or a non-local connection is in use.

## Usage in your project

Copy the `ansible/plugins/action_plugins/` directory into your repo (or point to it from a shared path), then tell Ansible where to find it via `ansible.cfg`:

```ini
[defaults]
action_plugins = ./ansible/plugins/action_plugins
```

The plugins override the built-in modules by the same short name (`stat`, `copy`, `template`, etc.). No other changes to your playbooks or roles are needed — the fast path activates automatically when the connection is `local`, and falls back to standard behaviour otherwise.

If you also use the `kubernetes.core` collection overrides, add the collections path:

```ini
[defaults]
action_plugins  = ./ansible/plugins/action_plugins
collections_path = ./ansible/collections
```

> **Note:** `action_plugins` accepts a colon-separated list of directories, so you can append this project's path to an existing value rather than replacing it:
> ```ini
> action_plugins = ./my/existing/plugins:./ansible/plugins/action_plugins
> ```

### Optional: route `ansible.builtin.*` names to the fast plugins

The overrides above activate for the bare short name (`template:`) and for `ansible.legacy.template:`, but **not** for the fully-qualified `ansible.builtin.template:` — that FQCN is hard-bound to stock ansible-core. If your roles use `ansible.builtin.*` names and you cannot edit them, enable the bundled callback plugin to transparently redirect them to the fast plugins:

```ini
[defaults]
callback_plugins = ./ansible/plugins/callback_plugins
```

It auto-loads (no `callbacks_enabled` entry needed) and, on startup, rewrites `ansible.builtin.<name>` to `ansible.legacy.<name>` for any name you have a local override of. Builtins you have **not** overridden (`debug`, `set_fact`, …) are untouched, and it fails open if a future ansible-core changes the internals it patches. (This replaces a strategy-plugin approach, which ansible-core 2.19 deprecates and ~2.21 removes.)

### Confirming the fast path ran

Every successful fast (in-process) result carries the key `__produced_by_fast_plugin: true`; the standard fallback result never does. Read it in a playbook via subscript (dotted access to `_`-prefixed keys is blocked by templating):

```yaml
- template: { src: x.j2, dest: /tmp/x }
  register: r
- assert:
    that: "r['__produced_by_fast_plugin'] | default(false)"
```

## Plugins

| Plugin | Fast-path behaviour | Fallback trigger |
|---|---|---|
| `stat` | `os.stat()` in-process | non-local, `become` |
| `file` | `os`/`shutil` filesystem ops in-process | non-local, `become`, check-mode, `access_time`/`modification_time` |
| `tempfile` | `tempfile.mkstemp/mkdtemp` in-process | non-local, `become`, check-mode |
| `copy` | atomic write via `shutil.move` | non-local, `src`-based copy, `become`, unsupported args |
| `template` | Jinja2 rendering via `ansible.template.Templar` | non-local, `become`, unsupported args |
| `command` | `subprocess.run` (no shell) | non-local, `become`, async, `environment` vars, check-mode |
| `shell` | `subprocess.run` (no Ansible module overhead) | non-local, `become`, async, `environment` vars, check-mode |
| `hashivault_read` | `hvac.Client` in-process | non-local |
| `find_next_helm_release_number` | `kubectl get secrets` via subprocess | non-local |

The `kubernetes.core` collection overrides (`k8s`, `k8s_info`, `helm_repository`) follow the same pattern via `kubectl`/`helm`, delegating to the genuine collection when non-local.

## Prerequisites

- Docker (with Compose v2 — `docker compose`)
- GNU Make
- No local Python or Ansible installation required for running tests

## Quick start

```bash
# Run the full test suite for all matrix pairs
make test

# Unit tests only — fast, no Docker services needed beyond the controller image
make test-unit

# Integration tests only
make test-int
```

`make help` lists all available targets.

## Benchmarking

`make bench` measures the speedup the fast plugins deliver versus stock ansible-core on a local connection. For each plugin it runs the same task in a loop both ways — once with the fast action plugins enabled (the repo `ansible.cfg`) and once forced to stock ansible-core (`bench/ansible_stock.cfg`) — and reports the per-task wall-clock and the ratio. It needs only the controller image; no external services start.

```bash
make bench                                   # default: 200 iterations, all plugins
make bench BENCH_ARGS='-n 500 -p copy,stat'  # custom size / subset of plugins
```

Example output (100 iterations, median of 3; absolute times vary by host, the speedup is the stable figure). Both matrix pairs land around **17–19× faster** overall:

**Python 3.12 / ansible-core 2.19.3**

```
plugin          stock       fast    speedup
--------------------------------------------
stat         13.503s    0.807s      16.7x
copy         26.400s    1.434s      18.4x
template     27.200s    1.710s      15.9x
file         14.068s    0.795s      17.7x
command      13.904s    0.798s      17.4x
tempfile     13.557s    0.782s      17.3x
--------------------------------------------
TOTAL       108.631s    6.325s      17.2x
```

**Python 3.14 / ansible-core 2.20.5**

```
plugin          stock       fast    speedup
--------------------------------------------
stat         14.323s    0.849s      16.9x
copy         28.220s    1.389s      20.3x
template     28.089s    1.404s      20.0x
file         14.850s    0.819s      18.1x
command      14.537s    0.838s      17.4x
tempfile     14.208s    0.777s      18.3x
--------------------------------------------
TOTAL       114.227s    6.076s      18.8x
```

> Each pair uses its own controller image — select it with `PAIR`, e.g. `make bench PAIR=3.14:2.20.5:5.6.0`. Because the controller image builds to a single shared tag, switch pairs with `make build MATRIX='<pair>'` first (otherwise `docker compose run` reuses the cached image regardless of the build args).

The reported time subtracts a zero-iteration overhead run (ansible startup + per-run setup) from the measured run, so it isolates the plugin's own per-invocation cost; the timed run is repeated and the median is reported. Each run also asserts the fast-path marker (`__produced_by_fast_plugin`) is present in fast mode and absent in stock mode, so the harness fails loudly rather than silently comparing the wrong code paths. Run `python bench/benchmark.py -h` for all flags (`-n/--iterations`, `-r/--repeat`, `-w/--warmup`, `-p/--plugins`).

## Test matrix

Tests run against multiple Python × ansible-core pairs. The matrix is defined at the top of `Makefile`:

```makefile
MATRIX := 3.12:2.19.3:5.4.0  3.14:2.20.5:5.6.0
#          ^Python  ^ansible-core  ^ansible-modules-hashivault
```

The third column is the `ansible-modules-hashivault` version, which is version-locked to `ansible-core` because it provides the fallback module for `hashivault_read`.

Run a single pair:

```bash
make test MATRIX='3.12:2.19.3:5.4.0'
```

Drop into a shell inside a specific controller image:

```bash
make shell PAIR=3.14:2.20.5:5.6.0
```

## Project structure

```
ansible/
  plugins/
    action_plugins/       # Short-name action plugin overrides (production code)
      _action_utils.py    # Shared helpers: _is_local(), atomic_write(), mark_fast_result()
      stat.py
      file.py
      tempfile.py
      copy.py
      template.py
      command.py
      shell.py
      hashivault_read.py
      find_next_helm_release_number.py
    callback_plugins/     # aflp_builtin_redirect: optional ansible.builtin.* -> fast routing
  collections/            # FQCN collection overrides (e.g. kubernetes.core)
    ansible_collections/

tests/
  conftest.py             # make_action() fixture factory for unit tests
  unit/                   # Pure Python tests, no external services
  integration/
    playbooks/            # ansible-playbook files; use assert tasks for verification
    templates/            # Jinja2 fixtures used by test_template.yml
    inventory/
      local.ini           # localhost / connection: local
      ssh.ini             # ssh-target container
    test_integration.py   # pytest wrapper that invokes ansible-playbook

bench/
  benchmark.py            # Runner: times fast vs stock per plugin, reports speedup
  ansible_stock.cfg       # Stock baseline config (no fast action/callback plugins)
  playbooks/bench.yml     # Parametrised timed playbook (bench_plugin, bench_iterations)
  templates/bench.j2      # Jinja2 fixture for the template benchmark

docker/
  Dockerfile              # Controller image (ARG: PYTHON_VERSION, ANSIBLE_CORE_VERSION, HASHIVAULT_MODULE_VERSION)
  docker-compose.yml      # All services (controller, vault, kind/dind, ssh-target)
  ssh-target/             # openssh-server container for fallback tests
  kind-setup/             # Bootstraps a kind cluster inside DinD; seeds Helm secrets

ansible.cfg               # Sets action_plugins + callback_plugins paths, disables host_key_checking
Makefile
pyproject.toml
.github/workflows/ci.yml
```

## Docker Compose services

| Service | Role |
|---|---|
| `dind` | Docker-in-Docker daemon; kind creates Kubernetes nodes here |
| `kind-setup` | One-shot: creates a kind cluster, seeds Helm release secrets, exports kubeconfig |
| `vault` | HashiCorp Vault in dev mode; root token `root` |
| `vault-seed` | One-shot: writes test secrets to KV v1 and KV v2 |
| `ssh-target` | Runs openssh-server; controller connects here for fallback path tests |
| `controller` | Runs pytest; depends on all other services being healthy/completed |

## Running tests without Docker (unit tests only)

Install dependencies locally and run pytest directly:

```bash
pip install "ansible-core==2.19.3" hvac "ansible-modules-hashivault==5.4.0" \
    pytest pytest-mock pytest-cov
pytest tests/unit/ -v
```

## Adding a new matrix pair

1. Append `PY:AC:HV` to `MATRIX` in `Makefile`.
2. Add a matching `include` entry in `.github/workflows/ci.yml`.

## Adding a new plugin

1. Create `ansible/plugins/action_plugins/myplugin.py` following the local-gate pattern in any existing plugin.
2. Add `tests/unit/test_myplugin.py` with fast-path and fallback test classes.
3. Add `tests/integration/playbooks/test_myplugin.yml` with inline `assert` tasks.
4. Register it in `tests/integration/test_integration.py`.
