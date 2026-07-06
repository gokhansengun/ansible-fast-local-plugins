from __future__ import annotations

import hashlib
import os
import stat as stat_module
import sys
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import FAST_PLUGIN_MARKER, make_action


class _FakeResp:
    def __init__(self, body=b'', status=200):
        self._body = body
        self._status = status

    def read(self):
        return self._body

    def getcode(self):
        return self._status


def _make(args, **kwargs):
    action = make_action('get_url', args, **kwargs)
    mod = sys.modules['_aflp_get_url']
    return action, mod


class TestGetUrlFastPath:
    def test_download_new_file(self, tmp_path):
        dest = tmp_path / 'out.bin'
        action, mod = _make({'url': 'http://h/file', 'dest': str(dest)})
        with patch.object(mod, 'open_url', return_value=_FakeResp(b'DATA')):
            result = action.run(task_vars={})
        assert result['changed'] is True
        assert result['status_code'] == 200
        assert result['checksum_src'] == hashlib.sha1(b'DATA').hexdigest()
        assert dest.read_bytes() == b'DATA'
        assert result[FAST_PLUGIN_MARKER] is True

    def test_username_password_aliases_stay_fast(self, tmp_path):
        """username/password are stock-argspec aliases; they must not trigger fallback."""
        dest = tmp_path / 'out.bin'
        action, mod = _make({'url': 'http://h/file', 'dest': str(dest),
                             'username': 'u', 'password': 'p'})
        m = MagicMock(return_value=_FakeResp(b'DATA'))
        with patch.object(mod, 'open_url', m):
            result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True
        kwargs = m.call_args.kwargs
        assert kwargs['url_username'] == 'u'
        assert kwargs['url_password'] == 'p'

    def test_download_is_idempotent(self, tmp_path):
        dest = tmp_path / 'out.bin'
        dest.write_bytes(b'DATA')
        action, mod = _make({'url': 'http://h/file', 'dest': str(dest)})
        with patch.object(mod, 'open_url', return_value=_FakeResp(b'DATA')):
            result = action.run(task_vars={})
        assert result['changed'] is False
        assert result['checksum_src'] == result['checksum_dest']

    def test_checksum_match_skips_download(self, tmp_path):
        dest = tmp_path / 'out.bin'
        dest.write_bytes(b'EXISTING')
        digest = hashlib.sha256(b'EXISTING').hexdigest()
        action, mod = _make({'url': 'http://h/file', 'dest': str(dest),
                             'checksum': 'sha256:' + digest})
        m = MagicMock()
        with patch.object(mod, 'open_url', m):
            result = action.run(task_vars={})
        m.assert_not_called()
        assert result['changed'] is False
        assert result['msg'] == 'file already exists'

    def test_checksum_mismatch_on_download_fails(self, tmp_path):
        dest = tmp_path / 'out.bin'
        action, mod = _make({'url': 'http://h/file', 'dest': str(dest),
                             'checksum': 'sha256:' + 'a' * 64})
        with patch.object(mod, 'open_url', return_value=_FakeResp(b'DATA')):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'did not match' in result['msg']
        assert not dest.exists()

    def test_checksum_match_on_download_writes(self, tmp_path):
        dest = tmp_path / 'out.bin'
        digest = hashlib.sha256(b'DATA').hexdigest()
        action, mod = _make({'url': 'http://h/file', 'dest': str(dest),
                             'checksum': 'sha256:' + digest})
        with patch.object(mod, 'open_url', return_value=_FakeResp(b'DATA')):
            result = action.run(task_vars={})
        assert result['changed'] is True
        assert dest.read_bytes() == b'DATA'

    def test_non_200_fails(self, tmp_path):
        dest = tmp_path / 'out.bin'
        action, mod = _make({'url': 'http://h/file', 'dest': str(dest)})
        with patch.object(mod, 'open_url', return_value=_FakeResp(b'nope', status=404)):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert result['status_code'] == 404
        assert 'Request failed' in result['msg']

    def test_url_error_fails(self, tmp_path):
        dest = tmp_path / 'out.bin'
        action, mod = _make({'url': 'http://h/file', 'dest': str(dest)})
        with patch.object(mod, 'open_url', side_effect=URLError('boom')):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'Request failed' in result['msg']

    def test_mode_applied_on_new_file(self, tmp_path):
        dest = tmp_path / 'out.bin'
        action, mod = _make({'url': 'http://h/file', 'dest': str(dest), 'mode': '0600'})
        with patch.object(mod, 'open_url', return_value=_FakeResp(b'DATA')):
            result = action.run(task_vars={})
        assert result['changed'] is True
        assert stat_module.S_IMODE(os.stat(str(dest)).st_mode) == 0o600

    def test_mode_only_change(self, tmp_path):
        dest = tmp_path / 'out.bin'
        dest.write_bytes(b'DATA')
        os.chmod(str(dest), 0o644)
        action, mod = _make({'url': 'http://h/file', 'dest': str(dest), 'mode': '0600'})
        with patch.object(mod, 'open_url', return_value=_FakeResp(b'DATA')):
            result = action.run(task_vars={})
        # content identical, but mode differs -> changed via attribute update
        assert result['changed'] is True
        assert stat_module.S_IMODE(os.stat(str(dest)).st_mode) == 0o600

    def test_missing_url_or_dest_fails(self, tmp_path):
        action, mod = _make({'dest': str(tmp_path / 'x')})
        result = action.run(task_vars={})
        assert result['failed'] is True

    def test_failure_is_unmarked(self, tmp_path):
        dest = tmp_path / 'out.bin'
        action, mod = _make({'url': 'http://h/file', 'dest': str(dest)})
        with patch.object(mod, 'open_url', return_value=_FakeResp(b'nope', status=500)):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert FAST_PLUGIN_MARKER not in result


