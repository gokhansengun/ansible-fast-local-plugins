from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/action-plugins')
))

from tests.conftest import make_action


def _kv1_response(data):
    return {'data': data, 'lease_duration': 3600, 'lease_id': '', 'renewable': False}


def _kv2_response(data, metadata=None):
    return {
        'data': {'data': data, 'metadata': metadata or {'version': 1}},
        'lease_duration': 0,
        'lease_id': '',
        'renewable': False,
    }


class TestHashivaultReadFastPath:
    def test_kv1_returns_value(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200',
            'token': 'root',
            'secret': 'test/mysecret',
            'mount_point': 'secret',
            'version': 1,
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v1.read_secret.return_value = _kv1_response(
            {'username': 'admin', 'password': 's3cr3t'}
        )
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['value'] == {'username': 'admin', 'password': 's3cr3t'}
        assert result['changed'] is False

    def test_kv1_key_extraction(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200',
            'token': 'root',
            'secret': 'test/mysecret',
            'mount_point': 'secret',
            'version': 1,
            'key': 'password',
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v1.read_secret.return_value = _kv1_response(
            {'username': 'admin', 'password': 's3cr3t'}
        )
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result['value'] == 's3cr3t'

    def test_kv2_returns_value(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200',
            'token': 'root',
            'secret': 'test/mysecret',
            'mount_point': 'secretv2',
            'version': 2,
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.return_value = _kv2_response(
            {'api_key': 'mykey'}
        )
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result['value'] == {'api_key': 'mykey'}
        assert result['metadata'] == {'version': 1}

    def test_kv2_key_extraction(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200',
            'token': 'root',
            'secret': 'test/mysecret',
            'mount_point': 'secretv2',
            'version': 2,
            'key': 'api_key',
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.return_value = _kv2_response(
            {'api_key': 'mykey'}
        )
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result['value'] == 'mykey'

    def test_missing_secret_arg_returns_failed(self):
        action = make_action('hashivault_read', {'url': 'http://vault:8200', 'token': 'root'})
        with patch('hvac.Client', return_value=MagicMock()):
            result = action.run(task_vars={})
        assert result.get('failed') is True
        assert 'secret is required' in result['msg']

    def test_invalid_version_returns_failed(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200',
            'token': 'root',
            'secret': 'x',
            'version': 'bad',
        })
        result = action.run(task_vars={})
        assert result.get('failed') is True

    def test_vault_exception_returns_failed(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200',
            'token': 'root',
            'secret': 'x',
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v1.read_secret.side_effect = Exception('connection refused')
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result.get('failed') is True

    def test_missing_hvac_returns_failed(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200',
            'token': 'root',
            'secret': 'x',
        })
        with patch.dict('sys.modules', {'hvac': None}):
            result = action.run(task_vars={})
        assert result.get('failed') is True
        assert 'hvac' in result['msg']

    def test_uses_env_vars_for_url_and_token(self, monkeypatch):
        monkeypatch.setenv('VAULT_ADDR', 'http://envvault:8200')
        monkeypatch.setenv('VAULT_TOKEN', 'envtoken')
        action = make_action('hashivault_read', {'secret': 'x'})
        mock_client = MagicMock()
        mock_client.secrets.kv.v1.read_secret.return_value = _kv1_response({'k': 'v'})
        captured = {}
        def fake_client(**kwargs):
            captured.update(kwargs)
            return mock_client
        with patch('hvac.Client', side_effect=fake_client):
            action.run(task_vars={})
        assert captured['url'] == 'http://envvault:8200'
        assert captured['token'] == 'envtoken'


class TestHashivaultReadFallback:
    def test_delegates_for_non_local(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200',
            'token': 'root',
            'secret': 'x',
        }, local=False)
        with patch.object(action, '_execute_module', return_value={'changed': False}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()
