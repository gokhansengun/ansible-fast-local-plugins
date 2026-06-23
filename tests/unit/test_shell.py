from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import FAST_PLUGIN_MARKER, make_action, mock_builtin_run


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

    def test_failed_command_is_still_marked(self):
        # A non-zero command rc is the command's outcome, not a fallback. The
        # result must carry the marker so a `failed_when: false` probe (e.g.
        # `kubectl get cm` that returns non-zero) isn't misreported as a fallback.
        action = make_action('shell', {'_raw_params': 'exit 7'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert result['rc'] == 7
        assert result[FAST_PLUGIN_MARKER] is True

    def test_stderr_captured(self):
        action = make_action('shell', {'_raw_params': 'echo err >&2'})
        result = action.run(task_vars={})
        assert result['stderr'] == 'err'

    def test_chdir(self, tmp_path):
        action = make_action('shell', {'_raw_params': 'pwd', 'chdir': str(tmp_path)})
        result = action.run(task_vars={})
        # resolve symlinks for macOS /private/tmp vs /tmp
        assert os.path.realpath(result['stdout']) == os.path.realpath(str(tmp_path))

    def test_chdir_via_args_dict(self, tmp_path):
        # `shell: <cmd>` + `args: {chdir: ...}` merges chdir into task.args, the
        # common form (e.g. helm tasks). It must run on the fast path in chdir.
        action = make_action('shell', {'_raw_params': 'pwd', 'chdir': str(tmp_path)})
        result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result[FAST_PLUGIN_MARKER] is True
        assert os.path.realpath(result['stdout']) == os.path.realpath(str(tmp_path))

    def test_chdir_expands_user_and_env(self, tmp_path, monkeypatch):
        # ansible declares chdir as a path arg, so ~ and $VARS are expanded.
        monkeypatch.setenv('AFLP_CHDIR', str(tmp_path))
        action = make_action('shell', {'_raw_params': 'pwd', 'chdir': '$AFLP_CHDIR'})
        result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert os.path.realpath(result['stdout']) == os.path.realpath(str(tmp_path))

    def test_chdir_missing_directory_fails(self, tmp_path):
        missing = str(tmp_path / 'does-not-exist')
        action = make_action('shell', {'_raw_params': 'pwd', 'chdir': missing})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'change directory' in result['msg']
        # a fast-path failure is not marked
        assert FAST_PLUGIN_MARKER not in result

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

    def test_fast_path_sets_marker(self):
        action = make_action('shell', {'_raw_params': 'echo hi'})
        result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_fast_path_failure_is_unmarked(self):
        action = make_action('shell', {})  # no command -> failure
        result = action.run(task_vars={})
        assert result.get('failed') is True
        assert FAST_PLUGIN_MARKER not in result


class TestShellFallback:
    def test_delegates_for_non_local(self):
        action = make_action('shell', {'_raw_params': 'echo x'}, local=False)
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

    def test_fallback_result_is_unmarked(self):
        action = make_action('shell', {'_raw_params': 'echo x'}, local=False)
        with mock_builtin_run('shell', {'rc': 0}):
            result = action.run(task_vars={})
        assert FAST_PLUGIN_MARKER not in result


class TestShellEnvironment:
    """`environment:` is now applied in-process (no fallback to stock)."""

    def test_environment_var_visible_to_command(self):
        action = make_action('shell', {'_raw_params': 'echo "$HELM_PASSWORD"'},
                             environment=[{'HELM_PASSWORD': 's3cr3t'}])
        result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['stdout'] == 's3cr3t'
        # ran on the fast path, not via fallback
        assert result[FAST_PLUGIN_MARKER] is True

    def test_environment_layers_later_wins(self):
        action = make_action('shell', {'_raw_params': 'echo "$X"'},
                             environment=[{'X': 'first'}, {'X': 'second'}])
        result = action.run(task_vars={})
        assert result['stdout'] == 'second'

    def test_environment_added_on_top_of_process_env(self, monkeypatch):
        # Vars not named in environment: are still inherited from the process.
        monkeypatch.setenv('AFLP_INHERITED', 'inherited-value')
        action = make_action('shell', {'_raw_params': 'echo "$AFLP_INHERITED:$EXTRA"'},
                             environment=[{'EXTRA': 'added'}])
        result = action.run(task_vars={})
        assert result['stdout'] == 'inherited-value:added'

    def test_empty_environment_default_is_ignored(self):
        # ansible-core sets environment=[{}] by default; that must stay fast.
        action = make_action('shell', {'_raw_params': 'echo hi'}, environment=[{}])
        result = action.run(task_vars={})
        assert result['stdout'] == 'hi'
        assert result[FAST_PLUGIN_MARKER] is True

    def test_environment_does_not_fall_back_under_strict(self, monkeypatch):
        # Regression: environment used to force a fallback, which AFLP_STRICT then
        # turned into a hard failure. It must now run on the fast path instead.
        monkeypatch.setenv('AFLP_STRICT', '1')
        action = make_action('shell', {'_raw_params': 'echo "$X"'},
                             environment=[{'X': 'ok'}])
        result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['stdout'] == 'ok'
        assert result[FAST_PLUGIN_MARKER] is True
