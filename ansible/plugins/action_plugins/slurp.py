from __future__ import annotations

import base64
import errno
import os
import sys

from ansible.module_utils.common.text.converters import to_text
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local, mark_fast_result, strict_guard  # noqa: E402

display = Display()

FAST_SLURP_VERSION = '1.0'


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
            'fast_slurp v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_SLURP_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        if not _is_local(conn) or self._play_context.become:
            display.debug('fast_slurp: delegating to standard slurp module')
            strict_guard(conn, self._play_context, self._task)
            return self._execute_module(task_vars=task_vars, wrap_async=self._task.async_val)

        return mark_fast_result(self._run_local(args, result))

    def _run_local(self, args, result):
        display.debug('fast_slurp: local connection, reading file in-process')

        source = args.get('src') or args.get('path')
        if not source:
            return dict(failed=True, msg='src is required')
        source = os.path.expanduser(os.path.expandvars(source))

        try:
            with open(source, 'rb') as f:
                data = base64.b64encode(f.read())
        except OSError as e:
            if e.errno == errno.ENOENT:
                msg = 'File not found: %s' % source
            elif e.errno == errno.EACCES:
                msg = 'File is not readable: %s' % source
            elif e.errno == errno.EISDIR:
                msg = 'Source is a directory and must be a file: %s' % source
            else:
                msg = 'Unable to slurp file: %s' % source
            return dict(failed=True, msg=msg)

        result.update(dict(content=to_text(data), source=source, encoding='base64'))
        return result
