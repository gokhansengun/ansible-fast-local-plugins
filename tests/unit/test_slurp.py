from __future__ import annotations

import base64
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import FAST_PLUGIN_MARKER, make_action


class TestSlurpFastPath:
    def test_reads_file_as_base64(self, tmp_path):
        f = tmp_path / 'data.bin'
        f.write_bytes(b'hello\x00world')
        action = make_action('slurp', {'src': str(f)})
        result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['encoding'] == 'base64'
        assert result['source'] == str(f)
        assert base64.b64decode(result['content']) == b'hello\x00world'

    def test_path_alias(self, tmp_path):
        f = tmp_path / 'data.txt'
        f.write_text('abc')
        action = make_action('slurp', {'path': str(f)})
        result = action.run(task_vars={})
        assert base64.b64decode(result['content']) == b'abc'

    def test_missing_file_fails(self, tmp_path):
        action = make_action('slurp', {'src': str(tmp_path / 'nope')})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'File not found' in result['msg']

    def test_directory_fails(self, tmp_path):
        action = make_action('slurp', {'src': str(tmp_path)})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'is a directory' in result['msg']

    def test_missing_src_fails(self):
        action = make_action('slurp', {})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'src is required' in result['msg']

    def test_sets_marker(self, tmp_path):
        f = tmp_path / 'data.txt'
        f.write_text('x')
        action = make_action('slurp', {'src': str(f)})
        result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_failure_is_unmarked(self, tmp_path):
        action = make_action('slurp', {'src': str(tmp_path / 'nope')})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert FAST_PLUGIN_MARKER not in result


class TestSlurpFallback:
    def test_delegates_for_non_local_connection(self, tmp_path):
        f = tmp_path / 'x'
        f.write_text('x')
        action = make_action('slurp', {'src': str(f)}, local=False)
        with patch.object(action, '_execute_module', return_value={'content': 'x'}) as mock_exec:
            result = action.run(task_vars={})
        mock_exec.assert_called_once()
        assert FAST_PLUGIN_MARKER not in result

    def test_delegates_when_become_is_set(self, tmp_path):
        f = tmp_path / 'x'
        f.write_text('x')
        action = make_action('slurp', {'src': str(f)}, become=True)
        with patch.object(action, '_execute_module', return_value={'content': 'x'}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()
