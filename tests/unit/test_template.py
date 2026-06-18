from __future__ import annotations

import hashlib
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))

from tests.conftest import FAST_PLUGIN_MARKER, make_action, mock_builtin_run


def _make_template_action(tmp_path, template_content, dest_name='out.txt',
                           extra_args=None, **kwargs):
    """Helper: writes a template file to tmp_path and wires up the action."""
    src = tmp_path / 'test.j2'
    src.write_text(template_content)
    dest = str(tmp_path / dest_name)
    args = {'src': str(src), 'dest': dest}
    if extra_args:
        args.update(extra_args)
    action = make_action('template', args, **kwargs)
    # _find_needle must resolve to the real file path
    action._find_needle = MagicMock(return_value=str(src))
    return action, dest


class TestTemplateFastPath:
    def test_renders_simple_variable(self, tmp_path):
        action, dest = _make_template_action(tmp_path, 'Hello {{ name }}!')
        result = action.run(task_vars={'name': 'World'})
        assert not result.get('failed'), result
        assert result['changed'] is True
        assert open(dest).read() == 'Hello World!'

    def test_idempotent_same_output(self, tmp_path):
        action, dest = _make_template_action(tmp_path, 'static content')
        result1 = action.run(task_vars={})
        assert result1['changed'] is True
        action2, _ = _make_template_action(tmp_path, 'static content')
        result2 = action2.run(task_vars={})
        assert result2['changed'] is False

    def test_force_false_skips_existing(self, tmp_path):
        action, dest = _make_template_action(tmp_path, 'new content', extra_args={'force': False})
        # Pre-create the destination
        open(dest, 'w').write('existing')
        result = action.run(task_vars={})
        assert result['changed'] is False
        assert open(dest).read() == 'existing'

    def test_sets_mode(self, tmp_path):
        action, dest = _make_template_action(tmp_path, 'data', extra_args={'mode': '0600'})
        result = action.run(task_vars={})
        assert not result.get('failed')
        import stat
        mode = stat.S_IMODE(os.stat(dest).st_mode)
        assert mode == 0o600

    def test_returns_checksum(self, tmp_path):
        action, dest = _make_template_action(tmp_path, 'hello')
        result = action.run(task_vars={})
        expected = hashlib.sha1(b'hello', usedforsecurity=False).hexdigest()
        assert result['checksum'] == expected

    def test_undefined_variable_returns_failed(self, tmp_path):
        action, dest = _make_template_action(tmp_path, '{{ undefined_var }}')
        result = action.run(task_vars={})
        assert result.get('failed') is True

    def test_missing_src_returns_failed(self, tmp_path):
        action = make_action('template', {'dest': str(tmp_path / 'out.txt')})
        result = action.run(task_vars={})
        assert result.get('failed') is True

    def test_fast_path_sets_marker(self, tmp_path):
        action, dest = _make_template_action(tmp_path, 'Hello {{ name }}!')
        result = action.run(task_vars={'name': 'World'})
        assert result[FAST_PLUGIN_MARKER] is True

    def test_fast_path_failure_is_unmarked(self, tmp_path):
        action = make_action('template', {'dest': str(tmp_path / 'out.txt')})  # no src
        result = action.run(task_vars={})
        assert result.get('failed') is True
        assert FAST_PLUGIN_MARKER not in result

    def test_jinja2_import_macros(self, tmp_path):
        (tmp_path / 'macros.j2').write_text(
            "{% macro greet(name) %}Hello, {{ name }}!{% endmacro %}"
        )
        action, dest = _make_template_action(
            tmp_path,
            "{% import 'macros.j2' as m %}{{ m.greet(who) }}",
        )
        result = action.run(task_vars={'who': 'Ansible'})
        assert not result.get('failed'), result
        assert open(dest).read() == 'Hello, Ansible!'

    def test_jinja2_from_import(self, tmp_path):
        (tmp_path / 'macros.j2').write_text(
            "{% macro upper(s) %}{{ s | upper }}{% endmacro %}"
        )
        action, dest = _make_template_action(
            tmp_path,
            "{% from 'macros.j2' import upper %}{{ upper(word) }}",
        )
        result = action.run(task_vars={'word': 'ansible'})
        assert not result.get('failed'), result
        assert open(dest).read() == 'ANSIBLE'


class TestTemplateFallback:
    def test_delegates_for_non_local(self, tmp_path):
        action, dest = _make_template_action(tmp_path, 'x', local=False)
        with mock_builtin_run('template', {'changed': True}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_for_unsupported_arg(self, tmp_path):
        action, dest = _make_template_action(tmp_path, 'x', extra_args={'validate': '/bin/true %s'})
        with mock_builtin_run('template', {'changed': True}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_delegates_for_become(self, tmp_path):
        action, dest = _make_template_action(tmp_path, 'x', become=True)
        with mock_builtin_run('template', {'changed': True}) as mock:
            action.run(task_vars={})
        mock.assert_called_once()

    def test_fallback_result_is_unmarked(self, tmp_path):
        action, dest = _make_template_action(tmp_path, 'x', local=False)
        with mock_builtin_run('template', {'changed': True}):
            result = action.run(task_vars={})
        assert FAST_PLUGIN_MARKER not in result
