from __future__ import annotations

import importlib.util
import os
import shutil
import stat as stat_module
import sys
import tempfile

from ansible.module_utils._text import to_native


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


def _is_local(connection):
    if getattr(connection, 'transport', None) == 'local':
        return True
    load_name = getattr(connection, '_load_name', '') or ''
    if load_name == 'local' or load_name.endswith('.local'):
        return True
    if 'connection.local' in type(connection).__module__:
        return True
    return False


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

        shutil.move(tmp_path, dest)
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return 'failed to write file: %s' % to_native(e)

    return None
