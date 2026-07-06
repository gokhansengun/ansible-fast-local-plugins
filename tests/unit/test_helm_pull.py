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
        action = _action({'chart_ref': 'bitnami/nginx', 'destination': str(tmp_path),
                          'binary_path': str(_fake_helm(tmp_path))})
        result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['changed'] is True
        assert result['rc'] == 0
        assert 'pull' in result['command']

    def test_repo_url_alias(self, tmp_path):
        """url is a genuine-argspec alias for repo_url; without normalization
        the pull silently ran without --repo."""
        rec = tmp_path / 'args.txt'
        action = _action({'chart_ref': 'nginx', 'url': 'https://example.com/charts',
                          'destination': str(tmp_path),
                          'binary_path': str(_fake_helm(tmp_path, record=rec))})
        result = action.run(task_vars={})
        assert not result.get('failed'), result
        out = rec.read_text()
        assert '--repo' in out
        assert 'https://example.com/charts' in out

    def test_unsupported_arg_delegates(self):
        """untar_dir does not exist in the genuine argspec; the fast path must
        delegate so the genuine module rejects it, not silently ignore it."""
        action = _action({'chart_ref': 'nginx', 'destination': '/tmp',
                          'untar_dir': '/tmp/charts'})
        with patch.object(action, '_execute_module', return_value={'failed': True}) as m:
            action.run(task_vars={})
        m.assert_called_once()

    def test_missing_chart_ref_returns_failed(self):
        action = _action({'chart_version': '1.0.0'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'chart_ref' in result['msg']

    def test_missing_destination_returns_failed(self, tmp_path):
        """destination is required by the genuine argspec; the fast path must not
        accept a task the fallback would reject."""
        action = _action({'chart_ref': 'nginx',
                          'binary_path': str(_fake_helm(tmp_path))})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'destination' in result['msg']

    def test_helm_nonzero_exit_returns_failed(self, tmp_path):
        action = _action({'chart_ref': 'bitnami/nginx', 'destination': str(tmp_path),
                          'binary_path': str(_fake_helm(tmp_path, 'echo "boom" >&2\nexit 1\n'))})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert result['rc'] == 1
        assert 'boom' in result['stderr']

    def test_invalid_binary_returns_failed(self):
        action = _action({'chart_ref': 'bitnami/nginx', 'destination': '/tmp',
                          'binary_path': '/nonexistent/helm'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'failed to run helm' in result['msg']

    def test_passes_version_and_repo(self, tmp_path):
        rec = tmp_path / 'args.txt'
        action = _action({'chart_ref': 'nginx', 'chart_version': '1.2.3',
                          'repo_url': 'https://example.com/charts',
                          'destination': str(tmp_path),
                          'binary_path': str(_fake_helm(tmp_path, record=rec))})
        action.run(task_vars={})
        out = rec.read_text()
        assert '--version' in out and '1.2.3' in out
        assert '--repo' in out

    def test_passes_credentials(self, tmp_path):
        rec = tmp_path / 'args.txt'
        action = _action({'chart_ref': 'nginx', 'repo_username': 'u', 'repo_password': 'p',
                          'pass_credentials': True, 'destination': str(tmp_path),
                          'binary_path': str(_fake_helm(tmp_path, record=rec))})
        action.run(task_vars={})
        out = rec.read_text()
        assert '--username' in out and '--password' in out and '--pass-credentials' in out

    def test_passes_untar_chart_destination_and_ca_cert(self, tmp_path):
        """Genuine param names (untar_chart, destination, chart_ca_cert) — the
        override previously read invented names (untar, chart_destination,
        ca_cert) and silently ignored these."""
        rec = tmp_path / 'args.txt'
        action = _action({'chart_ref': 'nginx', 'untar_chart': True,
                          'destination': '/tmp/dl', 'chart_ca_cert': '/tmp/ca.pem',
                          'binary_path': str(_fake_helm(tmp_path, record=rec))})
        action.run(task_vars={})
        out = rec.read_text()
        assert '--untar' in out
        assert '--destination' in out and '/tmp/dl' in out
        assert '--ca-file' in out and '/tmp/ca.pem' in out

    def test_passes_insecure_skip_tls_verify(self, tmp_path):
        rec = tmp_path / 'args.txt'
        action = _action({'chart_ref': 'nginx', 'skip_tls_certs_check': True,
                          'destination': str(tmp_path),
                          'binary_path': str(_fake_helm(tmp_path, record=rec))})
        action.run(task_vars={})
        assert '--insecure-skip-tls-verify' in rec.read_text()

    def test_fast_path_sets_marker(self, tmp_path):
        action = _action({'chart_ref': 'bitnami/nginx', 'destination': str(tmp_path),
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
