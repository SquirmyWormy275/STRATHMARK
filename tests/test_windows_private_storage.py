"""Native Windows ACL checks, collected by every supported Windows CI lane."""

import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from strathmark.private_storage import (
    _windows,
    open_private_exclusive,
    private_directory,
    private_file,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="real Windows ACL enforcement")


def _acl(path):
    # Independent .NET inspection, not the implementation's ctypes ACL parser.
    script = """
$acl = Get-Acl -LiteralPath $env:STRATHMARK_ACL_TEST_PATH
$rules = @($acl.Access | ForEach-Object {
  @{ sid = $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value;
     type = $_.AccessControlType.ToString(); rights = [int]$_.FileSystemRights }
})
@{ protected = $acl.AreAccessRulesProtected; rules = $rules;
   user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
} | ConvertTo-Json -Depth 4 -Compress
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "STRATHMARK_ACL_TEST_PATH": str(path)},
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _assert_private(path, *, protected=True):
    acl = _acl(path)
    if protected:
        assert acl["protected"]
    assert acl["rules"] == [{"sid": acl["user"], "type": "Allow", "rights": 0x1F01FF}]


def test_new_acl_is_private_before_post_creation_tightening(tmp_path, monkeypatch):
    backend = _windows()
    original = backend._secure_handle
    root = tmp_path / "new-private"
    target = root / "secret.bin"
    seen = []

    def inspect(handle, directory, descriptor):
        path = root if directory else target
        _assert_private(path)
        seen.append(path)
        original(handle, directory, descriptor)

    monkeypatch.setattr(backend, "_secure_handle", inspect)
    private_directory(root)
    with os.fdopen(open_private_exclusive(target), "wb") as stream:
        stream.write(b"private from first byte")
    assert root in seen and target in seen


def test_existing_acl_tightening_preserves_content_and_parent_home(tmp_path):
    home_before = _acl(Path.home())
    root = tmp_path / "existing"
    root.mkdir()
    target = root / "secret.bin"
    target.write_bytes(b"retained authority")
    private_file(target)
    _assert_private(root)
    _assert_private(target)
    assert target.read_bytes() == b"retained authority"
    assert _acl(Path.home()) == home_before
    with pytest.raises(PermissionError, match="dedicated"):
        private_directory(Path.home())


def test_sqlite_sidecars_inherit_only_the_current_user(tmp_path):
    path = private_file(tmp_path / "database-root" / "authority.sqlite3")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE sample(value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('private')")
        connection.commit()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            assert sidecar.exists()
            _assert_private(sidecar, protected=False)
        private_file(path)
        for suffix in ("-wal", "-shm"):
            _assert_private(Path(f"{path}{suffix}"))


def test_hardlinks_and_alternate_streams_are_rejected(tmp_path):
    root = private_directory(tmp_path / "private")
    target = private_file(root / "authority")
    alias = root / "alias"
    os.link(target, alias)
    with pytest.raises(PermissionError, match="hard link"):
        private_file(alias)
    with pytest.raises(ValueError, match="alternate streams"):
        private_file(root / "authority:hidden")


def test_acl_write_failure_stops_without_changing_content(tmp_path, monkeypatch):
    root = private_directory(tmp_path / "private")
    path = root / "inherited.bin"
    path.write_bytes(b"unchanged")
    monkeypatch.setattr(_windows().security, "SetSecurityInfo", lambda *args: 5)
    with pytest.raises(PermissionError, match="Win32 5"):
        private_file(path)
    assert path.read_bytes() == b"unchanged"


def test_exclusive_creation_does_not_overwrite_existing_file(tmp_path):
    path = tmp_path / "authority"
    with os.fdopen(open_private_exclusive(path), "wb") as stream:
        stream.write(b"first")
    with pytest.raises(FileExistsError):
        open_private_exclusive(path)
    assert path.read_bytes() == b"first"
