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
                 'aflp', 'kubernetes_core', 'plugins', 'action', 'k8s.py')
)
_spec = importlib.util.spec_from_file_location('_aflp_k8s', _PLUGIN_PATH)
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
        defaults = dict(server_side=False, field_manager=None, force_conflicts=False,
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
            self._call(server_side=True, force_conflicts=True)
        cmd = mock_run.call_args[0][0]
        assert '--force-conflicts' in cmd

    def test_never_passes_client_side_force(self):
        """kubectl apply --force deletes and recreates the resource; the task's
        force argument means replace in kubernetes.core, so it never lands here."""
        with patch('subprocess.run', return_value=_proc(stdout='{}')) as mock_run:
            self._call(server_side=False)
        cmd = mock_run.call_args[0][0]
        assert '--force' not in cmd

    def test_uses_src_file(self):
        obj = _obj()
        with patch('subprocess.run', return_value=_proc(stdout=json.dumps(obj))) as mock_run:
            self._call(src='/path/cm.yaml')
        cmd = mock_run.call_args[0][0]
        assert '/path/cm.yaml' in cmd


# ------------------------------------------------------------------ #
# _kubectl_get                                                         #
# ------------------------------------------------------------------ #
class TestKubectlGet:
    def _call(self, **kwargs):
        defaults = dict(kind='ConfigMap', api_version='v1', name='my-cm',
                        namespace='ns', kubeconfig=None, context=None,
                        binary_path='kubectl')
        defaults.update(kwargs)
        return _mod._kubectl_get(**defaults)

    def test_returns_parsed_object(self):
        with patch('subprocess.run', return_value=_proc(stdout=json.dumps(_obj()))):
            assert self._call()['kind'] == 'ConfigMap'

    def test_returns_none_when_absent(self):
        # --ignore-not-found exits 0 with empty stdout
        with patch('subprocess.run', return_value=_proc(stdout='\n')):
            assert self._call() is None

    def test_nonzero_exit_raises(self):
        with patch('subprocess.run', return_value=_proc(1, stderr='forbidden')):
            with pytest.raises(RuntimeError, match='forbidden'):
                self._call()

    def test_subprocess_exception_raises(self):
        with patch('subprocess.run', side_effect=OSError('no kubectl')):
            with pytest.raises(RuntimeError, match='failed to run kubectl get'):
                self._call()

    def test_uses_ignore_not_found_and_namespace(self):
        with patch('subprocess.run', return_value=_proc(stdout='')) as mock_run:
            self._call()
        cmd = mock_run.call_args[0][0]
        assert '--ignore-not-found' in cmd
        assert '--namespace' in cmd and 'ns' in cmd

    def test_non_core_resource_type(self):
        with patch('subprocess.run', return_value=_proc(stdout='')) as mock_run:
            self._call(kind='Deployment', api_version='apps/v1', name='web')
        assert 'deployment.apps' in mock_run.call_args[0][0]


# ------------------------------------------------------------------ #
# _kubectl_create / _kubectl_replace                                   #
# ------------------------------------------------------------------ #
class TestKubectlCreateReplace:
    def _call(self, fn, manifest_str='{}', **kwargs):
        defaults = dict(kubeconfig=None, context=None, binary_path='kubectl')
        defaults.update(kwargs)
        return fn(manifest_str=manifest_str, **defaults)

    def test_create_verb_and_stdin(self):
        manifest = json.dumps(_obj())
        with patch('subprocess.run', return_value=_proc(stdout=manifest)) as mock_run:
            result = self._call(_mod._kubectl_create, manifest)
        cmd = mock_run.call_args[0][0]
        assert cmd[:2] == ['kubectl', 'create']
        assert mock_run.call_args[1]['input'] == manifest
        assert result['kind'] == 'ConfigMap'

    def test_replace_verb(self):
        with patch('subprocess.run', return_value=_proc(stdout='{}')) as mock_run:
            self._call(_mod._kubectl_replace)
        assert mock_run.call_args[0][0][:2] == ['kubectl', 'replace']

    def test_nonzero_exit_raises(self):
        with patch('subprocess.run', return_value=_proc(1, stderr='already exists')):
            with pytest.raises(RuntimeError, match='already exists'):
                self._call(_mod._kubectl_create)

    def test_subprocess_exception_raises(self):
        with patch('subprocess.run', side_effect=OSError('no kubectl')):
            with pytest.raises(RuntimeError, match='failed to run kubectl create'):
                self._call(_mod._kubectl_create)


# ------------------------------------------------------------------ #
# _merge_types / _objects_match / _resolve_objects                     #
# ------------------------------------------------------------------ #
class TestMergeTypes:
    def test_default_order_matches_genuine(self):
        assert _mod._merge_types(None) == ['strategic-merge', 'merge']

    def test_explicit_list_is_kept(self):
        assert _mod._merge_types(['merge']) == ['merge']

    def test_bare_string_is_comma_split(self):
        assert _mod._merge_types('json,merge') == ['json', 'merge']


class TestObjectsMatch:
    def test_identical_objects_match(self):
        assert _mod._objects_match(_obj(), _obj()) is True

    def test_resource_version_only_diff_matches(self):
        before, after = _obj(), _obj()
        before['metadata']['resourceVersion'] = '1'
        after['metadata']['resourceVersion'] = '2'
        after['metadata']['generation'] = 3
        assert _mod._objects_match(before, after) is True

    def test_real_diff_does_not_match(self):
        before, after = _obj(), _obj()
        after['data'] = {'k': 'v'}
        assert _mod._objects_match(before, after) is False


class TestResolveObjects:
    def test_arguments_fill_gaps_in_definition(self):
        objs = _mod._resolve_objects({'spec': {'replicas': 2}}, None, 'Deployment',
                                     'apps/v1', 'web', 'ns')
        assert objs == [{'spec': {'replicas': 2}, 'apiVersion': 'apps/v1',
                         'kind': 'Deployment',
                         'metadata': {'name': 'web', 'namespace': 'ns'}}]

    def test_definition_wins_over_arguments(self):
        objs = _mod._resolve_objects({'kind': 'ConfigMap', 'metadata': {'name': 'a'}},
                                     None, 'Deployment', 'apps/v1', 'b', 'ns')
        assert objs[0]['kind'] == 'ConfigMap'
        assert objs[0]['metadata']['name'] == 'a'

    def test_multidoc_yields_every_object(self):
        objs = _mod._resolve_objects(_load_template('rbac_multidoc.yml'), None,
                                     None, 'v1', None, None)
        assert [o['kind'] for o in objs] == ['ServiceAccount', 'ClusterRole',
                                             'ClusterRoleBinding']

    def test_plain_list_definition_yields_every_object(self):
        objs = _mod._resolve_objects([_obj('a'), _obj('b')], None, None, 'v1',
                                     None, None)
        assert [o['metadata']['name'] for o in objs] == ['a', 'b']

    def test_no_definition_builds_manifest_from_arguments(self):
        objs = _mod._resolve_objects(None, None, 'ConfigMap', 'v1', 'cm', 'ns')
        assert objs == [{'apiVersion': 'v1', 'kind': 'ConfigMap',
                         'metadata': {'name': 'cm', 'namespace': 'ns'}}]

    def test_src_file_is_read(self, tmp_path):
        src = tmp_path / 'cm.yml'
        src.write_text('apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: from-src\n')
        objs = _mod._resolve_objects(None, str(src), None, 'v1', None, 'ns')
        assert objs[0]['metadata'] == {'name': 'from-src', 'namespace': 'ns'}


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
    """state=present without apply=true is create-or-patch, never `kubectl apply`.

    kubernetes.core defaults apply to false, i.e. an existing resource is
    PATCHed with exactly the fields the manifest carries.  `kubectl apply`
    instead three-way-merges against last-applied-configuration and *deletes*
    every field the manifest omits.
    """

    def _absent(self):
        return _proc(stdout='')            # kubectl get --ignore-not-found

    def _live(self, obj=None):
        return _proc(stdout=json.dumps(obj or _obj()))

    def test_creates_resource_when_absent(self):
        action = _action({'kind': 'ConfigMap', 'name': 'cm', 'namespace': 'default',
                          'definition': _obj()})
        with patch('subprocess.run', side_effect=[self._absent(), self._live()]) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True
        assert result['method'] == 'create'
        assert mock_run.call_args_list[1][0][0][:2] == ['kubectl', 'create']

    def test_patches_resource_when_it_exists(self):
        live = _obj()
        patched = _obj()
        patched['data'] = {'key': 'value'}
        action = _action({'definition': patched})
        with patch('subprocess.run',
                   side_effect=[self._live(live), self._live(patched)]) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True
        assert result['method'] == 'update'
        assert mock_run.call_args_list[1][0][0][:2] == ['kubectl', 'patch']

    def test_never_calls_kubectl_apply_without_apply_true(self):
        """The regression: a partial definition applied over a resource that was
        created with `kubectl apply` used to null out every omitted field."""
        partial = {'apiVersion': 'apps/v1', 'kind': 'Deployment',
                   'metadata': {'name': 'cattle-cluster-agent', 'namespace': 'cattle-system'},
                   'spec': {'template': {'spec': {'hostAliases': [{'ip': '1.2.3.4'}]}}}}
        live = {'apiVersion': 'apps/v1', 'kind': 'Deployment',
                'metadata': {'name': 'cattle-cluster-agent', 'namespace': 'cattle-system'},
                'spec': {'selector': {'matchLabels': {'app': 'cattle'}}}}
        action = _action({'state': 'present', 'definition': partial})
        with patch('subprocess.run',
                   side_effect=[self._live(live), self._live(live)]) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        verbs = [call[0][0][1] for call in mock_run.call_args_list]
        assert verbs == ['get', 'patch']
        assert 'apply' not in verbs
        # the patch body carries only what the manifest asked for
        patch_cmd = mock_run.call_args_list[1][0][0]
        body = json.loads(patch_cmd[patch_cmd.index('--patch') + 1])
        assert body['spec'] == {'template': {'spec': {'hostAliases': [{'ip': '1.2.3.4'}]}}}

    def test_noop_when_patch_changes_nothing(self):
        live = _obj()
        action = _action({'kind': 'ConfigMap', 'name': 'cm', 'namespace': 'default',
                          'definition': _obj()})
        with patch('subprocess.run', side_effect=[self._live(live), self._live(live)]):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is False

    def test_resource_version_only_change_is_not_a_change(self):
        live, after = _obj(), _obj()
        live['metadata']['resourceVersion'] = '11'
        after['metadata']['resourceVersion'] = '12'
        action = _action({'definition': _obj()})
        with patch('subprocess.run', side_effect=[self._live(live), self._live(after)]):
            result = action.run(task_vars={})
        assert result['changed'] is False

    def test_falls_back_to_next_merge_type(self):
        """strategic-merge is rejected for CRDs; genuine retries with merge."""
        live = _obj()
        action = _action({'definition': _obj()})
        side = [self._live(live),
                _proc(1, stderr='unable to find api field in struct'),
                self._live(live)]
        with patch('subprocess.run', side_effect=side) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        types = [c[0][0][c[0][0].index('--type') + 1]
                 for c in mock_run.call_args_list[1:]]
        assert types == ['strategic', 'merge']

    def test_force_replaces_instead_of_patching(self):
        live = _obj()
        action = _action({'definition': _obj(), 'force': True})
        with patch('subprocess.run',
                   side_effect=[self._live(live), self._live(live)]) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['method'] == 'replace'
        assert mock_run.call_args_list[1][0][0][:2] == ['kubectl', 'replace']

    def test_fast_path_sets_marker(self):
        action = _action({'kind': 'ConfigMap', 'name': 'cm', 'namespace': 'default',
                          'definition': _obj()})
        with patch('subprocess.run', side_effect=[self._absent(), self._live()]):
            result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_noop_result_is_marked(self):
        live = _obj()
        action = _action({'kind': 'ConfigMap', 'name': 'cm', 'namespace': 'default',
                          'definition': _obj()})
        with patch('subprocess.run', side_effect=[self._live(live), self._live(live)]):
            result = action.run(task_vars={})
        assert result['changed'] is False
        assert result[FAST_PLUGIN_MARKER] is True

    def test_fast_path_failure_is_unmarked(self):
        action = _action({'namespace': 'default'})  # no definition/src/kind -> failure
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert FAST_PLUGIN_MARKER not in result

    def test_get_failure_returns_failed(self):
        action = _action({'definition': _obj()})
        with patch('subprocess.run', return_value=_proc(1, stderr='forbidden')):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'forbidden' in result['msg']

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
        multiple k8s objects in a single stream.  All 3 must be reconciled,
        not just the first document.
        """
        action = _action({'state': 'present',
                          'definition': _load_template('rbac_multidoc.yml')})
        # get(absent) + create, three times over
        side = [self._absent(), _proc(stdout='{}')] * 3
        with patch('subprocess.run', side_effect=side) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True
        created = [json.loads(c[1]['input'])['kind']
                   for c in mock_run.call_args_list if c[0][0][1] == 'create']
        assert created == ['ServiceAccount', 'ClusterRole', 'ClusterRoleBinding']
        # several objects -> genuine's aggregated results shape
        assert len(result['result']['results']) == 3

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
        created = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRole',
                   'metadata': {'name': 'registry-creds-reader'}}
        with patch('subprocess.run',
                   side_effect=[self._absent(), _proc(stdout=json.dumps(created))]):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True

    def test_create_failure_returns_failed(self):
        action = _action({'definition': _obj()})
        with patch('subprocess.run',
                   side_effect=[self._absent(), _proc(1, stderr='server error')]):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'server error' in result['msg']

    def test_uses_src_file(self, tmp_path):
        src = tmp_path / 'cm.yaml'
        src.write_text('apiVersion: v1\nkind: ConfigMap\nmetadata:\n'
                       '  name: from-src\n  namespace: ns\n')
        action = _action({'src': str(src)})
        with patch('subprocess.run', side_effect=[self._absent(), _proc(stdout='{}')]) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True
        assert json.loads(mock_run.call_args_list[1][1]['input'])['metadata']['name'] == 'from-src'

    def test_unreadable_src_returns_failed(self):
        action = _action({'src': '/manifests/does-not-exist.yaml'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'failed to read src' in result['msg']

    def test_minimal_manifest_built_from_kind_name(self):
        action = _action({'kind': 'ConfigMap', 'name': 'minimal', 'namespace': 'ns',
                          'api_version': 'v1'})
        with patch('subprocess.run', side_effect=[self._absent(), _proc(stdout='{}')]) as mock_run:
            action.run(task_vars={})
        manifest = json.loads(mock_run.call_args_list[1][1]['input'])
        assert manifest['kind'] == 'ConfigMap'
        assert manifest['metadata']['name'] == 'minimal'


# ------------------------------------------------------------------ #
# ActionModule – apply: true                                           #
# ------------------------------------------------------------------ #
class TestK8sFastPathApply:
    def _apply_result(self, obj=None):
        return _proc(stdout=json.dumps(obj or _obj()))

    def test_apply_true_uses_kubectl_apply(self):
        action = _action({'definition': _obj(), 'apply': True})
        with patch('subprocess.run', side_effect=[_proc(1), self._apply_result()]) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True
        assert [c[0][0][1] for c in mock_run.call_args_list] == ['diff', 'apply']

    def test_noop_when_diff_shows_no_changes(self):
        action = _action({'definition': _obj(), 'apply': True})
        with patch('subprocess.run', return_value=_proc(0)):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is False

    def test_applies_when_diff_fails(self):
        """A diff error (rc=2, permission denied etc.) should not abort — assume changes."""
        action = _action({'definition': _obj(), 'apply': True})
        with patch('subprocess.run', side_effect=[_proc(2, stderr='forbidden'),
                                                   self._apply_result()]):
            result = action.run(task_vars={})
        assert result['changed'] is True

    def test_apply_failure_returns_failed(self):
        action = _action({'definition': _obj(), 'apply': True})
        with patch('subprocess.run', side_effect=[_proc(1),
                                                   _proc(1, stderr='server error')]):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'server error' in result['msg']

    def test_uses_src_file(self):
        action = _action({'src': '/manifests/cm.yaml', 'apply': True})
        with patch('subprocess.run', side_effect=[_proc(1), self._apply_result()]) as mock_run:
            result = action.run(task_vars={})
        assert result['changed'] is True
        # both diff and apply calls should use the src path (no local read needed)
        for call in mock_run.call_args_list:
            assert '/manifests/cm.yaml' in call[0][0]

    def test_server_side_apply_flags_forwarded(self):
        action = _action({'definition': _obj(), 'apply': True,
                          'server_side_apply': {'field_manager': 'my-ctrl',
                                                'force_conflicts': True}})
        with patch('subprocess.run', side_effect=[_proc(1), self._apply_result()]) as mock_run:
            action.run(task_vars={})
        apply_cmd = mock_run.call_args_list[1][0][0]
        assert '--server-side' in apply_cmd
        assert '--field-manager' in apply_cmd
        assert '--force-conflicts' in apply_cmd


# ------------------------------------------------------------------ #
# ActionModule – state: latest                                         #
# ------------------------------------------------------------------ #
class TestK8sUnsupportedArgs:
    def test_unsupported_arg_delegates(self):
        """append_hash changes the created object's name; the fast path must
        delegate to genuine rather than silently ignore it."""
        action = _action({'state': 'present', 'definition': _obj('c', 'ConfigMap', 'ns'),
                          'append_hash': True})
        with mock_collection_run(FQCN, {'changed': True, 'result': {}}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_validate_certs_true_delegates(self):
        action = _action({'state': 'absent', 'kind': 'ConfigMap', 'name': 'c',
                          'validate_certs': True})
        with mock_collection_run(FQCN, {'changed': False, 'result': {}}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_validate_certs_false_stays_fast_with_flag(self):
        action = _action({'state': 'absent', 'kind': 'ConfigMap', 'name': 'c',
                          'namespace': 'ns', 'validate_certs': False})
        with patch('subprocess.run', return_value=_proc(stdout='configmap/c\n')) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result[FAST_PLUGIN_MARKER] is True
        cmd = mock_run.call_args[0][0]
        assert '--insecure-skip-tls-verify' in cmd


class TestK8sFastPathLatest:
    def test_noop_when_patch_changes_nothing(self):
        """state=latest is idempotent like state=present."""
        live = _proc(stdout=json.dumps(_obj()))
        action = _action({'state': 'latest', 'definition': _obj()})
        with patch('subprocess.run', side_effect=[live, live]):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is False

    def test_creates_when_absent(self):
        action = _action({'state': 'latest', 'definition': _obj()})
        with patch('subprocess.run', side_effect=[_proc(stdout=''),
                                                   _proc(stdout=json.dumps(_obj()))]):
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

    def test_api_alias_selects_api_version(self):
        """`api`/`version` are genuine-argspec aliases for api_version; without
        normalization the delete silently targeted api_version=v1."""
        action = _action({'state': 'absent', 'kind': 'Deployment', 'name': 'web',
                          'namespace': 'default', 'api': 'apps/v1'})
        with patch('subprocess.run', return_value=_proc(stdout='deployment.apps/web\n')) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        cmd = ' '.join(mock_run.call_args[0][0])
        assert 'deployment.apps' in cmd

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
        live = _obj()
        patched = _obj()
        patched['metadata']['labels'] = {'env': 'test'}
        action = _action({'state': 'patched', 'kind': 'ConfigMap',
                          'name': 'my-cm', 'namespace': 'default',
                          'definition': {'metadata': {'labels': {'env': 'test'}}}})
        with patch('subprocess.run',
                   side_effect=[_proc(stdout=json.dumps(live)),
                                _proc(stdout=json.dumps(patched))]) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is True
        assert [c[0][0][1] for c in mock_run.call_args_list] == ['get', 'patch']

    def test_missing_resource_is_not_created(self):
        """Genuine warns and leaves the cluster alone rather than creating."""
        action = _action({'state': 'patched', 'kind': 'ConfigMap', 'name': 'ghost',
                          'namespace': 'default',
                          'definition': {'metadata': {'labels': {'env': 'test'}}}})
        with patch('subprocess.run', return_value=_proc(stdout='')) as mock_run:
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['changed'] is False
        assert mock_run.call_count == 1                      # get only, no create
        assert 'patched' in result['warnings'][0]

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
        """Every merge_type is tried; the last error surfaces."""
        action = _action({'state': 'patched', 'kind': 'ConfigMap', 'name': 'cm',
                          'definition': {'metadata': {'labels': {'x': 'y'}}}})
        side = [_proc(stdout=json.dumps(_obj())),
                _proc(1, stderr='strategic unsupported'),
                _proc(1, stderr='patch rejected')]
        with patch('subprocess.run', side_effect=side):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'patch rejected' in result['msg']

    def test_merge_type_forwarded(self):
        obj = _proc(stdout=json.dumps(_obj()))
        action = _action({'state': 'patched', 'kind': 'ConfigMap', 'name': 'cm',
                          'definition': {'metadata': {}},
                          'merge_type': 'json'})
        with patch('subprocess.run', side_effect=[obj, obj]) as mock_run:
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

    def test_apply_false_stays_on_the_fast_path(self):
        """apply=false is the kubernetes.core default (create-or-patch); the fast
        path implements it, so there is nothing to delegate."""
        action = _action({'definition': _obj(), 'apply': False})
        with patch('subprocess.run',
                   side_effect=[_proc(stdout=''), _proc(stdout=json.dumps(_obj()))]):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result[FAST_PLUGIN_MARKER] is True

    def test_fallback_result_is_unmarked(self):
        action = _action({'definition': _obj()}, local=False)
        with mock_collection_run(FQCN, {'changed': False, 'result': {}}):
            result = action.run(task_vars={})
        assert FAST_PLUGIN_MARKER not in result

    def test_wait_delegation_result_is_unmarked(self):
        action = _action({'definition': _obj(), 'wait': True})
        with mock_collection_run(FQCN, {'changed': False, 'result': {}}):
            result = action.run(task_vars={})
        assert FAST_PLUGIN_MARKER not in result


class TestK8sGenuineMissing:
    """The overrides now live in aflp.kubernetes_core and delegate to the genuine
    kubernetes.core collection for non-local / become tasks and for unsupported
    args (wait/template). When that collection is absent, those paths fail with
    an actionable message rather than running or recursing."""

    def test_non_local_without_genuine_fails(self, monkeypatch):
        mod = _load_plugin('k8s', plugin_dir=PLUGIN_DIR)
        monkeypatch.setattr(mod, '_load_standard_action', lambda: None)
        action = _action({'definition': _obj()}, local=False)
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'not installed' in result['msg']

    def test_wait_without_genuine_fails(self, monkeypatch):
        mod = _load_plugin('k8s', plugin_dir=PLUGIN_DIR)
        monkeypatch.setattr(mod, '_load_standard_action', lambda: None)
        action = _action({'definition': _obj(), 'wait': True})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'wait' in result['msg']
