from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tracemalloc
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from ansible.module_utils.common.text.converters import to_bytes

from tests.conftest import FAST_PLUGIN_MARKER, _load_plugin, make_action, mock_builtin_run


def _sha1(data):
    return hashlib.sha1(data).hexdigest()


class TestFetchFastPath:
    def test_flat_fetch_to_file(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_bytes(b'payload')
        dest = tmp_path / 'out.txt'
        action = make_action('fetch', {'src': str(src), 'dest': str(dest), 'flat': True})
        result = action.run(task_vars={})
        assert result['changed'] is True
        assert result['dest'] == str(dest)
        assert result['checksum'] == _sha1(b'payload')
        assert result['remote_checksum'] == _sha1(b'payload')
        assert dest.read_bytes() == b'payload'
        assert result[FAST_PLUGIN_MARKER] is True

    def test_flat_fetch_is_idempotent(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_bytes(b'payload')
        dest = tmp_path / 'out.txt'
        args = {'src': str(src), 'dest': str(dest), 'flat': True}
        make_action('fetch', args).run(task_vars={})
        result = make_action('fetch', args).run(task_vars={})
        assert result['changed'] is False
        assert result['checksum'] == _sha1(b'payload')

    def test_flat_trailing_slash_uses_basename(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_bytes(b'data')
        destdir = tmp_path / 'into'
        destdir.mkdir()
        action = make_action(
            'fetch', {'src': str(src), 'dest': str(destdir) + '/', 'flat': True})
        result = action.run(task_vars={})
        assert result['changed'] is True
        assert result['dest'] == str(destdir / 'src.txt')
        assert (destdir / 'src.txt').read_bytes() == b'data'

    def test_non_flat_uses_host_subdir(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_bytes(b'abc')
        destroot = tmp_path / 'fetched'
        action = make_action('fetch', {'src': str(src), 'dest': str(destroot)})
        result = action.run(task_vars={'inventory_hostname': 'host1'})
        expected = os.path.normpath('%s/%s/%s' % (str(destroot), 'host1', str(src)))
        assert result['dest'] == expected
        assert os.path.exists(expected)
        assert result['changed'] is True

    def test_missing_source_fail_on_missing(self, tmp_path):
        action = make_action(
            'fetch', {'src': str(tmp_path / 'nope'), 'dest': str(tmp_path / 'd'), 'flat': True})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert result['changed'] is False

    def test_missing_source_ignored(self, tmp_path):
        action = make_action('fetch', {'src': str(tmp_path / 'nope'),
                                       'dest': str(tmp_path / 'd'), 'flat': True,
                                       'fail_on_missing': False})
        result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['changed'] is False
        assert 'does not exist' in result['msg']

    def test_source_is_directory_fails(self, tmp_path):
        d = tmp_path / 'adir'
        d.mkdir()
        action = make_action('fetch', {'src': str(d), 'dest': str(tmp_path / 'd'), 'flat': True})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'directory' in result['msg']

    def test_failure_is_unmarked(self, tmp_path):
        action = make_action(
            'fetch', {'src': str(tmp_path / 'nope'), 'dest': str(tmp_path / 'd'), 'flat': True})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert FAST_PLUGIN_MARKER not in result


class TestFetchFallback:
    def test_delegates_for_non_local_connection(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_bytes(b'x')
        action = make_action('fetch', {'src': str(src), 'dest': str(tmp_path / 'o'), 'flat': True},
                             local=False)
        with mock_builtin_run('fetch', {'changed': True}) as mock_run:
            result = action.run(task_vars={})
        mock_run.assert_called_once()
        assert result == {'changed': True}
        assert FAST_PLUGIN_MARKER not in result

    def test_delegates_when_become_is_set(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_bytes(b'x')
        action = make_action('fetch', {'src': str(src), 'dest': str(tmp_path / 'o'), 'flat': True},
                             become=True)
        with mock_builtin_run('fetch', {'changed': True}) as mock_run:
            action.run(task_vars={})
        mock_run.assert_called_once()

    def test_delegates_in_check_mode(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_bytes(b'x')
        action = make_action('fetch', {'src': str(src), 'dest': str(tmp_path / 'o'), 'flat': True},
                             check_mode=True)
        with mock_builtin_run('fetch', {'changed': True}) as mock_run:
            action.run(task_vars={})
        mock_run.assert_called_once()


class TestFetchStreaming:
    """The copy must stream; buffering the whole file OOM-kills the controller.

    fetch used to read the source into a bytes object and write it back out, so
    peak RSS tracked the file size - fatal on the multi-GB database dumps this
    plugin is actually used to pull.
    """

    def test_copy_uses_shutil_copyfile(self, tmp_path):
        mod = _load_plugin('fetch')
        src = tmp_path / 'src.bin'
        src.write_bytes(b'payload' * 1000)
        dest = tmp_path / 'out.bin'
        action = make_action('fetch', {'src': str(src), 'dest': str(dest), 'flat': True})

        with patch.object(mod, 'shutil') as mock_shutil:
            mock_shutil.copyfile.side_effect = shutil.copyfile
            result = action.run(task_vars={})

        mock_shutil.copyfile.assert_called_once_with(
            to_bytes(str(src)), to_bytes(str(dest)))
        assert result['changed'] is True
        assert dest.read_bytes() == src.read_bytes()
        assert result[FAST_PLUGIN_MARKER] is True

    def test_peak_memory_does_not_track_file_size(self, tmp_path):
        size = 16 * 1024 * 1024
        src = tmp_path / 'big.bin'
        src.write_bytes(b'\xab' * size)
        dest = tmp_path / 'big.out'
        action = make_action('fetch', {'src': str(src), 'dest': str(dest), 'flat': True})

        tracemalloc.start()
        try:
            result = action.run(task_vars={})
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert result['changed'] is True
        assert result['checksum'] == result['remote_checksum']
        assert dest.stat().st_size == size
        # The buffering implementation peaked at >= the 16 MiB file; streaming
        # holds a 64 KiB block at most (and nothing at all via os.sendfile).
        assert peak < size // 4, 'fetch buffered %d bytes for a %d byte file' % (peak, size)

    def test_large_file_is_byte_exact(self, tmp_path):
        payload = os.urandom(3 * 1024 * 1024 + 7)  # not a multiple of any copy block size
        src = tmp_path / 'big.bin'
        src.write_bytes(payload)
        dest = tmp_path / 'big.out'
        action = make_action('fetch', {'src': str(src), 'dest': str(dest), 'flat': True})

        result = action.run(task_vars={})

        assert result['changed'] is True
        assert result['checksum'] == _sha1(payload)
        assert dest.read_bytes() == payload
