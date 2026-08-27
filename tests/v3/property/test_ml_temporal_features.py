from __future__ import annotations

import math
import sys
from base64 import b64decode, b64encode
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from inspect import signature
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

import strathmark.v3.composition as composition_module
import strathmark.v3.factory.ml_training as ml_training_module
from strathmark.v3.assessors.ml import SpecialistGate
from strathmark.v3.composition import (
    V3RuntimeConfig,
    compose_production_ml_authorities,
    compose_test_ml_audit_authority,
    compose_test_ml_candidate_authority,
)
from strathmark.v3.contracts.canonical import canonical_bytes
from strathmark.v3.contracts.errors import ConfigurationError
from strathmark.v3.contracts.evidence import (
    ContextProperty,
    EvidencePacket,
    ResultObservation,
    TargetContext,
)
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
from strathmark.v3.factory.ml_training import (
    FEATURE_NAMES,
    AuthorizedGateExamples,
    AuthorizedMLPackets,
    AuthorizedMLRows,
    AuthorizedOOFPredictions,
    CausalTrainingRow,
    GateExample,
    MLAuthorityEnvironment,
    MLDataRole,
    MLRoleAssignment,
    MLRoleManifest,
    OOFComponentPrediction,
    SpecialistEligibility,
    TrustedMLAuditAuthority,
    TrustedMLRoleAuthority,
    _fit_specialist_gate_values,
    _gate_value,
    _member_digest,
    _prediction_values,
    _scope_member_values,
    _train_catboost_hierarchy,
    build_inference_features,
    canonical_gate_features,
    mean_pinball_loss,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityError,
    IntegrityKeyClass,
    IntegrityKeyIdentity,
    P256EphemeralSigner,
    P256WindowsCNGSigner,
    SignedManifest,
    sign_manifest,
)


def _context(
    event: str = "underhand",
    size: int = 300,
    material: str = "gum",
    taxonomy: str = "taxonomy:v1",
    conversion: str = "conversion:v1",
) -> TargetContext:
    return TargetContext(
        event,
        size,
        material,
        taxonomy,
        conversion,
        (ContextProperty("density", "720", "kg_m3", None),),
    )


def _observation(
    sequence: int, raw_ms: int, *, day: int, tournament: str = "a"
) -> ResultObservation:
    return ResultObservation(
        StableIdentifier(f"evidence:ml-{sequence}-{tournament}"),
        StableIdentifier("competitor:ml-a"),
        StableIdentifier(f"tournament:{tournament}"),
        StableIdentifier("round:heat"),
        StableIdentifier(f"field:{tournament}-{sequence}"),
        _context(),
        sequence,
        f"2026-07-{day:02d}T12:00:00.000Z",
        3,
        None,
        None,
        None,
        OfficialResult(ResultStatus.COMPLETION, raw_ms, None, 1, None),
        f"{sequence:064x}",
    )


def _packet(
    observations: tuple[ResultObservation, ...], *, target: TargetContext | None = None
) -> EvidencePacket:
    maximum = max((item.observation_sequence for item in observations), default=0)
    context = target or _context()
    return EvidencePacket.create(
        competitor_id=StableIdentifier("competitor:ml-a"),
        target_context=context,
        observations=observations,
        taxonomy_version=context.taxonomy_version,
        conversion_version=context.conversion_version,
        historical_cutoff_key="history:2026-07-31",
        tournament_epoch_id=StableIdentifier("epoch:ml-sealed"),
        tournament_event_sequence=maximum,
    )


def _verified_authority(assignments: tuple[tuple[str, MLDataRole], ...]):
    signer = P256EphemeralSigner.generate("ml-role-test")
    signed = sign_manifest(
        "ml_role_manifest",
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "d" * 64,
            "assignments": [
                {"tournament_id": tournament_id, "role": role.value}
                for tournament_id, role in assignments
            ],
        },
        signer=signer,
        created_at="2026-01-01T00:00:00.000Z",
    )
    return compose_test_ml_candidate_authority(signed, signer=signer)


def _verified_audit_authority(tournaments: tuple[str, ...]):
    signer = P256EphemeralSigner.generate("ml-audit-role-test")
    signed = sign_manifest(
        "ml_audit_role_manifest",
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "e" * 64,
            "assignments": [
                {"tournament_id": item, "role": MLDataRole.LOCKED_AUDIT.value}
                for item in tournaments
            ],
        },
        signer=signer,
        created_at="2026-01-01T00:00:00.000Z",
    )
    return compose_test_ml_audit_authority(signed, signer=signer)


def _signed_authority_payload(payload: object):
    signer = P256EphemeralSigner.generate("ml-role-payload-test")
    signed = sign_manifest(
        "ml_role_manifest",
        payload,  # type: ignore[arg-type]
        signer=signer,
        created_at="2026-01-01T00:00:00.000Z",
    )
    return signed, signer


@dataclass(frozen=True)
class _AuthorizedCase:
    authority: TrustedMLRoleAuthority
    rows: AuthorizedMLRows

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def __getitem__(self, index):
        return self.rows[index]


def _authorized_rows(packets: tuple[EvidencePacket, ...], role: MLDataRole = MLDataRole.TRAINING):
    tournaments = sorted(
        {
            str(observation.tournament_id)
            for packet in packets
            for observation in packet.observations
        }
    )
    assignments = [(item, role) for item in tournaments]
    for filler_role in (
        MLDataRole.TRAINING,
        MLDataRole.TUNING,
        MLDataRole.CALIBRATION,
    ):
        if filler_role is not role:
            assignments.append((f"tournament:filler-{filler_role.value}", filler_role))
    authority = _verified_authority(tuple(assignments))
    rows = authority.build_causal_training_matrix(authority.authorize_packets(role, packets))
    return _AuthorizedCase(authority, rows)


