from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
)
from strathmark.v3.infrastructure.rolling_head_registry import (
    ExternalRollingHeadRegistry,
    RollingHeadRegistryError,
)
from strathmark.v3.infrastructure.sqlite.connection import (
    open_v3_connection,
)
from strathmark.v3.infrastructure.sqlite.jobs import DurableJobRepository
from tests.v3.integration.test_rolling_restart_capacity import (
    _capacity,
    _job_request,
)

NOW = "2026-08-24T19:00:00.000Z"


def _registry(
    root: Path,
    signer: P256EphemeralSigner,
    *,
    trust: IntegrityTrustStore | None = None,
    refresh_threshold: int = 2,
    max_elapsed_ms: int = 300_000,
) -> ExternalRollingHeadRegistry:
    return ExternalRollingHeadRegistry(
        root,
        bootstrap_identity=signer.identity,
        trust_store=trust or IntegrityTrustStore((signer.identity,)),
        active_identity=signer.identity,
        signer=signer,
        refresh_threshold=refresh_threshold,
        max_elapsed_ms=max_elapsed_ms,
    )


def _open(
    registry: ExternalRollingHeadRegistry,
    database: Path,
    signer: P256EphemeralSigner,
):
    return registry.open_repository(
        database,
        capacity=_capacity(),
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
        published_at=NOW,
    )


def _advance_local(repository, count: int) -> None:
    with open_v3_connection(repository.database_path, read_only=True) as connection:
        start = int(connection.execute("SELECT COUNT(*) FROM v3_job_specs").fetchone()[0])
    for offset in range(1, count + 1):
        repository.enqueue(_job_request(start + offset))


def test_registry_bootstraps_once_and_supplies_external_head_automatically(
    tmp_path: Path,
) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:rolling-head-bootstrap")
    registry = _registry(tmp_path / "external", signer)
    database = tmp_path / "rolling.sqlite3"
    repository = _open(registry, database, signer)
    assert registry.record_count == 1
    assert registry.latest_expected_head is not None
    assert repository.recover_rolling_restart().checkpoint_sequence == 1

    restarted = _open(registry, database, signer)
    assert restarted.recover_rolling_restart().checkpoint_sequence == 1
    assert registry.record_count == 1


def test_registry_rejects_coherent_local_rollback_to_valid_older_head(
    tmp_path: Path,
) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:rolling-head-rollback")
    registry = _registry(tmp_path / "external", signer, refresh_threshold=1)
    database = tmp_path / "rolling.sqlite3"
    repository = _open(registry, database, signer)
    backup = tmp_path / "older.sqlite3"
    shutil.copy2(database, backup)
    _advance_local(repository, 1)
    assert (
        registry.refresh_if_due(
            database,
            capacity=_capacity(),
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity,)),
            published_at=NOW,
        )
        is not None
    )
    shutil.copy2(backup, database)
    with pytest.raises(RollingHeadRegistryError, match="local rolling head rolled back"):
        _open(registry, database, signer)


def test_registry_ignores_partial_temp_and_retries_external_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:rolling-head-crash")
    registry = _registry(tmp_path / "external", signer, refresh_threshold=1)
    database = tmp_path / "rolling.sqlite3"
    repository = _open(registry, database, signer)
    _advance_local(repository, 1)

    import strathmark.v3.infrastructure.rolling_head_registry as registry_module

    original = registry_module.os.link

    def crash_before_publish(_source: Path, _destination: Path) -> None:
        raise OSError("injected external publication crash")

    monkeypatch.setattr(registry_module.os, "link", crash_before_publish)
    with pytest.raises(RollingHeadRegistryError, match="cannot be published"):
        registry.refresh_if_due(
            database,
            capacity=_capacity(),
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity,)),
            published_at=NOW,
        )
    assert registry.record_count == 1
    monkeypatch.setattr(registry_module.os, "link", original)
    assert (
        registry.refresh_if_due(
            database,
            capacity=_capacity(),
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity,)),
            published_at=NOW,
        )
        is not None
    )
    assert registry.record_count == 2


def test_periodic_refresh_keeps_long_running_suffix_bounded(tmp_path: Path) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:rolling-head-periodic")
    registry = _registry(tmp_path / "external", signer, refresh_threshold=2)
    database = tmp_path / "rolling.sqlite3"
    repository = _open(registry, database, signer)
    for _ in range(6):
        _advance_local(repository, 2)
        refreshed = registry.refresh_if_due(
            database,
            capacity=_capacity(),
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity,)),
            published_at=NOW,
        )
        assert refreshed is not None
    started = time.perf_counter()
    restarted = _open(registry, database, signer)
    assert time.perf_counter() - started < 5
    assert restarted.recover_rolling_restart().checkpoint_sequence == 7
    assert registry.record_count == 7


