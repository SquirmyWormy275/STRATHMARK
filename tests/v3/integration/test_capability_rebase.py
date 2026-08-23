from __future__ import annotations

import base64
import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import strathmark.v3.application.capability_reactions as capability_module
from strathmark.v3.application.capability_reactions import (
    CapabilityAdmissionVerifier,
    CapabilityAuthorityPort,
    CapabilityCapacityVerifier,
    CapabilityReactionError,
    CapabilityReactionReceipt,
    CapabilityReactionService,
    CapabilityStateSnapshot,
    SealedCapabilityAdmission,
    SealedCapabilityCapacity,
    SQLiteCapabilityAuthority,
    activate_historical_import_cutover,
    seal_capability_admission,
    seal_capability_capacity,
    seal_historical_import_cutover,
)
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.evidence import ResultObservation, TargetContext
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
from strathmark.v3.domain.capability import CapabilityPrior, HistoricalImportBinding
from strathmark.v3.domain.evidence import (
    AdmissionReason,
    AdmittedEvidence,
    EvidenceSource,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
    sign_manifest,
)
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import EventStoreConflict


@dataclass
class _Authority(CapabilityAuthorityPort):
    accepted: set[int]
    invalidated: tuple[StableIdentifier, ...] = ()
    reactions: int = 6
    reject_at_commit: bool = False

    def verify_source(self, evidence) -> None:  # type: ignore[no-untyped-def]
        if evidence.source_global_sequence not in self.accepted:
            raise ValueError("source not authoritative")

    def invalidated_unissued_work(self, evidence) -> tuple[StableIdentifier, ...]:  # type: ignore[no-untyped-def]
        return self.invalidated if evidence.supersedes_revision is not None else ()

    def mandatory_reaction_count(self, evidence, lineage_sources, invalidated_work) -> int:  # type: ignore[no-untyped-def]
        del evidence, lineage_sources, invalidated_work
        return self.reactions

    def verify_source_at_commit(self, connection, evidence) -> None:  # type: ignore[no-untyped-def]
        if self.reject_at_commit:
            raise ValueError("source ceased to be latest before commit")
        self.verify_source(evidence)


def _admitted(raw_ms: int | None, revision: int, *, result_key: str = "result:one"):
    status = ResultStatus.COMPLETION if raw_ms is not None else ResultStatus.VOID
    observation = ResultObservation(
        evidence_id=StableIdentifier(f"evidence:{result_key.split(':')[1]}-{revision}"),
        competitor_id=StableIdentifier("competitor:alice"),
        tournament_id=StableIdentifier("tournament:show"),
        round_id=StableIdentifier("round:heat"),
        field_id=StableIdentifier("field:one"),
        context=TargetContext("underhand", 300, "pine", "taxonomy:v1", "conversion:v1"),
        observation_sequence=revision,
        occurred_at_utc=f"2026-03-{revision:02d}T12:00:00.000Z",
        issued_mark=3,
        completion_clock_ms=raw_ms,
        placing=1 if raw_ms is not None else None,
        gap_ms=0 if raw_ms is not None else None,
        result=OfficialResult(status, raw_ms, None, revision, revision - 1 or None),
        source_digest=canonical_digest({"result": result_key, "revision": revision, "raw": raw_ms}),
    )
    return AdmittedEvidence(
        observation,
        EvidenceSource.LIVE_ISSUED_RACE,
        raw_ms is not None,
        raw_ms,
        AdmissionReason.ELIGIBLE_COMPLETION
        if raw_ms is not None
        else AdmissionReason.STATUS_INELIGIBLE,
    )


def _service(
    tmp_path: Path,
    accepted: set[int],
    invalidated=(),
    *,
    reactions: int = 6,
    envelope: capability_module.CapabilityCapacityEnvelope | None = None,
):
    signer = P256EphemeralSigner.generate("capability-test")
    verifier = CapabilityAdmissionVerifier(IntegrityTrustStore((signer.identity,)))
    capacity = seal_capability_capacity(
        envelope or capability_module.CapabilityCapacityEnvelope(),
        signer=signer,
        created_at="2026-04-01T00:00:00.000Z",
    )
    return CapabilityReactionService(
        tmp_path / "capability.sqlite3",
        verifier=verifier,
        authority=_Authority(accepted, invalidated, reactions),
        capacity=capacity,
        capacity_verifier=CapabilityCapacityVerifier(IntegrityTrustStore((signer.identity,))),
    ), signer


def _seal(admitted, sequence: int, signer, *, result_key: str = "result:one"):
    return seal_capability_admission(
        admitted=admitted,
        result_key=StableIdentifier(result_key),
        source_global_sequence=sequence,
        authority_digest="e" * 64,
        prior=CapabilityPrior.from_median_seconds("40", calibrated_beta="0.12"),
        evidence_log_variance="0.0025",
        conversion_log_variance="0",
        effective_weight="1",
        historical_binding=None,
        signer=signer,
        created_at=f"2026-04-{min(sequence, 28):02d}T00:00:00.000Z",
    )


def _react(service, sealed, key: str):
    return service.react(
        sealed,
        command_id=IdempotencyKey(f"command:{key}"),
        actor_id=StableIdentifier("actor:system"),
        occurred_at_utc="2026-04-01T00:00:00.000Z",
        monotonic_elapsed_ms=1,
        complete_derivation_barrier=False,
    )


def test_signed_admission_advances_and_exact_retry_returns_identical_receipt(
    tmp_path: Path,
) -> None:
    service, signer = _service(tmp_path, {1})
    sealed = _seal(_admitted(40_000, 1), 1, signer)
    first = _react(service, sealed, "first")
    retry = _react(service, sealed, "first")
    assert first == retry
    assert first.after_state is not None
    assert first.event_kind == "capability_updated"
    assert first.governor_receipt_digest == canonical_digest(sealed.to_dict())
    assert first.after_state.last_transition.source_authority_digest == "e" * 64


def test_retry_verifies_signature_then_binds_the_complete_original_manifest(tmp_path: Path) -> None:
    service, signer = _service(tmp_path, {1})
    original = _seal(_admitted(40_000, 1), 1, signer)
    first = _react(service, original, "signed-retry")

    invalid_signature = SealedCapabilityAdmission(
        replace(
            original.manifest,
            signature_der_b64=base64.b64encode(b"not-a-der-signature").decode("ascii"),
        )
    )
    with pytest.raises(CapabilityReactionError, match="signature"):
        _react(service, invalid_signature, "signed-retry")

    resigned = _seal(_admitted(40_000, 1), 1, signer)
    assert resigned.manifest.body_json == original.manifest.body_json
    assert resigned.manifest.signature_der_b64 != original.manifest.signature_der_b64
    with pytest.raises(EventStoreConflict, match="different signed admission"):
        _react(service, resigned, "signed-retry")

    attacker = P256EphemeralSigner.generate("retry-attacker")
    with pytest.raises(CapabilityReactionError, match="signature"):
        _react(service, _seal(_admitted(40_000, 1), 1, attacker), "signed-retry")
    assert _react(service, original, "signed-retry").to_dict() == first.to_dict()


