from __future__ import annotations

import os
import stat
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/plugins/action_plugins')
))
import _action_utils as utils


# ---------------------------------------------------------------------------
# _is_local
# ---------------------------------------------------------------------------

class TestIsLocal:
    def test_transport_local(self):
        conn = MagicMock()
        conn.transport = 'local'
        assert utils._is_local(conn) is True

    def test_load_name_local(self):
        conn = MagicMock()
        conn.transport = None
        conn._load_name = 'local'
        assert utils._is_local(conn) is True

    def test_load_name_dotlocal(self):
        conn = MagicMock()
        conn.transport = None
        conn._load_name = 'community.general.local'
        assert utils._is_local(conn) is True

    def test_module_path_contains_connection_local(self):
        conn = MagicMock()
        conn.transport = 'not-local'
        conn._load_name = 'notlocal'
        type(conn).__module__ = 'ansible.plugins.connection.local'
        assert utils._is_local(conn) is True

    def test_ssh_is_not_local(self):
        conn = MagicMock()
        conn.transport = 'ssh'
        conn._load_name = 'ssh'
        assert utils._is_local(conn) is False

    def test_winrm_is_not_local(self):
        conn = MagicMock()
        conn.transport = 'winrm'
        conn._load_name = 'winrm'
        assert utils._is_local(conn) is False

    def test_kill_switch_forces_not_local(self, monkeypatch):
        conn = MagicMock()
        conn.transport = 'local'  # genuinely local...
        monkeypatch.setenv('AFLP_DISABLE', '1')
        assert utils._is_local(conn) is False  # ...but the kill-switch overrides

    def test_kill_switch_unset_is_local(self, monkeypatch):
        conn = MagicMock()
        conn.transport = 'local'
        monkeypatch.delenv('AFLP_DISABLE', raising=False)
        assert utils._is_local(conn) is True


# ---------------------------------------------------------------------------
# _fast_disabled (AFLP_DISABLE kill-switch)
# ---------------------------------------------------------------------------

class TestFastDisabled:
    @pytest.mark.parametrize('value', ['1', 'true', 'TRUE', 'Yes', 'on', '  on  '])
    def test_truthy_values_disable(self, monkeypatch, value):
        monkeypatch.setenv('AFLP_DISABLE', value)
        assert utils._fast_disabled() is True

    @pytest.mark.parametrize('value', ['0', 'false', 'no', 'off', '', 'maybe'])
    def test_other_values_do_not_disable(self, monkeypatch, value):
        monkeypatch.setenv('AFLP_DISABLE', value)
        assert utils._fast_disabled() is False

    def test_unset_does_not_disable(self, monkeypatch):
        monkeypatch.delenv('AFLP_DISABLE', raising=False)
        assert utils._fast_disabled() is False


# ---------------------------------------------------------------------------
# strict_guard (AFLP_STRICT)
# ---------------------------------------------------------------------------

class TestStrictGuard:
    def _conn(self, transport='local'):
        c = MagicMock()
        c.transport = transport
        c._load_name = transport
        return c

    def _pc(self, become=False, check_mode=False):
        pc = MagicMock()
        pc.become = become
        pc.check_mode = check_mode
        return pc

    def test_noop_when_unset(self, monkeypatch):
        monkeypatch.delenv('AFLP_STRICT', raising=False)
        utils.strict_guard(self._conn('ssh'), self._pc(become=True))  # must not raise

    def test_raises_for_non_local(self, monkeypatch):
        from ansible.errors import AnsibleActionFail
        monkeypatch.setenv('AFLP_STRICT', '1')
        with pytest.raises(AnsibleActionFail, match='non-local connection'):
            utils.strict_guard(self._conn('ssh'), self._pc())

    def test_raises_for_become_with_reason(self, monkeypatch):
        from ansible.errors import AnsibleActionFail
        monkeypatch.setenv('AFLP_STRICT', '1')
        with pytest.raises(AnsibleActionFail, match='become'):
            utils.strict_guard(self._conn('local'), self._pc(become=True))

    def test_reason_defaults_to_unsupported_arguments(self, monkeypatch):
        from ansible.errors import AnsibleActionFail
        monkeypatch.setenv('AFLP_STRICT', '1')
        with pytest.raises(AnsibleActionFail, match='unsupported arguments'):
            utils.strict_guard(self._conn('local'), self._pc())

    def test_suppressed_by_disable(self, monkeypatch):
        # AFLP_DISABLE is an intentional fallback; strict must not fire.
        monkeypatch.setenv('AFLP_STRICT', '1')
        monkeypatch.setenv('AFLP_DISABLE', '1')
        utils.strict_guard(self._conn('ssh'), self._pc(become=True))  # must not raise

    def test_strict_enabled_parsing(self, monkeypatch):
        monkeypatch.setenv('AFLP_STRICT', 'YES')
        assert utils._strict_enabled() is True
        monkeypatch.setenv('AFLP_STRICT', 'off')
        assert utils._strict_enabled() is False


