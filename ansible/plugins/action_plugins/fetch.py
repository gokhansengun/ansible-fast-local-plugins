from __future__ import annotations

import os
import shutil
import sys

from ansible.module_utils.common.text.converters import to_bytes
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display
from ansible.utils.hashing import checksum, md5
from ansible.utils.path import makedirs_safe

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local, _load_builtin_action, mark_fast_result, strict_guard  # noqa: E402

display = Display()

FAST_FETCH_VERSION = '1.1'


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
            'fast_fetch v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_FETCH_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        # The standard fetch is an action plugin (not a module) that forks the
        # slurp/stat modules to read the source. For a local connection the
        # source is right here, so we read it directly. Fall back for non-local,
        # become or check-mode.
        if (not _is_local(conn) or self._play_context.become
                or self._play_context.check_mode):
            display.debug('fast_fetch: delegating to standard fetch action plugin')
            strict_guard(conn, self._play_context, self._task)
            _Standard = _load_builtin_action('fetch').ActionModule
            std = _Standard(
                self._task, conn, self._play_context,
                self._loader, self._templar, self._shared_loader_obj,
            )
            return std.run(task_vars=task_vars)

        return mark_fast_result(self._run_local(args, task_vars, result))

    def _run_local(self, args, task_vars, result):
        display.debug('fast_fetch: local connection, copying file in-process')

        source = args.get('src')
        dest = args.get('dest')
        flat = boolean(args.get('flat', False), strict=False)
        fail_on_missing = boolean(args.get('fail_on_missing', True), strict=False)
        validate_checksum = boolean(args.get('validate_checksum', True), strict=False)

        if not isinstance(source, str) or not isinstance(dest, str):
            return dict(failed=True, msg='src and dest are required')

        source = os.path.expanduser(os.path.expandvars(source))

        if not os.path.exists(source):
            result['changed'] = False
            result['file'] = source
            if fail_on_missing:
                result['failed'] = True
                result['msg'] = 'the remote file does not exist, not transferring, ignored'
            else:
                result['msg'] = 'the remote file does not exist, not transferring, ignored'
            return result

        if os.path.isdir(to_bytes(source)):
            result['changed'] = False
            result['msg'] = 'remote file is a directory, fetch cannot work on directories'
            if fail_on_missing:
                result['failed'] = True
            else:
                result['msg'] += ', not transferring, ignored'
                del result['changed']
            return result

        remote_checksum = checksum(source)

        # Calculate the destination path exactly as the standard fetch does.
        if flat:
            if os.path.isdir(to_bytes(dest)) and not dest.endswith(os.sep):
                return dict(failed=True, msg='dest is an existing directory, use a trailing '
                                             'slash if you want to fetch src into that directory')
            if dest.endswith(os.sep):
                dest = os.path.join(dest, os.path.basename(source))
                if os.path.isdir(to_bytes(dest)):
                    return dict(failed=True, msg="calculated dest '%s' is an existing directory, "
                                                 'use another path' % dest)
            if not dest.startswith('/'):
                dest = self._loader.path_dwim(dest)
        else:
            target_name = task_vars.get('inventory_hostname') or self._play_context.remote_addr
            dest = '%s/%s/%s' % (self._loader.path_dwim(dest), target_name, source)
        dest = os.path.normpath(dest)

        local_checksum = checksum(dest)

        if remote_checksum != local_checksum:
            makedirs_safe(os.path.dirname(dest))
            try:
                # Stream the copy: reading the whole file first costs RSS equal to its
                # size, which OOM-kills the controller on multi-GB database dumps.
                shutil.copyfile(to_bytes(source), to_bytes(dest))
            except OSError as e:
                return dict(failed=True, msg='Failed to fetch the file: %s' % e)
            new_checksum = checksum(dest)
            try:
                new_md5 = md5(dest)
            except ValueError:
                new_md5 = None
            if validate_checksum and new_checksum != remote_checksum:
                result.update(dict(failed=True, md5sum=new_md5, msg='checksum mismatch',
                                   file=source, dest=dest, remote_md5sum=None,
                                   checksum=new_checksum, remote_checksum=remote_checksum))
            else:
                result.update(dict(changed=True, md5sum=new_md5, file=source, dest=dest,
                                   remote_md5sum=None, checksum=new_checksum,
                                   remote_checksum=remote_checksum))
        else:
            try:
                local_md5 = md5(dest)
            except ValueError:
                local_md5 = None
            result.update(dict(changed=False, md5sum=local_md5, file=source, dest=dest,
                               checksum=local_checksum))

        return result