def test_retry_rejects_noninline_authority_and_mismatched_receipt_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, signer = _service(tmp_path, {1})
    sealed = _seal(_admitted(40_000, 1), 1, signer)
    first = _react(service, sealed, "retry-defensive")
    aggregate = service._aggregate_id(StableIdentifier("competitor:alice"), first.context_digest)
    with open_v3_connection(service._events.database_path, read_only=True) as connection:
        row = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE global_sequence=?",
            (first.source_global_sequence,),
        ).fetchone()
    assert row is not None
    real_event = capability_module.EventEnvelope.from_dict(json.loads(str(row[0])))
    stored = service._events.lookup_exact_retry(
        principal_id="actor:system",
        idempotency_key="command:retry-defensive",
        command_kind=real_event.command.kind,
        target_aggregate=str(aggregate),
        payload_digest=real_event.command.payload_digest,
    )
    monkeypatch.setattr(service._events, "lookup_exact_retry", lambda **_kwargs: stored)

    class _EnvelopeFactory:
        payload = object()

        @classmethod
        def from_dict(cls, _value):
            return SimpleNamespace(
                command=SimpleNamespace(
                    kind=real_event.command.kind,
                    target_aggregate=real_event.command.target_aggregate,
                    payload_digest=real_event.command.payload_digest,
                    payload=cls.payload,
                )
            )

    monkeypatch.setattr(capability_module, "EventEnvelope", _EnvelopeFactory)
    with pytest.raises(EventStoreConflict, match="not inline"):
        service._exact_retry(
            IdempotencyKey("command:retry-defensive"),
            StableIdentifier("actor:system"),
            sealed,
        )

    _EnvelopeFactory.payload = real_event.command.payload

    class _ReceiptFactory:
        @classmethod
        def from_dict(cls, _value):
            return replace(first, governor_receipt_digest="f" * 64)

    monkeypatch.setattr(capability_module, "CapabilityReactionReceipt", _ReceiptFactory)
    with pytest.raises(EventStoreConflict, match="different admission"):
        service._exact_retry(
            IdempotencyKey("command:retry-defensive"),
            StableIdentifier("actor:system"),
            sealed,
        )


def test_capacity_manifest_is_p256_signed_pinned_and_tamper_evident() -> None:
    signer = P256EphemeralSigner.generate("capacity-authority")
    envelope = capability_module.CapabilityCapacityEnvelope()
    sealed = seal_capability_capacity(
        envelope, signer=signer, created_at="2026-04-01T00:00:00.000Z"
    )
    verifier = CapabilityCapacityVerifier(IntegrityTrustStore((signer.identity,)))
    assert verifier.verify(sealed) == envelope

    tampered = SealedCapabilityCapacity(
        replace(
            sealed.manifest,
            signature_der_b64=base64.b64encode(b"not-a-der-signature").decode("ascii"),
        )
    )
    with pytest.raises(CapabilityReactionError, match="signature"):
        verifier.verify(tampered)
    attacker = P256EphemeralSigner.generate("capacity-attacker")
    untrusted = seal_capability_capacity(
        envelope, signer=attacker, created_at="2026-04-01T00:00:00.000Z"
    )
    with pytest.raises(CapabilityReactionError, match="untrusted"):
        verifier.verify(untrusted)


def test_untrusted_or_unsupported_evidence_never_advances(tmp_path: Path) -> None:
    service, _signer = _service(tmp_path, {1})
    attacker = P256EphemeralSigner.generate("attacker")
    sealed = _seal(_admitted(40_000, 1), 1, attacker)
    with pytest.raises(ValueError):
        _react(service, sealed, "attacker")
    service2, signer2 = _service(tmp_path / "other", set())
    with pytest.raises(ValueError, match="authoritative"):
        _react(service2, _seal(_admitted(40_000, 1), 1, signer2), "unsupported")


def test_source_revision_is_reverified_inside_append_transaction(tmp_path: Path) -> None:
    signer = P256EphemeralSigner.generate("commit-guard")
    authority = _Authority({1}, reject_at_commit=True)
    trust = IntegrityTrustStore((signer.identity,))
    service = CapabilityReactionService(
        tmp_path / "commit-guard.sqlite3",
        verifier=CapabilityAdmissionVerifier(trust),
        authority=authority,
        capacity=seal_capability_capacity(
            capability_module.CapabilityCapacityEnvelope(),
            signer=signer,
            created_at="2026-04-01T00:00:00.000Z",
        ),
        capacity_verifier=CapabilityCapacityVerifier(trust),
    )
    with pytest.raises(ValueError, match="ceased to be latest"):
        _react(service, _seal(_admitted(40_000, 1), 1, signer), "commit-race")
    with open_v3_connection(service._events.database_path, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM v3_idempotency_records").fetchone()[0] == 0


def test_correction_and_void_append_rebase_with_exact_clean_digest(tmp_path: Path) -> None:
    service, signer = _service(tmp_path, {1, 2, 3, 4, 99}, (StableIdentifier("field:future"),))
    _react(service, _seal(_admitted(40_000, 1), 1, signer), "r1")
    _react(
        service,
        _seal(_admitted(70_000, 1, result_key="result:two"), 2, signer, result_key="result:two"),
        "r2",
    )
    _react(
        service,
        _seal(
            _admitted(42_000, 1, result_key="result:three"), 3, signer, result_key="result:three"
        ),
        "r3",
    )
    corrected = _react(
        service,
        _seal(_admitted(35_000, 2, result_key="result:two"), 99, signer, result_key="result:two"),
        "correct",
    )
    assert corrected.event_kind == "capability_state_rebased"
    assert corrected.invalidated_unissued_work == (StableIdentifier("field:future"),)
    clean = service.replay_active_state(
        StableIdentifier("competitor:alice"), corrected.context_digest
    )
    assert clean is not None and clean.state_digest == corrected.after_state.state_digest
    voided = _react(
        service,
        _seal(_admitted(None, 2, result_key="result:three"), 4, signer, result_key="result:three"),
        "void",
    )
    assert voided.after_state is not None
    assert voided.after_state.observation_count == 2
    assert voided.before_state.state_digest != voided.after_state.state_digest


def test_seal_is_closed_and_source_bound(tmp_path: Path) -> None:
    service, signer = _service(tmp_path, {1})
    sealed = _seal(_admitted(40_000, 1), 1, signer)
    payload = sealed.manifest.to_dict()
    payload["kind"] = "other"
    with pytest.raises(ValueError):
        SealedCapabilityAdmission.from_dict(payload)
    assert _react(service, sealed, "closed").source_global_sequence == 1


def test_signed_admission_and_receipt_rejection_matrix(tmp_path: Path) -> None:
    service, signer = _service(tmp_path, {1})
    admitted = _admitted(40_000, 1)
    with pytest.raises(CapabilityReactionError):
        seal_capability_admission(
            admitted="bad",  # type: ignore[arg-type]
            result_key=StableIdentifier("result:one"),
            source_global_sequence=1,
            authority_digest="e" * 64,
            prior=CapabilityPrior.from_median_seconds("40", calibrated_beta="0.12"),
            evidence_log_variance="0",
            conversion_log_variance="0",
            effective_weight="1",
            historical_binding=None,
            signer=signer,
            created_at="2026-04-01T00:00:00.000Z",
        )
    with pytest.raises(CapabilityReactionError):
        CapabilityAdmissionVerifier("trust")  # type: ignore[arg-type]
    with pytest.raises(CapabilityReactionError):
        SealedCapabilityAdmission("manifest")  # type: ignore[arg-type]
    verifier = CapabilityAdmissionVerifier(IntegrityTrustStore((signer.identity,)))
    with pytest.raises(CapabilityReactionError):
        verifier.verify("sealed")  # type: ignore[arg-type]
    wrong_payload = SealedCapabilityAdmission(
        sign_manifest(
            "capability_admission",
            {"schema_version": "wrong", "evidence": {}},
            signer=signer,
            created_at="2026-04-01T00:00:00.000Z",
        )
    )
    with pytest.raises(CapabilityReactionError, match="not closed"):
        verifier.verify(wrong_payload)
    nonobject = SealedCapabilityAdmission(
        sign_manifest(
            "capability_admission",
            {"schema_version": "strathmark-v3-capability-admission-v1", "evidence": "bad"},
            signer=signer,
            created_at="2026-04-01T00:00:00.000Z",
        )
    )
    with pytest.raises(CapabilityReactionError, match="not an object"):
        verifier.verify(nonobject)
    invalid_evidence = SealedCapabilityAdmission(
        sign_manifest(
            "capability_admission",
            {"schema_version": "strathmark-v3-capability-admission-v1", "evidence": {}},
            signer=signer,
            created_at="2026-04-01T00:00:00.000Z",
        )
    )
    with pytest.raises(CapabilityReactionError, match="evidence is invalid"):
        verifier.verify(invalid_evidence)
    malformed_manifest = _seal(admitted, 1, signer).to_dict()
    malformed_manifest["signature_der_b64"] = "%%%"
    with pytest.raises(CapabilityReactionError):
        SealedCapabilityAdmission.from_dict(malformed_manifest)

    receipt = _react(service, _seal(admitted, 1, signer), "receipt")
    assert CapabilityReactionReceipt.from_dict(receipt.to_dict()) == receipt
    assert CapabilityStateSnapshot.from_dict(
        receipt.after_state and CapabilityStateSnapshot.from_state(receipt.after_state).to_dict()
    )  # type: ignore[arg-type]
    value = receipt.to_dict()
    for key, replacement in (
        ("schema_version", "wrong"),
        ("capacity", "bad"),
        ("receipt_digest", "f" * 64),
    ):
        with pytest.raises(CapabilityReactionError):
            CapabilityReactionReceipt.from_dict({**value, key: replacement})
    snapshot = CapabilityStateSnapshot.from_state(receipt.after_state)  # type: ignore[arg-type]
    with pytest.raises(CapabilityReactionError):
        CapabilityStateSnapshot.from_dict({**snapshot.to_dict(), "current_form": "bad"})


class _FakeCursor:
    def __init__(self, *, one=None, rows=()):
        self._one = one
        self._rows = rows

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, responses):
        self._responses = iter(responses)

    def execute(self, _sql, _params):
        return next(self._responses)


