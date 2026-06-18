from __future__ import annotations

import grp
import os
import pwd
import shutil
import stat as stat_module
import sys

from ansible.module_utils.common.text.converters import to_native
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local, _parse_mode, mark_fast_result, strict_guard  # noqa: E402

display = Display()

FAST_FILE_VERSION = '1.0'


def _resolve_uid(owner):
    """Return (uid, error). uid=-1 means 'no change'."""
    if owner is None:
        return -1, None
    try:
        return pwd.getpwnam(str(owner)).pw_uid, None
    except KeyError:
        try:
            return int(owner), None
        except (ValueError, TypeError):
            return None, 'user not found: %s' % owner


def _resolve_gid(group):
    """Return (gid, error). gid=-1 means 'no change'."""
    if group is None:
        return -1, None
    try:
        return grp.getgrnam(str(group)).gr_gid, None
    except KeyError:
        try:
            return int(group), None
        except (ValueError, TypeError):
            return None, 'group not found: %s' % group


def _apply_attrs(path, mode_int, uid, gid, follow=True):
    """Apply mode/owner/group to path. Returns (changed, error_msg)."""
    changed = False
    try:
        st = os.stat(path) if follow else os.lstat(path)
    except OSError as e:
        return False, to_native(e)

    if mode_int is not None:
        if stat_module.S_IMODE(st.st_mode) != mode_int:
            try:
                os.chmod(path, mode_int)
                changed = True
            except OSError as e:
                return changed, 'chmod failed on %s: %s' % (path, to_native(e))

    new_uid = uid if uid >= 0 else st.st_uid
    new_gid = gid if gid >= 0 else st.st_gid
    if new_uid != st.st_uid or new_gid != st.st_gid:
        try:
            os.chown(path, new_uid, new_gid)
            changed = True
        except OSError as e:
            return changed, 'chown failed on %s: %s' % (path, to_native(e))

    return changed, None


