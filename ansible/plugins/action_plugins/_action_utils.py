from __future__ import annotations

import importlib.util
import os
import shutil
import stat as stat_module
import sys
import tempfile

from ansible.module_utils.common.text.converters import to_native, to_text

# Result key stamped onto every successful fast (in-process) result so tests can
# confirm the fast action plugin actually handled the task rather than the
# standard fallback. ansible-core only strips keys prefixed '_ansible_', so this
# survives into the registered result. The standard/fallback path never sets it.
FAST_PLUGIN_MARKER = '__produced_by_fast_plugin'


def mark_fast_result(result, force=False):
    """Stamp a fast-path result dict with FAST_PLUGIN_MARKER.

    Presence of the key means "the fast in-process plugin produced this result".
    By default only non-failed dicts are marked, so a *plugin* failure (it could
    not do its job) or a delegated/fallback result stays unmarked.

    `force=True` marks even a `failed` result. command/shell use this for a result
    that came from actually running the command: a non-zero `rc` is the command's
    outcome, not a fallback, so the plugin DID run in-process and the result must
    carry the marker (otherwise a `failed_when:`-rescued non-zero command looks
    like a fallback to the summary callback). Pre-execution bails (no command,
    missing chdir) are returned without force and stay unmarked.

    Non-dict values pass through untouched; setdefault avoids clobbering an
    explicit value.
    """
    if isinstance(result, dict) and (force or not result.get('failed')):
        result.setdefault(FAST_PLUGIN_MARKER, True)
    return result


