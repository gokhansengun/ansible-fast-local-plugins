from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

CALLBACK_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '../../ansible/plugins/callback_plugins/aflp_fast_path_summary.py'))

FAST_PLUGIN_MARKER = '__produced_by_fast_plugin'


def _load_callback():
    spec = importlib.util.spec_from_file_location('_aflp_summary_cb', CALLBACK_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['_aflp_summary_cb'] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cb():
    mod = _load_callback()
    return mod.CallbackModule()


def _result(action, result_dict):
    return SimpleNamespace(_task=SimpleNamespace(action=action), _result=result_dict)


def _fast_result(action):
    return _result(action, {'changed': True, FAST_PLUGIN_MARKER: True})


def _fallback_result(action):
    return _result(action, {'changed': True})  # no marker


class TestFastPathSummary:
    def test_overridden_names_discovered(self, cb):
        # The sibling action_plugins dir is real, so these must be present.
        assert 'copy' in cb._overridden
        assert 'lineinfile' in cb._overridden
        assert 'uri' in cb._overridden

    def test_counts_fast_result(self, cb):
        cb.v2_runner_on_ok(_fast_result('copy'))
        assert cb._fast['copy'] == 1
        assert cb._fallback['copy'] == 0

    def test_counts_fallback_result(self, cb):
        cb.v2_runner_on_ok(_fallback_result('copy'))
        assert cb._fast['copy'] == 0
        assert cb._fallback['copy'] == 1

    def test_short_name_strips_namespace(self, cb):
        cb.v2_runner_on_ok(_fast_result('ansible.legacy.template'))
        assert cb._fast['template'] == 1

    def test_ignores_non_overridden_actions(self, cb):
        cb.v2_runner_on_ok(_fallback_result('debug'))
        cb.v2_runner_on_ok(_fast_result('set_fact'))
        assert 'debug' not in cb._fast and 'debug' not in cb._fallback
        assert 'set_fact' not in cb._fast and 'set_fact' not in cb._fallback

    def test_loop_items_counted_from_item_hook(self, cb):
        # ansible fires v2_runner_item_on_ok once per item that actually ran,
        # never for skipped or failed items, so counting there needs no
        # `skipped` inspection at all.
        cb.v2_runner_item_on_ok(_result('stat', {FAST_PLUGIN_MARKER: True, 'item': 'a'}))
        cb.v2_runner_item_on_ok(_result('stat', {FAST_PLUGIN_MARKER: True, 'item': 'b'}))
        cb.v2_runner_item_on_ok(_result('stat', {'item': 'c'}))  # one fell back
        assert cb._fast['stat'] == 2
        assert cb._fallback['stat'] == 1

    def test_loop_aggregate_not_double_counted(self, cb):
        # The aggregated on_ok of a looped task (pre-2.21 shape, items carry
        # `skipped`) must contribute nothing: its items were already counted.
        cb.v2_runner_item_on_ok(_result('stat', {FAST_PLUGIN_MARKER: True}))
        cb.v2_runner_on_ok(_result('stat', {'results': [
            {FAST_PLUGIN_MARKER: True},
            {'skipped': True},
        ]}))
        assert cb._fast['stat'] == 1
        assert cb._fallback['stat'] == 0

    def test_loop_aggregate_2_21_shape_skipped_items_not_fallbacks(self, cb):
        # ansible-core 2.21 hands callbacks per-item dicts WITHOUT the `skipped`
        # flag (only skip_reason/false_condition survive). Parsing the aggregate
        # used to count these as unmarked fallbacks; they must be ignored.
        cb.v2_runner_item_on_ok(_result('template', {FAST_PLUGIN_MARKER: True, 'item': 'a'}))
        cb.v2_runner_on_ok(_result('template', {'changed': True, 'results': [
            {FAST_PLUGIN_MARKER: True, 'changed': True, 'item': 'a'},
            {'changed': False, 'skip_reason': 'Conditional result was False',
             'false_condition': 'item in changed', 'item': 'b'},
            {'changed': False, 'skip_reason': 'Conditional result was False',
             'false_condition': 'item in changed', 'item': 'c'},
        ]}))
        assert cb._fast['template'] == 1
        assert cb._fallback['template'] == 0

    def test_loop_aggregate_with_empty_results_ignored(self, cb):
        cb.v2_runner_on_ok(_result('copy', {'results': []}))
        assert cb._fast['copy'] == 0
        assert cb._fallback['copy'] == 0

    def test_item_hook_ignores_non_overridden_actions(self, cb):
        cb.v2_runner_item_on_ok(_result('debug', {'item': 1}))
        assert 'debug' not in cb._fast and 'debug' not in cb._fallback

    def test_reads_public_task_and_result_attrs(self, cb):
        # ansible-core 2.19+ names: `task` / `result` (the underscored ones are
        # deprecated). Both spellings must work.
        modern = SimpleNamespace(task=SimpleNamespace(action='copy'),
                                 result={FAST_PLUGIN_MARKER: True})
        cb.v2_runner_on_ok(modern)
        cb.v2_runner_item_on_ok(modern)
        assert cb._fast['copy'] == 2

    def test_record_is_fail_open(self, cb):
        # A malformed result must not raise.
        cb.v2_runner_on_ok(SimpleNamespace(_task=None, _result=None))
        cb.v2_runner_on_ok(_result('copy', None))
        assert cb._fast['copy'] == 0

    def test_summary_prints_table(self, cb):
        cb.v2_runner_on_ok(_fast_result('copy'))
        cb.v2_runner_on_ok(_fast_result('copy'))
        cb.v2_runner_on_ok(_fallback_result('lineinfile'))
        printed = []
        cb._display.banner = lambda msg: printed.append(msg)
        cb._display.display = lambda msg, *a, **k: printed.append(msg)
        cb.v2_playbook_on_stats(object())
        text = '\n'.join(printed)
        assert 'AFLP FAST-PATH SUMMARY' in text
        assert 'copy' in text and 'fast=2' in text
        assert 'lineinfile' in text and 'fell back to stock' in text
        assert 'TOTAL' in text

    def test_summary_silent_when_no_data(self, cb):
        printed = []
        cb._display.banner = lambda msg: printed.append(msg)
        cb._display.display = lambda msg, *a, **k: printed.append(msg)
        cb.v2_playbook_on_stats(object())
        assert printed == []
