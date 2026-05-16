from __future__ import annotations

import contextlib
import importlib
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'ansible', 'plugins', 'action_plugins')
)
# Ensure sibling utilities (_action_utils) are importable by the plugins themselves.
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

COLLECTION_PLUGIN_DIRS = {
    'kubernetes.core': os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', 'ansible', 'collections',
                     'ansible_collections', 'kubernetes', 'core', 'plugins', 'action')
    ),
}


def _load_plugin(plugin_name: str, plugin_dir: str = None):
    """Load an action plugin by file path.

    importlib.import_module('copy') would return the cached stdlib copy module
    because it lands in sys.modules before pytest starts.  Loading by absolute
    path with a private cache key sidesteps that collision entirely and works
    the same way for stat, tempfile, and any other stdlib-shadowing name.
    """
    dir_ = plugin_dir or PLUGIN_DIR
    cache_key = f'_aflp_{plugin_name}'
    if cache_key in sys.modules:
        return sys.modules[cache_key]
    plugin_path = os.path.join(dir_, plugin_name + '.py')
    spec = importlib.util.spec_from_file_location(cache_key, plugin_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[cache_key] = mod  # register before exec to handle any intra-plugin imports
    spec.loader.exec_module(mod)
    return mod


def _local_conn():
    conn = MagicMock()
    conn.transport = 'local'
    conn._load_name = 'local'
    return conn


def _ssh_conn():
    conn = MagicMock()
    conn.transport = 'ssh'
    conn._load_name = 'ssh'
    return conn


def _play_ctx(become=False, check_mode=False):
    ctx = MagicMock()
    ctx.become = become
    ctx.check_mode = check_mode
    return ctx


def _task(args, async_val=0, environment=None, check_mode=False, no_log=False):
    t = MagicMock()
    t.args = dict(args)
    t.async_val = async_val
    t.environment = environment or []
    t.check_mode = check_mode
    t.no_log = no_log
    t.diff = []
    return t


def make_action(plugin_name, args, *, local=True, become=False, check_mode=False,
                async_val=0, environment=None, extra_task_attrs=None, plugin_dir=None):
    """Instantiate an ActionModule for plugin_name with mocked Ansible internals."""
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar
    from ansible.plugins import loader as plugins_loader

    mod = _load_plugin(plugin_name, plugin_dir=plugin_dir)

    conn = _local_conn() if local else _ssh_conn()
    play_ctx = _play_ctx(become=become, check_mode=check_mode)
    task = _task(args, async_val=async_val, environment=environment, check_mode=check_mode)
    if extra_task_attrs:
        for k, v in extra_task_attrs.items():
            setattr(task, k, v)

    loader = DataLoader()
    templar = Templar(loader=loader, variables={})

    action = mod.ActionModule(
        task=task,
        connection=conn,
        play_context=play_ctx,
        loader=loader,
        templar=templar,
        shared_loader_obj=plugins_loader,
    )
    return action


@contextlib.contextmanager
def mock_builtin_run(plugin_name, return_value):
    """Patch _load_builtin_action inside the named plugin so fallback tests stay unit tests.

    _load_builtin_action loads the ansible builtin under a private sys.modules key, making
    it a different class object than ansible.plugins.action.<name>.ActionModule.  Patching
    the canonical path therefore misses the actual call.  This helper patches the reference
    inside the already-loaded plugin module so the mock intercepts the real call site.
    """
    plugin_mod = _load_plugin(plugin_name)
    mock_mod = MagicMock()
    mock_instance = MagicMock()
    mock_instance.run.return_value = return_value
    mock_mod.ActionModule.return_value = mock_instance
    with patch.object(plugin_mod, '_load_builtin_action', return_value=mock_mod):
        yield mock_instance.run


@contextlib.contextmanager
def mock_collection_run(fqcn: str, return_value):
    """Patch a collection action plugin's Python module in sys.modules for fallback tests.

    The fallback path in collection plugins does a lazy
    ``from ansible_collections.<fqcn> import ActionModule`` inside run().
    This context manager inserts a mock for the full module path (and any
    missing intermediate packages) so the import resolves without needing
    the real collection installed.

    fqcn: dotted name relative to ansible_collections, e.g.
          'kubernetes.core.plugins.action.helm_repository'
    """
    from unittest.mock import MagicMock, patch

    mock_mod = MagicMock()
    mock_instance = MagicMock()
    mock_instance.run.return_value = return_value
    mock_mod.ActionModule.return_value = mock_instance

    full_module = 'ansible_collections.' + fqcn
    parts = full_module.split('.')
    patch_dict = {}
    for i in range(2, len(parts) + 1):  # skip 'ansible_collections' itself
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            patch_dict[key] = MagicMock()
    patch_dict[full_module] = mock_mod

    with patch.dict(sys.modules, patch_dict):
        yield mock_instance.run


@pytest.fixture
def local_conn():
    return _local_conn()


@pytest.fixture
def ssh_conn():
    return _ssh_conn()