def test_role_authority_is_signed_non_relabelable_and_raw_inputs_fail_closed() -> None:
    assignments = (
        ("tournament:a", MLDataRole.TRAINING),
        ("tournament:b", MLDataRole.TUNING),
        ("tournament:c", MLDataRole.CALIBRATION),
    )
    authority = _verified_authority(assignments)
    assert authority.manifest.generation_digest == "d" * 64
    assert authority.manifest.manifest_digest
    assert authority.signed_manifest_digest
    assert authority.signer_key_id == "ml-role-test"
    training = authority.authorize_packets(
        MLDataRole.TRAINING,
        (_packet((_observation(1, 40_000, day=1, tournament="a"),)),),
    )
    training_rows = authority.build_causal_training_matrix(training)
    assert training_rows.role is MLDataRole.TRAINING
    assert tuple(authority.build_causal_training_matrix(copy(training))) == tuple(training_rows)
    with pytest.raises(ValueError, match="authorized signed scope"):
        authority.build_causal_training_matrix(training.packets)  # type: ignore[arg-type]

    signer = P256EphemeralSigner.generate("ml-role-forgery")
    signed = sign_manifest(
        "ml_role_manifest",
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "d" * 64,
            "assignments": [
                {"tournament_id": tournament_id, "role": role.value}
                for tournament_id, role in assignments
            ],
        },
        signer=signer,
        created_at="2026-01-01T00:00:00.000Z",
    )
    with pytest.raises(Exception, match="digest|signature|encoding"):
        forged = SignedManifest(
            signed.kind,
            signed.body_json.replace('"training"', '"locked_audit"'),
            signed.body_digest,
            signed.key_id,
            b64encode(signer.sign(signed.body_json.encode())).decode(),
        )
        compose_test_ml_candidate_authority(forged, signer=signer)


def test_candidate_role_scope_has_no_importable_or_replaceable_audit_capability() -> None:
    assert not hasattr(ml_training_module, "_ROLE_AUTHORITY_SEAL")
    assert not hasattr(ml_training_module, "verify_ml_role_authority")
    assert not hasattr(ml_training_module, "verify_ml_audit_authority")
    authority = _verified_authority(
        (
            ("tournament:a", MLDataRole.TRAINING),
            ("tournament:b", MLDataRole.TUNING),
            ("tournament:c", MLDataRole.CALIBRATION),
        )
    )
    training = authority.authorize_packets(
        MLDataRole.TRAINING,
        (_packet((_observation(1, 40_000, day=1, tournament="a"),)),),
    )
    with pytest.raises(TypeError):
        replace(training, role=MLDataRole.LOCKED_AUDIT)
    with pytest.raises(ValueError, match="candidate authority cannot access locked audit"):
        authority.authorize_packets(MLDataRole.LOCKED_AUDIT, ())
    object.__setattr__(training, "_members", ())
    with pytest.raises(ValueError, match="members differ"):
        authority.build_causal_training_matrix(training)

    v2_context = _context(taxonomy="taxonomy:v2")
    v2_observation = replace(_observation(2, 41_000, day=2, tournament="a"), context=v2_context)
    with pytest.raises(ValueError, match="one taxonomy and conversion"):
        authority.authorize_packets(
            MLDataRole.TRAINING,
            (
                _packet((_observation(1, 40_000, day=1, tournament="a"),)),
                _packet((v2_observation,), target=v2_context),
            ),
        )


@pytest.mark.parametrize("copier", [copy, deepcopy])
def test_copied_candidate_scope_cannot_be_mutated_into_locked_audit(copier) -> None:
    authority = _verified_authority(
        (
            ("tournament:a", MLDataRole.TRAINING),
            ("tournament:b", MLDataRole.TUNING),
            ("tournament:c", MLDataRole.CALIBRATION),
        )
    )
    candidate = authority.authorize_packets(
        MLDataRole.TRAINING,
        (_packet((_observation(1, 40_000, day=1, tournament="a"),)),),
    )
    attacked = copier(candidate)
    with pytest.raises(AttributeError):
        object.__setattr__(attacked, "_role", MLDataRole.LOCKED_AUDIT)
    with pytest.raises(AttributeError):
        object.__setattr__(attacked, "_purpose", "audit_packets")
    attacker = P256EphemeralSigner.generate("ml-scope-attacker")
    forged_payload = dict(candidate._payload())
    forged_payload.update(
        role=MLDataRole.LOCKED_AUDIT.value,
        purpose="audit_packets",
        member_digest=_member_digest(
            "audit_packets", [packet.content_digest for packet in attacked.packets]
        ),
        signer_identity=attacker.identity.to_dict(),
    )
    object.__setattr__(
        attacked,
        "_authorization_envelope",
        sign_manifest(
            "ml_scope_authorization",
            forged_payload,
            signer=attacker,
            created_at="2026-01-01T00:00:00.000Z",
        ),
    )
    with pytest.raises(ValueError, match="signature|authority|authorized"):
        audit = _verified_audit_authority(("tournament:a",))
        audit.build_locked_audit_replay_matrix(attacked)


def test_attacker_self_signed_and_self_trusted_scope_fails_pinned_audit_factory() -> None:
    packet = _packet((_observation(1, 40_000, day=1, tournament=MLDataRole.LOCKED_AUDIT.value),))
    attacker = P256EphemeralSigner.generate("ml-self-trust-attacker")
    attacker_manifest = sign_manifest(
        "ml_audit_role_manifest",
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "a" * 64,
            "assignments": [
                {
                    "tournament_id": "tournament:locked_audit",
                    "role": MLDataRole.LOCKED_AUDIT.value,
                }
            ],
        },
        signer=attacker,
        created_at="2026-01-01T00:00:00.000Z",
    )
    attacker_authority = compose_test_ml_audit_authority(attacker_manifest, signer=attacker)
    attacker_scope = attacker_authority.authorize_packets((packet,))
    pinned_authority = _verified_audit_authority(("tournament:locked_audit",))

    with pytest.raises(ValueError, match="signature|composition-authorized"):
        pinned_authority.build_locked_audit_replay_matrix(attacker_scope)


def test_pinned_factory_revalidates_every_signed_envelope_field() -> None:
    signer = P256EphemeralSigner.generate("ml-pinned-envelope-test")
    manifest = sign_manifest(
        "ml_role_manifest",
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "d" * 64,
            "assignments": [
                {"tournament_id": "tournament:a", "role": "training"},
                {"tournament_id": "tournament:b", "role": "tuning"},
                {"tournament_id": "tournament:c", "role": "calibration"},
            ],
        },
        signer=signer,
        created_at="2026-01-01T00:00:00.000Z",
    )
    authority = compose_test_ml_candidate_authority(manifest, signer=signer)
    scope = authority.authorize_packets(
        MLDataRole.TRAINING,
        (_packet((_observation(1, 40_000, day=1, tournament="a"),)),),
    )

    for field, value, message in (
        ("role", "invalid", "signed role is invalid"),
        ("taxonomy_version", [], "taxonomy or conversion"),
        ("conversion_version", "conversion:wrong", "differs from its signed members"),
    ):
        attacked = copy(scope)
        payload = dict(scope._payload())
        payload[field] = value
        object.__setattr__(
            attacked,
            "_authorization_envelope",
            sign_manifest(
                "ml_scope_authorization",
                payload,
                signer=signer,
                created_at="2026-01-01T00:00:00.000Z",
            ),
        )
        with pytest.raises(ValueError, match=message):
            authority.build_causal_training_matrix(attacked)


