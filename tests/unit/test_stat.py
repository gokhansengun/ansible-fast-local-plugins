from __future__ import annotations

import hashlib
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import FAST_PLUGIN_MARKER, make_action


class TestStatFastPath:
    def test_existing_regular_file(self, tmp_path):
        f = tmp_path / 'hello.txt'
        f.write_text('hello world')
        action = make_action('stat', {'path': str(f)})
        result = action.run(task_vars={})
        assert not result.get('failed')
        s = result['stat']
        assert s['exists'] is True
        assert s['isreg'] is True
        assert s['isdir'] is False
        assert s['size'] == 11

    def test_existing_file_has_checksum(self, tmp_path):
        f = tmp_path / 'data.txt'
        f.write_bytes(b'abc')
        action = make_action('stat', {'path': str(f), 'get_checksum': True, 'checksum_algorithm': 'sha256'})
        result = action.run(task_vars={})
        assert 'checksum' in result['stat']
        assert len(result['stat']['checksum']) == 64  # sha256 hex

    def test_missing_file(self, tmp_path):
        action = make_action('stat', {'path': str(tmp_path / 'missing.txt')})
        result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['stat']['exists'] is False

    def test_directory(self, tmp_path):
        action = make_action('stat', {'path': str(tmp_path)})
        result = action.run(task_vars={})
        s = result['stat']
        assert s['isdir'] is True
        assert s['isreg'] is False

    def test_symlink_follow_false(self, tmp_path):
        target = tmp_path / 'target.txt'
        target.write_text('content')
        link = tmp_path / 'link.txt'
        link.symlink_to(target)
        action = make_action('stat', {'path': str(link), 'follow': False})
        result = action.run(task_vars={})
        s = result['stat']
        assert s['islnk'] is True
        assert 'lnk_target' in s

    def test_symlink_follow_true(self, tmp_path):
        target = tmp_path / 'target.txt'
        target.write_text('content')
        link = tmp_path / 'link.txt'
        link.symlink_to(target)
        action = make_action('stat', {'path': str(link), 'follow': True})
        result = action.run(task_vars={})
        assert result['stat']['islnk'] is False
        assert result['stat']['isreg'] is True

    def test_checksum_alias_selects_algorithm(self, tmp_path):
        """`checksum: sha256` is a stock alias for checksum_algorithm; it must not
        silently fall back to sha1."""
        f = tmp_path / 'data.txt'
        f.write_bytes(b'content')
        action = make_action('stat', {'path': str(f), 'checksum': 'sha256'})
        result = action.run(task_vars={})
        assert result['stat']['checksum'] == hashlib.sha256(b'content').hexdigest()

    def test_path_aliases_accepted(self, tmp_path):
        f = tmp_path / 'data.txt'
        f.write_text('x')
        for alias in ('dest', 'name'):
            action = make_action('stat', {alias: str(f)})
            result = action.run(task_vars={})
            assert result['stat']['exists'] is True, alias

    def test_missing_path_arg_returns_failed(self):
        action = make_action('stat', {})
        result = action.run(task_vars={})
        assert result.get('failed') is True

    def test_fast_path_sets_marker(self, tmp_path):
        f = tmp_path / 'hello.txt'
        f.write_text('hi')
        action = make_action('stat', {'path': str(f)})
        result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_missing_file_result_is_marked(self, tmp_path):
        action = make_action('stat', {'path': str(tmp_path / 'missing.txt')})
        result = action.run(task_vars={})
        assert result['stat']['exists'] is False
        assert result[FAST_PLUGIN_MARKER] is True

    def test_fast_path_failure_is_unmarked(self):
        action = make_action('stat', {})  # no path -> failure
        result = action.run(task_vars={})
        assert result.get('failed') is True
        assert FAST_PLUGIN_MARKER not in result

    def test_permission_bits_present(self, tmp_path):
        f = tmp_path / 'bits.txt'
        f.write_text('x')
        os.chmod(str(f), 0o644)
        action = make_action('stat', {'path': str(f)})
        result = action.run(task_vars={})
        s = result['stat']
        assert s['rusr'] is True
        assert s['wusr'] is True
        assert s['xusr'] is False
        assert s['rgrp'] is True
        assert s['wgrp'] is False

    def test_pw_name_and_gr_name_present(self, tmp_path):
        f = tmp_path / 'owner.txt'
        f.write_text('x')
        action = make_action('stat', {'path': str(f)})
        result = action.run(task_vars={})
        s = result['stat']
        assert isinstance(s['pw_name'], str)
        assert isinstance(s['gr_name'], str)


class TestStatFallback:
    def test_delegates_for_unsupported_args(self, tmp_path):
        """get_attributes needs lsattr; the fast path must delegate, not silently
        return its hardcoded empty attributes."""
        action = make_action('stat', {'path': str(tmp_path), 'get_attributes': True})
        with patch.object(action, '_execute_module', return_value={'stat': {}}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()

    def test_delegates_for_non_local_connection(self, tmp_path):
        f = tmp_path / 'x.txt'
        f.write_text('x')
        action = make_action('stat', {'path': str(f)}, local=False)
        with patch.object(action, '_execute_module', return_value={'stat': {'exists': True}}) as mock_exec:
            result = action.run(task_vars={})
        mock_exec.assert_called_once()
        assert result == {'stat': {'exists': True}}

    def test_delegates_when_become_is_set(self, tmp_path):
        f = tmp_path / 'x.txt'
        f.write_text('x')
        action = make_action('stat', {'path': str(f)}, become=True)
        with patch.object(action, '_execute_module', return_value={'stat': {'exists': True}}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()

    def test_fallback_result_is_unmarked(self, tmp_path):
        f = tmp_path / 'x.txt'
        f.write_text('x')
        action = make_action('stat', {'path': str(f)}, local=False)
        with patch.object(action, '_execute_module', return_value={'stat': {'exists': True}}):
            result = action.run(task_vars={})
        assert FAST_PLUGIN_MARKER not in result
