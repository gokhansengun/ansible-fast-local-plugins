from __future__ import annotations

import os
import pwd
import stat
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import FAST_PLUGIN_MARKER, make_action


def _action(args, **kwargs):
    return make_action('file', args, **kwargs)


CURRENT_USER = pwd.getpwuid(os.getuid()).pw_name


# ------------------------------------------------------------------ #
# state: absent                                                        #
# ------------------------------------------------------------------ #
class TestFileAbsent:
    def test_removes_regular_file(self, tmp_path):
        f = tmp_path / 'target.txt'
        f.write_text('x')
        result = _action({'path': str(f), 'state': 'absent'}).run(task_vars={})
        assert result['changed'] is True
        assert not f.exists()

    def test_removes_directory(self, tmp_path):
        d = tmp_path / 'subdir'
        d.mkdir()
        (d / 'inner.txt').write_text('x')
        result = _action({'path': str(d), 'state': 'absent'}).run(task_vars={})
        assert result['changed'] is True
        assert not d.exists()

    def test_removes_symlink(self, tmp_path):
        target = tmp_path / 'target.txt'
        target.write_text('x')
        link = tmp_path / 'link'
        link.symlink_to(target)
        result = _action({'path': str(link), 'state': 'absent'}).run(task_vars={})
        assert result['changed'] is True
        assert not link.exists()
        assert target.exists()  # only the link is removed

    def test_noop_when_already_absent(self, tmp_path):
        result = _action({'path': str(tmp_path / 'ghost'), 'state': 'absent'}).run(task_vars={})
        assert result['changed'] is False
        assert not result.get('failed')


# ------------------------------------------------------------------ #
# state: directory                                                     #
# ------------------------------------------------------------------ #
class TestFileDirectory:
    def test_creates_directory(self, tmp_path):
        d = tmp_path / 'newdir'
        result = _action({'path': str(d), 'state': 'directory'}).run(task_vars={})
        assert result['changed'] is True
        assert d.is_dir()

    def test_creates_intermediate_directories(self, tmp_path):
        d = tmp_path / 'a' / 'b' / 'c'
        result = _action({'path': str(d), 'state': 'directory'}).run(task_vars={})
        assert result['changed'] is True
        assert d.is_dir()

    def test_idempotent_when_directory_exists(self, tmp_path):
        d = tmp_path / 'existing'
        d.mkdir()
        result = _action({'path': str(d), 'state': 'directory'}).run(task_vars={})
        assert result['changed'] is False

    def test_sets_mode(self, tmp_path):
        d = tmp_path / 'modedir'
        result = _action({'path': str(d), 'state': 'directory', 'mode': '0700'}).run(task_vars={})
        assert result['changed'] is True
        assert stat.S_IMODE(d.stat().st_mode) == 0o700

    def test_fails_when_path_is_a_file(self, tmp_path):
        f = tmp_path / 'file.txt'
        f.write_text('x')
        result = _action({'path': str(f), 'state': 'directory'}).run(task_vars={})
        assert result['failed'] is True

    def test_recurse_applies_mode_to_children(self, tmp_path):
        d = tmp_path / 'tree'
        d.mkdir()
        child_file = d / 'child.txt'
        child_file.write_text('x')
        child_dir = d / 'subdir'
        child_dir.mkdir()
        _action({'path': str(d), 'state': 'directory', 'mode': '0755', 'recurse': True}).run(task_vars={})
        assert stat.S_IMODE(child_file.stat().st_mode) == 0o755
        assert stat.S_IMODE(child_dir.stat().st_mode) == 0o755

    def test_owner_sets_ownership(self, tmp_path):
        d = tmp_path / 'owneddir'
        result = _action({'path': str(d), 'state': 'directory',
                          'owner': CURRENT_USER}).run(task_vars={})
        assert not result.get('failed')
        assert d.stat().st_uid == os.getuid()


