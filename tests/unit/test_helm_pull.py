from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from unittest.mock import patch

from tests.conftest import (
    COLLECTION_PLUGIN_DIRS,
    FAST_PLUGIN_MARKER,
    make_action,
)

PLUGIN_DIR = COLLECTION_PLUGIN_DIRS['aflp.kubernetes_core']


def _action(args, **kwargs):
    return make_action('helm_pull', args, plugin_dir=PLUGIN_DIR, **kwargs)


def _fake_helm(tmp_path, body='exit 0\n', record=None):
    fake = tmp_path / 'helm'
    if record is not None:
        fake.write_text(f'#!/bin/sh\necho "$@" > {record}\n{body}')
    else:
        fake.write_text('#!/bin/sh\n' + body)
    fake.chmod(0o755)
    return fake


class TestHelmPullFastPath:
    def test_pulls_chart_successfully(self, tmp_path):
        action = _action({'chart_ref': 'bitnami/nginx',
                          'binary_path': str(_fake_helm(tmp_path))})
        result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['changed'] is True
        assert result['rc'] == 0
        assert 'pull' in result['command']

    def test_missing_chart_ref_returns_failed(self):
        action = _action({'chart_version': '1.0.0'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'chart_ref' in result['msg']

    def test_helm_nonzero_exit_returns_failed(self, tmp_path):
        action = _action({'chart_ref': 'bitnami/nginx',
                          'binary_path': str(_fake_helm(tmp_path, 'echo "boom" >&2\nexit 1\n'))})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert result['rc'] == 1
        assert 'boom' in result['stderr']

    def test_invalid_binary_returns_failed(self):
        action = _action({'chart_ref': 'bitnami/nginx', 'binary_path': '/nonexistent/helm'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'failed to run helm' in result['msg']

    def test_passes_version_and_repo(self, tmp_path):
        rec = tmp_path / 'args.txt'
        action = _action({'chart_ref': 'nginx', 'chart_version': '1.2.3',
                          'repo_url': 'https://example.com/charts',
                          'binary_path': str(_fake_helm(tmp_path, record=rec))})
        action.run(task_vars={})
        out = rec.read_text()
        assert '--version' in out and '1.2.3' in out
        assert '--repo' in out

    def test_passes_credentials(self, tmp_path):
        rec = tmp_path / 'args.txt'
        action = _action({'chart_ref': 'nginx', 'repo_username': 'u', 'repo_password': 'p',
                          'pass_credentials': True,
                          'binary_path': str(_fake_helm(tmp_path, record=rec))})
        action.run(task_vars={})
        out = rec.read_text()
        assert '--username' in out and '--password' in out and '--pass-credentials' in out

    def test_passes_untar_and_destination(self, tmp_path):
        rec = tmp_path / 'args.txt'
        action = _action({'chart_ref': 'nginx', 'untar': True, 'untar_dir': '/tmp/charts',
                          'chart_destination': '/tmp/dl',
                          'binary_path': str(_fake_helm(tmp_path, record=rec))})
        action.run(task_vars={})
        out = rec.read_text()
        assert '--untar' in out and '--untardir' in out and '--destination' in out

    def test_passes_insecure_skip_tls_verify(self, tmp_path):
        rec = tmp_path / 'args.txt'
        action = _action({'chart_ref': 'nginx', 'skip_tls_certs_check': True,
                          'binary_path': str(_fake_helm(tmp_path, record=rec))})
        action.run(task_vars={})
        assert '--insecure-skip-tls-verify' in rec.read_text()

    def test_fast_path_sets_marker(self, tmp_path):
        action = _action({'chart_ref': 'bitnami/nginx',
                          'binary_path': str(_fake_helm(tmp_path))})
        result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_fast_path_failure_is_unmarked(self):
        action = _action({'chart_version': '1.0.0'})  # no chart_ref -> failure
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert FAST_PLUGIN_MARKER not in result


class TestHelmPullFallback:
    """Genuine kubernetes.core.helm_pull is module-only (no action plugin), so the
    override delegates via self._execute_module (the genuine module), not by
    instantiating a standard action class like the other overrides."""

    def test_delegates_for_non_local(self):
        action = _action({'chart_ref': 'bitnami/nginx'}, local=False)
        with patch.object(action, '_execute_module', return_value={'changed': True}) as m:
            action.run(task_vars={})
        m.assert_called_once()

    def test_delegates_for_become(self):
        action = _action({'chart_ref': 'bitnami/nginx'}, become=True)
        with patch.object(action, '_execute_module', return_value={'changed': True}) as m:
            action.run(task_vars={})
        m.assert_called_once()

    def test_fallback_result_is_unmarked(self):
        action = _action({'chart_ref': 'bitnami/nginx'}, local=False)
        with patch.object(action, '_execute_module', return_value={'changed': True}):
            result = action.run(task_vars={})
        assert FAST_PLUGIN_MARKER not in result
