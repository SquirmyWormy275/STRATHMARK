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
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from uuid import uuid4

SCHEMA = "strath-encrypted-recovery-v1"
CHUNK_BYTES = 1024 * 1024


DEFAULT_PLAINTEXT_BYTES = 128 * 1024 * 1024
MAX_PLAINTEXT_BYTES = 1024**4
MAX_PROCESS_SECONDS = 6 * 60 * 60


def _allowance(value):
    # Only the caller's independent source/inventory may enlarge this budget.
    if type(value) is not int or not 0 <= value <= MAX_PLAINTEXT_BYTES:
        raise ValueError("invalid independent plaintext byte allowance")
    return value


def _ciphertext_allowance(plaintext_bytes):
    return 2 * _allowance(plaintext_bytes) + CHUNK_BYTES


def _duration(plaintext_bytes):
    # One MiB/second allows conservative removable-drive throughput, with a cap.
    return min(MAX_PROCESS_SECONDS, 30 + _allowance(plaintext_bytes) / CHUNK_BYTES)


@contextmanager
def _regular_input(path, maximum):
    """Open a bounded regular file once, without following a final link."""
    path = Path(path).absolute()
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400:
        raise ValueError("archive input must be a regular file, not a link or device")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or getattr(opened, "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("archive input changed while opening")
        if opened.st_size > maximum:
            raise ValueError("archive input exceeds byte allowance")
        with os.fdopen(descriptor, "rb", closefd=False) as incoming:
            yield incoming
    finally:
        os.close(descriptor)


def _hash_stream(stream, maximum, seconds):
    deadline = time.monotonic() + seconds
    digest, count = hashlib.sha256(), 0
    for chunk in iter(lambda: stream.read(min(CHUNK_BYTES, maximum - count + 1)), b""):
        count += len(chunk)
        if count > maximum:
            raise ValueError("archive input exceeds byte allowance")
        if time.monotonic() > deadline:
            raise ValueError("archive input deadline expired")
        digest.update(chunk)
    return digest.hexdigest()


def sha(path):
    with _regular_input(path, DEFAULT_PLAINTEXT_BYTES) as stream:
        return _hash_stream(stream, DEFAULT_PLAINTEXT_BYTES, _duration(DEFAULT_PLAINTEXT_BYTES))


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
def _gpg_stream(source, policy, arguments, *, input_limit, output_limit, seconds):
    """Bound one GPG process and hash exactly the bytes supplied through its pipe.

    A watchdog kills GPG even when its output pipe blocks. An underlying regular
    file read stuck inside the OS can still delay feeder cleanup; this is not an
    interruptible filesystem API. Bulk reads preserve removable-drive throughput.
    """
    if hasattr(source, "read"):
        incoming_context = nullcontext(source)
    else:
        incoming_context = _regular_input(source, input_limit)
    with (
        incoming_context as incoming,
        subprocess.Popen(
            _command(policy) + arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process,
    ):
        failures = []
        stopped, expired = threading.Event(), threading.Event()
        input_digest = hashlib.sha256()
        input_bytes = 0

        def expire():
            expired.set()
            stopped.set()
            if process.poll() is None:
                process.kill()

        def feed():
            nonlocal input_bytes
            try:
                with process.stdin:
                    while not stopped.is_set():
                        chunk = incoming.read(min(CHUNK_BYTES, input_limit - input_bytes + 1))
                        if not chunk:
                            break
                        input_bytes += len(chunk)
                        if input_bytes > input_limit:
                            raise ValueError("archive input exceeds byte allowance")
                        input_digest.update(chunk)
                        process.stdin.write(chunk)
            except BaseException as exc:
                failures.append(exc)
                if process.poll() is None:
                    process.kill()

        class Output:
            bytes_read = 0

            @property
            def input_sha256(self):
                return input_digest.hexdigest()

            def read(self, size=-1):
                if expired.is_set():
                    raise ValueError("GPG archive processing deadline expired")
                size = min(
                    size if size >= 0 else CHUNK_BYTES,
                    CHUNK_BYTES,
                    output_limit - self.bytes_read + 1,
                )
                chunk = process.stdout.read(size)
                self.bytes_read += len(chunk)
                if expired.is_set():
                    raise ValueError("GPG archive processing deadline expired")
                if self.bytes_read > output_limit:
                    raise ValueError("GPG archive output exceeds byte allowance")
                return chunk

        output = Output()
        feeder = threading.Thread(target=feed, name="strath-gpg-input")
        watchdog = threading.Timer(seconds, expire)
        watchdog.name = "strath-gpg-deadline"
        watchdog.start()
        feeder.start()
        try:
            yield output
            # TAR can stop before GPG's authenticated end marker. Bound the tail too.
            while output.read(CHUNK_BYTES):
                pass
            process.stdout.close()
            returncode = process.wait()
            feeder.join()
            if expired.is_set():
                raise ValueError("GPG archive processing deadline expired")
            if failures and isinstance(failures[0], ValueError):
                raise failures[0]
            if returncode != 0 or failures:
                cause = failures[0] if failures else None
                raise ValueError("archive failed authenticated GPG processing") from cause
        finally:
            stopped.set()
            watchdog.cancel()
            if process.poll() is None:
                process.kill()
            process.stdout.close()
            process.wait()
            feeder.join()
            watchdog.join()


def _decryption_report(path, policy, maximum):
    maximum = _allowance(maximum)
    with _gpg_stream(
        path,
        policy,
        ["--decrypt"],
        input_limit=_ciphertext_allowance(maximum),
        output_limit=maximum,
        seconds=_duration(maximum),
    ) as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest(), stream.bytes_read, stream.input_sha256


def plaintext_sha(path, policy, *, max_plaintext_bytes=DEFAULT_PLAINTEXT_BYTES):
    """Bounded integrity readback; this alone does not authenticate archive origin."""
    return _decryption_report(path, policy, max_plaintext_bytes)[0]


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
    source, destination = Path(source).absolute(), Path(destination).absolute()
    with _regular_input(source, MAX_PLAINTEXT_BYTES) as incoming:
        return _encrypt_opened(source, incoming, destination, policy_path)


def _encrypt_opened(source, incoming, destination, policy_path):
    policy = load_policy(policy_path)
    destination.parent.resolve(strict=True)
    if Path(policy["gpg_home"]).stat().st_dev == destination.parent.stat().st_dev:
        raise ValueError("private recovery key and encrypted archive need different filesystems")
    source_size = os.fstat(incoming.fileno()).st_size
    maximum, seconds = _allowance(source_size), _duration(source_size)
    expected = _hash_stream(incoming, maximum, seconds)
    incoming.seek(0)
    receipt = destination.with_name(destination.name + ".verified.json")
    with _publication_lock(destination):
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("encrypted archive already exists")
        if receipt.exists() or receipt.is_symlink():
            # A killed publisher may leave the verified receipt before the
            # ciphertext commit. Only quarantine a matching, tool-owned orphan.
            if receipt.is_symlink() or receipt.stat().st_size > 16384:
                raise FileExistsError("unrecognized orphan encryption receipt")
            orphan = _receipt(receipt)
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
                    incoming,
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
                    input_limit=maximum,
                    output_limit=_ciphertext_allowance(maximum),
                    seconds=seconds,
                ) as stream:
                    for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
                        outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            incoming.seek(0)
            actual, count, ciphertext_digest = _decryption_report(temporary, policy, maximum)
            if (
                actual != expected
                or count != source_size
                or _hash_stream(incoming, maximum, seconds) != expected
            ):
                raise ValueError("encrypted archive readback or source stability check failed")
            result = {
                "schema_version": SCHEMA,
                "recipient_fingerprint": policy["recipient_fingerprint"],
                "ciphertext_sha256": ciphertext_digest,
                "plaintext_sha256": expected,
                "plaintext_bytes": source_size,
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


def _receipt(path):
    with _regular_input(path, 16384) as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError("encrypted archive receipt exceeds 16 KB")
    receipt = json.loads(raw)
    if not isinstance(receipt, dict):
        raise ValueError("invalid encrypted archive receipt")
    return receipt


def inspect_file(path, policy_path, *, max_plaintext_bytes=DEFAULT_PLAINTEXT_BYTES):
    """Check bounded legacy integrity only; the adjacent receipt is not trusted."""
    maximum = _allowance(max_plaintext_bytes)
    path = Path(path).absolute()
    policy = load_policy(policy_path)
    receipt = _receipt(path.with_name(path.name + ".verified.json"))
    if (
        receipt.get("schema_version") != SCHEMA
        or receipt.get("recipient_fingerprint") != policy["recipient_fingerprint"]
    ):
        raise ValueError("encrypted archive receipt identity differs")
    size = receipt.get("plaintext_bytes")
    if type(size) is not int or not 0 <= size <= maximum:
        raise ValueError("receipt plaintext size exceeds independent byte allowance")
    actual, count, ciphertext = _decryption_report(path, policy, maximum)
    if ciphertext != receipt.get("ciphertext_sha256"):
        raise ValueError("encrypted archive ciphertext checksum differs")
    if actual != receipt.get("plaintext_sha256") or count != size:
        raise ValueError("encrypted archive plaintext checksum or size differs")
    return {
        "verification_scope": "integrity_only",
        "origin_authenticated": False,
        "plaintext_sha256": actual,
        "plaintext_bytes": count,
        "untrusted_receipt": receipt,
    }


def verify_file(
    path,
    policy_path,
    *,
    expected_plaintext_sha256=None,
    max_plaintext_bytes=DEFAULT_PLAINTEXT_BYTES,
):
    """Require the plaintext identity retained independently of ciphertext/receipt.

    The pin identifies exact content, not a signer, installation label or latest
    sequence. Never obtain it from the neighboring unsigned receipt.
    """
    if (
        not isinstance(expected_plaintext_sha256, str)
        or re.fullmatch(r"[a-fA-F0-9]{64}", expected_plaintext_sha256) is None
    ):
        raise ValueError("verification requires an independently trusted plaintext SHA-256")
    result = inspect_file(path, policy_path, max_plaintext_bytes=max_plaintext_bytes)
    if result["plaintext_sha256"] != expected_plaintext_sha256.lower():
        raise ValueError("archive differs from independently trusted plaintext identity")
    return {**result, "verification_scope": "trusted_plaintext_identity"}


def tar_digests(path, policy_path, *, max_plaintext_bytes=DEFAULT_PLAINTEXT_BYTES):
    """Read all decrypted tar members without creating a plaintext archive."""
    policy = load_policy(policy_path)
    from strathmark.input_limits import tar_stream_digests

    maximum = _allowance(max_plaintext_bytes)
    with _gpg_stream(
        path,
        policy,
        ["--decrypt"],
        input_limit=_ciphertext_allowance(maximum),
        output_limit=maximum,
        seconds=_duration(maximum),
    ) as stream:
        return tar_stream_digests(stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("encrypt", "verify", "inspect"))
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument(
        "--expected-plaintext-sha256",
        help="independently retained plaintext identity; never use the sidecar",
    )
    parser.add_argument(
        "--max-plaintext-bytes",
        type=int,
        default=DEFAULT_PLAINTEXT_BYTES,
        help="independent source/inventory allowance (default: 128 MiB)",
    )
    args = parser.parse_args()
    if args.operation == "encrypt":
        if args.destination is None:
            parser.error("encryption requires a new destination")
        if args.expected_plaintext_sha256 is not None:
            parser.error("the trusted plaintext identity applies only to verify")
        result = encrypt_file(args.source, args.destination, args.policy)
    elif args.operation == "verify":
        if args.expected_plaintext_sha256 is None:
            parser.error("verify requires --expected-plaintext-sha256 from an independent record")
        result = verify_file(
            args.source,
            args.policy,
            expected_plaintext_sha256=args.expected_plaintext_sha256,
            max_plaintext_bytes=args.max_plaintext_bytes,
        )
    else:
        if args.expected_plaintext_sha256 is not None:
            parser.error("use verify to check a trusted plaintext identity")
        result = inspect_file(
            args.source, args.policy, max_plaintext_bytes=args.max_plaintext_bytes
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
