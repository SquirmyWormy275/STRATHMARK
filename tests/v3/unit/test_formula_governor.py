from __future__ import annotations

from dataclasses import replace

import pytest

from strathmark.v3.application.formula_governor import (
    FormulaGovernorError,
    FormulaHistoricalAuthority,
    FormulaLiveAuthority,
    FormulaProjectionFactory,
    HistoricalCutoverAuthority,
    SealedFormulaGovernorBatch,
    _validate_batch_scope,
    seal_formula_governor_batch,
)
from strathmark.v3.assessors.base import EvidenceQuality, FormulaInputPacket
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.evidence import (
    ContextProperty,
    EvidencePacket,
    ResultObservation,
    TargetContext,
)
from strathmark.v3.contracts.identifiers import StableIdentifier, deterministic_identifier
from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
from strathmark.v3.domain.epochs import EpochMember, EvidenceEpoch, ReactionBarrier, freeze_epoch
from strathmark.v3.domain.evidence import (
    AdmissionReason,
    AdmittedEvidence,
    EvidenceSource,
    IssuedFieldFact,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
    sign_key_rotation,
    sign_manifest,
)

CUTOFF = "2026-08-29T12:00:00.000Z"
HISTORY = "history:2026-08-01"
ACTIVE = StableIdentifier("tournament:show-a")


def _context() -> TargetContext:
    return TargetContext(
        "underhand",
        300,
        "eucalyptus",
        "taxonomy:v1",
        "conversion:v1",
        (ContextProperty("density", "720", "kg_m3", None),),
    )


def _observation(*, sequence: int = 1, historical: bool = False) -> ResultObservation:
    tournament = "archive-a" if historical else "show-a"
    return ResultObservation(
        StableIdentifier(f"evidence:result-{sequence}"),
        StableIdentifier("competitor:opaque-a"),
        StableIdentifier(f"tournament:{tournament}"),
        StableIdentifier("round:heat"),
        StableIdentifier(f"field:field-{sequence}"),
        _context(),
        sequence,
        "2026-08-20T12:00:00.000Z",
        3,
        None,
        None,
        None,
        OfficialResult(ResultStatus.COMPLETION, 40000, None, 1, None),
        f"{sequence:064x}",
    )


def _result_key(item: ResultObservation, revision: int = 1) -> StableIdentifier:
    return deterministic_identifier(
        "result",
        {
            "field_id": str(item.field_id),
            "field_revision": revision,
            "competitor_id": str(item.competitor_id),
        },
    )


def _epoch(item: ResultObservation, **member_changes: object) -> EvidenceEpoch:
    member = replace(
        EpochMember(
            str(_result_key(item)),
            item.result.revision,
            item.observation_sequence,
            True,
        ),
        **member_changes,
    )
    maximum = max(item.observation_sequence, member.source_sequence)
    return freeze_epoch(
        round_id=item.round_id,
        epoch_revision=1,
        historical_cutoff_key=HISTORY,
        closed_through_sequence=maximum,
        members=(member,),
        barrier=ReactionBarrier.complete_through(maximum),
    )


def _packet(item: ResultObservation, epoch: EvidenceEpoch) -> EvidencePacket:
    return EvidencePacket.create(
        competitor_id=item.competitor_id,
        target_context=item.context,
        observations=(item,),
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
        historical_cutoff_key=HISTORY,
        tournament_epoch_id=epoch.epoch_id,
        tournament_event_sequence=epoch.maximum_tournament_sequence,
    )


def _live(item: ResultObservation) -> FormulaLiveAuthority:
    receipt = StableIdentifier("receipt:issued-field")
    return FormulaLiveAuthority(
        item.evidence_id,
        IssuedFieldFact(
            item.field_id,
            1,
            (item.competitor_id,),
            receipt,
            item.tournament_id,
            item.round_id,
            item.context,
            ((item.competitor_id, item.issued_mark),),
        ),
        1,
        receipt,
    )


def _historical(item: ResultObservation) -> FormulaHistoricalAuthority:
    return FormulaHistoricalAuthority(
        item.evidence_id,
        _result_key(item),
        HistoricalCutoverAuthority(
            StableIdentifier("historical_cutover:verified-import"),
            HISTORY,
            canonical_digest({"cutover": HISTORY, "source": "archive:v1"}),
        ),
    )


