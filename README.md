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

### Seeing fast-path vs fallback across a whole run

The bundled `aflp_fast_path_summary` callback tallies, per action, how many task
results came from a fast in-process plugin versus the stock fallback, and prints a
table when the play finishes. It makes *silent* fallbacks visible — a task you
expected to run fast but which hit `become`, a non-local connection or an
unsupported argument shows up in the `fallback` column. Enable it in `ansible.cfg`:

```ini
[defaults]
callback_plugins  = ./ansible/plugins/callback_plugins
callbacks_enabled = aflp_fast_path_summary
```

```
AFLP FAST-PATH SUMMARY *********************************************************
  copy               fast=142    fallback=0
  lineinfile         fast=15     fallback=1        <- fell back to stock
  template           fast=88     fallback=0
  uri                fast=30     fallback=2        <- fell back to stock
  TOTAL              fast=275    fallback=3
```

Only actions that have a local override are listed (unrelated tasks like `debug`
or `set_fact` are ignored), looped tasks are counted per item, and it is fail-open
and read-only — it never alters a result or breaks a run.

Run `make demo` for a guided tour: it runs a small play normally (a mixed
`fast`/`fallback` table), again with `AFLP_DISABLE=1` (every row `fast=0`), and
again with `AFLP_STRICT=1` (the run fails loudly at the one fallback task), then
finishes with a mini fast-vs-stock benchmark for `copy`/`stat`.

### Disabling the fast path (kill-switch)

Set `AFLP_DISABLE` to a truthy value (`1`, `true`, `yes`, `on`) to force **every**
fast plugin to fall back to stock ansible-core for that run — no edits to
`ansible.cfg`, roles or playbooks. The controller then behaves exactly as if the
fast plugins were not installed.

```bash
AFLP_DISABLE=1 ansible-playbook site.yml
```

This is the quickest way to answer "is a fast plugin causing this?" — flip it and
re-run; if the symptom disappears, a fast plugin is implicated. It also pairs with
the summary callback above (everything shows up as `fallback`) and is handy for
A/B correctness checks. The variable is read per task, so it can vary between runs.

### Strict mode (fail on fallback)

Set `AFLP_STRICT` to a truthy value to make any task that would fall back to stock
ansible-core **fail loudly** instead of silently running the slow path. On a
local-only controller every task is expected to hit the fast path, so an
unexpected fallback (a stray `become`, a non-local connection, an unsupported
argument) signals an unintended task — strict mode surfaces it.

```bash
AFLP_STRICT=1 ansible-playbook site.yml
```

The failing task reports why it would have fallen back, e.g.:

```
fatal: [localhost]: FAILED! => {"msg": "AFLP_STRICT: this task would fall back
from a fast local plugin to stock ansible-core (become); refusing because
AFLP_STRICT is set, so unintended fallbacks are caught rather than silently run slow."}
```

`AFLP_DISABLE` takes precedence — it is an *intentional* global fallback, so it
suppresses strict mode rather than making every task fail.

## Plugins

| Plugin | Fast-path behaviour | Fallback trigger |
|---|---|---|
| `stat` | `os.stat()` in-process | non-local, `become` |
| `file` | `os`/`shutil` filesystem ops in-process | non-local, `become`, check-mode, `access_time`/`modification_time` |
| `tempfile` | `tempfile.mkstemp/mkdtemp` in-process | non-local, `become`, check-mode |
| `copy` | atomic write via `shutil.move` | non-local, `src`-based copy, `become`, unsupported args |
| `template` | Jinja2 rendering via `ansible.template.Templar` | non-local, `become`, unsupported args |
| `lineinfile` | in-process regexp/line edit + atomic write | non-local, `become`, check-mode, file-attr args (`mode`/`owner`/…), `validate` |
| `uri` | HTTP request via `ansible.module_utils.urls.open_url` | non-local, `become`, async, check-mode, `dest`/`src`, `form-multipart`, unsupported args |
| `slurp` | `base64`-encode a file read in-process | non-local, `become` |
| `fetch` | read source + atomic local write (no slurp/stat fork) | non-local, `become`, check-mode |
| `get_url` | download via `open_url` + checksum-based atomic write | non-local, `become`, async, check-mode, dir `dest`, checksum-URL, file-attr args |
| `command` | `subprocess.run` (no shell) | non-local, `become`, async, `environment` vars, check-mode |
| `shell` | `subprocess.run` (no Ansible module overhead) | non-local, `become`, async, `environment` vars, check-mode |
| `hashivault_read` | `hvac.Client` in-process | non-local |
| `find_next_helm_release_number` | `kubectl get secrets` via subprocess | non-local |

