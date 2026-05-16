from __future__ import annotations

import grp
import hashlib
import os
import pwd
import stat as stat_module
import sys

from ansible.module_utils._text import to_native
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local  # noqa: E402

display = Display()

FAST_STAT_VERSION = '1.0'

_CHECKSUM_MAP = {
    'sha1': hashlib.sha1,
    'sha224': hashlib.sha224,
    'sha256': hashlib.sha256,
    'sha384': hashlib.sha384,
    'sha512': hashlib.sha512,
    'md5': hashlib.md5,
}


def _checksum(path, algorithm):
    h = _CHECKSUM_MAP.get(algorithm, hashlib.sha1)()
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


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
            'fast_stat v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_STAT_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        if not _is_local(conn) or self._play_context.become:
            display.debug('fast_stat: non-local connection, delegating to module')
            return self._execute_module(task_vars=task_vars, wrap_async=self._task.async_val)

        display.debug('fast_stat: local connection, using in-process stat')

        path = args.get('path') or args.get('dest') or args.get('name')
        if not path:
            return dict(failed=True, msg='path is required')

        follow = boolean(args.get('follow', False), strict=False)
        get_checksum = boolean(args.get('get_checksum', True), strict=False)
        checksum_algorithm = args.get('checksum_algorithm', 'sha1')
        get_mime = boolean(args.get('get_mime', False), strict=False)

        path = os.path.expanduser(os.path.expandvars(path))

        # File does not exist (and is not a dangling symlink)
        if not os.path.exists(path) and not os.path.islink(path):
            result.update(dict(changed=False, stat=dict(exists=False, path=path)))
            return result

        try:
            st = os.stat(path) if follow else os.lstat(path)
        except OSError as e:
            return dict(failed=True, msg='stat failed: %s' % to_native(e))

        mode = stat_module.S_IMODE(st.st_mode)
        is_lnk = stat_module.S_ISLNK(st.st_mode)
        is_dir = stat_module.S_ISDIR(st.st_mode)
        is_reg = stat_module.S_ISREG(st.st_mode)

        try:
            pw_name = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            pw_name = str(st.st_uid)

        try:
            gr_name = grp.getgrgid(st.st_gid).gr_name
        except KeyError:
            gr_name = str(st.st_gid)

        stat_info = dict(
            exists=True,
            path=path,
            mode='%04o' % mode,
            isdir=is_dir,
            isreg=is_reg,
            islnk=is_lnk,
            isblk=stat_module.S_ISBLK(st.st_mode),
            ischr=stat_module.S_ISCHR(st.st_mode),
            isfifo=stat_module.S_ISFIFO(st.st_mode),
            issock=stat_module.S_ISSOCK(st.st_mode),
            uid=st.st_uid,
            gid=st.st_gid,
            pw_name=pw_name,
            gr_name=gr_name,
            size=st.st_size,
            inode=st.st_ino,
            dev=st.st_dev,
            nlink=st.st_nlink,
            atime=st.st_atime,
            mtime=st.st_mtime,
            ctime=st.st_ctime,
            rusr=bool(mode & stat_module.S_IRUSR),
            wusr=bool(mode & stat_module.S_IWUSR),
            xusr=bool(mode & stat_module.S_IXUSR),
            rgrp=bool(mode & stat_module.S_IRGRP),
            wgrp=bool(mode & stat_module.S_IWGRP),
            xgrp=bool(mode & stat_module.S_IXGRP),
            roth=bool(mode & stat_module.S_IROTH),
            woth=bool(mode & stat_module.S_IWOTH),
            xoth=bool(mode & stat_module.S_IXOTH),
            isuid=bool(mode & stat_module.S_ISUID),
            isgid=bool(mode & stat_module.S_ISGID),
            readable=os.access(path, os.R_OK),
            writeable=os.access(path, os.W_OK),
            executable=os.access(path, os.X_OK),
            # get_attributes (lsattr) requires a subprocess — skipped in fast path
            attributes=[],
            attr_flags='',
        )

        if is_lnk:
            stat_info['lnk_source'] = os.path.realpath(path)
            stat_info['lnk_target'] = os.readlink(path)

        if is_reg and get_checksum:
            cs = _checksum(path, checksum_algorithm)
            if cs:
                stat_info['checksum'] = cs

        if is_reg and get_mime:
            import mimetypes
            mime, encoding = mimetypes.guess_type(path)
            stat_info['mimetype'] = mime or 'application/octet-stream'
            stat_info['charset'] = encoding or 'binary'

        result.update(dict(changed=False, stat=stat_info))
        return result
