from __future__ import annotations

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import make_action, mock_builtin_run


class TestCopyFastPath:
    def test_writes_content_to_dest(self, tmp_path):
        dest = str(tmp_path / 'out.txt')
        action = make_action('copy', {'dest': dest, 'content': 'hello world'})
        result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True
        assert open(dest).read() == 'hello world'

    def test_idempotent_same_content(self, tmp_path):
        dest = tmp_path / 'out.txt'
        dest.write_text('hello world')
        action = make_action('copy', {'dest': str(dest), 'content': 'hello world'})
        result = action.run(task_vars={})
        assert result['changed'] is False

    def test_changed_on_different_content(self, tmp_path):
        dest = tmp_path / 'out.txt'
        dest.write_text('old content')
        action = make_action('copy', {'dest': str(dest), 'content': 'new content'})
        result = action.run(task_vars={})
        assert result['changed'] is True
        assert dest.read_text() == 'new content'

    def test_force_false_does_not_overwrite(self, tmp_path):
        dest = tmp_path / 'out.txt'
        dest.write_text('original')
        action = make_action('copy', {'dest': str(dest), 'content': 'new', 'force': False})
        result = action.run(task_vars={})
        assert result['changed'] is False
        assert dest.read_text() == 'original'

    def test_sets_file_mode(self, tmp_path):
        dest = str(tmp_path / 'out.txt')
        action = make_action('copy', {'dest': dest, 'content': 'data', 'mode': '0600'})
        result = action.run(task_vars={})
        assert not result.get('failed')
        import stat
        mode = stat.S_IMODE(os.stat(dest).st_mode)
        assert mode == 0o600

    def test_returns_checksum(self, tmp_path):
        dest = str(tmp_path / 'out.txt')
        action = make_action('copy', {'dest': dest, 'content': 'abc'})
        result = action.run(task_vars={})
        expected = hashlib.sha1(b'abc', usedforsecurity=False).hexdigest()
        assert result['checksum'] == expected

    def test_missing_dest_returns_failed(self):
        action = make_action('copy', {'content': 'hello'})
        result = action.run(task_vars={})
        assert result.get('failed') is True


class TestCopyFallback:
    def test_delegates_for_non_local(self, tmp_path):
        dest = str(tmp_path / 'out.txt')
        action = make_action('copy', {'dest': dest, 'content': 'x'}, local=False)
        with mock_builtin_run('copy', {'changed': True}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_for_src_based_copy(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_text('x')
        dest = str(tmp_path / 'dst.txt')
        action = make_action('copy', {'src': str(src), 'dest': dest})
        with mock_builtin_run('copy', {'changed': True}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_for_unsupported_arg(self, tmp_path):
        dest = str(tmp_path / 'out.txt')
        action = make_action('copy', {'dest': dest, 'content': 'x', 'backup': True})
        with mock_builtin_run('copy', {'changed': True}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_when_become(self, tmp_path):
        dest = str(tmp_path / 'out.txt')
        action = make_action('copy', {'dest': dest, 'content': 'x'}, become=True)
        with mock_builtin_run('copy', {'changed': True}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()
