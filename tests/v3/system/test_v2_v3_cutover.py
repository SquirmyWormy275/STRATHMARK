from __future__ import annotations

import json
from dataclasses import replace

import pytest

from strathmark.v3.application.cutover import (
    REQUIRED_RELEASE_EVIDENCE,
    Authority,
    CutoverCoordinator,
    CutoverPorts,
    EvidenceReceipt,
    ReleaseTier,
    V2FreezeSnapshot,
    V3InitializationSnapshot,
    create_release_attestation,
    create_v2_final_manifest,
    verify_authority_handoff,
    verify_release_attestation,
    verify_windows_capacity_manifest,
)
from strathmark.v3.application.lifecycle import LifecycleService, SnapshotKind, UpstreamSnapshot
from strathmark.v3.contracts.commands import CommandKind
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.events import AggregateKind, CompetitionEngineSelection, EventKind
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.contracts.statuses import EngineExecutionMode, PredictionEngine
from strathmark.v3.infrastructure.integrity import (
    IntegrityError,
    IntegrityTrustStore,
    P256EphemeralSigner,
    P256WindowsCNGSigner,
    SignedManifest,
    sign_manifest,
)
from strathmark.v3.infrastructure.sqlite.event_store import EventStoreConflict, SQLiteEventStore

NOW = "2026-08-25T20:00:00.000Z"
DIGEST = "a" * 64


def _selection(scope: str, engine: PredictionEngine) -> CompetitionEngineSelection:
    return CompetitionEngineSelection(
        scope_id=StableIdentifier(scope),
        engine=engine,
        mode=EngineExecutionMode.REHEARSAL,
        selected_by_actor_id=StableIdentifier("actor:tournament-manager"),
        selected_at_utc=NOW,
        reason_code="new_competition",
        consumer_contract_digest="5" * 64,
        source_commit="7d0312a7f58a4a4b3ea4daad8efd2671fefaac3c",
    )


def test_v3_tournament_open_binds_one_immutable_scope_selection(tmp_path) -> None:
    database = tmp_path / "selection-authority.sqlite3"
    lifecycle = LifecycleService(database)
    tournament = StableIdentifier("tournament:v3-show")
    selection = _selection(str(tournament), PredictionEngine.V3)
    round_id = StableIdentifier("round:heat-1")
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {
                "bundle_id": "bundle:v3-current",
                "historical_cutoff_key": "history:before-show",
            },
        ),
        command_id=IdempotencyKey("command:selected-tournament-snapshot"),
        actor_id=StableIdentifier("actor:tournament-manager"),
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=0,
    )
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            round_id,
            1,
            tournament,
            round_id,
            {
                "round_ordinal": 1,
                "predecessor_round_ids": [],
                "successor_round_ids": [],
            },
        ),
        command_id=IdempotencyKey("command:selected-round-snapshot"),
        actor_id=StableIdentifier("actor:tournament-manager"),
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=0,
    )
    lifecycle._execute(
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        tournament,
        {"configured": True},
        IdempotencyKey("command:configure-selected-v3"),
        StableIdentifier("actor:tournament-manager"),
        NOW,
        0,
    )
    arguments = dict(
        bundle_id=StableIdentifier("bundle:v3-current"),
        historical_cutoff_key="history:before-show",
        root_round_ids=(round_id,),
        command_id=IdempotencyKey("command:open-selected-v3"),
        actor_id=StableIdentifier("actor:tournament-manager"),
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
        engine_selection=selection,
    )

    first = lifecycle.open_tournament(tournament, **arguments)
    repeated = lifecycle.open_tournament(tournament, **arguments)

    assert repeated == first
    opened = [
        event
        for event in SQLiteEventStore(database).events()
        if event.kind is EventKind.TOURNAMENT_OPENED
    ]
    assert len(opened) == 1
    payload = opened[0].command.payload.to_value()
    assert payload["engine_selection"] == selection.to_dict()

    changed = _selection(str(tournament), PredictionEngine.V3)
    changed = replace(changed, reason_code="pre_lock_correction")
    with pytest.raises(EventStoreConflict):
        lifecycle.open_tournament(tournament, **{**arguments, "engine_selection": changed})