def test_candidate_and_audit_interfaces_reject_own_signer_opposite_role_envelopes() -> None:
    candidate_signer = P256EphemeralSigner.generate("ml-candidate-disjoint-test")
    candidate_manifest = sign_manifest(
        "ml_role_manifest",
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "c" * 64,
            "assignments": [
                {"tournament_id": "tournament:training", "role": "training"},
                {"tournament_id": "tournament:tuning", "role": "tuning"},
                {"tournament_id": "tournament:calibration", "role": "calibration"},
            ],
        },
        signer=candidate_signer,
        created_at="2026-01-01T00:00:00.000Z",
    )
    candidate = compose_test_ml_candidate_authority(candidate_manifest, signer=candidate_signer)
    candidate_scope = candidate.authorize_packets(
        MLDataRole.TRAINING,
        (_packet((_observation(1, 40_000, day=1, tournament="training"),)),),
    )
    attacked_candidate_scope = copy(candidate_scope)
    candidate_payload = dict(candidate_scope._payload())
    candidate_payload.update(
        role=MLDataRole.LOCKED_AUDIT.value,
        purpose="audit_packets",
        member_digest=_member_digest(
            "audit_packets",
            [packet.content_digest for packet in candidate_scope.packets],
        ),
    )
    object.__setattr__(
        attacked_candidate_scope,
        "_authorization_envelope",
        sign_manifest(
            "ml_scope_authorization",
            candidate_payload,
            signer=candidate_signer,
            created_at="2026-01-01T00:00:00.000Z",
        ),
    )
    assert not hasattr(candidate, "build_locked_audit_replay_matrix")
    assert not hasattr(candidate, "evaluate_frozen_replay")
    with pytest.raises(ValueError, match="authority type|role|purpose"):
        candidate.build_causal_training_matrix(attacked_candidate_scope)
    candidate_rows = candidate.build_causal_training_matrix(candidate_scope)
    with pytest.raises(ValueError, match="forbidden for this authority type"):
        candidate._verify_rows(candidate_rows, (MLDataRole.LOCKED_AUDIT,))

    audit_signer = P256EphemeralSigner.generate("ml-audit-disjoint-test")
    audit_manifest = sign_manifest(
        "ml_audit_role_manifest",
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "a" * 64,
            "assignments": [
                {
                    "tournament_id": "tournament:locked_audit",
                    "role": "locked_audit",
                }
            ],
        },
        signer=audit_signer,
        created_at="2026-01-01T00:00:00.000Z",
    )
    audit = compose_test_ml_audit_authority(audit_manifest, signer=audit_signer)
    audit_scope = audit.authorize_packets(
        (_packet((_observation(1, 40_000, day=1, tournament=MLDataRole.LOCKED_AUDIT.value),)),)
    )
    attacked_audit_scope = copy(audit_scope)
    audit_payload = dict(audit_scope._payload())
    audit_payload.update(
        role=MLDataRole.TRAINING.value,
        purpose="candidate_packets",
        member_digest=_member_digest(
            "candidate_packets", [packet.content_digest for packet in audit_scope.packets]
        ),
    )
    object.__setattr__(
        attacked_audit_scope,
        "_authorization_envelope",
        sign_manifest(
            "ml_scope_authorization",
            audit_payload,
            signer=audit_signer,
            created_at="2026-01-01T00:00:00.000Z",
        ),
    )
    assert not hasattr(audit, "build_causal_training_matrix")
    assert not hasattr(audit, "train_catboost_hierarchy")
    with pytest.raises(ValueError, match="authority type|role|purpose"):
        audit.build_locked_audit_replay_matrix(attacked_audit_scope)
    audit_rows = audit.build_locked_audit_replay_matrix(audit_scope)
    with pytest.raises(ValueError, match="forbidden for this authority type"):
        audit._verify_rows(audit_rows, (MLDataRole.TRAINING,))


def test_derived_scopes_must_bind_the_exact_parent_scope() -> None:
    authority = _verified_authority(
        (
            ("tournament:training", MLDataRole.TRAINING),
            ("tournament:tune-a", MLDataRole.TUNING),
            ("tournament:tune-b", MLDataRole.TUNING),
            ("tournament:cal-a", MLDataRole.CALIBRATION),
            ("tournament:cal-b", MLDataRole.CALIBRATION),
        )
    )

    def rows(role: MLDataRole, tournament: str, day: int) -> AuthorizedMLRows:
        scoped = authority.authorize_packets(
            role,
            (_packet((_observation(day, 40_000, day=day, tournament=tournament),)),),
        )
        return authority.build_causal_training_matrix(scoped)

    tune_a = rows(MLDataRole.TUNING, "tune-a", 1)
    tune_b = rows(MLDataRole.TUNING, "tune-b", 2)
    predictions = authority.grouped_oof_component_predictions(
        tune_a, model_factory=lambda **kwargs: _OOFModel()
    )
    with pytest.raises(ValueError, match="do not share"):
        authority.gate_examples_from_oof(predictions, tune_b)

    cal_a = rows(MLDataRole.CALIBRATION, "cal-a", 3)
    cal_b = rows(MLDataRole.CALIBRATION, "cal-b", 4)
    calibration_predictions = authority.grouped_oof_component_predictions(
        cal_a, model_factory=lambda **kwargs: _OOFModel()
    )
    with pytest.raises(ValueError, match="trusted calibration-role authority"):
        authority.fit_pit_calibrator(
            cal_b,
            calibration_predictions,
            SpecialistGate("0", (("log_history_depth", "0"), ("missing_fraction", "0"))),
        )


