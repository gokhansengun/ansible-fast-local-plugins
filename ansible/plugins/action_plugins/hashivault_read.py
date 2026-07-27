from __future__ import annotations

import os
import sys

from ansible.module_utils.common.text.converters import to_native
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local, mark_fast_result, strict_guard  # noqa: E402

display = Display()

FAST_HASHIVAULT_READ_VERSION = '1.1'

# Arguments the fast path honours. The connection/TLS/namespace set mirrors stock
# hashivault_client(); the read-specific set mirrors hashivault_read's own argspec
# additions. Everything else — the non-token login credentials (`username`,
# `password`, `role_id`, `secret_id`, `aws_header`, `login_mount_point`) — needs a
# real auth flow we do not reproduce, so it triggers a fallback instead of being
# silently ignored (which would authenticate as the wrong identity). `authtype` is
# supported only for its default value, see _run()'s conditional gate.
#
# Note `validate_certs` is deliberately absent: it is not in stock's argspec at all
# (stock uses `verify` + `ca_cert`), so it must fall back and let the stock module
# reject it as an unsupported parameter.
#
# Stock's argspec declares no aliases, so there is no _ARG_ALIASES map here.
_SUPPORTED_ARGS = frozenset({
    'url', 'token', 'authtype', 'namespace', 'timeout',
    'ca_cert', 'ca_path', 'client_cert', 'client_key', 'verify',
    'mount_point', 'secret', 'secret_version', 'version', 'key', 'default',
})

# The only auth type the fast path performs (client.token = token). Any other
# value needs client.auth.<method>.login(), so the task delegates to stock.
_SUPPORTED_AUTHTYPE = 'token'


def _default_token():
    """Stock's hashivault_default_token fallback: env var, then ~/.vault-token."""
    if 'VAULT_TOKEN' in os.environ:
        return os.environ['VAULT_TOKEN']
    token_file = os.path.expanduser('~/.vault-token')
    if os.path.exists(token_file):
        try:
            with open(token_file, 'r') as f:
                return f.read().strip()
        except OSError:
            return ''
    return ''


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
            'fast_hashivault_read v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_HASHIVAULT_READ_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        unsupported = set(args) - _SUPPORTED_ARGS
        authtype = args.get('authtype')
        unsupported_authtype = (
            authtype is not None and str(authtype).strip().lower() != _SUPPORTED_AUTHTYPE
        )

        if not _is_local(conn) or unsupported or unsupported_authtype:
            extra_reasons = []
            if unsupported:
                display.debug('fast_hashivault_read: unsupported args %r, delegating'
                              % sorted(unsupported))
                extra_reasons.append('unsupported arguments: %s' % ', '.join(sorted(unsupported)))
            if unsupported_authtype:
                extra_reasons.append('authtype: %s' % authtype)
            if not extra_reasons:
                display.debug('fast_hashivault_read: non-local, delegating to module')
            strict_guard(conn, self._play_context, self._task, extra_reasons=extra_reasons)
            return self._execute_module(task_vars=task_vars, wrap_async=self._task.async_val)

        return mark_fast_result(self._run_local(args, result))

    def _run_local(self, args, result):
        display.debug('fast_hashivault_read: local connection, calling hvac in-process')

        try:
            import hvac
            from hvac.exceptions import InvalidPath
        except ImportError:
            return dict(failed=True, rc=1, msg='hvac Python library is required')

        secret = args.get('secret')
        if not secret:
            return dict(failed=True, rc=1, msg='secret is required')

        try:
            version = int(args.get('version', 1))
        except (TypeError, ValueError):
            return dict(failed=True, rc=1, msg='version must be an integer')

        mount_point = args.get('mount_point', 'secret')
        key = args.get('key')
        default = args.get('default')

        # Stock treats a leading slash as "absolute path", dropping the mount point.
        if secret.startswith('/'):
            secret = secret.lstrip('/')
            mount_point = ''
        secret_path = '%s/%s' % (mount_point, secret) if mount_point else secret

        try:
            client = hvac.Client(**self._client_kwargs(args))
            if version == 2:
                secret_version = args.get('secret_version')
                if secret_version is not None:
                    secret_version = int(secret_version)  # stock argspec: type=int
                raw = client.secrets.kv.v2.read_secret_version(
                    path=secret,
                    mount_point=mount_point,
                    version=secret_version,
                )
            else:
                raw = client.secrets.kv.v1.read_secret(
                    path=secret,
                    mount_point=mount_point,
                )
        except InvalidPath:
            # A missing path is not an error to stock — it maps to "no response" and
            # is reported by the not-found branch below. Catching it with the generic
            # handler instead would emit hvac's opaque 'None, on get <url>' text, and
            # callers keying on stock's "is not in vault" wording (the read-or-create
            # idiom) would treat a first run as a hard failure.
            raw = None
        except Exception as e:
            return dict(failed=True, rc=1, msg=u'Error %s(%s) reading %s'
                        % (e.__class__.__name__, to_native(e), secret_path))

        if not raw:
            if default is not None:
                result.update(dict(changed=False, rc=0, value=default))
                return result
            return dict(failed=True, rc=1,
                        msg=u'Secret %s is not in vault' % secret_path)

        if version == 2:
            data = raw.get('data', {})
            metadata = data.get('metadata', {})
            data = data.get('data', {})
        else:
            data = raw['data']
            metadata = {}

        if key and key not in data:
            if default is not None:
                result.update(dict(changed=False, rc=0, value=default))
                return result
            return dict(failed=True, rc=1,
                        msg=u'Key %s is not in secret %s' % (key, secret_path))

        value = data[key] if key else data

        result.update(dict(
            changed=False,
            rc=0,
            value=value,
            metadata=metadata,
            # raw/data are fast-path extras stock does not return; `data` holds the
            # secret's own key/value mapping on both kv versions.
            raw=raw,
            data=data,
        ))
        # Stock omits these keys entirely when the response has no such field,
        # rather than substituting a default.
        for field in ('lease_duration', 'lease_id', 'renewable', 'wrap_info'):
            field_value = raw.get(field, None)
            if field_value is not None:
                result[field] = field_value
        return result

    @staticmethod
    def _client_kwargs(args):
        """Build hvac.Client kwargs the way stock hashivault_client() does."""
        ca_cert = args.get('ca_cert') or os.environ.get('VAULT_CACERT', '')
        ca_path = args.get('ca_path') or os.environ.get('VAULT_CAPATH', '')

        check_verify = args.get('verify')
        if check_verify is None:
            # Stock argspec default: not os.environ.get('VAULT_SKIP_VERIFY', '')
            check_verify = not os.environ.get('VAULT_SKIP_VERIFY', '')
        else:
            check_verify = boolean(check_verify, strict=False)  # stock argspec: type=bool
        if check_verify:
            verify = ca_cert or ca_path or check_verify
        else:
            verify = check_verify

        client_cert = args.get('client_cert') or os.environ.get('VAULT_CLIENT_CERT', '')
        client_key = args.get('client_key') or os.environ.get('VAULT_CLIENT_KEY', '')

        return dict(
            url=args.get('url') or os.environ.get('VAULT_ADDR', ''),
            token=args.get('token') or _default_token(),
            cert=(client_cert, client_key),
            verify=verify,
            namespace=args.get('namespace') or os.environ.get('VAULT_NAMESPACE') or None,
            timeout=int(args.get('timeout', 30)),  # stock argspec: type=int
        )
