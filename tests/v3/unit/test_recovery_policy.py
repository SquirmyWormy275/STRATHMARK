"""Local encryption trust inputs must not be replaceable by another account."""

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from strathmark.v3.infrastructure import encrypted_archive as recovery

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux recovery policy permissions")


@pytest.fixture
def policy_files(tmp_path):
    paths = {
        "policy": tmp_path / "config" / "policy.json",
        "binary": tmp_path / "bin" / "gpg",
        "home": tmp_path / "keys" / "gpg-home",
    }
    for path in paths.values():
        path.parent.mkdir(mode=0o755)
    paths["binary"].write_bytes(b"synthetic executable; never run")
    paths["binary"].chmod(0o755)
    paths["home"].mkdir(mode=0o700)
    value = {
        "schema_version": "strath-recovery-encryption-policy-v1",
        "gpg_binary": str(paths["binary"]),
        "gpg_binary_sha256": hashlib.sha256(paths["binary"].read_bytes()).hexdigest(),
        "gpg_home": str(paths["home"]),
        "recipient_fingerprint": "A" * 40,
    }
    paths["policy"].write_text(json.dumps(value))
    paths["policy"].chmod(0o644)
    return paths


@pytest.mark.parametrize("subject", ["policy", "binary", "home"])
@pytest.mark.parametrize("ancestor", [False, True])
def test_peer_writable_input_or_parent_is_refused_without_chmod(policy_files, subject, ancestor):
    path = policy_files[subject].parent if ancestor else policy_files[subject]
    original = policy_files["policy"].read_bytes()
    path.chmod(0o777)
    with pytest.raises((ValueError, PermissionError), match="owner-controlled|replaceable"):
        recovery.load_policy(policy_files["policy"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o777
    assert policy_files["policy"].read_bytes() == original


@pytest.mark.parametrize("subject", ["policy", "binary", "home"])
@pytest.mark.parametrize("ancestor", [False, True])
def test_unrelated_input_owner_is_refused(policy_files, subject, ancestor, monkeypatch):
    original = Path.lstat
    target = policy_files[subject].parent if ancestor else policy_files[subject]

    def foreign_owner(path, *args, **kwargs):
        status = original(path, *args, **kwargs)
        if path == target:
            fields = list(status)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return status

    monkeypatch.setattr(Path, "lstat", foreign_owner)
    with pytest.raises((ValueError, PermissionError), match="owner-controlled|unrelated owner"):
        recovery.load_policy(policy_files["policy"])


@pytest.mark.parametrize("mode", [0o755, 0o1777])
def test_safe_and_trusted_sticky_ancestors_remain_unchanged(policy_files, mode):
    parents = [path.parent for path in policy_files.values()]
    for parent in parents:
        parent.chmod(mode)
    configured = recovery.load_policy(policy_files["policy"])
    assert configured["gpg_binary"] == str(policy_files["binary"])
    assert configured["gpg_home"] == str(policy_files["home"])
    assert all(stat.S_IMODE(parent.stat().st_mode) == mode for parent in parents)


@pytest.mark.parametrize("mode", [0o600, 0o711, 0o755])
def test_private_key_home_requires_exact_mode_700(policy_files, mode):
    policy_files["home"].chmod(mode)
    try:
        with pytest.raises(ValueError, match="mode 700"):
            recovery.load_policy(policy_files["policy"])
        assert stat.S_IMODE(policy_files["home"].stat().st_mode) == mode
    finally:
        policy_files["home"].chmod(0o700)


def test_command_uses_canonical_targets_despite_relative_alias_changes(
    policy_files, tmp_path, monkeypatch
):
    binary_alias, home_alias = tmp_path / "gpg", tmp_path / "key-home"
    binary_alias.symlink_to(policy_files["binary"])
    home_alias.symlink_to(policy_files["home"], target_is_directory=True)
    value = json.loads(policy_files["policy"].read_bytes())
    value.update(gpg_binary="gpg", gpg_home="key-home")
    policy_files["policy"].write_text(json.dumps(value))
    monkeypatch.chdir(tmp_path)
    configured = recovery.load_policy(policy_files["policy"])
    binary_alias.unlink()
    home_alias.unlink()
    binary_alias.symlink_to(tmp_path / "wrong-program")
    home_alias.symlink_to(tmp_path / "wrong-key-home", target_is_directory=True)
    assert recovery._command(configured) == [
        str(policy_files["binary"]),
        "--homedir",
        str(policy_files["home"]),
        "--batch",
        "--no-tty",
    ]


def test_policy_must_be_a_regular_file_before_any_read(policy_files):
    policy_files["policy"].unlink()
    os.mkfifo(policy_files["policy"])
    with pytest.raises(ValueError, match="wrong file type"):
        recovery.load_policy(policy_files["policy"])


@pytest.mark.parametrize("parent_alias", [False, True])
def test_policy_trust_root_cannot_be_redirected_through_alias(policy_files, tmp_path, parent_alias):
    original = policy_files["policy"].read_bytes()
    alias = tmp_path / "policy-alias"
    if parent_alias:
        alias.symlink_to(policy_files["policy"].parent, target_is_directory=True)
        alias = alias / "policy.json"
    else:
        alias.symlink_to(policy_files["policy"])
    with pytest.raises(ValueError, match="links|wrong file type|owner-controlled"):
        recovery.load_policy(alias)
    assert policy_files["policy"].read_bytes() == original