def _seal(
    item: ResultObservation,
    epoch: EvidenceEpoch,
    packet: EvidencePacket,
    signer: P256EphemeralSigner,
    *,
    historical: bool = False,
    cutoff: str = CUTOFF,
) -> SealedFormulaGovernorBatch:
    return seal_formula_governor_batch(
        evidence=packet,
        epoch=epoch,
        cutoff_at_utc=cutoff,
        active_tournament_id=ACTIVE,
        authoritative_tournament_ids=(StableIdentifier("tournament:archive-a"),),
        legacy_tournament_ids=(),
        live_authorities=() if historical else (_live(item),),
        historical_authorities=(_historical(item),) if historical else (),
        signer=signer,
        created_at=CUTOFF,
    )


def _factory(signer: P256EphemeralSigner, *, cutoff: str = CUTOFF) -> FormulaProjectionFactory:
    return FormulaProjectionFactory(
        trust_store=IntegrityTrustStore((signer.identity,)),
        cutoff_at_utc=cutoff,
        active_tournament_id=ACTIVE,
        authoritative_tournament_ids=(StableIdentifier("tournament:archive-a"),),
        legacy_tournament_ids=(),
    )


def _resign_payload(
    sealed: SealedFormulaGovernorBatch,
    signer: P256EphemeralSigner,
    mutate: object,
) -> SealedFormulaGovernorBatch:
    payload = sealed.manifest.body()["payload"]
    assert isinstance(payload, dict)
    mutate(payload)  # type: ignore[operator]
    return SealedFormulaGovernorBatch(
        sign_manifest(sealed.manifest.kind, payload, signer=signer, created_at=CUTOFF)
    )


def test_trusted_live_and_historical_batches_derive_formula_facts() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:governor")
    for historical, expected_quality in (
        (False, EvidenceQuality.ISSUED_OFFICIAL),
        (True, EvidenceQuality.VERIFIED_HISTORICAL),
    ):
        item = _observation(historical=historical)
        epoch = _epoch(item)
        packet = _packet(item, epoch)
        sealed = _seal(item, epoch, packet, signer, historical=historical)
        projected = _factory(signer).project(evidence=packet, epoch=epoch, sealed_batch=sealed)
        assert projected.observation_facts[0].quality is expected_quality
        assert projected.governor_receipt.signer_key_id == signer.identity.key_id
        assert projected.governor_receipt.signed_manifest_body_digest == sealed.manifest.body_digest
        assert projected.governor_receipt.tournament_epoch_content_digest == epoch.content_digest


def test_live_to_historical_rewrap_requires_the_pinned_governor_key() -> None:
    trusted = P256EphemeralSigner.generate("integrity-key:trusted")
    attacker = P256EphemeralSigner.generate("integrity-key:attacker")
    item = _observation(historical=True)
    epoch = _epoch(item)
    packet = _packet(item, epoch)
    rewrapped = _seal(item, epoch, packet, attacker, historical=True)
    with pytest.raises(FormulaGovernorError, match="invalid or untrusted"):
        _factory(trusted).project(evidence=packet, epoch=epoch, sealed_batch=rewrapped)


def test_attacker_trust_substitution_cannot_change_an_existing_pinned_factory() -> None:
    trusted = P256EphemeralSigner.generate("integrity-key:trusted")
    attacker = P256EphemeralSigner.generate("integrity-key:attacker")
    factory = _factory(trusted)
    item = _observation()
    epoch = _epoch(item)
    packet = _packet(item, epoch)
    attacked = _seal(item, epoch, packet, attacker)
    _factory(attacker).project(evidence=packet, epoch=epoch, sealed_batch=attacked)
    with pytest.raises(FormulaGovernorError, match="invalid or untrusted"):
        factory.project(evidence=packet, epoch=epoch, sealed_batch=attacked)


def test_cutoff_timestamp_change_with_the_same_key_breaks_factory_binding() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:governor")
    item = _observation()
    epoch = _epoch(item)
    packet = _packet(item, epoch)
    changed = _seal(item, epoch, packet, signer, cutoff="2026-08-30T12:00:00.000Z")
    with pytest.raises(FormulaGovernorError, match="cutoff_at_utc binding differs"):
        _factory(signer).project(evidence=packet, epoch=epoch, sealed_batch=changed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", EvidenceSource.HISTORICAL_IMPORT.value),
        ("reason", AdmissionReason.HISTORICAL_CUTOVER.value),
        ("source_digest", "f" * 64),
        ("raw_time_ms", 41000),
    ],
)
def test_trusted_signature_still_rejects_forged_origin_and_provenance(
    field: str, value: object
) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:governor")
    item = _observation()
    epoch = _epoch(item)
    packet = _packet(item, epoch)
    sealed = _seal(item, epoch, packet, signer)

    def mutate(payload: dict[str, object]) -> None:
        admissions = payload["admissions"]
        assert isinstance(admissions, list)
        admissions[0][field] = value

    forged = _resign_payload(sealed, signer, mutate)
    with pytest.raises(FormulaGovernorError):
        _factory(signer).project(evidence=packet, epoch=epoch, sealed_batch=forged)