class _FakeContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


def test_sqlite_authority_live_historical_and_invalidation_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _service_instance, signer = _service(tmp_path, {1})
    verifier = CapabilityAdmissionVerifier(IntegrityTrustStore((signer.identity,)))
    live = verifier.verify(_seal(_admitted(40_000, 1), 1, signer))
    import_id = "v2import:" + "1" * 64
    source_cutoff = "2026-03-31T23:59:59.999Z"
    canonical_row = {"competitor_id": str(live.competitor_id), "raw_time_ms": live.raw_time_ms}
    row_digest = canonical_digest(canonical_row)
    catalog_digest = "c" * 64
    tip_digest = "d" * 64
    provenance_digest = canonical_digest(
        {
            "schema_version": "strathmark-v3-historical-capability-provenance-v1",
            "import_id": import_id,
            "row_digest": row_digest,
            "canonical_row": canonical_row,
            "source_cutoff": source_cutoff,
            "source_catalog_digest": catalog_digest,
            "source_tip_digest": tip_digest,
            "result_key": str(live.result_key),
            "competitor_id": str(live.competitor_id),
            "raw_time_ms": live.raw_time_ms,
            "context_digest": live.context_digest,
            "observed_at_utc": live.observed_at_utc,
        }
    )
    historical = replace(
        live,
        source=EvidenceSource.HISTORICAL_IMPORT,
        admission_reason=AdmissionReason.HISTORICAL_CUTOVER,
        historical_binding=HistoricalImportBinding(
            import_id, row_digest, source_cutoff, "a" * 64, provenance_digest
        ),
    )
    authority = SQLiteCapabilityAuthority(tmp_path / "unused.sqlite3")
    monkeypatch.setattr(
        capability_module,
        "open_v3_connection",
        lambda *_args, **_kwargs: _FakeContext(_FakeConnection([_FakeCursor(one=None)])),
    )
    with pytest.raises(CapabilityReactionError, match="issued result"):
        authority.verify_source(live)
    with pytest.raises(CapabilityReactionError, match="exact imported-row authority"):
        authority.verify_source(historical)
    live_row = (
        str(live.result_key),
        live.result_revision,
        str(live.competitor_id),
        live.observation_digest,
        1,
        9,
        live.authority_digest,
    )
    monkeypatch.setattr(
        capability_module,
        "open_v3_connection",
        lambda *_args, **_kwargs: _FakeContext(_FakeConnection([_FakeCursor(one=live_row)])),
    )
    authority.verify_source(live)
    monkeypatch.setattr(
        capability_module,
        "open_v3_connection",
        lambda *_args, **_kwargs: _FakeContext(
            _FakeConnection(
                [
                    _FakeCursor(
                        one=(
                            historical.authority_digest,
                            import_id,
                            source_cutoff,
                            catalog_digest,
                            tip_digest,
                            json.dumps(canonical_row, separators=(",", ":")),
                            1,
                            1,
                            "a" * 64,
                            "a" * 64,
                        )
                    )
                ]
            )
        ),
    )
    authority.verify_source(historical)

    for import_eligible, row_eligible, cutover_digest in (
        (0, 1, "a" * 64),
        (1, 0, "a" * 64),
        (1, 1, None),
        (1, 1, "malformed"),
        (1, 1, "b" * 64),
    ):
        monkeypatch.setattr(
            capability_module,
            "open_v3_connection",
            lambda *_args, _values=(import_eligible, row_eligible, cutover_digest), **_kwargs: (
                _FakeContext(
                    _FakeConnection(
                        [
                            _FakeCursor(
                                one=(
                                    historical.authority_digest,
                                    import_id,
                                    source_cutoff,
                                    catalog_digest,
                                    tip_digest,
                                    json.dumps(canonical_row, separators=(",", ":")),
                                    *_values,
                                    cutover_digest,
                                )
                            )
                        ]
                    )
                )
            ),
        )
        with pytest.raises(CapabilityReactionError, match="signed cutover"):
            authority.verify_source(historical)

    wrong_provenance = replace(
        historical,
        historical_binding=replace(historical.historical_binding, provenance_digest="f" * 64),
    )
    monkeypatch.setattr(
        capability_module,
        "open_v3_connection",
        lambda *_args, **_kwargs: _FakeContext(
            _FakeConnection(
                [
                    _FakeCursor(
                        one=(
                            historical.authority_digest,
                            import_id,
                            source_cutoff,
                            catalog_digest,
                            tip_digest,
                            json.dumps(canonical_row, separators=(",", ":")),
                            1,
                            1,
                            "a" * 64,
                            "a" * 64,
                        )
                    )
                ]
            )
        ),
    )
    with pytest.raises(CapabilityReactionError, match="normalized provenance"):
        authority.verify_source(wrong_provenance)

    tampered_row = {"competitor_id": str(live.competitor_id), "raw_time_ms": 41_000}
    tampered_provenance = canonical_digest(
        {
            "schema_version": "strathmark-v3-historical-capability-provenance-v1",
            "import_id": import_id,
            "row_digest": row_digest,
            "canonical_row": tampered_row,
            "source_cutoff": source_cutoff,
            "source_catalog_digest": catalog_digest,
            "source_tip_digest": tip_digest,
            "result_key": str(live.result_key),
            "competitor_id": str(live.competitor_id),
            "raw_time_ms": live.raw_time_ms,
            "context_digest": live.context_digest,
            "observed_at_utc": live.observed_at_utc,
        }
    )
    mismatched = replace(
        historical,
        historical_binding=replace(
            historical.historical_binding, provenance_digest=tampered_provenance
        ),
    )
    monkeypatch.setattr(
        capability_module,
        "open_v3_connection",
        lambda *_args, **_kwargs: _FakeContext(
            _FakeConnection(
                [
                    _FakeCursor(
                        one=(
                            historical.authority_digest,
                            import_id,
                            source_cutoff,
                            catalog_digest,
                            tip_digest,
                            json.dumps(tampered_row, separators=(",", ":")),
                            1,
                            1,
                            "a" * 64,
                            "a" * 64,
                        )
                    )
                ]
            )
        ),
    )
    with pytest.raises(CapabilityReactionError, match="row digest"):
        authority.verify_source(mismatched)
    assert authority.invalidated_unissued_work(live) == ()
    corrected = live.__class__(
        **{
            field: (
                2
                if field == "result_revision"
                else 1
                if field == "supersedes_revision"
                else getattr(live, field)
            )
            for field in live.__dataclass_fields__
        }
    )
    monkeypatch.setattr(
        capability_module,
        "open_v3_connection",
        lambda *_args, **_kwargs: _FakeContext(
            _FakeConnection([_FakeCursor(rows=(("field:a",), ("field:b",)))])
        ),
    )
    assert authority.invalidated_unissued_work(corrected) == (
        StableIdentifier("field:a"),
        StableIdentifier("field:b"),
    )


