from __future__ import annotations

import hashlib
import os
import sys

from ansible.errors import AnsibleError
from ansible.module_utils.common.text.converters import to_bytes, to_text
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

# Make sibling utilities importable without requiring action_plugins on sys.path.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from _action_utils import _is_local, _parse_mode, atomic_write, _load_builtin_action, mark_fast_result, strict_guard  # noqa: E402

display = Display()

FAST_COPY_VERSION = '1.2'

# Args whose presence forces the standard plugin (features the fast path doesn't
# reimplement: ownership, SELinux, backups, validation, remote-side source, ...).
_UNSUPPORTED_ARGS = (
    'attributes', 'backup', 'group', 'owner', 'remote_src',
    'selevel', 'serole', 'setype', 'seuser', 'unsafe_writes', 'validate',
)


def _tree_has_symlink(root):
    """True if `root`, or anything under it, is a symlink.

    os.walk (followlinks=False) lists a symlinked dir as a name but does not
    descend into it, so each entry is checked individually. The fast path can't
    faithfully reproduce stock's local_follow handling of symlink trees, so any
    symlink forces a fallback.
    """
    if os.path.islink(root):
        return True
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            if os.path.islink(os.path.join(base, name)):
                return True
    return False


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

        # The fast path handles a local connection writing either inline `content`
        # or a single local-file `src` to `dest`. Everything else delegates. We
        # collect plugin-specific reasons so AFLP_STRICT names the real trigger
        # rather than the generic "unsupported arguments". (become / check_mode /
        # async / non-local are detected by strict_guard itself, so they force a
        # fallback here but are not added to extra_reasons.)
        has_content = 'content' in args
        has_src = 'src' in args
        mode = args.get('mode')

        fallback = (
            not _is_local(conn)
            or self._play_context.become
            or self._play_context.check_mode
            or bool(self._task.async_val)
        )
        extra_reasons = []

        unsupported = [k for k in _UNSUPPORTED_ARGS if args.get(k)]
        if unsupported:
            fallback = True
            extra_reasons.append('unsupported arg(s): ' + ', '.join(sorted(unsupported)))
        if has_content and has_src:
            fallback = True
            extra_reasons.append('both content and src set')
        elif not has_content and not has_src:
            fallback = True
            extra_reasons.append('neither content nor src set')
        if mode == 'preserve':
            fallback = True
            extra_reasons.append('mode=preserve')
        if args.get('local_follow') is False:
            fallback = True
            extra_reasons.append('local_follow=false')

        # Resolve a `src` on the controller (== target for a local connection).
        # A directory source is recursively copied in-process for the common case;
        # the trickier shapes (symlinks, mode/directory_mode=preserve) fall back.
        resolved_src = None
        if has_src and not has_content and not fallback:
            try:
                resolved_src = self._find_needle('files', args['src'])
            except AnsibleError as e:
                fallback = True
                extra_reasons.append('source not found (%s)' % to_text(e))
            else:
                if os.path.isdir(resolved_src):
                    reason = self._dir_unsupported(args, resolved_src)
                    if reason:
                        fallback = True
                        extra_reasons.append(reason)
                        resolved_src = None

        if fallback:
            display.debug('fast_copy: delegating to standard copy plugin')
            strict_guard(conn, self._play_context, self._task,
                         extra_reasons=extra_reasons or None)
            _Standard = _load_builtin_action('copy').ActionModule
            std = _Standard(
                self._task, conn, self._play_context,
                self._loader, self._templar, self._shared_loader_obj,
            )
            return std.run(task_vars=task_vars)

        return mark_fast_result(self._run_local(args, resolved_src))

    def _run_local(self, args, resolved_src):
        dest = args.get('dest')
        if not dest:
            return dict(failed=True, msg='dest is required')

        force = boolean(args.get('force', True), strict=False)
        mode = args.get('mode')

        # Resolve dest. ansible has already templated task args, so any '{{ }}'
        # in dest is resolved; template() here only re-resolves residual markers.
        dest = os.path.expanduser(os.path.expandvars(
            self._templar.template(dest)
        ))

        if resolved_src is not None and os.path.isdir(resolved_src):
            return self._copy_dir(args, resolved_src, dest, mode, force)

        if resolved_src is not None:
            display.debug('fast_copy: local + src path, using in-process copy')
            try:
                with open(resolved_src, 'rb') as f:
                    b_content = f.read()
            except OSError as e:
                return dict(failed=True,
                            msg='could not read source %s: %s' % (resolved_src, to_text(e)))
            # Copying a file onto a directory dest places it inside, by basename
            # (matches stock copy).
            if dest.endswith(os.sep) or os.path.isdir(dest):
                dest = os.path.join(dest, os.path.basename(resolved_src))
        else:
            display.debug('fast_copy: local + content path, using in-process write')
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

        result = dict(
            changed=changed,
            dest=dest,
            checksum=new_checksum,
            size=len(b_content),
        )
        if resolved_src is not None:
            result['src'] = resolved_src
        return result

    @staticmethod
    def _dir_unsupported(args, src_dir):
        """Return a fallback reason if a directory `src` can't be fast-pathed,
        else None. The fast path handles regular-file trees; symlinks and
        preserve/unsupported directory_mode delegate to stock."""
        dm = args.get('directory_mode')
        if dm == 'preserve':
            return 'directory_mode=preserve'
        if dm is not None:
            try:
                _parse_mode(dm)
            except ValueError:
                return 'unsupported directory_mode %r' % (dm,)
        if _tree_has_symlink(src_dir):
            return 'symlink in source tree'
        return None

    def _ensure_dir(self, path, mode_int):
        """Create `path` (and parents) if missing and apply `mode_int` when set.
        Returns True if the directory was created or its mode changed."""
        existed = os.path.isdir(path)
        if not existed:
            os.makedirs(path, exist_ok=True)
        changed = not existed
        if mode_int is not None:
            if (os.stat(path).st_mode & 0o7777) != mode_int:
                os.chmod(path, mode_int)
                changed = True
        return changed

    def _copy_one_file(self, src_file, dest_file, mode, force):
        """Copy a single file with checksum idempotency. Returns (changed, err)."""
        try:
            with open(src_file, 'rb') as f:
                b = f.read()
        except OSError as e:
            return False, 'could not read source %s: %s' % (src_file, to_text(e))
        new_checksum = hashlib.sha1(b, usedforsecurity=False).hexdigest()
        if os.path.exists(dest_file):
            if not force:
                return False, None
            try:
                with open(dest_file, 'rb') as f:
                    if hashlib.sha1(f.read(), usedforsecurity=False).hexdigest() == new_checksum:
                        return False, None
            except OSError:
                pass
        err = atomic_write(dest_file, b, mode)
        return (False, err) if err else (True, None)

    def _copy_dir(self, args, src_dir, dest, mode, force):
        """Recursively copy a local directory tree in-process.

        Honors stock's trailing-slash semantics: `src: dir/` copies the *contents*
        into dest, `src: dir` copies the directory *itself* (nested by basename).
        Files get `mode` (or umask default); directories get `directory_mode` (or
        umask default), including the dest root; empty directories are created.
        """
        display.debug('fast_copy: local + directory src, recursive in-process copy')
        src_dir = src_dir.rstrip(os.sep) or os.sep
        contents = to_text(args.get('src') or '').endswith('/')
        dir_mode = args.get('directory_mode')
        dir_mode_int = _parse_mode(dir_mode) if dir_mode is not None else None

        changed = self._ensure_dir(dest, dir_mode_int)
        root_target = dest if contents else os.path.join(dest, os.path.basename(src_dir))
        if root_target != dest and self._ensure_dir(root_target, dir_mode_int):
            changed = True

        for base, dirs, files in os.walk(src_dir):
            rel = os.path.relpath(base, src_dir)
            for d in sorted(dirs):
                rel_d = d if rel == '.' else os.path.join(rel, d)
                if self._ensure_dir(os.path.join(root_target, rel_d), dir_mode_int):
                    changed = True
            for f in sorted(files):
                rel_f = f if rel == '.' else os.path.join(rel, f)
                ch, err = self._copy_one_file(
                    os.path.join(base, f), os.path.join(root_target, rel_f), mode, force)
                if err:
                    return dict(failed=True, msg=err)
                if ch:
                    changed = True

        return dict(changed=changed, dest=dest.rstrip(os.sep) + os.sep, src=src_dir)