# ---------------------------------------------------------------------------
# _parse_mode
# ---------------------------------------------------------------------------

class TestParseMode:
    def test_int_passthrough(self):
        assert utils._parse_mode(0o644) == 0o644

    def test_octal_string_with_prefix(self):
        assert utils._parse_mode('0644') == 0o644

    def test_octal_string_o_prefix(self):
        assert utils._parse_mode('0o644') == 0o644

    def test_bare_octal_string(self):
        assert utils._parse_mode('644') == 0o644

    def test_executable_mode(self):
        assert utils._parse_mode('0755') == 0o755

    def test_preserve_raises(self):
        with pytest.raises(ValueError, match='preserve'):
            utils._parse_mode('preserve')

    def test_unsupported_type_raises(self):
        with pytest.raises(ValueError):
            utils._parse_mode(['0644'])


# ---------------------------------------------------------------------------
# mark_fast_result
# ---------------------------------------------------------------------------

class TestMarkFastResult:
    def test_marks_successful_dict(self):
        result = utils.mark_fast_result({'changed': True})
        assert result[utils.FAST_PLUGIN_MARKER] is True

    def test_marks_changed_false_dict(self):
        # Idempotent (changed=False) success results are still marked.
        result = utils.mark_fast_result({'changed': False})
        assert result[utils.FAST_PLUGIN_MARKER] is True

    def test_does_not_mark_failed_dict(self):
        result = utils.mark_fast_result({'failed': True, 'msg': 'boom'})
        assert utils.FAST_PLUGIN_MARKER not in result

    def test_returns_same_object(self):
        d = {'changed': True}
        assert utils.mark_fast_result(d) is d

    def test_does_not_clobber_existing_marker(self):
        result = utils.mark_fast_result({'changed': True, utils.FAST_PLUGIN_MARKER: 'preset'})
        assert result[utils.FAST_PLUGIN_MARKER] == 'preset'

    def test_non_dict_passes_through(self):
        assert utils.mark_fast_result(None) is None
        assert utils.mark_fast_result('x') == 'x'

    def test_marker_key_name(self):
        assert utils.FAST_PLUGIN_MARKER == '__produced_by_fast_plugin'


# ---------------------------------------------------------------------------
# atomic_write
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_creates_file_with_content(self, tmp_path):
        dest = str(tmp_path / 'output.txt')
        err = utils.atomic_write(dest, b'hello world')
        assert err is None
        with open(dest, 'rb') as f:
            assert f.read() == b'hello world'

    def test_overwrites_existing_file(self, tmp_path):
        dest = tmp_path / 'output.txt'
        dest.write_bytes(b'old content')
        err = utils.atomic_write(str(dest), b'new content')
        assert err is None
        assert dest.read_bytes() == b'new content'

    def test_sets_explicit_mode(self, tmp_path):
        dest = str(tmp_path / 'output.txt')
        err = utils.atomic_write(dest, b'data', mode='0600')
        assert err is None
        mode = stat.S_IMODE(os.stat(dest).st_mode)
        assert mode == 0o600

    def test_new_file_uses_umask_default(self, tmp_path):
        # A fresh file with no explicit mode must get the umask default
        # (0666 & ~umask), like stock ansible — not mkstemp's restrictive 0600.
        dest = str(tmp_path / 'fresh.txt')
        err = utils.atomic_write(dest, b'data')
        assert err is None
        cur_umask = os.umask(0)
        os.umask(cur_umask)
        assert stat.S_IMODE(os.stat(dest).st_mode) == (0o666 & ~cur_umask)

    def test_preserves_existing_mode(self, tmp_path):
        dest = tmp_path / 'output.txt'
        dest.write_bytes(b'old')
        os.chmod(str(dest), 0o640)
        err = utils.atomic_write(str(dest), b'new')
        assert err is None
        mode = stat.S_IMODE(os.stat(str(dest)).st_mode)
        assert mode == 0o640

    def test_missing_parent_directory_returns_error(self, tmp_path):
        dest = str(tmp_path / 'nonexistent' / 'output.txt')
        err = utils.atomic_write(dest, b'data')
        assert err is not None
        assert 'does not exist' in err