def test_v3_lifecycle_rejects_v2_or_cross_scope_selection_without_writing(tmp_path) -> None:
    database = tmp_path / "selection-rejections.sqlite3"
    lifecycle = LifecycleService(database)
    tournament = StableIdentifier("tournament:v3-show")
    arguments = dict(
        bundle_id=StableIdentifier("bundle:v3-current"),
        historical_cutoff_key="history:before-show",
        root_round_ids=(StableIdentifier("round:heat-1"),),
        command_id=IdempotencyKey("command:reject-selection"),
        actor_id=StableIdentifier("actor:tournament-manager"),
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )

    with pytest.raises(ContractError, match="V2-selected scope"):
        lifecycle.open_tournament(
            tournament,
            **arguments,
            engine_selection=_selection(str(tournament), PredictionEngine.V2),
        )
    with pytest.raises(ContractError, match="scope identity"):
        lifecycle.open_tournament(
            tournament,
            **arguments,
            engine_selection=_selection("tournament:other-show", PredictionEngine.V3),
        )

    assert SQLiteEventStore(database).events() == ()


def test_distinct_scope_selection_facts_do_not_share_identity() -> None:
    v2 = _selection("tournament:v2-show", PredictionEngine.V2)
    v3 = _selection("tournament:v3-show", PredictionEngine.V3)

    assert v2.scope_id != v3.scope_id
    assert v2.selection_digest != v3.selection_digest


def _provider_cng_signer(monkeypatch: pytest.MonkeyPatch, key_name: str) -> P256WindowsCNGSigner:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    import strathmark.v3.infrastructure.integrity as module

    private = ec.generate_private_key(ec.SECP256R1())

    class FakeProviderKey(module._WindowsCNGProviderKey):
        def attest_public_key(self) -> bytes:
            return private.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )

        def sign_digest(self, digest: bytes) -> bytes:
            return private.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))

    backend = object.__new__(FakeProviderKey)
    monkeypatch.setattr(
        module._WindowsCNGProviderKey,
        "open",
        classmethod(lambda _cls, _provider, _key_name: backend),
    )
    return P256WindowsCNGSigner.open(key_name)


def _evidence() -> tuple[EvidenceReceipt, ...]:
    return tuple(
        EvidenceReceipt(name, "passed", f"{index:064x}", NOW)
        for index, name in enumerate(REQUIRED_RELEASE_EVIDENCE, start=1)
    )


def _release(signer, tier: ReleaseTier) -> SignedManifest:
    return create_release_attestation(
        evidence=_evidence(),
        source_commit="2e696d8",
        platform="windows-11-x86_64-python-3.13",
        tier=tier,
        signer=signer,
        created_at=NOW,
    )


def _v2_manifest(signer) -> SignedManifest:
    return create_v2_final_manifest(
        V2FreezeSnapshot(
            trusted_writes_frozen=True,
            open_tournaments=0,
            in_flight_requests=0,
            ambiguous_requests=0,
            final_sequence=812,
            schema_digest="1" * 64,
            receipt_root_digest="2" * 64,
        ),
        signer=signer,
        created_at=NOW,
    )


def _initialization(release: SignedManifest) -> V3InitializationSnapshot:
    return V3InitializationSnapshot(
        initialized=True,
        open_tournaments=0,
        release_attestation_digest=release.body_digest,
        database_digest="3" * 64,
        bundle_digest="4" * 64,
        consumer_contract_digest="5" * 64,
        isolated_rehearsal_digest="6" * 64,
        isolated_rehearsal_passed=True,
    )


def test_release_attestation_requires_complete_unique_passed_evidence() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:release")
    trust = IntegrityTrustStore((signer.identity,))
    release = _release(signer, ReleaseTier.REHEARSAL)

    payload = verify_release_attestation(release, trust_store=trust)
    assert tuple(item["name"] for item in payload["evidence"]) == REQUIRED_RELEASE_EVIDENCE
    assert payload["tier"] == "rehearsal"

    with pytest.raises(ValueError, match="complete"):
        create_release_attestation(
            evidence=_evidence()[:-1],
            source_commit="2e696d8",
            platform="windows-11-x86_64-python-3.13",
            tier=ReleaseTier.REHEARSAL,
            signer=signer,
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="passed"):
        create_release_attestation(
            evidence=(replace(_evidence()[0], result="failed"), *_evidence()[1:]),
            source_commit="2e696d8",
            platform="windows-11-x86_64-python-3.13",
            tier=ReleaseTier.REHEARSAL,
            signer=signer,
            created_at=NOW,
        )


