from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

import importlib.util

# Load the collection plugin module directly so we can test _kubectl_get and _resource_type.
_PLUGIN_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..',
                 'ansible', 'collections', 'ansible_collections',
                 'aflp', 'kubernetes_core', 'plugins', 'action', 'k8s_info.py')
)
_spec = importlib.util.spec_from_file_location('_aflp_k8s_info', _PLUGIN_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from tests.conftest import (
    COLLECTION_PLUGIN_DIRS,
    FAST_PLUGIN_MARKER,
    _load_plugin,
    make_action,
    mock_collection_run,
)

PLUGIN_DIR = COLLECTION_PLUGIN_DIRS['aflp.kubernetes_core']
FQCN = 'kubernetes.core.plugins.action.k8s_info'


def _action(args, **kwargs):
    return make_action('k8s_info', args, plugin_dir=PLUGIN_DIR, **kwargs)


def _kubectl_list(items):
    """Return a MagicMock subprocess result with a kubectl List JSON body."""
    body = json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': items})
    return MagicMock(returncode=0, stdout=body, stderr='')


def _kubectl_single(obj):
    """Return a MagicMock subprocess result for a single-object kubectl response."""
    body = json.dumps(obj)
    return MagicMock(returncode=0, stdout=body, stderr='')


# ------------------------------------------------------------------ #
# _resource_type helper                                                #
# ------------------------------------------------------------------ #
class TestResourceType:
    def test_core_kind_returns_lowercase(self):
        assert _mod._resource_type('Pod', 'v1') == 'pod'

    def test_non_core_kind_returns_kind_dot_group(self):
        assert _mod._resource_type('Deployment', 'apps/v1') == 'deployment.apps'

    def test_non_core_with_long_group(self):
        assert _mod._resource_type('Ingress', 'networking.k8s.io/v1') == 'ingress.networking.k8s.io'


# ------------------------------------------------------------------ #
# _kubectl_get helper                                                  #
# ------------------------------------------------------------------ #
class TestKubectlGet:
    def _call(self, **kwargs):
        defaults = dict(
            kind='Pod', api_version='v1', name=None, namespace=None,
            label_selectors=[], field_selectors=[], kubeconfig=None,
            context=None, binary_path='kubectl',
        )
        defaults.update(kwargs)
        return _mod._kubectl_get(**defaults)

    def test_returns_items_from_list_response(self):
        items = [{'kind': 'Pod', 'metadata': {'name': 'p1'}},
                 {'kind': 'Pod', 'metadata': {'name': 'p2'}}]
        with patch('subprocess.run', return_value=_kubectl_list(items)):
            result = self._call()
        assert len(result) == 2
        assert result[0]['metadata']['name'] == 'p1'

    def test_wraps_single_object_in_list(self):
        obj = {'kind': 'Pod', 'metadata': {'name': 'p1'}, 'spec': {}}
        with patch('subprocess.run', return_value=_kubectl_single(obj)):
            result = self._call(name='p1')
        assert isinstance(result, list)
        assert result[0]['metadata']['name'] == 'p1'

    def test_empty_list_returns_empty(self):
        with patch('subprocess.run', return_value=_kubectl_list([])):
            result = self._call()
        assert result == []

    def test_kubectl_failure_raises_runtime_error(self):
        fail = MagicMock(returncode=1, stderr='server unreachable', stdout='')
        with patch('subprocess.run', return_value=fail):
            with pytest.raises(RuntimeError, match='server unreachable'):
                self._call()

    def test_named_resource_not_found_returns_empty(self):
        # kubectl exits non-zero with an API (NotFound) for a missing named
        # resource; genuine k8s_info treats that as an empty result, not an error.
        notfound = MagicMock(
            returncode=1, stdout='',
            stderr='Error from server (NotFound): pods "secret-unsealer-0" not found')
        with patch('subprocess.run', return_value=notfound):
            result = self._call(name='secret-unsealer-0')
        assert result == []

    def test_invalid_json_raises_runtime_error(self):
        bad = MagicMock(returncode=0, stdout='not-json', stderr='')
        with patch('subprocess.run', return_value=bad):
            with pytest.raises(RuntimeError, match='failed to parse'):
                self._call()

    def test_subprocess_exception_raises_runtime_error(self):
        with patch('subprocess.run', side_effect=FileNotFoundError('no kubectl')):
            with pytest.raises(RuntimeError, match='failed to run kubectl'):
                self._call()

    def test_passes_namespace_flag(self):
        with patch('subprocess.run', return_value=_kubectl_list([])) as mock_run:
            self._call(namespace='test-ns')
        cmd = mock_run.call_args[0][0]
        assert '--namespace' in cmd
        assert 'test-ns' in cmd

    def test_passes_label_selector_flag(self):
        with patch('subprocess.run', return_value=_kubectl_list([])) as mock_run:
            self._call(label_selectors=['app=web', 'env=prod'])
        cmd = mock_run.call_args[0][0]
        assert '--selector' in cmd
        assert 'app=web,env=prod' in cmd

    def test_passes_field_selector_flag(self):
        with patch('subprocess.run', return_value=_kubectl_list([])) as mock_run:
            self._call(field_selectors=['status.phase=Running'])
        cmd = mock_run.call_args[0][0]
        assert '--field-selector' in cmd

    def test_passes_kubeconfig_flag(self):
        with patch('subprocess.run', return_value=_kubectl_list([])) as mock_run:
            self._call(kubeconfig='/home/user/.kube/config')
        cmd = mock_run.call_args[0][0]
        assert '--kubeconfig' in cmd
        assert '/home/user/.kube/config' in cmd

    def test_passes_context_flag(self):
        with patch('subprocess.run', return_value=_kubectl_list([])) as mock_run:
            self._call(context='my-context')
        cmd = mock_run.call_args[0][0]
        assert '--context' in cmd
        assert 'my-context' in cmd


# ------------------------------------------------------------------ #
# ActionModule fast path                                               #
# ------------------------------------------------------------------ #
class TestK8sInfoFastPath:
    def test_returns_resources_list(self):
        items = [{'kind': 'Pod', 'metadata': {'name': 'p1'}}]
        action = _action({'kind': 'Pod', 'namespace': 'default'})
        with patch('subprocess.run', return_value=_kubectl_list(items)):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is False
        assert len(result['resources']) == 1
        assert result['resources'][0]['metadata']['name'] == 'p1'

    def test_missing_kind_returns_failed(self):
        action = _action({'namespace': 'default'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'kind' in result['msg']

    def test_kubectl_failure_returns_failed(self):
        fail = MagicMock(returncode=1, stderr='server unreachable', stdout='')
        action = _action({'kind': 'Pod'})
        with patch('subprocess.run', return_value=fail):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'server unreachable' in result['msg']

    def test_named_resource_not_found_returns_empty_resources(self):
        # Regression: `until: r.resources | length == 0` failed with
        # "object of type 'dict' has no attribute 'resources'" because a NotFound
        # for a named resource was returned as a failure (no resources key)
        # instead of resources: [] like genuine kubernetes.core.k8s_info does.
        notfound = MagicMock(
            returncode=1, stdout='',
            stderr='Error from server (NotFound): pods "secret-unsealer-0" not found')
        action = _action({'kind': 'Pod', 'name': 'secret-unsealer-0', 'namespace': 'default'})
        with patch('subprocess.run', return_value=notfound):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['resources'] == []
        assert result['changed'] is False
        # The marker must still be present (fast path handled it).
        assert result[FAST_PLUGIN_MARKER] is True

    def test_non_core_api_version(self):
        items = [{'kind': 'Deployment', 'metadata': {'name': 'd1'}}]
        action = _action({'kind': 'Deployment', 'api_version': 'apps/v1', 'namespace': 'default'})
        with patch('subprocess.run', return_value=_kubectl_list(items)) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed')
        cmd = mock_run.call_args[0][0]
        assert 'deployment.apps' in cmd

    def test_empty_result_returns_empty_list(self):
        action = _action({'kind': 'Pod', 'namespace': 'empty-ns'})
        with patch('subprocess.run', return_value=_kubectl_list([])):
            result = action.run(task_vars={})
        assert result['resources'] == []

    def test_custom_binary_path(self):
        items = [{'kind': 'Pod', 'metadata': {'name': 'p1'}}]
        action = _action({'kind': 'Pod', 'binary_path': '/usr/local/bin/kubectl'})
        with patch('subprocess.run', return_value=_kubectl_list(items)) as mock_run:
            action.run(task_vars={})
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == '/usr/local/bin/kubectl'

    def test_name_passed_to_kubectl(self):
        obj = {'kind': 'Pod', 'metadata': {'name': 'specific-pod'}}
        action = _action({'kind': 'Pod', 'name': 'specific-pod', 'namespace': 'default'})
        with patch('subprocess.run', return_value=_kubectl_single(obj)) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed')
        cmd = mock_run.call_args[0][0]
        assert 'specific-pod' in cmd

    def test_fast_path_sets_marker(self):
        items = [{'kind': 'Pod', 'metadata': {'name': 'p1'}}]
        action = _action({'kind': 'Pod', 'namespace': 'default'})
        with patch('subprocess.run', return_value=_kubectl_list(items)):
            result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_fast_path_failure_is_unmarked(self):
        action = _action({'namespace': 'default'})  # no kind -> failure
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert FAST_PLUGIN_MARKER not in result


class TestK8sInfoGenuineMissing:
    """The overrides now live in aflp.kubernetes_core and delegate to the genuine
    kubernetes.core collection for non-local / become tasks. When that collection
    is absent, a non-local task fails with an actionable message rather than
    running or recursing into itself."""

    def test_non_local_without_genuine_fails(self, monkeypatch):
        mod = _load_plugin('k8s_info', plugin_dir=PLUGIN_DIR)
        monkeypatch.setattr(mod, '_load_standard_action', lambda: None)
        action = _action({'kind': 'Node', 'api_version': 'v1', 'name': 'x'}, local=False)
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'not installed' in result['msg']
