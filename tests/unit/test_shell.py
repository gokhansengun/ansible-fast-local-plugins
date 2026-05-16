from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/action-plugins')
))

from tests.conftest import make_action, mock_builtin_run


class TestShellFastPath:
    def test_basic_command(self):
        action = make_action('shell', {'_raw_params': 'echo hello'})
        result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['stdout'] == 'hello'
        assert result['rc'] == 0

    def test_cmd_key(self):
        action = make_action('shell', {'cmd': 'echo world'})
        result = action.run(task_vars={})
        assert result['stdout'] == 'world'

    def test_failed_command(self):
        action = make_action('shell', {'_raw_params': 'exit 1'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert result['rc'] == 1

    def test_stderr_captured(self):
        action = make_action('shell', {'_raw_params': 'echo err >&2'})
        result = action.run(task_vars={})
        assert result['stderr'] == 'err'

    def test_chdir(self, tmp_path):
        action = make_action('shell', {'_raw_params': 'pwd', 'chdir': str(tmp_path)})
        result = action.run(task_vars={})
        # resolve symlinks for macOS /private/tmp vs /tmp
        assert os.path.realpath(result['stdout']) == os.path.realpath(str(tmp_path))

    def test_creates_skips_when_file_exists(self, tmp_path):
        existing = tmp_path / 'existing.txt'
        existing.write_text('x')
        action = make_action('shell', {'_raw_params': 'echo run', 'creates': str(existing)})
        result = action.run(task_vars={})
        assert result['skipped'] is True
        assert result['rc'] == 0

    def test_removes_skips_when_file_absent(self, tmp_path):
        action = make_action('shell', {'_raw_params': 'echo run',
                                       'removes': str(tmp_path / 'absent.txt')})
        result = action.run(task_vars={})
        assert result['skipped'] is True

    def test_stdin_passed_to_command(self):
        action = make_action('shell', {'_raw_params': 'cat', 'stdin': 'hello from stdin'})
        result = action.run(task_vars={})
        assert 'hello from stdin' in result['stdout']

    def test_no_command_returns_failed(self):
        action = make_action('shell', {})
        result = action.run(task_vars={})
        assert result.get('failed') is True

    def test_result_has_timing_fields(self):
        action = make_action('shell', {'_raw_params': 'true'})
        result = action.run(task_vars={})
        assert 'start' in result
        assert 'end' in result
        assert 'delta' in result

    def test_strip_empty_ends_default(self):
        action = make_action('shell', {'_raw_params': 'printf "hello\n\n"'})
        result = action.run(task_vars={})
        assert result['stdout'] == 'hello'

    def test_strip_empty_ends_disabled(self):
        action = make_action('shell', {'_raw_params': 'printf "hello\n\n"', 'strip_empty_ends': False})
        result = action.run(task_vars={})
        assert result['stdout'].endswith('\n')


class TestShellFallback:
    def test_delegates_for_non_local(self):
        action = make_action('shell', {'_raw_params': 'echo x'}, local=False)
        with mock_builtin_run('shell', {'rc': 0}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_with_environment(self):
        action = make_action('shell', {'_raw_params': 'echo $X'},
                             environment=[{'X': '1'}])
        with mock_builtin_run('shell', {'rc': 0}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_in_check_mode(self):
        action = make_action('shell', {'_raw_params': 'echo x'}, check_mode=True)
        with mock_builtin_run('shell', {'rc': 0}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_for_async(self):
        action = make_action('shell', {'_raw_params': 'echo x'}, async_val=60)
        with mock_builtin_run('shell', {'rc': 0}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()
