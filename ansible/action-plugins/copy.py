from __future__ import annotations

import hashlib
import os
import sys

from ansible.module_utils._text import to_bytes, to_text
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local, atomic_write, _load_builtin_action  # noqa: E402

display = Display()

FAST_COPY_VERSION = '1.0'


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
            'fast_copy v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_COPY_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        # Only optimise the content: + local path — fall back for everything else:
        # src, remote_src, directory copy, become, backup, validate
        has_content = 'content' in args
        has_src = 'src' in args
        unsupported_args = (
            'attributes', 'backup', 'group', 'owner', 'remote_src',
            'selevel', 'serole', 'setype', 'seuser', 'unsafe_writes', 'validate',
        )
        requires_standard_plugin = (
            self._play_context.become
            or self._play_context.check_mode
            or self._task.async_val
            or any(args.get(key) for key in unsupported_args)
        )

        if not _is_local(conn) or not has_content or has_src or requires_standard_plugin:
            display.debug('fast_copy: delegating to standard copy plugin')
            _Standard = _load_builtin_action('copy').ActionModule
            std = _Standard(
                self._task, conn, self._play_context,
                self._loader, self._templar, self._shared_loader_obj,
            )
            return std.run(task_vars=task_vars)

        display.debug('fast_copy: local + content path, using in-process write')

        dest = args.get('dest')
        if not dest:
            return dict(failed=True, msg='dest is required')

        force = boolean(args.get('force', True), strict=False)
        mode = args.get('mode')

        # Resolve dest
        dest = os.path.expanduser(os.path.expandvars(
            self._templar.template(dest, convert_bare=True)
        ))

        # Render content (may contain Jinja2 from task args templating)
        content = args.get('content', '')
        if content is None:
            content = ''
        b_content = to_bytes(to_text(content), errors='surrogate_or_strict')
        new_checksum = hashlib.sha1(b_content, usedforsecurity=False).hexdigest()

        # Idempotency check — no subprocess
        changed = True
        if os.path.exists(dest):
            if not force:
                return dict(changed=False, dest=dest,
                            checksum=new_checksum, msg='file exists and force=False')
            try:
                with open(dest, 'rb') as f:
                    if hashlib.sha1(f.read(), usedforsecurity=False).hexdigest() == new_checksum:
                        changed = False
            except OSError:
                pass

        if changed:
            err = atomic_write(dest, b_content, mode)
            if err:
                return dict(failed=True, msg=err)

        return dict(
            changed=changed,
            dest=dest,
            checksum=new_checksum,
            size=len(b_content),
        )
