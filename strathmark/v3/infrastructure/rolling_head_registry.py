"""Independently retained rolling-restart heads for offline rollback detection."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from strathmark.v3.application.capacity import CapacityManifest
from strathmark.v3.application.job_ports import (
    DurableJobError,
    RollingRestartExpectedHead,
    RollingRestartReceipt,
    RollingRestartTrust,
)
from strathmark.v3.contracts.canonical import canonical_bytes
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.infrastructure.integrity import (
    IntegrityError,
    IntegrityKeyIdentity,
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)

ZERO_DIGEST = "0" * 64
RECORD_SCHEMA_VERSION = "strathmark-v3-external-rolling-head-v1"
MANIFEST_KIND = "rolling_restart_external_head"
MAX_REFRESH_THRESHOLD = 48


class RollingHeadRegistryError(RuntimeError):
    """External retained-head material is unavailable, corrupt, or conflicting."""


@dataclass(frozen=True, slots=True)
class ExternalRollingHeadRecord:
    registry_sequence: int
    prior_registry_digest: str
    rolling_checkpoint_sequence: int
    rolling_checkpoint_digest: str
    source_global_sequence: int
    manifest: SignedManifest

    @property
    def registry_digest(self) -> str:
        return self.manifest.body_digest

    @property
    def expected_head(self) -> RollingRestartExpectedHead:
        return RollingRestartExpectedHead(
            self.rolling_checkpoint_sequence,
            self.rolling_checkpoint_digest,
        )


class ExternalRollingHeadRegistry:
    """Separate append-only filesystem anchor for rolling checkpoint heads.

    SQLite commits never wait on this registry.  A periodic composition service
    validates the bounded local suffix through ``DurableJobRepository`` and then
    advances this independently retained head.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        bootstrap_identity: IntegrityKeyIdentity,
        trust_store: IntegrityTrustStore,
        active_identity: IntegrityKeyIdentity,
        signer: P256Signer,
        refresh_threshold: int = MAX_REFRESH_THRESHOLD,
        max_elapsed_ms: int = 300_000,
    ) -> None:
        if isinstance(root, bool) or not isinstance(root, (Path, str)):
            raise RollingHeadRegistryError("rolling-head registry root is invalid")
        if not isinstance(bootstrap_identity, IntegrityKeyIdentity):
            raise RollingHeadRegistryError("rolling-head bootstrap identity is invalid")
        if not isinstance(trust_store, IntegrityTrustStore):
            raise RollingHeadRegistryError("rolling-head trust store is invalid")
        if not isinstance(active_identity, IntegrityKeyIdentity):
            raise RollingHeadRegistryError("rolling-head active identity is invalid")
        if not callable(getattr(signer, "sign", None)) or not hasattr(
            signer, "identity"
        ):
            raise RollingHeadRegistryError("rolling-head signer is invalid")
        if signer.identity != active_identity:
            raise RollingHeadRegistryError("active signer identity differs")
        try:
            trust_store.identity(bootstrap_identity.key_id)
            trust_store.identity(active_identity.key_id)
        except IntegrityError as exc:
            raise RollingHeadRegistryError(
                "rolling-head authority key is not trusted"
            ) from exc
        if (
            isinstance(refresh_threshold, bool)
            or not isinstance(refresh_threshold, int)
            or refresh_threshold <= 0
            or refresh_threshold > MAX_REFRESH_THRESHOLD
        ):
            raise RollingHeadRegistryError(
                "rolling-head refresh threshold exceeds the declared RPO"
            )
        if (
            isinstance(max_elapsed_ms, bool)
            or not isinstance(max_elapsed_ms, int)
            or max_elapsed_ms <= 0
        ):
            raise RollingHeadRegistryError("rolling-head elapsed RPO is invalid")
        self.root = Path(root).expanduser().resolve(strict=False)
        self.heads_root = self.root / "heads"
        self.root.mkdir(parents=True, exist_ok=True)
        self.heads_root.mkdir(parents=True, exist_ok=True)
        self._trust_store = trust_store
        self._active_identity = active_identity
        self._signer = signer
        self.refresh_threshold = refresh_threshold
        self.max_elapsed_ms = max_elapsed_ms
        self._install_bootstrap_identity(bootstrap_identity)
        self._records = self._load_records()

    @property
    def record_count(self) -> int:
        return len(self._records)

    @property
    def latest_expected_head(self) -> RollingRestartExpectedHead | None:
        return None if not self._records else self._records[-1].expected_head

    def open_repository(
        self,
        database_path: Path | str,
        *,
        capacity: CapacityManifest,
        signer: P256Signer,
        trust_store: IntegrityTrustStore,
        published_at: str,
    ):
        """Open under the retained head, then catch up an interrupted publication."""

        from strathmark.v3.infrastructure.sqlite.jobs import DurableJobRepository

        timestamp = require_utc_milliseconds(published_at)
        expected = self.latest_expected_head
        try:
            repository = DurableJobRepository(
                database_path,
                capacity=capacity,
                signer=signer,
                trust_store=trust_store,
                restart_trust=(
                    None
                    if expected is None
                    else RollingRestartTrust.externally_anchored(expected)
                ),
            )
            receipt = repository.recover_rolling_restart()
            status = repository.rolling_restart_suffix_status()
        except DurableJobError as exc:
            message = str(exc)
            if "external rolling head rolled back" in message:
                raise RollingHeadRegistryError(
                    "local rolling head rolled back"
                ) from exc
            raise RollingHeadRegistryError(
                "local rolling restart cannot reconcile to the retained head"
            ) from exc
        if (
            status.checkpoint_sequence != receipt.checkpoint_sequence
            or status.checkpoint_digest != receipt.checkpoint_digest
        ):
            raise RollingHeadRegistryError(
                "local rolling restart status changed during reconciliation"
            )
        self._publish_verified_receipt(receipt, published_at=timestamp, force=True)
        if expected is None:
            anchored = self.latest_expected_head
            if anchored is None:
                raise RollingHeadRegistryError(
                    "rolling-head bootstrap publication failed"
                )
            repository = DurableJobRepository(
                database_path,
                capacity=capacity,
                signer=signer,
                trust_store=trust_store,
                restart_trust=RollingRestartTrust.externally_anchored(anchored),
            )
        return repository

    def refresh_if_due(
        self,
        database_path: Path | str,
        *,
        capacity: CapacityManifest,
        signer: P256Signer,
        trust_store: IntegrityTrustStore,
        published_at: str,
    ) -> ExternalRollingHeadRecord | None:
        """Validate a bounded suffix and publish when the declared RPO is due."""

        from strathmark.v3.infrastructure.sqlite.jobs import DurableJobRepository

        timestamp = require_utc_milliseconds(published_at)
        expected = self.latest_expected_head
        if expected is None:
            self.open_repository(
                database_path,
                capacity=capacity,
                signer=signer,
                trust_store=trust_store,
                published_at=timestamp,
            )
            return self._records[-1]
        try:
            repository = DurableJobRepository(
                database_path,
                capacity=capacity,
                signer=signer,
                trust_store=trust_store,
                restart_trust=RollingRestartTrust.externally_anchored(expected),
            )
            receipt = repository.recover_rolling_restart()
            status = repository.rolling_restart_suffix_status()
        except DurableJobError as exc:
            message = str(exc)
            if "external rolling head rolled back" in message:
                raise RollingHeadRegistryError(
                    "local rolling head rolled back"
                ) from exc
            raise RollingHeadRegistryError(
                "local rolling restart cannot reconcile to the retained head"
            ) from exc
        if (
            status.checkpoint_sequence != receipt.checkpoint_sequence
            or status.checkpoint_digest != receipt.checkpoint_digest
        ):
            raise RollingHeadRegistryError(
                "local rolling restart status changed during reconciliation"
            )
        if status.checkpoint_sequence > expected.checkpoint_sequence:
            return self._publish_verified_receipt(
                receipt, published_at=timestamp, force=True
            )
        try:
            compacted = repository.refresh_rolling_restart_checkpoint_if_due(
                observed_at=timestamp,
                delta_threshold=self.refresh_threshold,
                max_elapsed_ms=self.max_elapsed_ms,
            )
            if compacted is None:
                return None
            verified = repository.recover_rolling_restart()
        except DurableJobError as exc:
            raise RollingHeadRegistryError(
                "local rolling restart cannot compact the retained suffix"
            ) from exc
        if (
            verified.checkpoint_sequence != compacted.checkpoint_sequence
            or verified.checkpoint_digest != compacted.checkpoint_digest
        ):
            raise RollingHeadRegistryError(
                "local rolling checkpoint changed before external publication"
            )
        return self._publish_verified_receipt(
            verified, published_at=timestamp, force=True
        )

    def _publish_verified_receipt(
        self,
        receipt: RollingRestartReceipt,
        *,
        published_at: str,
        force: bool,
    ) -> ExternalRollingHeadRecord | None:
        if not isinstance(receipt, RollingRestartReceipt):
            raise RollingHeadRegistryError(
                "rolling-head publication requires a verified restart receipt"
            )
        latest = None if not self._records else self._records[-1]
        if latest is not None:
            if receipt.checkpoint_sequence < latest.rolling_checkpoint_sequence:
                raise RollingHeadRegistryError("local rolling head rolled back")
            if receipt.checkpoint_sequence == latest.rolling_checkpoint_sequence:
                if receipt.checkpoint_digest != latest.rolling_checkpoint_digest:
                    raise RollingHeadRegistryError("local rolling head forked")
                return None
            if not force and (
                receipt.checkpoint_sequence - latest.rolling_checkpoint_sequence
                < self.refresh_threshold
            ):
                return None
        sequence = 1 if latest is None else latest.registry_sequence + 1
        prior_digest = ZERO_DIGEST if latest is None else latest.registry_digest
        payload = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "registry_sequence": sequence,
            "prior_registry_digest": prior_digest,
            "rolling_checkpoint_sequence": receipt.checkpoint_sequence,
            "rolling_checkpoint_digest": receipt.checkpoint_digest,
            "source_global_sequence": receipt.source_global_sequence,
        }
        manifest = sign_manifest(
            MANIFEST_KIND,
            payload,
            signer=self._signer,
            created_at=published_at,
        )
        record = ExternalRollingHeadRecord(
            sequence,
            prior_digest,
            receipt.checkpoint_sequence,
            receipt.checkpoint_digest,
            receipt.source_global_sequence,
            manifest,
        )
        self._publish_record(record)
        self._records = self._load_records()
        winner = self._records[-1]
        if winner != record:
            raise RollingHeadRegistryError(
                "rolling-head sequence already binds different material"
            )
        return winner

    def _install_bootstrap_identity(self, identity: IntegrityKeyIdentity) -> None:
        path = self.root / "bootstrap-key.json"
        expected = canonical_bytes(identity.to_dict())
        try:
            _publish_bytes_no_clobber(self.root, path, expected)
        except OSError as exc:
            raise RollingHeadRegistryError(
                "rolling-head bootstrap identity cannot be persisted"
            ) from exc
        try:
            observed = path.read_bytes()
        except OSError as exc:
            raise RollingHeadRegistryError(
                "rolling-head bootstrap identity cannot be read"
            ) from exc
        if observed != expected:
            raise RollingHeadRegistryError("rolling-head bootstrap key mismatch")

    def _publish_record(self, record: ExternalRollingHeadRecord) -> None:
        target = self.heads_root / f"{record.registry_sequence:016d}.json"
        encoded = canonical_bytes(record.manifest.to_dict())
        try:
            created = _publish_bytes_no_clobber(self.heads_root, target, encoded)
        except OSError as exc:
            raise RollingHeadRegistryError(
                "rolling-head record cannot be published"
            ) from exc
        if not created:
            winner = self._read_record(target)
            if winner != record:
                raise RollingHeadRegistryError(
                    "rolling-head sequence already binds different material"
                )

    def _load_records(self) -> tuple[ExternalRollingHeadRecord, ...]:
        paths = sorted(self.heads_root.glob("[0-9]*.json"))
        records: list[ExternalRollingHeadRecord] = []
        prior_digest = ZERO_DIGEST
        prior_checkpoint_sequence = 0
        prior_source_sequence = 0
        for expected_sequence, path in enumerate(paths, start=1):
            if path.name != f"{expected_sequence:016d}.json":
                raise RollingHeadRegistryError(
                    "external rolling-head registry has a sequence gap"
                )
            record = self._read_record(path)
            if record.registry_sequence != expected_sequence:
                raise RollingHeadRegistryError(
                    "external rolling-head registry has a sequence gap"
                )
            if record.prior_registry_digest != prior_digest:
                raise RollingHeadRegistryError(
                    "external rolling-head registry lineage differs"
                )
            if (
                record.rolling_checkpoint_sequence <= prior_checkpoint_sequence
                or record.source_global_sequence < prior_source_sequence
            ):
                raise RollingHeadRegistryError(
                    "external rolling-head registry rolled back or forked"
                )
            records.append(record)
            prior_digest = record.registry_digest
            prior_checkpoint_sequence = record.rolling_checkpoint_sequence
            prior_source_sequence = record.source_global_sequence
        return tuple(records)

    def _read_record(self, path: Path) -> ExternalRollingHeadRecord:
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
            if canonical_bytes(value) != raw:
                raise ValueError("record is not canonical")
            manifest = SignedManifest.from_dict(value)
            if manifest.kind != MANIFEST_KIND:
                raise ValueError("record kind differs")
            payload = verify_manifest(manifest, self._trust_store)
        except Exception as exc:
            raise RollingHeadRegistryError(
                "external rolling-head record integrity differs"
            ) from exc
        if (
            set(payload)
            != {
                "schema_version",
                "registry_sequence",
                "prior_registry_digest",
                "rolling_checkpoint_sequence",
                "rolling_checkpoint_digest",
                "source_global_sequence",
            }
            or payload.get("schema_version") != RECORD_SCHEMA_VERSION
        ):
            raise RollingHeadRegistryError(
                "external rolling-head record schema differs"
            )
        try:
            registry_sequence = _positive(
                payload["registry_sequence"], "registry sequence"
            )
            rolling_sequence = _positive(
                payload["rolling_checkpoint_sequence"],
                "rolling checkpoint sequence",
            )
            source_sequence = _nonnegative(
                payload["source_global_sequence"], "source sequence"
            )
            prior_digest = _digest(payload["prior_registry_digest"], "prior registry")
            rolling_digest = _digest(
                payload["rolling_checkpoint_digest"], "rolling checkpoint"
            )
        except RollingHeadRegistryError:
            raise
        return ExternalRollingHeadRecord(
            registry_sequence,
            prior_digest,
            rolling_sequence,
            rolling_digest,
            source_sequence,
            manifest,
        )


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RollingHeadRegistryError(f"{label} must be a positive integer")
    return value


def _fsync_directory(path: Path) -> None:
    """Persist the no-clobber directory entry where the platform supports it."""

    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_bytes_no_clobber(root: Path, target: Path, encoded: bytes) -> bool:
    """Install bytes once without an overwrite-capable final operation."""

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".pending-",
            suffix=".tmp",
            dir=root,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            return False
        _fsync_directory(root)
        return True
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _nonnegative(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RollingHeadRegistryError(f"{label} must be a non-negative integer")
    return value


def _digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RollingHeadRegistryError(f"{label} must be a lower-case SHA-256 digest")
    return value


__all__ = [
    "ExternalRollingHeadRecord",
    "ExternalRollingHeadRegistry",
    "MAX_REFRESH_THRESHOLD",
    "RollingHeadRegistryError",
]