def test_only_exact_trusted_signed_historical_cutover_activates_row_authority(
    tmp_path: Path,
) -> None:
    from strathmark.v3.infrastructure.v2_import import import_v2_snapshot
    from tests.v3.integration.test_v2_readonly_import import _create_evidence_source

    source = tmp_path / "source" / "v2.sqlite3"
    database = tmp_path / "destination" / "v3.sqlite3"
    _create_evidence_source(source)
    imported = import_v2_snapshot(source, database, cutoff="2026-03-31T23:59:59.999Z")
    signer = P256EphemeralSigner.generate("u9-historical-cutover")
    attacker = P256EphemeralSigner.generate("u9-historical-cutover-attacker")
    trust = IntegrityTrustStore((signer.identity,))
    sealed = seal_historical_import_cutover(
        database,
        f"v2import:{imported.source_tip_digest}",
        signer=signer,
        created_at="2026-04-01T00:00:00.000Z",
    )
    with pytest.raises(CapabilityReactionError, match="manifest kind"):
        activate_historical_import_cutover(
            database,
            seal_capability_capacity(
                capability_module.CapabilityCapacityEnvelope(),
                signer=signer,
                created_at="2026-04-01T00:00:00.000Z",
            ).manifest,
            trust_store=trust,
            activated_at="2026-04-01T00:00:00.000Z",
        )
    untrusted = sign_manifest(
        capability_module.HISTORICAL_CUTOVER_MANIFEST_KIND,
        sealed.body()["payload"],
        signer=attacker,
        created_at="2026-04-01T00:00:00.000Z",
    )
    with pytest.raises(CapabilityReactionError, match="signature"):
        activate_historical_import_cutover(
            database,
            untrusted,
            trust_store=trust,
            activated_at="2026-04-01T00:00:00.000Z",
        )
    with pytest.raises(CapabilityReactionError, match="signature"):
        activate_historical_import_cutover(
            database,
            sealed,
            trust_store=trust,
            activated_at="not-a-time",
        )
    malformed = sign_manifest(
        capability_module.HISTORICAL_CUTOVER_MANIFEST_KIND,
        {},
        signer=signer,
        created_at="2026-04-01T00:00:00.000Z",
    )
    with pytest.raises(CapabilityReactionError, match="not closed"):
        activate_historical_import_cutover(
            database,
            malformed,
            trust_store=trust,
            activated_at="2026-04-01T00:00:00.000Z",
        )
    altered_payload = dict(sealed.body()["payload"])
    altered_payload["row_digests"] = ["f" * 64]
    altered = sign_manifest(
        capability_module.HISTORICAL_CUTOVER_MANIFEST_KIND,
        altered_payload,
        signer=signer,
        created_at="2026-04-01T00:00:00.000Z",
    )
    with pytest.raises(CapabilityReactionError, match="exact ineligible import"):
        activate_historical_import_cutover(
            database,
            altered,
            trust_store=trust,
            activated_at="2026-04-01T00:00:00.000Z",
        )
    cutover_digest = activate_historical_import_cutover(
        database,
        sealed,
        trust_store=trust,
        activated_at="2026-04-01T00:00:00.000Z",
    )
    with pytest.raises(CapabilityReactionError, match="uncutover authority"):
        seal_historical_import_cutover(
            database,
            f"v2import:{imported.source_tip_digest}",
            signer=signer,
            created_at="2026-04-01T00:00:00.000Z",
        )
    with pytest.raises(CapabilityReactionError, match="exact ineligible import"):
        activate_historical_import_cutover(
            database,
            sealed,
            trust_store=trust,
            activated_at="2026-04-01T00:00:00.000Z",
        )

    with open_v3_connection(database, read_only=True) as connection:
        row = connection.execute(
            "SELECT event.global_sequence, event.event_digest, imported.import_id, "
            "imported.source_cutoff, imported.source_catalog_digest, imported.source_tip_digest, "
            "imported.eligible, imported.cutover_manifest_digest, historical.row_digest, "
            "historical.canonical_json, historical.eligible "
            "FROM v3_events event JOIN v3_historical_imports imported "
            "ON imported.import_id=event.source_import_id JOIN v3_historical_import_rows historical "
            "ON historical.import_id=imported.import_id"
        ).fetchone()
    assert row is not None and (int(row[6]), int(row[10])) == (1, 1)
    canonical_row = json.loads(str(row[9]))
    admitted = _admitted(40_000, 1)
    provenance = canonical_digest(
        {
            "schema_version": "strathmark-v3-historical-capability-provenance-v1",
            "import_id": str(row[2]),
            "row_digest": str(row[8]),
            "canonical_row": canonical_row,
            "source_cutoff": str(row[3]),
            "source_catalog_digest": str(row[4]),
            "source_tip_digest": str(row[5]),
            "result_key": "result:one",
            "competitor_id": str(admitted.observation.competitor_id),
            "raw_time_ms": admitted.raw_time_ms,
            "context_digest": canonical_digest(admitted.observation.context.to_dict()),
            "observed_at_utc": admitted.observation.occurred_at_utc,
        }
    )
    historical = CapabilityAdmissionVerifier(trust).verify(
        seal_capability_admission(
            admitted=replace(
                admitted,
                source=EvidenceSource.HISTORICAL_IMPORT,
                reason=AdmissionReason.HISTORICAL_CUTOVER,
            ),
            result_key=StableIdentifier("result:one"),
            source_global_sequence=int(row[0]),
            authority_digest=str(row[1]),
            prior=CapabilityPrior.from_median_seconds("40", calibrated_beta="0.12"),
            evidence_log_variance="0.0025",
            conversion_log_variance="0",
            effective_weight="0.85",
            historical_binding=HistoricalImportBinding(
                str(row[2]), str(row[8]), str(row[3]), cutover_digest, provenance
            ),
            signer=signer,
            created_at="2026-04-01T00:00:00.000Z",
        )
    )
    authority = SQLiteCapabilityAuthority(database)
    authority.verify_source(historical)
    assert authority.mandatory_reaction_count(historical, (), ()) == 0


def test_service_conflicts_overflow_and_barrier_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, signer = _service(tmp_path, {1, 2, 3})
    sealed = _seal(_admitted(40_000, 1), 1, signer)
    first = _react(service, sealed, "first-conflict")
    with pytest.raises(CapabilityReactionError, match="first revision"):
        _react(service, sealed, "duplicate-material")
    with pytest.raises(CapabilityReactionError, match="active revision"):
        _react(
            service,
            _seal(
                _admitted(39_000, 2, result_key="result:missing"),
                2,
                signer,
                result_key="result:missing",
            ),
            "missing-prior",
        )
    with pytest.raises(CapabilityReactionError):
        service.react(
            sealed,
            command_id="command:bad",  # type: ignore[arg-type]
            actor_id=StableIdentifier("actor:system"),
            occurred_at_utc="2026-04-01T00:00:00.000Z",
            monotonic_elapsed_ms=1,
            complete_derivation_barrier=False,
        )
    with pytest.raises(CapabilityReactionError):
        service.react(
            sealed,
            command_id=IdempotencyKey("command:bad-bool"),
            actor_id=StableIdentifier("actor:system"),
            occurred_at_utc="2026-04-01T00:00:00.000Z",
            monotonic_elapsed_ms=1,
            complete_derivation_barrier="yes",  # type: ignore[arg-type]
        )
    with pytest.raises(CapabilityReactionError):
        CapabilityReactionService(
            tmp_path / "bad.sqlite3",
            verifier="bad",  # type: ignore[arg-type]
            capacity=seal_capability_capacity(
                capability_module.CapabilityCapacityEnvelope(),
                signer=signer,
                created_at="2026-04-01T00:00:00.000Z",
            ),
            capacity_verifier=CapabilityCapacityVerifier(IntegrityTrustStore((signer.identity,))),
        )
    with pytest.raises(CapabilityReactionError):
        CapabilityReactionService(
            tmp_path / "bad-cap.sqlite3",
            verifier=service._verifier,
            capacity="bad",  # type: ignore[arg-type]
            capacity_verifier=CapabilityCapacityVerifier(IntegrityTrustStore((signer.identity,))),
        )
    assert service._aggregate_id(StableIdentifier("competitor:alice"), first.context_digest)
    with pytest.raises(CapabilityReactionError):
        service._aggregate_id(StableIdentifier("competitor:alice"), "short")

    overflow_capacity = seal_capability_capacity(
        capability_module.CapabilityCapacityEnvelope(1, 128, 512),
        signer=signer,
        created_at="2026-04-01T00:00:00.000Z",
    )
    overflow_service = CapabilityReactionService(
        tmp_path / "overflow.sqlite3",
        verifier=service._verifier,
        authority=_Authority({1, 3}),
        capacity=overflow_capacity,
        capacity_verifier=CapabilityCapacityVerifier(IntegrityTrustStore((signer.identity,))),
    )
    _react(overflow_service, sealed, "overflow-first")
    overflow = _react(
        overflow_service,
        _seal(_admitted(41_000, 1, result_key="result:two"), 3, signer, result_key="result:two"),
        "overflow-second",
    )
    assert not overflow.capacity.admitted
    assert overflow.after_state.state_digest == first.after_state.state_digest  # type: ignore[union-attr]
    replayed = overflow_service.replay_active_state(
        StableIdentifier("competitor:alice"), overflow.context_digest
    )
    assert replayed is not None and replayed.observation_count == 1

    calls = []
    monkeypatch.setattr(service, "_complete_barrier", lambda *args: calls.append(args))
    assert (
        service.react(
            sealed,
            command_id=IdempotencyKey("command:first-conflict"),
            actor_id=StableIdentifier("actor:system"),
            occurred_at_utc="2026-04-01T00:00:00.000Z",
            monotonic_elapsed_ms=1,
            complete_derivation_barrier=True,
        )
        == first
    )
    assert calls


