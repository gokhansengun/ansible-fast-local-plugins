from __future__ import annotations

import os

from ansible.plugins.callback import CallbackBase
from ansible.utils.display import Display

DOCUMENTATION = '''
  name: aflp_builtin_redirect
  type: aggregate
  short_description: Route ansible.builtin.* tasks to the fast local action plugins
  version_added: historical
  description:
    - On load this callback monkeypatches the action-plugin loader so that a task
      written as C(ansible.builtin.<name>) resolves to the local fast action plugin
      of the same short name (by rewriting it to C(ansible.legacy.<name>)), whenever
      such an override exists in the configured C(action_plugins) path.
    - This lets role authors keep fully-qualified C(ansible.builtin.*) names and still
      benefit from the in-process fast path, without editing the roles. Builtin names
      that have no local override are left untouched.
    - It exists because custom B(strategy) plugins — the other interception point — are
      deprecated in ansible-core 2.19 and slated for removal (see ansible/ansible#84725).
      A callback is not deprecated and loads before the worker fork, so the patch is
      inherited by every task worker.
  requirements:
    - Point C(callback_plugins) at this directory. No C(callbacks_enabled) entry is
      required (CALLBACK_NEEDS_ENABLED is False, so it auto-loads for playbook runs).
  notes:
    - The patch is intentionally fail-open. Any error while installing it, or while
      rewriting a name at call time, falls back to stock ansible-core behaviour rather
      than breaking the run. Re-verify against ansible-core internals when bumping the
      version matrix (the patched symbol is ansible.plugins.loader.action_loader.get).
'''

display = Display()

_BUILTIN_PREFIX = 'ansible.builtin.'
_LEGACY_PREFIX = 'ansible.legacy.'


def _overridden_names(*path_sources):
    """Return the set of short action-plugin names overridden locally.

    A name is considered overridden when a '<name>.py' file (not starting with
    '_') exists in one of the given directories. Multiple sources are unioned so
    the lookup is robust whether the action_plugins path is resolved from config
    or relative to this file.
    """
    names = set()
    for path in path_sources:
        if not path:
            continue
        try:
            entries = os.listdir(path)
        except OSError:
            continue
        for entry in entries:
            if entry.endswith('.py') and not entry.startswith('_'):
                names.add(entry[:-3])
    return names


def _redirect_name(name, overridden):
    """Map 'ansible.builtin.<name>' -> 'ansible.legacy.<name>' when overridden.

    Everything else (short names, other collections, non-overridden builtins) is
    returned unchanged.
    """
    if isinstance(name, str) and name.startswith(_BUILTIN_PREFIX):
        short = name[len(_BUILTIN_PREFIX):]
        if short in overridden:
            return _LEGACY_PREFIX + short
    return name


def _install_patch():
    """Wrap action_loader.get so allowlisted ansible.builtin.* names hit the fast path.

    Idempotent: a sentinel attribute on the wrapper prevents double-patching when
    more than one callback instance is created.
    """
    from ansible import constants as C
    from ansible.plugins.loader import action_loader

    if getattr(action_loader.get, '_aflp_patched', False):
        return

    # Union two sources: the configured action_plugins path(s), and the
    # action_plugins directory sibling to this callback in the repo layout.
    sibling = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'action_plugins')
    )
    config_paths = list(getattr(C, 'DEFAULT_ACTION_PLUGIN_PATH', None) or [])
    overridden = _overridden_names(sibling, *config_paths)
    if not overridden:
        display.vvv('aflp_builtin_redirect: no local action-plugin overrides found; not patching')
        return

    original_get = action_loader.get

    def patched_get(name, *args, **kwargs):
        try:
            name = _redirect_name(name, overridden)
        except Exception:  # fail-open: never let the rewrite break resolution
            pass
        return original_get(name, *args, **kwargs)

    patched_get._aflp_patched = True
    patched_get._aflp_original = original_get
    action_loader.get = patched_get
    display.vvv(
        'aflp_builtin_redirect: routing ansible.builtin.{%s} to the fast local plugins'
        % ','.join(sorted(overridden))
    )


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'aggregate'
    CALLBACK_NAME = 'aflp_builtin_redirect'
    CALLBACK_NEEDS_ENABLED = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            _install_patch()
        except Exception as e:  # fail-open: a bad patch must not abort the play
            display.warning(
                'aflp_builtin_redirect: could not install action-loader patch, '
                'ansible.builtin.* tasks will use stock plugins: %s' % e
            )