def test_role_authority_contract_rejects_untrusted_or_malformed_construction() -> None:
    with pytest.raises(ValueError, match="tournament identity"):
        MLRoleAssignment("bad", MLDataRole.TRAINING)
    with pytest.raises(ValueError, match="closed data role"):
        MLRoleAssignment("tournament:a", "training")  # type: ignore[arg-type]
    for constructor in (
        MLRoleManifest,
        AuthorizedMLPackets,
        AuthorizedMLRows,
        AuthorizedOOFPredictions,
        AuthorizedGateExamples,
        TrustedMLRoleAuthority,
        TrustedMLAuditAuthority,
    ):
        with pytest.raises(TypeError, match="factory|signature|authority"):
            constructor()
    forged_manifest = object.__new__(MLRoleManifest)
    with pytest.raises(ValueError, match="not signature-authorized"):
        forged_manifest._validate()

    authority = _verified_authority(
        tuple(
            (f"tournament:{role.value}", role)
            for role in (MLDataRole.TRAINING, MLDataRole.TUNING, MLDataRole.CALIBRATION)
        )
    )
    packet = _packet((_observation(1, 40_000, day=1, tournament=MLDataRole.TRAINING.value),))
    with pytest.raises(ValueError, match="exclusively"):
        authority.authorize_packets(MLDataRole.TUNING, (packet,))
    assert authority.manifest.assignments
    object.__setattr__(
        authority.manifest,
        "_assignments",
        (MLRoleAssignment("tournament:training", MLDataRole.LOCKED_AUDIT),),
    )
    with pytest.raises(ValueError, match="candidate authority cannot access locked audit"):
        authority.authorize_packets(MLDataRole.LOCKED_AUDIT, (packet,))

    malformed = object.__new__(AuthorizedMLPackets)
    object.__setattr__(malformed, "_members", ())
    object.__setattr__(malformed, "_authorization_envelope", object())
    with pytest.raises(ValueError, match="malformed"):
        malformed._payload()
    object.__setattr__(
        malformed,
        "_authorization_envelope",
        SimpleNamespace(body=lambda: {"payload": []}),
    )
    with pytest.raises(ValueError, match="malformed"):
        malformed._payload()
    object.__setattr__(
        malformed,
        "_authorization_envelope",
        SimpleNamespace(body=lambda: {"payload": {"role": "bad"}}),
    )
    with pytest.raises(ValueError, match="role is malformed"):
        _ = malformed.role
    assert malformed.authority_digest == ""
    assert malformed.taxonomy_version == ""
    assert malformed.conversion_version == ""
    with pytest.raises(ValueError, match="unknown ML authorization scope"):
        _scope_member_values(object, ())


def test_test_composition_is_explicitly_ephemeral_and_not_production_ready() -> None:
    signed, signer = _signed_authority_payload(
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "d" * 64,
            "assignments": [
                {"tournament_id": f"tournament:{role.value}", "role": role.value}
                for role in (
                    MLDataRole.TRAINING,
                    MLDataRole.TUNING,
                    MLDataRole.CALIBRATION,
                )
            ],
        }
    )
    authority = compose_test_ml_candidate_authority(signed, signer=signer)
    assert authority.authority_environment is MLAuthorityEnvironment.TEST_EPHEMERAL
    assert not authority.production_ready
    with pytest.raises(ValueError, match="not production-authoritative"):
        authority.require_production_ready()
    assert set(signature(compose_production_ml_authorities).parameters) == {"config"}

    class WrappedSigner:
        identity = signer.identity

        def sign(self, payload: bytes) -> bytes:
            return signer.sign(payload)

    with pytest.raises(ValueError, match="explicit ephemeral test signer"):
        compose_test_ml_candidate_authority(
            signed,
            signer=WrappedSigner(),  # type: ignore[arg-type]
        )


def test_production_composition_rejects_installed_ephemeral_identity(
    tmp_path: Path,
) -> None:
    paths = tuple(tmp_path / f"path-{index}" for index in range(8))
    config = V3RuntimeConfig(*paths, False)
    config.integrity_key_root.mkdir(parents=True)
    signer = P256EphemeralSigner.generate("ml-installed-ephemeral")
    (config.integrity_key_root / "ml-candidate-public-identity.json").write_bytes(
        canonical_bytes(signer.identity.to_dict())
    )

    with pytest.raises(ConfigurationError, match="rejects non-CNG or test-ephemeral"):
        compose_production_ml_authorities(config)


def test_installed_ml_authority_parsers_fail_closed(tmp_path: Path) -> None:
    paths = tuple(tmp_path / f"parser-{index}" for index in range(8))
    config = V3RuntimeConfig(*paths, False, canonical_max_bytes=128)
    candidate = config.integrity_key_root / "candidate.json"
    config.integrity_key_root.mkdir(parents=True)

    with pytest.raises(ConfigurationError, match="cannot be read"):
        composition_module._read_installation_mapping(candidate, config)
    for raw, message in (
        (b"", "byte bound"),
        (b"x" * 129, "byte bound"),
        (b"{", "canonical JSON"),
        (b"[]", "canonical object"),
        (b'{"value": 1}', "canonical object"),
    ):
        candidate.write_bytes(raw)
        with pytest.raises(ConfigurationError, match=message):
            composition_module._read_installation_mapping(candidate, config)

    key_name = config.integrity_key_root / "key-name.txt"
    with pytest.raises(ConfigurationError, match="cannot be read"):
        composition_module._read_installation_key_name(key_name)
    for raw in (b"", b" bad", b"bad\n", b"bad\x00", b"x" * 513, b"\xff"):
        key_name.write_bytes(raw)
        with pytest.raises(ConfigurationError, match="cannot be read|is invalid"):
            composition_module._read_installation_key_name(key_name)


def test_production_composition_rejects_test_config_and_damaged_registry(
    tmp_path: Path,
) -> None:
    paths = tuple(tmp_path / f"damaged-{index}" for index in range(8))
    with pytest.raises(ConfigurationError, match="non-test runtime"):
        compose_production_ml_authorities(V3RuntimeConfig(*paths, True))
    config = V3RuntimeConfig(*paths, False)
    config.integrity_key_root.mkdir(parents=True)
    (config.integrity_key_root / "ml-candidate-public-identity.json").write_bytes(
        canonical_bytes({"bad": True})
    )
    with pytest.raises(ConfigurationError, match="public identity is invalid"):
        compose_production_ml_authorities(config)


