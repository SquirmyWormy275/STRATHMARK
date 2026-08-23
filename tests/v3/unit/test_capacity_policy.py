from __future__ import annotations

import json

import pytest

from strathmark.v3.application import capacity as capacity_module
from strathmark.v3.application.capacity import (
    AdmissionDecision,
    CapacityError,
    CapacityManifest,
    CapacityUse,
    JobKind,
    JobLane,
    JobPriority,
    JobResourceClass,
    LaneCapacity,
    QueueLoad,
    decide_admission,
    validate_capacity_use,
)


def _manifest() -> CapacityManifest:
    return CapacityManifest(
        schema_version="strathmark-v3-job-capacity-v1",
        max_open_tournaments=1,
        max_round_entrants=48,
        max_field_entrants=12,
        max_plausible_qualifiers=48,
        max_context_cards=48,
        max_queued_jobs=12,
        max_receipt_bytes=1_048_576,
        max_blob_bytes=16_777_216,
        max_api_page_size=100,
        reserved_imminent_jobs=2,
        reserved_recovery_jobs=2,
        aging_interval_ms=1_000,
        aging_increment=5,
        lanes=(
            LaneCapacity(JobLane.HOT_FIELD, 4, 2),
            LaneCapacity(JobLane.INFERENCE, 4, 1),
            LaneCapacity(JobLane.LOOKUP_RECOVERY, 2, 2),
            LaneCapacity(JobLane.MAINTENANCE, 2, 1),
        ),
    )


def test_manifest_is_canonical_round_trippable_and_checked_in() -> None:
    manifest = _manifest()
    assert CapacityManifest.from_dict(manifest.to_dict()) == manifest
    assert len(manifest.digest) == 64
    checked_in = CapacityManifest.load("benchmarks/v3/job_capacity_manifest.json")
    assert checked_in.max_open_tournaments == 1
    assert checked_in.max_field_entrants == 12
    assert checked_in.lane(JobLane.LOOKUP_RECOVERY).max_leased >= 1
    with open("benchmarks/v3/job_capacity_manifest.json", encoding="utf-8") as handle:
        assert json.load(handle) == checked_in.to_dict()


def test_admission_reserves_imminent_capacity_and_isolates_recovery() -> None:
    manifest = _manifest()
    load = QueueLoad(total_active=9, lane_active=3, lane_leased=0)
    assert decide_admission(
        manifest, JobLane.HOT_FIELD, JobPriority.IMMINENT_FIELD, load
    ) == AdmissionDecision(True, "capacity_available")
    assert decide_admission(
        manifest, JobLane.HOT_FIELD, JobPriority.SCHEDULED_ENTRANT, load
    ) == AdmissionDecision(False, "imminent_capacity_reserved")
    assert decide_admission(
        manifest,
        JobLane.HOT_FIELD,
        JobPriority.IMMINENT_FIELD,
        QueueLoad(total_active=10, lane_active=3, lane_leased=0),
    ) == AdmissionDecision(False, "recovery_capacity_reserved")

    recovery_load = QueueLoad(total_active=10, lane_active=0, lane_leased=0)
    assert decide_admission(
        manifest,
        JobLane.LOOKUP_RECOVERY,
        JobPriority.RECOVERY,
        recovery_load,
    ).admitted
    assert decide_admission(
        manifest,
        JobLane.LOOKUP_RECOVERY,
        JobPriority.RECOVERY,
        QueueLoad(total_active=12, lane_active=0, lane_leased=0),
    ) == AdmissionDecision(False, "global_queue_full")


def test_admission_rejects_lane_total_lease_and_maintenance_limits() -> None:
    manifest = _manifest()
    assert (
        decide_admission(
            manifest,
            JobLane.INFERENCE,
            JobPriority.PLAUSIBLE_QUALIFIER,
            QueueLoad(3, 4, 0),
        ).reason
        == "lane_queue_full"
    )
    assert (
        decide_admission(
            manifest,
            JobLane.INFERENCE,
            JobPriority.PLAUSIBLE_QUALIFIER,
            QueueLoad(3, 1, 1),
            for_claim=True,
        ).reason
        == "lane_lease_full"
    )
    assert (
        decide_admission(
            manifest,
            JobLane.MAINTENANCE,
            JobPriority.MAINTENANCE,
            QueueLoad(0, 0, 0),
            maintenance_suspended=True,
        ).reason
        == "maintenance_suspended"
    )
    assert (
        decide_admission(
            manifest,
            JobLane.INFERENCE,
            JobPriority.PLAUSIBLE_QUALIFIER,
            QueueLoad(12, 0, 0),
        ).reason
        == "global_queue_full"
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: LaneCapacity("inference", 1, 1),
        lambda: LaneCapacity(JobLane.INFERENCE, 0, 1),
        lambda: LaneCapacity(JobLane.INFERENCE, 1, 2),
        lambda: QueueLoad(-1, 0, 0),
        lambda: QueueLoad(0, 1, 2),
        lambda: CapacityManifest.from_dict({}),
    ],
)
def test_capacity_contracts_fail_closed(factory) -> None:
    with pytest.raises(CapacityError):
        factory()


