from __future__ import annotations

import io
import os
import sys
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import FAST_PLUGIN_MARKER, make_action


class _FakeHeaders:
    """Minimal stand-in for http.client.HTTPMessage."""
    def __init__(self, d):
        self._d = dict(d)

    def items(self):
        return list(self._d.items())

    def get(self, key, default=None):
        return self._d.get(key, default)


class _FakeResp:
    def __init__(self, body=b'', status=200, headers=None, url='http://test/'):
        self._body = body
        self._status = status
        self._headers = _FakeHeaders(headers or {})
        self._url = url

    def getcode(self):
        return self._status

    def geturl(self):
        return self._url

    def read(self):
        return self._body

    @property
    def headers(self):
        return self._headers


def _make(args, **kwargs):
    """Build a uri action and return (action, plugin_module) for patching open_url."""
    action = make_action('uri', args, **kwargs)
    mod = sys.modules['_aflp_uri']
    return action, mod


def _http_error(url, code, body=b'', headers=None):
    return HTTPError(url, code, 'error', Message(), io.BytesIO(body))


class TestUriFastPath:
    def test_simple_get(self):
        action, mod = _make({'url': 'http://test/'})
        resp = _FakeResp(body=b'hello', headers={'Content-Type': 'text/plain'})
        with patch.object(mod, 'open_url', return_value=resp):
            result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['status'] == 200
        assert result['url'] == 'http://test/'
        assert result['content_type'] == 'text/plain'
        assert result['changed'] is False

    def test_content_only_when_requested(self):
        action, mod = _make({'url': 'http://test/', 'return_content': True})
        resp = _FakeResp(body=b'body-text', headers={'Content-Type': 'text/plain'})
        with patch.object(mod, 'open_url', return_value=resp):
            result = action.run(task_vars={})
        assert result['content'] == 'body-text'

    def test_no_content_key_without_return_content(self):
        action, mod = _make({'url': 'http://test/'})
        resp = _FakeResp(body=b'body-text', headers={'Content-Type': 'text/plain'})
        with patch.object(mod, 'open_url', return_value=resp):
            result = action.run(task_vars={})
        assert 'content' not in result

    def test_json_is_parsed(self):
        action, mod = _make({'url': 'http://test/'})
        resp = _FakeResp(body=b'{"a": 1, "b": [2, 3]}',
                         headers={'Content-Type': 'application/json'})
        with patch.object(mod, 'open_url', return_value=resp):
            result = action.run(task_vars={})
        assert result['json'] == {'a': 1, 'b': [2, 3]}

    def test_json_suffix_content_type_is_parsed(self):
        action, mod = _make({'url': 'http://test/'})
        resp = _FakeResp(body=b'{"x": true}',
                         headers={'Content-Type': 'application/vnd.api+json'})
        with patch.object(mod, 'open_url', return_value=resp):
            result = action.run(task_vars={})
        assert result['json'] == {'x': True}

    def test_non_json_has_no_json_key(self):
        action, mod = _make({'url': 'http://test/'})
        resp = _FakeResp(body=b'plain', headers={'Content-Type': 'text/plain'})
        with patch.object(mod, 'open_url', return_value=resp):
            result = action.run(task_vars={})
        assert 'json' not in result

    def test_status_code_mismatch_fails(self):
        action, mod = _make({'url': 'http://test/', 'status_code': [200]})
        resp = _FakeResp(status=500, body=b'oops', headers={'Content-Type': 'text/plain'})
        with patch.object(mod, 'open_url', return_value=resp):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert result['status'] == 500
        assert 'Status code was 500' in result['msg']

    def test_accepts_configured_status_code(self):
        action, mod = _make({'url': 'http://test/', 'status_code': [200, 404]})
        err = _http_error('http://test/', 404, body=b'missing')
        with patch.object(mod, 'open_url', side_effect=err):
            result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['status'] == 404

    def test_scalar_status_code_normalised(self):
        action, mod = _make({'url': 'http://test/', 'status_code': 201})
        resp = _FakeResp(status=201, headers={'Content-Type': 'text/plain'})
        with patch.object(mod, 'open_url', return_value=resp):
            result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['status'] == 201

    def test_comma_string_status_code_split(self):
        # `status_code: 200,204` reaches the plugin as the string "200,204";
        # stock argspec (type=list, elements=int) splits it before coercion.
        action, mod = _make({'url': 'http://test/', 'status_code': '200,204'})
        resp = _FakeResp(status=204, headers={'Content-Type': 'text/plain'})
        with patch.object(mod, 'open_url', return_value=resp):
            result = action.run(task_vars={})
        assert not result.get('failed')
        assert result['status'] == 204

    def test_http_error_treated_as_response(self):
        action, mod = _make({'url': 'http://test/', 'status_code': [404],
                             'return_content': True})
        err = _http_error('http://test/', 404, body=b'not found')
        with patch.object(mod, 'open_url', side_effect=err):
            result = action.run(task_vars={})
        assert result['status'] == 404
        assert result['content'] == 'not found'

    def test_url_error_fails_gracefully(self):
        action, mod = _make({'url': 'http://test/'})
        with patch.object(mod, 'open_url', side_effect=URLError('connection refused')):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert result['status'] == -1
        assert 'Request failed' in result['msg']

    def test_missing_url_fails(self):
        action, mod = _make({})
        result = action.run(task_vars={})
        assert result.get('failed') is True
        assert 'url is required' in result['msg']

    def test_post_json_body_encoded_and_content_type_set(self):
        action, mod = _make({
            'url': 'http://test/', 'method': 'post',
            'body': {'k': 'v'}, 'body_format': 'json',
        })
        resp = _FakeResp(status=200, headers={'Content-Type': 'application/json'})
        m = MagicMock(return_value=resp)
        with patch.object(mod, 'open_url', m):
            action.run(task_vars={})
        kwargs = m.call_args.kwargs
        assert kwargs['method'] == 'POST'
        assert kwargs['data'] == b'{"k": "v"}'
        ct = {k.lower(): v for k, v in kwargs['headers'].items()}['content-type']
        assert ct == 'application/json'

    def test_form_urlencoded_body(self):
        action, mod = _make({
            'url': 'http://test/', 'method': 'POST',
            'body': {'a': '1'}, 'body_format': 'form-urlencoded',
        })
        resp = _FakeResp(headers={'Content-Type': 'text/plain'})
        m = MagicMock(return_value=resp)
        with patch.object(mod, 'open_url', m):
            action.run(task_vars={})
        assert m.call_args.kwargs['data'] == b'a=1'

    def test_headers_passed_through(self):
        action, mod = _make({
            'url': 'http://test/',
            'headers': {'X-Custom': 'abc'},
        })
        resp = _FakeResp(headers={'Content-Type': 'text/plain'})
        m = MagicMock(return_value=resp)
        with patch.object(mod, 'open_url', m):
            action.run(task_vars={})
        assert m.call_args.kwargs['headers']['X-Custom'] == 'abc'

    def test_response_headers_lowercased(self):
        action, mod = _make({'url': 'http://test/'})
        resp = _FakeResp(headers={'Content-Type': 'text/plain', 'X-Request-Id': '42'})
        with patch.object(mod, 'open_url', return_value=resp):
            result = action.run(task_vars={})
        assert result['x_request_id'] == '42'

    def test_user_password_aliases_stay_fast(self):
        """user/password are stock-argspec aliases; they must not trigger fallback."""
        action, mod = _make({
            'url': 'http://test/', 'user': 'elastic', 'password': 's3cret',
            'force_basic_auth': True,
        })
        resp = _FakeResp(headers={'Content-Type': 'text/plain'})
        m = MagicMock(return_value=resp)
        with patch.object(mod, 'open_url', m):
            result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True
        kwargs = m.call_args.kwargs
        assert kwargs['url_username'] == 'elastic'
        assert kwargs['url_password'] == 's3cret'

    def test_fast_path_sets_marker(self):
        action, mod = _make({'url': 'http://test/'})
        resp = _FakeResp(headers={'Content-Type': 'text/plain'})
        with patch.object(mod, 'open_url', return_value=resp):
            result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_failed_status_is_unmarked(self):
        action, mod = _make({'url': 'http://test/', 'status_code': [200]})
        resp = _FakeResp(status=503, headers={'Content-Type': 'text/plain'})
        with patch.object(mod, 'open_url', return_value=resp):
            result = action.run(task_vars={})
        assert result['failed'] is True
        assert FAST_PLUGIN_MARKER not in result


