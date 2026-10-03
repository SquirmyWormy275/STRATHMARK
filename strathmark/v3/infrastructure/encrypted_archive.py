"""Standard-library GPG archive encryption with full decryption readback.

This file also ships as STRATHEX's standalone recovery_security.py bootstrap.
Private keys stay in an explicitly configured workstation GPG home. No source
archive is deleted, and no plaintext is written to the archive filesystem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path
from uuid import uuid4

SCHEMA = "strath-encrypted-recovery-v1"


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_policy(path):
    path = Path(path).resolve(strict=True)
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
        Path(value["gpg_binary"]).resolve(strict=True),
        Path(value["gpg_home"]).resolve(strict=True),
    )
    if sha(binary) != value["gpg_binary_sha256"] or not home.is_dir():
        raise ValueError("GPG binary or private recovery home changed")
    if os.name == "posix" and (home.stat().st_uid != os.getuid() or home.stat().st_mode & 0o077):
        raise ValueError("GPG private home must be owned by this user with mode 700")
    return value


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


def plaintext_sha(path, policy):
    """Hash the entire decrypted archive without exposing or storing its contents."""
    with subprocess.Popen(
        _command(policy) + ["--decrypt", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        digest = hashlib.sha256()
        for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
            digest.update(chunk)
        process.stdout.close()
        if process.wait() != 0:
            raise ValueError("encrypted archive failed authenticated GPG decryption")
    return digest.hexdigest()


def encrypt_file(source, destination, policy_path):
    source, destination = Path(source).resolve(strict=True), Path(destination).absolute()
    policy = load_policy(policy_path)
    destination.parent.resolve(strict=True)
    if (
        destination.exists()
        or destination.is_symlink()
        or destination.with_name(destination.name + ".verified.json").exists()
    ):
        raise FileExistsError("encrypted archive or verification receipt already exists")
    if Path(policy["gpg_home"]).stat().st_dev == destination.parent.stat().st_dev:
        raise ValueError("private recovery key and encrypted archive need different filesystems")
    expected = sha(source)
    created = False
    try:
        with source.open("rb") as incoming:
            outgoing = _private_file(destination)
            created = True
            with outgoing:
                completed = subprocess.run(
                    _command(policy)
                    + [
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
                    stdin=incoming,
                    stdout=outgoing,
                    stderr=subprocess.PIPE,
                )
                if completed.returncode != 0:
                    raise ValueError("GPG could not encrypt to the recovery recipient")
                outgoing.flush()
                os.fsync(outgoing.fileno())
        if plaintext_sha(destination, policy) != expected or sha(source) != expected:
            raise ValueError("encrypted archive readback or source stability check failed")
        result = {
            "schema_version": SCHEMA,
            "recipient_fingerprint": policy["recipient_fingerprint"],
            "ciphertext_sha256": sha(destination),
            "plaintext_sha256": expected,
            "plaintext_bytes": source.stat().st_size,
            "source_archive_name": source.name,
            "authenticated_decryption_readback": True,
            "original_preserved": True,
        }
        with _private_file(destination.with_name(destination.name + ".verified.json")) as stream:
            stream.write(json.dumps(result, indent=2).encode())
            stream.flush()
            os.fsync(stream.fileno())
        _sync(destination)
        return result
    except Exception:
        # Keep partial evidence for diagnosis; never advertise it as verified.
        if created and destination.exists():
            destination.rename(destination.with_name(destination.name + ".failed-" + uuid4().hex))
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
    result = {}
    with subprocess.Popen(
        _command(policy) + ["--decrypt", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|gz") as archive:
                for member in archive:
                    if (
                        member.issym()
                        or member.islnk()
                        or Path(member.name).is_absolute()
                        or ".." in Path(member.name).parts
                    ):
                        raise ValueError("encrypted competition archive contains unsafe members")
                    if not member.isfile():
                        continue
                    if member.name in result:
                        raise ValueError("encrypted competition archive repeats a member")
                    digest = hashlib.sha256()
                    stream = archive.extractfile(member)
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                    result[member.name] = digest.hexdigest()
            # Drain through GPG's authenticated end marker, even after tar EOF.
            while process.stdout.read(1024 * 1024):
                pass
            process.stdout.close()
            if process.wait() != 0:
                raise ValueError("encrypted tar failed authenticated GPG decryption")
        except Exception:
            process.kill()
            process.wait()
            raise
    return result


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
