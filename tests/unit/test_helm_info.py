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
)

PLUGIN_DIR = COLLECTION_PLUGIN_DIRS['aflp.kubernetes_core']
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

    def test_name_and_namespace_aliases(self, tmp_path):
        """name/namespace are genuine-argspec aliases for release_name /
        release_namespace; without normalization helm was run without -n and
        queried the default namespace."""
        rec = tmp_path / 'args.txt'
        action = _action({'name': 'test-release', 'namespace': 'test-ns',
                          'binary_path': str(_fake_helm(tmp_path, record=rec))})
        result = action.run(task_vars={})
        assert not result.get('failed'), result
        out = rec.read_text()
        assert '--namespace' in out
        assert 'test-ns' in out

    def test_unsupported_arg_delegates(self):
        """ca_cert is honoured by genuine helm_info but not read by the fast
        path; it must delegate rather than silently ignore it."""
        action = _action({'release_name': 'r', 'release_namespace': 'ns',
                          'ca_cert': '/tmp/ca.pem'})
        with mock_collection_run(FQCN, {'changed': False, 'status': {}}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

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


class TestHelmInfoGenuineMissing:
    """The overrides now live in aflp.kubernetes_core and delegate to the genuine
    kubernetes.core collection for non-local / become tasks. When that collection
    is absent, a non-local task fails with an actionable message rather than
    running or recursing into itself."""

    def test_non_local_without_genuine_fails(self, monkeypatch):
        mod = _load_plugin('helm_info', plugin_dir=PLUGIN_DIR)
        monkeypatch.setattr(mod, '_load_standard_action', lambda: None)
        action = _action({'release_name': 'r'}, local=False)
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'not installed' in result['msg']
