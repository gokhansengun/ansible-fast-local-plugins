from __future__ import annotations

import hashlib
import os
import stat
import sys

import pytest

from ansible.errors import AnsibleActionFail, AnsibleError

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import FAST_PLUGIN_MARKER, make_action, mock_builtin_run


def _src_action(args, src_path, **kwargs):
    """Build a copy action for a `src` task, stubbing _find_needle (which needs a
    real role/search-path context unavailable to the unit harness) to resolve to
    the given on-disk path."""
    action = make_action('copy', args, **kwargs)
    action._find_needle = lambda dirname, needle: src_path
    return action


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

    def test_fast_path_sets_marker(self, tmp_path):
        dest = str(tmp_path / 'out.txt')
        action = make_action('copy', {'dest': dest, 'content': 'hello world'})
        result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_idempotent_skip_is_marked(self, tmp_path):
        dest = tmp_path / 'out.txt'
        dest.write_text('same')
        action = make_action('copy', {'dest': str(dest), 'content': 'same'})
        result = action.run(task_vars={})
        assert result['changed'] is False
        assert result[FAST_PLUGIN_MARKER] is True

    def test_fast_path_failure_is_unmarked(self):
        action = make_action('copy', {'content': 'hello'})  # no dest -> failure
        result = action.run(task_vars={})
        assert result.get('failed') is True
        assert FAST_PLUGIN_MARKER not in result