def _mock_history(
    seed,
    count: int,
    *,
    preserve_first_result: bool = False,
):
    rows = []
    for index in range(1, count + 1):
        result_key = (
            StableIdentifier("result:one")
            if preserve_first_result and index == 1
            else StableIdentifier(f"result:capacity-{index:03d}")
        )
        rows.append(
            replace(
                seed,
                result_key=result_key,
                source_global_sequence=index,
                observation_digest=canonical_digest({"capacity_history": index}),
            )
        )
    return rows


def test_service_computes_and_admits_exact_256_128_512_capacity_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalidated = tuple(StableIdentifier(f"field:capacity-{index:03d}") for index in range(128))
    service, signer = _service(tmp_path, {1, 256}, invalidated, reactions=512)
    seed_sealed = _seal(_admitted(40_000, 1), 1, signer)
    _react(service, seed_sealed, "capacity-boundary-seed")
    seed = CapabilityAdmissionVerifier(IntegrityTrustStore((signer.identity,))).verify(seed_sealed)
    history = _mock_history(seed, 255, preserve_first_result=True)
    monkeypatch.setattr(service, "_history", lambda *_args, **_kwargs: history)

    receipt = _react(
        service,
        _seal(_admitted(39_000, 2), 256, signer),
        "capacity-exact-boundary",
    )
    assert receipt.capacity.admitted
    assert (
        receipt.capacity.lineage_rows,
        receipt.capacity.invalidated_work,
        receipt.capacity.mandatory_reactions,
    ) == (256, 128, 512)
    assert receipt.invalidated_unissued_work == invalidated
    assert receipt.after_state is not None and receipt.after_state.observation_count == 255


