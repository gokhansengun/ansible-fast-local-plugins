from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import FAST_PLUGIN_MARKER, make_action


def _kv1_response(data):
    return {'data': data, 'lease_duration': 3600, 'lease_id': '', 'renewable': False}


def _kv2_response(data, metadata=None):
    return {
        'data': {'data': data, 'metadata': metadata or {'version': 1}},
        'lease_duration': 0,
        'lease_id': '',
        'renewable': False,
    }


def _invalid_path(url='http://vault:8200/v1/kv/data/missing'):
    """hvac's exception for a path that does not exist.

    Its str() is 'None, on get <url>' — opaque, which is exactly why the plugin
    must translate it into stock's "is not in vault" wording rather than passing
    it through a generic handler.
    """
    from hvac.exceptions import InvalidPath
    return InvalidPath(None, method='get', url=url)


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
        assert result['rc'] == 0

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
        assert result['rc'] == 0

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
        assert result['rc'] == 1
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
        assert result['rc'] == 1

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
        assert result['rc'] == 1

    def test_missing_hvac_returns_failed(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200',
            'token': 'root',
            'secret': 'x',
        })
        with patch.dict('sys.modules', {'hvac': None}):
            result = action.run(task_vars={})
        assert result.get('failed') is True
        assert result['rc'] == 1
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

    def test_fast_path_sets_marker(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'secret': 'test/mysecret', 'version': 1,
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v1.read_secret.return_value = _kv1_response({'k': 'v'})
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_fast_path_failure_is_unmarked(self):
        action = make_action('hashivault_read', {'url': 'http://vault:8200', 'token': 'root'})
        with patch('hvac.Client', return_value=MagicMock()):
            result = action.run(task_vars={})  # missing secret -> failure
        assert result.get('failed') is True
        assert FAST_PLUGIN_MARKER not in result


class TestHashivaultReadNotFound:
    """Parity for the read-or-create idiom: a missing secret/key must be reported
    the way the stock module reports it, since callers branch on that message."""

    def test_kv2_missing_secret_reports_stock_message(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'mount_point': 'kv', 'secret': 'tbox/common/identity', 'version': 2,
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.side_effect = _invalid_path()
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result.get('failed') is True
        assert result['rc'] == 1
        assert result['msg'] == 'Secret kv/tbox/common/identity is not in vault'

    def test_kv1_missing_secret_reports_stock_message(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'mount_point': 'kv1-test', 'secret': 'nope', 'version': 1,
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v1.read_secret.side_effect = _invalid_path()
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result.get('failed') is True
        assert result['msg'] == 'Secret kv1-test/nope is not in vault'

    def test_empty_response_reports_stock_message(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'mount_point': 'kv', 'secret': 'empty', 'version': 2,
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.return_value = None
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result['msg'] == 'Secret kv/empty is not in vault'

    def test_not_found_failure_is_unmarked(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'mount_point': 'kv', 'secret': 'missing', 'version': 2,
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.side_effect = _invalid_path()
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert FAST_PLUGIN_MARKER not in result

    def test_missing_secret_returns_default(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'mount_point': 'kv', 'secret': 'missing', 'version': 2,
            'default': 'fallback-value',
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.side_effect = _invalid_path()
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['value'] == 'fallback-value'
        assert result['rc'] == 0
        assert result[FAST_PLUGIN_MARKER] is True

    def test_missing_key_fails(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'mount_point': 'kv', 'secret': 'test/mysecret', 'version': 2,
            'key': 'absent',
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.return_value = _kv2_response(
            {'api_key': 'mykey'}
        )
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result.get('failed') is True
        assert result['rc'] == 1
        assert result['msg'] == 'Key absent is not in secret kv/test/mysecret'

    def test_missing_key_returns_default(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'mount_point': 'kv', 'secret': 'test/mysecret', 'version': 2,
            'key': 'absent', 'default': 'fallback-value',
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.return_value = _kv2_response(
            {'api_key': 'mykey'}
        )
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['value'] == 'fallback-value'

    def test_leading_slash_secret_drops_mount_point(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'mount_point': 'kv', 'secret': '/absolute/path', 'version': 2,
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.side_effect = _invalid_path()
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result['msg'] == 'Secret absolute/path is not in vault'
        _args, kwargs = mock_client.secrets.kv.v2.read_secret_version.call_args
        assert kwargs['path'] == 'absolute/path'
        assert kwargs['mount_point'] == ''

    def test_generic_exception_uses_stock_wording(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'mount_point': 'kv', 'secret': 'x', 'version': 1,
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v1.read_secret.side_effect = Exception('connection refused')
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result.get('failed') is True
        assert result['msg'] == 'Error Exception(connection refused) reading kv/x'


class TestHashivaultReadResultShape:
    def test_lease_fields_omitted_when_absent(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'secret': 'test/mysecret', 'version': 1,
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v1.read_secret.return_value = {'data': {'k': 'v'}}
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result['value'] == {'k': 'v'}
        assert 'lease_duration' not in result
        assert 'lease_id' not in result
        assert 'renewable' not in result

    def test_lease_fields_passed_through_when_present(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'secret': 'test/mysecret', 'version': 1,
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v1.read_secret.return_value = _kv1_response({'k': 'v'})
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result['lease_duration'] == 3600
        assert result['renewable'] is False

    def test_kv1_metadata_is_empty_dict(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'secret': 'test/mysecret', 'version': 1,
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v1.read_secret.return_value = _kv1_response({'k': 'v'})
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result['metadata'] == {}


class TestHashivaultReadArgCoercion:
    """Stock coerces argument types inside AnsibleModule, which the fast path never
    runs — a templated value arrives as a string and must be coerced here."""

    def test_secret_version_string_is_coerced(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'secret': 'test/mysecret', 'version': 2, 'secret_version': '3',
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.return_value = _kv2_response({'k': 'v'})
        with patch('hvac.Client', return_value=mock_client):
            action.run(task_vars={})
        _args, kwargs = mock_client.secrets.kv.v2.read_secret_version.call_args
        assert kwargs['version'] == 3

    def test_timeout_string_is_coerced(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'secret': 'test/mysecret', 'version': 1, 'timeout': '45',
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v1.read_secret.return_value = _kv1_response({'k': 'v'})
        captured = {}

        def fake_client(**kwargs):
            captured.update(kwargs)
            return mock_client

        with patch('hvac.Client', side_effect=fake_client):
            action.run(task_vars={})
        assert captured['timeout'] == 45

    def test_verify_false_string_is_coerced(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'secret': 'test/mysecret', 'version': 1, 'verify': 'false',
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v1.read_secret.return_value = _kv1_response({'k': 'v'})
        captured = {}

        def fake_client(**kwargs):
            captured.update(kwargs)
            return mock_client

        with patch('hvac.Client', side_effect=fake_client):
            action.run(task_vars={})
        assert captured['verify'] is False

    def test_ca_cert_becomes_verify_path(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'secret': 'test/mysecret', 'version': 1, 'ca_cert': '/tmp/ca.pem',
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v1.read_secret.return_value = _kv1_response({'k': 'v'})
        captured = {}

        def fake_client(**kwargs):
            captured.update(kwargs)
            return mock_client

        with patch('hvac.Client', side_effect=fake_client):
            action.run(task_vars={})
        assert captured['verify'] == '/tmp/ca.pem'


class TestHashivaultReadArgGate:
    """Args the fast path cannot reproduce must delegate, not be silently ignored."""

    @pytest.mark.parametrize('arg,value', [
        ('username', 'admin'),
        ('password', 's3cr3t'),
        ('role_id', 'rid'),
        ('secret_id', 'sid'),
        ('login_mount_point', 'ldap'),
        ('aws_header', 'hdr'),
        ('validate_certs', False),  # not a stock argument at all
    ])
    def test_unsupported_arg_delegates(self, arg, value):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root', 'secret': 'x', arg: value,
        })
        with patch.object(action, '_execute_module', return_value={'changed': False}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_non_token_authtype_delegates(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root', 'secret': 'x', 'authtype': 'approle',
        })
        with patch.object(action, '_execute_module', return_value={'changed': False}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_token_authtype_stays_on_fast_path(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root',
            'secret': 'test/mysecret', 'version': 1, 'authtype': 'token',
        })
        mock_client = MagicMock()
        mock_client.secrets.kv.v1.read_secret.return_value = _kv1_response({'k': 'v'})
        with patch('hvac.Client', return_value=mock_client):
            result = action.run(task_vars={})
        assert result['value'] == {'k': 'v'}
        assert result[FAST_PLUGIN_MARKER] is True

    def test_gated_delegation_is_unmarked(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root', 'secret': 'x', 'username': 'admin',
        })
        with patch.object(action, '_execute_module', return_value={'changed': False}):
            result = action.run(task_vars={})
        assert FAST_PLUGIN_MARKER not in result


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

    def test_fallback_result_is_unmarked(self):
        action = make_action('hashivault_read', {
            'url': 'http://vault:8200', 'token': 'root', 'secret': 'x',
        }, local=False)
        with patch.object(action, '_execute_module', return_value={'changed': False}):
            result = action.run(task_vars={})
        assert FAST_PLUGIN_MARKER not in result
