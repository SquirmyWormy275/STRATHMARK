"""Durable authority for the explicitly separate Linux competition profile.

Linux installation keys are persistent owner-only files. They never impersonate
Windows CNG identities or satisfy the Windows production release verifier.
Every accepted command includes a signed state and response, an immutable request
identity, and the preceding chain digest. A separate head detects database rollback.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import tarfile
import tempfile
import zlib
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.infrastructure.integrity import (
    IntegrityKeyClass,
    IntegrityKeyIdentity,
    IntegrityTrustStore,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)

MAX_BYTES = 20_000_000
EMPTY_DIGEST = "0" * 64


class LinuxLifecycleError(ValueError):
    """A local competition operation cannot safely proceed."""


def encode(value: object) -> bytes:
    return canonical_bytes(value, max_bytes=MAX_BYTES)


def digest(value: object) -> str:
    return canonical_digest(value, max_bytes=MAX_BYTES)


def _private(path: Path) -> None:
    if path.is_symlink():
        raise LinuxLifecycleError("installation material cannot be a symlink")
    stat = path.stat()
    if os.name == "posix" and (stat.st_uid != os.geteuid() or stat.st_mode & 0o077):
        raise LinuxLifecycleError("installation material must be owned by this user and private")


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_private(path: Path, raw: bytes, *, replace: bool = False) -> None:
    temporary = (
        path.with_name(path.name + "." + secrets.token_hex(8) + ".next") if replace else path
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if replace and temporary.exists():
            temporary.unlink()


def _archive_digests(path):
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = [item for item in archive.getmembers() if item.isfile()]
            if len({item.name for item in members}) != len(members):
                raise LinuxLifecycleError("recovery archive has duplicate members")
            return {
                item.name: sha256(archive.extractfile(item).read()).hexdigest() for item in members
            }
    except (OSError, tarfile.TarError, EOFError, zlib.error) as error:
        raise LinuxLifecycleError("recovery archive is corrupted") from error


class LinuxInstallationSigner:
    """File-backed local authority; deliberately distinct from production CNG."""

    def __init__(self, key: ec.EllipticCurvePrivateKey) -> None:
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise LinuxLifecycleError("installation signing key must be P-256")
        public = base64.b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        ).decode("ascii")
        self._key = key
        self.identity = IntegrityKeyIdentity(
            "integrity-key:linux-" + digest({"public_key": public}),
            IntegrityKeyClass.LINUX_INSTALLATION,
            "linux_owner_private_p256_sha256",
            public,
        )

    def sign(self, payload: bytes) -> bytes:
        return self._key.sign(payload, ec.ECDSA(hashes.SHA256()))


class LinuxLifecycleStore:
    """Single-writer signed command log with exact retry and restart recovery."""

    @classmethod
    def initialize(cls, root: Path | str) -> LinuxLifecycleStore:
        root = Path(root).expanduser().absolute()
        if root.is_symlink():
            raise LinuxLifecycleError("installation root cannot be a symlink")
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        _private(root)
        key_path = root / "installation-key.pem"
        identity_path = root / "installation-identity.json"
        if key_path.exists() or identity_path.exists():
            if not key_path.exists() or not identity_path.exists():
                raise LinuxLifecycleError("incomplete installation identity requires recovery")
            return cls(root)
        if (root / "competition.sqlite3").exists():
            raise LinuxLifecycleError("never create a replacement key for an existing ledger")
        key = ec.generate_private_key(ec.SECP256R1())
        signer = LinuxInstallationSigner(key)
        _write_private(
            key_path,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        _write_private(identity_path, encode(signer.identity.to_dict()))
        _write_private(root / "head.json", encode({"sequence": 0, "event_digest": EMPTY_DIGEST}))
        with sqlite3.connect(root / "competition.sqlite3") as connection:
            connection.executescript(
                """
                PRAGMA trusted_schema=OFF;
                PRAGMA synchronous=FULL;
                PRAGMA journal_mode=WAL;
                CREATE TABLE commands (
                    sequence INTEGER PRIMARY KEY,
                    command_id TEXT NOT NULL UNIQUE,
                    operation TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    event_digest TEXT NOT NULL UNIQUE,
                    manifest_json TEXT NOT NULL,
                    body_json TEXT NOT NULL
                );
                CREATE TRIGGER commands_no_update BEFORE UPDATE ON commands
                    BEGIN SELECT RAISE(ABORT, 'immutable Linux command authority'); END;
                CREATE TRIGGER commands_no_delete BEFORE DELETE ON commands
                    BEGIN SELECT RAISE(ABORT, 'immutable Linux command authority'); END;
                """
            )
        os.chmod(root / "competition.sqlite3", 0o600)
        _sync_directory(root)
        return cls(root)

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().absolute()
        for path in (
            self.root,
            self.root / "installation-key.pem",
            self.root / "installation-identity.json",
            self.root / "head.json",
        ):
            _private(path)
        key = serialization.load_pem_private_key(
            (self.root / "installation-key.pem").read_bytes(), password=None
        )
        self.signer = LinuxInstallationSigner(key)
        expected = json.loads((self.root / "installation-identity.json").read_bytes())
        if expected != self.signer.identity.to_dict():
            raise LinuxLifecycleError("installation key differs from pinned public identity")
        self.trust = IntegrityTrustStore((self.signer.identity,))
        self.database = self.root / "competition.sqlite3"
        _private(self.database)
        with self._connect() as connection:
            self._verify(connection)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _writer_lock(self):
        """Hold the installation lock through the SQLite commit and head fsync."""
        descriptor = os.open(self.root / "writer.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            else:
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            yield
        finally:
            os.close(descriptor)

    def _body(self, row) -> dict:
        try:
            manifest = SignedManifest.from_dict(json.loads(row[6]))
            signed_tip = verify_manifest(manifest, self.trust)
            body = json.loads(row[7])
            if signed_tip != {"event_digest": digest(body)}:
                raise LinuxLifecycleError("command body differs from signed digest")
            if manifest.kind != "linux_competition_command" or (
                body["sequence"],
                body["command_id"],
                body["operation"],
                body["scope_id"],
                body["request_digest"],
                digest(body),
            ) != tuple(row[:6]):
                raise LinuxLifecycleError("command indexes differ from signed authority")
            if body["request_digest"] != digest(body["request"]) or body["state_digest"] != digest(
                body["state"]
            ):
                raise LinuxLifecycleError("command request or state identity differs")
            return body
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise LinuxLifecycleError("Linux command authority is invalid") from error

    def _verify(self, connection) -> tuple[dict, dict]:
        previous = EMPTY_DIGEST
        state = {"roots": {}}
        expected_head = json.loads((self.root / "head.json").read_bytes())
        sequence = 0
        head_seen = expected_head == {"sequence": 0, "event_digest": EMPTY_DIGEST}
        for row in connection.execute("SELECT * FROM commands ORDER BY sequence"):
            body = self._body(row)
            if body["sequence"] != sequence + 1 or body["previous_digest"] != previous:
                raise LinuxLifecycleError("Linux command authority chain is incomplete")
            sequence, previous, state = body["sequence"], row[5], body["state"]
            if expected_head == {"sequence": sequence, "event_digest": previous}:
                head_seen = True
        if not head_seen:
            raise LinuxLifecycleError("database rollback differs from retained installation head")
        return state, {"sequence": sequence, "event_digest": previous}

    def state(self) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN")
            state, _head = self._verify(connection)
            return state

    def archive_backup(self, directory: Path) -> Path:
        """Verify an independently stored, consistent installation recovery archive."""
        directory = Path(directory).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix=".backup-", dir=self.root.parent) as staging:
            snapshot = Path(staging) / "installation"
            self.backup(snapshot)
            head = json.loads((snapshot / "head.json").read_bytes())
            target = (
                directory
                / f"linux-competition-{self.signer.identity.key_id[-16:]}-{head['sequence']:08d}-{head['event_digest'][:16]}.tar.gz"
            )
            files = {
                str(path.relative_to(Path(staging))): sha256(path.read_bytes()).hexdigest()
                for path in snapshot.rglob("*")
                if path.is_file()
            }
            if target.exists():
                if _archive_digests(target) != files:
                    raise LinuxLifecycleError("existing recovery archive readback differs")
                return target
            temporary = target.with_name(target.name + "." + secrets.token_hex(8) + ".next")
            try:
                with tarfile.open(temporary, "w:gz", compresslevel=1) as archive:
                    archive.add(snapshot, arcname="installation")
                if _archive_digests(temporary) != files:
                    raise LinuxLifecycleError("recovery archive readback differs")
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, target)
                _sync_directory(directory)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return target

    def lookup(
        self, command_id: str, request: dict, *, operation: str | None = None
    ) -> dict | None:
        with self._connect() as connection:
            connection.execute("BEGIN")
            self._verify(connection)
            row = connection.execute(
                "SELECT * FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None:
                return None
            body = self._body(row)
            if operation is not None and body["operation"] != operation:
                raise LinuxLifecycleError("same command identity has changed operation")
            if body["request_digest"] != digest(request):
                raise LinuxLifecycleError("same command identity has changed request bytes")
            return body["response"]

    def execute(
        self,
        *,
        command_id: str,
        operation: str,
        scope_id: str,
        request: dict,
        occurred_at: str,
        transition: Callable[[dict], dict],
    ) -> dict:
        if not all(isinstance(value, str) and value for value in (command_id, operation, scope_id)):
            raise LinuxLifecycleError("command identity, operation, and scope are required")
        request_hash = digest(request)
        with self._writer_lock(), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                state, head = self._verify(connection)
                row = connection.execute(
                    "SELECT * FROM commands WHERE command_id=?", (command_id,)
                ).fetchone()
                if row is not None:
                    body = self._body(row)
                    if (body["operation"], body["scope_id"], body["request_digest"]) != (
                        operation,
                        scope_id,
                        request_hash,
                    ):
                        raise LinuxLifecycleError(
                            "same command identity has changed authority or request"
                        )
                    response = body["response"]
                else:
                    response = transition(state)
                    if operation == "close_scope":
                        response["authority_sequence"] = head["sequence"] + 1
                    if not isinstance(response, dict):
                        raise LinuxLifecycleError(
                            "command transition must return a closed response"
                        )
                    response = json.loads(encode(response))
                    state = json.loads(encode(state))
                    body = {
                        "sequence": head["sequence"] + 1,
                        "previous_digest": head["event_digest"],
                        "command_id": command_id,
                        "operation": operation,
                        "scope_id": scope_id,
                        "request_digest": request_hash,
                        "request": request,
                        "state_digest": digest(state),
                        "state": state,
                        "response": response,
                    }
                    event_digest = digest(body)
                    manifest = sign_manifest(
                        "linux_competition_command",
                        {"event_digest": event_digest},
                        signer=self.signer,
                        created_at=occurred_at,
                    )
                    connection.execute(
                        "INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)",
                        (
                            body["sequence"],
                            command_id,
                            operation,
                            scope_id,
                            request_hash,
                            event_digest,
                            encode(manifest.to_dict()).decode(),
                            encode(body).decode(),
                        ),
                    )
                    head = {"sequence": body["sequence"], "event_digest": event_digest}
                connection.commit()
                _write_private(self.root / "head.json", encode(head), replace=True)
                return response
            except BaseException:
                connection.rollback()
                raise

    def backup(self, destination: Path | str) -> dict:
        """Export one independently verifiable, complete local authority snapshot."""
        destination = Path(destination).expanduser().absolute()
        destination.mkdir(parents=True, mode=0o700, exist_ok=False)
        with self._writer_lock(), self._connect() as source:
            source.execute("BEGIN IMMEDIATE")
            state, head = self._verify(source)
            # A separate read connection obtains a consistent SQLite backup while
            # the writer lock prevents new commands and head changes.
            with (
                sqlite3.connect(self.database) as reader,
                sqlite3.connect(destination / "competition.sqlite3") as target,
            ):
                reader.backup(target)
            for name in ("installation-key.pem", "installation-identity.json"):
                _write_private(destination / name, (self.root / name).read_bytes())
            for competition in state["roots"].values():
                history = competition.get("history")
                if history is None:
                    continue
                relative = Path(history["path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise LinuxLifecycleError("backup history path is not installation-relative")
                original = self.root / relative
                target = destination / relative
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                from hashlib import sha256

                raw = original.read_bytes()
                if sha256(raw).hexdigest() != history["sha256"]:
                    raise LinuxLifecycleError("backup history differs from signed source")
                _write_private(target, raw)
                _write_private(
                    target.parent / "history.json", (original.parent / "history.json").read_bytes()
                )
            _write_private(destination / "head.json", encode(head))
            os.chmod(destination / "competition.sqlite3", 0o600)
            descriptor = os.open(destination / "competition.sqlite3", os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _sync_directory(destination)
            source.rollback()
        restored = LinuxLifecycleStore(destination)
        if restored.state() != self.state():
            # A concurrent new command after the snapshot is allowed. Verify the
            # backup against its signed tip rather than demand the latest state.
            with restored._connect() as connection:
                _state, recovered_head = restored._verify(connection)
            if recovered_head != head:
                raise LinuxLifecycleError("restored backup head differs")
        return {"path": str(destination), "head": head, "verified": True}