def test_registry_detects_external_record_gap_and_active_key_mismatch(
    tmp_path: Path,
) -> None:
    old = P256EphemeralSigner.generate("integrity-key:rolling-head-old")
    new = P256EphemeralSigner.generate("integrity-key:rolling-head-new")
    root = tmp_path / "external"
    registry = _registry(root, old, refresh_threshold=1)
    database = tmp_path / "rolling.sqlite3"
    repository = _open(registry, database, old)
    _advance_local(repository, 1)
    registry.refresh_if_due(
        database,
        capacity=_capacity(),
        signer=old,
        trust_store=IntegrityTrustStore((old.identity,)),
        published_at=NOW,
    )
    (root / "heads" / "0000000000000001.json").unlink()
    with pytest.raises(RollingHeadRegistryError, match="sequence gap"):
        _registry(root, old)

    clean = tmp_path / "active-key"
    with pytest.raises(RollingHeadRegistryError, match="active signer identity"):
        ExternalRollingHeadRegistry(
            clean,
            bootstrap_identity=old.identity,
            trust_store=IntegrityTrustStore((old.identity, new.identity)),
            active_identity=new.identity,
            signer=old,
            refresh_threshold=2,
        )


def test_registry_refreshes_elapsed_rpo_before_delta_threshold(tmp_path: Path) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:rolling-head-time-rpo")
    registry = _registry(
        tmp_path / "external",
        signer,
        refresh_threshold=48,
        max_elapsed_ms=1_000,
    )
    database = tmp_path / "rolling.sqlite3"
    repository = _open(registry, database, signer)
    _advance_local(repository, 1)
    refreshed = registry.refresh_if_due(
        database,
        capacity=_capacity(),
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
        published_at=NOW,
    )
    assert refreshed is not None
    assert refreshed.rolling_checkpoint_sequence == 2


def test_registry_rejects_local_checkpoint_fork_at_retained_sequence(
    tmp_path: Path,
) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:rolling-head-fork")
    registry = _registry(tmp_path / "external", signer, refresh_threshold=1)
    database_a = tmp_path / "rolling-a.sqlite3"
    repository_a = _open(registry, database_a, signer)
    database_b = tmp_path / "rolling-b.sqlite3"
    shutil.copy2(database_a, database_b)
    repository_b = _open(registry, database_b, signer)
    _advance_local(repository_a, 1)
    _advance_local(repository_b, 2)
    repository_a.refresh_rolling_restart_checkpoint_if_due(observed_at=NOW, delta_threshold=1)
    repository_b.refresh_rolling_restart_checkpoint_if_due(observed_at=NOW, delta_threshold=1)
    assert (
        registry.refresh_if_due(
            database_a,
            capacity=_capacity(),
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity,)),
            published_at=NOW,
        )
        is not None
    )
    with pytest.raises(RollingHeadRegistryError, match="cannot reconcile to the retained head"):
        _open(registry, database_b, signer)


def test_registry_rotates_active_signer_without_rejecting_historical_records(
    tmp_path: Path,
) -> None:
    old = P256EphemeralSigner.generate("integrity-key:rolling-head-rotate-old")
    new = P256EphemeralSigner.generate("integrity-key:rolling-head-rotate-new")
    root = tmp_path / "external"
    old_registry = _registry(root, old, refresh_threshold=1)
    database = tmp_path / "rolling.sqlite3"
    repository = _open(old_registry, database, old)
    _advance_local(repository, 1)
    old_registry.refresh_if_due(
        database,
        capacity=_capacity(),
        signer=old,
        trust_store=IntegrityTrustStore((old.identity,)),
        published_at=NOW,
    )

    combined_trust = IntegrityTrustStore((old.identity, new.identity))
    rotated = ExternalRollingHeadRegistry(
        root,
        bootstrap_identity=old.identity,
        trust_store=combined_trust,
        active_identity=new.identity,
        signer=new,
        refresh_threshold=1,
    )
    repository = rotated.open_repository(
        database,
        capacity=_capacity(),
        signer=old,
        trust_store=combined_trust,
        published_at=NOW,
    )
    _advance_local(repository, 1)
    record = rotated.refresh_if_due(
        database,
        capacity=_capacity(),
        signer=old,
        trust_store=combined_trust,
        published_at=NOW,
    )
    assert record is not None
    assert record.manifest.key_id == new.identity.key_id
    assert rotated.record_count == 3