def _write_candidate_production_material(
    config: V3RuntimeConfig,
) -> tuple[IntegrityKeyIdentity, SignedManifest]:
    ephemeral = P256EphemeralSigner.generate("ml-candidate-installed-material")
    identity = IntegrityKeyIdentity(
        "integrity-key:ml-candidate-installed-test",
        IntegrityKeyClass.PRODUCTION_CNG,
        "windows_cng_p256_sha256",
        ephemeral.identity.public_key_der_b64,
    )

    class ManifestSigner:
        def __init__(self) -> None:
            self.identity = identity

        def sign(self, payload: bytes) -> bytes:
            return ephemeral.sign(payload)

    manifest = sign_manifest(
        "ml_role_manifest",
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "c" * 64,
            "assignments": [
                {"tournament_id": "tournament:training", "role": "training"},
                {"tournament_id": "tournament:tuning", "role": "tuning"},
                {"tournament_id": "tournament:calibration", "role": "calibration"},
            ],
        },
        signer=ManifestSigner(),
        created_at="2026-01-01T00:00:00.000Z",
    )
    root = config.integrity_key_root
    (root / "ml-candidate-public-identity.json").write_bytes(canonical_bytes(identity.to_dict()))
    (root / "ml-candidate-role-manifest.json").write_bytes(canonical_bytes(manifest.to_dict()))
    (root / "ml-candidate-cng-key-name.txt").write_text(
        "strathmark-candidate-test", encoding="utf-8"
    )
    return identity, manifest


def test_production_composition_rejects_bad_manifest_open_key_and_live_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = tuple(tmp_path / f"material-{index}" for index in range(8))
    config = V3RuntimeConfig(*paths, False)
    config.integrity_key_root.mkdir(parents=True)
    identity, manifest = _write_candidate_production_material(config)
    manifest_path = config.integrity_key_root / "ml-candidate-role-manifest.json"
    manifest_path.write_bytes(canonical_bytes({"bad": True}))
    with pytest.raises(ConfigurationError, match="CNG authority material is invalid"):
        compose_production_ml_authorities(config)

    manifest_path.write_bytes(canonical_bytes(manifest.to_dict()))

    def fail_open(_cls, _key_name):
        raise IntegrityError("test open failure")

    monkeypatch.setattr(P256WindowsCNGSigner, "open", classmethod(fail_open))
    with pytest.raises(ConfigurationError, match="CNG authority material is invalid"):
        compose_production_ml_authorities(config)

    other = P256EphemeralSigner.generate("ml-other-live-key")
    other_identity = IntegrityKeyIdentity(
        "integrity-key:ml-other-production-test",
        IntegrityKeyClass.PRODUCTION_CNG,
        "windows_cng_p256_sha256",
        other.identity.public_key_der_b64,
    )
    monkeypatch.setattr(
        P256WindowsCNGSigner,
        "open",
        classmethod(lambda _cls, _key_name: SimpleNamespace(identity=other_identity)),
    )
    with pytest.raises(ConfigurationError, match="differs from the live Windows CNG"):
        compose_production_ml_authorities(config)

    class BadBackend:
        def attest_public_key(self) -> bytes:
            raise IntegrityError("test re-attestation failure")

    fake_cng = object.__new__(P256WindowsCNGSigner)
    object.__setattr__(fake_cng, "_identity", identity)
    object.__setattr__(fake_cng, "_backend", BadBackend())
    monkeypatch.setattr(
        P256WindowsCNGSigner,
        "open",
        classmethod(lambda _cls, _key_name: fake_cng),
    )
    with pytest.raises(ValueError, match="live OS-attested Windows CNG signer"):
        compose_production_ml_authorities(config)


def test_production_composition_loads_separate_installed_cng_authorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

    paths = tuple(tmp_path / f"production-{index}" for index in range(8))
    config = V3RuntimeConfig(*paths, False)
    config.integrity_key_root.mkdir(parents=True)
    fake_cng_by_name: dict[str, P256WindowsCNGSigner] = {}

    for role_set in ("candidate", "audit"):
        ephemeral = P256EphemeralSigner.generate(f"ml-{role_set}-material")
        identity = IntegrityKeyIdentity(
            f"integrity-key:ml-{role_set}-production-test",
            IntegrityKeyClass.PRODUCTION_CNG,
            "windows_cng_p256_sha256",
            ephemeral.identity.public_key_der_b64,
        )

        class ManifestSigner:
            def __init__(self, identity, delegate):
                self.identity = identity
                self._delegate = delegate

            def sign(self, payload: bytes) -> bytes:
                return self._delegate.sign(payload)

        payload = {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": ("c" if role_set == "candidate" else "a") * 64,
            "assignments": (
                [
                    {"tournament_id": "tournament:training", "role": "training"},
                    {"tournament_id": "tournament:tuning", "role": "tuning"},
                    {
                        "tournament_id": "tournament:calibration",
                        "role": "calibration",
                    },
                ]
                if role_set == "candidate"
                else [
                    {
                        "tournament_id": "tournament:locked_audit",
                        "role": "locked_audit",
                    }
                ]
            ),
        }
        manifest = sign_manifest(
            "ml_role_manifest" if role_set == "candidate" else "ml_audit_role_manifest",
            payload,
            signer=ManifestSigner(identity, ephemeral),
            created_at="2026-01-01T00:00:00.000Z",
        )
        root = config.integrity_key_root
        (root / f"ml-{role_set}-public-identity.json").write_bytes(
            canonical_bytes(identity.to_dict())
        )
        (root / f"ml-{role_set}-role-manifest.json").write_bytes(
            canonical_bytes(manifest.to_dict())
        )
        key_name = f"strathmark-ml-{role_set}-test"
        (root / f"ml-{role_set}-cng-key-name.txt").write_text(key_name, encoding="utf-8")

        class FakeBackend:
            def __init__(self, identity, delegate):
                self._identity = identity
                self._delegate = delegate

            def attest_public_key(self) -> bytes:
                return b64decode(self._identity.public_key_der_b64)

            def sign_digest(self, digest: bytes) -> bytes:
                return self._delegate._private_key.sign(
                    digest, ec.ECDSA(Prehashed(hashes.SHA256()))
                )

        fake_cng = object.__new__(P256WindowsCNGSigner)
        object.__setattr__(fake_cng, "_identity", identity)
        object.__setattr__(fake_cng, "_backend", FakeBackend(identity, ephemeral))
        fake_cng_by_name[key_name] = fake_cng

    monkeypatch.setattr(
        P256WindowsCNGSigner,
        "open",
        classmethod(lambda _cls, key_name: fake_cng_by_name[key_name]),
    )
    candidate, audit = compose_production_ml_authorities(config)
    assert candidate.production_ready and audit.production_ready
    assert candidate.authority_environment is MLAuthorityEnvironment.PRODUCTION_CNG
    assert audit.authority_environment is MLAuthorityEnvironment.PRODUCTION_CNG
    candidate.require_production_ready()
    scope = candidate.authorize_packets(
        MLDataRole.TRAINING,
        (_packet((_observation(1, 40_000, day=1, tournament="training"),)),),
    )
    assert candidate.build_causal_training_matrix(scope)[0].row_id


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": "wrong",
            "generation_digest": "d" * 64,
            "assignments": [],
        },
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "d" * 64,
            "assignments": {},
        },
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "d" * 64,
            "assignments": [{"tournament_id": "tournament:a", "role": "invalid"}],
        },
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "d" * 64,
            "assignments": [{"tournament_id": "tournament:a", "role": "training", "extra": True}],
        },
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "d" * 64,
            "assignments": [],
        },
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "d" * 64,
            "assignments": [
                {"tournament_id": "tournament:a", "role": "training"},
                {"tournament_id": "tournament:a", "role": "tuning"},
            ],
        },
    ],
)
def test_signed_role_authority_payload_is_closed(payload: object) -> None:
    signed, signer = _signed_authority_payload(payload)
    with pytest.raises(ValueError, match="schema|assignments|invalid|exactly"):
        compose_test_ml_candidate_authority(signed, signer=signer)


