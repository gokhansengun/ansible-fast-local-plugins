from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from ansible.module_utils._text import to_native
from ansible.module_utils.urls import open_url
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local, atomic_write, mark_fast_result  # noqa: E402

display = Display()

FAST_GET_URL_VERSION = '1.0'

# Arguments the fast path honours. Anything else — file-ownership attrs, backup,
# tmp_dest, unsafe_writes, client certs, ... — triggers a fallback so the
# standard module's behaviour is preserved.
_SUPPORTED_ARGS = frozenset({
    'url', 'dest', 'checksum', 'force', 'timeout', 'headers', 'validate_certs',
    'url_username', 'url_password', 'force_basic_auth', 'http_agent',
    'follow_redirects', 'mode', 'decompress', 'use_proxy',
})

_CHECKSUM_URL_SCHEMES = ('http', 'https', 'ftp', 'sftp', 'file')


def _str2bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


class ActionModule(ActionBase):
    TRANSFERS_FILES = True

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = {}

        result = super().run(tmp, task_vars)
        del tmp

        conn = self._connection
        args = self._task.args

        display.debug(
            'fast_get_url v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_GET_URL_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        unsupported = set(args) - _SUPPORTED_ARGS
        # A checksum given as a URL, or a destination that is an existing
        # directory (filename derived from headers), are handled by the module.
        checksum_is_url = self._checksum_is_url(args.get('checksum'))
        dest_is_dir = self._dest_is_dir(args.get('dest'))

        if (not _is_local(conn) or self._play_context.become
                or self._task.async_val or self._play_context.check_mode
                or unsupported or checksum_is_url or dest_is_dir):
            if unsupported:
                display.debug('fast_get_url: unsupported args %r, delegating' % sorted(unsupported))
            else:
                display.debug('fast_get_url: delegating to standard get_url module')
            return self._execute_module(task_vars=task_vars, wrap_async=self._task.async_val)

        return mark_fast_result(self._run_local(args, result))

    @staticmethod
    def _checksum_is_url(checksum):
        if not checksum or ':' not in checksum:
            return False
        _algo, _sep, value = checksum.partition(':')
        return urlsplit(value).scheme in _CHECKSUM_URL_SCHEMES

    @staticmethod
    def _dest_is_dir(dest):
        if not isinstance(dest, str):
            return False
        return os.path.isdir(os.path.expanduser(os.path.expandvars(dest)))

    def _run_local(self, args, result):
        display.debug('fast_get_url: local connection, downloading in-process')

        url = args.get('url')
        dest = args.get('dest')
        if not url or not dest:
            return dict(failed=True, msg='both url and dest are required')
        dest = os.path.expanduser(os.path.expandvars(dest))

        force = _str2bool(args.get('force', False))
        mode = args.get('mode')

        algorithm = None
        expected = ''
        checksum_arg = args.get('checksum') or ''
        if checksum_arg:
            if ':' not in checksum_arg:
                return dict(failed=True,
                            msg='The checksum parameter has to be in format <algorithm>:<checksum>')
            algorithm, _sep, expected = checksum_arg.partition(':')
            expected = re.sub(r'\W+', '', expected).lower()

        result.update(dict(url=url, dest=dest, checksum_src=None, checksum_dest=None))

        # If a checksum is supplied and the existing file already matches it,
        # skip the download entirely (unless force).
        if not force and expected and os.path.exists(dest):
            dest_digest = self._digest_file(dest, algorithm)
            result['checksum_dest'] = dest_digest
            if dest_digest == expected:
                changed = self._apply_mode_only(dest, mode)
                result['changed'] = changed
                result['msg'] = ('file already exists but file attributes changed'
                                 if changed else 'file already exists')
                return result

        start = time.time()
        try:
            body, status = self._download(args, url)
        except URLError as e:
            return dict(failed=True, url=url, dest=dest,
                        msg='Request failed: %s' % to_native(e), elapsed=int(time.time() - start))
        except Exception as e:
            return dict(failed=True, url=url, dest=dest,
                        msg='Request failed: %s' % to_native(e), elapsed=int(time.time() - start))
        result['elapsed'] = int(time.time() - start)
        result['status_code'] = status

        if status != 200 and not url.startswith('file:/'):
            return dict(failed=True, url=url, dest=dest, status_code=status,
                        msg='Request failed', elapsed=result['elapsed'])

        checksum_src = hashlib.sha1(body).hexdigest()
        result['checksum_src'] = checksum_src

        if expected:
            got = hashlib.new(algorithm, body).hexdigest()
            if got != expected:
                return dict(failed=True, url=url, dest=dest,
                            msg='The checksum for the downloaded file did not match %s; it was %s.'
                                % (expected, got))

        dest_existed = os.path.exists(dest)
        checksum_dest = self._sha1_file(dest) if dest_existed else None
        result['checksum_dest'] = checksum_dest

        if checksum_src != checksum_dest:
            write_mode = mode
            if write_mode is None and not dest_existed:
                cur_umask = os.umask(0)
                os.umask(cur_umask)
                write_mode = 0o666 & ~cur_umask
            err = atomic_write(dest, body, mode=write_mode)
            if err:
                return dict(failed=True, url=url, dest=dest, msg=err)
            result['changed'] = True
        else:
            result['changed'] = self._apply_mode_only(dest, mode)

        try:
            result['md5sum'] = hashlib.md5(body).hexdigest()
        except ValueError:
            result['md5sum'] = None
        result.setdefault('msg', 'OK')
        return result

    def _download(self, args, url):
        r = open_url(
            url,
            method='GET',
            headers=dict(args.get('headers') or {}),
            timeout=args.get('timeout', 10),
            validate_certs=_str2bool(args.get('validate_certs', True), default=True),
            url_username=args.get('url_username'),
            url_password=args.get('url_password'),
            force_basic_auth=_str2bool(args.get('force_basic_auth', False)),
            follow_redirects=args.get('follow_redirects', 'urllib2'),
            http_agent=args.get('http_agent', 'ansible-httpget'),
            use_proxy=_str2bool(args.get('use_proxy', True), default=True),
            decompress=_str2bool(args.get('decompress', True), default=True),
        )
        return r.read(), r.getcode()

    @staticmethod
    def _apply_mode_only(dest, mode):
        """chmod dest to mode if it differs; return True if a change was made."""
        if mode is None:
            return False
        import stat as stat_module
        from _action_utils import _parse_mode
        try:
            want = _parse_mode(mode)
        except ValueError:
            return False
        have = stat_module.S_IMODE(os.stat(dest).st_mode)
        if have != want:
            os.chmod(dest, want)
            return True
        return False

    @staticmethod
    def _sha1_file(path):
        h = hashlib.sha1()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()

    def _digest_file(self, path, algorithm):
        h = hashlib.new(algorithm)
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()
