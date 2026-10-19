"""Standard-library GPG archive encryption with full decryption readback.

STRATHEX ships a release-matched standalone recovery_security.py companion.
Private keys stay in an explicitly configured workstation GPG home. No source
archive is deleted, and no plaintext is written to the archive filesystem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

SCHEMA = "strath-encrypted-recovery-v1"
CHUNK_BYTES = 1024 * 1024


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _authority_input(path, *, directory=False, canonicalize=True):
    """Check canonical local inputs without changing their permissions."""
    path = Path(path).expanduser().absolute()
    if canonicalize:
        path = path.resolve(strict=True)
    if os.name == "posix":
        from strathmark.private_storage import reject_links

        reject_links(path)
    status = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(status.st_mode):
        raise ValueError(f"GPG authority input has the wrong file type: {path}")
    if os.name == "posix" and (status.st_uid not in (0, os.geteuid()) or status.st_mode & 0o022):
        raise ValueError(f"GPG authority input is not owner-controlled: {path}")
    return path


def load_policy(path):
    # The policy is the trust root, so its original name must not be an alias
    # that another account can redirect to a different policy before validation.
    path = _authority_input(path, canonicalize=False)
    if path.stat().st_size > 16384:
        raise ValueError("recovery encryption policy exceeds 16 KB")
    value = json.loads(path.read_bytes())
    if (
        set(value)
        != {
            "schema_version",
            "gpg_binary",
            "gpg_binary_sha256",
            "gpg_home",
            "recipient_fingerprint",
        }
        or value["schema_version"] != "strath-recovery-encryption-policy-v1"
    ):
        raise ValueError("unsupported closed recovery encryption policy")
    if not re.fullmatch(r"[A-F0-9]{40,64}", value["recipient_fingerprint"]):
        raise ValueError("recovery recipient must be a full fingerprint")
    binary, home = (
        _authority_input(value["gpg_binary"]),
        _authority_input(value["gpg_home"], directory=True),
    )
    if sha(binary) != value["gpg_binary_sha256"] or not home.is_dir():
        raise ValueError("GPG binary or private recovery home changed")
    if os.name == "posix" and (
        home.stat().st_uid != os.geteuid() or stat.S_IMODE(home.stat().st_mode) != 0o700
    ):
        raise ValueError("GPG private home must be owned by this user with mode 700")
    # Execute the same canonical file whose digest was checked, never a relative
    # PATH lookup or a symlink alias that can later identify another binary/home.
    return {**value, "gpg_binary": str(binary), "gpg_home": str(home)}


def _command(policy):
    return [policy["gpg_binary"], "--homedir", policy["gpg_home"], "--batch", "--no-tty"]


def _private_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def _sync(path):
    descriptor = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _gpg_stream(path, policy, arguments):
    """Feed GPG in bounded bulk reads, then require its authenticated completion.

    GPG's small direct reads can be very slow on removable NTFS volumes. Pipes
    keep those reads in memory; only this feeder touches the source filesystem.
    Output must be consumed concurrently so neither pipe can fill and deadlock.
    """
    with subprocess.Popen(
        _command(policy) + arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        failures = []
        stopped = threading.Event()

        def feed():
            try:
                with Path(path).open("rb") as incoming, process.stdin:
                    while not stopped.is_set():
                        chunk = incoming.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        process.stdin.write(chunk)
            except BrokenPipeError:
                # An early GPG exit is rejected by the return-code check below.
                failures.append(ValueError("GPG closed its archive input early"))
            except BaseException as exc:
                failures.append(exc)
                process.kill()

        feeder = threading.Thread(target=feed, name="strath-gpg-input")
        feeder.start()
        try:
            yield process.stdout
            # A tar reader can stop before GPG's authenticated end marker.
            while process.stdout.read(CHUNK_BYTES):
                pass
            process.stdout.close()
            returncode = process.wait()
            feeder.join()
            if returncode != 0 or failures:
                cause = failures[0] if failures else None
                raise ValueError("archive failed authenticated GPG processing") from cause
        finally:
            stopped.set()
            if process.poll() is None:
                process.kill()
            process.stdout.close()
            process.wait()
            feeder.join()


def plaintext_sha(path, policy):
    """Hash the entire decrypted archive without exposing or storing its contents."""
    with _gpg_stream(path, policy, ["--decrypt"]) as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _publication_lock(destination):
    import fcntl

    path = destination.with_name(destination.name + ".lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _publish(source, destination):
    # A hard link publishes a complete file atomically and refuses overwrite.
    os.link(source, destination)
    _sync(destination)
    source.unlink()


def encrypt_file(source, destination, policy_path):
    source, destination = Path(source).resolve(strict=True), Path(destination).absolute()
    policy = load_policy(policy_path)
    destination.parent.resolve(strict=True)
    if Path(policy["gpg_home"]).stat().st_dev == destination.parent.stat().st_dev:
        raise ValueError("private recovery key and encrypted archive need different filesystems")
    expected = sha(source)
    receipt = destination.with_name(destination.name + ".verified.json")
    with _publication_lock(destination):
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("encrypted archive already exists")
        if receipt.exists() or receipt.is_symlink():
            # A killed publisher may leave the verified receipt before the
            # ciphertext commit. Only quarantine a matching, tool-owned orphan.
            if receipt.is_symlink() or receipt.stat().st_size > 16384:
                raise FileExistsError("unrecognized orphan encryption receipt")
            orphan = json.loads(receipt.read_bytes())
            if (
                orphan.get("schema_version") != SCHEMA
                or orphan.get("recipient_fingerprint") != policy["recipient_fingerprint"]
                or orphan.get("plaintext_sha256") != expected
                or orphan.get("source_archive_name") != source.name
                or orphan.get("authenticated_decryption_readback") is not True
            ):
                raise FileExistsError("unrecognized orphan encryption receipt")
            receipt.rename(receipt.with_name(receipt.name + ".orphaned-" + uuid4().hex))
            _sync(receipt)
        token = uuid4().hex
        temporary = destination.with_name(destination.name + "." + token + ".next")
        temporary_receipt = receipt.with_name(receipt.name + "." + token + ".next")
        created = []
        try:
            outgoing = _private_file(temporary)
            created.append(temporary)
            with outgoing:
                with _gpg_stream(
                    source,
                    policy,
                    [
                        "--trust-model",
                        "always",
                        "--recipient",
                        policy["recipient_fingerprint"],
                        "--compress-algo",
                        "none",
                        "--cipher-algo",
                        "AES256",
                        "--encrypt",
                    ],
                ) as stream:
                    for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
                        outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if plaintext_sha(temporary, policy) != expected or sha(source) != expected:
                raise ValueError("encrypted archive readback or source stability check failed")
            result = {
                "schema_version": SCHEMA,
                "recipient_fingerprint": policy["recipient_fingerprint"],
                "ciphertext_sha256": sha(temporary),
                "plaintext_sha256": expected,
                "plaintext_bytes": source.stat().st_size,
                "source_archive_name": source.name,
                "authenticated_decryption_readback": True,
                "original_preserved": True,
            }
            stream = _private_file(temporary_receipt)
            created.append(temporary_receipt)
            with stream:
                stream.write(json.dumps(result, indent=2).encode())
                stream.flush()
                os.fsync(stream.fileno())
            # Durably publish the receipt first. The ciphertext name is the
            # commit marker, so a final-named archive always has its receipt.
            _publish(temporary_receipt, receipt)
            _publish(temporary, destination)
            return result
        except BaseException:
            # Never touch another publisher's destination or source archive.
            for path in created:
                if path.exists():
                    path.rename(path.with_name(path.name + ".failed-" + uuid4().hex))
            raise


def verify_file(path, policy_path):
    path = Path(path).resolve(strict=True)
    policy = load_policy(policy_path)
    receipt_path = path.with_name(path.name + ".verified.json")
    if receipt_path.stat().st_size > 16384:
        raise ValueError("encrypted archive verification receipt exceeds 16 KB")
    receipt = json.loads(receipt_path.read_bytes())
    if (
        receipt.get("schema_version") != SCHEMA
        or receipt.get("recipient_fingerprint") != policy["recipient_fingerprint"]
        or sha(path) != receipt["ciphertext_sha256"]
    ):
        raise ValueError("encrypted archive identity or ciphertext checksum differs")
    if plaintext_sha(path, policy) != receipt["plaintext_sha256"]:
        raise ValueError("encrypted archive plaintext checksum differs")
    return receipt


def tar_digests(path, policy_path):
    """Read all decrypted tar members without creating a plaintext archive."""
    policy = load_policy(policy_path)
    from strathmark.input_limits import tar_stream_digests

    with _gpg_stream(path, policy, ["--decrypt"]) as stream:
        return tar_stream_digests(stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("encrypt", "verify"))
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.operation == "encrypt":
        if args.destination is None:
            parser.error("encryption requires a new destination")
        result = encrypt_file(args.source, args.destination, args.policy)
    else:
        result = verify_file(args.source, args.policy)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