def test_signed_role_authority_rejects_wrong_kind_and_untrusted_signer() -> None:
    payload = {
        "schema_version": "strathmark-v3-ml-role-manifest-v3",
        "generation_digest": "d" * 64,
        "assignments": [
            {"tournament_id": f"tournament:{role.value}", "role": role.value}
            for role in (MLDataRole.TRAINING, MLDataRole.TUNING, MLDataRole.CALIBRATION)
        ],
    }
    signer = P256EphemeralSigner.generate("ml-role-wrong-kind")
    wrong_kind = sign_manifest(
        "other_manifest",
        payload,
        signer=signer,
        created_at="2026-01-01T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="ml_role_manifest"):
        compose_test_ml_candidate_authority(wrong_kind, signer=signer)

    signed, signer = _signed_authority_payload(payload)
    other = P256EphemeralSigner.generate("ml-role-untrusted")
    with pytest.raises(ValueError, match="invalid or untrusted"):
        compose_test_ml_candidate_authority(signed, signer=other)


def test_candidate_and_audit_authorities_have_disjoint_signed_role_sets() -> None:
    candidate_payload = {
        "schema_version": "strathmark-v3-ml-role-manifest-v3",
        "generation_digest": "d" * 64,
        "assignments": [{"tournament_id": "tournament:a", "role": "training"}],
    }
    signed, signer = _signed_authority_payload(candidate_payload)
    with pytest.raises(ValueError, match="training, tuning, and calibration only"):
        compose_test_ml_candidate_authority(signed, signer=signer)

    signer = P256EphemeralSigner.generate("ml-audit-wrong-role")
    audit_signed = sign_manifest(
        "ml_audit_role_manifest",
        candidate_payload,
        signer=signer,
        created_at="2026-01-01T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="locked-audit tournaments only"):
        compose_test_ml_audit_authority(audit_signed, signer=signer)


def test_locked_audit_matrix_has_one_evaluator_only_entrypoint() -> None:
    authority = _verified_audit_authority(("tournament:locked_audit",))
    audit_packet = _packet(
        (_observation(1, 40_000, day=1, tournament=MLDataRole.LOCKED_AUDIT.value),)
    )
    scoped = authority.authorize_packets((audit_packet,))
    assert not hasattr(authority, "build_causal_training_matrix")
    with pytest.raises(ValueError, match="authorized signed scope"):
        authority.build_locked_audit_replay_matrix(scoped.packets)  # type: ignore[arg-type]
    rows = authority.build_locked_audit_replay_matrix(scoped)
    assert rows.role is MLDataRole.LOCKED_AUDIT
    assert rows[0].row_id


@given(st.lists(st.integers(min_value=10_000, max_value=120_000), min_size=1, max_size=12))
def test_each_training_row_uses_only_strictly_prior_history(
    raw_times: list[int],
) -> None:
    packet = _packet(
        tuple(
            _observation(index, raw, day=index, tournament=f"t{index}")
            for index, raw in enumerate(raw_times, 1)
        )
    )
    rows = _authorized_rows((packet,))
    assert len(rows) == len(raw_times)
    for index, row in enumerate(rows):
        features = row.feature_dict
        assert features["history_depth"] == index
        assert row.training_max_sequence < row.observation_sequence
        assert row.training_max_occurred_at_utc < row.occurred_at_utc or index == 0
        assert set(features) == set(FEATURE_NAMES)
        assert not any(
            "formula" in name or "artifact" in name or "weight" in name for name in features
        )


def test_future_value_and_future_packet_mutations_cannot_change_prior_rows() -> None:
    original = _packet((_observation(1, 40_000, day=1), _observation(2, 45_000, day=2)))
    mutated = _packet((_observation(1, 40_000, day=1), _observation(2, 95_000, day=2)))
    earlier = _authorized_rows((original,))[0]
    changed = _authorized_rows((mutated,))[0]
    assert earlier.feature_dict == changed.feature_dict
    assert earlier.target_log_seconds == changed.target_log_seconds


def test_grouped_splits_exclude_same_date_and_every_future_group() -> None:
    rows = _authorized_rows(
        (
            _packet(
                (
                    _observation(1, 40_000, day=1, tournament="a"),
                    _observation(2, 41_000, day=2, tournament="b"),
                    _observation(3, 42_000, day=2, tournament="c"),
                    _observation(4, 43_000, day=3, tournament="d"),
                )
            ),
        ),
        MLDataRole.TUNING,
    )
    splits = rows.authority.grouped_rolling_origin_splits(rows.rows)
    assert splits
    for split in splits:
        train = [rows[index] for index in split.train_indices]
        validation = [rows[index] for index in split.validation_indices]
        assert max(item.occurred_at_utc[:10] for item in train) < min(
            item.occurred_at_utc[:10] for item in validation
        )
        assert not (
            {item.tournament_id for item in train} & {item.tournament_id for item in validation}
        )


def test_grouped_splits_exclude_the_validation_tournament_across_all_prior_dates() -> None:
    rows = _authorized_rows(
        (
            _packet(
                (
                    _observation(1, 40_000, day=1, tournament="repeat"),
                    _observation(2, 41_000, day=2, tournament="other"),
                    _observation(3, 42_000, day=3, tournament="repeat"),
                )
            ),
        ),
        MLDataRole.TUNING,
    )
    repeat_split = next(
        split
        for split in rows.authority.grouped_rolling_origin_splits(rows.rows)
        if split.validation_tournament_id == "tournament:repeat"
        and split.validation_date == "2026-07-03"
    )
    assert all(
        rows[index].tournament_id != "tournament:repeat" for index in repeat_split.train_indices
    )


@pytest.mark.parametrize(
    ("history_depth", "missing_fraction"),
    [(-1, 0.0), (math.inf, 0.0), (0, -0.1), (0, 1.1), (0, math.nan)],
)
def test_canonical_gate_transform_rejects_nonfinite_or_out_of_range_inputs(
    history_depth: float, missing_fraction: float
) -> None:
    with pytest.raises(ValueError, match="finite bounds"):
        canonical_gate_features(history_depth=history_depth, missing_fraction=missing_fraction)


def test_inference_uses_target_context_and_sealed_history_with_unseen_safe_values() -> None:
    unseen = _packet(
        (_observation(1, 40_000, day=1),),
        target=_context("invented_event", 375, "invented_species"),
    )
    features = build_inference_features(unseen)
    assert features["event_family"] == "invented_event"
    assert features["species"] == "invented_species"
    assert features["size_mm"] == 375
    assert features["history_depth"] == 1
    assert {
        "sequence_recency",
        "history_log_trend",
        "context_distance",
        "eligible_tournament_sequence",
        "current_form_log_seconds",
    } <= set(features)


def test_unsealed_or_digest_tampered_packets_fail_before_matrix_construction() -> None:
    packet = _packet((_observation(1, 40_000, day=1),))
    tampered = object.__new__(EvidencePacket)
    for name in EvidencePacket.__dataclass_fields__:
        object.__setattr__(tampered, name, getattr(packet, name))
    object.__setattr__(tampered, "content_digest", "f" * 64)
    authority = _verified_authority(
        (
            ("tournament:a", MLDataRole.TRAINING),
            ("tournament:b", MLDataRole.TUNING),
            ("tournament:c", MLDataRole.CALIBRATION),
        )
    )
    with pytest.raises(ValueError, match="sealed evidence packet"):
        authority.authorize_packets(MLDataRole.TRAINING, (tampered,))
    with pytest.raises(ValueError, match="EvidencePacket"):
        authority.authorize_packets(MLDataRole.TRAINING, (object(),))  # type: ignore[arg-type]


def test_noncompletion_is_not_a_target_and_duplicate_evidence_is_rejected() -> None:
    completion = _observation(1, 40_000, day=1)
    dnf = replace(
        _observation(2, 41_000, day=2),
        result=OfficialResult(ResultStatus.DNF, None, None, 1, None),
    )
    packet = _packet((completion, dnf))
    assert [row.row_id for row in _authorized_rows((packet,))] == [str(completion.evidence_id)]
    authority = _verified_authority(
        (
            ("tournament:a", MLDataRole.TRAINING),
            ("tournament:b", MLDataRole.TUNING),
            ("tournament:c", MLDataRole.CALIBRATION),
        )
    )
    with pytest.raises(ValueError, match="duplicate"):
        authority.build_causal_training_matrix(
            authority.authorize_packets(MLDataRole.TRAINING, (packet, packet))
        )
    with pytest.raises(ValueError, match="immutable"):
        authority.authorize_packets(MLDataRole.TRAINING, {packet})  # type: ignore[arg-type]


def _row() -> CausalTrainingRow:
    return _authorized_rows((_packet((_observation(1, 40_000, day=1),)),))[0]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"features": ()}, "schema"),
        ({"training_max_sequence": 1}, "sequence"),
        (
            {
                "training_max_sequence": 1,
                "observation_sequence": 2,
                "training_max_occurred_at_utc": "2026-07-02T12:00:00.000Z",
            },
            "time",
        ),
        ({"target_log_seconds": "1.0"}, "canonical"),
        ({"taxonomy_version": ""}, "taxonomy and conversion"),
    ],
)
def test_causal_row_contract_rejects_leakage_shapes(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_row(), **changes)


