from __future__ import annotations

import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin

from ansible.module_utils._text import to_native, to_text
from ansible.module_utils.urls import open_url
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local, mark_fast_result  # noqa: E402

display = Display()

FAST_URI_VERSION = '1.0'

JSON_CANDIDATES = {'json', 'javascript'}

# Arguments the in-process fast path knows how to honour. If a task sets any
# argument outside this set (dest, src, client_cert, unix_socket, creates,
# removes, ca_path, ...), we delegate to the standard uri module so behaviour
# stays correct rather than silently ignoring it.
_SUPPORTED_ARGS = frozenset({
    'url', 'method', 'return_content', 'body', 'body_format', 'headers',
    'status_code', 'timeout', 'validate_certs', 'url_username', 'url_password',
    'force_basic_auth', 'follow_redirects', 'http_agent', 'decompress',
})


def _str2bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


class ActionModule(ActionBase):
    TRANSFERS_FILES = False
    _supports_async = True

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = {}

        result = super().run(tmp, task_vars)
        del tmp

        conn = self._connection
        args = self._task.args

        display.debug(
            'fast_uri v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_URI_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        # Fall back for: non-local, become, async, check-mode, the
        # form-multipart body format (needs the module's multipart builder), or
        # any argument the fast path does not handle (dest/src/client_cert/...).
        unsupported = set(args) - _SUPPORTED_ARGS
        if (not _is_local(conn) or self._play_context.become
                or self._task.async_val or self._play_context.check_mode
                or args.get('body_format') == 'form-multipart'
                or unsupported):
            if unsupported:
                display.debug('fast_uri: unsupported args %r, delegating to module' % sorted(unsupported))
            else:
                display.debug('fast_uri: delegating to standard uri module')
            return self._execute_module(task_vars=task_vars, wrap_async=self._task.async_val)

        return mark_fast_result(self._run_local(args, result))

    def _run_local(self, args, result):
        display.debug('fast_uri: local connection, using in-process open_url')

        url = args.get('url')
        if not url:
            return dict(failed=True, msg='url is required')

        method = str(args.get('method', 'GET')).upper()
        return_content = _str2bool(args.get('return_content', False))
        validate_certs = _str2bool(args.get('validate_certs', True), default=True)
        force_basic_auth = _str2bool(args.get('force_basic_auth', False))
        follow_redirects = args.get('follow_redirects', 'safe')
        timeout = args.get('timeout', 30)
        http_agent = args.get('http_agent', 'ansible-httpget')
        body_format = str(args.get('body_format', 'raw')).lower()

        headers = dict(args.get('headers') or {})
        data = self._encode_body(args.get('body'), body_format, headers)

        status_code = args.get('status_code', [200])
        if not isinstance(status_code, (list, tuple)):
            status_code = [status_code]
        status_code = [int(x) for x in status_code]

        start = time.time()
        try:
            resp_info, body = self._request(
                url, method, data, headers, timeout, validate_certs,
                args.get('url_username'), args.get('url_password'),
                force_basic_auth, follow_redirects, http_agent,
            )
        except URLError as e:
            return dict(failed=True, url=url, status=-1,
                        elapsed=int(time.time() - start),
                        msg='Request failed: %s' % to_native(e))
        except Exception as e:
            return dict(failed=True, url=url, status=-1,
                        elapsed=int(time.time() - start),
                        msg='Request failed: %s' % to_native(e))
        elapsed = int(time.time() - start)

        status = int(resp_info['status'])
        final_url = resp_info.get('url', url)

        # Lowercase + underscore every header key (dashes break templating).
        uresp = {}
        for key, value in resp_info.items():
            uresp[key.replace('-', '_').lower()] = value
        uresp['url'] = final_url
        uresp['status'] = status
        uresp['elapsed'] = elapsed
        uresp['changed'] = False
        uresp['redirected'] = final_url != url
        uresp.setdefault('msg', 'OK (%s bytes)' % len(body or b''))
        if 'location' in uresp:
            uresp['location'] = urljoin(url, uresp['location'])

        content_type = uresp.get('content_type', 'application/octet-stream')
        u_content = to_text(body, encoding=self._charset(content_type)) if body is not None else ''

        if self._maybe_json(content_type) and u_content:
            try:
                uresp['json'] = json.loads(u_content)
            except Exception:
                pass

        if return_content:
            uresp['content'] = u_content

        if status not in status_code:
            uresp['failed'] = True
            uresp['msg'] = 'Status code was %s and not %s: %s' % (
                status, status_code, uresp.get('msg', ''))

        result.update(uresp)
        return result

    def _request(self, url, method, data, headers, timeout, validate_certs,
                 url_username, url_password, force_basic_auth, follow_redirects,
                 http_agent):
        """Perform the HTTP request, returning (info_dict, body_bytes).

        A non-2xx response arrives as an HTTPError, which is itself a readable
        response — we treat it as a normal result so status_code can accept it.
        """
        try:
            r = open_url(
                url, data=data, headers=headers, method=method,
                timeout=timeout, validate_certs=validate_certs,
                url_username=url_username, url_password=url_password,
                force_basic_auth=force_basic_auth,
                follow_redirects=follow_redirects, http_agent=http_agent,
            )
            info = {'status': r.getcode(), 'url': r.geturl()}
            info.update({k: v for k, v in r.headers.items()})
            return info, r.read()
        except HTTPError as e:
            info = {'status': e.code, 'url': getattr(e, 'url', None) or url,
                    'msg': to_native(e)}
            if e.headers:
                info.update({k: v for k, v in e.headers.items()})
            try:
                body = e.read()
            except Exception:
                body = b''
            return info, body

    @staticmethod
    def _encode_body(body, body_format, headers):
        if body is None:
            return None
        has_ct = any(h.lower() == 'content-type' for h in headers)
        if body_format == 'json':
            if not has_ct:
                headers['Content-Type'] = 'application/json'
            if isinstance(body, (dict, list)):
                return json.dumps(body).encode('utf-8')
            return to_native(body).encode('utf-8')
        if body_format == 'form-urlencoded':
            if not has_ct:
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
            if isinstance(body, dict):
                return urlencode(body).encode('utf-8')
            if isinstance(body, (list, tuple)):
                return urlencode(list(body)).encode('utf-8')
            return to_native(body).encode('utf-8')
        # raw
        if isinstance(body, bytes):
            return body
        return to_native(body).encode('utf-8')

    @staticmethod
    def _maybe_json(content_type):
        sub_type = content_type.split(';')[0].split('/')[-1].lower()
        if '+' in sub_type:
            sub_type = sub_type.partition('+')[2]
        return sub_type in JSON_CANDIDATES

    @staticmethod
    def _charset(content_type):
        for part in content_type.split(';')[1:]:
            part = part.strip()
            if part.lower().startswith('charset='):
                return part.split('=', 1)[1].strip() or 'utf-8'
        return 'utf-8'
