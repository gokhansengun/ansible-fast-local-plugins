from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import (
    COLLECTION_PLUGIN_DIRS,
    FAST_PLUGIN_MARKER,
    _load_plugin,
    make_action,
    mock_collection_run,
    shadow_collection_import,
)

PLUGIN_DIR = COLLECTION_PLUGIN_DIRS['kubernetes.core']
FQCN = 'kubernetes.core.plugins.action.helm_info'

# A fake helm that returns a deployed-release status for `helm status` and a
# values document for `helm get values`. Anything else exits non-zero.
_HELM_OK = (
    'if [ "$1" = "status" ]; then\n'
    '  echo \'{"name":"test-release","namespace":"test-ns","version":3,'
    '"info":{"status":"deployed"}}\'\n'
    '  exit 0\n'
    'elif [ "$1" = "get" ] && [ "$2" = "values" ]; then\n'
    '  echo \'{"replicaCount":2}\'\n'
    '  exit 0\n'
    'fi\n'
    'exit 1\n'
)


def _action(args, **kwargs):
    return make_action('helm_info', args, plugin_dir=PLUGIN_DIR, **kwargs)


def _fake_helm(tmp_path, body=_HELM_OK, record=None):
    fake = tmp_path / 'helm'
    if record is not None:
        fake.write_text(f'#!/bin/sh\necho "$@" > {record}\n{body}')
    else:
        fake.write_text('#!/bin/sh\n' + body)
    fake.chmod(0o755)
    return fake


class TestHelmInfoFastPath:
    def test_reads_release_status(self, tmp_path):
        action = _action({'release_name': 'test-release', 'release_namespace': 'test-ns',
                          'binary_path': str(_fake_helm(tmp_path))})
        result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['changed'] is False
        assert result['status']['name'] == 'test-release'
        assert result['status']['info']['status'] == 'deployed'

    def test_missing_release_name_returns_failed(self):
        action = _action({'release_namespace': 'test-ns'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'release_name' in result['msg']

    def test_release_not_found_is_not_an_error(self, tmp_path):
        body = 'echo "Error: release: not found" >&2\nexit 1\n'
        action = _action({'release_name': 'absent', 'release_namespace': 'test-ns',
                          'binary_path': str(_fake_helm(tmp_path, body=body))})
        result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['changed'] is False
        assert result['status'] is None

    def test_helm_error_returns_failed(self, tmp_path):
        body = 'echo "boom" >&2\nexit 1\n'
        action = _action({'release_name': 'r', 'release_namespace': 'ns',
                          'binary_path': str(_fake_helm(tmp_path, body=body))})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'boom' in result['msg']

    def test_invalid_binary_returns_failed(self):
        action = _action({'release_name': 'r', 'binary_path': '/nonexistent/helm'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'failed to run helm' in result['msg']

    def test_release_state_filter_excludes_unmatched(self, tmp_path):
        # status reports 'deployed', but only 'failed' is accepted -> no status.
        action = _action({'release_name': 'test-release', 'release_namespace': 'test-ns',
                          'release_state': ['failed'],
                          'binary_path': str(_fake_helm(tmp_path))})
        result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['status'] is None

    def test_get_all_values_attaches_values(self, tmp_path):
        action = _action({'release_name': 'test-release', 'release_namespace': 'test-ns',
                          'get_all_values': True,
                          'binary_path': str(_fake_helm(tmp_path))})
        result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['status']['values'] == {'replicaCount': 2}

    def test_passes_namespace_and_kubeconfig(self, tmp_path):
        rec = tmp_path / 'args.txt'
        action = _action({'release_name': 'r', 'release_namespace': 'myns',
                          'kubeconfig': '/tmp/kc', 'context': 'myctx',
                          'binary_path': str(_fake_helm(tmp_path, record=rec))})
        action.run(task_vars={})
        out = rec.read_text()
        assert '--namespace' in out and 'myns' in out
        assert '--kubeconfig' in out
        assert '--kube-context' in out

    def test_fast_path_sets_marker(self, tmp_path):
        action = _action({'release_name': 'test-release', 'release_namespace': 'test-ns',
                          'binary_path': str(_fake_helm(tmp_path))})
        result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_fast_path_failure_is_unmarked(self):
        action = _action({'release_namespace': 'test-ns'})  # no release_name -> failure
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert FAST_PLUGIN_MARKER not in result


class TestHelmInfoFallback:
    def test_delegates_for_non_local(self):
        action = _action({'release_name': 'r'}, local=False)
        with mock_collection_run(FQCN, {'changed': False, 'status': {}}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_for_become(self):
        action = _action({'release_name': 'r'}, become=True)
        with mock_collection_run(FQCN, {'changed': False, 'status': {}}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_fallback_result_is_unmarked(self):
        action = _action({'release_name': 'r'}, local=False)
        with mock_collection_run(FQCN, {'changed': False, 'status': {}}):
            result = action.run(task_vars={})
        assert FAST_PLUGIN_MARKER not in result


class TestHelmInfoFallbackRecursion:
    """When this collection shadows kubernetes.core, the fallback import resolves
    to this very plugin; delegating used to recurse until 'maximum recursion depth
    exceeded'. become on a local connection must use the fast path; non-local
    connections must fail with an actionable message."""

    def _shadow(self):
        return shadow_collection_import(
            FQCN, _load_plugin('helm_info', plugin_dir=PLUGIN_DIR))

    def test_become_uses_fast_path_when_self_shadowed(self, tmp_path):
        action = _action({'release_name': 'test-release', 'release_namespace': 'test-ns',
                          'binary_path': str(_fake_helm(tmp_path))}, become=True)
        with self._shadow():
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['status']['name'] == 'test-release'

    def test_non_local_fails_cleanly_when_self_shadowed(self):
        action = _action({'release_name': 'r'}, local=False)
        with self._shadow():
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'local connection' in result['msg']
