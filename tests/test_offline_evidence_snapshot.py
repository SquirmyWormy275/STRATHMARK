"""Durable, cloud-independent prior-history snapshot contract tests.

Every test uses a temporary SQLite path and an in-process source double.  Cloud
configuration is deliberately removed or poisoned; no network adapter is used.
"""

from __future__ import annotations

import gc
import json
import sqlite3
import time
import tracemalloc
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from strathmark.ledger import (
    LedgerConflictError,
    LedgerQueryTimeoutError,
    PredictionLedger,
    SQLiteQueryDeadline,
)
from strathmark.predictor import (
    CompetitorRecord,
    PredictionBundle,
    PredictionEngineProvider,
    WoodProfile,
)
from strathmark.shadow import (
    OBSERVATION_SCHEMA_VERSION,
    SHADOW_TARGET_SINGLE_ELAPSED,
    ShadowFieldRequest,
    ShadowPredictionService,
    derive_current_receipt_status,
)
from strathmark.store import (
    EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
    EvidenceSnapshotConflictError,
    EvidenceSnapshotIntegrityError,
    EvidenceSnapshotPayload,
    ResultStore,
    canonical_evidence_source_digest,
)

UTC = timezone.utc
CUTOFF = date(2026, 11, 2)
CAPTURED_AT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
WOOD = WoodProfile(species="Pine", diameter_mm=300, quality=5)


