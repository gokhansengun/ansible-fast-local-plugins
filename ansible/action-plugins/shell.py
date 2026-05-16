from __future__ import annotations

import datetime
import os
import subprocess
import sys

from ansible.module_utils._text import to_bytes, to_native, to_text
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local, _load_builtin_action  # noqa: E402

display = Display()

FAST_SHELL_VERSION = '1.0'


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
            'fast_shell v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_SHELL_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        # Fall back for: non-local, become, async, check_mode, or non-empty environment.
        # ansible-core 2.19+ sets task.environment=[{}] by default (empty dict in a list),
        # which is truthy but means "no extra vars" — only fall back for genuinely populated
        # environment dicts.
        real_env = any(self._task.environment or [])
        if (not _is_local(conn) or self._play_context.become
                or self._task.async_val or self._play_context.check_mode
                or real_env):
            display.debug('fast_shell: delegating to standard shell plugin')
            _Standard = _load_builtin_action('shell').ActionModule
            std = _Standard(
                self._task, conn, self._play_context,
                self._loader, self._templar, self._shared_loader_obj,
            )
            return std.run(task_vars=task_vars)

        display.debug('fast_shell: local connection, running in-process via subprocess')

        # _raw_params is set when using "shell: <cmd>" free-form syntax;
        # cmd is set when using "shell:\n  cmd: <cmd>" dict syntax.
        cmd = args.get('_raw_params') or args.get('cmd', '')
        if not cmd:
            return dict(failed=True, msg='no command given')

        executable = args.get('executable') or '/bin/sh'
        chdir = args.get('chdir')
        creates = args.get('creates')
        removes = args.get('removes')
        stdin_data = args.get('stdin')
        stdin_add_newline = args.get('stdin_add_newline', True)
        strip_empty_ends = args.get('strip_empty_ends', True)

        if creates:
            creates = os.path.expanduser(os.path.expandvars(creates))
            if os.path.exists(creates):
                return dict(changed=False, stdout='', stderr='', rc=0, cmd=cmd,
                            skipped=True, msg='skipped, since %s exists' % creates)

        if removes:
            removes = os.path.expanduser(os.path.expandvars(removes))
            if not os.path.exists(removes):
                return dict(changed=False, stdout='', stderr='', rc=0, cmd=cmd,
                            skipped=True, msg='skipped, since %s does not exist' % removes)

        stdin_bytes = None
        if stdin_data is not None:
            s = to_text(stdin_data)
            if stdin_add_newline and not s.endswith('\n'):
                s += '\n'
            stdin_bytes = to_bytes(s, errors='surrogate_or_strict')

        start = datetime.datetime.now()
        try:
            proc = subprocess.run(
                cmd,
                shell=True,  # intentional: this plugin implements Ansible's shell module
                executable=executable,
                cwd=chdir,
                input=stdin_bytes,
                capture_output=True,
            )
        except Exception as e:
            return dict(failed=True, msg='error running command: %s' % to_native(e))
        end = datetime.datetime.now()

        stdout = to_text(proc.stdout, errors='surrogate_or_strict')
        stderr = to_text(proc.stderr, errors='surrogate_or_strict')

        if strip_empty_ends:
            stdout = stdout.rstrip('\r\n')
            stderr = stderr.rstrip('\r\n')

        failed = proc.returncode != 0
        result.update(dict(
            changed=True,
            stdout=stdout,
            stderr=stderr,
            stdout_lines=stdout.splitlines(),
            stderr_lines=stderr.splitlines(),
            rc=proc.returncode,
            cmd=cmd,
            start=str(start),
            end=str(end),
            delta=str(end - start),
            msg='' if not failed else 'non-zero return code',
            failed=failed,
        ))
        return result
