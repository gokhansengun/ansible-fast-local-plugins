"""Unit tests for the aflp_kubernetes_redirect callback's pure helpers.

The monkeypatch itself (action_loader.get) is exercised end-to-end by
tests/integration/test_integration.py::test_kubernetes_fqcn_redirect. Here we
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
        'ansible', 'plugins', 'callback_plugins', 'aflp_kubernetes_redirect.py',
    )
)

ACTION_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'ansible', 'collections',
                 'ansible_collections', 'aflp', 'kubernetes_core', 'plugins', 'action')
)


def _load_callback_module():
    cache_key = '_aflp_callback_k8s_redirect'
    if cache_key in sys.modules:
        return sys.modules[cache_key]
    spec = importlib.util.spec_from_file_location(cache_key, CALLBACK_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[cache_key] = mod
    spec.loader.exec_module(mod)
    return mod


cb = _load_callback_module()


# --- _redirect_name -------------------------------------------------------

def test_redirect_overridden_k8s_goes_to_aflp():
    assert cb._redirect_name('kubernetes.core.k8s', {'k8s'}) == 'aflp.kubernetes_core.k8s'


def test_redirect_overridden_helm_pull_goes_to_aflp():
    assert cb._redirect_name('kubernetes.core.helm_pull', {'helm_pull'}) \
        == 'aflp.kubernetes_core.helm_pull'


def test_redirect_non_overridden_kubernetes_core_unchanged():
    # helm (full module) has no fast override, so it must be left on genuine
    assert cb._redirect_name('kubernetes.core.helm', {'k8s', 'k8s_info'}) \
        == 'kubernetes.core.helm'


def test_redirect_leaves_other_collections_alone():
    assert cb._redirect_name('community.kubernetes.k8s', {'k8s'}) == 'community.kubernetes.k8s'


def test_redirect_leaves_aflp_names_alone():
    assert cb._redirect_name('aflp.kubernetes_core.k8s', {'k8s'}) == 'aflp.kubernetes_core.k8s'


def test_redirect_tolerates_non_string():
    assert cb._redirect_name(None, {'k8s'}) is None


# --- _overridden_names / _action_dir --------------------------------------

def test_overridden_names_empty_for_no_path():
    assert cb._overridden_names(None) == set()


def test_overridden_names_discovers_the_five_overrides():
    names = cb._overridden_names(ACTION_DIR)
    assert {'k8s', 'k8s_info', 'helm_repository', 'helm_pull', 'helm_info'} <= names
    # private helpers (if any) are excluded
    assert not any(n.startswith('_') for n in names)


def test_action_dir_points_at_aflp_collection():
    # The callback must resolve its own collection's action dir relative to itself.
    assert cb._action_dir() == ACTION_DIR
    assert os.path.isdir(cb._action_dir())