# ------------------------------------------------------------------ #
# state: file                                                          #
# ------------------------------------------------------------------ #
class TestFileFile:
    def test_noop_on_existing_file(self, tmp_path):
        f = tmp_path / 'file.txt'
        f.write_text('x')
        result = _action({'path': str(f), 'state': 'file'}).run(task_vars={})
        assert result['changed'] is False
        assert not result.get('failed')

    def test_fails_when_file_does_not_exist(self, tmp_path):
        result = _action({'path': str(tmp_path / 'ghost'), 'state': 'file'}).run(task_vars={})
        assert result['failed'] is True

    def test_sets_mode_on_existing_file(self, tmp_path):
        f = tmp_path / 'file.txt'
        f.write_text('x')
        result = _action({'path': str(f), 'state': 'file', 'mode': '0600'}).run(task_vars={})
        assert result['changed'] is True
        assert stat.S_IMODE(f.stat().st_mode) == 0o600

    def test_idempotent_when_mode_already_matches(self, tmp_path):
        f = tmp_path / 'file.txt'
        f.write_text('x')
        f.chmod(0o600)
        result = _action({'path': str(f), 'state': 'file', 'mode': '0600'}).run(task_vars={})
        assert result['changed'] is False


# ------------------------------------------------------------------ #
# state: touch                                                         #
# ------------------------------------------------------------------ #
class TestFileTouch:
    def test_creates_file_when_absent(self, tmp_path):
        f = tmp_path / 'new.txt'
        result = _action({'path': str(f), 'state': 'touch'}).run(task_vars={})
        assert result['changed'] is True
        assert f.exists()

    def test_updates_mtime_when_file_exists(self, tmp_path):
        f = tmp_path / 'existing.txt'
        f.write_text('x')
        old_mtime = f.stat().st_mtime
        import time; time.sleep(0.05)
        result = _action({'path': str(f), 'state': 'touch'}).run(task_vars={})
        assert result['changed'] is True
        assert f.stat().st_mtime >= old_mtime

    def test_sets_mode_on_new_file(self, tmp_path):
        f = tmp_path / 'new.txt'
        _action({'path': str(f), 'state': 'touch', 'mode': '0640'}).run(task_vars={})
        assert stat.S_IMODE(f.stat().st_mode) == 0o640


