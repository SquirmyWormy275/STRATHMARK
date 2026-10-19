import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

import pytest

from strathmark.v3.infrastructure import encrypted_archive as recovery_security
from strathmark.v3.infrastructure.encrypted_archive import (
    encrypt_file,
    inspect_file,
    load_policy,
    plaintext_sha,
    tar_digests,
    verify_file,
)


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
    checked = verify_file(
        destination,
        policy,
        expected_plaintext_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    assert checked["verification_scope"] == "trusted_plaintext_identity"
    assert checked["untrusted_receipt"] == result
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
    with pytest.raises(ValueError, match="ciphertext checksum|authenticated GPG"):
        verify_file(
            destination,
            policy,
            expected_plaintext_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        )


def test_private_key_and_archive_cannot_share_filesystem(gpg_archive):
    source, _, policy = gpg_archive
    with pytest.raises(ValueError, match="different filesystems"):
        encrypt_file(source, source.parent / "same-filesystem.gpg", policy)


def test_native_gpg_uses_validated_paths_after_aliases_change(gpg_archive, tmp_path):
    source, destination, policy = gpg_archive
    value = json.loads(policy.read_bytes())
    binary_alias, home_alias = tmp_path / "gpg-alias", tmp_path / "home-alias"
    binary_alias.symlink_to(value["gpg_binary"])
    home_alias.symlink_to(value["gpg_home"], target_is_directory=True)
    value.update(gpg_binary=str(binary_alias), gpg_home=str(home_alias))
    policy.write_text(json.dumps(value))
    encrypt_file(source, destination, policy)
    configured = load_policy(policy)
    binary_alias.unlink()
    home_alias.unlink()
    binary_alias.symlink_to(tmp_path / "missing-program")
    home_alias.symlink_to(tmp_path / "missing-key-home", target_is_directory=True)
    assert plaintext_sha(destination, configured) == hashlib.sha256(source.read_bytes()).hexdigest()


def test_bulk_streaming_and_truncated_authenticated_tail(gpg_archive):
    source, destination, policy = gpg_archive
    # More than either OS pipe's capacity; small fixtures cannot expose deadlock.
    source.write_bytes(b"synthetic archive input\x00" * 200000)
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    encrypt_file(source, destination, policy)
    configured = load_policy(policy)
    assert plaintext_sha(destination, configured) == expected
    destination.write_bytes(destination.read_bytes()[:-12])
    # Bypass the receipt checksum deliberately to exercise GPG's own end check.
    with pytest.raises(ValueError, match="authenticated GPG"):
        plaintext_sha(destination, configured)
    assert not [t for t in threading.enumerate() if t.name == "strath-gpg-input"]


def test_early_gpg_exit_preserves_source_and_joins_feeder(gpg_archive):
    source, destination, policy = gpg_archive
    source.write_bytes(b"synthetic oversized pipe input" * 200000)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    configured = json.loads(policy.read_text())
    configured["recipient_fingerprint"] = "A" * 40
    policy.write_text(json.dumps(configured))
    with pytest.raises(ValueError, match="authenticated GPG"):
        encrypt_file(source, destination, policy)
    assert not destination.exists()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    assert not [t for t in threading.enumerate() if t.name == "strath-gpg-input"]


def test_tar_failure_closes_gpg_and_feeder(gpg_archive, tmp_path):
    source, destination, policy = gpg_archive
    linked = tmp_path / "unsafe-link"
    linked.symlink_to("../outside")
    with tarfile.open(source, "w:gz") as archive:
        archive.add(linked, arcname="unsafe-link")
    encrypt_file(source, destination, policy)
    with pytest.raises(ValueError, match="unsafe members"):
        tar_digests(destination, policy)
    assert not [t for t in threading.enumerate() if t.name == "strath-gpg-input"]


@pytest.mark.parametrize("archive_mode", [0o700, 0o777])
def test_native_authority_backup_is_encrypted_and_restores_exact_retry(
    gpg_archive, tmp_path, archive_mode
):
    from strathmark.v3.linux_lifecycle_store import LinuxLifecycleStore

    _, destination, policy = gpg_archive
    # Removable ciphertext storage need not provide owner-only Unix modes.
    destination.parent.chmod(archive_mode)
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
    assert inspect_file(archive, policy)["verification_scope"] == "integrity_only"
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


def test_killed_publisher_leaves_no_final_ciphertext_and_exact_retry_recovers(gpg_archive):
    source, destination, policy = gpg_archive
    script = """import os, sys
from pathlib import Path
from strathmark.v3.infrastructure import encrypted_archive as module
original = module._sync
def die_after_receipt_commit(path):
    original(path)
    if str(path).endswith('.verified.json'):
        os._exit(99)
module._sync = die_after_receipt_commit
module.encrypt_file(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
"""
    killed = subprocess.run(
        [sys.executable, "-c", script, str(source), str(destination), str(policy)],
        capture_output=True,
    )
    assert killed.returncode == 99, killed.stderr
    assert not destination.exists()
    assert destination.with_name(destination.name + ".verified.json").exists()
    # The lock is released by the OS and the same source can be retried safely.
    result = encrypt_file(source, destination, policy)
    checked = verify_file(
        destination,
        policy,
        expected_plaintext_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    assert checked["verification_scope"] == "trusted_plaintext_identity"
    assert checked["untrusted_receipt"] == result
    assert len(list(destination.parent.glob("*.orphaned-*"))) == 1
    assert source.exists()


def test_killed_authority_backup_recovers_with_a_fresh_snapshot(gpg_archive, tmp_path):
    from strathmark.v3.linux_lifecycle_store import LinuxLifecycleStore

    _, destination, policy = gpg_archive
    store = LinuxLifecycleStore.initialize(tmp_path / "authority")
    script = """import os, sys
from pathlib import Path
from strathmark.v3.infrastructure import encrypted_archive as module
from strathmark.v3.linux_lifecycle_store import LinuxLifecycleStore
original = module._sync
def die_after_receipt_commit(path):
    original(path)
    if str(path).endswith('.verified.json'):
        os._exit(99)
module._sync = die_after_receipt_commit
LinuxLifecycleStore(Path(sys.argv[1])).archive_backup(Path(sys.argv[2]), encryption_policy=Path(sys.argv[3]))
"""
    killed = subprocess.run(
        [sys.executable, "-c", script, str(store.root), str(destination.parent), str(policy)],
        capture_output=True,
    )
    assert killed.returncode == 99, killed.stderr
    assert not list(destination.parent.glob("*.gpg"))
    recovered = store.archive_backup(destination.parent, encryption_policy=policy)
    assert inspect_file(recovered, policy)["verification_scope"] == "integrity_only"
    assert store.archive_backup(destination.parent, encryption_policy=policy) == recovered


def _public_only_archive(source, destination, policy, public_home, *, reported_size=None):
    """Model an attacker who knows only the public recipient key and receipt schema."""
    configured = load_policy(policy)
    public_home.mkdir(mode=0o700)
    public_key = subprocess.check_output(
        [
            configured["gpg_binary"],
            "--homedir",
            configured["gpg_home"],
            "--batch",
            "--export",
            configured["recipient_fingerprint"],
        ]
    )
    command = [configured["gpg_binary"], "--homedir", str(public_home), "--batch"]
    subprocess.run(command + ["--import"], input=public_key, check=True, capture_output=True)
    secret_keys = subprocess.check_output(
        command + ["--with-colons", "--list-secret-keys"], stderr=subprocess.DEVNULL
    )
    assert b"sec:" not in secret_keys
    subprocess.run(
        command
        + [
            "--trust-model",
            "always",
            "--recipient",
            configured["recipient_fingerprint"],
            "--compress-algo",
            "zlib",
            "--output",
            str(destination),
            "--encrypt",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    receipt = {
        "schema_version": recovery_security.SCHEMA,
        "recipient_fingerprint": configured["recipient_fingerprint"],
        "ciphertext_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "plaintext_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "plaintext_bytes": source.stat().st_size if reported_size is None else reported_size,
        "source_archive_name": "claimed-genuine-backup.tar.gz",
        "authenticated_decryption_readback": True,
        "original_preserved": True,
    }
    destination.with_name(destination.name + ".verified.json").write_text(json.dumps(receipt))
    subprocess.run(
        ["gpgconf", "--homedir", str(public_home), "--kill", "gpg-agent"], capture_output=True
    )
    return receipt


def test_public_key_holder_cannot_forge_independently_pinned_recovery(gpg_archive, tmp_path):
    source, destination, policy = gpg_archive
    trusted_identity = hashlib.sha256(source.read_bytes()).hexdigest()
    forged = tmp_path / "chosen-plaintext"
    forged.write_bytes(b"attacker-selected recovery contents")
    _public_only_archive(forged, destination, policy, tmp_path / "public-only")
    result = inspect_file(destination, policy)
    assert result["verification_scope"] == "integrity_only"
    assert result["origin_authenticated"] is False
    with pytest.raises(ValueError, match="independently trusted plaintext identity"):
        verify_file(destination, policy, expected_plaintext_sha256=trusted_identity)
    assert source.exists() and destination.exists()


@pytest.mark.parametrize("pin", [None, "", "a" * 63, "g" * 64, 3, "a" * 64 + "\n"])
def test_missing_or_invalid_trust_pin_fails_before_archive_or_policy_access(tmp_path, pin):
    with pytest.raises(ValueError, match="independently trusted plaintext SHA-256"):
        verify_file(
            tmp_path / "does-not-exist", tmp_path / "no-policy", expected_plaintext_sha256=pin
        )


def test_genuine_archive_wrong_identity_is_rejected(gpg_archive):
    source, destination, policy = gpg_archive
    encrypt_file(source, destination, policy)
    with pytest.raises(ValueError, match="independently trusted plaintext identity"):
        verify_file(destination, policy, expected_plaintext_sha256="a" * 64)
    assert (
        verify_file(
            destination,
            policy,
            expected_plaintext_sha256=hashlib.sha256(source.read_bytes()).hexdigest().upper(),
        )["origin_authenticated"]
        is False
    )


@pytest.mark.parametrize(
    "target,kind",
    [
        ("archive", "symlink"),
        ("archive", "fifo"),
        ("archive", "directory"),
        ("receipt", "symlink"),
        ("receipt", "fifo"),
        ("receipt", "directory"),
    ],
)
def test_nonregular_archive_and_receipt_refused_before_gpg(gpg_archive, monkeypatch, target, kind):
    source, destination, policy = gpg_archive
    encrypt_file(source, destination, policy)
    path = (
        destination
        if target == "archive"
        else destination.with_name(destination.name + ".verified.json")
    )
    path.unlink()
    if kind == "symlink":
        path.symlink_to(source)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()

    def unexpected(_policy):
        pytest.fail("GPG must not start for a nonregular input")

    monkeypatch.setattr(recovery_security, "_command", unexpected)
    with pytest.raises(ValueError, match="regular file"):
        inspect_file(destination, policy)


def test_oversized_ciphertext_refused_before_process_launch(gpg_archive, monkeypatch):
    source, destination, policy = gpg_archive
    encrypt_file(source, destination, policy)
    maximum = source.stat().st_size
    with destination.open("r+b") as stream:
        stream.truncate(recovery_security._ciphertext_allowance(maximum) + 1)
    monkeypatch.setattr(
        recovery_security, "_command", lambda _policy: pytest.fail("unexpected GPG")
    )
    with pytest.raises(ValueError, match="input exceeds byte allowance"):
        inspect_file(destination, policy, max_plaintext_bytes=maximum)


@pytest.mark.parametrize("size", [-1, True, "3", None, 2**50])
def test_receipt_cannot_enlarge_or_corrupt_independent_allowance(gpg_archive, monkeypatch, size):
    source, destination, policy = gpg_archive
    result = encrypt_file(source, destination, policy)
    result["plaintext_bytes"] = size
    destination.with_name(destination.name + ".verified.json").write_text(json.dumps(result))
    monkeypatch.setattr(
        recovery_security, "_command", lambda _policy: pytest.fail("unexpected GPG")
    )
    with pytest.raises(ValueError, match="independent byte allowance"):
        inspect_file(destination, policy)


def test_receipt_exact_size_mismatch_rejected(gpg_archive):
    source, destination, policy = gpg_archive
    result = encrypt_file(source, destination, policy)
    result["plaintext_bytes"] -= 1
    destination.with_name(destination.name + ".verified.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="checksum or size differs"):
        inspect_file(destination, policy)


def test_gpg_compression_expansion_is_bounded_and_processes_joined(gpg_archive, tmp_path):
    _, destination, policy = gpg_archive
    forged = tmp_path / "compressed-bomb"
    forged.write_bytes(b"x" * (4 * 1024 * 1024))
    _public_only_archive(forged, destination, policy, tmp_path / "public-only", reported_size=1)
    assert destination.stat().st_size < 65536
    with pytest.raises(ValueError, match="output exceeds byte allowance"):
        inspect_file(destination, policy, max_plaintext_bytes=65536)
    assert not [t for t in threading.enumerate() if t.name.startswith("strath-gpg-")]


def test_gpg_deadline_kills_blocked_output_and_joins_threads(gpg_archive, monkeypatch):
    source, _, policy = gpg_archive
    configured = load_policy(policy)
    processes = []
    original = subprocess.Popen

    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(
        recovery_security,
        "_command",
        lambda _: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    monkeypatch.setattr(recovery_security.subprocess, "Popen", launch)
    monkeypatch.setattr(recovery_security, "_duration", lambda _: 0.15)
    started = time.monotonic()
    with pytest.raises(ValueError, match="deadline expired"):
        plaintext_sha(source, configured)
    assert time.monotonic() - started < 5
    assert processes and all(process.poll() is not None for process in processes)
    assert not [t for t in threading.enumerate() if t.name.startswith("strath-gpg-")]


def test_ciphertext_digest_and_decryption_use_the_same_open_file(gpg_archive, monkeypatch):
    source, destination, policy = gpg_archive
    encrypt_file(source, destination, policy)
    original = subprocess.Popen
    old_bytes = destination.read_bytes()

    def replace_after_open(*args, **kwargs):
        replacement = destination.with_suffix(".replacement")
        replacement.write_bytes(b"replacement must never be fed to GPG")
        os.replace(replacement, destination)
        return original(*args, **kwargs)

    monkeypatch.setattr(recovery_security.subprocess, "Popen", replace_after_open)
    result = verify_file(
        destination,
        policy,
        expected_plaintext_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    assert result["untrusted_receipt"]["ciphertext_sha256"] == hashlib.sha256(old_bytes).hexdigest()
    assert destination.read_bytes() == b"replacement must never be fed to GPG"


def test_trusted_large_source_allowance_is_explicit_and_receipt_does_not_supply_it(gpg_archive):
    source, destination, policy = gpg_archive
    source.write_bytes(b"larger than the test's independent allowance" * 10000)
    encrypt_file(source, destination, policy)
    with pytest.raises(ValueError, match="independent byte allowance"):
        inspect_file(destination, policy, max_plaintext_bytes=1024)
    result = verify_file(
        destination,
        policy,
        expected_plaintext_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        max_plaintext_bytes=source.stat().st_size,
    )
    assert result["plaintext_bytes"] == source.stat().st_size


def test_cli_verify_requires_independent_pin_and_inspect_is_explicit(gpg_archive):
    source, destination, policy = gpg_archive
    encrypt_file(source, destination, policy)
    command = [
        sys.executable,
        str(Path(recovery_security.__file__)),
        "verify",
        "--policy",
        str(policy),
        "--source",
        str(destination),
    ]
    missing = subprocess.run(command, capture_output=True, text=True)
    assert missing.returncode == 2 and "independent record" in missing.stderr
    genuine = subprocess.run(
        command + ["--expected-plaintext-sha256", hashlib.sha256(source.read_bytes()).hexdigest()],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(genuine.stdout)["verification_scope"] == "trusted_plaintext_identity"
    command[2] = "inspect"
    inspected = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(inspected.stdout)["verification_scope"] == "integrity_only"


def test_native_retry_rejects_publicly_forged_archive_even_with_matching_sidecar(
    gpg_archive, tmp_path
):
    from strathmark.v3.linux_lifecycle_store import LinuxLifecycleError, LinuxLifecycleStore

    _, destination, policy = gpg_archive
    store = LinuxLifecycleStore.initialize(tmp_path / "authority-forgery")
    archive = store.archive_backup(destination.parent, encryption_policy=policy)
    archive.unlink()
    archive.with_name(archive.name + ".verified.json").unlink()
    forged = tmp_path / "different-snapshot.tar.gz"
    unrelated = tmp_path / "different-head"
    unrelated.write_bytes(b"not the independently retained authority snapshot")
    with tarfile.open(forged, "w:gz") as container:
        container.add(unrelated, arcname="installation/head.json")
    _public_only_archive(forged, archive, policy, tmp_path / "native-public-only")
    assert inspect_file(archive, policy)["verification_scope"] == "integrity_only"
    with pytest.raises(LinuxLifecycleError, match="readback differs"):
        store.archive_backup(destination.parent, encryption_policy=policy)
    assert store.state() == {"roots": {}}


def test_input_growth_after_open_cannot_exceed_feeder_budget(tmp_path, monkeypatch):
    source = tmp_path / "growing-input"
    source.write_bytes(b"original")
    original = subprocess.Popen

    def launch(*args, **kwargs):
        with source.open("ab") as stream:
            stream.write(b"x" * 100)
        return original(*args, **kwargs)

    monkeypatch.setattr(recovery_security.subprocess, "Popen", launch)
    monkeypatch.setattr(
        recovery_security,
        "_command",
        lambda _: [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ],
    )
    with pytest.raises(ValueError, match="input exceeds byte allowance"):
        with recovery_security._gpg_stream(
            source, {}, [], input_limit=16, output_limit=32, seconds=5
        ) as stream:
            while stream.read(16):
                pass
    assert not [t for t in threading.enumerate() if t.name.startswith("strath-gpg-")]
