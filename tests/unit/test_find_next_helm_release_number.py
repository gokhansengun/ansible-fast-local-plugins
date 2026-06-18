from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock
import subprocess

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

import find_next_helm_release_number as mod
from tests.conftest import FAST_PLUGIN_MARKER, make_action


def _kubectl_result(versions):
    """Simulate kubectl output: header line + one version per line."""
    lines = ['VERSION'] + [str(v) for v in versions]
    return MagicMock(returncode=0, stdout='\n'.join(lines) + '\n', stderr='')


class TestGetCurrentVersion:
    def test_single_version(self):
        with patch('subprocess.run', return_value=_kubectl_result([3])):
            assert mod._get_current_version('myrelease', 'mynamespace') == 3

    def test_multiple_versions_returns_highest(self):
        with patch('subprocess.run', return_value=_kubectl_result([1, 3, 2])):
            assert mod._get_current_version('myrelease', 'mynamespace') == 3

    def test_no_versions_returns_none(self):
        with patch('subprocess.run', return_value=_kubectl_result([])):
            assert mod._get_current_version('myrelease', 'mynamespace') is None

    def test_kubectl_failure_raises(self):
        fail = MagicMock(returncode=1, stderr='connection refused', stdout='')
        with patch('subprocess.run', return_value=fail):
            with pytest.raises(RuntimeError, match='connection refused'):
                mod._get_current_version('myrelease', 'mynamespace')

    def test_non_integer_version_raises(self):
        bad = MagicMock(returncode=0, stdout='VERSION\nbadvalue\n', stderr='')
        with patch('subprocess.run', return_value=bad):
            with pytest.raises(RuntimeError, match='non-integer'):
                mod._get_current_version('myrelease', 'mynamespace')


class TestFindNextHelmReleaseNumberFastPath:
    def test_returns_next_version(self):
        action = make_action('find_next_helm_release_number', {
            'release_name': 'myrelease',
            'namespace': 'mynamespace',
        })
        with patch('subprocess.run', return_value=_kubectl_result([3])):
            result = action.run(task_vars={})
        assert not result.get('failed'), result
        assert result['current_version'] == 3
        assert result['next_version'] == 4
        assert result['changed'] is False

    def test_no_existing_release_returns_version_1(self):
        action = make_action('find_next_helm_release_number', {
            'release_name': 'newrelease',
            'namespace': 'mynamespace',
        })
        with patch('subprocess.run', return_value=_kubectl_result([])):
            result = action.run(task_vars={})
        assert result['current_version'] == 0
        assert result['next_version'] == 1

    def test_picks_highest_of_multiple_versions(self):
        action = make_action('find_next_helm_release_number', {
            'release_name': 'myrelease',
            'namespace': 'mynamespace',
        })
        with patch('subprocess.run', return_value=_kubectl_result([1, 5, 3])):
            result = action.run(task_vars={})
        assert result['current_version'] == 5
        assert result['next_version'] == 6

    def test_missing_release_name_returns_failed(self):
        action = make_action('find_next_helm_release_number', {'namespace': 'ns'})
        result = action.run(task_vars={})
        assert result.get('failed') is True

    def test_missing_namespace_returns_failed(self):
        action = make_action('find_next_helm_release_number', {'release_name': 'r'})
        result = action.run(task_vars={})
        assert result.get('failed') is True

    def test_kubectl_failure_returns_failed(self):
        action = make_action('find_next_helm_release_number', {
            'release_name': 'r',
            'namespace': 'ns',
        })
        fail = MagicMock(returncode=1, stderr='not found', stdout='')
        with patch('subprocess.run', return_value=fail):
            result = action.run(task_vars={})
        assert result.get('failed') is True

    def test_result_has_modified_at(self):
        action = make_action('find_next_helm_release_number', {
            'release_name': 'r',
            'namespace': 'ns',
        })
        with patch('subprocess.run', return_value=_kubectl_result([1])):
            result = action.run(task_vars={})
        assert isinstance(result.get('modified_at'), int)
        assert result['modified_at'] > 0

    def test_fast_path_sets_marker(self):
        action = make_action('find_next_helm_release_number', {
            'release_name': 'r', 'namespace': 'ns',
        })
        with patch('subprocess.run', return_value=_kubectl_result([1])):
            result = action.run(task_vars={})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_fast_path_failure_is_unmarked(self):
        action = make_action('find_next_helm_release_number', {'namespace': 'ns'})  # no release_name
        result = action.run(task_vars={})
        assert result.get('failed') is True
        assert FAST_PLUGIN_MARKER not in result


class TestFindNextHelmFallback:
    def test_delegates_for_non_local(self):
        action = make_action('find_next_helm_release_number', {
            'release_name': 'r',
            'namespace': 'ns',
        }, local=False)
        with patch.object(action, '_execute_module', return_value={'changed': False}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_fallback_result_is_unmarked(self):
        action = make_action('find_next_helm_release_number', {
            'release_name': 'r', 'namespace': 'ns',
        }, local=False)
        with patch.object(action, '_execute_module', return_value={'changed': False}):
            result = action.run(task_vars={})
        assert FAST_PLUGIN_MARKER not in result
