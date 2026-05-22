from __future__ import annotations

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load_template(name):
    with open(os.path.join(_TEMPLATES_DIR, name)) as f:
        return f.read()

# Load the collection plugin module directly to test module-level helpers.
_PLUGIN_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..',
                 'ansible', 'collections', 'ansible_collections',
                 'kubernetes', 'core', 'plugins', 'action', 'k8s.py')
)
_spec = importlib.util.spec_from_file_location('_aflp_k8s', _PLUGIN_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from tests.conftest import COLLECTION_PLUGIN_DIRS, make_action, mock_collection_run

PLUGIN_DIR = COLLECTION_PLUGIN_DIRS['kubernetes.core']
FQCN = 'kubernetes.core.plugins.action.k8s'


def _action(args, **kwargs):
    return make_action('k8s', args, plugin_dir=PLUGIN_DIR, **kwargs)


# ------------------------------------------------------------------ helpers --


def _proc(returncode=0, stdout='', stderr=''):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def _obj(name='test-obj', kind='ConfigMap', namespace='default'):
    return {
        'apiVersion': 'v1',
        'kind': kind,
        'metadata': {'name': name, 'namespace': namespace},
    }


# ------------------------------------------------------------------ #
# _to_manifest_str                                                     #
# ------------------------------------------------------------------ #
class TestToManifestStr:
    def test_dict_input(self):
        d = {'apiVersion': 'v1', 'kind': 'ConfigMap'}
        out = _mod._to_manifest_str(d)
        assert json.loads(out) == d

    def test_list_input(self):
        lst = [{'kind': 'ConfigMap'}, {'kind': 'Secret'}]
        out = _mod._to_manifest_str(lst)
        assert json.loads(out) == lst

    def test_json_string_input(self):
        d = {'apiVersion': 'v1', 'kind': 'ConfigMap'}
        out = _mod._to_manifest_str(json.dumps(d))
        assert json.loads(out) == d

    def test_yaml_string_input(self):
        yaml_str = (
            'apiVersion: rbac.authorization.k8s.io/v1\n'
            'kind: ClusterRole\n'
            'metadata:\n'
            '  name: registry-creds-reader\n'
            '  namespace: default\n'
            'rules:\n'
            '- apiGroups: [""]\n'
            '  resources: ["secrets"]\n'
            '  verbs: ["get", "list"]\n'
        )
        out = _mod._to_manifest_str(yaml_str)
        parsed = json.loads(out)
        assert parsed['apiVersion'] == 'rbac.authorization.k8s.io/v1'
        assert parsed['kind'] == 'ClusterRole'
        assert parsed['metadata']['name'] == 'registry-creds-reader'
        assert parsed['rules'][0]['resources'] == ['secrets']

    def test_yaml_multidoc_string_input(self):
        # lookup('template', ...) can return a multi-document YAML (3 objects
        # separated by ---).  _to_manifest_str must return a JSON list of all 3.
        out = _mod._to_manifest_str(_load_template('rbac_multidoc.yml'))
        parsed = json.loads(out)
        assert parsed['kind'] == 'List', 'expected a k8s List for multi-document YAML'
        assert len(parsed['items']) == 3
        kinds = [obj['kind'] for obj in parsed['items']]
        assert kinds == ['ServiceAccount', 'ClusterRole', 'ClusterRoleBinding']

    def test_invalid_string_raises(self):
        # A plain scalar is valid YAML but not a mapping/sequence — must fail.
        with pytest.raises(ValueError, match='not valid JSON'):
            _mod._to_manifest_str('just plain text')

    def test_unsupported_type_raises(self):
        with pytest.raises(ValueError, match='dict, list, or JSON string'):
            _mod._to_manifest_str(42)


# ------------------------------------------------------------------ #
# _kubectl_diff                                                        #
# ------------------------------------------------------------------ #
class TestKubectlDiff:
    def _call(self, manifest_str='{}', src=None, **kwargs):
        defaults = dict(kubeconfig=None, context=None, binary_path='kubectl')
        defaults.update(kwargs)
        return _mod._kubectl_diff(manifest_str=manifest_str, src=src, **defaults)

    def test_exit_0_means_no_changes(self):
        with patch('subprocess.run', return_value=_proc(0)):
            assert self._call() is False

    def test_exit_1_means_changes(self):
        with patch('subprocess.run', return_value=_proc(1)):
            assert self._call() is True

    def test_exit_2_raises_runtime_error(self):
        with patch('subprocess.run', return_value=_proc(2, stderr='forbidden')):
            with pytest.raises(RuntimeError, match='kubectl diff error'):
                self._call()

    def test_subprocess_exception_raises(self):
        with patch('subprocess.run', side_effect=FileNotFoundError('no kubectl')):
            with pytest.raises(RuntimeError, match='failed to run kubectl diff'):
                self._call()

    def test_passes_kubeconfig(self):
        with patch('subprocess.run', return_value=_proc(0)) as mock_run:
            self._call(kubeconfig='/kube/config')
        cmd = mock_run.call_args[0][0]
        assert '--kubeconfig' in cmd
        assert '/kube/config' in cmd

    def test_uses_src_flag_when_src_given(self):
        with patch('subprocess.run', return_value=_proc(0)) as mock_run:
            self._call(src='/manifests/cm.yaml')
        cmd = mock_run.call_args[0][0]
        assert '-f' in cmd
        assert '/manifests/cm.yaml' in cmd
        # no stdin when src is set
        assert mock_run.call_args[1].get('input') is None

    def test_pipes_stdin_when_no_src(self):
        manifest = json.dumps({'kind': 'ConfigMap'})
        with patch('subprocess.run', return_value=_proc(0)) as mock_run:
            self._call(manifest_str=manifest)
        assert mock_run.call_args[1]['input'] == manifest


# ------------------------------------------------------------------ #
# _kubectl_apply                                                       #
# ------------------------------------------------------------------ #
class TestKubectlApply:
    def _call(self, manifest_str='{}', src=None, **kwargs):
        defaults = dict(server_side=False, field_manager=None, force=False,
                        kubeconfig=None, context=None, binary_path='kubectl')
        defaults.update(kwargs)
        return _mod._kubectl_apply(manifest_str=manifest_str, src=src, **defaults)

    def test_returns_parsed_object(self):
        obj = _obj()
        with patch('subprocess.run', return_value=_proc(stdout=json.dumps(obj))):
            result = self._call()
        assert result['kind'] == 'ConfigMap'

    def test_nonzero_exit_raises(self):
        with patch('subprocess.run', return_value=_proc(1, stderr='no resource')):
            with pytest.raises(RuntimeError, match='no resource'):
                self._call()

    def test_subprocess_exception_raises(self):
        with patch('subprocess.run', side_effect=OSError('not found')):
            with pytest.raises(RuntimeError, match='failed to run kubectl apply'):
                self._call()

    def test_invalid_json_returns_empty_dict(self):
        with patch('subprocess.run', return_value=_proc(stdout='not-json')):
            result = self._call()
        assert result == {}

    def test_server_side_flag(self):
        with patch('subprocess.run', return_value=_proc(stdout='{}')) as mock_run:
            self._call(server_side=True)
        cmd = mock_run.call_args[0][0]
        assert '--server-side' in cmd

    def test_field_manager_flag(self):
        with patch('subprocess.run', return_value=_proc(stdout='{}')) as mock_run:
            self._call(server_side=True, field_manager='my-controller')
        cmd = mock_run.call_args[0][0]
        assert '--field-manager' in cmd
        assert 'my-controller' in cmd

    def test_force_conflicts_flag_for_server_side(self):
        with patch('subprocess.run', return_value=_proc(stdout='{}')) as mock_run:
            self._call(server_side=True, force=True)
        cmd = mock_run.call_args[0][0]
        assert '--force-conflicts' in cmd

    def test_force_flag_for_client_side(self):
        with patch('subprocess.run', return_value=_proc(stdout='{}')) as mock_run:
            self._call(server_side=False, force=True)
        cmd = mock_run.call_args[0][0]
        assert '--force' in cmd

    def test_uses_src_file(self):
        obj = _obj()
        with patch('subprocess.run', return_value=_proc(stdout=json.dumps(obj))) as mock_run:
            self._call(src='/path/cm.yaml')
        cmd = mock_run.call_args[0][0]
        assert '/path/cm.yaml' in cmd


# ------------------------------------------------------------------ #
# _kubectl_delete                                                      #
# ------------------------------------------------------------------ #
class TestKubectlDelete:
    def _call(self, **kwargs):
        defaults = dict(kind='ConfigMap', api_version='v1', name='my-cm',
                        namespace=None, label_selectors=[], field_selectors=[],
                        kubeconfig=None, context=None, binary_path='kubectl')
        defaults.update(kwargs)
        return _mod._kubectl_delete(**defaults)

    def test_returns_true_when_resource_deleted(self):
        with patch('subprocess.run', return_value=_proc(stdout='configmap/my-cm\n')):
            assert self._call() is True

    def test_returns_false_when_nothing_deleted(self):
        with patch('subprocess.run', return_value=_proc(stdout='')):
            assert self._call() is False

    def test_nonzero_exit_raises(self):
        with patch('subprocess.run', return_value=_proc(1, stderr='server error')):
            with pytest.raises(RuntimeError, match='server error'):
                self._call()

    def test_subprocess_exception_raises(self):
        with patch('subprocess.run', side_effect=OSError('no kubectl')):
            with pytest.raises(RuntimeError, match='failed to run kubectl delete'):
                self._call()

    def test_passes_namespace(self):
        with patch('subprocess.run', return_value=_proc(stdout='')) as mock_run:
            self._call(namespace='test-ns')
        cmd = mock_run.call_args[0][0]
        assert '--namespace' in cmd
        assert 'test-ns' in cmd

    def test_passes_label_selector(self):
        with patch('subprocess.run', return_value=_proc(stdout='')) as mock_run:
            self._call(name=None, label_selectors=['app=web'])
        cmd = mock_run.call_args[0][0]
        assert '--selector' in cmd
        assert 'app=web' in cmd

    def test_ignore_not_found_flag(self):
        with patch('subprocess.run', return_value=_proc(stdout='')) as mock_run:
            self._call()
        cmd = mock_run.call_args[0][0]
        assert '--ignore-not-found' in cmd

    def test_output_name_flag(self):
        with patch('subprocess.run', return_value=_proc(stdout='')) as mock_run:
            self._call()
        cmd = mock_run.call_args[0][0]
        assert '--output=name' in cmd

    def test_non_core_resource_type(self):
        with patch('subprocess.run', return_value=_proc(stdout='')) as mock_run:
            self._call(kind='Deployment', api_version='apps/v1', name='my-deploy')
        cmd = mock_run.call_args[0][0]
        assert 'deployment.apps' in cmd


# ------------------------------------------------------------------ #
# _kubectl_patch                                                       #
# ------------------------------------------------------------------ #
class TestKubectlPatch:
    def _call(self, **kwargs):
        defaults = dict(kind='ConfigMap', api_version='v1', name='my-cm',
                        namespace=None, patch_data={'metadata': {'labels': {'env': 'test'}}},
                        merge_type=None, kubeconfig=None, context=None, binary_path='kubectl')
        defaults.update(kwargs)
        return _mod._kubectl_patch(**defaults)

    def test_returns_parsed_object(self):
        obj = _obj()
        with patch('subprocess.run', return_value=_proc(stdout=json.dumps(obj))):
            result = self._call()
        assert result['kind'] == 'ConfigMap'

    def test_nonzero_exit_raises(self):
        with patch('subprocess.run', return_value=_proc(1, stderr='not found')):
            with pytest.raises(RuntimeError, match='not found'):
                self._call()

    def test_subprocess_exception_raises(self):
        with patch('subprocess.run', side_effect=OSError('no kubectl')):
            with pytest.raises(RuntimeError, match='failed to run kubectl patch'):
                self._call()

    def test_invalid_json_output_raises(self):
        with patch('subprocess.run', return_value=_proc(stdout='bad-json')):
            with pytest.raises(RuntimeError, match='failed to parse kubectl patch output'):
                self._call()

    def test_default_type_is_strategic(self):
        with patch('subprocess.run', return_value=_proc(stdout='{}')) as mock_run:
            self._call()
        cmd = mock_run.call_args[0][0]
        assert '--type' in cmd
        idx = cmd.index('--type')
        assert cmd[idx + 1] == 'strategic'

    def test_strategic_merge_alias(self):
        with patch('subprocess.run', return_value=_proc(stdout='{}')) as mock_run:
            self._call(merge_type='strategic-merge')
        cmd = mock_run.call_args[0][0]
        idx = cmd.index('--type')
        assert cmd[idx + 1] == 'strategic'

    def test_merge_type(self):
        with patch('subprocess.run', return_value=_proc(stdout='{}')) as mock_run:
            self._call(merge_type='merge')
        cmd = mock_run.call_args[0][0]
        idx = cmd.index('--type')
        assert cmd[idx + 1] == 'merge'

    def test_json_patch_type(self):
        with patch('subprocess.run', return_value=_proc(stdout='{}')) as mock_run:
            self._call(merge_type='json')
        cmd = mock_run.call_args[0][0]
        idx = cmd.index('--type')
        assert cmd[idx + 1] == 'json'

    def test_passes_namespace(self):
        with patch('subprocess.run', return_value=_proc(stdout='{}')) as mock_run:
            self._call(namespace='staging')
        cmd = mock_run.call_args[0][0]
        assert '--namespace' in cmd
        assert 'staging' in cmd


# ------------------------------------------------------------------ #
# ActionModule – state: present                                        #
# ------------------------------------------------------------------ #
class TestK8sFastPathPresent:
    def _apply_result(self, obj=None):
        return _proc(stdout=json.dumps(obj or _obj()))

    def test_creates_resource_when_diff_shows_changes(self):
        action = _action({'kind': 'ConfigMap', 'name': 'cm', 'namespace': 'default',
                          'definition': _obj()})
        with patch('subprocess.run', side_effect=[_proc(1), self._apply_result()]):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True

    def test_noop_when_diff_shows_no_changes(self):
        action = _action({'kind': 'ConfigMap', 'name': 'cm', 'namespace': 'default',
                          'definition': _obj()})
        with patch('subprocess.run', return_value=_proc(0)):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is False

    def test_applies_when_diff_fails(self):
        """A diff error (rc=2, permission denied etc.) should not abort — assume changes."""
        action = _action({'kind': 'ConfigMap', 'name': 'cm', 'namespace': 'default',
                          'definition': _obj()})
        with patch('subprocess.run', side_effect=[_proc(2, stderr='forbidden'),
                                                   self._apply_result()]):
            result = action.run(task_vars={})
        assert result['changed'] is True

    def test_missing_definition_src_and_kind_returns_failed(self):
        action = _action({'namespace': 'default'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'definition' in result['msg'] or 'kind' in result['msg']

    def test_invalid_definition_string_returns_failed(self):
        # A plain scalar is valid YAML but not a k8s manifest — must fail.
        action = _action({'definition': 'just plain text'})
        result = action.run(task_vars={})
        assert result['failed'] is True

    def test_definition_as_yaml_multidoc_string_from_template_lookup(self):
        """definition is a multi-document YAML string (3 objects separated by ---).

        lookup('template', 'rbac.yml.j2') can render a file that contains
        multiple k8s objects in a single stream.  All 3 must reach kubectl,
        not just the first document.
        """
        action = _action({'state': 'present',
                          'definition': _load_template('rbac_multidoc.yml')})
        applied = [
            {'apiVersion': 'v1', 'kind': 'ServiceAccount',
             'metadata': {'name': 'registry-creds'}},
            {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRole',
             'metadata': {'name': 'registry-creds-reader'}},
            {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRoleBinding',
             'metadata': {'name': 'registry-creds-binding'}},
        ]
        with patch('subprocess.run', side_effect=[_proc(1),
                                                   _proc(stdout=json.dumps(applied))]) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True
        # All 3 objects must have been sent to kubectl as a List, not just the first.
        diff_input = mock_run.call_args_list[0][1]['input']
        manifest = json.loads(diff_input)
        assert manifest['kind'] == 'List', 'expected a k8s List sent to kubectl for multi-doc YAML'
        assert len(manifest['items']) == 3

    def test_definition_as_yaml_string_from_template_lookup(self):
        """definition passed as a YAML string (as returned by lookup('template', ...))."""
        yaml_str = (
            'apiVersion: rbac.authorization.k8s.io/v1\n'
            'kind: ClusterRole\n'
            'metadata:\n'
            '  name: registry-creds-reader\n'
            '  namespace: default\n'
            'rules:\n'
            '- apiGroups: [""]\n'
            '  resources: ["secrets"]\n'
            '  verbs: ["get", "list"]\n'
        )
        action = _action({'state': 'present', 'definition': yaml_str})
        applied = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRole',
                   'metadata': {'name': 'registry-creds-reader'}}
        with patch('subprocess.run', side_effect=[_proc(1),
                                                   _proc(stdout=json.dumps(applied))]):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True

    def test_apply_failure_returns_failed(self):
        action = _action({'definition': _obj()})
        with patch('subprocess.run', side_effect=[_proc(1),
                                                   _proc(1, stderr='server error')]):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'server error' in result['msg']

    def test_uses_src_file(self):
        action = _action({'src': '/manifests/cm.yaml'})
        with patch('subprocess.run', side_effect=[_proc(1), self._apply_result()]) as mock_run:
            result = action.run(task_vars={})
        assert result['changed'] is True
        # both diff and apply calls should use the src path
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert '/manifests/cm.yaml' in cmd

    def test_minimal_manifest_built_from_kind_name(self):
        action = _action({'kind': 'ConfigMap', 'name': 'minimal', 'namespace': 'ns',
                          'api_version': 'v1'})
        with patch('subprocess.run', side_effect=[_proc(1), self._apply_result()]) as mock_run:
            action.run(task_vars={})
        diff_input = mock_run.call_args_list[0][1]['input']
        manifest = json.loads(diff_input)
        assert manifest['kind'] == 'ConfigMap'
        assert manifest['metadata']['name'] == 'minimal'

    def test_server_side_apply_flag_forwarded(self):
        action = _action({'definition': _obj(),
                          'server_side_apply': {'field_manager': 'my-ctrl'}})
        with patch('subprocess.run', side_effect=[_proc(1), self._apply_result()]) as mock_run:
            action.run(task_vars={})
        apply_cmd = mock_run.call_args_list[1][0][0]
        assert '--server-side' in apply_cmd
        assert '--field-manager' in apply_cmd


# ------------------------------------------------------------------ #
# ActionModule – state: latest                                         #
# ------------------------------------------------------------------ #
class TestK8sFastPathLatest:
    def test_noop_when_no_diff(self):
        """state=latest is idempotent like state=present."""
        action = _action({'state': 'latest', 'definition': _obj()})
        with patch('subprocess.run', return_value=_proc(0)):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is False

    def test_applies_when_diff_shows_changes(self):
        action = _action({'state': 'latest', 'definition': _obj()})
        with patch('subprocess.run', side_effect=[_proc(1), _proc(stdout=json.dumps(_obj()))]):
            result = action.run(task_vars={})
        assert result['changed'] is True


# ------------------------------------------------------------------ #
# ActionModule – state: absent                                         #
# ------------------------------------------------------------------ #
class TestK8sFastPathAbsent:
    def test_deletes_resource_returns_changed_true(self):
        action = _action({'state': 'absent', 'kind': 'ConfigMap',
                          'name': 'my-cm', 'namespace': 'default'})
        with patch('subprocess.run', return_value=_proc(stdout='configmap/my-cm\n')):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True

    def test_noop_when_not_found(self):
        action = _action({'state': 'absent', 'kind': 'ConfigMap',
                          'name': 'ghost', 'namespace': 'default'})
        with patch('subprocess.run', return_value=_proc(stdout='')):
            result = action.run(task_vars={})
        assert result['changed'] is False

    def test_missing_kind_and_definition_returns_failed(self):
        action = _action({'state': 'absent', 'name': 'my-cm'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'kind' in result['msg'] or 'definition' in result['msg']

    def test_absent_with_single_object_definition(self):
        action = _action({'state': 'absent', 'definition': _obj('my-cm', 'ConfigMap', 'default')})
        with patch('subprocess.run', return_value=_proc(stdout='configmap/my-cm\n')):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True

    def test_absent_with_single_object_definition_noop_when_not_found(self):
        action = _action({'state': 'absent', 'definition': _obj('ghost', 'ConfigMap', 'default')})
        with patch('subprocess.run', return_value=_proc(stdout='')):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is False

    def test_absent_with_multidoc_definition(self):
        multidoc = _load_template('rbac_multidoc.yml')
        # Three sequential delete calls: all return something deleted.
        action = _action({'state': 'absent', 'definition': multidoc})
        side = [_proc(stdout='serviceaccount/registry-creds\n'),
                _proc(stdout='clusterrole/registry-creds-reader\n'),
                _proc(stdout='clusterrolebinding/registry-creds-binding\n')]
        with patch('subprocess.run', side_effect=side):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True

    def test_absent_with_multidoc_definition_noop_when_all_gone(self):
        multidoc = _load_template('rbac_multidoc.yml')
        action = _action({'state': 'absent', 'definition': multidoc})
        with patch('subprocess.run', return_value=_proc(stdout='')):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is False

    def test_absent_with_definition_missing_name_returns_failed(self):
        bad_obj = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {}}
        action = _action({'state': 'absent', 'definition': bad_obj})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'metadata.name' in result['msg']

    def test_absent_with_definition_delete_failure_returns_failed(self):
        action = _action({'state': 'absent', 'definition': _obj('my-cm', 'ConfigMap', 'default')})
        with patch('subprocess.run', return_value=_proc(1, stderr='forbidden')):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'forbidden' in result['msg']

    def test_delete_failure_returns_failed(self):
        action = _action({'state': 'absent', 'kind': 'ConfigMap', 'name': 'my-cm'})
        with patch('subprocess.run', return_value=_proc(1, stderr='forbidden')):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'forbidden' in result['msg']

    def test_deletes_by_label_selector(self):
        action = _action({'state': 'absent', 'kind': 'ConfigMap',
                          'label_selectors': ['app=test'], 'namespace': 'default'})
        with patch('subprocess.run', return_value=_proc(stdout='configmap/test-cm\n')) as mock_run:
            result = action.run(task_vars={})
        cmd = mock_run.call_args[0][0]
        assert '--selector' in cmd
        assert result['changed'] is True


# ------------------------------------------------------------------ #
# ActionModule – state: patched                                        #
# ------------------------------------------------------------------ #
class TestK8sFastPathPatched:
    def test_patches_resource(self):
        obj = _obj()
        action = _action({'state': 'patched', 'kind': 'ConfigMap',
                          'name': 'my-cm', 'namespace': 'default',
                          'definition': {'metadata': {'labels': {'env': 'test'}}}})
        with patch('subprocess.run', return_value=_proc(stdout=json.dumps(obj))):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True

    def test_missing_kind_returns_failed(self):
        action = _action({'state': 'patched', 'name': 'cm',
                          'definition': {'metadata': {}}})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'kind' in result['msg']

    def test_missing_name_returns_failed(self):
        action = _action({'state': 'patched', 'kind': 'ConfigMap',
                          'definition': {'metadata': {}}})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'name' in result['msg']

    def test_missing_definition_returns_failed(self):
        action = _action({'state': 'patched', 'kind': 'ConfigMap', 'name': 'cm'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'definition' in result['msg']

    def test_patch_failure_returns_failed(self):
        action = _action({'state': 'patched', 'kind': 'ConfigMap', 'name': 'cm',
                          'definition': {'metadata': {'labels': {'x': 'y'}}}})
        with patch('subprocess.run', return_value=_proc(1, stderr='not found')):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'not found' in result['msg']

    def test_merge_type_forwarded(self):
        obj = _obj()
        action = _action({'state': 'patched', 'kind': 'ConfigMap', 'name': 'cm',
                          'definition': {'metadata': {}},
                          'merge_type': 'json'})
        with patch('subprocess.run', return_value=_proc(stdout=json.dumps(obj))) as mock_run:
            action.run(task_vars={})
        cmd = mock_run.call_args[0][0]
        idx = cmd.index('--type')
        assert cmd[idx + 1] == 'json'


# ------------------------------------------------------------------ #
# ActionModule – unknown state                                         #
# ------------------------------------------------------------------ #
class TestK8sFastPathUnknownState:
    def test_unknown_state_returns_failed(self):
        action = _action({'state': 'bogus', 'kind': 'ConfigMap'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'unsupported state' in result['msg']


# ------------------------------------------------------------------ #
# ActionModule – fallback / delegation                                 #
# ------------------------------------------------------------------ #
class TestK8sFallback:
    def test_delegates_for_non_local(self):
        action = _action({'definition': _obj()}, local=False)
        with mock_collection_run(FQCN, {'changed': False, 'result': {}}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_for_become(self):
        action = _action({'definition': _obj()}, become=True)
        with mock_collection_run(FQCN, {'changed': False, 'result': {}}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_when_wait_set(self):
        action = _action({'definition': _obj(), 'wait': True})
        with mock_collection_run(FQCN, {'changed': False, 'result': {}}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_when_wait_condition_set(self):
        action = _action({'definition': _obj(),
                          'wait_condition': {'type': 'Ready', 'status': 'True'}})
        with mock_collection_run(FQCN, {'changed': False, 'result': {}}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_when_template_set(self):
        action = _action({'template': {'path': '/tmpl/cm.j2'}})
        with mock_collection_run(FQCN, {'changed': True, 'result': {}}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_when_apply_false(self):
        action = _action({'definition': _obj(), 'apply': False})
        with mock_collection_run(FQCN, {'changed': True, 'result': {}}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()
