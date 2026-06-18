from __future__ import annotations

import os
import re
import sys
import time

from ansible.module_utils.common.text.converters import to_bytes, to_native
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local, atomic_write, mark_fast_result, strict_guard  # noqa: E402

display = Display()

FAST_LINEINFILE_VERSION = '1.0'

# Arguments the in-process fast path honours. Anything else — file attributes
# (mode/owner/group/se*), validate, attributes, unsafe_writes — triggers a
# fallback to the standard lineinfile module so its behaviour is preserved
# exactly rather than silently dropped.
_SUPPORTED_ARGS = frozenset({
    'path', 'dest', 'destfile', 'name', 'state', 'regexp', 'regex',
    'search_string', 'line', 'value', 'insertafter', 'insertbefore',
    'backrefs', 'create', 'backup', 'firstmatch',
})


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
            'fast_lineinfile v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_LINEINFILE_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        unsupported = set(args) - _SUPPORTED_ARGS
        if (not _is_local(conn) or self._play_context.become
                or self._play_context.check_mode or unsupported):
            if unsupported:
                display.debug('fast_lineinfile: unsupported args %r, delegating to module'
                              % sorted(unsupported))
            else:
                display.debug('fast_lineinfile: delegating to standard module')
            strict_guard(conn, self._play_context, self._task)
            return self._execute_module(task_vars=task_vars, wrap_async=self._task.async_val)

        return mark_fast_result(self._run_local(args, result))

    def _run_local(self, args, result):
        display.debug('fast_lineinfile: local connection, editing file in-process')

        path = args.get('path') or args.get('dest') or args.get('destfile') or args.get('name')
        if not path:
            return dict(failed=True, msg='path is required')
        path = os.path.expanduser(os.path.expandvars(path))

        if os.path.isdir(to_bytes(path)):
            return dict(failed=True, rc=256, msg='Path %s is a directory !' % path)

        state = args.get('state', 'present')
        regexp = args.get('regexp', args.get('regex'))
        search_string = args.get('search_string')
        line = args.get('line', args.get('value'))
        backrefs = boolean(args.get('backrefs', False), strict=False)
        create = boolean(args.get('create', False), strict=False)
        backup = boolean(args.get('backup', False), strict=False)
        firstmatch = boolean(args.get('firstmatch', False), strict=False)
        insertafter = args.get('insertafter')
        insertbefore = args.get('insertbefore')

        if state == 'present':
            if backrefs and regexp is None:
                return dict(failed=True, msg='regexp is required with backrefs=true')
            if line is None:
                return dict(failed=True, msg='line is required with state=present')
            if insertbefore is None and insertafter is None:
                insertafter = 'EOF'
            return self._present(result, path, regexp, search_string, line,
                                 insertafter, insertbefore, create, backup,
                                 backrefs, firstmatch)
        return self._absent(result, path, regexp, search_string, line, backup)

    # ------------------------------------------------------------------ #
    # Ported from ansible.modules.lineinfile (present/absent algorithm).   #
    # ------------------------------------------------------------------ #
    def _present(self, result, dest, regexp, search_string, line, insertafter,
                 insertbefore, create, backup, backrefs, firstmatch):
        b_dest = to_bytes(dest)
        file_existed = os.path.exists(b_dest)
        if not file_existed:
            if not create:
                return dict(failed=True, rc=257, msg='Destination %s does not exist !' % dest)
            b_destpath = os.path.dirname(b_dest)
            if b_destpath and not os.path.exists(b_destpath):
                try:
                    os.makedirs(b_destpath)
                except Exception as e:
                    return dict(failed=True, msg='Error creating %s (%s)'
                                % (to_native(b_destpath), to_native(e)))
            b_lines = []
        else:
            with open(b_dest, 'rb') as f:
                b_lines = f.readlines()

        if regexp is not None:
            bre_m = re.compile(to_bytes(regexp))

        if insertafter not in (None, 'BOF', 'EOF'):
            bre_ins = re.compile(to_bytes(insertafter))
        elif insertbefore not in (None, 'BOF'):
            bre_ins = re.compile(to_bytes(insertbefore))
        else:
            bre_ins = None

        # index[0]: line num where regexp/search/exact line matched.
        # index[1]: line num where insertafter/insertbefore matched.
        index = [-1, -1]
        match = None
        exact_line_match = False
        b_line = to_bytes(line)

        # 1. regexp matches a line -> replace it (insertafter/before ignored).
        if regexp is not None:
            for lineno, b_cur_line in enumerate(b_lines):
                match_found = bre_m.search(b_cur_line)
                if match_found:
                    index[0] = lineno
                    match = match_found
                    if firstmatch:
                        break

        # 2. search_string matches a line -> replace it.
        if search_string is not None:
            for lineno, b_cur_line in enumerate(b_lines):
                match_found = to_bytes(search_string) in b_cur_line
                if match_found:
                    index[0] = lineno
                    match = match_found
                    if firstmatch:
                        break

        # 3. No regexp/search match: look for the exact line and the
        #    insertafter/insertbefore anchor.
        if not match:
            for lineno, b_cur_line in enumerate(b_lines):
                if b_line == b_cur_line.rstrip(b'\r\n'):
                    index[0] = lineno
                    exact_line_match = True
                elif bre_ins is not None and bre_ins.search(b_cur_line):
                    if insertafter:
                        index[1] = lineno + 1
                        if firstmatch:
                            break
                    if insertbefore:
                        index[1] = lineno
                        if firstmatch:
                            break

        msg = ''
        changed = False
        b_linesep = to_bytes(os.linesep)

        if index[0] != -1:
            if backrefs and match:
                b_new_line = match.expand(b_line)
            else:
                b_new_line = b_line

            if not b_new_line.endswith(b_linesep):
                b_new_line += b_linesep

            # No regexp/search match, but an insertafter/insertbefore anchor was
            # found: insert the line relative to that anchor.
            if (regexp, search_string, match) == (None, None, None) and not exact_line_match:
                if insertafter and insertafter != 'EOF':
                    if b_lines and b_lines[-1][-1:] not in (b'\n', b'\r'):
                        b_lines[-1] = b_lines[-1] + b_linesep
                    if len(b_lines) == index[1]:
                        if b_lines[index[1] - 1].rstrip(b'\r\n') != b_line:
                            b_lines.append(b_line + b_linesep)
                            msg = 'line added'
                            changed = True
                    elif b_lines[index[1]].rstrip(b'\r\n') != b_line:
                        b_lines.insert(index[1], b_line + b_linesep)
                        msg = 'line added'
                        changed = True
                elif insertbefore and insertbefore != 'BOF':
                    if index[1] <= 0:
                        if b_lines[index[1]].rstrip(b'\r\n') != b_line:
                            b_lines.insert(index[1], b_line + b_linesep)
                            msg = 'line added'
                            changed = True
                    elif b_lines[index[1] - 1].rstrip(b'\r\n') != b_line:
                        b_lines.insert(index[1], b_line + b_linesep)
                        msg = 'line added'
                        changed = True
            elif b_lines[index[0]] != b_new_line:
                b_lines[index[0]] = b_new_line
                msg = 'line replaced'
                changed = True
        elif backrefs:
            # regexp given with backrefs but no match: leave the file untouched.
            pass
        elif insertbefore == 'BOF' or insertafter == 'BOF':
            b_lines.insert(0, b_line + b_linesep)
            msg = 'line added'
            changed = True
        elif insertafter == 'EOF' or index[1] == -1:
            if b_lines and b_lines[-1][-1:] not in (b'\n', b'\r'):
                b_lines.append(b_linesep)
            b_lines.append(b_line + b_linesep)
            msg = 'line added'
            changed = True
        elif insertafter and index[1] != -1:
            if len(b_lines) == index[1]:
                if b_lines[index[1] - 1].rstrip(b'\r\n') != b_line:
                    b_lines.append(b_line + b_linesep)
                    msg = 'line added'
                    changed = True
            elif b_line != b_lines[index[1]].rstrip(b'\n\r'):
                b_lines.insert(index[1], b_line + b_linesep)
                msg = 'line added'
                changed = True
        else:
            b_lines.insert(index[1], b_line + b_linesep)
            msg = 'line added'
            changed = True

        backupdest = ''
        if changed:
            if backup and file_existed:
                backupdest = self._backup_local(dest)
            err = self._write(dest, b_lines)
            if err:
                return dict(failed=True, msg=err)

        result.update(dict(changed=changed, msg=msg, backup=backupdest))
        return result

    def _absent(self, result, dest, regexp, search_string, line, backup):
        b_dest = to_bytes(dest)
        if not os.path.exists(b_dest):
            result.update(dict(changed=False, found=0, msg='file not present', backup=''))
            return result

        with open(b_dest, 'rb') as f:
            b_lines = f.readlines()

        if regexp is not None:
            bre_c = re.compile(to_bytes(regexp))
        found = []
        b_line = to_bytes(line) if line is not None else None

        def matcher(b_cur_line):
            if regexp is not None:
                match_found = bre_c.search(b_cur_line)
            elif search_string is not None:
                match_found = to_bytes(search_string) in b_cur_line
            else:
                match_found = b_line == b_cur_line.rstrip(b'\r\n')
            if match_found:
                found.append(b_cur_line)
            return not match_found

        b_lines = [l for l in b_lines if matcher(l)]
        changed = len(found) > 0

        backupdest = ''
        msg = ''
        if changed:
            if backup:
                backupdest = self._backup_local(dest)
            err = self._write(dest, b_lines)
            if err:
                return dict(failed=True, msg=err)
            msg = '%s line(s) removed' % len(found)

        result.update(dict(changed=changed, found=len(found), msg=msg, backup=backupdest))
        return result

    @staticmethod
    def _write(dest, b_lines):
        """Atomically write b_lines to dest. Returns an error string or None.

        atomic_write keeps an existing file's perms and applies the umask
        default to a brand-new file (matching the standard module's atomic_move).
        """
        return atomic_write(dest, b''.join(b_lines))

    @staticmethod
    def _backup_local(fn):
        """Create a timestamped backup copy, mirroring module.backup_local()."""
        ext = time.strftime('%Y-%m-%d@%H:%M:%S~', time.localtime(time.time()))
        backupdest = '%s.%s.%s' % (fn, os.getpid(), ext)
        with open(to_bytes(fn), 'rb') as src, open(to_bytes(backupdest), 'wb') as dst:
            dst.write(src.read())
        return backupdest
