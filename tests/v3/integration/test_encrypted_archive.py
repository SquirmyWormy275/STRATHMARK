import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

from strathmark.v3.infrastructure.encrypted_archive import encrypt_file, tar_digests, verify_file


@pytest.fixture
def gpg_archive(tmp_path):
    binary = shutil.which("gpg")
    if binary is None or not Path("/dev/shm").is_dir():
        pytest.skip("native GPG and independent temporary filesystem are required")
    home = tmp_path / "gpg"
    home.mkdir(mode=0o700)
    base = [
        binary,
        "--homedir",
        str(home),
        "--batch",
        "--pinentry-mode",
        "loopback",
        "--passphrase",
        "",
    ]
    subprocess.run(
        base + ["--quick-generate-key", "STRATH synthetic test", "ed25519", "cert", "0"],
        check=True,
        capture_output=True,
    )
    listed = subprocess.check_output(
        base + ["--with-colons", "--list-keys"], stderr=subprocess.DEVNULL
    ).decode()
    fingerprint = next(
        line.split(":")[9] for line in listed.splitlines() if line.startswith("fpr:")
    )
    subprocess.run(
        base + ["--quick-add-key", fingerprint, "cv25519", "encr", "0"],
        check=True,
        capture_output=True,
    )
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": "strath-recovery-encryption-policy-v1",
                "gpg_binary": binary,
                "gpg_binary_sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
                "gpg_home": str(home),
                "recipient_fingerprint": fingerprint,
            }
        )
    )
    source = tmp_path / "synthetic.tar.gz"
    data = tmp_path / "synthetic-key.txt"
    data.write_bytes(b"synthetic signing-key bytes; no production data")
    with tarfile.open(source, "w:gz") as archive:
        archive.add(data, arcname="installation/synthetic-key.txt")
    with tempfile.TemporaryDirectory(prefix="strath-gpg-test-", dir="/dev/shm") as directory:
        if Path(directory).stat().st_dev == home.stat().st_dev:
            pytest.skip("test requires independent temporary filesystems")
        yield source, Path(directory) / "backup.tar.gz.gpg", policy
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"], capture_output=True)


def test_native_encrypt_decrypt_and_complete_member_readback(gpg_archive):
    source, destination, policy = gpg_archive
    original = source.read_bytes()
    result = encrypt_file(source, destination, policy)
    assert result["authenticated_decryption_readback"] is True
    assert result["plaintext_sha256"] == hashlib.sha256(original).hexdigest()
    assert source.read_bytes() == original
    assert verify_file(destination, policy) == result
    assert tar_digests(destination, policy) == {
        "installation/synthetic-key.txt": hashlib.sha256(
            b"synthetic signing-key bytes; no production data"
        ).hexdigest()
    }
    assert destination.stat().st_mode & 0o077 == 0


def test_ciphertext_tampering_and_repeat_output_are_refused(gpg_archive):
    source, destination, policy = gpg_archive
    encrypt_file(source, destination, policy)
    original = destination.read_bytes()
    with pytest.raises(FileExistsError):
        encrypt_file(source, destination, policy)
    assert destination.read_bytes() == original
    destination.write_bytes(original[:-10] + b"corruption")
    with pytest.raises(ValueError, match="ciphertext checksum"):
        verify_file(destination, policy)


def test_private_key_and_archive_cannot_share_filesystem(gpg_archive):
    source, _, policy = gpg_archive
    with pytest.raises(ValueError, match="different filesystems"):
        encrypt_file(source, source.parent / "same-filesystem.gpg", policy)


def test_native_authority_backup_is_encrypted_and_restores_exact_retry(gpg_archive, tmp_path):
    from strathmark.v3.linux_lifecycle_store import LinuxLifecycleStore

    _, destination, policy = gpg_archive
    store = LinuxLifecycleStore.initialize(tmp_path / "authority")
    command = dict(
        command_id="command:synthetic",
        operation="open",
        scope_id="tournament:synthetic",
        request={"value": 1},
        occurred_at="2026-10-03T00:00:00.000Z",
    )

    def transition(state):
        state["roots"]["tournament:synthetic"] = {"value": 1}
        return {"accepted": 1}

    assert store.execute(**command, transition=transition) == {"accepted": 1}
    archive = store.archive_backup(destination.parent, encryption_policy=policy)
    assert archive.name.endswith(".tar.gz.gpg")
    assert not list(destination.parent.glob("*.tar.gz"))
    assert store.archive_backup(destination.parent, encryption_policy=policy) == archive
    verify_file(archive, policy)
    # Restore to private staging through the real GPG process, never on the archive filesystem.
    configured = json.loads(policy.read_text())
    restored = tmp_path / "restored"
    restored.mkdir(mode=0o700)
    decrypted = subprocess.run(
        [
            configured["gpg_binary"],
            "--homedir",
            configured["gpg_home"],
            "--batch",
            "--decrypt",
            str(archive),
        ],
        check=True,
        capture_output=True,
    ).stdout
    import io

    with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:gz") as incoming:
        incoming.extractall(restored, filter="data")
    # Python's safe data filter deliberately does not restore directory modes.
    (restored / "installation").chmod(0o700)
    recovered = LinuxLifecycleStore(restored / "installation")
    assert recovered.signer.identity == store.signer.identity
    assert recovered.state() == store.state()

    def fail_if_reexecuted(_state):
        pytest.fail("a restored exact retry must not rerun the transition")

    assert recovered.execute(**command, transition=fail_if_reexecuted) == {"accepted": 1}
