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


def _run(playbook_file, inventory=None, extra_vars=None):
    inv = inventory or INV_LOCAL
    cmd = ['ansible-playbook', '-i', inv, playbook_file]
    for key, val in (extra_vars or {}).items():
        cmd += ['-e', f'{key}={val}']
    result = subprocess.run(cmd, capture_output=True, text=True, cwd='/workspace')
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