class TestCopyFallback:
    def test_delegates_for_non_local(self, tmp_path):
        dest = str(tmp_path / 'out.txt')
        action = make_action('copy', {'dest': dest, 'content': 'x'}, local=False)
        with mock_builtin_run('copy', {'changed': True}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_for_remote_src(self, tmp_path):
        # remote_src means the source is on the target side; the fast path doesn't
        # handle it (the source isn't read from the controller's files/ context).
        src = tmp_path / 'src.txt'
        src.write_text('x')
        dest = str(tmp_path / 'dst.txt')
        action = make_action('copy', {'src': str(src), 'dest': dest, 'remote_src': True})
        with mock_builtin_run('copy', {'changed': True}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_for_symlinked_directory_src(self, tmp_path):
        # A regular-file directory is fast-pathed; a tree with a symlink falls back
        # (the fast path can't reproduce stock's local_follow handling).
        src_dir = tmp_path / 'srcdir'
        src_dir.mkdir()
        (src_dir / 'f.txt').write_text('x')
        (src_dir / 'link').symlink_to(src_dir / 'f.txt')
        dest = str(tmp_path / 'dstdir')
        action = _src_action({'src': str(src_dir), 'dest': dest}, str(src_dir))
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

    def test_fallback_result_is_unmarked(self, tmp_path):
        dest = str(tmp_path / 'out.txt')
        action = make_action('copy', {'dest': dest, 'content': 'x'}, local=False)
        with mock_builtin_run('copy', {'changed': True}):
            result = action.run(task_vars={})
        assert FAST_PLUGIN_MARKER not in result


class TestCopySrcFastPath:
    """A local `src` file copy now runs in-process instead of falling back."""

    def test_src_file_copied_in_process(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_text('source bytes')
        dest = str(tmp_path / 'dst.txt')
        action = _src_action({'src': str(src), 'dest': dest}, str(src))
        result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True
        assert open(dest).read() == 'source bytes'
        # fast path ran (no fallback)
        assert result[FAST_PLUGIN_MARKER] is True
        assert result['src'] == str(src)
        assert result['checksum'] == hashlib.sha1(b'source bytes',
                                                  usedforsecurity=False).hexdigest()

    def test_src_copy_is_idempotent(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_text('same bytes')
        dest = tmp_path / 'dst.txt'
        dest.write_text('same bytes')
        action = _src_action({'src': str(src), 'dest': str(dest)}, str(src))
        result = action.run(task_vars={})
        assert result['changed'] is False
        assert result[FAST_PLUGIN_MARKER] is True

    def test_src_copy_into_directory_dest(self, tmp_path):
        src = tmp_path / 'ca.crt'
        src.write_text('CERT')
        dest_dir = tmp_path / 'destdir'
        dest_dir.mkdir()
        action = _src_action({'src': str(src), 'dest': str(dest_dir)}, str(src))
        result = action.run(task_vars={})
        assert not result.get('failed'), result
        # copied into the directory by basename, like stock copy
        assert result['dest'] == str(dest_dir / 'ca.crt')
        assert (dest_dir / 'ca.crt').read_text() == 'CERT'

    def test_src_copy_sets_mode(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_text('data')
        dest = str(tmp_path / 'dst.txt')
        action = _src_action({'src': str(src), 'dest': dest, 'mode': '0600'}, str(src))
        result = action.run(task_vars={})
        assert not result.get('failed')
        assert stat.S_IMODE(os.stat(dest).st_mode) == 0o600

    def test_src_not_found_falls_back(self, tmp_path):
        dest = str(tmp_path / 'dst.txt')
        action = make_action('copy', {'src': 'nope.txt', 'dest': dest})

        def _raise(dirname, needle):
            raise AnsibleError('Could not find file nope.txt')
        action._find_needle = _raise
        with mock_builtin_run('copy', {'changed': True}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()


class TestCopyStrictReason:
    """AFLP_STRICT names the precise copy-specific trigger, not the generic
    'unsupported arguments'."""

    def test_strict_reports_remote_src(self, tmp_path, monkeypatch):
        monkeypatch.setenv('AFLP_STRICT', '1')
        dest = str(tmp_path / 'dst.txt')
        action = make_action('copy', {'src': '/x', 'dest': dest, 'remote_src': True})
        with pytest.raises(AnsibleActionFail, match='remote_src'):
            action.run(task_vars={})

    def test_strict_reports_symlink_in_tree(self, tmp_path, monkeypatch):
        monkeypatch.setenv('AFLP_STRICT', '1')
        src_dir = tmp_path / 'srcdir'
        src_dir.mkdir()
        (src_dir / 'f.txt').write_text('x')
        (src_dir / 'link').symlink_to(src_dir / 'f.txt')
        dest = str(tmp_path / 'dstdir')
        action = _src_action({'src': str(src_dir), 'dest': dest}, str(src_dir))
        with pytest.raises(AnsibleActionFail, match='symlink in source tree'):
            action.run(task_vars={})

    def test_strict_reports_backup_arg(self, tmp_path, monkeypatch):
        monkeypatch.setenv('AFLP_STRICT', '1')
        dest = str(tmp_path / 'dst.txt')
        action = make_action('copy', {'dest': dest, 'content': 'x', 'backup': True})
        with pytest.raises(AnsibleActionFail, match='backup'):
            action.run(task_vars={})


class TestCopyDirFastPath:
    """A regular-file directory tree is copied recursively in-process."""

    def _tree(self, tmp_path):
        src = tmp_path / 'tree'
        (src / 'sub').mkdir(parents=True)
        (src / 'empty').mkdir()
        (src / 'a.txt').write_text('A\n')
        (src / 'sub' / 'b.txt').write_text('B\n')
        return src

    def test_trailing_slash_copies_contents(self, tmp_path):
        src = self._tree(tmp_path)
        dest = tmp_path / 'dst'
        action = _src_action({'src': str(src) + '/', 'dest': str(dest)}, str(src))
        result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True
        assert result[FAST_PLUGIN_MARKER] is True
        assert result['dest'].endswith('/')
        # contents land directly in dest (no 'tree/' level)
        assert (dest / 'a.txt').read_text() == 'A\n'
        assert (dest / 'sub' / 'b.txt').read_text() == 'B\n'
        assert (dest / 'empty').is_dir()
        assert not (dest / 'tree').exists()

    def test_no_slash_nests_dir_by_basename(self, tmp_path):
        src = self._tree(tmp_path)
        dest = tmp_path / 'dst'
        action = _src_action({'src': str(src), 'dest': str(dest)}, str(src))
        result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert (dest / 'tree' / 'a.txt').read_text() == 'A\n'
        assert (dest / 'tree' / 'sub' / 'b.txt').read_text() == 'B\n'
        assert (dest / 'tree' / 'empty').is_dir()

    def test_dir_copy_is_idempotent(self, tmp_path):
        src = self._tree(tmp_path)
        dest = tmp_path / 'dst'

        def _act():
            return _src_action({'src': str(src) + '/', 'dest': str(dest)}, str(src))
        assert _act().run(task_vars={})['changed'] is True
        assert _act().run(task_vars={})['changed'] is False

    def test_file_mode_applies_to_files(self, tmp_path):
        src = self._tree(tmp_path)
        dest = tmp_path / 'dst'
        action = _src_action({'src': str(src) + '/', 'dest': str(dest), 'mode': '0640'}, str(src))
        action.run(task_vars={})
        assert stat.S_IMODE(os.stat(dest / 'a.txt').st_mode) == 0o640
        assert stat.S_IMODE(os.stat(dest / 'sub' / 'b.txt').st_mode) == 0o640

    def test_directory_mode_applies_to_dirs(self, tmp_path):
        src = self._tree(tmp_path)
        dest = tmp_path / 'dst'
        action = _src_action(
            {'src': str(src) + '/', 'dest': str(dest), 'directory_mode': '0750'}, str(src))
        action.run(task_vars={})
        assert stat.S_IMODE(os.stat(dest).st_mode) == 0o750
        assert stat.S_IMODE(os.stat(dest / 'sub').st_mode) == 0o750

    def test_directory_mode_preserve_falls_back(self, tmp_path):
        src = self._tree(tmp_path)
        dest = str(tmp_path / 'dst')
        action = _src_action(
            {'src': str(src), 'dest': dest, 'directory_mode': 'preserve'}, str(src))
        with mock_builtin_run('copy', {'changed': True}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()