def test_ephemeral_signer_cannot_assert_production_release() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:release")
    with pytest.raises(IntegrityError, match="production authority"):
        _release(signer, ReleaseTier.PRODUCTION)


@pytest.mark.parametrize(
    "failure_stage",
    ("freeze_v2", "resolve_inflight", "v2_manifest", "verify_v3", "consumer_rehearsal"),
)
def test_every_preparation_failure_restores_exactly_v2_authority(
    monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    signer = _provider_cng_signer(monkeypatch, f"u19-{failure_stage}")
    release = _release(signer, ReleaseTier.PRODUCTION)
    calls: list[str] = []

    def stage(name: str, value):
        def run():
            calls.append(name)
            if name == failure_stage:
                raise RuntimeError(f"secret from {name}")
            return value

        return run

    ports = CutoverPorts(
        freeze_v2=stage("freeze_v2", V2FreezeSnapshot(True, 0, 1, 1, 812, "1" * 64, "2" * 64)),
        resolve_inflight=stage(
            "resolve_inflight", V2FreezeSnapshot(True, 0, 0, 0, 812, "1" * 64, "2" * 64)
        ),
        finalize_v2_manifest=stage("v2_manifest", _v2_manifest(signer)),
        verify_v3=stage("verify_v3", _initialization(release)),
        rehearse_consumer=stage("consumer_rehearsal", "6" * 64),
        resume_v2=lambda: calls.append("resume_v2"),
    )
    attempt = CutoverCoordinator(
        ports,
        release_attestation=release,
        trust_store=IntegrityTrustStore((signer.identity,)),
        signer=signer,
    ).prepare(created_at=NOW)

    assert attempt.ready is False
    assert attempt.declared_authority is Authority.V2
    assert attempt.failure_stage == failure_stage
    assert attempt.reason_code == "cutover_preparation_failed"
    assert attempt.handoff is None
    if failure_stage != "freeze_v2":
        assert calls[-1] == "resume_v2"
    assert "secret" not in json.dumps(attempt.to_dict())


def test_production_handoff_is_signed_cutover_ready_but_does_not_switch_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = _provider_cng_signer(monkeypatch, "u19-handoff")
    trust = IntegrityTrustStore((signer.identity,))
    release = _release(signer, ReleaseTier.PRODUCTION)
    v2 = _v2_manifest(signer)
    initialization = _initialization(release)
    calls: list[str] = []
    ports = CutoverPorts(
        freeze_v2=lambda: V2FreezeSnapshot(True, 0, 1, 1, 812, "1" * 64, "2" * 64),
        resolve_inflight=lambda: V2FreezeSnapshot(True, 0, 0, 0, 812, "1" * 64, "2" * 64),
        finalize_v2_manifest=lambda: v2,
        verify_v3=lambda: initialization,
        rehearse_consumer=lambda: "6" * 64,
        resume_v2=lambda: calls.append("resume_v2"),
    )

    attempt = CutoverCoordinator(
        ports,
        release_attestation=release,
        trust_store=trust,
        signer=signer,
    ).prepare(created_at=NOW)

    assert attempt.ready is True
    assert attempt.declared_authority is Authority.V2
    assert attempt.failure_stage is None
    assert attempt.handoff is not None
    payload = verify_authority_handoff(attempt.handoff, trust_store=trust)
    assert payload["status"] == "cutover_ready"
    assert payload["current_authority"] == "v2"
    assert payload["next_authority"] == "v3"
    assert payload["endpoint_switched"] is False
    assert payload["v2_audit_only"] is False
    assert payload["requires_explicit_release_authorization"] is True
    assert calls == []


@pytest.mark.parametrize(
    "snapshot",
    (
        V2FreezeSnapshot(False, 0, 0, 0, 1, "1" * 64, "2" * 64),
        V2FreezeSnapshot(True, 1, 0, 0, 1, "1" * 64, "2" * 64),
        V2FreezeSnapshot(True, 0, 1, 0, 1, "1" * 64, "2" * 64),
        V2FreezeSnapshot(True, 0, 0, 1, 1, "1" * 64, "2" * 64),
    ),
)
def test_v2_final_manifest_rejects_unresolved_or_open_authority(snapshot: V2FreezeSnapshot) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:v2")
    with pytest.raises(ValueError, match="freeze boundary"):
        create_v2_final_manifest(snapshot, signer=signer, created_at=NOW)


def test_handoff_verification_rejects_tamper() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:release")
    release = _release(signer, ReleaseTier.REHEARSAL)
    payload = {
        "schema_version": "strathmark-v3-authority-handoff-v1",
        "status": "cutover_ready",
        "current_authority": "v2",
        "next_authority": "v3",
        "endpoint_switched": False,
        "v2_audit_only": False,
        "requires_explicit_release_authorization": True,
        "release_attestation_digest": release.body_digest,
        "v2_final_manifest_digest": DIGEST,
        "v3_database_digest": DIGEST,
        "v3_bundle_digest": DIGEST,
        "consumer_contract_digest": DIGEST,
        "isolated_rehearsal_digest": DIGEST,
    }
    manifest = sign_manifest("authority_handoff", payload, signer=signer, created_at=NOW)
    forged = SignedManifest(
        manifest.kind,
        manifest.body_json,
        manifest.body_digest,
        manifest.key_id,
        manifest.signature_der_b64[:-2] + "AA",
    )
    with pytest.raises(IntegrityError):
        verify_authority_handoff(
            forged,
            trust_store=IntegrityTrustStore((signer.identity,)),
        )


def test_windows_capacity_manifest_is_complete_pinned_and_inside_hard_budgets() -> None:
    with open("benchmarks/v3/windows_capacity_manifest.json", encoding="utf-8") as handle:
        manifest = json.load(handle)
    verified = verify_windows_capacity_manifest(manifest)
    assert verified["candidate_tier"] == "rehearsal"
    assert verified["measured"]["field_assembly_p99_ms"] < 2_000
    assert verified["measured"]["critical_restart_worst_ms"] <= 5_000

    with pytest.raises(ValueError, match="stress"):
        verify_windows_capacity_manifest(
            {**manifest, "stress_matrix": manifest["stress_matrix"][:-1]}
        )
    with pytest.raises(ValueError, match="budget"):
        verify_windows_capacity_manifest(
            {
                **manifest,
                "measured": {**manifest["measured"], "field_assembly_p99_ms": 2_000},
            }
        )


def test_cutover_value_contracts_reject_invalid_evidence_freeze_initialization_and_ports() -> None:
    invalid_evidence = (
        {"name": "unknown"},
        {"result": "maybe"},
        {"artifact_digest": "bad"},
    )
    base_evidence = _evidence()[0]
    for mutation in invalid_evidence:
        with pytest.raises(ValueError):
            replace(base_evidence, **mutation)

    base_v2 = V2FreezeSnapshot(True, 0, 0, 0, 1, "1" * 64, "2" * 64)
    for mutation in (
        {"trusted_writes_frozen": 1},
        {"open_tournaments": -1},
        {"schema_digest": "bad"},
    ):
        with pytest.raises(ValueError):
            replace(base_v2, **mutation)

    base_v3 = V3InitializationSnapshot(
        True, 0, "1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64, True
    )
    for mutation in (
        {"initialized": 1},
        {"open_tournaments": -1},
        {"database_digest": "bad"},
    ):
        with pytest.raises(ValueError):
            replace(base_v3, **mutation)

    with pytest.raises(ValueError, match="every explicit"):
        CutoverPorts(None, lambda: None, lambda: None, lambda: None, lambda: None, lambda: None)
    with pytest.raises(ValueError, match="typed ports"):
        CutoverCoordinator(
            object(),
            release_attestation=_release(
                P256EphemeralSigner.generate("integrity-key:test"), ReleaseTier.REHEARSAL
            ),
            trust_store=IntegrityTrustStore(
                (P256EphemeralSigner.generate("integrity-key:other").identity,)
            ),
            signer=P256EphemeralSigner.generate("integrity-key:last"),
        )


def test_release_and_handoff_closed_schema_rejections() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:closed")
    trust = IntegrityTrustStore((signer.identity,))
    valid_release = _release(signer, ReleaseTier.REHEARSAL)

    for arguments in (
        {"evidence": (*_evidence()[:-1], "bad")},
        {"source_commit": "not-a-commit"},
        {"platform": "bad platform"},
        {"tier": "rehearsal"},
    ):
        values = {
            "evidence": _evidence(),
            "source_commit": "2e696d8",
            "platform": "windows-11-x86_64-python-3.13",
            "tier": ReleaseTier.REHEARSAL,
            "signer": signer,
            "created_at": NOW,
            **arguments,
        }
        with pytest.raises(ValueError):
            create_release_attestation(**values)

    wrong_kind = sign_manifest("other_manifest", {"value": 1}, signer=signer, created_at=NOW)
    with pytest.raises(ValueError, match="kind"):
        verify_release_attestation(wrong_kind, trust_store=trust)
    payload = valid_release.body()["payload"]
    extra = sign_manifest(
        "v3_release_attestation", {**payload, "extra": True}, signer=signer, created_at=NOW
    )
    with pytest.raises(ValueError, match="fields"):
        verify_release_attestation(extra, trust_store=trust)
    evidence = [dict(item) for item in payload["evidence"]]
    evidence[0]["extra"] = True
    malformed = sign_manifest(
        "v3_release_attestation",
        {**payload, "evidence": evidence},
        signer=signer,
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="evidence"):
        verify_release_attestation(malformed, trust_store=trust)
    forged_production = sign_manifest(
        "v3_release_attestation",
        {**payload, "tier": "production"},
        signer=signer,
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="production CNG"):
        verify_release_attestation(forged_production, trust_store=trust)

    ephemeral_handoff = sign_manifest(
        "authority_handoff",
        {
            "schema_version": "strathmark-v3-authority-handoff-v1",
            "status": "cutover_ready",
            "current_authority": "v2",
            "next_authority": "v3",
            "endpoint_switched": False,
            "v2_audit_only": False,
            "requires_explicit_release_authorization": True,
            "release_attestation_digest": DIGEST,
            "v2_final_manifest_digest": DIGEST,
            "v3_database_digest": DIGEST,
            "v3_bundle_digest": DIGEST,
            "consumer_contract_digest": DIGEST,
            "isolated_rehearsal_digest": DIGEST,
        },
        signer=signer,
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="production CNG"):
        verify_authority_handoff(ephemeral_handoff, trust_store=trust)
    with pytest.raises(ValueError, match="kind"):
        verify_authority_handoff(wrong_kind, trust_store=trust)
    extra_handoff = sign_manifest(
        "authority_handoff",
        {**ephemeral_handoff.body()["payload"], "extra": True},
        signer=signer,
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="fields"):
        verify_authority_handoff(extra_handoff, trust_store=trust)
    switched_handoff = sign_manifest(
        "authority_handoff",
        {**ephemeral_handoff.body()["payload"], "endpoint_switched": True},
        signer=signer,
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="pre-switch"):
        verify_authority_handoff(switched_handoff, trust_store=trust)


def test_failed_v2_resume_declares_manual_authority() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:resume")
    release = _release(signer, ReleaseTier.REHEARSAL)
    ports = CutoverPorts(
        freeze_v2=lambda: V2FreezeSnapshot(True, 0, 1, 1, 1, "1" * 64, "2" * 64),
        resolve_inflight=lambda: (_ for _ in ()).throw(RuntimeError("failure")),
        finalize_v2_manifest=lambda: _v2_manifest(signer),
        verify_v3=lambda: _initialization(release),
        rehearse_consumer=lambda: "6" * 64,
        resume_v2=lambda: (_ for _ in ()).throw(RuntimeError("resume failure")),
    )
    attempt = CutoverCoordinator(
        ports,
        release_attestation=release,
        trust_store=IntegrityTrustStore((signer.identity,)),
        signer=signer,
    ).prepare(created_at=NOW)
    assert attempt.declared_authority is Authority.TRADITIONAL_MANUAL
    assert attempt.reason_code == "v2_resume_failed_manual_authority_required"


def test_windows_manifest_closed_schema_rejections() -> None:
    with open("benchmarks/v3/windows_capacity_manifest.json", encoding="utf-8") as handle:
        manifest = json.load(handle)
    mutations = (
        {"extra": True},
        {"schema_version": "wrong"},
        {"candidate_tier": "maybe"},
        {"machine": {"operating_system": "Windows-11"}},
        {"machine": {**manifest["machine"], "gpu_vram_mib": True}},
        {"declared_envelope": {**manifest["declared_envelope"], "max_field_entrants": 13}},
        {"measured": {"field_assembly_runs": 100}},
        {"measured": {**manifest["measured"], "rss_growth_bytes": True}},
        {"artifact_pins": {"field_assembly_manifest_sha256": DIGEST}},
        {"limitations": []},
    )
    for mutation in mutations:
        with pytest.raises(ValueError):
            verify_windows_capacity_manifest({**manifest, **mutation})
