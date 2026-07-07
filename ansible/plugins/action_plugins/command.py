from __future__ import annotations

import datetime
import os
import shlex
import subprocess
import sys

from ansible.module_utils.common.text.converters import to_bytes, to_native, to_text
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local, _load_builtin_action, mark_fast_result, resolve_environment, strict_guard  # noqa: E402

display = Display()

FAST_COMMAND_VERSION = '1.0'


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
            'fast_command v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_COMMAND_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        # Fall back for: non-local, become, async, or check_mode. `environment:`
        # is handled in-process (see _run_local), so it no longer forces a fallback.
        if (not _is_local(conn) or self._play_context.become
                or self._task.async_val or self._play_context.check_mode):
            display.debug('fast_command: delegating to standard command plugin')
            strict_guard(conn, self._play_context, self._task)
            _Standard = _load_builtin_action('command').ActionModule
            std = _Standard(
                self._task, conn, self._play_context,
                self._loader, self._templar, self._shared_loader_obj,
            )
            return std.run(task_vars=task_vars)

        return mark_fast_result(self._run_local(args, result))

    def _run_local(self, args, result):
        display.debug('fast_command: local connection, running in-process via subprocess')

        # argv takes precedence: it is already a pre-split list.
        # Otherwise split _raw_params / cmd with shlex so the OS receives
        # discrete arguments rather than a shell-interpreted string.
        argv = args.get('argv')
        if argv:
            cmd = list(argv)
        else:
            raw = args.get('_raw_params') or args.get('cmd', '')
            if not raw:
                return dict(failed=True, msg='no command given')
            cmd = shlex.split(raw)

        # chdir (often via `args: {chdir: ...}`) is a path arg, so expand ~ and env
        # vars to match stock; fail clearly on a missing dir.
        chdir = args.get('chdir')
        if chdir:
            chdir = os.path.expanduser(os.path.expandvars(chdir))
            if not os.path.isdir(chdir):
                return dict(failed=True, rc=257,
                            msg='Unable to change directory before execution: '
                                '%s does not exist or is not a directory' % chdir)
        creates = args.get('creates')
        removes = args.get('removes')
        stdin_data = args.get('stdin')
        stdin_add_newline = boolean(args.get('stdin_add_newline', True), strict=False)
        strip_empty_ends = boolean(args.get('strip_empty_ends', True), strict=False)

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

        # Apply `environment:` on top of the inherited process env (later levels
        # win, matching ansible). env=None inherits os.environ unchanged.
        env_overrides = resolve_environment(self._task, self._templar)
        run_env = None
        if env_overrides:
            run_env = os.environ.copy()
            run_env.update(env_overrides)

        start = datetime.datetime.now()
        try:
            proc = subprocess.run(
                cmd,
                shell=False,  # intentional: this plugin implements Ansible's command module
                cwd=chdir,
                input=stdin_bytes,
                capture_output=True,
                env=run_env,
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
        # The command actually ran in-process: mark it even when rc != 0 (the
        # non-zero exit is the command's, not a fallback), so a `failed_when:`-
        # rescued failure isn't misreported as a fallback.
        return mark_fast_result(result, force=True)
