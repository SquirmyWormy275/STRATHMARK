from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace

import pytest

from strathmark.v3.application.operations import (
    REQUIRED_OPERATIONAL_METRICS,
    DependencyObservation,
    DependencyState,
    OperationalFacts,
    OperationalMetrics,
    OperationalProbeSet,
    OperationalStatusService,
    ReadinessPath,
    SupportBundleExporter,
    verify_support_bundle,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
)

NOW = "2026-08-25T19:00:00.000Z"


def _ready(name: str) -> DependencyObservation:
    return DependencyObservation(name, DependencyState.READY, "verified", NOW)


def _probes(**overrides: object) -> OperationalProbeSet:
    names = OperationalProbeSet.required_dependency_names()
    values: dict[str, object] = {name: (lambda name=name: _ready(name)) for name in names}
    values.update(overrides)
    return OperationalProbeSet(**values)


def test_dependency_graph_exposes_every_dimension_and_path_without_aggregate_green() -> None:
    status = OperationalStatusService(_probes()).snapshot(observed_at=NOW)

    assert tuple(item.name for item in status.dependencies) == (
        "event_integrity",
        "projection_currency",
        "blob_integrity",
        "pinned_bundle",
        "formula",
        "ml",
        "llm_local_primary",
        "llm_local_secondary",
        "llm_cloud",
        "pool_degradation_mode",
        "writer_latency",
        "queue_deadline_risk",
        "disk_reserve",
        "backup_age",
        "issue_recovery_path",
        "cloud_consent",
    )
    assert set(status.paths) == set(ReadinessPath)
    assert all(path.ready for path in status.paths.values())
    assert "ready" not in status.to_dict()
    assert "overall_ready" not in status.to_dict()


@pytest.mark.parametrize("dependency", OperationalProbeSet.required_dependency_names())
def test_each_dependency_failure_is_exact_and_only_blocks_declared_paths(dependency: str) -> None:
    failed = DependencyObservation(
        dependency,
        DependencyState.UNAVAILABLE,
        "dependency_unavailable",
        NOW,
    )
    status = OperationalStatusService(_probes(**{dependency: lambda: failed})).snapshot(
        observed_at=NOW
    )

    observed = status.dependency(dependency)
    assert observed == failed
    for path, readiness in status.paths.items():
        assert readiness.ready is (dependency not in readiness.required_dependencies)
        assert readiness.blocking_dependencies == (
            (dependency,) if dependency in readiness.required_dependencies else ()
        )
        assert path is readiness.path


def test_probe_exception_is_redacted_and_does_not_prevent_other_probes() -> None:
    def broken() -> DependencyObservation:
        raise RuntimeError("Bearer secret-value at C:/private/operator/path")

    status = OperationalStatusService(_probes(blob_integrity=broken)).snapshot(observed_at=NOW)

    assert status.dependency("blob_integrity").reason_code == "probe_failed_closed"
    encoded = json.dumps(status.to_dict())
    assert "secret-value" not in encoded
    assert "private/operator" not in encoded
    assert status.dependency("event_integrity").state is DependencyState.READY


def test_stale_corrupt_and_saturated_are_distinct_operator_truth() -> None:
    status = OperationalStatusService(
        _probes(
            projection_currency=lambda: DependencyObservation(
                "projection_currency", DependencyState.STALE, "barrier_lag", NOW
            ),
            blob_integrity=lambda: DependencyObservation(
                "blob_integrity", DependencyState.CORRUPT, "digest_mismatch", NOW
            ),
            queue_deadline_risk=lambda: DependencyObservation(
                "queue_deadline_risk", DependencyState.SATURATED, "deadline_at_risk", NOW
            ),
        )
    ).snapshot(observed_at=NOW)

    assert status.dependency("projection_currency").state is DependencyState.STALE
    assert status.dependency("blob_integrity").state is DependencyState.CORRUPT
    assert status.dependency("queue_deadline_risk").state is DependencyState.SATURATED
    assert status.paths[ReadinessPath.FIELD].ready is False
    assert status.paths[ReadinessPath.LOOKUP].ready is False
    assert status.paths[ReadinessPath.SUPPORT].ready is True