def _real_reaction_capacity_fixture(tmp_path: Path, completed_reactions: int):
    from tests.v3.integration.test_derivation_barrier import (
        ACTOR,
        NOW,
        _bootstrap,
        _result_source,
        _submission,
    )

    lifecycle, _heat, field = _bootstrap(tmp_path)
    lifecycle.record_live_result(
        _submission(field, "a", ResultStatus.COMPLETION),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:u9-capacity-result-a-r1"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    lifecycle.record_live_result(
        _submission(field, "b", ResultStatus.DNS),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:u9-capacity-result-b-r1"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    lifecycle.settle_live_race(
        field,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:u9-capacity-settle-r1"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=5,
    )
    signer = P256EphemeralSigner.generate(f"u9-real-capacity-{completed_reactions}")
    trust = IntegrityTrustStore((signer.identity,))
    capacity = seal_capability_capacity(
        capability_module.CapabilityCapacityEnvelope(), signer=signer, created_at=NOW
    )
    service = CapabilityReactionService(
        lifecycle.projections.database_path,
        verifier=CapabilityAdmissionVerifier(trust),
        capacity=capacity,
        capacity_verifier=CapabilityCapacityVerifier(trust),
    )

    def sealed_result(source_sequence: int):
        with open_v3_connection(lifecycle.projections.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT result.result_key, result.observation_json, event.event_digest "
                "FROM v3_result_revisions result JOIN v3_events event "
                "ON event.global_sequence=result.source_global_sequence "
                "WHERE result.source_global_sequence=?",
                (source_sequence,),
            ).fetchone()
        assert row is not None
        observation = ResultObservation.from_dict(json.loads(str(row[1])))
        admitted = AdmittedEvidence(
            observation,
            EvidenceSource.LIVE_ISSUED_RACE,
            True,
            observation.result.raw_time_ms,
            AdmissionReason.ELIGIBLE_COMPLETION,
        )
        return seal_capability_admission(
            admitted=admitted,
            result_key=StableIdentifier(str(row[0])),
            source_global_sequence=source_sequence,
            authority_digest=str(row[2]),
            prior=CapabilityPrior.from_median_seconds("40", calibrated_beta="0.12"),
            evidence_log_variance="0.0025",
            conversion_log_variance="0",
            effective_weight="1",
            historical_binding=None,
            signer=signer,
            created_at=NOW,
        )

    sources = []
    first_source = _result_source(lifecycle, field, "a", 1)
    sources.append(first_source)
    _react(service, sealed_result(first_source), "u9-real-capacity-r1")
    for revision in range(2, 86):
        lifecycle.record_live_result(
            _submission(field, "a", ResultStatus.COMPLETION, revision=revision),
            field_revision=1,
            claimed_receipt_id=StableIdentifier("receipt:heat-a"),
            command_id=IdempotencyKey(f"command:u9-capacity-result-a-r{revision}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=5 + revision,
        )
        source = _result_source(lifecycle, field, "a", revision)
        sources.append(source)
        _react(service, sealed_result(source), f"u9-real-capacity-r{revision}")
    for reaction in tuple(capability_module.MandatoryReaction)[:completed_reactions]:
        lifecycle.complete_derivation_reaction(
            first_source,
            reaction,
            canonical_digest({"source": first_source, "reaction": reaction.value}),
            command_id=IdempotencyKey(f"command:u9-capacity-complete-{reaction.value}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=100,
        )
    lifecycle.record_live_result(
        _submission(field, "a", ResultStatus.COMPLETION, revision=86),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:u9-capacity-result-a-r86"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=101,
    )
    final_source = _result_source(lifecycle, field, "a", 86)
    sources.append(final_source)
    return lifecycle, service, capacity, trust, sealed_result(final_source), tuple(sources)


def test_real_durable_reaction_capacity_is_causally_scoped_and_restart_safe(
    tmp_path: Path,
) -> None:
    lifecycle, service, capacity, trust, sealed, sources = _real_reaction_capacity_fixture(
        tmp_path, 3
    )
    evidence = CapabilityAdmissionVerifier(trust).verify(sealed)
    authority = SQLiteCapabilityAuthority(lifecycle.projections.database_path)
    assert authority.mandatory_reaction_count(evidence, sources, ()) == 513
    with open_v3_connection(lifecycle.projections.database_path, read_only=True) as connection:
        total_pending = connection.execute(
            "SELECT COUNT(*) FROM v3_derivation_reactions pending WHERE state='pending' "
            "AND NOT EXISTS (SELECT 1 FROM v3_derivation_reactions completed "
            "WHERE completed.source_global_sequence=pending.source_global_sequence "
            "AND completed.reaction_type=pending.reaction_type AND completed.state='completed')"
        ).fetchone()[0]
        before_events = connection.execute(
            "SELECT COUNT(*) FROM v3_events WHERE event_kind IN (?, ?)",
            (
                capability_module.EventKind.CAPABILITY_UPDATED.value,
                capability_module.EventKind.CAPABILITY_STATE_REBASED.value,
            ),
        ).fetchone()[0]
    assert total_pending == 513 + len(tuple(capability_module.MandatoryReaction))
    command_id = IdempotencyKey("command:u9-real-capacity-final-513")
    kwargs = {
        "command_id": command_id,
        "actor_id": StableIdentifier("actor:system"),
        "occurred_at_utc": "2026-04-01T00:00:00.000Z",
        "monotonic_elapsed_ms": 102,
        "complete_derivation_barrier": False,
    }
    rejected = service.react(sealed, **kwargs)
    assert rejected.capacity.mandatory_reactions == 513
    assert not rejected.capacity.admitted
    assert not rejected.capacity.next_round_barrier_open
    restarted = CapabilityReactionService(
        lifecycle.projections.database_path,
        verifier=CapabilityAdmissionVerifier(trust),
        capacity=capacity,
        capacity_verifier=CapabilityCapacityVerifier(trust),
    )
    assert restarted.react(sealed, **kwargs).to_dict() == rejected.to_dict()
    with open_v3_connection(lifecycle.projections.database_path, read_only=True) as connection:
        rejected_events = connection.execute(
            "SELECT COUNT(*) FROM v3_events WHERE event_kind IN (?, ?)",
            (
                capability_module.EventKind.CAPABILITY_UPDATED.value,
                capability_module.EventKind.CAPABILITY_STATE_REBASED.value,
            ),
        ).fetchone()[0]
    assert rejected_events == before_events

    fourth = tuple(capability_module.MandatoryReaction)[3]
    lifecycle.complete_derivation_reaction(
        sources[0],
        fourth,
        canonical_digest({"source": sources[0], "reaction": fourth.value}),
        command_id=IdempotencyKey(f"command:u9-capacity-complete-{fourth.value}"),
        actor_id=StableIdentifier("actor:system"),
        occurred_at_utc="2026-04-01T00:00:00.000Z",
        monotonic_elapsed_ms=103,
    )
    assert authority.mandatory_reaction_count(evidence, sources, ()) == 512
    admitted_service = CapabilityReactionService(
        lifecycle.projections.database_path,
        verifier=CapabilityAdmissionVerifier(trust),
        capacity=capacity,
        capacity_verifier=CapabilityCapacityVerifier(trust),
    )
    admitted_kwargs = {**kwargs, "command_id": IdempotencyKey("command:u9-real-capacity-final-512")}
    receipt = admitted_service.react(sealed, **admitted_kwargs)
    assert receipt.capacity.admitted and receipt.capacity.mandatory_reactions == 512
    final_service = CapabilityReactionService(
        lifecycle.projections.database_path,
        verifier=CapabilityAdmissionVerifier(trust),
        capacity=capacity,
        capacity_verifier=CapabilityCapacityVerifier(trust),
    )
    assert final_service.react(sealed, **admitted_kwargs).to_dict() == receipt.to_dict()
    state = final_service.replay_active_state(evidence.competitor_id, evidence.context_digest)
    assert state is not None and receipt.after_state is not None
    assert state.state_digest == receipt.after_state.state_digest
    with open_v3_connection(lifecycle.projections.database_path, read_only=True) as connection:
        final_events = connection.execute(
            "SELECT COUNT(*) FROM v3_events WHERE event_kind IN (?, ?)",
            (
                capability_module.EventKind.CAPABILITY_UPDATED.value,
                capability_module.EventKind.CAPABILITY_STATE_REBASED.value,
            ),
        ).fetchone()[0]
        final_capability = connection.execute(
            "SELECT COUNT(*) FROM v3_derivation_reactions WHERE source_global_sequence=? "
            "AND reaction_type=? AND state='completed'",
            (evidence.source_global_sequence, capability_module.MandatoryReaction.CAPABILITY.value),
        ).fetchone()[0]
    assert final_events == before_events + 1
    assert final_capability == 0


@pytest.mark.parametrize(
    ("case", "history_count", "invalidated_count", "reactions", "expected_reason"),
    (
        ("lineage", 256, 0, 1, "lineage_capacity_exceeded"),
        ("invalidation", 1, 129, 1, "invalidation_capacity_exceeded"),
        ("reaction", 0, 0, 513, "reaction_capacity_exceeded"),
    ),
)
def test_each_one_beyond_capacity_is_evidence_preserving_and_durably_barrier_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    history_count: int,
    invalidated_count: int,
    reactions: int,
    expected_reason: str,
) -> None:
    source_sequence = history_count + 1
    invalidated = tuple(
        StableIdentifier(f"field:overflow-{index:03d}") for index in range(invalidated_count)
    )
    accepted = {source_sequence, 1} if invalidated_count else {source_sequence}
    service, signer = _service(tmp_path / case, accepted, invalidated, reactions=reactions)
    seed_sealed = _seal(_admitted(40_000, 1), 1, signer)
    if invalidated_count:
        _react(service, seed_sealed, f"capacity-{case}-seed")
    seed = CapabilityAdmissionVerifier(IntegrityTrustStore((signer.identity,))).verify(seed_sealed)
    is_correction = invalidated_count > 0
    history = _mock_history(seed, history_count, preserve_first_result=is_correction)
    monkeypatch.setattr(service, "_history", lambda *_args, **_kwargs: history)
    monkeypatch.setattr(capability_module, "replay_capability", lambda _rows: None)
    sealed = _seal(
        _admitted(39_000, 2 if is_correction else 1, result_key="result:one"),
        source_sequence,
        signer,
    )
    command_id = IdempotencyKey(f"command:capacity-{case}")
    kwargs = {
        "command_id": command_id,
        "actor_id": StableIdentifier("actor:system"),
        "occurred_at_utc": "2026-04-01T00:00:00.000Z",
        "monotonic_elapsed_ms": 1,
        "complete_derivation_barrier": True,
    }
    receipt = service.react(sealed, **kwargs)
    assert not receipt.capacity.admitted
    assert receipt.capacity.evidence_preserved
    assert not receipt.capacity.next_round_barrier_open
    assert receipt.capacity.reason == expected_reason
    assert service.react(sealed, **kwargs).to_dict() == receipt.to_dict()
    with open_v3_connection(service._events.database_path, read_only=True) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM v3_events WHERE event_kind IN (?, ?)",
            (
                capability_module.EventKind.CAPABILITY_UPDATED.value,
                capability_module.EventKind.CAPABILITY_STATE_REBASED.value,
            ),
        ).fetchone()[0]
        reaction_count = connection.execute(
            "SELECT COUNT(*) FROM v3_derivation_reactions WHERE source_global_sequence=?",
            (source_sequence,),
        ).fetchone()[0]
    assert event_count == (1 if is_correction else 0)
    assert reaction_count == 0


def test_unsorted_invalidation_and_idempotency_material_conflict(tmp_path: Path) -> None:
    service, signer = _service(
        tmp_path,
        {1, 2},
        (StableIdentifier("field:z"), StableIdentifier("field:a")),
    )
    _react(service, _seal(_admitted(40_000, 1), 1, signer), "first")
    with pytest.raises(CapabilityReactionError, match="sorted and unique"):
        _react(service, _seal(_admitted(39_000, 2), 2, signer), "correction")
    other = _seal(
        _admitted(41_000, 1, result_key="result:other"), 2, signer, result_key="result:other"
    )
    with pytest.raises(Exception, match="different signed admission"):
        service.react(
            other,
            command_id=IdempotencyKey("command:first"),
            actor_id=StableIdentifier("actor:system"),
            occurred_at_utc="2026-04-01T00:00:00.000Z",
            monotonic_elapsed_ms=1,
            complete_derivation_barrier=False,
        )


