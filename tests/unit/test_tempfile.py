from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import make_action


class TestTempfileFastPath:
    def test_creates_temp_file(self, tmp_path):
        action = make_action('tempfile', {'state': 'file', 'path': str(tmp_path)})
        result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['state'] == 'file'
        assert result['changed'] is True
        assert os.path.isfile(result['path'])
        os.unlink(result['path'])

    def test_creates_temp_directory(self, tmp_path):
        action = make_action('tempfile', {'state': 'directory', 'path': str(tmp_path)})
        result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['state'] == 'directory'
        assert os.path.isdir(result['path'])
        os.rmdir(result['path'])

    def test_custom_prefix(self, tmp_path):
        action = make_action('tempfile', {'state': 'file', 'path': str(tmp_path), 'prefix': 'mytest_'})
        result = action.run(task_vars={})
        assert os.path.basename(result['path']).startswith('mytest_')
        os.unlink(result['path'])

    def test_custom_suffix(self, tmp_path):
        action = make_action('tempfile', {'state': 'file', 'path': str(tmp_path), 'suffix': '.tmp'})
        result = action.run(task_vars={})
        assert result['path'].endswith('.tmp')
        os.unlink(result['path'])

    def test_default_state_is_file(self, tmp_path):
        action = make_action('tempfile', {'path': str(tmp_path)})
        result = action.run(task_vars={})
        assert result['state'] == 'file'
        assert os.path.isfile(result['path'])
        os.unlink(result['path'])

    def test_invalid_path_returns_failed(self):
        action = make_action('tempfile', {'state': 'file', 'path': '/nonexistent/dir/that/cannot/exist'})
        result = action.run(task_vars={})
        assert result.get('failed') is True


class TestTempfileFallback:
    def test_delegates_for_non_local(self):
        action = make_action('tempfile', {'state': 'file'}, local=False)
        with patch.object(action, '_execute_module', return_value={'changed': True, 'path': '/tmp/x'}) as mock:
            result = action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_when_become(self):
        action = make_action('tempfile', {'state': 'file'}, become=True)
        with patch.object(action, '_execute_module', return_value={'changed': True, 'path': '/tmp/x'}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_in_check_mode(self):
        action = make_action('tempfile', {'state': 'file'}, check_mode=True)
        with patch.object(action, '_execute_module', return_value={'changed': False}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()
