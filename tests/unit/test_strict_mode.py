from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from ansible.errors import AnsibleActionFail

from tests.conftest import (
    COLLECTION_PLUGIN_DIRS,
    FAST_PLUGIN_MARKER,
    _load_plugin,
    make_action,
)


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setenv('AFLP_STRICT', '1')
    monkeypatch.delenv('AFLP_DISABLE', raising=False)


# ---------------------------------------------------------------------------
# In strict mode, any path that would fall back must raise instead.
# ---------------------------------------------------------------------------

class TestStrictRaisesOnFallback:
    def test_stat_become(self, strict, tmp_path):
        f = tmp_path / 'x'
        f.write_text('x')
        action = make_action('stat', {'path': str(f)}, become=True)  # become -> fallback
        with pytest.raises(AnsibleActionFail, match='AFLP_STRICT'):
            action.run(task_vars={})

    def test_uri_non_local(self, strict):
        action = make_action('uri', {'url': 'http://h/'}, local=False)
        with pytest.raises(AnsibleActionFail, match='non-local connection'):
            action.run(task_vars={})

    def test_copy_unsupported_arg(self, strict, tmp_path):
        # owner forces the standard copy plugin.
        action = make_action('copy', {'content': 'x', 'dest': str(tmp_path / 'o'), 'owner': 'root'})
        with pytest.raises(AnsibleActionFail, match='AFLP_STRICT'):
            action.run(task_vars={})

    def test_lineinfile_mode_arg(self, strict, tmp_path):
        f = tmp_path / 'f'
        f.write_text('a\n')
        action = make_action('lineinfile', {'path': str(f), 'line': 'b', 'mode': '0644'})
        with pytest.raises(AnsibleActionFail, match='AFLP_STRICT'):
            action.run(task_vars={})

    def test_get_url_check_mode(self, strict, tmp_path):
        action = make_action('get_url', {'url': 'http://h/f', 'dest': str(tmp_path / 'd')},
                             check_mode=True)
        with pytest.raises(AnsibleActionFail, match='AFLP_STRICT'):
            action.run(task_vars={})


# ---------------------------------------------------------------------------
# Strict mode does not interfere with tasks that take the fast path.
# ---------------------------------------------------------------------------

class TestStrictAllowsFastPath:
    def test_stat_fast_ok(self, strict, tmp_path):
        f = tmp_path / 'x'
        f.write_text('hi')
        action = make_action('stat', {'path': str(f)})
        result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_copy_fast_ok(self, strict, tmp_path):
        action = make_action('copy', {'content': 'x', 'dest': str(tmp_path / 'o')})
        result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True


class TestStrictSuppressedByDisable:
    def test_disable_wins(self, strict, monkeypatch, tmp_path):
        # With both set, AFLP_DISABLE (intentional fallback) wins: fall back, do not raise.
        monkeypatch.setenv('AFLP_DISABLE', '1')
        f = tmp_path / 'x'
        f.write_text('x')
        action = make_action('stat', {'path': str(f)})
        with patch.object(action, '_execute_module', return_value={'stat': {}}) as m:
            action.run(task_vars={})
        m.assert_called_once()


# ---------------------------------------------------------------------------
# The self-contained kubernetes.core overrides honour strict mode too.
# ---------------------------------------------------------------------------

class TestStrictK8sOverrides:
    @pytest.mark.parametrize('name', ['k8s', 'k8s_info', 'helm_repository', 'helm_pull', 'helm_info'])
    def test_strict_guard_raises_non_local(self, strict, name):
        mod = _load_plugin(name, plugin_dir=COLLECTION_PLUGIN_DIRS['aflp.kubernetes_core'])
        conn = MagicMock()
        conn.transport = 'ssh'
        conn._load_name = 'ssh'
        pc = MagicMock()
        pc.become = False
        with pytest.raises(AnsibleActionFail, match='AFLP_STRICT'):
            mod._strict_guard(conn, pc)

    @pytest.mark.parametrize('name', ['k8s', 'k8s_info', 'helm_repository', 'helm_pull', 'helm_info'])
    def test_strict_guard_suppressed_by_disable(self, strict, monkeypatch, name):
        monkeypatch.setenv('AFLP_DISABLE', '1')
        mod = _load_plugin(name, plugin_dir=COLLECTION_PLUGIN_DIRS['aflp.kubernetes_core'])
        conn = MagicMock()
        conn.transport = 'ssh'
        pc = MagicMock()
        pc.become = True
        mod._strict_guard(conn, pc)  # must not raise

    @pytest.mark.parametrize('name', ['k8s', 'k8s_info', 'helm_repository', 'helm_pull', 'helm_info'])
    def test_strict_guard_noop_when_unset(self, monkeypatch, name):
        monkeypatch.delenv('AFLP_STRICT', raising=False)
        mod = _load_plugin(name, plugin_dir=COLLECTION_PLUGIN_DIRS['aflp.kubernetes_core'])
        conn = MagicMock()
        conn.transport = 'ssh'
        pc = MagicMock()
        pc.become = True
        mod._strict_guard(conn, pc)  # must not raise