class ActionModule(ActionBase):
    TRANSFERS_FILES = False

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = {}

        result = super().run(tmp, task_vars)
        del tmp

        conn = self._connection
        args = self._task.args

        display.debug(
            'fast_file v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_FILE_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        if not _is_local(conn) or self._play_context.become or self._play_context.check_mode:
            display.debug('fast_file: delegating to module')
            strict_guard(conn, self._play_context, self._task)
            return self._execute_module(task_vars=task_vars, wrap_async=self._task.async_val)

        # access_time / modification_time require complex datetime parsing — delegate.
        if args.get('access_time') or args.get('modification_time'):
            display.debug('fast_file: access_time/modification_time set, delegating')
            strict_guard(conn, self._play_context, self._task)
            return self._execute_module(task_vars=task_vars, wrap_async=self._task.async_val)

        return mark_fast_result(self._run_local(args, result))

    def _run_local(self, args, result):
        path = args.get('path') or args.get('dest') or args.get('name')
        if not path:
            return dict(failed=True, msg='path is required')
        path = os.path.expanduser(os.path.expandvars(path))

        state   = args.get('state', 'file')
        src     = args.get('src')
        follow  = boolean(args.get('follow', True),   strict=False)
        force   = boolean(args.get('force', False),   strict=False)
        recurse = boolean(args.get('recurse', False), strict=False)

        mode_raw = args.get('mode')
        mode_int = None
        if mode_raw is not None:
            try:
                mode_int = _parse_mode(mode_raw)
            except ValueError as e:
                return dict(failed=True, msg='invalid mode: %s' % to_native(e))

        uid, err = _resolve_uid(args.get('owner'))
        if err:
            return dict(failed=True, msg=err)
        gid, err = _resolve_gid(args.get('group'))
        if err:
            return dict(failed=True, msg=err)

        # ------------------------------------------------------------------ #
        # state: absent                                                        #
        # ------------------------------------------------------------------ #
        if state == 'absent':
            changed = False
            if os.path.islink(path) or os.path.isfile(path):
                try:
                    os.unlink(path)
                    changed = True
                except OSError as e:
                    return dict(failed=True, msg='could not remove %s: %s' % (path, to_native(e)))
            elif os.path.isdir(path):
                try:
                    shutil.rmtree(path)
                    changed = True
                except OSError as e:
                    return dict(failed=True, msg='could not remove %s: %s' % (path, to_native(e)))
            result.update(dict(changed=changed, path=path, state='absent'))
            return result

        # ------------------------------------------------------------------ #
        # state: directory                                                     #
        # ------------------------------------------------------------------ #
        if state == 'directory':
            changed = False
            if not os.path.exists(path):
                try:
                    os.makedirs(path)
                    changed = True
                except OSError as e:
                    return dict(failed=True, msg='could not create directory %s: %s' % (path, to_native(e)))
            elif not os.path.isdir(path):
                return dict(failed=True, msg='%s already exists and is not a directory' % path)

            attr_changed, err = _apply_attrs(path, mode_int, uid, gid, follow)
            if err:
                return dict(failed=True, msg=err)
            changed = changed or attr_changed

            if recurse:
                for dirpath, dirnames, filenames in os.walk(path):
                    for name in dirnames + filenames:
                        sub_changed, err = _apply_attrs(
                            os.path.join(dirpath, name), mode_int, uid, gid, follow)
                        if err:
                            return dict(failed=True, msg=err)
                        changed = changed or sub_changed

            result.update(dict(changed=changed, path=path, state='directory'))
            return result

        # ------------------------------------------------------------------ #
        # state: file                                                          #
        # ------------------------------------------------------------------ #
        if state == 'file':
            if not os.path.exists(path) and not os.path.islink(path):
                return dict(failed=True, msg='%s does not exist' % path, state='absent')
            attr_changed, err = _apply_attrs(path, mode_int, uid, gid, follow)
            if err:
                return dict(failed=True, msg=err)
            result.update(dict(changed=attr_changed, path=path, state='file'))
            return result

        # ------------------------------------------------------------------ #
        # state: touch                                                         #
        # ------------------------------------------------------------------ #
        if state == 'touch':
            changed = False
            if not os.path.exists(path) and not os.path.islink(path):
                try:
                    open(path, 'ab').close()
                    changed = True
                except OSError as e:
                    return dict(failed=True, msg='could not create %s: %s' % (path, to_native(e)))
            else:
                try:
                    os.utime(path, None)
                    changed = True
                except OSError as e:
                    return dict(failed=True, msg='could not touch %s: %s' % (path, to_native(e)))

            attr_changed, err = _apply_attrs(path, mode_int, uid, gid, follow)
            if err:
                return dict(failed=True, msg=err)
            result.update(dict(changed=changed or attr_changed, path=path, state='file'))
            return result

        # ------------------------------------------------------------------ #
        # state: link                                                          #
        # ------------------------------------------------------------------ #
        if state == 'link':
            if not src:
                return dict(failed=True, msg='src is required for state=link')
            src = os.path.expanduser(os.path.expandvars(src))
            changed = False

            if os.path.islink(path):
                if os.readlink(path) == src:
                    attr_changed, err = _apply_attrs(path, mode_int, uid, gid, follow=False)
                    if err:
                        return dict(failed=True, msg=err)
                    result.update(dict(changed=attr_changed, path=path, state='link', src=src))
                    return result
                try:
                    os.unlink(path)
                except OSError as e:
                    return dict(failed=True, msg='could not remove existing symlink: %s' % to_native(e))
            elif os.path.exists(path):
                if not force:
                    return dict(failed=True,
                                msg='%s already exists and is not a symlink; use force=true to replace' % path)
                try:
                    os.unlink(path) if os.path.isfile(path) else shutil.rmtree(path)
                except OSError as e:
                    return dict(failed=True, msg='could not remove %s: %s' % (path, to_native(e)))

            if not os.path.exists(src) and not force:
                return dict(failed=True, msg='src %s does not exist' % src)

            try:
                os.symlink(src, path)
                changed = True
            except OSError as e:
                return dict(failed=True, msg='could not create symlink %s -> %s: %s' % (path, src, to_native(e)))

            attr_changed, err = _apply_attrs(path, mode_int, uid, gid, follow=False)
            if err:
                return dict(failed=True, msg=err)
            result.update(dict(changed=changed or attr_changed, path=path, state='link', src=src))
            return result

        # ------------------------------------------------------------------ #
        # state: hard                                                          #
        # ------------------------------------------------------------------ #
        if state == 'hard':
            if not src:
                return dict(failed=True, msg='src is required for state=hard')
            src = os.path.expanduser(os.path.expandvars(src))

            if not os.path.exists(src):
                return dict(failed=True, msg='src %s does not exist' % src)

            changed = False
            if os.path.exists(path):
                if os.path.samefile(path, src):
                    attr_changed, err = _apply_attrs(path, mode_int, uid, gid, follow)
                    if err:
                        return dict(failed=True, msg=err)
                    result.update(dict(changed=attr_changed, path=path, state='hard', src=src))
                    return result
                try:
                    os.unlink(path)
                except OSError as e:
                    return dict(failed=True, msg='could not remove %s: %s' % (path, to_native(e)))

            try:
                os.link(src, path)
                changed = True
            except OSError as e:
                return dict(failed=True, msg='could not create hard link: %s' % to_native(e))

            attr_changed, err = _apply_attrs(path, mode_int, uid, gid, follow)
            if err:
                return dict(failed=True, msg=err)
            result.update(dict(changed=changed or attr_changed, path=path, state='hard', src=src))
            return result

        return dict(failed=True, msg='unsupported state: %s' % state)