@pytest.mark.parametrize(
    "member_change",
    [
        {"source_sequence": 2},
        {"revision": 2},
        {"numeric_eligible": False},
        {"result_key": "result:absent"},
    ],
)
def test_epoch_member_identity_sequence_revision_and_eligibility_are_bound(
    member_change: dict[str, object],
) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:governor")
    item = _observation()
    epoch = _epoch(item)
    packet = _packet(item, epoch)
    sealed = _seal(item, epoch, packet, signer)
    changed_epoch = _epoch(item, **member_change)
    with pytest.raises(FormulaGovernorError, match="epoch identity"):
        _factory(signer).project(evidence=packet, epoch=changed_epoch, sealed_batch=sealed)


def test_epoch_content_digest_is_in_the_signed_scope() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:governor")
    item = _observation()
    epoch = _epoch(item)
    packet = _packet(item, epoch)
    sealed = _seal(item, epoch, packet, signer)

    def mutate(payload: dict[str, object]) -> None:
        epoch_value = payload["epoch"]
        assert isinstance(epoch_value, dict)
        epoch_value["content_digest"] = "f" * 64

    forged = _resign_payload(sealed, signer, mutate)
    with pytest.raises(FormulaGovernorError, match="epoch binding differs"):
        _factory(signer).project(evidence=packet, epoch=epoch, sealed_batch=forged)


def test_trusted_key_rotation_verifies_old_and_new_governor_batches() -> None:
    old = P256EphemeralSigner.generate("integrity-key:governor-old")
    new = P256EphemeralSigner.generate("integrity-key:governor-new")
    item = _observation()
    epoch = _epoch(item)
    packet = _packet(item, epoch)
    old_batch = _seal(item, epoch, packet, old)
    new_batch = _seal(item, epoch, packet, new)
    rotation = sign_key_rotation(old, new.identity, created_at=CUTOFF)
    rotated = _factory(old).with_rotation(rotation)
    assert rotated.project(evidence=packet, epoch=epoch, sealed_batch=old_batch)
    assert rotated.project(evidence=packet, epoch=epoch, sealed_batch=new_batch)


def test_public_boundary_rejects_preclassified_admissions_and_direct_formula_inputs() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:governor")
    item = _observation()
    epoch = _epoch(item)
    packet = _packet(item, epoch)
    preclassified = AdmittedEvidence(
        item,
        EvidenceSource.LIVE_ISSUED_RACE,
        True,
        40000,
        AdmissionReason.ELIGIBLE_COMPLETION,
    )
    with pytest.raises(FormulaGovernorError, match="immutable typed tuple"):
        seal_formula_governor_batch(
            evidence=packet,
            epoch=epoch,
            cutoff_at_utc=CUTOFF,
            active_tournament_id=ACTIVE,
            authoritative_tournament_ids=(StableIdentifier("tournament:archive-a"),),
            legacy_tournament_ids=(),
            live_authorities=(preclassified,),  # type: ignore[arg-type]
            historical_authorities=(),
            signer=signer,
            created_at=CUTOFF,
        )
    with pytest.raises(ValueError, match="verified governor projection"):
        FormulaInputPacket(packet, object(), ())


def test_historical_cutover_key_is_bound_inside_the_trusted_batch() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:governor")
    item = _observation(historical=True)
    epoch = _epoch(item)
    packet = _packet(item, epoch)
    sealed = _seal(item, epoch, packet, signer, historical=True)

    def mutate(payload: dict[str, object]) -> None:
        admissions = payload["admissions"]
        assert isinstance(admissions, list)
        admissions[0]["authority_reference"]["historical_cutoff_key"] = "history:other"

    forged = _resign_payload(sealed, signer, mutate)
    with pytest.raises(FormulaGovernorError, match="historical cutoff authority differs"):
        _factory(signer).project(evidence=packet, epoch=epoch, sealed_batch=forged)


def test_formula_projection_factory_exists_only_at_the_signed_governor_boundary() -> None:
    assert FormulaProjectionFactory.__module__ == "strathmark.v3.application.formula_governor"