def test_support_export_is_bounded_signed_self_contained_and_redacted() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:support-test")
    status = OperationalStatusService(_probes()).snapshot(observed_at=NOW)
    exporter = SupportBundleExporter(signer=signer, max_bytes=256_000)

    bundle = exporter.export(
        status=status,
        created_at=NOW,
        artifacts={
            "configuration.json": {
                "database_path": "C:/private/operator/v3.sqlite3",
                "api_token": "top-secret",
                "mode": "race_day",
            },
            "jobs.json": [{"job_id": "job:one", "state": "retryable-failed"}],
            "receipt.json": {
                "receipt_id": "receipt:one",
                "competitor_id": "competitor:pseudonymous-1",
            },
        },
    )

    verified = verify_support_bundle(
        bundle,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    assert verified["status_digest"] == status.digest
    assert verified["entry_count"] == 4
    assert b"top-secret" not in bundle
    assert b"private/operator" not in bundle
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert archive.namelist() == [
            "configuration.json",
            "jobs.json",
            "receipt.json",
            "status.json",
            "support-manifest.json",
        ]
        configuration = json.loads(archive.read("configuration.json"))
        assert configuration == {
            "api_token": "[REDACTED]",
            "database_path": "[REDACTED]",
            "mode": "race_day",
        }

    with pytest.raises(ValueError, match="digest"):
        verify_support_bundle(
            bundle[:-1] + bytes([bundle[-1] ^ 1]),
            trust_store=IntegrityTrustStore((signer.identity,)),
        )


def test_support_export_rejects_unsafe_names_and_oversized_material() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:support-test")
    status = OperationalStatusService(_probes()).snapshot(observed_at=NOW)
    exporter = SupportBundleExporter(signer=signer, max_bytes=16_384)

    with pytest.raises(ValueError, match="artifact name"):
        exporter.export(status=status, created_at=NOW, artifacts={"../secret": {}})
    with pytest.raises(ValueError, match="maximum"):
        exporter.export(
            status=status,
            created_at=NOW,
            artifacts={"large.json": {"value": "x" * 20_000}},
        )


def test_observation_rejects_unknown_names_and_mismatched_probe_identity() -> None:
    with pytest.raises(ValueError, match="dependency name"):
        replace(_ready("event_integrity"), name="unknown")

    status_service = OperationalStatusService(
        _probes(event_integrity=lambda: _ready("blob_integrity"))
    )
    status = status_service.snapshot(observed_at=NOW)
    assert status.dependency("event_integrity").state is DependencyState.UNAVAILABLE
    assert status.dependency("event_integrity").reason_code == "probe_identity_mismatch"


def test_status_carries_complete_race_day_facts_and_metrics_without_hiding_paths() -> None:
    facts = OperationalFacts(
        telemetry_available=True,
        active_bundle_digest="a" * 64,
        frozen_epoch_id="epoch:show-round-2",
        frozen_weights_digest="b" * 64,
        queue_depths=(
            ("hot_field", 1),
            ("inference", 20),
            ("lookup_recovery", 0),
            ("maintenance", 0),
        ),
        oldest_job_age_ms=2_500,
        assessor_availability=(
            ("formula", True),
            ("ml", True),
            ("llm_local_primary", True),
            ("llm_local_secondary", True),
            ("llm_cloud", False),
        ),
        model_warmth=(("llm_local_primary", True), ("llm_local_secondary", False)),
        last_event_digest="c" * 64,
        projection_healthy=True,
        backup_healthy=True,
        readiness_sla_risk=False,
    )
    metrics = OperationalMetrics.from_mapping(
        {name: index for index, name in enumerate(REQUIRED_OPERATIONAL_METRICS)}
    )
    status = OperationalStatusService(_probes()).snapshot(
        observed_at=NOW,
        facts=facts,
        metrics=metrics,
    )

    encoded = status.to_dict()
    assert encoded["facts"]["active_bundle_digest"] == "a" * 64
    assert encoded["facts"]["queue_depths"]["inference"] == 20
    assert encoded["metrics"]["values"]["deadline_misses"] == 1
    assert tuple(encoded["metrics"]["values"]) == REQUIRED_OPERATIONAL_METRICS
    assert set(status.paths) == set(ReadinessPath)


def test_telemetry_rejects_missing_metrics_and_noncanonical_lanes() -> None:
    with pytest.raises(ValueError, match="complete"):
        OperationalMetrics.from_mapping({"stage_latency_ms": 1})
    with pytest.raises(ValueError, match="queue lanes"):
        OperationalFacts(
            telemetry_available=True,
            active_bundle_digest="a" * 64,
            frozen_epoch_id="epoch:one",
            frozen_weights_digest="b" * 64,
            queue_depths=(("inference", 1),),
            oldest_job_age_ms=1,
            assessor_availability=(("formula", True),),
            model_warmth=(("llm_local_primary", True),),
            last_event_digest="c" * 64,
            projection_healthy=True,
            backup_healthy=True,
            readiness_sla_risk=False,
        )


def test_operational_contract_rejects_invalid_observations_probes_metrics_and_snapshots() -> None:
    base = _ready("event_integrity")
    invalid_observations = (
        lambda: replace(base, state="ready"),
        lambda: replace(base, reason_code="BAD-REASON"),
        lambda: replace(base, detail=""),
        lambda: replace(base, detail="x" * 257),
    )
    for build in invalid_observations:
        with pytest.raises(ValueError):
            build()
    assert replace(base, detail="operator-visible").to_dict()["detail"] == "operator-visible"

    with pytest.raises(ValueError, match="explicit probe"):
        replace(_probes(), event_integrity=None)
    with pytest.raises(ValueError, match="probe set"):
        OperationalStatusService(object())
    service = OperationalStatusService(_probes())
    with pytest.raises(ValueError, match="facts"):
        service.snapshot(observed_at=NOW, facts=object())
    with pytest.raises(ValueError, match="metrics"):
        service.snapshot(observed_at=NOW, metrics=object())
    stale = service.snapshot(
        observed_at=NOW,
    )
    with pytest.raises(ValueError, match="snapshot"):
        stale.dependency("unknown")

    timestamp_mismatch = OperationalStatusService(
        _probes(
            event_integrity=lambda: DependencyObservation(
                "event_integrity",
                DependencyState.READY,
                "verified",
                "2026-08-25T19:00:01.000Z",
            )
        )
    ).snapshot(observed_at=NOW)
    assert timestamp_mismatch.dependency("event_integrity").state is DependencyState.STALE
    assert (
        timestamp_mismatch.dependency("event_integrity").reason_code == "probe_timestamp_mismatch"
    )

    with pytest.raises(ValueError, match="availability"):
        OperationalMetrics("yes", ())
    with pytest.raises(ValueError, match="complete"):
        OperationalMetrics(True, ())
    with pytest.raises(ValueError, match="cannot contain"):
        OperationalMetrics(False, (("stage_latency_ms", 1),))
    with pytest.raises(ValueError, match="non-negative"):
        OperationalMetrics.from_mapping(
            {name: -1 if name == "stage_latency_ms" else 0 for name in REQUIRED_OPERATIONAL_METRICS}
        )


def test_operational_facts_reject_invalid_digests_epoch_depth_age_and_member_shapes() -> None:
    valid = OperationalFacts(
        telemetry_available=True,
        active_bundle_digest="a" * 64,
        frozen_epoch_id="epoch:one",
        frozen_weights_digest="b" * 64,
        queue_depths=(
            ("hot_field", 0),
            ("inference", 0),
            ("lookup_recovery", 0),
            ("maintenance", 0),
        ),
        oldest_job_age_ms=0,
        assessor_availability=(
            ("formula", True),
            ("ml", True),
            ("llm_local_primary", True),
            ("llm_local_secondary", True),
            ("llm_cloud", True),
        ),
        model_warmth=(("llm_local_primary", True), ("llm_local_secondary", True)),
        last_event_digest="c" * 64,
        projection_healthy=True,
        backup_healthy=True,
        readiness_sla_risk=False,
    )
    mutations = (
        {"telemetry_available": 1},
        {"active_bundle_digest": "bad"},
        {"frozen_epoch_id": "round:one"},
        {
            "queue_depths": (
                ("hot_field", -1),
                ("inference", 0),
                ("lookup_recovery", 0),
                ("maintenance", 0),
            )
        },
        {"oldest_job_age_ms": -1},
        {"assessor_availability": (("formula", True),)},
        {"model_warmth": (("llm_local_primary", "yes"), ("llm_local_secondary", True))},
    )
    for mutation in mutations:
        with pytest.raises(ValueError):
            replace(valid, **mutation)