def test_manifest_rejects_duplicate_or_missing_lanes_and_bad_bounds() -> None:
    good = _manifest().to_dict()
    duplicate = {**good, "lanes": [good["lanes"][0]] * 4}
    with pytest.raises(CapacityError):
        CapacityManifest.from_dict(duplicate)
    missing = {**good, "lanes": good["lanes"][:-1]}
    with pytest.raises(CapacityError):
        CapacityManifest.from_dict(missing)
    bad = {**good, "max_field_entrants": 0}
    with pytest.raises(CapacityError):
        CapacityManifest.from_dict(bad)


def test_capacity_complete_rejection_matrix(tmp_path) -> None:
    good = _manifest().to_dict()
    cases = [
        {**good, "schema_version": "wrong"},
        {**good, "reserved_imminent_jobs": good["max_queued_jobs"]},
        {**good, "max_field_entrants": 49},
        {**good, "max_plausible_qualifiers": 49},
        {**good, "max_context_cards": 47},
        {**good, "lanes": tuple()},
        {
            **good,
            "max_queued_jobs": 100,
        },
        {**good, "lanes": "not-an-array"},
    ]
    for value in cases:
        with pytest.raises(CapacityError):
            CapacityManifest.from_dict(value)
    with pytest.raises(CapacityError):
        CapacityManifest(
            schema_version="strathmark-v3-job-capacity-v1",
            max_open_tournaments=1,
            max_round_entrants=1,
            max_field_entrants=1,
            max_plausible_qualifiers=1,
            max_context_cards=1,
            max_queued_jobs=3,
            max_receipt_bytes=1,
            max_blob_bytes=1,
            max_api_page_size=1,
            reserved_imminent_jobs=1,
            reserved_recovery_jobs=1,
            aging_interval_ms=1,
            aging_increment=1,
            lanes=(object(),),
        )
    with pytest.raises(CapacityError):
        LaneCapacity.from_dict({"lane": "unknown", "max_queued": 1, "max_leased": 1})
    with pytest.raises(CapacityError):
        _manifest().lane("inference")

    with pytest.raises(CapacityError):
        CapacityManifest.load(True)
    with pytest.raises(CapacityError):
        CapacityManifest.load(tmp_path / "missing.json")
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{", encoding="utf-8")
    with pytest.raises(CapacityError):
        CapacityManifest.load(bad_json)
    array_json = tmp_path / "array.json"
    array_json.write_text("[]", encoding="utf-8")
    with pytest.raises(CapacityError):
        CapacityManifest.load(array_json)
    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_text(json.dumps(good, indent=2), encoding="utf-8")
    with pytest.raises(CapacityError):
        CapacityManifest.load(noncanonical)


def test_capacity_admission_and_scalar_type_guards_are_total() -> None:
    manifest = _manifest()
    load = QueueLoad(0, 0, 0)
    with pytest.raises(CapacityError):
        AdmissionDecision(1, "reason")
    with pytest.raises(CapacityError):
        AdmissionDecision(True, "")
    with pytest.raises(CapacityError):
        AdmissionDecision(True, 1)
    for arguments in (
        (object(), JobLane.INFERENCE, JobPriority.IMMINENT_FIELD, load),
        (manifest, "inference", JobPriority.IMMINENT_FIELD, load),
        (manifest, JobLane.INFERENCE, 400, load),
        (manifest, JobLane.INFERENCE, JobPriority.IMMINENT_FIELD, object()),
    ):
        with pytest.raises(CapacityError):
            decide_admission(*arguments)
    with pytest.raises(CapacityError):
        decide_admission(
            manifest,
            JobLane.INFERENCE,
            JobPriority.IMMINENT_FIELD,
            load,
            for_claim=1,
        )
    with pytest.raises(CapacityError):
        decide_admission(
            manifest,
            JobLane.INFERENCE,
            JobPriority.IMMINENT_FIELD,
            load,
            maintenance_suspended=1,
        )

    for value in (True, "1", 0, -1):
        with pytest.raises(CapacityError):
            capacity_module._positive(value, "value")
    for value in (True, "0", -1):
        with pytest.raises(CapacityError):
            capacity_module._nonnegative(value, "value")


def test_every_declared_operational_limit_is_consumed_before_admission() -> None:
    manifest = _manifest()
    baseline = CapacityUse(1, 48, 12, 48, 48, 1_048_576, 16_777_216, 100)
    assert validate_capacity_use(manifest, baseline).admitted
    fields = tuple(baseline.__dataclass_fields__)
    for field in fields:
        values = {name: getattr(baseline, name) for name in fields}
        values[field] += 1
        assert not validate_capacity_use(manifest, CapacityUse(**values)).admitted
    with pytest.raises(CapacityError):
        validate_capacity_use(object(), baseline)
    with pytest.raises(CapacityError):
        validate_capacity_use(manifest, object())


def test_job_kind_mapping_is_closed_and_resource_explicit() -> None:
    assert JobKind.LOCAL_LLM_CARD.lane is JobLane.INFERENCE
    assert JobKind.LOCAL_LLM_CARD.resource_class is JobResourceClass.LOCAL_GPU
    assert JobKind.CLOUD_LLM_CARD.resource_class is JobResourceClass.CLOUD
    assert JobKind.MODEL_FACTORY.lane is JobLane.MAINTENANCE


def test_manifest_rejects_zero_critical_reservations() -> None:
    values = _manifest().to_dict()
    for field in ("reserved_imminent_jobs", "reserved_recovery_jobs"):
        with pytest.raises(CapacityError):
            CapacityManifest.from_dict({**values, field: 0})
