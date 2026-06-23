from __future__ import annotations

import os

from ansible.plugins.callback import CallbackBase
from ansible.utils.display import Display

DOCUMENTATION = '''
  name: aflp_kubernetes_redirect
  type: aggregate
  short_description: Route kubernetes.core.* tasks to the fast aflp.kubernetes_core overrides
  version_added: historical
  description:
    - On load this callback monkeypatches the action-plugin loader so that a task
      written as C(kubernetes.core.<name>) resolves to the fast local override of the
      same short name in the C(aflp.kubernetes_core) collection, whenever such an
      override exists. Only the overridden names are rewritten; every other
      C(kubernetes.core.*) action (and the genuine modules behind them) is untouched.
    - This lets role authors keep C(kubernetes.core.*) names and still benefit from the
      in-process fast path, without editing the roles. Unlike the directory-shadow
      approach, the genuine C(kubernetes.core) collection stays installed and importable,
      so the overrides can delegate to it for non-local or C(become) tasks.
    - The rewrite targets C(action_loader.get) only, not Python imports, so the in-plugin
      delegation import of C(ansible_collections.kubernetes.core) still resolves to the
      genuine collection.
  requirements:
    - Point C(callback_plugins) at this directory. No C(callbacks_enabled) entry is
      required (CALLBACK_NEEDS_ENABLED is False, so it auto-loads for playbook runs).
  notes:
    - The patch is intentionally fail-open. Any error while installing it, or while
      rewriting a name at call time, falls back to stock behaviour rather than breaking
      the run. Re-verify against ansible-core internals when bumping the version matrix
      (the patched symbol is ansible.plugins.loader.action_loader.get).
'''

display = Display()

_SOURCE_PREFIX = 'kubernetes.core.'
_TARGET_PREFIX = 'aflp.kubernetes_core.'


def _overridden_names(*path_sources):
    """Return the set of short action names overridden by aflp.kubernetes_core.

    A name is considered overridden when a '<name>.py' file (not starting with
    '_') exists in one of the given action directories.
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
    """Map 'kubernetes.core.<name>' -> 'aflp.kubernetes_core.<name>' when overridden.

    Everything else (other collections, non-overridden kubernetes.core actions) is
    returned unchanged.
    """
    if isinstance(name, str) and name.startswith(_SOURCE_PREFIX):
        short = name[len(_SOURCE_PREFIX):]
        if short in overridden:
            return _TARGET_PREFIX + short
    return name


def _action_dir():
    """Locate the aflp.kubernetes_core action-plugin dir relative to this file.

    This file lives in ansible/plugins/callback_plugins/, so the collection is two
    levels up (ansible/collections/...), not one.
    """
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        '..', '..', 'collections', 'ansible_collections', 'aflp', 'kubernetes_core',
        'plugins', 'action',
    ))


def _install_patch():
    """Wrap action_loader.get so allowlisted kubernetes.core.* names hit the fast path.

    Idempotent: a sentinel attribute on the wrapper prevents double-patching when
    more than one callback instance is created.
    """
    from ansible.plugins.loader import action_loader

    if getattr(action_loader.get, '_aflp_k8s_patched', False):
        return

    overridden = _overridden_names(_action_dir())
    if not overridden:
        display.vvv('aflp_kubernetes_redirect: no aflp.kubernetes_core overrides found; not patching')
        return

    original_get = action_loader.get

    def patched_get(name, *args, **kwargs):
        try:
            name = _redirect_name(name, overridden)
        except Exception:  # fail-open: never let the rewrite break resolution
            pass
        return original_get(name, *args, **kwargs)

    patched_get._aflp_k8s_patched = True
    patched_get._aflp_original = original_get
    action_loader.get = patched_get

    # ansible-core's action-handler resolution (TaskExecutor) only routes a task
    # through action_loader.get(<fqcn>) when has_plugin(<fqcn>) is True; otherwise
    # it falls back to the 'normal' action and runs the genuine MODULE directly.
    # The genuine kubernetes.core ships some of our overrides as module-only (e.g.
    # helm_pull has no action plugin), so without this, those names bypass the
    # rewrite entirely. Report our overridden names as present so resolution keeps
    # the fqcn handler, which patched_get then rewrites to aflp.kubernetes_core.
    original_has = action_loader.has_plugin

    def patched_has(name, *args, **kwargs):
        try:
            if _redirect_name(name, overridden) != name:
                return True
        except Exception:  # fail-open
            pass
        return original_has(name, *args, **kwargs)

    patched_has._aflp_original = original_has
    action_loader.has_plugin = patched_has

    display.vvv(
        'aflp_kubernetes_redirect: routing kubernetes.core.{%s} to aflp.kubernetes_core'
        % ','.join(sorted(overridden))
    )


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'aggregate'
    CALLBACK_NAME = 'aflp_kubernetes_redirect'
    CALLBACK_NEEDS_ENABLED = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            _install_patch()
        except Exception as e:  # fail-open: a bad patch must not abort the play
            display.warning(
                'aflp_kubernetes_redirect: could not install action-loader patch, '
                'kubernetes.core.* tasks will use the genuine collection: %s' % e
            )
