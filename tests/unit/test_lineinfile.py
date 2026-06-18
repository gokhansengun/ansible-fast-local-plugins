from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import FAST_PLUGIN_MARKER, make_action


def _run(path, args):
    args = dict(args)
    args.setdefault('path', str(path))
    action = make_action('lineinfile', args)
    return action.run(task_vars={})


class TestLineinfilePresent:
    def test_append_to_existing_file(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('alpha\nbeta\n')
        result = _run(f, {'line': 'gamma'})
        assert result['changed'] is True
        assert result['msg'] == 'line added'
        assert f.read_text() == 'alpha\nbeta\ngamma\n'

    def test_append_is_idempotent(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('alpha\nbeta\n')
        _run(f, {'line': 'gamma'})
        result = _run(f, {'line': 'gamma'})
        assert result['changed'] is False
        assert f.read_text() == 'alpha\nbeta\ngamma\n'

    def test_regexp_replaces_matching_line(self, tmp_path):
        f = tmp_path / 'cfg'
        f.write_text('SELINUX=permissive\nOTHER=1\n')
        result = _run(f, {'regexp': '^SELINUX=', 'line': 'SELINUX=enforcing'})
        assert result['changed'] is True
        assert result['msg'] == 'line replaced'
        assert f.read_text() == 'SELINUX=enforcing\nOTHER=1\n'

    def test_regexp_replace_is_idempotent(self, tmp_path):
        f = tmp_path / 'cfg'
        f.write_text('SELINUX=permissive\n')
        _run(f, {'regexp': '^SELINUX=', 'line': 'SELINUX=enforcing'})
        result = _run(f, {'regexp': '^SELINUX=', 'line': 'SELINUX=enforcing'})
        assert result['changed'] is False
        assert f.read_text() == 'SELINUX=enforcing\n'

    def test_regexp_no_match_appends(self, tmp_path):
        f = tmp_path / 'cfg'
        f.write_text('OTHER=1\n')
        result = _run(f, {'regexp': '^SELINUX=', 'line': 'SELINUX=enforcing'})
        assert result['changed'] is True
        assert f.read_text() == 'OTHER=1\nSELINUX=enforcing\n'

    def test_backrefs_expands_groups(self, tmp_path):
        f = tmp_path / 'cfg'
        f.write_text('host=oldname\n')
        result = _run(f, {'regexp': r'^(host=).*', 'line': r'\g<1>newname', 'backrefs': True})
        assert result['changed'] is True
        assert f.read_text() == 'host=newname\n'

    def test_backrefs_no_match_leaves_unchanged(self, tmp_path):
        f = tmp_path / 'cfg'
        f.write_text('nothing=here\n')
        result = _run(f, {'regexp': r'^(host=).*', 'line': r'\g<1>newname', 'backrefs': True})
        assert result['changed'] is False
        assert f.read_text() == 'nothing=here\n'

    def test_insertafter_regex(self, tmp_path):
        f = tmp_path / 'httpd.conf'
        f.write_text('#Listen 80\nServerName x\n')
        result = _run(f, {'insertafter': '^#Listen ', 'line': 'Listen 8080'})
        assert result['changed'] is True
        assert f.read_text() == '#Listen 80\nListen 8080\nServerName x\n'

    def test_insertbefore_regex(self, tmp_path):
        f = tmp_path / 'services'
        f.write_text('www 80/tcp\n')
        result = _run(f, {'insertbefore': '^www', 'line': '# http port'})
        assert result['changed'] is True
        assert f.read_text() == '# http port\nwww 80/tcp\n'

    def test_insertbefore_bof(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('second\n')
        result = _run(f, {'insertbefore': 'BOF', 'line': 'first'})
        assert result['changed'] is True
        assert f.read_text() == 'first\nsecond\n'

    def test_create_true_makes_file(self, tmp_path):
        f = tmp_path / 'new.txt'
        result = _run(f, {'line': 'hello', 'create': True})
        assert result['changed'] is True
        assert f.read_text() == 'hello\n'

    def test_missing_file_without_create_fails(self, tmp_path):
        result = _run(tmp_path / 'nope.txt', {'line': 'hello'})
        assert result.get('failed') is True
        assert result.get('rc') == 257

    def test_line_required_for_present(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('x\n')
        result = _run(f, {'regexp': '^x'})
        assert result.get('failed') is True
        assert 'line is required' in result['msg']

    def test_backrefs_requires_regexp(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('x\n')
        result = _run(f, {'line': 'y', 'backrefs': True})
        assert result.get('failed') is True
        assert 'regexp is required' in result['msg']

    def test_path_is_directory_fails(self, tmp_path):
        result = _run(tmp_path, {'line': 'x'})
        assert result.get('failed') is True
        assert result.get('rc') == 256

    def test_no_trailing_newline_handled(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_bytes(b'alpha')  # no trailing newline
        result = _run(f, {'line': 'beta'})
        assert result['changed'] is True
        assert f.read_text() == 'alpha\nbeta\n'

    def test_backup_creates_copy(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('alpha\n')
        result = _run(f, {'line': 'beta', 'backup': True})
        assert result['changed'] is True
        assert result['backup']
        assert os.path.exists(result['backup'])
        with open(result['backup']) as fh:
            assert fh.read() == 'alpha\n'

    def test_value_alias_for_line(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('a\n')
        result = _run(f, {'value': 'b'})
        assert result['changed'] is True
        assert f.read_text() == 'a\nb\n'

    def test_fast_path_sets_marker(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('a\n')
        result = _run(f, {'line': 'b'})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_fast_path_failure_is_unmarked(self, tmp_path):
        result = _run(tmp_path / 'nope.txt', {'line': 'x'})  # missing file, no create
        assert result.get('failed') is True
        assert FAST_PLUGIN_MARKER not in result


class TestLineinfileAbsent:
    def test_remove_by_regexp(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('keep\n%wheel ALL\nkeep2\n')
        result = _run(f, {'state': 'absent', 'regexp': '^%wheel'})
        assert result['changed'] is True
        assert result['found'] == 1
        assert f.read_text() == 'keep\nkeep2\n'

    def test_remove_by_exact_line(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('one\ntwo\none\n')
        result = _run(f, {'state': 'absent', 'line': 'one'})
        assert result['changed'] is True
        assert result['found'] == 2
        assert f.read_text() == 'two\n'

    def test_remove_no_match_is_unchanged(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('one\ntwo\n')
        result = _run(f, {'state': 'absent', 'regexp': '^three'})
        assert result['changed'] is False
        assert result['found'] == 0

    def test_absent_missing_file_is_noop(self, tmp_path):
        result = _run(tmp_path / 'nope.txt', {'state': 'absent', 'regexp': '^x'})
        assert result['changed'] is False
        assert result['msg'] == 'file not present'

    def test_absent_sets_marker(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('drop\n')
        result = _run(f, {'state': 'absent', 'line': 'drop'})
        assert result[FAST_PLUGIN_MARKER] is True


class TestLineinfileFallback:
    def test_delegates_for_non_local_connection(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('x\n')
        action = make_action('lineinfile', {'path': str(f), 'line': 'y'}, local=False)
        with patch.object(action, '_execute_module', return_value={'changed': True}) as mock_exec:
            result = action.run(task_vars={})
        mock_exec.assert_called_once()
        assert result == {'changed': True}
        assert FAST_PLUGIN_MARKER not in result

    def test_delegates_when_become_is_set(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('x\n')
        action = make_action('lineinfile', {'path': str(f), 'line': 'y'}, become=True)
        with patch.object(action, '_execute_module', return_value={'changed': True}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()

    def test_delegates_for_unsupported_args(self, tmp_path):
        """mode/owner/validate must go to the standard module, not be dropped."""
        f = tmp_path / 'f.txt'
        f.write_text('x\n')
        action = make_action('lineinfile', {'path': str(f), 'line': 'y', 'mode': '0644'})
        with patch.object(action, '_execute_module', return_value={'changed': True}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()

    def test_delegates_in_check_mode(self, tmp_path):
        f = tmp_path / 'f.txt'
        f.write_text('x\n')
        action = make_action('lineinfile', {'path': str(f), 'line': 'y'}, check_mode=True)
        with patch.object(action, '_execute_module', return_value={'changed': True}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()
