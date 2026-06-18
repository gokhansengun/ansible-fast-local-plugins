from __future__ import annotations

import hashlib
import os
import sys

from ansible.errors import AnsibleError, AnsibleUndefinedVariable
from ansible.module_utils._text import to_bytes, to_native, to_text
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.action import ActionBase
from ansible.template import generate_ansible_template_vars, trust_as_template
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local, atomic_write, _load_builtin_action, mark_fast_result  # noqa: E402

display = Display()

FAST_TEMPLATE_VERSION = '1.0'  # bump this to confirm the right file is loaded


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
            'fast_template v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_TEMPLATE_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        unsupported_args = (
            'attributes', 'backup', 'group', 'owner',
            'selevel', 'serole', 'setype', 'seuser',
            'unsafe_writes', 'validate',
        )

        if (not _is_local(conn) or self._play_context.become
                or self._play_context.check_mode or self._task.async_val
                or any(args.get(key) for key in unsupported_args)):
            display.debug('fast_template: non-local connection, delegating to standard plugin')
            _Standard = _load_builtin_action('template').ActionModule
            std = _Standard(
                self._task, conn, self._play_context,
                self._loader, self._templar, self._shared_loader_obj,
            )
            return std.run(task_vars=task_vars)

        return mark_fast_result(self._run_local(args, task_vars))

    def _run_local(self, args, task_vars):
        display.debug('fast_template: local connection, using in-process path')

        src = args.get('src')
        dest = args.get('dest')

        if not src or not dest:
            return dict(failed=True, msg='src and dest are required')

        force = boolean(args.get('force', True), strict=False)
        mode = args.get('mode')

        env_overrides = {
            k: args[k]
            for k in ('block_start_string', 'block_end_string',
                      'variable_start_string', 'variable_end_string',
                      'comment_start_string', 'comment_end_string',
                      'trim_blocks', 'lstrip_blocks')
            if k in args
        }

        try:
            source_path = self._find_needle('templates', src)
        except AnsibleError as e:
            return dict(failed=True, msg=to_native(e))

        # Read and mark the template content as trusted for rendering.
        # In Ansible 2.19+ all template strings must carry TrustedAsTemplate
        # or the engine returns them unchanged as a security measure.
        try:
            template_data = trust_as_template(
                self._loader.get_text_file_contents(source_path)
            )
        except OSError as e:
            return dict(failed=True, msg='cannot read template: %s' % to_native(e))

        dest = os.path.expanduser(os.path.expandvars(dest))

        temp_vars = task_vars.copy()
        temp_vars.update(generate_ansible_template_vars(src, source_path, dest))

        # copy_with_new_env is the 2.19-approved way to inject variables and
        # environment overrides into a templar without touching internal state.
        # searchpath lets Jinja2 resolve {% import %} / {% include %} / {% from %}
        # relative to the directory that contains the template being rendered.
        data_templar = self._templar.copy_with_new_env(
            available_variables=temp_vars,
            searchpath=[os.path.dirname(source_path)],
        )

        try:
            rendered = data_templar.template(
                template_data,
                preserve_trailing_newlines=True,
                escape_backslashes=False,
                overrides=env_overrides or None,
            )
        except AnsibleUndefinedVariable as e:
            return dict(failed=True, msg='undefined variable: %s' % to_native(e))

        if rendered is None:
            rendered = ''

        b_rendered = to_bytes(to_text(rendered), errors='surrogate_or_strict')
        new_checksum = hashlib.sha1(b_rendered, usedforsecurity=False).hexdigest()

        changed = True
        if os.path.exists(dest):
            if not force:
                return dict(changed=False, dest=dest, msg='file exists and force=False')
            try:
                with open(dest, 'rb') as f:
                    if hashlib.sha1(f.read(), usedforsecurity=False).hexdigest() == new_checksum:
                        changed = False
            except OSError:
                pass

        if changed:
            err = atomic_write(dest, b_rendered, mode)
            if err:
                return dict(failed=True, msg=err)

        return dict(changed=changed, dest=dest, checksum=new_checksum, src=source_path)
