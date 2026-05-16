from __future__ import annotations

import os
import sys
import tempfile as _tempfile

from ansible.module_utils._text import to_native
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local  # noqa: E402

display = Display()

FAST_TEMPFILE_VERSION = '1.0'


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
            'fast_tempfile v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_TEMPFILE_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        if not _is_local(conn) or self._play_context.become or self._play_context.check_mode:
            display.debug('fast_tempfile: non-local connection, delegating to module')
            return self._execute_module(task_vars=task_vars, wrap_async=self._task.async_val)

        display.debug('fast_tempfile: local connection, using in-process tempfile')

        state = args.get('state', 'file')
        base = args.get('path') or _tempfile.gettempdir()
        prefix = args.get('prefix', 'ansible.')
        suffix = args.get('suffix', '')

        base = os.path.expanduser(os.path.expandvars(base))

        try:
            if state == 'directory':
                path = _tempfile.mkdtemp(prefix=prefix, suffix=suffix, dir=base)
            else:
                fd, path = _tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=base)
                os.close(fd)
        except Exception as e:
            return dict(failed=True, msg='failed to create tempfile: %s' % to_native(e))

        result.update(dict(changed=True, path=path, state=state))
        return result