def test_new_reaction_completion_and_private_history_corruption_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, signer = _service(tmp_path, {1})
    calls = []
    monkeypatch.setattr(service, "_complete_barrier", lambda *args: calls.append(args))
    receipt = service.react(
        _seal(_admitted(40_000, 1), 1, signer),
        command_id=IdempotencyKey("command:barrier-new"),
        actor_id=StableIdentifier("actor:system"),
        occurred_at_utc="2026-04-01T00:00:00.000Z",
        monotonic_elapsed_ms=1,
        complete_derivation_barrier=True,
    )
    assert calls

    original_event_envelope = capability_module.EventEnvelope
    monkeypatch.setattr(
        capability_module,
        "open_v3_connection",
        lambda *_args, **_kwargs: _FakeContext(_FakeConnection([_FakeCursor(rows=(("{}",),))])),
    )

    class _EnvelopeFactory:
        payload = object()

        @classmethod
        def from_dict(cls, _value):
            return SimpleNamespace(command=SimpleNamespace(payload=cls.payload))

    from types import SimpleNamespace

    monkeypatch.setattr(capability_module, "EventEnvelope", _EnvelopeFactory)
    aggregate = service._aggregate_id(StableIdentifier("competitor:alice"), receipt.context_digest)
    with pytest.raises(CapabilityReactionError, match="inline"):
        service._history(aggregate)
    _EnvelopeFactory.payload = capability_module.InlinePayload.from_value(
        {"schema_version": "wrong", "evidence": {}, "receipt": {}}
    )
    with pytest.raises(CapabilityReactionError, match="malformed"):
        service._history(aggregate)
    _EnvelopeFactory.payload = capability_module.InlinePayload.from_value(
        {
            "schema_version": capability_module.CAPABILITY_REACTION_SCHEMA_VERSION,
            "evidence": {},
            "receipt": {"capacity": {"admitted": "yes"}},
        }
    )
    with pytest.raises(CapabilityReactionError, match="receipt"):
        service._history(aggregate)
    monkeypatch.setattr(capability_module, "EventEnvelope", original_event_envelope)


def test_direct_barrier_capacity_and_lifecycle_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, signer = _service(tmp_path, {1})
    receipt = _react(service, _seal(_admitted(40_000, 1), 1, signer), "barrier")
    rejected = capability_module.replace(
        receipt,
        capacity=capability_module.RebaseCapacityDecision(
            False,
            True,
            False,
            "lineage_capacity_exceeded",
            257,
            0,
            1,
            receipt.capacity.envelope_digest,
        ),
    )
    service._complete_barrier(
        rejected,
        StableIdentifier("actor:system"),
        "2026-04-01T00:00:00.000Z",
        1,
    )
    lifecycle_calls = []

    class _Lifecycle:
        def __init__(self, path):
            lifecycle_calls.append(path)

        def complete_derivation_reaction(self, *args, **kwargs):
            lifecycle_calls.append((args, kwargs))

    import strathmark.v3.application.lifecycle as lifecycle_module

    monkeypatch.setattr(lifecycle_module, "LifecycleService", _Lifecycle)
    service._complete_barrier(
        receipt,
        StableIdentifier("actor:system"),
        "2026-04-01T00:00:00.000Z",
        1,
    )
    assert len(lifecycle_calls) == 2


def test_capacity_and_authority_defensive_contract_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = P256EphemeralSigner.generate("capacity-defensive")
    trust = IntegrityTrustStore((signer.identity,))
    verifier = CapabilityCapacityVerifier(trust)
    envelope = capability_module.CapabilityCapacityEnvelope()
    with pytest.raises(CapabilityReactionError):
        SealedCapabilityCapacity("bad")  # type: ignore[arg-type]
    wrong_kind = _seal(_admitted(40_000, 1), 1, signer)
    with pytest.raises(CapabilityReactionError, match="kind differs"):
        SealedCapabilityCapacity(wrong_kind.manifest)
    with pytest.raises(CapabilityReactionError):
        seal_capability_capacity("bad", signer=signer, created_at="2026-04-01T00:00:00.000Z")  # type: ignore[arg-type]
    with pytest.raises(CapabilityReactionError):
        CapabilityCapacityVerifier("bad")  # type: ignore[arg-type]

    wrong_payload = SealedCapabilityCapacity(
        sign_manifest(
            capability_module.CAPABILITY_CAPACITY_MANIFEST_KIND,
            {"schema_version": "wrong", "envelope": {}},
            signer=signer,
            created_at="2026-04-01T00:00:00.000Z",
        )
    )
    with pytest.raises(CapabilityReactionError, match="not closed"):
        verifier.verify(wrong_payload)
    invalid_envelope = SealedCapabilityCapacity(
        sign_manifest(
            capability_module.CAPABILITY_CAPACITY_MANIFEST_KIND,
            {"schema_version": "strathmark-v3-capability-capacity-manifest-v1", "envelope": {}},
            signer=signer,
            created_at="2026-04-01T00:00:00.000Z",
        )
    )
    with pytest.raises(CapabilityReactionError, match="envelope is invalid"):
        verifier.verify(invalid_envelope)

    sealed_capacity = seal_capability_capacity(
        envelope, signer=signer, created_at="2026-04-01T00:00:00.000Z"
    )
    with pytest.raises(CapabilityReactionError):
        CapabilityReactionService(
            tmp_path / "bad-capacity-verifier.sqlite3",
            verifier=CapabilityAdmissionVerifier(trust),
            capacity=sealed_capacity,
            capacity_verifier="bad",  # type: ignore[arg-type]
        )

    no_binding = SimpleNamespace(
        source=EvidenceSource.HISTORICAL_IMPORT,
        historical_binding=None,
    )
    with pytest.raises(CapabilityReactionError, match="no row binding"):
        SQLiteCapabilityAuthority._verify(_FakeConnection([]), no_binding)  # type: ignore[arg-type]

    live = CapabilityAdmissionVerifier(trust).verify(wrong_kind)
    binding = HistoricalImportBinding(
        "v2import:" + "1" * 64,
        canonical_digest({"row": 1}),
        "2026-03-31T23:59:59.999Z",
        "a" * 64,
        "f" * 64,
    )
    historical = replace(
        live,
        source=EvidenceSource.HISTORICAL_IMPORT,
        admission_reason=AdmissionReason.HISTORICAL_CUTOVER,
        historical_binding=binding,
    )
    malformed_row = (
        historical.authority_digest,
        binding.import_id,
        binding.source_cutoff,
        "c" * 64,
        "d" * 64,
        "{",
        1,
        1,
        binding.cutover_manifest_digest,
        binding.cutover_manifest_digest,
    )
    with pytest.raises(CapabilityReactionError, match="not canonical JSON"):
        SQLiteCapabilityAuthority._verify(
            _FakeConnection([_FakeCursor(one=malformed_row)]), historical
        )