def test_signed_governor_public_contract_rejects_all_untyped_or_ambiguous_authority() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:governor")
    item = _observation()
    epoch = _epoch(item)
    packet = _packet(item, epoch)
    live = _live(item)
    historical = _historical(item)
    with pytest.raises(FormulaGovernorError, match="IssuedFieldFact"):
        FormulaLiveAuthority(item.evidence_id, object(), 1, live.claimed_receipt_id)  # type: ignore[arg-type]
    for revision in (0, True, "1"):
        with pytest.raises(FormulaGovernorError, match="revision must be positive"):
            FormulaLiveAuthority(
                item.evidence_id,
                live.issued_field,
                revision,  # type: ignore[arg-type]
                live.claimed_receipt_id,
            )
    with pytest.raises(FormulaGovernorError, match="cutover authority"):
        FormulaHistoricalAuthority(item.evidence_id, _result_key(item), object())  # type: ignore[arg-type]
    with pytest.raises(FormulaGovernorError, match="SignedManifest"):
        SealedFormulaGovernorBatch(object())  # type: ignore[arg-type]
    wrong_kind = sign_manifest("other", {}, signer=signer, created_at=CUTOFF)
    with pytest.raises(FormulaGovernorError, match="wrong manifest kind"):
        SealedFormulaGovernorBatch(wrong_kind)
    assert _seal(item, epoch, packet, signer).to_dict()["kind"] == "formula_governor_batch"

    common = {
        "evidence": packet,
        "epoch": epoch,
        "cutoff_at_utc": CUTOFF,
        "active_tournament_id": ACTIVE,
        "authoritative_tournament_ids": (StableIdentifier("tournament:archive-a"),),
        "legacy_tournament_ids": (),
        "signer": signer,
        "created_at": CUTOFF,
    }
    with pytest.raises(FormulaGovernorError, match="historical Formula authorities"):
        seal_formula_governor_batch(
            **common,
            live_authorities=(live,),
            historical_authorities=(object(),),  # type: ignore[arg-type]
        )
    with pytest.raises(FormulaGovernorError, match="cannot repeat"):
        seal_formula_governor_batch(
            **common,
            live_authorities=(live,),
            historical_authorities=(historical,),
        )
    with pytest.raises(FormulaGovernorError, match="exactly cover"):
        seal_formula_governor_batch(**common, live_authorities=(), historical_authorities=())
    with pytest.raises(FormulaGovernorError, match="not authoritatively issued"):
        seal_formula_governor_batch(
            **common,
            live_authorities=(
                replace(
                    live,
                    claimed_receipt_id=StableIdentifier("receipt:untrusted"),
                ),
            ),
            historical_authorities=(),
        )
    wrong_cutover = replace(
        historical,
        cutover=replace(historical.cutover, historical_cutoff_key="history:other"),
    )
    with pytest.raises(FormulaGovernorError, match="packet cutoff"):
        seal_formula_governor_batch(
            **common,
            live_authorities=(),
            historical_authorities=(wrong_cutover,),
        )


def test_projection_factory_rejects_untrusted_rotation_shape_and_closed_payload_drift() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:governor")
    item = _observation()
    epoch = _epoch(item)
    packet = _packet(item, epoch)
    sealed = _seal(item, epoch, packet, signer)
    with pytest.raises(FormulaGovernorError, match="pinned trust store"):
        FormulaProjectionFactory(
            trust_store=object(),  # type: ignore[arg-type]
            cutoff_at_utc=CUTOFF,
            active_tournament_id=ACTIVE,
            authoritative_tournament_ids=(),
            legacy_tournament_ids=(),
        )
    with pytest.raises(FormulaGovernorError, match="not trusted"):
        _factory(signer).with_rotation(
            sign_manifest("not_rotation", {}, signer=signer, created_at=CUTOFF)
        )
    with pytest.raises(FormulaGovernorError, match="sealed governor batch"):
        _factory(signer).project(
            evidence=packet,
            epoch=epoch,
            sealed_batch=object(),  # type: ignore[arg-type]
        )

    def version(payload: dict[str, object]) -> None:
        payload["schema_version"] = "unsupported"

    with pytest.raises(FormulaGovernorError, match="version is unsupported"):
        _factory(signer).project(
            evidence=packet,
            epoch=epoch,
            sealed_batch=_resign_payload(sealed, signer, version),
        )

    def no_admissions(payload: dict[str, object]) -> None:
        payload["admissions"] = []

    with pytest.raises(FormulaGovernorError, match="exactly cover"):
        _factory(signer).project(
            evidence=packet,
            epoch=epoch,
            sealed_batch=_resign_payload(sealed, signer, no_admissions),
        )


