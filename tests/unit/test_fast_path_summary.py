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

    def test_loop_results_counted_per_item(self, cb):
        looped = _result('stat', {'results': [
            {FAST_PLUGIN_MARKER: True},
            {FAST_PLUGIN_MARKER: True},
            {},  # one fell back
        ]})
        cb.v2_runner_on_ok(looped)
        assert cb._fast['stat'] == 2
        assert cb._fallback['stat'] == 1

    def test_loop_skipped_items_excluded(self, cb):
        looped = _result('stat', {'results': [
            {FAST_PLUGIN_MARKER: True},
            {'skipped': True},
        ]})
        cb.v2_runner_on_ok(looped)
        assert cb._fast['stat'] == 1
        assert cb._fallback['stat'] == 0

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