@pytest.mark.parametrize(
    "values",
    [(-1, 1, 1), (1, -1, 1), (1, 1, -1), (True, 1, 1)],
)
def test_specialist_eligibility_rejects_invalid_counts(
    values: tuple[object, ...],
) -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        SpecialistEligibility(*values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"pinball_advantage": math.nan}, "finite"),
        ({"history_depth": -1}, "bounds"),
        ({"missing_fraction": 2.0}, "bounds"),
        ({"specialist_better": 1}, "outcome"),
        ({"fold_id": ""}, "identity"),
    ],
)
def test_gate_example_rejects_untrusted_shapes(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(GateExample(0.1, 1, 0.0, True, "fold:a"), **changes)


def test_oof_contract_and_loss_fail_closed() -> None:
    valid = OOFComponentPrediction("row", "fold", (1.0,) * 7, None, "key", 0, 0.0)
    with pytest.raises(ValueError, match="seven"):
        replace(valid, universal_log_quantiles=(1.0,))
    with pytest.raises(ValueError, match="finite"):
        replace(valid, specialist_log_quantiles=(math.nan,) * 7)
    with pytest.raises(ValueError, match="support"):
        replace(valid, history_depth=-1)
    with pytest.raises(ValueError, match="seven"):
        mean_pinball_loss(1.0, (1.0,))
    with pytest.raises(ValueError, match="finite"):
        mean_pinball_loss(1.0, (math.nan,) * 7)
    assert replace(valid, specialist_log_quantiles=(1.0,) * 7)


def test_gate_and_hierarchy_reject_empty_or_non_oof_training() -> None:
    with pytest.raises(ValueError, match="at least two"):
        _fit_specialist_gate_values(())
    duplicate = GateExample(0.1, 1, 0.0, True, "fold:a")
    with pytest.raises(ValueError, match="at least two"):
        _fit_specialist_gate_values((duplicate, duplicate))
    with pytest.raises(ValueError, match="at least one"):
        _train_catboost_hierarchy((), model_factory=lambda **kwargs: object())


def test_every_ml_training_entrypoint_enforces_its_signed_role_and_coverage() -> None:
    training = _authorized_rows((_packet((_observation(1, 40_000, day=1),)),))
    tuning = _authorized_rows((_packet((_observation(1, 40_000, day=1),)),), MLDataRole.TUNING)
    calibration = _authorized_rows(
        (_packet((_observation(1, 40_000, day=1),)),), MLDataRole.CALIBRATION
    )
    with pytest.raises(ValueError, match="signature|authorized"):
        training.authority.specialist_eligibility(tuning.rows)
    with pytest.raises(ValueError, match="role"):
        training.authority.grouped_rolling_origin_splits(training.rows)

    tuning_predictions = tuning.authority.grouped_oof_component_predictions(
        tuning.rows, model_factory=lambda **kwargs: _OOFModel()
    )
    assert training.rows.rows == tuple(training.rows)
    assert tuning_predictions.predictions == tuple(tuning_predictions)
    other_tuning = _authorized_rows(
        (_packet((_observation(1, 40_000, day=1),)),), MLDataRole.TUNING
    )
    with pytest.raises(ValueError, match="signature|authority"):
        tuning.authority.gate_examples_from_oof(tuning_predictions, other_tuning.rows)
    one_fold = tuning.authority.gate_examples_from_oof(tuning_predictions, tuning.rows)
    assert one_fold.examples == ()
    with pytest.raises(ValueError, match="at least two"):
        tuning.authority.fit_specialist_gate(one_fold)

    gate_rows = _authorized_rows(
        (
            _packet(
                tuple(
                    _observation(index, 40_000 + index * 1_000, day=index, tournament=f"g{index}")
                    for index in range(1, 4)
                )
            ),
        ),
        MLDataRole.TUNING,
    )
    # Exact signed OOF scopes can only be minted by grouped causal prediction.
    authorized_controlled = gate_rows.authority.grouped_oof_component_predictions(
        gate_rows.rows, model_factory=lambda **kwargs: _OOFModel()
    )
    assert (
        gate_rows.authority.gate_examples_from_oof(authorized_controlled, gate_rows.rows).examples
        == ()
    )

    missing_calibration_predictions = calibration.authority.grouped_oof_component_predictions(
        calibration.rows, model_factory=lambda **kwargs: _OOFModel()
    )
    with pytest.raises(ValueError, match="trusted calibration-role authority"):
        calibration.authority.fit_pit_calibrator(
            calibration.rows,
            missing_calibration_predictions,
            object(),
        )
    with pytest.raises(ValueError, match="uniquely match"):
        calibration.authority.fit_pit_calibrator(
            calibration.rows,
            missing_calibration_predictions,
            SpecialistGate("0", (("log_history_depth", "0"), ("missing_fraction", "0"))),
        )
    with pytest.raises(ValueError, match="signature|authorized"):
        calibration.authority.fit_pit_calibrator(
            training.rows,
            tuning_predictions,
            SpecialistGate("0", (("log_history_depth", "0"), ("missing_fraction", "0"))),
        )

    assert training.authority.specialist_eligibility(training.rows)
    assert len(tuning_predictions) == 0
    assert _gate_value(GateExample(0.1, 1, 0.25, True, "fold:a"))["fold_id"] == "fold:a"


class _OOFModel:
    def fit(self, features: list[list[object]], targets: list[float], **kwargs: object) -> None:
        self.value = sum(targets) / len(targets)

    def predict(self, features: list[list[object]]) -> list[list[float]]:
        return [[self.value] * 7 for _ in features]


def test_grouped_oof_predictions_are_prior_only_and_universal_when_sparse() -> None:
    rows = _authorized_rows(
        (
            _packet(
                (
                    _observation(1, 40_000, day=1, tournament="a"),
                    _observation(2, 41_000, day=2, tournament="b"),
                    _observation(3, 42_000, day=3, tournament="c"),
                )
            ),
        ),
        MLDataRole.TUNING,
    )
    predictions = rows.authority.grouped_oof_component_predictions(
        rows.rows, model_factory=lambda **kwargs: _OOFModel()
    )
    assert len(predictions) == 2
    assert all(item.specialist_log_quantiles is None for item in predictions)


def test_calibration_role_fits_only_from_grouped_oof_prediction_subset() -> None:
    rows = _authorized_rows(
        (
            _packet(
                tuple(
                    _observation(index, 39_000 + index * 1_000, day=index, tournament=f"c{index}")
                    for index in range(1, 4)
                )
            ),
        ),
        MLDataRole.CALIBRATION,
    )
    predictions = rows.authority.grouped_oof_component_predictions(
        rows.rows, model_factory=lambda **kwargs: _OOFModel()
    )
    assert 0 < len(predictions) < len(rows)
    calibrator = rows.authority.fit_pit_calibrator(
        rows.rows,
        predictions,
        SpecialistGate("0", (("log_history_depth", "0"), ("missing_fraction", "0"))),
    )
    assert calibrator.role == "calibration"


def test_default_catboost_factory_and_array_prediction_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CatBoostRegressor(_OOFModel):
        def __init__(self, **settings: object):
            self.settings = settings

    monkeypatch.setitem(
        sys.modules, "catboost", SimpleNamespace(CatBoostRegressor=CatBoostRegressor)
    )
    rows = _authorized_rows((_packet((_observation(1, 40_000, day=1),)),))
    universal, specialists, _eligibility = rows.authority.train_catboost_hierarchy(rows.rows)
    assert universal.settings["loss_function"].startswith("MultiQuantile")
    assert specialists == {}
    array_like = type("ArrayLike", (), {"tolist": lambda self: [[1.0] * 7]})()
    assert _prediction_values(array_like) == (1.0,) * 7
