from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from strathmark.v3.contracts.canonical import canonical_bytes
from strathmark.v3.contracts.commands import BlobReferenceV2, BlobRetentionClass
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.identifiers import deterministic_identifier
from strathmark.v3.infrastructure.blobs import (
    BLOB_DURABILITY_STAGES,
    BlobIntegrityError,
    BlobMetadata,
    BlobRetention,
    BlobStoreError,
    ContentAddressedBlobStore,
    InjectedBlobFailure,
)


def _metadata() -> BlobMetadata:
    return BlobMetadata(
        media_type="application/json",
        payload_schema_version="strathmark-v3-test-payload-v1",
        retention=BlobRetention.REQUIRED,
    )


def _blob_payload(label: bytes = b"payload") -> bytes:
    return label + b"x" * (65_537 - len(label))


def test_blob_is_fsynced_atomically_published_and_exactly_verified(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    payload = b'{"large":"' + (b"x" * 1_048_576) + b'"}'

    reference = store.publish(payload, metadata=_metadata())
    assert isinstance(reference, BlobReferenceV2)
    assert reference.blob_id == deterministic_identifier("blob", {"digest": reference.digest})
    assert reference.byte_count == len(payload)
    assert store.read(reference) == payload
    assert store.verify(reference) == reference
    assert store.publish(payload, metadata=_metadata()) == reference
    assert not tuple((tmp_path / "blobs").rglob("*.tmp"))
    with pytest.raises(ContractError, match="inline boundary"):
        store.publish(b"x" * 65_536, metadata=_metadata())


@pytest.mark.parametrize("stage", BLOB_DURABILITY_STAGES)
def test_every_blob_crash_prefix_is_partial_or_an_unreferenced_orphan(
    tmp_path: Path, stage: str
) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")

    def fail(observed: str) -> None:
        if observed == stage:
            raise InjectedBlobFailure(stage)

    with pytest.raises(InjectedBlobFailure, match=stage):
        store.publish(_blob_payload(), metadata=_metadata(), fault_hook=fail)

    # A renamed complete blob is harmless until an authority event references it.
    assert all(path.suffix != ".tmp" for path in store.complete_paths())


def test_write_and_fsync_are_distinct_injectable_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.infrastructure.blobs as module

    calls = 0
    real_fsync = module.os.fsync

    def counted(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        real_fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", counted)
    store = ContentAddressedBlobStore(tmp_path / "blobs")

    def fail(stage: str) -> None:
        if stage == "after_temp_write":
            assert calls == 0
            raise InjectedBlobFailure(stage)

    with pytest.raises(InjectedBlobFailure):
        store.publish(_blob_payload(), metadata=_metadata(), fault_hook=fail)


def test_identical_content_supports_distinct_reference_metadata(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    payload = _blob_payload(b"same bytes")
    first = store.publish(payload, metadata=_metadata())
    second = store.publish(
        payload,
        metadata=BlobMetadata(
            media_type="application/octet-stream",
            payload_schema_version="strathmark-v3-binary-payload-v2",
            retention=BlobRetention.ARCHIVABLE,
        ),
    )
    assert first.digest == second.digest
    assert first != second
    assert store.verify(first) == first
    assert store.verify(second) == second


def test_verified_blob_lease_hashes_once_and_blocks_mutation_until_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.infrastructure.blobs as module

    store = ContentAddressedBlobStore(tmp_path / "blobs")
    reference = store.publish(_blob_payload(b"leased"), metadata=_metadata())
    path = store.path_for(reference.digest)
    original_digest = module._file_digest
    digest_calls = 0

    def counted_digest(value: Path) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return original_digest(value)

    monkeypatch.setattr(module, "_file_digest", counted_digest)
    with store.verified_lease(reference) as lease:
        assert digest_calls == 1
        assert lease.verify_current() == reference
        assert digest_calls == 1
        if os.name == "nt":
            with pytest.raises(OSError):
                path.write_bytes(b"y" * reference.byte_count)
            with pytest.raises(OSError):
                path.unlink()
    path.write_bytes(b"y" * reference.byte_count)


def test_verified_blob_lease_closes_every_handle_after_lock_or_unlock_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.infrastructure.blobs as module

    store = ContentAddressedBlobStore(tmp_path / "blobs")
    first = store.publish(_blob_payload(b"lock-failure"), metadata=_metadata())
    first_path = store.path_for(first.digest)
    original_open = Path.open
    opened = []

    def tracked_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        opened.append(handle)
        return handle

    def fail_lock(_handle, _length):
        raise OSError("busy")

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(module, "_lock_blob_handle", fail_lock)
    with pytest.raises(BlobIntegrityError, match="leased immutably"):
        with store.verified_lease(first):
            pass
    assert opened and all(handle.closed for handle in opened)
    first_path.unlink()

    monkeypatch.undo()
    second = store.publish(_blob_payload(b"unlock-failure"), metadata=_metadata())
    second_path = store.path_for(second.digest)
    second_metadata = store._metadata_path(second)
    original_unlock = module._unlock_blob_handle
    calls = 0

    def fail_first_unlock(handle, length):
        nonlocal calls
        calls += 1
        original_unlock(handle, length)
        if calls == 1:
            raise OSError("unlock failed")

    monkeypatch.setattr(module, "_unlock_blob_handle", fail_first_unlock)
    lease = store.verified_lease(second)
    with pytest.raises(OSError, match="unlock failed"):
        with lease:
            pass
    second_path.unlink()
    second_metadata.unlink()


def test_referenced_missing_corrupt_or_metadata_mismatch_blob_fails_closed(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    reference = store.publish(_blob_payload(b"required"), metadata=_metadata())
    path = store.path_for(reference.digest)
    path.write_bytes(b"corrupt")
    with pytest.raises(BlobIntegrityError, match="digest|length"):
        store.verify(reference)

    path.unlink()
    with pytest.raises(BlobIntegrityError, match="missing"):
        store.verify(reference)

    other = store.publish(_blob_payload(b"required"), metadata=_metadata())
    wrong = replace(
        other,
        media_type="application/octet-stream",
        payload_schema_version="strathmark-v3-other-v1",
        retention_class=BlobRetentionClass.ARCHIVABLE,
    )
    with pytest.raises(BlobIntegrityError, match="metadata"):
        store.verify(wrong)


def test_blob_public_boundary_and_corruption_guards_are_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.infrastructure.blobs as module

    for arguments, message in (
        (("text/plain", "strathmark-v3-payload-v1", BlobRetention.REQUIRED), "media"),
        (("application/json", "bad", BlobRetention.REQUIRED), "schema"),
        (("application/json", "strathmark-v3-payload-v1", "required"), "retention"),
    ):
        with pytest.raises(BlobStoreError, match=message):
            BlobMetadata(*arguments)  # type: ignore[arg-type]
    with pytest.raises(BlobStoreError, match="BlobReferenceV2"):
        BlobMetadata.from_reference(object())  # type: ignore[arg-type]
    with pytest.raises(BlobStoreError, match="filesystem"):
        ContentAddressedBlobStore(True)  # type: ignore[arg-type]
    with pytest.raises(BlobStoreError, match="create mode"):
        ContentAddressedBlobStore(tmp_path / "bad-mode", create="no")  # type: ignore[arg-type]
    missing_root = tmp_path / "missing-existing"
    with pytest.raises(BlobIntegrityError, match="root is missing"):
        ContentAddressedBlobStore(missing_root, create=False)
    assert not missing_root.exists()

    store = ContentAddressedBlobStore(tmp_path / "blobs")
    read_only = ContentAddressedBlobStore(store.root, create=False)
    with pytest.raises(BlobStoreError, match="read-only"):
        read_only.publish(_blob_payload(), metadata=_metadata())
    with pytest.raises(BlobStoreError, match="immutable bytes"):
        store.publish(bytearray(_blob_payload()), metadata=_metadata())  # type: ignore[arg-type]
    with pytest.raises(BlobStoreError, match="declared limit"):
        store.publish(b"x" * (module.MAX_BLOB_BYTES + 1), metadata=_metadata())
    with pytest.raises(BlobStoreError, match="metadata"):
        store.publish(_blob_payload(), metadata=object())  # type: ignore[arg-type]
    with pytest.raises(BlobStoreError, match="fault hook"):
        store.publish(_blob_payload(), metadata=_metadata(), fault_hook="bad")  # type: ignore[arg-type]
    with pytest.raises(BlobStoreError, match="StoredBlobReference"):
        store.verify(object())  # type: ignore[arg-type]
    with pytest.raises(BlobStoreError, match="immutable tuple"):
        store.verify_required([])  # type: ignore[arg-type]
    with pytest.raises(BlobStoreError, match="digest"):
        store.path_for("bad")
    with pytest.raises(BlobIntegrityError, match="cannot be read"):
        module._file_digest(tmp_path / "missing")

    reference = store.publish(_blob_payload(b"same-length"), metadata=_metadata())
    content = store.path_for(reference.digest)
    content.write_bytes(b"y" * reference.byte_count)
    with pytest.raises(BlobIntegrityError, match="digest"):
        store.verify(reference)

    # A complete orphan content file with no descriptor is checked before reuse.
    orphan_payload = _blob_payload(b"orphan")
    orphan_digest = __import__("hashlib").sha256(orphan_payload).hexdigest()
    orphan_path = store.path_for(orphan_digest)
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.write_bytes(b"z" * len(orphan_payload))
    with pytest.raises(BlobIntegrityError, match="existing"):
        store.publish(orphan_payload, metadata=_metadata())

    readable = store.publish(_blob_payload(b"metadata-read"), metadata=_metadata())
    metadata_path = store._metadata_path(readable)
    real_read = Path.read_bytes

    def fail_metadata(path: Path) -> bytes:
        if path == metadata_path:
            raise OSError("denied")
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", fail_metadata)
    with pytest.raises(BlobIntegrityError, match="metadata cannot be read"):
        store.verify(readable)
    monkeypatch.undo()
    metadata_path.write_bytes(b"{}")
    with pytest.raises(BlobIntegrityError, match="metadata differs"):
        store.verify(readable)
    metadata_path.write_bytes(canonical_bytes(readable.to_dict()))
    assert store.verify_required((readable,)) == (readable,)


def test_posix_blob_durability_helpers_have_separate_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.infrastructure.blobs as module

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"x")
    monkeypatch.setattr(module.os, "name", "posix")
    module._atomic_replace(source, destination)
    assert destination.read_bytes() == b"x"

    observed: list[str] = []
    real_fsync_directory = module._fsync_directory
    monkeypatch.setattr(module, "_fsync_directory", lambda _path: observed.append("fsync"))
    module._finish_rename_durability(
        tmp_path,
        fault_hook=observed.append,
        rename_stage="rename",
        directory_stage="directory",
        windows_stage="windows",
    )
    assert observed == ["rename", "fsync", "directory"]

    descriptor_calls: list[object] = []
    monkeypatch.setattr(module.os, "open", lambda *_args: 17)
    monkeypatch.setattr(module.os, "fsync", descriptor_calls.append)
    monkeypatch.setattr(module.os, "close", descriptor_calls.append)
    real_fsync_directory(tmp_path)
    assert descriptor_calls == [17, 17]


def test_windows_blob_rename_failure_is_not_claimed_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ctypes

    import strathmark.v3.infrastructure.blobs as module

    class MoveFile:
        argtypes = None
        restype = None

        def __call__(self, *_args: object) -> int:
            return 0

    class Kernel:
        MoveFileExW = MoveFile()

    source = tmp_path / "source"
    source.write_bytes(b"x")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: Kernel())
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    with pytest.raises(BlobStoreError, match="rename failed"):
        module._atomic_replace(source, tmp_path / "destination")