class TestGetUrlFallback:
    def test_delegates_for_non_local_connection(self, tmp_path):
        action = make_action('get_url', {'url': 'http://h/f', 'dest': str(tmp_path / 'x')},
                             local=False)
        with patch.object(action, '_execute_module', return_value={'changed': True}) as mock_exec:
            result = action.run(task_vars={})
        mock_exec.assert_called_once()
        assert FAST_PLUGIN_MARKER not in result

    def test_delegates_when_become_is_set(self, tmp_path):
        action = make_action('get_url', {'url': 'http://h/f', 'dest': str(tmp_path / 'x')},
                             become=True)
        with patch.object(action, '_execute_module', return_value={'changed': True}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()

    def test_delegates_in_check_mode(self, tmp_path):
        action = make_action('get_url', {'url': 'http://h/f', 'dest': str(tmp_path / 'x')},
                             check_mode=True)
        with patch.object(action, '_execute_module', return_value={'changed': True}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()

    def test_delegates_for_unsupported_args(self, tmp_path):
        action = make_action('get_url', {'url': 'http://h/f', 'dest': str(tmp_path / 'x'),
                                         'owner': 'root'})
        with patch.object(action, '_execute_module', return_value={'changed': True}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()

    def test_delegates_when_alias_and_canonical_conflict(self, tmp_path):
        """Both username and url_username set: let the module's precedence rules decide."""
        action = make_action('get_url', {'url': 'http://h/f', 'dest': str(tmp_path / 'x'),
                                         'username': 'a', 'url_username': 'b'})
        with patch.object(action, '_execute_module', return_value={'changed': True}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()

    def test_delegates_for_checksum_url(self, tmp_path):
        action = make_action('get_url', {'url': 'http://h/f', 'dest': str(tmp_path / 'x'),
                                         'checksum': 'sha256:http://h/sums.txt'})
        with patch.object(action, '_execute_module', return_value={'changed': True}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()

    def test_delegates_for_directory_dest(self, tmp_path):
        action = make_action('get_url', {'url': 'http://h/f', 'dest': str(tmp_path)})
        with patch.object(action, '_execute_module', return_value={'changed': True}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()
