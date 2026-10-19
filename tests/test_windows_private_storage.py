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


def _powershell(script, **variables):
    script = "$ErrorActionPreference = 'Stop'; Set-StrictMode -Version Latest;\n" + script
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env={
            **{key: value for key, value in os.environ.items() if key.lower() != "psmodulepath"},
            **variables,
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


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
    return json.loads(_powershell(script, STRATHMARK_ACL_TEST_PATH=str(path)))


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

    def inspect(handle, directory, descriptor, **kwargs):
        path = root if directory else target
        _assert_private(path)
        seen.append(path)
        original(handle, directory, descriptor, **kwargs)

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
    private_directory(root)
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
    path = private_directory(tmp_path / "private") / "authority"
    with os.fdopen(open_private_exclusive(path), "wb") as stream:
        stream.write(b"first")
    with pytest.raises(FileExistsError):
        open_private_exclusive(path)
    assert path.read_bytes() == b"first"


def test_file_path_does_not_tighten_a_shared_parent(tmp_path):
    root = tmp_path / "shared-project"
    root.mkdir()
    script = """
$path = $env:STRATHMARK_ACL_TEST_PATH
$acl = Get-Acl -LiteralPath $path
$sid = [System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid,'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $path -AclObject $acl
"""
    _powershell(script, STRATHMARK_ACL_TEST_PATH=str(root))
    before = _acl(root)
    with pytest.raises(PermissionError, match="explicitly provisioned"):
        private_file(root / "authority.sqlite3")
    assert _acl(root) == before
    assert not (root / "authority.sqlite3").exists()


@pytest.mark.parametrize("rights", ["Modify", "DeleteSubdirectoriesAndFiles", "Write"])
def test_writable_ancestor_is_rejected_before_creating_authority(tmp_path, rights):
    root = private_directory(tmp_path / "shared")
    script = """
$path = $env:STRATHMARK_ACL_TEST_PATH
$acl = Get-Acl -LiteralPath $path
$sid = [System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid,$env:STRATHMARK_TEST_RIGHTS,'Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $path -AclObject $acl
"""
    _powershell(script, STRATHMARK_ACL_TEST_PATH=str(root), STRATHMARK_TEST_RIGHTS=rights)
    before = _acl(root)
    with pytest.raises(PermissionError, match="replaceable") as failure:
        private_file(root / "authority" / "store.sqlite3")
    assert str(root) in str(failure.value)
    assert "SID=S-1-1-0" in str(failure.value)
    assert "mask=0x" in str(failure.value)
    assert "blocked=0x" in str(failure.value)
    assert "flags=0x" in str(failure.value)
    assert not (root / "authority").exists()
    assert _acl(root) == before


def test_junction_ancestor_is_rejected(tmp_path):
    target = private_directory(tmp_path / "target")
    junction = tmp_path / "junction"
    script = "New-Item -ItemType Junction -Path $env:STRATHMARK_TEST_LINK -Target $env:STRATHMARK_TEST_TARGET | Out-Null"
    _powershell(script, STRATHMARK_TEST_LINK=str(junction), STRATHMARK_TEST_TARGET=str(target))
    with pytest.raises(ValueError, match="junctions|reparse"):
        private_file(junction / "authority.sqlite3")
    assert not (target / "authority.sqlite3").exists()


@pytest.mark.parametrize("scenario", ["trusted_owner", "unrelated_grant", "untrusted_owner"])
def test_owner_rights_is_bound_to_verified_ancestor_owner(tmp_path, monkeypatch, scenario):
    root = private_directory(tmp_path / "owner-rights")
    script = """
$path = $env:STRATHMARK_ACL_TEST_PATH
$acl = Get-Acl -LiteralPath $path
$sid = [System.Security.Principal.SecurityIdentifier]::new('S-1-3-4')
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
$acl.AddAccessRule($rule)
if ($env:STRATHMARK_TEST_SCENARIO -eq 'unrelated_grant') {
  $world = [System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
  $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($world,'DeleteSubdirectoriesAndFiles','Allow'))
}
Set-Acl -LiteralPath $path -AclObject $acl
"""
    _powershell(script, STRATHMARK_ACL_TEST_PATH=str(root), STRATHMARK_TEST_SCENARIO=scenario)
    before = _acl(root)
    assert any(rule["sid"] == "S-1-3-4" for rule in before["rules"])
    target = root / "private" / "authority.sqlite3"
    if scenario == "untrusted_owner":
        # Exercise the real native owner/ACL read with ownership disallowed;
        # OWNER_RIGHTS must never bypass that earlier trust decision.
        monkeypatch.setattr(_windows(), "_trusted_sid", lambda _sid: False)
        with pytest.raises(PermissionError, match="belongs to another account"):
            private_file(target)
    elif scenario == "unrelated_grant":
        with pytest.raises(PermissionError, match="SID=S-1-1-0"):
            private_file(target)
    else:
        private_file(target)
        _assert_private(target)
    assert target.exists() is (scenario == "trusted_owner")
    assert _acl(root) == before


def test_importer_provisions_fresh_temporary_root_before_reading_snapshot(tmp_path, monkeypatch):
    from strathmark.v3.infrastructure import v2_import

    source = tmp_path / "source.sqlite3"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE synthetic(value TEXT)")
        connection.execute("INSERT INTO synthetic VALUES ('preserved source')")
        connection.commit()
    before = source.read_bytes()
    inspected = []

    class SnapshotObserved(Exception):
        pass

    def inspect(snapshot, **_kwargs):
        _assert_private(snapshot.parent)
        _assert_private(snapshot)
        assert snapshot.read_bytes() == before
        inspected.append(snapshot.parent)
        raise SnapshotObserved

    monkeypatch.setattr(v2_import, "_read_source_snapshot", inspect)
    destination = tmp_path / "untouched-destination.sqlite3"
    with pytest.raises(SnapshotObserved):
        v2_import.import_v2_snapshot(source, destination, cutoff="2026-10-01T00:00:00.000Z")
    assert len(inspected) == 1 and not inspected[0].exists()
    assert source.read_bytes() == before
    assert not destination.exists()
