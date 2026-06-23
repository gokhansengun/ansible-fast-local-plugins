from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import (
    COLLECTION_PLUGIN_DIRS,
    FAST_PLUGIN_MARKER,
    _load_plugin,
    make_action,
    mock_builtin_run,
)


@pytest.fixture
def disabled(monkeypatch):
    """Globally disable the fast path for the duration of a test."""
    monkeypatch.setenv('AFLP_DISABLE', '1')


# ---------------------------------------------------------------------------
# Module-backed plugins fall back via _execute_module when disabled.
# ---------------------------------------------------------------------------

class TestKillSwitchModuleFallback:
    def test_stat(self, disabled, tmp_path):
        f = tmp_path / 'x'
        f.write_text('x')
        action = make_action('stat', {'path': str(f)})  # local=True, but disabled
        with patch.object(action, '_execute_module', return_value={'stat': {}}) as m:
            result = action.run(task_vars={})
        m.assert_called_once()
        assert FAST_PLUGIN_MARKER not in result

    def test_slurp(self, disabled, tmp_path):
        f = tmp_path / 'x'
        f.write_text('x')
        action = make_action('slurp', {'src': str(f)})
        with patch.object(action, '_execute_module', return_value={'content': 'eA=='}) as m:
            result = action.run(task_vars={})
        m.assert_called_once()
        assert FAST_PLUGIN_MARKER not in result

    def test_lineinfile(self, disabled, tmp_path):
        f = tmp_path / 'x'
        f.write_text('a\n')
        action = make_action('lineinfile', {'path': str(f), 'line': 'b'})
        with patch.object(action, '_execute_module', return_value={'changed': True}) as m:
            result = action.run(task_vars={})
        m.assert_called_once()
        assert FAST_PLUGIN_MARKER not in result

    def test_uri(self, disabled):
        action = make_action('uri', {'url': 'http://h/'})
        with patch.object(action, '_execute_module', return_value={'status': 200}) as m:
            result = action.run(task_vars={})
        m.assert_called_once()
        assert FAST_PLUGIN_MARKER not in result

    def test_get_url(self, disabled, tmp_path):
        action = make_action('get_url', {'url': 'http://h/f', 'dest': str(tmp_path / 'd')})
        with patch.object(action, '_execute_module', return_value={'changed': True}) as m:
            result = action.run(task_vars={})
        m.assert_called_once()
        assert FAST_PLUGIN_MARKER not in result


# ---------------------------------------------------------------------------
# Action-plugin-backed plugins fall back via the standard plugin when disabled.
# ---------------------------------------------------------------------------

class TestKillSwitchActionFallback:
    def test_copy(self, disabled, tmp_path):
        action = make_action('copy', {'content': 'x', 'dest': str(tmp_path / 'o')})
        with mock_builtin_run('copy', {'changed': True}) as mock_run:
            result = action.run(task_vars={})
        mock_run.assert_called_once()
        assert FAST_PLUGIN_MARKER not in result

    def test_fetch(self, disabled, tmp_path):
        src = tmp_path / 'src'
        src.write_text('x')
        action = make_action('fetch', {'src': str(src), 'dest': str(tmp_path / 'o'), 'flat': True})
        with mock_builtin_run('fetch', {'changed': True}) as mock_run:
            result = action.run(task_vars={})
        mock_run.assert_called_once()
        assert FAST_PLUGIN_MARKER not in result


# ---------------------------------------------------------------------------
# The self-contained kubernetes.core overrides honour the switch too.
# ---------------------------------------------------------------------------

class TestKillSwitchK8sOverrides:
    @pytest.mark.parametrize('name', ['k8s', 'k8s_info', 'helm_repository', 'helm_pull', 'helm_info'])
    def test_is_local_false_when_disabled(self, disabled, name):
        mod = _load_plugin(name, plugin_dir=COLLECTION_PLUGIN_DIRS['aflp.kubernetes_core'])
        conn = MagicMock()
        conn.transport = 'local'
        assert mod._is_local(conn) is False

    @pytest.mark.parametrize('name', ['k8s', 'k8s_info', 'helm_repository', 'helm_pull', 'helm_info'])
    def test_is_local_true_when_enabled(self, monkeypatch, name):
        monkeypatch.delenv('AFLP_DISABLE', raising=False)
        mod = _load_plugin(name, plugin_dir=COLLECTION_PLUGIN_DIRS['aflp.kubernetes_core'])
        conn = MagicMock()
        conn.transport = 'local'
        assert mod._is_local(conn) is True


# ---------------------------------------------------------------------------
# Control: with the switch unset, the fast path still runs (and marks).
# ---------------------------------------------------------------------------

class TestKillSwitchControl:
    def test_fast_path_runs_when_not_disabled(self, monkeypatch, tmp_path):
        monkeypatch.delenv('AFLP_DISABLE', raising=False)
        f = tmp_path / 'x'
        f.write_text('hi')
        action = make_action('stat', {'path': str(f)})
        with patch.object(action, '_execute_module') as m:
            result = action.run(task_vars={})
        m.assert_not_called()
        assert result[FAST_PLUGIN_MARKER] is True