@pytest.fixture(autouse=True)
def _isolate_cloud(monkeypatch):
    import strathmark.store as store_module

    for name in (
        "STRATHMARK_SUPABASE_URL",
        "STRATHMARK_SUPABASE_KEY",
        "SUPABASE_URL",
        "SUPABASE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(store_module, "_utc_now", lambda: CAPTURED_AT)


def _row(
    competitor_id: str = "missoula:competitor:alice",
    *,
    result_date: object = "2026-10-01",
    time_seconds: object = 40.0,
    competition_id: str = "missoula:tournament:2026-final",
) -> dict[str, object]:
    return {
        "schema_version": "strathmark.evidence-history-row.v1",
        "competitor_id": competitor_id,
        "event_code": "SB",
        "time_seconds": time_seconds,
        "species": "Pine",
        "diameter_mm": 300,
        "quality": 5,
        "competition_id": competition_id,
        "heat_id": "missoula:heat:sb-1",
        "result_date": result_date,
    }


def _payload(
    rows,
    *,
    source_id: str = "missoula:history-export:2026-11-01",
    cutoff: date = CUTOFF,
    captured_at: datetime = CAPTURED_AT,
) -> EvidenceSnapshotPayload:
    material = tuple(rows)
    digest = canonical_evidence_source_digest(
        source_id=source_id,
        cutoff=cutoff,
        captured_at=captured_at,
        rows=material,
    )
    return EvidenceSnapshotPayload(
        schema_version=EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
        source_id=source_id,
        cutoff=cutoff,
        captured_at=captured_at,
        rows=material,
        source_digest=digest,
    )


class _Source:
    def __init__(self, payload: EvidenceSnapshotPayload, failure: Exception | None = None):
        self.payload = payload
        self.failure = failure
        self.calls = 0

    def load_snapshot(self, *, cutoff: date) -> EvidenceSnapshotPayload:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        assert cutoff == self.payload.cutoff
        return self.payload


class _ChangingSequence(Sequence):
    """Expose one source row on the first traversal and another on any replay."""

    def __init__(self, first, second, *, reported_length=1):
        self.first = first
        self.second = second
        self.reported_length = reported_length
        self.traversals = 0

    def __len__(self):
        return self.reported_length

    def __getitem__(self, index):
        if index != 0:
            raise IndexError
        self.traversals += 1
        return self.first if self.traversals == 1 else self.second


def test_refresh_freezes_adapter_rows_once_before_digest_and_cardinality(tmp_path, monkeypatch):
    import strathmark.store as store_module

    first = _row(time_seconds=40.0)
    source_rows = _ChangingSequence(first, _row(time_seconds=99.0))
    payload = EvidenceSnapshotPayload(
        schema_version=EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
        source_id="missoula:history-export:single-pass",
        cutoff=CUTOFF,
        captured_at=CAPTURED_AT,
        rows=source_rows,
        source_digest=canonical_evidence_source_digest(
            source_id="missoula:history-export:single-pass",
            cutoff=CUTOFF,
            captured_at=CAPTURED_AT,
            rows=(first,),
        ),
    )
    store = ResultStore(tmp_path / "single-pass.db")
    store.refresh_evidence_snapshot(_Source(payload), cutoff=CUTOFF)
    assert source_rows.traversals == 1
    assert store.get_evidence_history("missoula:competitor:alice", "SB")[0].time_seconds == 40.0

    monkeypatch.setattr(store_module, "MAX_EVIDENCE_SNAPSHOT_ROWS", 0)
    lying_rows = _ChangingSequence(first, first, reported_length=0)
    lying_payload = replace(payload, rows=lying_rows)
    with pytest.raises(ValueError, match="rows"):
        ResultStore(tmp_path / "lying-cardinality.db").refresh_evidence_snapshot(
            _Source(lying_payload), cutoff=CUTOFF
        )
    assert lying_rows.traversals == 1


def test_refresh_attests_full_partial_and_explicit_empty_snapshots(tmp_path):
    store = ResultStore(tmp_path / "snapshots.db")

    full = store.refresh_evidence_snapshot(_Source(_payload([_row()])), cutoff=CUTOFF)
    assert full.completeness == "full"
    assert full.accepted_row_count == 1
    assert full.rejected_row_count == 0
    assert full.integrity == "verified"
    assert full.ready_for_offline is True

    partial_rows = [
        _row(),
        _row("missoula:competitor:bad-time", time_seconds="not-a-number"),
        _row("missoula:competitor:undated", result_date=None),
        _row("missoula:competitor:on-cutoff", result_date=CUTOFF.isoformat()),
        _row("missoula:competitor:future", result_date="2026-11-01"),
    ]
    partial_payload = _payload(
        partial_rows,
        source_id="missoula:history-export:partial",
        captured_at=datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
    )
    partial = store.refresh_evidence_snapshot(
        _Source(partial_payload),
        cutoff=CUTOFF,
        expected_active_snapshot_digest=full.snapshot_digest,
    )
    assert partial.completeness == "partial"
    assert partial.accepted_row_count == 1
    assert partial.rejected_row_count == 4
    assert partial.diagnostics == {
        "future_result_date": 1,
        "invalid_numeric": 1,
        "on_or_after_cutoff": 1,
        "undated": 1,
    }
    assert store.get_evidence_history("missoula:competitor:bad-time", "SB") == []

    empty = store.refresh_evidence_snapshot(
        _Source(_payload([], source_id="missoula:history-export:empty")),
        cutoff=CUTOFF,
        expected_active_snapshot_digest=partial.snapshot_digest,
    )
    assert empty.completeness == "empty"
    assert empty.accepted_row_count == 0
    assert empty.rejected_row_count == 0
    assert empty.ready_for_offline is True


def test_refresh_is_atomic_and_retains_prior_snapshot_on_adapter_or_digest_failure(tmp_path):
    store = ResultStore(tmp_path / "snapshots.db")
    first = store.refresh_evidence_snapshot(_Source(_payload([_row()])), cutoff=CUTOFF)

    broken_source = _Source(_payload([_row()]), failure=RuntimeError("source unavailable"))
    with pytest.raises(RuntimeError, match="source unavailable"):
        store.refresh_evidence_snapshot(broken_source, cutoff=CUTOFF)
    assert store.get_evidence_snapshot_status(as_of=CAPTURED_AT).snapshot_digest == (
        first.snapshot_digest
    )

    mismatched = replace(
        _payload([_row()], source_id="missoula:history-export:mismatch"),
        source_digest="0" * 64,
    )
    with pytest.raises(EvidenceSnapshotIntegrityError, match="source digest"):
        store.refresh_evidence_snapshot(_Source(mismatched), cutoff=CUTOFF)
    assert store.get_evidence_snapshot_status(as_of=CAPTURED_AT).snapshot_digest == (
        first.snapshot_digest
    )


def test_snapshot_survives_restart_reports_age_and_detects_stored_digest_mismatch(tmp_path):
    path = tmp_path / "snapshots.db"
    first = ResultStore(path)
    refreshed = first.refresh_evidence_snapshot(_Source(_payload([_row()])), cutoff=CUTOFF)

    restarted = ResultStore(path)
    current = restarted.get_evidence_snapshot_status(
        as_of=datetime(2026, 11, 3, 12, 0, tzinfo=UTC), max_age_days=3
    )
    assert current.snapshot_digest == refreshed.snapshot_digest
    assert current.age_days == 2
    assert current.freshness == "current"
    stale = restarted.get_evidence_snapshot_status(
        as_of=datetime(2026, 11, 10, 12, 0, tzinfo=UTC), max_age_days=3
    )
    assert stale.age_days == 9
    assert stale.freshness == "stale"
    assert stale.ready_for_offline is False

    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER evidence_snapshot_rows_no_update")
        conn.execute("UPDATE evidence_snapshot_rows SET time_seconds = 41.0")
        conn.commit()
    with pytest.raises(EvidenceSnapshotIntegrityError, match="digest"):
        ResultStore(path).get_evidence_snapshot_status(as_of=CAPTURED_AT)


class _OfflineProvider(PredictionEngineProvider):
    def __init__(self):
        self.calls = 0

    def snapshot(self, prediction_as_of: date) -> PredictionBundle:
        assert prediction_as_of == CUTOFF
        self.calls += 1
        return PredictionBundle(
            source="offline-test", warnings=("broad_prior_only",), degraded=True
        )


def _request(request_id: str, run_revision: str) -> ShadowFieldRequest:
    return ShadowFieldRequest(
        consumer_id="missoula:service:shadow",
        tournament_id="missoula:tournament:2027",
        event_occurrence_id="missoula:event:225-sb",
        field_run_id="missoula:field-run:1",
        operator_id="missoula:operator:7",
        request_id=request_id,
        run_revision=run_revision,
        event_code="SB",
        target_contract=SHADOW_TARGET_SINGLE_ELAPSED,
        prediction_as_of=CUTOFF,
        schedule_fingerprint="1" * 64,
        observation_schema_version=OBSERVATION_SCHEMA_VERSION,
        observation_fingerprint="2" * 64,
    )


def test_shadow_calculation_uses_only_active_local_snapshot_and_freezes_attestation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("STRATHMARK_SUPABASE_URL", "https://must-not-be-used.invalid")
    monkeypatch.setenv("STRATHMARK_SUPABASE_KEY", "must-not-be-used")
    path = tmp_path / "offline.db"
    store = ResultStore(path)
    source = _Source(
        _payload(
            [
                _row(),
                _row(
                    "missoula:competitor:bob",
                    result_date="2026-09-15",
                    time_seconds=44.0,
                ),
            ]
        )
    )
    status = store.refresh_evidence_snapshot(source, cutoff=CUTOFF)
    verification_calls = 0
    real_status = store.get_evidence_snapshot_status

    def counted_status(*args, **kwargs):
        nonlocal verification_calls
        verification_calls += 1
        return real_status(*args, **kwargs)

    monkeypatch.setattr(store, "get_evidence_snapshot_status", counted_status)
    refreshed_payload = _payload(
        [
            _row(),
            _row(
                "missoula:competitor:bob",
                result_date="2026-09-15",
                time_seconds=44.0,
            ),
            _row(
                "missoula:competitor:alice",
                result_date="2026-10-15",
                time_seconds=41.0,
                competition_id="missoula:tournament:2026-refresh",
            ),
        ],
        source_id="missoula:history-export:concurrent-refresh",
    )

    class RefreshAfterPersistLedger(PredictionLedger):
        def record_field(self, *args, **kwargs):
            recorded = super().record_field(*args, **kwargs)
            store.refresh_evidence_snapshot(
                _Source(refreshed_payload),
                cutoff=CUTOFF,
                expected_active_snapshot_digest=status.snapshot_digest,
            )
            return recorded

    provider = _OfflineProvider()
    service = ShadowPredictionService(
        RefreshAfterPersistLedger(path),
        prediction_provider=provider,
        result_store=store,
    )
    caller_supplied = [
        CompetitorRecord(
            name="must-not-persist",
            competitor_id="missoula:competitor:alice",
            history=[],
            gender="F",
        ),
        CompetitorRecord(
            name="also-must-not-persist",
            competitor_id="missoula:competitor:bob",
            history=[],
            gender="M",
        ),
    ]

    calculated = service.calculate(
        _request("missoula:request:offline-a", "missoula:run-revision:offline-a"),
        caller_supplied,
        WOOD,
    )
    assert source.calls == 1
    assert provider.calls == 1
    # Selection and the post-persist recheck are calculation-owned. The explicit
    # concurrent operator refresh performs its own independent verification.
    assert verification_calls == 3
    assert calculated.receipt is not None
    assert calculated.status.freshness == "stale"
    assert calculated.status.ready_for_review is False
    frozen = calculated.receipt.core["evidence_snapshot"]
    assert frozen["snapshot_digest"] == status.snapshot_digest
    assert frozen["source_id"] == "missoula:history-export:2026-11-01"
    assert frozen["cutoff"] == CUTOFF.isoformat()
    assert frozen["accepted_row_count"] == 2
    assert (
        calculated.receipt.core["active_input"]["evidence_snapshot"]["snapshot_digest"]
        == status.snapshot_digest
    )
    assert calculated.receipt.core["calculation_input"]["competitors"][0]["history"]
    replay = ShadowPredictionService(
        PredictionLedger(path),
        prediction_provider=_OfflineProvider(),
        result_store=ResultStore(path),
    ).calculate(
        _request("missoula:request:offline-a", "missoula:run-revision:offline-a"),
        caller_supplied,
        WOOD,
    )
    assert replay.receipt.core_json == calculated.receipt.core_json


def test_verified_selection_cas_rejects_refresh_before_hydration(tmp_path):
    path = tmp_path / "selection-cas.db"
    store = ResultStore(path)
    first = store.refresh_evidence_snapshot(_Source(_payload([_row()])), cutoff=CUTOFF)
    selection = store.load_evidence_for_competitors(["missoula:competitor:alice"])
    assert selection is not None
    store.refresh_evidence_snapshot(
        _Source(
            _payload(
                [_row(time_seconds=41.0)],
                source_id="missoula:history-export:selection-cas-refresh",
            )
        ),
        cutoff=CUTOFF,
        expected_active_snapshot_digest=first.snapshot_digest,
    )
    service = ShadowPredictionService(
        PredictionLedger(path),
        prediction_provider=_OfflineProvider(),
        result_store=store,
    )

    with pytest.raises(EvidenceSnapshotConflictError, match="changed after verification"):
        service.calculate(
            _request("missoula:request:selection-cas", "missoula:run-revision:selection-cas"),
            [
                CompetitorRecord(
                    name="ignored",
                    competitor_id="missoula:competitor:alice",
                    history=[],
                )
            ],
            WOOD,
            evidence_selection=selection,
        )


def test_verified_selection_uses_tip_cas_without_second_pre_hydration_full_scan(
    tmp_path, monkeypatch
):
    path = tmp_path / "selection-single-verification.db"
    store = ResultStore(path)
    store.refresh_evidence_snapshot(_Source(_payload([_row()])), cutoff=CUTOFF)
    selection = store.load_evidence_for_competitors(["missoula:competitor:alice"])
    assert selection is not None
    full_status_calls = 0
    real_status = store.get_evidence_snapshot_status

    def counted_status(*args, **kwargs):
        nonlocal full_status_calls
        full_status_calls += 1
        return real_status(*args, **kwargs)

    monkeypatch.setattr(store, "get_evidence_snapshot_status", counted_status)
    result = ShadowPredictionService(
        PredictionLedger(path),
        prediction_provider=_OfflineProvider(),
        result_store=store,
    ).calculate(
        _request(
            "missoula:request:selection-single-verification",
            "missoula:run-revision:selection-single-verification",
        ),
        [
            CompetitorRecord(
                name="ignored",
                competitor_id="missoula:competitor:alice",
                history=[],
            )
        ],
        WOOD,
        evidence_selection=selection,
    )

    assert result.receipt is not None
    assert full_status_calls == 1


def test_explicit_refresh_changes_active_fingerprint_and_requires_superseding_request(tmp_path):
    path = tmp_path / "offline.db"
    store = ResultStore(path)
    first_status = store.refresh_evidence_snapshot(_Source(_payload([_row()])), cutoff=CUTOFF)
    service = ShadowPredictionService(
        PredictionLedger(path), prediction_provider=_OfflineProvider(), result_store=store
    )
    first = service.calculate(
        _request("missoula:request:a", "missoula:run-revision:a"),
        [CompetitorRecord(name="local", competitor_id="missoula:competitor:alice")],
        WOOD,
    )

    second_payload = _payload(
        [_row(), _row(result_date="2026-10-20", time_seconds=39.0)],
        source_id="missoula:history-export:refresh-2",
    )
    second_status = store.refresh_evidence_snapshot(
        _Source(second_payload),
        cutoff=CUTOFF,
        expected_active_snapshot_digest=first_status.snapshot_digest,
    )
    second = service.calculate(
        _request("missoula:request:b", "missoula:run-revision:b"),
        [CompetitorRecord(name="local", competitor_id="missoula:competitor:alice")],
        WOOD,
    )

    assert second_status.snapshot_digest != first_status.snapshot_digest
    assert (
        first.receipt.core["active_input"]["fingerprint"]
        != (second.receipt.core["active_input"]["fingerprint"])
    )
    assert second.receipt.core["evidence_snapshot"]["supersedes_snapshot_digest"] == (
        first_status.snapshot_digest
    )
    assert second.receipt.core["evidence_snapshot"]["accepted_row_count"] == 2


def test_snapshot_cutoff_must_match_field_cutoff_and_same_day_rows_never_enter_history(tmp_path):
    path = tmp_path / "offline.db"
    store = ResultStore(path)
    payload = _payload([_row(), _row(result_date=CUTOFF.isoformat(), time_seconds=39.0)])
    store.refresh_evidence_snapshot(_Source(payload), cutoff=CUTOFF)
    assert len(store.get_evidence_history("missoula:competitor:alice", "SB")) == 1

    service = ShadowPredictionService(
        PredictionLedger(path), prediction_provider=_OfflineProvider(), result_store=store
    )
    with pytest.raises(ValueError, match="snapshot cutoff"):
        service.calculate(
            replace(
                _request("missoula:request:wrong-cutoff", "missoula:run-revision:wrong"),
                prediction_as_of=date(2026, 11, 3),
            ),
            [CompetitorRecord(name="local", competitor_id="missoula:competitor:alice")],
            WOOD,
        )


def test_existing_receipt_recovers_before_missing_tampered_or_refreshed_snapshot(
    tmp_path, monkeypatch
):
    path = tmp_path / "recovery-first.db"
    store = ResultStore(path)
    first_status = store.refresh_evidence_snapshot(_Source(_payload([_row()])), cutoff=CUTOFF)
    original_provider = _OfflineProvider()
    original_service = ShadowPredictionService(
        PredictionLedger(path), result_store=store, prediction_provider=original_provider
    )
    request = _request("missoula:request:recovery", "missoula:run-revision:recovery")
    original = original_service.calculate(
        request,
        [
            CompetitorRecord(
                name="caller-name-must-not-persist",
                competitor_id="missoula:competitor:alice",
                history=[],
                gender="F",
            )
        ],
        WOOD,
    )
    assert original.receipt is not None
    projection = original.receipt.core["request_projection"]
    assert projection["fingerprint"]
    assert "history" not in str(projection).lower()
    assert "caller-name-must-not-persist" not in original.receipt.core_json

    changed_cutoff = date(2026, 11, 3)
    changed_payload = _payload(
        [_row()],
        source_id="missoula:history-export:changed-cutoff",
        cutoff=changed_cutoff,
    )
    changed_status = store.refresh_evidence_snapshot(
        _Source(changed_payload),
        cutoff=changed_cutoff,
        expected_active_snapshot_digest=first_status.snapshot_digest,
    )
    refreshed_provider = _OfflineProvider()
    refreshed = ShadowPredictionService(
        PredictionLedger(path), result_store=store, prediction_provider=refreshed_provider
    ).calculate(
        request,
        [
            CompetitorRecord(
                name="different-caller-name",
                competitor_id="missoula:competitor:alice",
                history=[_historical_result(179.0)],
                gender="F",
            )
        ],
        WOOD,
    )
    assert refreshed_provider.calls == 0
    assert refreshed.receipt.core_json == original.receipt.core_json
    assert refreshed.status.freshness == "stale"
    assert refreshed.receipt.status == refreshed.status
    assert refreshed.status.ready_for_review is False

    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER evidence_snapshot_rows_no_update")
        conn.execute(
            "UPDATE evidence_snapshot_rows SET time_seconds = 41.0 WHERE snapshot_digest = ?",
            (changed_status.snapshot_digest,),
        )
        conn.commit()

    replay_provider = _OfflineProvider()
    replay = ShadowPredictionService(
        PredictionLedger(path),
        result_store=ResultStore(path),
        prediction_provider=replay_provider,
    ).calculate(
        request,
        [
            CompetitorRecord(
                name="different-caller-name",
                competitor_id="missoula:competitor:alice",
                history=[_historical_result(179.0)],
                gender="F",
            )
        ],
        WOOD,
    )
    assert replay_provider.calls == 0
    assert replay.receipt.core_json == original.receipt.core_json
    assert replay.status.freshness == "invalid"
    assert replay.receipt.status == replay.status
    assert replay.status.ready_for_review is False

    corrupt_claimed_mismatch = derive_current_receipt_status(
        original.receipt,
        ResultStore(path),
        claimed_active_fingerprint="f" * 64,
    )
    assert corrupt_claimed_mismatch.status.freshness == "invalid"
    assert corrupt_claimed_mismatch.status.ready_for_review is False

    missing_provider = _OfflineProvider()
    missing = ShadowPredictionService(
        PredictionLedger(path),
        result_store=ResultStore(tmp_path / "missing-current.db"),
        prediction_provider=missing_provider,
    ).calculate(
        request,
        [CompetitorRecord(name="local", competitor_id="missoula:competitor:alice", gender="F")],
        WOOD,
    )
    assert missing_provider.calls == 0
    assert missing.receipt.core_json == original.receipt.core_json
    assert missing.status.freshness == "invalid"
    assert missing.status.ready_for_review is False

    claimed_mismatch = derive_current_receipt_status(
        original.receipt,
        ResultStore(tmp_path / "missing-claimed-current.db"),
        claimed_active_fingerprint="f" * 64,
    )
    assert claimed_mismatch.status.freshness == "invalid"
    assert claimed_mismatch.status.ready_for_review is False

    with pytest.raises(LedgerConflictError, match="request_id"):
        ShadowPredictionService(
            PredictionLedger(path),
            result_store=ResultStore(path),
            prediction_provider=_OfflineProvider(),
        ).calculate(
            request,
            [CompetitorRecord(name="local", competitor_id="missoula:competitor:alice")],
            replace(WOOD, diameter_mm=325),
        )


def test_snapshot_status_deadline_interrupts_high_cardinality_verification_and_recovers(
    tmp_path, monkeypatch
):
    import strathmark.store as store_module

    rows = [_row(competitor_id=f"missoula:competitor:deadline-{index}") for index in range(500)]
    store = ResultStore(tmp_path / "deadline-snapshot.db")
    store.refresh_evidence_snapshot(_Source(_payload(rows)), cutoff=CUTOFF)

    real_sha256 = store_module._sha256
    hash_calls = 0

    def slow_sha256(value):
        nonlocal hash_calls
        hash_calls += 1
        time.sleep(0.001)
        return real_sha256(value)

    monkeypatch.setattr(store_module, "_sha256", slow_sha256)
    with pytest.raises(LedgerQueryTimeoutError):
        store.get_evidence_snapshot_status(
            query_deadline=SQLiteQueryDeadline(timeout_seconds=0.025)
        )
    assert 0 < hash_calls < len(rows)

    monkeypatch.setattr(store_module, "_sha256", real_sha256)
    recovered = store.get_evidence_snapshot_status()
    assert recovered is not None
    assert recovered.accepted_row_count == len(rows)


def test_snapshot_status_streams_high_cardinality_rows_with_bounded_extra_memory(tmp_path):
    import strathmark.store as store_module

    row_count = 5_000
    rows = [
        _row(
            competitor_id=f"missoula:competitor:memory-{index}",
            competition_id=f"missoula:tournament:memory-{index}",
        )
        for index in range(row_count)
    ]
    store = ResultStore(tmp_path / "streaming-memory.db")
    status = store.refresh_evidence_snapshot(_Source(_payload(rows)), cutoff=CUTOFF)
    with store._connect() as conn:
        raw_canonical_json = str(
            conn.execute(
                "SELECT canonical_json FROM evidence_snapshots WHERE snapshot_digest = ?",
                (status.snapshot_digest,),
            ).fetchone()[0]
        )
        canonical_bytes = len(raw_canonical_json.encode("utf-8"))

    # Measure the steady-state verifier rather than one-time interpreter,
    # coverage, and SQLite statement-cache setup. The warmed path still walks
    # and hashes every row, so retaining a second O(n) projection remains
    # visible in the measured peak.
    warmed = store.get_evidence_snapshot_status()
    assert warmed is not None
    gc.collect()
    tracemalloc.start()
    parsed = json.loads(raw_canonical_json)
    rendered = store_module._canonical_json(parsed)
    _, canonical_roundtrip_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del parsed, rendered
    gc.collect()
    tracemalloc.start()
    verified = store.get_evidence_snapshot_status()
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert verified is not None
    assert verified.accepted_row_count == row_count
    # Normalize for the substantial JSON-object overhead differences between
    # supported Python releases. The verifier must stay within two canonical
    # payloads of the unavoidable parse-and-canonicalize baseline, which still
    # rejects retaining another complete row projection.
    assert peak_bytes < canonical_roundtrip_peak + canonical_bytes * 2


def _historical_result(time_seconds: float):
    from strathmark.predictor import HistoricalResult

    return HistoricalResult(
        event_code="SB",
        time_seconds=time_seconds,
        species="Pine",
        diameter_mm=300,
        quality=5,
        result_date=date(2026, 10, 1),
    )


def test_trusted_service_requires_result_store_and_caller_history_is_inactive(tmp_path):
    path = tmp_path / "required-store.db"
    with pytest.raises(TypeError):
        ShadowPredictionService(PredictionLedger(path), prediction_provider=_OfflineProvider())

    store = ResultStore(path)
    store.refresh_evidence_snapshot(_Source(_payload([_row()])), cutoff=CUTOFF)
    service = ShadowPredictionService(
        PredictionLedger(path), result_store=store, prediction_provider=_OfflineProvider()
    )
    clean = service.calculate(
        _request("missoula:request:history-a", "missoula:run-revision:history-a"),
        [CompetitorRecord(name="local", competitor_id="missoula:competitor:alice")],
        WOOD,
    )
    supplied = service.calculate(
        _request("missoula:request:history-b", "missoula:run-revision:history-b"),
        [
            CompetitorRecord(
                name="PII display name",
                competitor_id="missoula:competitor:alice",
                history=[_historical_result(179.0)],
            )
        ],
        WOOD,
    )
    assert (
        clean.receipt.core["request_projection"]["competitors"]
        == (supplied.receipt.core["request_projection"]["competitors"])
    )
    assert clean.receipt.core["calculation_input"] == supplied.receipt.core["calculation_input"]
    assert "PII display name" not in supplied.receipt.core_json
    assert "179.0" not in supplied.receipt.core_json


def test_future_capture_is_rejected_and_persisted_future_capture_fails_integrity(tmp_path):
    store = ResultStore(tmp_path / "future-capture.db")
    future_payload = _payload(
        [_row()],
        source_id="missoula:history-export:future-capture",
        captured_at=CAPTURED_AT + timedelta(minutes=6),
    )
    with pytest.raises(ValueError, match="captured_at.*future"):
        store.refresh_evidence_snapshot(_Source(future_payload), cutoff=CUTOFF)

    store.refresh_evidence_snapshot(_Source(_payload([_row()])), cutoff=CUTOFF)
    with pytest.raises(EvidenceSnapshotIntegrityError, match="future"):
        store.get_evidence_snapshot_status(as_of=CAPTURED_AT - timedelta(minutes=6))


def test_activation_chain_is_append_only_idempotent_and_cas_conflicts_roll_back(tmp_path):
    path = tmp_path / "activation-chain.db"
    store = ResultStore(path)
    first_source = _Source(_payload([_row()]))
    first = store.refresh_evidence_snapshot(first_source, cutoff=CUTOFF)
    assert first.activation_revision == 1
    assert first.previous_activation_id is None

    duplicate = store.refresh_evidence_snapshot(first_source, cutoff=CUTOFF)
    assert duplicate.activation_id == first.activation_id
    assert duplicate.activation_revision == 1

    second_payload = _payload(
        [_row(time_seconds=39.0)], source_id="missoula:history-export:cas-winner"
    )
    second = store.refresh_evidence_snapshot(
        _Source(second_payload),
        cutoff=CUTOFF,
        expected_active_snapshot_digest=first.snapshot_digest,
    )
    assert second.activation_revision == 2
    assert second.previous_activation_id == first.activation_id
    assert second.supersedes_snapshot_digest == first.snapshot_digest

    losing_payload = _payload(
        [_row(time_seconds=38.0)], source_id="missoula:history-export:cas-loser"
    )
    with pytest.raises(EvidenceSnapshotConflictError, match="active snapshot"):
        store.refresh_evidence_snapshot(
            _Source(losing_payload),
            cutoff=CUTOFF,
            expected_active_snapshot_digest=first.snapshot_digest,
        )
    current = store.get_evidence_snapshot_status(as_of=CAPTURED_AT)
    assert current.snapshot_digest == second.snapshot_digest
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence_snapshots").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM evidence_snapshot_activations").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE evidence_snapshot_activations SET revision = 99")


