"""Unit tests for the aflp_builtin_redirect callback's pure helpers.

The monkeypatch itself (action_loader.get) is exercised end-to-end by
tests/integration/test_integration.py::test_builtin_fqcn_redirect. Here we
just pin down the name-rewrite logic and the override discovery, which are
plain functions with no Ansible runtime dependency.
"""
from __future__ import annotations

import importlib.util
import os
import sys

CALLBACK_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), '..', '..',
        'ansible', 'plugins', 'callback_plugins', 'aflp_builtin_redirect.py',
    )
)


def _load_callback_module():
    cache_key = '_aflp_callback_redirect'
    if cache_key in sys.modules:
        return sys.modules[cache_key]
    spec = importlib.util.spec_from_file_location(cache_key, CALLBACK_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[cache_key] = mod
    spec.loader.exec_module(mod)
    return mod


cb = _load_callback_module()


# --- _redirect_name -------------------------------------------------------

def test_redirect_overridden_builtin_goes_to_legacy():
    assert cb._redirect_name('ansible.builtin.template', {'template'}) == 'ansible.legacy.template'


def test_redirect_non_overridden_builtin_unchanged():
    # debug has no local fast plugin, so it must be left alone
    assert cb._redirect_name('ansible.builtin.debug', {'template', 'copy'}) == 'ansible.builtin.debug'


def test_redirect_leaves_short_names_alone():
    assert cb._redirect_name('template', {'template'}) == 'template'


def test_redirect_leaves_legacy_names_alone():
    assert cb._redirect_name('ansible.legacy.template', {'template'}) == 'ansible.legacy.template'


def test_redirect_leaves_other_collections_alone():
    assert cb._redirect_name('community.general.template', {'template'}) == 'community.general.template'


def test_redirect_tolerates_non_string():
    assert cb._redirect_name(None, {'template'}) is None


# --- _overridden_names ----------------------------------------------------

def test_overridden_names_discovers_real_plugins():
    names = cb._overridden_names(None)  # no path
    assert names == set()

    action_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', '..',
                     'ansible', 'plugins', 'action_plugins')
    )
    names = cb._overridden_names(action_dir)
    # known plugins present, private helper excluded
    assert 'template' in names
    assert 'copy' in names
    assert '_action_utils' not in names


def test_overridden_names_unions_sources_and_skips_missing_dirs():
    action_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', '..',
                     'ansible', 'plugins', 'action_plugins')
    )
    names = cb._overridden_names('/nonexistent/path', action_dir)
    assert 'stat' in names
