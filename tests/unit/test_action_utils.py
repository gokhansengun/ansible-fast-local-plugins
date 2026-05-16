from __future__ import annotations

import os
import stat
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../ansible/action-plugins')
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