def _load_builtin_action(plugin_name: str):
    """Load ansible's built-in action plugin from the installed package path.

    Ansible registers each custom action plugin in sys.modules under the key
    'ansible.plugins.action.<name>', shadowing the built-in.  A plain
    'from ansible.plugins.action.X import ActionModule' would therefore return
    the custom class and cause infinite recursion in fallback paths.  This
    helper loads the *installed* file directly, bypassing sys.modules.
    """
    cache_key = f'_builtin_action_{plugin_name}'
    if cache_key in sys.modules:
        return sys.modules[cache_key]
    import ansible.plugins.action as _apkg
    builtin_path = os.path.join(os.path.dirname(_apkg.__file__), plugin_name + '.py')
    spec = importlib.util.spec_from_file_location(cache_key, builtin_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[cache_key] = mod
    spec.loader.exec_module(mod)
    return mod


# Global kill-switch. When AFLP_DISABLE is truthy, every fast plugin treats the
# connection as non-local and delegates to stock ansible-core — an operator can
# force the entire fast path off (e.g. to check "is a fast plugin causing this?")
# without editing ansible.cfg, roles or playbooks. Read at gate time so it can be
# toggled per run.
_DISABLE_ENV = 'AFLP_DISABLE'
_TRUTHY = ('1', 'true', 'yes', 'on')


def _fast_disabled():
    """True when the fast path is globally disabled via the AFLP_DISABLE env var."""
    return os.environ.get(_DISABLE_ENV, '').strip().lower() in _TRUTHY


def _is_local(connection):
    # The kill-switch reports "not local" so the gate falls back to stock.
    if _fast_disabled():
        return False
    if getattr(connection, 'transport', None) == 'local':
        return True
    load_name = getattr(connection, '_load_name', '') or ''
    if load_name == 'local' or load_name.endswith('.local'):
        return True
    if 'connection.local' in type(connection).__module__:
        return True
    return False


def normalize_arg_aliases(args, alias_map):
    """Return a copy of ``args`` with argspec aliases folded onto their
    canonical names (``alias_map`` maps alias -> canonical).

    Alias resolution normally happens in the module's AnsibleModule argspec,
    which action plugins never run — without this, an aliased task either looks
    "unsupported" to a whitelist gate (needless fallback) or has the argument
    silently ignored. If a task sets both an alias and its canonical name, the
    alias is left in place so a whitelist gate treats it as unsupported and the
    stock module applies its own precedence rules.
    """
    args = dict(args)
    for alias, canonical in alias_map.items():
        if alias in args and canonical not in args:
            args[canonical] = args.pop(alias)
    return args


# Strict mode. When AFLP_STRICT is truthy, a fast plugin that would fall back to
# stock ansible-core raises instead. On a local-only controller every task is
# expected to hit the fast path, so an unexpected fallback (a stray become, a
# non-local connection, an unsupported argument) signals an unintended task —
# strict mode surfaces it loudly rather than silently running the slow path.
_STRICT_ENV = 'AFLP_STRICT'


def _strict_enabled():
    """True when AFLP_STRICT requests that fallbacks raise instead of delegating."""
    return os.environ.get(_STRICT_ENV, '').strip().lower() in _TRUTHY


def strict_guard(connection, play_context, task=None, extra_reasons=None):
    """Raise instead of falling back to stock, when AFLP_STRICT is set.

    Call this at the top of a fast plugin's fallback branch, just before it
    delegates. No-op unless AFLP_STRICT is truthy. The AFLP_DISABLE kill-switch
    is an *intentional* global fallback, so it suppresses the guard (the two are
    not meant to be combined). The raised error reports a best-effort reason so
    the offending task is easy to diagnose.

    `extra_reasons` (a str or list of str) lets a plugin add plugin-specific
    causes the generic detection below can't see — e.g. copy's `remote_src` or a
    directory source — so the message names the real trigger instead of the
    catch-all "unsupported arguments".
    """
    if not _strict_enabled() or _fast_disabled():
        return
    reasons = []
    if not _is_local(connection):
        reasons.append('non-local connection')
    if getattr(play_context, 'become', False):
        reasons.append('become')
    if getattr(play_context, 'check_mode', False):
        reasons.append('check_mode')
    if task is not None and getattr(task, 'async_val', 0):
        reasons.append('async')
    if extra_reasons:
        if isinstance(extra_reasons, str):
            extra_reasons = [extra_reasons]
        reasons.extend(extra_reasons)
    reason = ', '.join(reasons) or 'unsupported arguments'
    from ansible.errors import AnsibleActionFail
    raise AnsibleActionFail(
        'AFLP_STRICT: this task would fall back from a fast local plugin to '
        'stock ansible-core (%s); refusing because AFLP_STRICT is set, so '
        'unintended fallbacks are caught rather than silently run slow.' % reason)


def resolve_environment(task, templar):
    """Template and merge ``task.environment`` into a plain {str: str} dict for
    ``subprocess.run(env=...)``.

    ``environment`` is a list of dicts (play/block/task levels), applied in order
    so a later level wins — matching ansible-core's own merge. Each dict is
    templated against the current vars (so values like ``{{ password }}`` are
    resolved). Empty/zero-length dicts are skipped (ansible-core sets
    ``environment=[{}]`` by default, meaning "no extra vars"). Returns {} when
    there is nothing to add, so the caller can pass ``env=None`` and inherit the
    process environment unchanged.
    """
    final = {}
    environments = getattr(task, 'environment', None)
    if environments is None:
        return final
    if not isinstance(environments, list):
        environments = [environments]
    for env in environments:
        if not env:
            continue
        templated = templar.template(env)
        if isinstance(templated, dict):
            for k, v in templated.items():
                final[to_text(k)] = to_text(v)
    return final


def _parse_mode(mode):
    """Parse a file mode argument into an integer.

    Accepts int directly, or octal strings ('0644', '644', '0o644').
    Raises ValueError for unsupported values such as 'preserve'.
    """
    if isinstance(mode, int):
        return mode
    if not isinstance(mode, str):
        raise ValueError('unsupported mode type: %r' % mode)
    if mode == 'preserve':
        raise ValueError("mode='preserve' requires a source file and is not valid with content:")
    # Strings with an explicit base prefix (0o, 0x, 0b) use int's auto-detection.
    # Bare '644' and leading-zero '0644' are both treated as octal — int('644', 0)
    # would silently return decimal 644 rather than raise, so we avoid it here.
    if mode.startswith(('0o', '0O', '0x', '0X', '0b', '0B')):
        return int(mode, 0)
    return int(mode, 8)


def atomic_write(dest, b_content, mode=None):
    """Write b_content to dest atomically via a same-directory temp file.

    Returns an error string on failure, or None on success.
    """
    dest_dir = os.path.dirname(os.path.abspath(dest))
    if not os.path.isdir(dest_dir):
        return 'destination directory does not exist: %s' % dest_dir

    fd, tmp_path = tempfile.mkstemp(dir=dest_dir)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(b_content)

        if mode is not None:
            os.chmod(tmp_path, _parse_mode(mode))
        elif os.path.exists(dest):
            os.chmod(tmp_path, stat_module.S_IMODE(os.stat(dest).st_mode))
        else:
            # New file, no mode requested: apply the umask default like stock
            # ansible's atomic_move, rather than leaving mkstemp's restrictive
            # 0600. (mkstemp always creates 0600.)
            cur_umask = os.umask(0)
            os.umask(cur_umask)
            os.chmod(tmp_path, 0o666 & ~cur_umask)

        shutil.move(tmp_path, dest)
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return 'failed to write file: %s' % to_native(e)

    return None