def test_governor_scope_and_epoch_membership_fail_closed_independently() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:governor")
    item = _observation()
    epoch = _epoch(item)
    packet = _packet(item, epoch)
    with pytest.raises(FormulaGovernorError, match="typed evidence and epoch"):
        _validate_batch_scope(packet, object(), CUTOFF, ACTIVE, (), ())  # type: ignore[arg-type]

    def raw_epoch(**changes: object) -> EvidenceEpoch:
        value = object.__new__(EvidenceEpoch)
        for field in (
            "epoch_id",
            "round_id",
            "epoch_revision",
            "historical_cutoff_key",
            "maximum_tournament_sequence",
            "members",
            "content_digest",
        ):
            object.__setattr__(value, field, changes.get(field, getattr(epoch, field)))
        return value

    with pytest.raises(FormulaGovernorError, match="cutoff keys differ"):
        _validate_batch_scope(
            packet,
            raw_epoch(historical_cutoff_key="history:other"),
            CUTOFF,
            ACTIVE,
            (),
            (),
        )
    with pytest.raises(FormulaGovernorError, match="maximum sequences differ"):
        _validate_batch_scope(
            packet,
            raw_epoch(maximum_tournament_sequence=2),
            CUTOFF,
            ACTIVE,
            (),
            (),
        )
    for values, message in (
        (([], ()), "authoritative tournaments must be immutable"),
        (
            (
                (
                    StableIdentifier("tournament:z"),
                    StableIdentifier("tournament:a"),
                ),
                (),
            ),
            "unique and sorted",
        ),
        (((ACTIVE,), ()), "authority sets must be disjoint"),
    ):
        with pytest.raises(FormulaGovernorError, match=message):
            FormulaProjectionFactory(
                trust_store=IntegrityTrustStore((signer.identity,)),
                cutoff_at_utc=CUTOFF,
                active_tournament_id=ACTIVE,
                authoritative_tournament_ids=values[0],  # type: ignore[arg-type]
                legacy_tournament_ids=values[1],
            )

    empty_epoch = freeze_epoch(
        round_id=item.round_id,
        epoch_revision=1,
        historical_cutoff_key=HISTORY,
        closed_through_sequence=1,
        members=(),
        barrier=ReactionBarrier.complete_through(1),
    )
    with pytest.raises(FormulaGovernorError, match="absent from the signed epoch"):
        _seal(item, empty_epoch, _packet(item, empty_epoch), signer)
    ineligible_epoch = _epoch(item, numeric_eligible=False)
    with pytest.raises(FormulaGovernorError, match="differs from its signed epoch member"):
        _seal(item, ineligible_epoch, _packet(item, ineligible_epoch), signer)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda row: row.update(source="unknown"), "unknown closed value"),
        (lambda row: row.update(authority_reference="caller"), "reference is malformed"),
        (
            lambda row: row["authority_reference"].update(authority_digest="f" * 64),
            "live authority reference was forged",
        ),
        (lambda row: row.update(result_key="result:other"), "result identity differs"),
        (lambda row: row.pop("reason"), "unknown or missing fields"),
    ],
)
def test_signed_live_admission_payload_still_obeys_closed_internal_contract(
    mutation: object, message: str
) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:governor")
    item = _observation()
    epoch = _epoch(item)
    packet = _packet(item, epoch)
    sealed = _seal(item, epoch, packet, signer)

    def mutate(payload: dict[str, object]) -> None:
        admissions = payload["admissions"]
        assert isinstance(admissions, list)
        mutation(admissions[0])  # type: ignore[operator]

    with pytest.raises(FormulaGovernorError, match=message):
        _factory(signer).project(
            evidence=packet,
            epoch=epoch,
            sealed_batch=_resign_payload(sealed, signer, mutate),
        )


def test_signed_historical_authority_kind_and_result_identity_cannot_be_forged() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:governor")
    item = _observation(historical=True)
    epoch = _epoch(item)
    packet = _packet(item, epoch)
    sealed = _seal(item, epoch, packet, signer, historical=True)
    for field, value in (("kind", "live_issued_field"), ("result_key", "result:other")):

        def mutate(payload: dict[str, object], field: str = field, value: str = value) -> None:
            admissions = payload["admissions"]
            assert isinstance(admissions, list)
            admissions[0]["authority_reference"][field] = value

        with pytest.raises(FormulaGovernorError, match="historical authority reference was forged"):
            _factory(signer).project(
                evidence=packet,
                epoch=epoch,
                sealed_batch=_resign_payload(sealed, signer, mutate),
            )