def test_registry_no_clobber_publish_rejects_stale_conflicting_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:rolling-head-no-clobber")
    root = tmp_path / "external"
    winner = _registry(root, signer, refresh_threshold=1)
    stale = _registry(root, signer, refresh_threshold=1)
    database_a = tmp_path / "winner.sqlite3"
    _open(winner, database_a, signer)

    database_b = tmp_path / "conflict.sqlite3"
    repository_b = DurableJobRepository(
        database_b,
        capacity=_capacity(),
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    _advance_local(repository_b, 1)
    assert (
        repository_b.refresh_rolling_restart_checkpoint_if_due(observed_at=NOW, delta_threshold=1)
        is not None
    )

    import strathmark.v3.infrastructure.rolling_head_registry as registry_module

    monkeypatch.setattr(
        registry_module.os,
        "rename",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("overwrite-capable rename must not publish anchors")
        ),
    )
    with pytest.raises(RollingHeadRegistryError, match="sequence already binds different material"):
        _open(stale, database_b, signer)
    reloaded = _registry(root, signer, refresh_threshold=1)
    assert reloaded.record_count == 1
    assert reloaded.latest_expected_head == winner.latest_expected_head


def test_registry_bootstrap_identity_crash_retries_without_partial_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:rolling-bootstrap-crash")
    root = tmp_path / "external"
    import strathmark.v3.infrastructure.rolling_head_registry as registry_module

    original = registry_module.os.link
    monkeypatch.setattr(
        registry_module.os,
        "link",
        lambda *_args: (_ for _ in ()).throw(OSError("injected bootstrap crash")),
    )
    with pytest.raises(RollingHeadRegistryError, match="cannot be persisted"):
        _registry(root, signer)
    assert not (root / "bootstrap-key.json").exists()
    monkeypatch.setattr(registry_module.os, "link", original)
    assert _registry(root, signer).record_count == 0


def test_registry_bootstrap_identity_no_clobber_rejects_conflicting_writer(
    tmp_path: Path,
) -> None:
    first = P256EphemeralSigner.generate("integrity-key:rolling-bootstrap-first")
    conflict = P256EphemeralSigner.generate("integrity-key:rolling-bootstrap-conflict")
    root = tmp_path / "external"
    _registry(root, first)
    with pytest.raises(RollingHeadRegistryError, match="bootstrap key mismatch"):
        ExternalRollingHeadRegistry(
            root,
            bootstrap_identity=conflict.identity,
            trust_store=IntegrityTrustStore((first.identity, conflict.identity)),
            active_identity=conflict.identity,
            signer=conflict,
            refresh_threshold=2,
        )


def test_registry_pending_cleanup_failure_does_not_mask_durable_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:rolling-cleanup")
    original_unlink = Path.unlink

    def fail_pending_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if path.name.startswith(".pending-"):
            raise OSError("injected pending cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_pending_cleanup)
    root = tmp_path / "external"
    registry = _registry(root, signer)
    assert (root / "bootstrap-key.json").is_file()
    _open(registry, tmp_path / "rolling.sqlite3", signer)
    assert registry.record_count == 1


def test_registry_append_and_startup_latency_remain_bounded(tmp_path: Path) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:rolling-registry-latency")
    root = tmp_path / "external"
    registry = _registry(root, signer, refresh_threshold=1)
    database = tmp_path / "rolling.sqlite3"
    repository = _open(registry, database, signer)
    append_seconds: list[float] = []
    for _ in range(8):
        _advance_local(repository, 1)
        started = time.perf_counter()
        assert (
            registry.refresh_if_due(
                database,
                capacity=_capacity(),
                signer=signer,
                trust_store=IntegrityTrustStore((signer.identity,)),
                published_at=NOW,
            )
            is not None
        )
        append_seconds.append(time.perf_counter() - started)
    started = time.perf_counter()
    reloaded = _registry(root, signer, refresh_threshold=1)
    startup_seconds = time.perf_counter() - started
    metrics = {
        "external_append_p99_seconds": round(max(append_seconds), 3),
        "external_startup_seconds": round(startup_seconds, 3),
        "records": reloaded.record_count,
    }
    print(metrics)
    assert metrics["external_append_p99_seconds"] <= 1
    assert metrics["external_startup_seconds"] <= 1
