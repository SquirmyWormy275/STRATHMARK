"""Immutable whole-field shadow receipt contract tests.

Every test uses a temporary local SQLite ledger.  No network or production
database configuration is consulted.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from strathmark.calculator import HandicapCalculator, canonical_active_v2_request
from strathmark.ledger import LedgerConflictError, PredictionLedger
from strathmark.prediction_v2 import ForecastInterval, PredictiveDistribution
from strathmark.predictor import (
    CompetitorRecord,
    PredictionBundle,
    PredictionContext,
    PredictionEngineProvider,
    WoodProfile,
)
from strathmark.shadow import (
    ACTIVE_INPUT_SCHEMA_VERSION,
    OBSERVATION_SCHEMA_VERSION,
    RECEIPT_CORE_SCHEMA_VERSION,
    SHADOW_TARGET_SINGLE_ELAPSED,
    ShadowFieldRequest,
    ShadowPredictionService,
    ShadowReceiptCorruptionError,
)


class _Core:
    def __init__(self, version: str, median: float, digest: str, janka: float = 1690.0) -> None:
        self.model_version = version
        self.median = median
        self.source_checksum = digest
        self.janka = janka

        self.calibration = SimpleNamespace(version=f"cal-{version}")

    def artifact_fingerprint(self) -> str:
        return ("a" if self.model_version == "core-a" else "b") * 64

    def predict(self, request, *, history=None, wood_df=None):
        del request, history, wood_df
        return PredictiveDistribution(
            median=self.median,
            log_location=3.7,
            log_scale=0.2,
            interval=ForecastInterval(30.0, 55.0),
            source="hierarchical_dynamic_core",
            history_count=0,
            effective_history_weight=0.0,
            model_version=self.model_version,
            calibration_version=self.calibration.version,
        )

    def resolve_species_properties(self, species):
        del species
        return (
            {
                "janka_hardness": self.janka,
                "specific_gravity": 0.34,
                "crush_strength": 4000.0,
                "shear_strength": 1000.0,
                "modulus_of_rupture": 8000.0,
                "modulus_of_elasticity": 1_000_000.0,
            },
            False,
        )

    def species_property_frame(self):
        return None


class _Provider(PredictionEngineProvider):
    def __init__(self, version="core-a", median=42.0, digest="c" * 64, janka=1690.0):
        self.bundle = PredictionBundle(
            core=_Core(version, median, digest, janka), source=f"fixture-{version}"
        )
        self.calls = 0

    def snapshot(self, prediction_as_of):
        del prediction_as_of
        self.calls += 1
        return self.bundle


def _request(
    *,
    consumer_id: str = "missoula:service",
    request_id: str = "missoula:request-1",
    schedule_fingerprint: str = "1" * 64,
    observation_fingerprint: str = "2" * 64,
    target_contract: str = SHADOW_TARGET_SINGLE_ELAPSED,
) -> ShadowFieldRequest:
    return ShadowFieldRequest(
        consumer_id=consumer_id,
        tournament_id="missoula:tournament-2027",
        event_occurrence_id="missoula:event-225-sb",
        field_run_id="missoula:field-run-1",
        operator_id="missoula:operator-7",
        request_id=request_id,
        event_code="SB",
        target_contract=target_contract,
        prediction_as_of="2026-11-01T23:30:00-08:00",
        schedule_fingerprint=schedule_fingerprint,
        observation_schema_version=OBSERVATION_SCHEMA_VERSION,
        observation_fingerprint=observation_fingerprint,
    )


def _competitors(*, same_name: bool = False):
    return [
        CompetitorRecord(
            "Same Name" if same_name else "Alice",
            competitor_id="missoula-competitor:alice",
            gender="F",
        ),
        CompetitorRecord(
            "Same Name" if same_name else "Bob",
            competitor_id="missoula-competitor:bob",
            gender="M",
        ),
    ]


WOOD = WoodProfile(species="Pine", diameter_mm=300.0, quality=7)


def _wood_properties(janka: float) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "speciesid": "PINE",
                "janka_hardness": janka,
                "specific_gravity": 0.34,
                "crush_strength": 4000.0,
                "shear_strength": 1000.0,
                "modulus_of_rupture": 8000.0,
                "modulus_of_elasticity": 1_000_000.0,
            }
        ]
    )


@pytest.mark.parametrize(
    "competitor",
    [
        replace(_competitors()[0], manual_time_override=48.0),
        replace(_competitors()[0], tournament_time=47.0),
    ],
)
def test_trusted_shadow_rejects_manual_comparison_inputs_before_provider(tmp_path, competitor):
    provider = _Provider()
    field = [competitor, _competitors()[1]]

    with pytest.raises(ValueError, match="manual.*trusted shadow"):
        ShadowPredictionService(
            PredictionLedger(tmp_path / "shadow.db"), prediction_provider=provider
        ).calculate(_request(), field, WOOD)

    assert provider.calls == 0


def test_receipt_core_is_versioned_ordered_complete_and_separate_from_status(tmp_path):
    ledger = PredictionLedger(tmp_path / "shadow.db")
    result = ShadowPredictionService(ledger, prediction_provider=_Provider()).calculate(
        _request(), _competitors(same_name=True), WOOD
    )

    assert result.trusted is True
    assert result.receipt is not None
    core = result.receipt.core
    assert core["schema_version"] == RECEIPT_CORE_SCHEMA_VERSION
    assert core["active_input"]["schema_version"] == ACTIVE_INPUT_SCHEMA_VERSION
    assert core["observation"]["schema_version"] == OBSERVATION_SCHEMA_VERSION
    assert [
        row["competitor_id"] for row in core["active_input"]["caller_input"]["competitors"]
    ] == [
        "missoula-competitor:alice",
        "missoula-competitor:bob",
    ]
    assert "display_name" not in core["active_input"]["caller_input"]["competitors"][0]
    assert {row["competitor_id"] for row in core["predictions"]} == {
        "missoula-competitor:alice",
        "missoula-competitor:bob",
    }
    assert all(row["prediction_id"] for row in core["predictions"])
    assert all(row["versions"]["engine"] == "2.0.0" for row in core["predictions"])
    assert all("optimizer_metadata" in row for row in core["predictions"])
    assert all("ignored_factors" in row for row in core["predictions"])
    assert core["artifact"]["source_digest"] == "c" * 64
    assert core["artifact"]["artifact_digest"] == "a" * 64
    assert core["evidence_diagnostics"][0]["included_rows"] == 0
    assert "history" not in core["observation"]
    assert result.receipt.status.trust == "recorded"
    assert result.receipt.status.freshness == "current"
    assert result.receipt.status.ready_for_review is True
    assert "status" not in core


def test_receipt_separates_prelookup_input_from_bundle_resolved_calculation(tmp_path):
    provider = _Provider(janka=1777.0)
    result = ShadowPredictionService(
        PredictionLedger(tmp_path / "shadow.db"), prediction_provider=provider
    ).calculate(_request(), _competitors(), WOOD)

    assert result.receipt is not None
    core = result.receipt.core
    assert core["active_input"]["caller_input"]["wood_properties"]["janka_hardness"] == 1690.0
    assert core["calculation_input"]["wood_properties"]["janka_hardness"] == 1777.0
    assert core["calculation_input"]["wood_properties"]["species_missing"] is False


def test_shared_calculator_projection_preserves_bundle_property_semantics():
    provider = _Provider(janka=1777.0)
    context = PredictionContext(
        prediction_as_of=date(2026, 11, 2),
        request_id="missoula:request-1",
        seed=20260811,
        engine="v2",
    )
    expected = canonical_active_v2_request(
        _competitors(),
        WOOD,
        "SB",
        context,
        prediction_bundle=provider.bundle,
    )
    actual = HandicapCalculator(prediction_provider=provider)._canonical_ledger_request(
        _competitors(),
        WOOD,
        "SB",
        context,
        prediction_bundle=provider.bundle,
    )

    assert actual == expected
    assert actual["wood_properties"]["janka_hardness"] == 1777.0
    assert actual["wood_properties"]["species_missing"] is False
    assert "competitors" in actual and "entrants" not in actual


def test_lookup_before_calculation_replays_exact_core_after_restart_and_artifact_change(
    tmp_path,
):
    path = tmp_path / "shadow.db"
    provider_a = _Provider("core-a", 42.0, "c" * 64)
    first = ShadowPredictionService(
        PredictionLedger(path), prediction_provider=provider_a
    ).calculate(_request(), _competitors(), WOOD)
    assert provider_a.calls == 1

    provider_b = _Provider("core-b", 99.0, "d" * 64)
    replay = ShadowPredictionService(
        PredictionLedger(path), prediction_provider=provider_b
    ).calculate(_request(), _competitors(), WOOD)

    assert provider_b.calls == 0
    assert replay.receipt is not None and first.receipt is not None
    assert replay.receipt.core_json == first.receipt.core_json
    assert replay.receipt.core["artifact"]["model_version"] == "core-a"
    assert [row["prediction_id"] for row in replay.receipt.core["predictions"]] == [
        row["prediction_id"] for row in first.receipt.core["predictions"]
    ]


def test_same_request_changed_active_payload_conflicts_before_calculation(tmp_path):
    path = tmp_path / "shadow.db"
    service = ShadowPredictionService(PredictionLedger(path), prediction_provider=_Provider())
    service.calculate(_request(), _competitors(), WOOD)
    changed_provider = _Provider()
    changed_service = ShadowPredictionService(
        PredictionLedger(path), prediction_provider=changed_provider
    )

    with pytest.raises(LedgerConflictError, match="different active input"):
        changed_service.calculate(_request(schedule_fingerprint="9" * 64), _competitors(), WOOD)

    assert changed_provider.calls == 0


def test_same_request_changed_event_ceiling_conflicts_before_calculation(tmp_path):
    path = tmp_path / "shadow.db"
    ShadowPredictionService(
        PredictionLedger(path), prediction_provider=_Provider(), event_ceiling=80
    ).calculate(_request(), _competitors(), WOOD)
    changed_provider = _Provider()

    with pytest.raises(LedgerConflictError, match="different active input"):
        ShadowPredictionService(
            PredictionLedger(path),
            prediction_provider=changed_provider,
            event_ceiling=81,
        ).calculate(_request(), _competitors(), WOOD)

    assert changed_provider.calls == 0


def test_same_request_changed_configured_wood_properties_conflicts_before_calculation(
    tmp_path,
):
    path = tmp_path / "shadow.db"
    ShadowPredictionService(
        PredictionLedger(path),
        prediction_provider=_Provider(),
        wood_df=_wood_properties(1690.0),
    ).calculate(_request(), _competitors(), WOOD)
    changed_provider = _Provider()

    with pytest.raises(LedgerConflictError, match="different active input"):
        ShadowPredictionService(
            PredictionLedger(path),
            prediction_provider=changed_provider,
            wood_df=_wood_properties(1700.0),
        ).calculate(_request(), _competitors(), WOOD)

    assert changed_provider.calls == 0


def test_same_request_observation_only_change_replays_exact_receipt(tmp_path):
    path = tmp_path / "shadow.db"
    first = ShadowPredictionService(
        PredictionLedger(path), prediction_provider=_Provider()
    ).calculate(_request(), _competitors(), WOOD)
    replay_provider = _Provider("core-b", 99.0, "d" * 64)
    replay = ShadowPredictionService(
        PredictionLedger(path), prediction_provider=replay_provider
    ).calculate(
        _request(observation_fingerprint="8" * 64),
        _competitors(),
        replace(WOOD, quality=2),
    )

    assert replay_provider.calls == 0
    assert replay.receipt is not None and first.receipt is not None
    assert replay.receipt.core_json == first.receipt.core_json


def test_same_request_id_is_isolated_by_namespaced_consumer(tmp_path):
    ledger = PredictionLedger(tmp_path / "shadow.db")
    service = ShadowPredictionService(ledger, prediction_provider=_Provider())
    first = service.calculate(_request(), _competitors(), WOOD)
    second = service.calculate(_request(consumer_id="another-system:service"), _competitors(), WOOD)

    assert first.receipt is not None and second.receipt is not None
    assert first.receipt.core["consumer_id"] != second.receipt.core["consumer_id"]
    assert first.receipt.core_json != second.receipt.core_json


def test_context_only_fingerprint_change_leaves_active_hash_and_output_unchanged(
    tmp_path,
):
    service = ShadowPredictionService(
        PredictionLedger(tmp_path / "shadow.db"), prediction_provider=_Provider()
    )
    first = service.calculate(_request(request_id="missoula:request-a"), _competitors(), WOOD)
    second = service.calculate(
        _request(
            request_id="missoula:request-b",
            observation_fingerprint="8" * 64,
        ),
        _competitors(),
        replace(WOOD, quality=2),
    )

    assert first.receipt is not None and second.receipt is not None
    assert (
        first.receipt.core["active_input"]["fingerprint"]
        == second.receipt.core["active_input"]["fingerprint"]
    )
    assert [row["median_seconds"] for row in first.receipt.core["predictions"]] == [
        row["median_seconds"] for row in second.receipt.core["predictions"]
    ]
    assert (
        first.receipt.core["observation"]["fingerprint"]
        != second.receipt.core["observation"]["fingerprint"]
    )


def test_explicit_exclusive_utc_cutoff_is_frozen_across_dst_boundary(tmp_path):
    result = ShadowPredictionService(
        PredictionLedger(tmp_path / "shadow.db"), prediction_provider=_Provider()
    ).calculate(_request(), _competitors(), WOOD)

    assert result.receipt is not None
    assert result.receipt.core["prediction_as_of"] == date(2026, 11, 2).isoformat()
    assert result.receipt.core["cutoff_semantics"] == "exclusive-utc-date"


def test_unsupported_target_and_unbounded_or_non_namespaced_identity_fail_closed(
    tmp_path,
):
    service = ShadowPredictionService(
        PredictionLedger(tmp_path / "shadow.db"), prediction_provider=_Provider()
    )
    with pytest.raises(ValueError, match="unsupported shadow target"):
        service.calculate(_request(target_contract="best-of-three.v1"), _competitors(), WOOD)
    with pytest.raises(ValueError, match="consumer_id must be namespaced"):
        service.calculate(_request(consumer_id="Missoula"), _competitors(), WOOD)
    with pytest.raises(ValueError, match="at most 128"):
        service.calculate(_request(consumer_id="missoula:" + "x" * 120), _competitors(), WOOD)


def test_receipt_rows_are_append_only(tmp_path):
    path = tmp_path / "shadow.db"
    ShadowPredictionService(PredictionLedger(path), prediction_provider=_Provider()).calculate(
        _request(), _competitors(), WOOD
    )

    with (
        sqlite3.connect(path) as conn,
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        conn.execute("UPDATE shadow_receipts SET core_json = '{}' ")


def test_existing_ledger_field_without_receipt_fails_closed_before_provider(tmp_path):
    path = tmp_path / "shadow.db"
    ledger = PredictionLedger(path)
    HandicapCalculator(
        prediction_provider=_Provider(),
        ledger_sink=ledger,
        ledger_caller_id="missoula:service",
    ).calculate(
        _competitors(),
        WOOD,
        "SB",
        context=PredictionContext(
            prediction_as_of=date(2026, 11, 2),
            request_id="missoula:request-1",
            seed=20260811,
            engine="v2",
        ),
    )
    provider = _Provider("core-b", 99.0, "d" * 64)

    with pytest.raises(LedgerConflictError, match="incomplete.*receipt"):
        ShadowPredictionService(ledger, prediction_provider=provider).calculate(
            _request(), _competitors(), WOOD
        )

    assert provider.calls == 0


def _mutate_receipt(path, sql: str, parameters=()) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER shadow_receipts_no_update")
        conn.execute(sql, parameters)


def test_malformed_receipt_json_is_explicitly_untrusted_and_never_recalculated(tmp_path):
    path = tmp_path / "shadow.db"
    ShadowPredictionService(PredictionLedger(path), prediction_provider=_Provider()).calculate(
        _request(), _competitors(), WOOD
    )
    _mutate_receipt(path, "UPDATE shadow_receipts SET core_json = '{'")
    provider = _Provider("core-b", 99.0, "d" * 64)

    with pytest.raises(ShadowReceiptCorruptionError, match="malformed"):
        ShadowPredictionService(PredictionLedger(path), prediction_provider=provider).calculate(
            _request(), _competitors(), WOOD
        )

    assert provider.calls == 0


@pytest.mark.parametrize("corruption", ["fingerprint", "predictions"])
def test_internally_inconsistent_receipt_is_rejected_without_recalculation(tmp_path, corruption):
    path = tmp_path / "shadow.db"
    ShadowPredictionService(PredictionLedger(path), prediction_provider=_Provider()).calculate(
        _request(), _competitors(), WOOD
    )
    with sqlite3.connect(path) as conn:
        core_json = conn.execute("SELECT core_json FROM shadow_receipts").fetchone()[0]
    core = __import__("json").loads(core_json)
    if corruption == "fingerprint":
        core["active_input"]["fingerprint"] = "f" * 64
    else:
        core["predictions"][0]["competitor_id"] = "missoula-competitor:wrong"
    _mutate_receipt(
        path,
        "UPDATE shadow_receipts SET core_json = ?",
        (__import__("json").dumps(core, sort_keys=True, separators=(",", ":")),),
    )
    provider = _Provider("core-b", 99.0, "d" * 64)

    with pytest.raises(ShadowReceiptCorruptionError, match="inconsistent|fingerprint"):
        ShadowPredictionService(PredictionLedger(path), prediction_provider=provider).calculate(
            _request(), _competitors(), WOOD
        )

    assert provider.calls == 0


def _corrupt_prediction_projection(core, field):
    prediction = core["predictions"][0]
    if field == "median_seconds":
        prediction["median_seconds"] = int(prediction["median_seconds"])
    elif field == "assigned_mark":
        # JSON distinguishes 3 from 3.0 even though Python equality does not.
        prediction["assigned_mark"] = float(prediction["assigned_mark"])
    elif field == "versions":
        prediction["versions"]["calibration"] = "cal-tampered"
    elif field == "interval":
        prediction["interval"]["nominal_coverage"] = 0.5
    elif field == "optimizer_metadata":
        prediction["optimizer_metadata"]["tampered"] = True
    elif field == "warnings":
        prediction["warnings"].append("tampered warning")
    elif field == "ignored_factors":
        prediction["ignored_factors"].append("tampered_factor")
    else:  # pragma: no cover - the parametrization is the exhaustive caller
        raise AssertionError(f"unsupported corruption field: {field}")


@pytest.mark.parametrize(
    "field",
    [
        "median_seconds",
        "assigned_mark",
        "versions",
        "interval",
        "optimizer_metadata",
        "warnings",
        "ignored_factors",
    ],
)
def test_receipt_prediction_payload_corruption_is_rejected_before_provider(tmp_path, field):
    path = tmp_path / "shadow.db"
    ShadowPredictionService(PredictionLedger(path), prediction_provider=_Provider()).calculate(
        _request(), _competitors(), WOOD
    )
    with sqlite3.connect(path) as conn:
        core = json.loads(conn.execute("SELECT core_json FROM shadow_receipts").fetchone()[0])
    _corrupt_prediction_projection(core, field)
    _mutate_receipt(
        path,
        "UPDATE shadow_receipts SET core_json = ?",
        (json.dumps(core, sort_keys=True, separators=(",", ":")),),
    )
    provider = _Provider("core-b", 99.0, "d" * 64)

    with pytest.raises(ShadowReceiptCorruptionError, match="prediction.*inconsistent"):
        ShadowPredictionService(PredictionLedger(path), prediction_provider=provider).calculate(
            _request(), _competitors(), WOOD
        )

    assert provider.calls == 0


def test_persistence_failure_returns_an_explicit_untrusted_draft(tmp_path):
    class _FailingLedger(PredictionLedger):
        def record_field(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk unavailable")

    result = ShadowPredictionService(
        _FailingLedger(tmp_path / "shadow.db"), prediction_provider=_Provider()
    ).calculate(_request(), _competitors(), WOOD)

    assert result.trusted is False
    assert result.receipt is None
    assert result.status.trust == "write-failed"
    assert result.status.ready_for_review is False
    assert result.draft_predictions
    assert all(row["prediction_id"] is None for row in result.draft_predictions)
