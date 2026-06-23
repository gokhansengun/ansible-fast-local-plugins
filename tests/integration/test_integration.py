"""
Integration test runner.

Each test invokes ansible-playbook against the appropriate inventory
and asserts a zero exit code. All tests are marked `integration` so
they only run inside the Docker Compose stack (make test-int / make test).
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

PLAYBOOK_DIR = os.path.join(os.path.dirname(__file__), 'playbooks')
INV_LOCAL = os.path.join(os.path.dirname(__file__), 'inventory', 'local.ini')
INV_SSH = os.path.join(os.path.dirname(__file__), 'inventory', 'ssh.ini')


def _run(playbook_file, inventory=None, extra_vars=None, env=None):
    inv = inventory or INV_LOCAL
    cmd = ['ansible-playbook', '-i', inv, playbook_file]
    for key, val in (extra_vars or {}).items():
        cmd += ['-e', f'{key}={val}']
    run_env = {**os.environ, **env} if env else None
    result = subprocess.run(cmd, capture_output=True, text=True, cwd='/workspace', env=run_env)
    return result


def _assert_playbook(result):
    if result.returncode != 0:
        pytest.fail(
            f'ansible-playbook failed (rc={result.returncode})\n'
            f'--- stdout ---\n{result.stdout}\n'
            f'--- stderr ---\n{result.stderr}'
        )


@pytest.mark.integration
def test_file():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_file.yml')))


@pytest.mark.integration
def test_stat():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_stat.yml')))


@pytest.mark.integration
def test_tempfile():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_tempfile.yml')))


@pytest.mark.integration
def test_copy():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_copy.yml')))


@pytest.mark.integration
def test_command():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_command.yml')))


@pytest.mark.integration
def test_shell():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_shell.yml')))


@pytest.mark.integration
def test_template():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_template.yml')))


@pytest.mark.integration
def test_lineinfile():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_lineinfile.yml')))


@pytest.mark.integration
def test_uri():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_uri.yml')))


@pytest.mark.integration
def test_slurp():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_slurp.yml')))


@pytest.mark.integration
def test_fetch():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_fetch.yml')))


@pytest.mark.integration
def test_get_url():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_get_url.yml')))


@pytest.mark.integration
def test_kill_switch():
    """AFLP_DISABLE=1 must force every fast plugin to the stock path.

    The playbook asserts results are correct but carry no fast-path marker; the
    summary callback (enabled globally) should therefore report only fallbacks.
    """
    result = _run(os.path.join(PLAYBOOK_DIR, 'test_kill_switch.yml'),
                  env={'AFLP_DISABLE': '1'})
    _assert_playbook(result)
    assert 'AFLP FAST-PATH SUMMARY' in result.stdout
    import re
    for action in ('copy', 'stat', 'lineinfile'):
        m = re.search(rf'^\s*{action}\s+fast=(\d+)\s+fallback=(\d+)', result.stdout, re.M)
        assert m and int(m.group(1)) == 0 and int(m.group(2)) >= 1, (
            f'{action} should be all-fallback under AFLP_DISABLE\n{result.stdout}')


@pytest.mark.integration
def test_strict_mode_passes_all_fast():
    """AFLP_STRICT=1 must not interfere with a play whose tasks all run fast."""
    result = _run(os.path.join(PLAYBOOK_DIR, 'test_strict_mode.yml'),
                  env={'AFLP_STRICT': '1'})
    _assert_playbook(result)


@pytest.mark.integration
def test_strict_mode_fails_on_fallback():
    """AFLP_STRICT=1 must fail the play when a task falls back to stock.

    test_fast_path_summary.yml contains a lineinfile task with a file-attr arg
    that the fast plugin delegates; under strict mode that must raise.
    """
    result = _run(os.path.join(PLAYBOOK_DIR, 'test_fast_path_summary.yml'),
                  env={'AFLP_STRICT': '1'})
    assert result.returncode != 0, (
        f'strict mode should have failed on the fallback task\n{result.stdout}')
    assert 'AFLP_STRICT' in (result.stdout + result.stderr), result.stdout


@pytest.mark.integration
def test_environment_fast_path_under_strict():
    """The helm-repo shell task: a task whose only previously-unsupported feature
    is `environment:` now runs in-process. Even with AFLP_STRICT=1 it must NOT
    fall back (and so must NOT fail) — the env var is honored on the fast path."""
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_strict_environment.yml'),
                          env={'AFLP_STRICT': '1'}))


@pytest.mark.integration
def test_fast_path_summary():
    """The aflp_fast_path_summary callback must print a per-action tally.

    The playbook runs several fast tasks plus one lineinfile forced down the
    fallback (mode arg); the summary must show the fallback in the lineinfile row.
    """
    import re

    result = _run(os.path.join(PLAYBOOK_DIR, 'test_fast_path_summary.yml'))
    _assert_playbook(result)
    out = result.stdout
    assert 'AFLP FAST-PATH SUMMARY' in out, f'summary banner missing\n{out}'

    def _counts(action):
        m = re.search(rf'^\s*{action}\s+fast=(\d+)\s+fallback=(\d+)', out, re.M)
        assert m, f'no summary row for {action}\n{out}'
        return int(m.group(1)), int(m.group(2))

    copy_fast, copy_fb = _counts('copy')
    line_fast, line_fb = _counts('lineinfile')
    assert copy_fast >= 1 and copy_fb == 0, out
    # one lineinfile ran fast (plain) and one fell back (mode arg)
    assert line_fast >= 1 and line_fb >= 1, out


@pytest.mark.integration
def test_builtin_fqcn_redirect():
    """ansible.builtin.<name> must route to the fast local action plugins.

    The aflp_builtin_redirect callback rewrites overridden ansible.builtin.*
    names to ansible.legacy.* at loader resolution time; the playbook asserts
    the fast plugin (not stock builtin) actually handled the task.
    """
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_builtin_fqcn_redirect.yml')))


@pytest.mark.integration
def test_hashivault_read():
    vault_addr = os.environ.get('VAULT_ADDR', 'http://vault:8200')
    vault_token = os.environ.get('VAULT_TOKEN', 'root')
    result = _run(
        os.path.join(PLAYBOOK_DIR, 'test_hashivault_read.yml'),
        extra_vars={'vault_addr': vault_addr, 'vault_token': vault_token},
    )
    _assert_playbook(result)


@pytest.mark.integration
def test_find_next_helm_release_number():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_find_next_helm_release_number.yml')))


@pytest.mark.integration
def test_helm_repository():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_helm_repository.yml')))


@pytest.mark.integration
def test_helm_pull():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_helm_pull.yml')))


@pytest.mark.integration
def test_helm_info():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_helm_info.yml')))


@pytest.mark.integration
def test_kubernetes_fqcn_redirect():
    """The aflp_kubernetes_redirect callback routes kubernetes.core.<name> tasks to
    the fast aflp.kubernetes_core overrides without editing roles: a task written as
    kubernetes.core.k8s_info carries the fast-path marker (proving the rewrite fired
    and our plugin, not the genuine collection, handled it)."""
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_kubernetes_redirect.yml')))


@pytest.mark.integration
def test_k8s_info():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_k8s_info.yml')))


@pytest.mark.integration
def test_kubernetes_fallback_to_genuine():
    """With AFLP_DISABLE=1 the fast overrides delegate to the genuine kubernetes.core
    collection (a real installed dependency): the task still returns real cluster
    data, but unmarked — proving it ran the genuine plugin, not the fast path, and
    that delegation actually reaches the genuine collection rather than failing or
    recursing."""
    _assert_playbook(_run(
        os.path.join(PLAYBOOK_DIR, 'test_kubernetes_fallback_genuine.yml'),
        env={'AFLP_DISABLE': '1'},
    ))


@pytest.mark.integration
def test_k8s():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_k8s.yml')))


@pytest.mark.integration
def test_fallback_ssh():
    _assert_playbook(_run(
        os.path.join(PLAYBOOK_DIR, 'test_fallback_ssh.yml'),
        inventory=INV_SSH,
    ))


# ---------------------------------------------------------------------------
# Output-parity testing (#5)
#
# For each deterministic local plugin, run parity.yml twice — once stock
# (AFLP_DISABLE=1) and once fast — then assert two things:
#   1. side-effect parity: the produced file's checksum+mode are identical;
#   2. result parity: every result key present in BOTH dumps has an equal value.
# Result parity compares only the keys both paths emit (the fast plugins
# intentionally return a minimal subset), recursing into nested dicts/lists, and
# skips a denylist of volatile keys (timestamps, elapsed, msg text, ...).
# ---------------------------------------------------------------------------

PARITY_PLAYBOOK = os.path.join(PLAYBOOK_DIR, 'parity.yml')
PARITY_PLUGINS = [
    'copy', 'copy_src', 'copy_dir', 'copy_dir_slash', 'template', 'lineinfile',
    'stat', 'slurp', 'fetch', 'get_url', 'command', 'command_env', 'shell',
]
FAST_PLUGIN_MARKER = '__produced_by_fast_plugin'

# Keys whose values legitimately differ between two separate runs (or are just
# human-facing text), skipped at any depth of the result comparison.
_PARITY_DENYLIST = {
    FAST_PLUGIN_MARKER, 'invocation', 'warnings', 'deprecations',
    'elapsed', 'start', 'end', 'delta', 'diff', 'msg',
    'atime', 'mtime', 'ctime', 'inode', 'dev',
    # 'src': stock copy/template report an internal AnsiballZ tmp staging path
    # (e.g. /root/.ansible/tmp/.../.source); the fast plugins report the real
    # source. It is an implementation detail, not a behavioural contract.
    'src',
}


def _parity_diffs(stock, fast, path=''):
    """Recursively diff common keys of two result values. Returns a list of strings."""
    diffs = []
    if isinstance(stock, dict) and isinstance(fast, dict):
        for key in set(stock) & set(fast):
            if key in _PARITY_DENYLIST or key.startswith('_ansible_'):
                continue
            diffs += _parity_diffs(stock[key], fast[key], f'{path}.{key}')
    elif isinstance(stock, list) and isinstance(fast, list):
        if len(stock) != len(fast):
            diffs.append(f'{path}: list length stock={len(stock)} fast={len(fast)}')
        else:
            for i, (s, f) in enumerate(zip(stock, fast)):
                diffs += _parity_diffs(s, f, f'{path}[{i}]')
    elif stock != fast:
        diffs.append(f'{path or "<root>"}: stock={stock!r} fast={fast!r}')
    return diffs


def _run_parity_mode(plugin, tag, env):
    out = f'/tmp/aflp_parity_{plugin}_{tag}.json'
    work = f'/tmp/aflp_parity_{plugin}_work'
    result = _run(PARITY_PLAYBOOK,
                  extra_vars={'parity_plugin': plugin, 'result_out': out, 'work_dir': work},
                  env=env)
    _assert_playbook(result)
    with open(out) as fh:
        return json.load(fh)


@pytest.mark.integration
@pytest.mark.parametrize('plugin', PARITY_PLUGINS)
def test_parity(plugin):
    """Fast plugin output must match stock ansible-core for the same task."""
    stock = _run_parity_mode(plugin, 'stock', env={'AFLP_DISABLE': '1'})
    fast = _run_parity_mode(plugin, 'fast', env=None)

    # Validity: ensure we genuinely compared fast vs stock, not fast vs fast.
    assert fast['result'].get(FAST_PLUGIN_MARKER) is True, \
        f'{plugin}: fast run was not marked — did the fast path run?'
    assert FAST_PLUGIN_MARKER not in stock['result'], \
        f'{plugin}: stock run carried the fast marker — kill-switch did not take effect'

    # Side-effect parity (produced file checksum + mode), when applicable.
    assert fast['sidecar'] == stock['sidecar'], (
        f'{plugin} side-effect differs: fast={fast["sidecar"]} stock={stock["sidecar"]}')

    # Result parity over common keys.
    diffs = _parity_diffs(stock['result'], fast['result'])
    assert not diffs, f'{plugin} result parity diffs:\n' + '\n'.join(diffs)