def test_real_u5_multi_event_correction_returns_only_dependent_unissued_work(
    tmp_path: Path,
) -> None:
    from strathmark.v3.application.lifecycle import LifecycleService, SnapshotKind, UpstreamSnapshot
    from strathmark.v3.contracts.commands import CommandKind
    from strathmark.v3.contracts.events import AggregateKind, EventKind
    from tests.v3.integration.test_derivation_barrier import (
        ACTOR,
        NOW,
        _append,
        _complete_source,
        _result_source,
        _snapshot,
        _start_round_close,
        _submission,
    )

    lifecycle = LifecycleService(tmp_path / "authority.sqlite3")
    tournament = StableIdentifier("tournament:show")
    heat = StableIdentifier("round:heat")
    heat_field = StableIdentifier("field:heat-a")
    unrelated_round = StableIdentifier("round:unrelated")
    context = TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1")
    _snapshot(
        lifecycle,
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
        ),
        "u9-tournament-snapshot",
    )
    for round_id, predecessors, successors in (
        (heat, [], ["round:issued", "round:unissued"]),
        (unrelated_round, [], []),
    ):
        _snapshot(
            lifecycle,
            UpstreamSnapshot(
                SnapshotKind.ROUND,
                round_id,
                1,
                tournament,
                round_id,
                {
                    "round_ordinal": 1,
                    "predecessor_round_ids": predecessors,
                    "successor_round_ids": successors,
                },
            ),
            f"u9-{round_id}-snapshot",
        )
        _append(
            lifecycle,
            CommandKind.CONFIGURE_ROUND,
            EventKind.ROUND_CONFIGURED,
            AggregateKind.ROUND,
            round_id,
            {"configured": True},
            f"u9-{round_id}-configure",
        )
    _append(
        lifecycle,
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        tournament,
        {"configured": True},
        "u9-configure-show",
    )
    lifecycle.open_tournament(
        tournament,
        bundle_id=StableIdentifier("bundle:verified"),
        historical_cutoff_key="history:prior",
        root_round_ids=(heat, unrelated_round),
        command_id=IdempotencyKey("command:u9-open-show"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    root_epoch, _root_stored = lifecycle.freeze_round_epoch(
        heat,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(),
        command_id=IdempotencyKey("command:u9-freeze-root"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    _snapshot(
        lifecycle,
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            heat_field,
            1,
            tournament,
            heat,
            {
                "competitor_ids": ["competitor:a", "competitor:b"],
                "target_context": context.to_dict(),
                "stand_ids": ["stand:one", "stand:two"],
            },
        ),
        "u9-heat-field-snapshot",
    )
    _append(
        lifecycle,
        CommandKind.OPTIMIZE_FIELD,
        EventKind.FIELD_OPTIMIZED,
        AggregateKind.FIELD,
        heat_field,
        {"round_id": str(heat), "epoch_id": str(root_epoch.epoch_id), "field_revision": 1},
        "u9-prepare-heat",
    )
    _append(
        lifecycle,
        CommandKind.ACKNOWLEDGE_ISSUE,
        EventKind.FIELD_ISSUED,
        AggregateKind.FIELD,
        heat_field,
        {
            "round_id": str(heat),
            "epoch_id": str(root_epoch.epoch_id),
            "field_revision": 1,
            "receipt_id": "receipt:heat-a",
            "competitor_ids": ["competitor:a", "competitor:b"],
            "issued_marks": {"competitor:a": 3, "competitor:b": 3},
        },
        "u9-issue-heat",
    )
    original = lifecycle.record_live_result(
        _submission(heat_field, "a", ResultStatus.COMPLETION),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:u9-real-result-a"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    second = lifecycle.record_live_result(
        _submission(heat_field, "b", ResultStatus.DNS),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:u9-real-result-b"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    lifecycle.settle_live_race(
        heat_field,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:u9-real-settle"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=5,
    )
    _complete_source(lifecycle, original.first_global_sequence, "u9-real-a")
    _complete_source(lifecycle, second.first_global_sequence, "u9-real-b")

    signer = P256EphemeralSigner.generate("real-u5-capability")
    trust = IntegrityTrustStore((signer.identity,))
    capacity = seal_capability_capacity(
        capability_module.CapabilityCapacityEnvelope(), signer=signer, created_at=NOW
    )
    capability = CapabilityReactionService(
        lifecycle.projections.database_path,
        verifier=CapabilityAdmissionVerifier(trust),
        capacity=capacity,
        capacity_verifier=CapabilityCapacityVerifier(trust),
    )

    def sealed_result(source_sequence: int):
        with open_v3_connection(lifecycle.projections.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT result.result_key, result.observation_json, event.event_digest "
                "FROM v3_result_revisions result JOIN v3_events event "
                "ON event.global_sequence=result.source_global_sequence "
                "WHERE result.source_global_sequence=?",
                (source_sequence,),
            ).fetchone()
        assert row is not None
        observation = ResultObservation.from_dict(json.loads(str(row[1])))
        admitted = AdmittedEvidence(
            observation,
            EvidenceSource.LIVE_ISSUED_RACE,
            True,
            observation.result.raw_time_ms,
            AdmissionReason.ELIGIBLE_COMPLETION,
        )
        return seal_capability_admission(
            admitted=admitted,
            result_key=StableIdentifier(str(row[0])),
            source_global_sequence=source_sequence,
            authority_digest=str(row[2]),
            prior=CapabilityPrior.from_median_seconds("40", calibrated_beta="0.12"),
            evidence_log_variance="0.0025",
            conversion_log_variance="0",
            effective_weight="1",
            historical_binding=None,
            signer=signer,
            created_at=NOW,
        )

    original_sealed = sealed_result(original.first_global_sequence)
    _react(capability, original_sealed, "u9-real-capability-original")

    _start_round_close(lifecycle, heat, "u9-real-heat")
    closure, _stored = lifecycle.close_evidence_round(
        heat,
        command_id=IdempotencyKey("command:u9-real-close"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=11,
    )

    def prepare_round(round_name: str, field_name: str, *, issue: bool, dependent: bool) -> None:
        round_id = StableIdentifier(f"round:{round_name}")
        field_id = StableIdentifier(f"field:{field_name}")
        if dependent:
            _snapshot(
                lifecycle,
                UpstreamSnapshot(
                    SnapshotKind.ROUND,
                    round_id,
                    1,
                    StableIdentifier("tournament:show"),
                    round_id,
                    {
                        "round_ordinal": 2,
                        "predecessor_round_ids": [str(heat)],
                        "successor_round_ids": [],
                    },
                ),
                f"u9-{round_name}-snapshot",
            )
            _append(
                lifecycle,
                CommandKind.CONFIGURE_ROUND,
                EventKind.ROUND_CONFIGURED,
                AggregateKind.ROUND,
                round_id,
                {"configured": True},
                f"u9-{round_name}-configure",
            )
        epoch, _receipt = lifecycle.freeze_round_epoch(
            round_id,
            epoch_revision=1,
            historical_cutoff_key="history:prior",
            closure_ids=(closure,) if dependent else (),
            command_id=IdempotencyKey(f"command:u9-{round_name}-freeze"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=12,
        )
        _snapshot(
            lifecycle,
            UpstreamSnapshot(
                SnapshotKind.FIELD,
                field_id,
                1,
                StableIdentifier("tournament:show"),
                round_id,
                {
                    "competitor_ids": ["competitor:a"],
                    "target_context": context.to_dict(),
                    "stand_ids": ["stand:one"],
                },
            ),
            f"u9-{field_name}-snapshot",
        )
        _append(
            lifecycle,
            CommandKind.OPTIMIZE_FIELD,
            EventKind.FIELD_OPTIMIZED,
            AggregateKind.FIELD,
            field_id,
            {"round_id": str(round_id), "epoch_id": str(epoch.epoch_id), "field_revision": 1},
            f"u9-{field_name}-prepare",
        )
        if issue:
            _append(
                lifecycle,
                CommandKind.ACKNOWLEDGE_ISSUE,
                EventKind.FIELD_ISSUED,
                AggregateKind.FIELD,
                field_id,
                {
                    "round_id": str(round_id),
                    "epoch_id": str(epoch.epoch_id),
                    "field_revision": 1,
                    "receipt_id": f"receipt:{field_name}",
                    "competitor_ids": ["competitor:a"],
                    "issued_marks": {"competitor:a": 3},
                },
                f"u9-{field_name}-issue",
            )

    prepare_round("unissued", "dependent-unissued", issue=False, dependent=True)
    prepare_round("issued", "dependent-issued", issue=True, dependent=True)
    prepare_round("unrelated", "unrelated", issue=False, dependent=False)

    corrected = lifecycle.record_live_result(
        _submission(heat_field, "a", ResultStatus.COMPLETION, revision=2),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:u9-real-correction"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=13,
    )
    corrected_source = _result_source(lifecycle, heat_field, "a", 2)
    assert corrected.first_global_sequence != corrected_source
    corrected_sealed = sealed_result(corrected_source)
    with pytest.raises(CapabilityReactionError, match="latest settled issued result revision"):
        SQLiteCapabilityAuthority(lifecycle.projections.database_path).verify_source(
            CapabilityAdmissionVerifier(trust).verify(original_sealed)
        )
    receipt = _react(capability, corrected_sealed, "u9-real-capability-correction")
    assert receipt.invalidated_unissued_work == (StableIdentifier("field:dependent-unissued"),)
    assert receipt.capacity.mandatory_reactions == len(tuple(capability_module.MandatoryReaction))