# ------------------------------------------------------------------ #
# state: link                                                          #
# ------------------------------------------------------------------ #
class TestFileLink:
    def test_creates_symlink(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_text('x')
        link = tmp_path / 'link'
        result = _action({'path': str(link), 'state': 'link', 'src': str(src)}).run(task_vars={})
        assert result['changed'] is True
        assert link.is_symlink()
        assert os.readlink(str(link)) == str(src)

    def test_idempotent_when_symlink_already_correct(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_text('x')
        link = tmp_path / 'link'
        link.symlink_to(src)
        result = _action({'path': str(link), 'state': 'link', 'src': str(src)}).run(task_vars={})
        assert result['changed'] is False

    def test_replaces_wrong_symlink(self, tmp_path):
        src1 = tmp_path / 'src1.txt'
        src1.write_text('a')
        src2 = tmp_path / 'src2.txt'
        src2.write_text('b')
        link = tmp_path / 'link'
        link.symlink_to(src1)
        result = _action({'path': str(link), 'state': 'link', 'src': str(src2)}).run(task_vars={})
        assert result['changed'] is True
        assert os.readlink(str(link)) == str(src2)

    def test_force_replaces_existing_file_with_symlink(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_text('x')
        target = tmp_path / 'target.txt'
        target.write_text('existing')
        result = _action({'path': str(target), 'state': 'link',
                          'src': str(src), 'force': True}).run(task_vars={})
        assert result['changed'] is True
        assert target.is_symlink()

    def test_fails_when_file_exists_without_force(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_text('x')
        target = tmp_path / 'target.txt'
        target.write_text('existing')
        result = _action({'path': str(target), 'state': 'link', 'src': str(src)}).run(task_vars={})
        assert result['failed'] is True

    def test_fails_when_src_missing_without_force(self, tmp_path):
        result = _action({'path': str(tmp_path / 'link'), 'state': 'link',
                          'src': str(tmp_path / 'ghost')}).run(task_vars={})
        assert result['failed'] is True

    def test_allows_dangling_symlink_with_force(self, tmp_path):
        result = _action({'path': str(tmp_path / 'link'), 'state': 'link',
                          'src': str(tmp_path / 'ghost'), 'force': True}).run(task_vars={})
        assert result['changed'] is True
        assert os.path.islink(str(tmp_path / 'link'))

    def test_missing_src_returns_failed(self, tmp_path):
        result = _action({'path': str(tmp_path / 'link'), 'state': 'link'}).run(task_vars={})
        assert result['failed'] is True


# ------------------------------------------------------------------ #
# state: hard                                                          #
# ------------------------------------------------------------------ #
class TestFileHard:
    def test_creates_hard_link(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_text('x')
        link = tmp_path / 'hardlink'
        result = _action({'path': str(link), 'state': 'hard', 'src': str(src)}).run(task_vars={})
        assert result['changed'] is True
        assert link.stat().st_ino == src.stat().st_ino

    def test_idempotent_when_hard_link_exists(self, tmp_path):
        src = tmp_path / 'src.txt'
        src.write_text('x')
        link = tmp_path / 'hardlink'
        os.link(str(src), str(link))
        result = _action({'path': str(link), 'state': 'hard', 'src': str(src)}).run(task_vars={})
        assert result['changed'] is False

    def test_fails_when_src_missing(self, tmp_path):
        result = _action({'path': str(tmp_path / 'link'), 'state': 'hard',
                          'src': str(tmp_path / 'ghost')}).run(task_vars={})
        assert result['failed'] is True

    def test_missing_src_arg_returns_failed(self, tmp_path):
        result = _action({'path': str(tmp_path / 'link'), 'state': 'hard'}).run(task_vars={})
        assert result['failed'] is True


# ------------------------------------------------------------------ #
# edge cases                                                           #
# ------------------------------------------------------------------ #
class TestFileEdgeCases:
    def test_missing_path_returns_failed(self):
        result = _action({}).run(task_vars={})
        assert result['failed'] is True

    def test_invalid_mode_returns_failed(self, tmp_path):
        f = tmp_path / 'file.txt'
        f.write_text('x')
        result = _action({'path': str(f), 'state': 'file', 'mode': 'invalid'}).run(task_vars={})
        assert result['failed'] is True

    def test_unknown_state_returns_failed(self, tmp_path):
        result = _action({'path': str(tmp_path / 'x'), 'state': 'bogus'}).run(task_vars={})
        assert result['failed'] is True


# ------------------------------------------------------------------ #
# fallback                                                             #
# ------------------------------------------------------------------ #
class TestFileFallback:
    def test_delegates_for_non_local(self, tmp_path):
        from unittest.mock import patch
        action = _action({'path': str(tmp_path / 'x'), 'state': 'directory'}, local=False)
        with patch.object(action, '_execute_module', return_value={'changed': False}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_when_become(self, tmp_path):
        from unittest.mock import patch
        action = _action({'path': str(tmp_path / 'x'), 'state': 'directory'}, become=True)
        with patch.object(action, '_execute_module', return_value={'changed': False}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_in_check_mode(self, tmp_path):
        from unittest.mock import patch
        action = _action({'path': str(tmp_path / 'x'), 'state': 'directory'}, check_mode=True)
        with patch.object(action, '_execute_module', return_value={'changed': False}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_when_access_time_set(self, tmp_path):
        from unittest.mock import patch
        action = _action({'path': str(tmp_path / 'x'), 'state': 'touch',
                          'access_time': 'now'})
        with patch.object(action, '_execute_module', return_value={'changed': True}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_fallback_result_is_unmarked(self, tmp_path):
        from unittest.mock import patch
        action = _action({'path': str(tmp_path / 'x'), 'state': 'directory'}, local=False)
        with patch.object(action, '_execute_module', return_value={'changed': False}):
            result = action.run(task_vars={})
        assert FAST_PLUGIN_MARKER not in result

    def test_access_time_fallback_is_unmarked(self, tmp_path):
        from unittest.mock import patch
        action = _action({'path': str(tmp_path / 'x'), 'state': 'touch', 'access_time': 'now'})
        with patch.object(action, '_execute_module', return_value={'changed': True}):
            result = action.run(task_vars={})
        assert FAST_PLUGIN_MARKER not in result


# ------------------------------------------------------------------ #
# fast-path marker                                                     #
# ------------------------------------------------------------------ #
class TestFileFastMarker:
    def test_directory_success_is_marked(self, tmp_path):
        result = _action({'path': str(tmp_path / 'd'), 'state': 'directory'}).run(task_vars={})
        assert result['changed'] is True
        assert result[FAST_PLUGIN_MARKER] is True

    def test_touch_success_is_marked(self, tmp_path):
        result = _action({'path': str(tmp_path / 'f'), 'state': 'touch'}).run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_fast_path_failure_is_unmarked(self):
        result = _action({}).run(task_vars={})  # no path -> failure
        assert result.get('failed') is True
        assert FAST_PLUGIN_MARKER not in result