The `kubernetes.core` collection overrides (`k8s`, `k8s_info`, `helm_repository`) follow the same pattern via `kubectl`/`helm`, delegating to the genuine collection when non-local.

Equivalence to stock ansible-core is enforced by **output-parity tests**: for each deterministic plugin the integration suite runs the same task fast and again with `AFLP_DISABLE=1` (stock), then asserts the produced file (checksum + mode) and the shared result keys match.

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
make bench                                   # default: 100 iterations, all plugins
make bench BENCH_ARGS='-n 500 -p copy,stat'  # custom size / subset of plugins
```

Example output (100 iterations, median of 3; absolute times vary by host, the speedup is the stable figure). Both matrix pairs land around **11–12× faster** overall, with the file-writing plugins (`copy`/`template`) up around **15×**:

**Python 3.12 / ansible-core 2.19.3**

```
plugin          stock       fast    speedup
--------------------------------------------
stat         14.107s    1.305s      10.8x
copy         27.054s    1.910s      14.2x
template     27.138s    1.843s      14.7x
file         14.781s    1.323s      11.2x
command      14.372s    1.280s      11.2x
tempfile     13.985s    1.253s      11.2x
lineinfile   14.280s    1.361s      10.5x
slurp        13.898s    1.266s      11.0x
fetch        13.804s    1.739s       7.9x
get_url      19.019s    2.332s       8.2x
--------------------------------------------
TOTAL       172.439s   15.613s      11.0x
```

**Python 3.14 / ansible-core 2.20.5**

```
plugin          stock       fast    speedup
--------------------------------------------
stat         14.768s    1.355s      10.9x
copy         28.932s    1.838s      15.7x
template     29.824s    1.871s      15.9x
file         15.497s    1.374s      11.3x
command      15.206s    1.338s      11.4x
tempfile     14.731s    1.308s      11.3x
lineinfile   15.843s    1.329s      11.9x
slurp        14.845s    1.361s      10.9x
fetch        14.610s    1.723s       8.5x
get_url      19.729s    2.265s       8.7x
--------------------------------------------
TOTAL       183.987s   15.762s      11.7x
```

> Each pair uses its own controller image — the default is the first `MATRIX` entry (`3.14:2.20.5:5.6.0`); select another with `PAIR`, e.g. `make bench PAIR=3.12:2.19.3:5.4.0`. Because the controller image builds to a single shared tag, switch pairs with `make build MATRIX='<pair>'` first (otherwise `docker compose run` reuses the cached image regardless of the build args).

The reported time subtracts a zero-iteration overhead run (ansible startup + per-run setup) from the measured run, so it isolates the plugin's own per-invocation cost; the timed run is repeated and the median is reported. Each run also asserts the fast-path marker (`__produced_by_fast_plugin`) is present in fast mode and absent in stock mode, so the harness fails loudly rather than silently comparing the wrong code paths. Run `python bench/benchmark.py -h` for all flags (`-n/--iterations`, `-r/--repeat`, `-w/--warmup`, `-p/--plugins`).

## Test matrix

Tests run against multiple Python × ansible-core pairs. The matrix is defined at the top of `Makefile`:

```makefile
MATRIX := 3.14:2.20.5:5.6.0  3.12:2.19.3:5.4.0
#          ^Python  ^ansible-core  ^ansible-modules-hashivault
```

The third column is the `ansible-modules-hashivault` version, which is version-locked to `ansible-core` because it provides the fallback module for `hashivault_read`.

Run a single pair:

```bash
make test MATRIX='3.12:2.19.3:5.4.0'
```

Drop into a shell inside a specific controller image (the default pair is the
first `MATRIX` entry; pass `PAIR` to pick another):

```bash
make shell PAIR=3.12:2.19.3:5.4.0
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
      lineinfile.py
      uri.py
      slurp.py
      fetch.py
      get_url.py
      command.py
      shell.py
      hashivault_read.py
      find_next_helm_release_number.py
    callback_plugins/     # aflp_builtin_redirect (ansible.builtin.* -> fast routing)
                          # aflp_fast_path_summary (per-action fast/fallback tally)
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