def test_stale_snapshot_blocks_new_calculation_but_not_exact_receipt_recovery(
    tmp_path, monkeypatch
):
    import strathmark.store as store_module

    path = tmp_path / "stale-new.db"
    store = ResultStore(path)
    store.refresh_evidence_snapshot(_Source(_payload([_row()])), cutoff=CUTOFF)
    service = ShadowPredictionService(
        PredictionLedger(path), result_store=store, prediction_provider=_OfflineProvider()
    )
    existing_request = _request("missoula:request:before-stale", "missoula:run-revision:before")
    existing = service.calculate(
        existing_request,
        [CompetitorRecord(name="local", competitor_id="missoula:competitor:alice")],
        WOOD,
    )

    monkeypatch.setattr(store_module, "_utc_now", lambda: CAPTURED_AT + timedelta(days=8))
    with pytest.raises(ValueError, match="stale|not ready"):
        service.calculate(
            _request("missoula:request:after-stale", "missoula:run-revision:after"),
            [CompetitorRecord(name="local", competitor_id="missoula:competitor:alice")],
            WOOD,
        )
    replay_provider = _OfflineProvider()
    replay = ShadowPredictionService(
        PredictionLedger(path), result_store=store, prediction_provider=replay_provider
    ).calculate(
        existing_request,
        [CompetitorRecord(name="local", competitor_id="missoula:competitor:alice")],
        WOOD,
    )
    assert replay_provider.calls == 0
    assert replay.receipt.core_json == existing.receipt.core_json
    assert replay.status.freshness == "stale"
    assert replay.receipt.status == replay.status
    assert replay.status.ready_for_review is False


