from __future__ import annotations

import os
import sys

from ansible.module_utils._text import to_native
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local  # noqa: E402

display = Display()

FAST_HASHIVAULT_READ_VERSION = '1.0'


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

        if not _is_local(conn):
            display.debug('fast_hashivault_read: non-local, delegating to module')
            return self._execute_module(task_vars=task_vars, wrap_async=self._task.async_val)

        display.debug('fast_hashivault_read: local connection, calling hvac in-process')

        try:
            import hvac
        except ImportError:
            return dict(failed=True, msg='hvac Python library is required')

        url = args.get('url') or os.environ.get('VAULT_ADDR', 'https://127.0.0.1:8200')
        token = args.get('token') or os.environ.get('VAULT_TOKEN')
        mount_point = args.get('mount_point', 'secret')
        secret = args.get('secret')
        key = args.get('key')
        try:
            version = int(args.get('version', 1))
        except (TypeError, ValueError):
            return dict(failed=True, msg='version must be an integer')
        validate_certs = args.get('validate_certs', True)
        ca_cert = args.get('ca_cert')

        if not secret:
            return dict(failed=True, msg='secret is required')

        try:
            client = hvac.Client(
                url=url,
                token=token,
                verify=ca_cert if ca_cert else validate_certs,
            )

            if version == 2:
                secret_version = args.get('secret_version')
                raw = client.secrets.kv.v2.read_secret_version(
                    path=secret,
                    mount_point=mount_point,
                    version=secret_version,
                )
                data = raw.get('data', {})
                value = data.get('data', {})
                metadata = data.get('metadata', {})
            else:
                raw = client.secrets.kv.v1.read_secret(
                    path=secret,
                    mount_point=mount_point,
                )
                data = raw.get('data', {})
                value = data
                metadata = {}

        except Exception as e:
            return dict(failed=True, msg='vault read failed: %s' % to_native(e))

        if key:
            if key not in value:
                display.warning('hashivault_read: key %r not found in secret %r' % (key, secret))
            value = value.get(key)

        result.update(dict(
            changed=False,
            value=value,
            raw=raw,
            data=data,
            metadata=metadata,
            lease_duration=raw.get('lease_duration', 0),
            lease_id=raw.get('lease_id', ''),
            renewable=raw.get('renewable', False),
        ))
        return result
