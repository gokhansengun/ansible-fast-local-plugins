"""
Integration test runner.

Each test invokes ansible-playbook against the appropriate inventory
and asserts a zero exit code. All tests are marked `integration` so
they only run inside the Docker Compose stack (make test-int / make test).
"""
from __future__ import annotations

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
def test_k8s_info():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_k8s_info.yml')))


@pytest.mark.integration
def test_k8s_info_fallback_no_recursion():
    """Replicates 'Task failed: maximum recursion depth exceeded'.

    The fallback gate in the overriding kubernetes.core.k8s_info action plugin
    imports its own module (the collection shadows kubernetes.core), so any
    become/non-local task delegates to itself until the recursion limit.
    """
    result = _run(os.path.join(PLAYBOOK_DIR, 'test_k8s_info_fallback_recursion.yml'))
    output = result.stdout + result.stderr
    assert 'maximum recursion depth exceeded' not in output, (
        f'k8s_info fallback delegated to itself\n'
        f'--- stdout ---\n{result.stdout}\n'
        f'--- stderr ---\n{result.stderr}'
    )
    _assert_playbook(result)


@pytest.mark.integration
def test_k8s():
    _assert_playbook(_run(os.path.join(PLAYBOOK_DIR, 'test_k8s.yml')))


@pytest.mark.integration
def test_fallback_ssh():
    _assert_playbook(_run(
        os.path.join(PLAYBOOK_DIR, 'test_fallback_ssh.yml'),
        inventory=INV_SSH,
    ))