def test_source_envelope_bounds_reject_hostile_shapes_before_persistence(tmp_path, monkeypatch):
    import strathmark.store as store_module

    payload = _payload([_row(), _row("missoula:competitor:bob")])
    monkeypatch.setattr(store_module, "MAX_EVIDENCE_SNAPSHOT_ROWS", 1)
    with pytest.raises(ValueError, match="rows"):
        ResultStore(tmp_path / "cardinality.db").refresh_evidence_snapshot(
            _Source(payload), cutoff=CUTOFF
        )

    monkeypatch.setattr(store_module, "MAX_EVIDENCE_SOURCE_BYTES", 64)
    with pytest.raises(ValueError, match="bytes"):
        canonical_evidence_source_digest(
            source_id="missoula:history-export:bytes",
            cutoff=CUTOFF,
            captured_at=CAPTURED_AT,
            rows=(),
        )
    monkeypatch.setattr(store_module, "MAX_EVIDENCE_SOURCE_BYTES", 32 * 1024 * 1024)

    with pytest.raises(ValueError, match="oversized string"):
        canonical_evidence_source_digest(
            source_id="missoula:history-export:string",
            cutoff=CUTOFF,
            captured_at=CAPTURED_AT,
            rows=({"value": "x" * 513},),
        )
    nested = {"a": {"b": {"c": {"d": {"e": "too-deep"}}}}}
    with pytest.raises(ValueError, match="nesting"):
        canonical_evidence_source_digest(
            source_id="missoula:history-export:nesting",
            cutoff=CUTOFF,
            captured_at=CAPTURED_AT,
            rows=(nested,),
        )
    with pytest.raises(ValueError, match="string keys"):
        canonical_evidence_source_digest(
            source_id="missoula:history-export:key",
            cutoff=CUTOFF,
            captured_at=CAPTURED_AT,
            rows=({1: "collision", "1": "must-not-overwrite"},),
        )
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="cycle"):
        canonical_evidence_source_digest(
            source_id="missoula:history-export:cycle",
            cutoff=CUTOFF,
            captured_at=CAPTURED_AT,
            rows=(cycle,),
        )
    monkeypatch.setattr(store_module, "MAX_EVIDENCE_SOURCE_NODES", 3)
    with pytest.raises(ValueError, match="nodes"):
        canonical_evidence_source_digest(
            source_id="missoula:history-export:nodes",
            cutoff=CUTOFF,
            captured_at=CAPTURED_AT,
            rows=({"a": 1, "b": 2},),
        )
