from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import COLLECTION_PLUGIN_DIRS, make_action, mock_collection_run

PLUGIN_DIR = COLLECTION_PLUGIN_DIRS['kubernetes.core']
FQCN = 'kubernetes.core.plugins.action.helm_repository'


def _action(args, **kwargs):
    return make_action('helm_repository', args, plugin_dir=PLUGIN_DIR, **kwargs)


class TestHelmRepositoryFastPath:
    def test_adds_repo_successfully(self, tmp_path):
        fake_helm = tmp_path / 'helm'
        fake_helm.write_text('#!/bin/sh\nexit 0\n')
        fake_helm.chmod(0o755)
        action = _action({'name': 'myrepo', 'repo_url': 'https://example.com/charts',
                          'binary_path': str(fake_helm)})
        result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['changed'] is True
        assert result['repo_name'] == 'myrepo'
        assert result['repo_url'] == 'https://example.com/charts'

    def test_accepts_repo_name_alias(self, tmp_path):
        fake_helm = tmp_path / 'helm'
        fake_helm.write_text('#!/bin/sh\nexit 0\n')
        fake_helm.chmod(0o755)
        action = _action({'repo_name': 'alias-repo', 'repo_url': 'https://example.com',
                          'binary_path': str(fake_helm)})
        result = action.run(task_vars={})
        assert result['repo_name'] == 'alias-repo'

    def test_missing_name_returns_failed(self):
        action = _action({'repo_url': 'https://example.com/charts'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'name' in result['msg']

    def test_missing_repo_url_returns_failed(self):
        action = _action({'name': 'myrepo'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'repo_url' in result['msg']

    def test_helm_nonzero_exit_returns_failed(self, tmp_path):
        fake_helm = tmp_path / 'helm'
        fake_helm.write_text('#!/bin/sh\necho "some error" >&2\nexit 1\n')
        fake_helm.chmod(0o755)
        action = _action({'name': 'myrepo', 'repo_url': 'https://example.com/charts',
                          'binary_path': str(fake_helm)})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert result['rc'] == 1
        assert 'some error' in result['stderr']

    def test_invalid_binary_returns_failed(self):
        action = _action({'name': 'myrepo', 'repo_url': 'https://example.com/charts',
                          'binary_path': '/nonexistent/helm'})
        result = action.run(task_vars={})
        assert result['failed'] is True
        assert 'failed to run helm' in result['msg']

    def test_passes_username_and_password(self, tmp_path):
        received = tmp_path / 'args.txt'
        fake_helm = tmp_path / 'helm'
        fake_helm.write_text(f'#!/bin/sh\necho "$@" > {received}\nexit 0\n')
        fake_helm.chmod(0o755)
        action = _action({'name': 'myrepo', 'repo_url': 'https://example.com/charts',
                          'repo_username': 'user', 'repo_password': 'pass',
                          'binary_path': str(fake_helm)})
        action.run(task_vars={})
        args_out = received.read_text()
        assert '--username' in args_out
        assert '--password' in args_out

    def test_passes_force_update_flag(self, tmp_path):
        received = tmp_path / 'args.txt'
        fake_helm = tmp_path / 'helm'
        fake_helm.write_text(f'#!/bin/sh\necho "$@" > {received}\nexit 0\n')
        fake_helm.chmod(0o755)
        action = _action({'name': 'myrepo', 'repo_url': 'https://example.com/charts',
                          'force_update': True, 'binary_path': str(fake_helm)})
        action.run(task_vars={})
        assert '--force-update' in received.read_text()

    def test_passes_insecure_skip_tls_verify(self, tmp_path):
        received = tmp_path / 'args.txt'
        fake_helm = tmp_path / 'helm'
        fake_helm.write_text(f'#!/bin/sh\necho "$@" > {received}\nexit 0\n')
        fake_helm.chmod(0o755)
        action = _action({'name': 'myrepo', 'repo_url': 'https://example.com/charts',
                          'insecure_skip_tls_verify': True, 'binary_path': str(fake_helm)})
        action.run(task_vars={})
        assert '--insecure-skip-tls-verify' in received.read_text()

    def test_passes_ca_cert(self, tmp_path):
        received = tmp_path / 'args.txt'
        fake_helm = tmp_path / 'helm'
        fake_helm.write_text(f'#!/bin/sh\necho "$@" > {received}\nexit 0\n')
        fake_helm.chmod(0o755)
        action = _action({'name': 'myrepo', 'repo_url': 'https://example.com/charts',
                          'ca_cert': '/etc/ssl/ca.crt', 'binary_path': str(fake_helm)})
        action.run(task_vars={})
        assert '--ca-file' in received.read_text()


class TestHelmRepositoryFallback:
    def test_delegates_for_non_local(self):
        action = _action({'name': 'myrepo', 'repo_url': 'https://example.com/charts'},
                         local=False)
        with mock_collection_run(FQCN, {'changed': False, 'rc': 0}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_for_become(self):
        action = _action({'name': 'myrepo', 'repo_url': 'https://example.com/charts'},
                         become=True)
        with mock_collection_run(FQCN, {'changed': False, 'rc': 0}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()
