from __future__ import annotations

import os
import subprocess
import sys
import time

from ansible.module_utils._text import to_native
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local, mark_fast_result  # noqa: E402

display = Display()

FAST_FIND_NEXT_HELM_RELEASE_NUMBER_VERSION = '1.0'


def _get_current_version(release_name, namespace):
    proc = subprocess.run(
        [
            'kubectl', 'get', 'secrets',
            '-n', namespace,
            '-l', 'name=%s,status=deployed' % release_name,
            '-o', 'custom-columns=VERSION:metadata.labels.version',
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())

    lines = [line for line in proc.stdout.splitlines()[1:] if line.strip()]

    if not lines:
        return None

    versions = []
    for line in lines:
        try:
            versions.append(int(line.strip()))
        except ValueError:
            raise RuntimeError('unexpected non-integer version label: %r' % line.strip())
    return sorted(versions, reverse=True)[0]


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
            'fast_find_next_helm_release_number v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_FIND_NEXT_HELM_RELEASE_NUMBER_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        if not _is_local(conn):
            display.debug('fast_find_next_helm_release_number: non-local, delegating to module')
            return self._execute_module(task_vars=task_vars, wrap_async=self._task.async_val)

        return mark_fast_result(self._run_local(args, result))

    def _run_local(self, args, result):
        display.debug('fast_find_next_helm_release_number: local connection, running in-process')

        release_name = args.get('release_name')
        namespace = args.get('namespace')

        if not release_name or not namespace:
            return dict(failed=True, msg='release_name and namespace are required')

        try:
            current_version = _get_current_version(release_name, namespace)
        except Exception as e:
            return dict(failed=True, msg='failed to query helm secrets: %s' % to_native(e))

        result.update(dict(
            changed=False,
            current_version=current_version if current_version is not None else 0,
            next_version=current_version + 1 if current_version is not None else 1,
            modified_at=int(time.time()),
        ))
        return result
