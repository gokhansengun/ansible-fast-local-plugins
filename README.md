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
collections_paths = ./ansible/collections
```

> **Note:** `action_plugins` accepts a colon-separated list of directories, so you can append this project's path to an existing value rather than replacing it:
> ```ini
> action_plugins = ./my/existing/plugins:./ansible/plugins/action_plugins
> ```

## Plugins

| Plugin | Fast-path behaviour | Fallback trigger |
|---|---|---|
| `stat` | `os.stat()` in-process | non-local, `become` |
| `tempfile` | `tempfile.mkstemp/mkdtemp` in-process | non-local, `become`, check-mode |
| `copy` | atomic write via `shutil.move` | non-local, `src`-based copy, `become`, unsupported args |
| `template` | Jinja2 rendering via `ansible.template.Templar` | non-local, `become`, unsupported args |
| `shell` | `subprocess.run` (no Ansible module overhead) | non-local, `become`, async, `environment` vars, check-mode |
| `hashivault_read` | `hvac.Client` in-process | non-local |
| `find_next_helm_release_number` | `kubectl get secrets` via subprocess | non-local |

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
      _action_utils.py    # Shared helpers: _is_local(), atomic_write(), _parse_mode()
      stat.py
      tempfile.py
      copy.py
      template.py
      shell.py
      hashivault_read.py
      find_next_helm_release_number.py
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

docker/
  Dockerfile              # Controller image (ARG: PYTHON_VERSION, ANSIBLE_CORE_VERSION, HASHIVAULT_MODULE_VERSION)
  docker-compose.yml      # All services (controller, vault, kind/dind, ssh-target)
  ssh-target/             # openssh-server container for fallback tests
  kind-setup/             # Bootstraps a kind cluster inside DinD; seeds Helm secrets

ansible.cfg               # Sets action_plugins path, disables host_key_checking
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
