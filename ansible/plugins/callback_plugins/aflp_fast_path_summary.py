from __future__ import annotations

import os
from collections import defaultdict

from ansible.plugins.callback import CallbackBase
from ansible.utils.display import Display

DOCUMENTATION = '''
  name: aflp_fast_path_summary
  type: aggregate
  short_description: Report fast-path vs fallback usage of the local action plugins
  version_added: historical
  description:
    - Tallies, per action, how many task results were produced in-process by a fast
      local action plugin versus delegated to the stock ansible-core module/plugin,
      then prints a summary table at the end of the play run.
    - Classification uses the C(__produced_by_fast_plugin) marker that every fast
      plugin stamps on a successful in-process result; the stock fallback never sets
      it. Looped tasks are counted per item. Only actions that have a local override
      are reported, so unrelated tasks (debug, set_fact, ...) do not clutter the table.
    - The point is to make silent fallbacks visible. A task you expected to run fast
      but which hit C(become), a non-local connection or an unsupported argument shows
      up in the C(fallback) column, so you can investigate rather than silently losing
      the speedup.
  requirements:
    - Enable via C(callbacks_enabled = aflp_fast_path_summary) in ansible.cfg
      (CALLBACK_NEEDS_ENABLED is True, so it is opt-in like profile_tasks/timer).
  notes:
    - Failed and skipped results are not classified, because the marker is intentionally
      absent on failures regardless of which path ran, so counting them would mislead.
    - Fail-open. Any error while recording a result is swallowed so observability never
      breaks a play.
'''

display = Display()

FAST_PLUGIN_MARKER = '__produced_by_fast_plugin'


def _overridden_names(*path_sources):
    """Return the set of short action-plugin names overridden locally.

    A name is overridden when a '<name>.py' (not starting with '_') exists in one
    of the given directories. Mirrors the discovery used by aflp_builtin_redirect.
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


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'aggregate'
    CALLBACK_NAME = 'aflp_fast_path_summary'
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        sibling = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'action_plugins')
        )
        try:
            from ansible import constants as C
            config_paths = list(getattr(C, 'DEFAULT_ACTION_PLUGIN_PATH', None) or [])
        except Exception:
            config_paths = []
        self._overridden = _overridden_names(sibling, *config_paths)
        self._fast = defaultdict(int)
        self._fallback = defaultdict(int)

    @staticmethod
    def _short_name(action):
        if not isinstance(action, str):
            return action
        return action.rsplit('.', 1)[-1]

    def _tally(self, short, result_dict):
        if not isinstance(result_dict, dict):
            return
        if result_dict.get(FAST_PLUGIN_MARKER):
            self._fast[short] += 1
        else:
            self._fallback[short] += 1

    def _record(self, result):
        try:
            short = self._short_name(getattr(result._task, 'action', None))
            if short not in self._overridden:
                return
            res = result._result or {}
            items = res.get('results')
            # Looped task: classify each item (the aggregated on_ok carries them);
            # skipped items did not run the plugin, so they are excluded.
            if isinstance(items, list) and items:
                for item in items:
                    if isinstance(item, dict) and not item.get('skipped'):
                        self._tally(short, item)
            else:
                self._tally(short, res)
        except Exception:  # fail-open: observability must never break a run
            pass

    def v2_runner_on_ok(self, result):
        self._record(result)

    def v2_playbook_on_stats(self, stats):
        actions = sorted(set(self._fast) | set(self._fallback))
        if not actions:
            return
        self._display.banner('AFLP FAST-PATH SUMMARY')
        total_fast = total_fallback = 0
        for action in actions:
            fast = self._fast[action]
            fallback = self._fallback[action]
            total_fast += fast
            total_fallback += fallback
            note = '   <- fell back to stock' if fallback else ''
            self._display.display('  %-18s fast=%-6d fallback=%-6d%s'
                                  % (action, fast, fallback, note))
        self._display.display('  %-18s fast=%-6d fallback=%-6d'
                              % ('TOTAL', total_fast, total_fallback))