class TestUriFallback:
    def test_delegates_for_non_local_connection(self):
        action = make_action('uri', {'url': 'http://test/'}, local=False)
        with patch.object(action, '_execute_module', return_value={'status': 200}) as mock_exec:
            result = action.run(task_vars={})
        mock_exec.assert_called_once()
        assert result == {'status': 200}
        assert FAST_PLUGIN_MARKER not in result

    def test_delegates_when_become_is_set(self):
        action = make_action('uri', {'url': 'http://test/'}, become=True)
        with patch.object(action, '_execute_module', return_value={'status': 200}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()

    def test_delegates_in_check_mode(self):
        action = make_action('uri', {'url': 'http://test/'}, check_mode=True)
        with patch.object(action, '_execute_module', return_value={'status': 200}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()

    def test_delegates_for_unsupported_args(self):
        """dest (file download) is unsupported in-process; must reach the module."""
        action = make_action('uri', {'url': 'http://test/', 'dest': '/tmp/out'})
        with patch.object(action, '_execute_module', return_value={'status': 200}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()

    def test_delegates_when_alias_and_canonical_conflict(self):
        """Both user and url_username set: let the module's precedence rules decide."""
        action = make_action('uri', {'url': 'http://test/', 'user': 'a',
                                     'url_username': 'b'})
        with patch.object(action, '_execute_module', return_value={'status': 200}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()

    def test_delegates_for_form_multipart(self):
        action = make_action('uri', {'url': 'http://test/', 'body_format': 'form-multipart',
                                     'body': {'f': 'x'}})
        with patch.object(action, '_execute_module', return_value={'status': 200}) as mock_exec:
            action.run(task_vars={})
        mock_exec.assert_called_once()
