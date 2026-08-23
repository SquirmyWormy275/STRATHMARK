"""Causal hierarchical-ML training contracts for STRATHMARK V3.

This module constructs targets and features only from immutable evidence packets.  It
contains no formula-assessor output, ensemble weights, or mutable artifact lookup.
CatBoost is imported only by the explicit training entrypoint.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from enum import Enum
from statistics import median
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from strathmark.v3.assessors.ml import (
    PITCalibrator,
    SpecialistGate,
    build_positive_distribution,
    combine_quantiles,
)
from strathmark.v3.contracts.canonical import canonical_decimal_string, canonical_digest
from strathmark.v3.contracts.evidence import (
    EvidencePacket,
    ResultObservation,
    TargetContext,
    _require_digest,
)
from strathmark.v3.contracts.forecasts import PositiveTimeDistribution
from strathmark.v3.contracts.statuses import admit_raw_completion
from strathmark.v3.infrastructure.integrity import (
    IntegrityError,
    IntegrityKeyIdentity,
    IntegrityTrustStore,
    P256EphemeralSigner,
    P256Signer,
    SignedManifest,
    require_production_cng_signer,
    sign_manifest,
    verify_manifest,
)

QUANTILE_LEVELS = ("0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95")
CATEGORICAL_FEATURES = ("event_family", "species")
NUMERIC_FEATURES = (
    "size_mm",
    "density",
    "density_missing",
    "history_depth",
    "exact_history_depth",
    "history_log_median",
    "history_log_spread",
    "history_missing",
    "sequence_recency",
    "history_log_trend",
    "context_distance",
    "eligible_tournament_sequence",
    "current_form_log_seconds",
)
FEATURE_NAMES = (*CATEGORICAL_FEATURES, *NUMERIC_FEATURES)
GATE_FEATURE_NAMES = ("log_history_depth", "missing_fraction")
MIN_SPECIALIST_ROWS = 500
MIN_SPECIALIST_COMPETITORS = 30
MIN_SPECIALIST_TOURNAMENTS = 10


@dataclass(frozen=True, slots=True, order=True)
class SpecialistEligibility:
    admitted_rows: int
    competitors: int
    tournaments: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.admitted_rows, "admitted_rows"),
            (self.competitors, "competitors"),
            (self.tournaments, "tournaments"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a nonnegative integer")

    @property
    def available(self) -> bool:
        return (
            self.admitted_rows >= MIN_SPECIALIST_ROWS
            and self.competitors >= MIN_SPECIALIST_COMPETITORS
            and self.tournaments >= MIN_SPECIALIST_TOURNAMENTS
        )

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "admitted_rows": self.admitted_rows,
            "competitors": self.competitors,
            "tournaments": self.tournaments,
            "available": self.available,
        }


@dataclass(frozen=True, slots=True)
class CausalTrainingRow:
    row_id: str
    competitor_id: str
    tournament_id: str
    occurred_at_utc: str
    observation_sequence: int
    specialist_key: str
    features: tuple[tuple[str, object], ...]
    target_log_seconds: str
    source_packet_digest: str
    training_max_sequence: int
    training_max_occurred_at_utc: str
    field_id: str
    taxonomy_version: str
    conversion_version: str

    def __post_init__(self) -> None:
        if tuple(name for name, _ in self.features) != FEATURE_NAMES:
            raise ValueError(
                "ML row features must exactly match the frozen ordered schema"
            )
        if self.training_max_sequence >= self.observation_sequence:
            raise ValueError("ML row history must be strictly prior by sequence")
        if (
            self.training_max_sequence
            and self.training_max_occurred_at_utc >= self.occurred_at_utc
        ):
            raise ValueError("ML row history must be strictly prior in time")
        if canonical_decimal_string(self.target_log_seconds) != self.target_log_seconds:
            raise ValueError("ML target must be a canonical log-seconds decimal")
        if not self.taxonomy_version or not self.conversion_version:
            raise ValueError(
                "ML rows require explicit taxonomy and conversion versions"
            )

    @property
    def feature_dict(self) -> dict[str, object]:
        return dict(self.features)


@dataclass(frozen=True, slots=True)
class RollingOriginSplit:
    fold_id: str
    train_indices: tuple[int, ...]
    validation_indices: tuple[int, ...]
    validation_date: str
    validation_tournament_id: str


class MLDataRole(str, Enum):
    TRAINING = "training"
    TUNING = "tuning"
    CALIBRATION = "calibration"
    LOCKED_AUDIT = "locked_audit"


class MLAuthorityEnvironment(str, Enum):
    TEST_EPHEMERAL = "test_ephemeral"
    PRODUCTION_CNG = "production_cng"


@dataclass(frozen=True, slots=True, order=True)
class MLRoleAssignment:
    tournament_id: str
    role: MLDataRole

    def __post_init__(self) -> None:
        if not isinstance(self.tournament_id, str) or not self.tournament_id.startswith(
            "tournament:"
        ):
            raise ValueError("ML role assignment requires a tournament identity")
        if not isinstance(self.role, MLDataRole):
            raise ValueError("ML role assignment requires a closed data role")

    def to_dict(self) -> dict[str, str]:
        return {"tournament_id": self.tournament_id, "role": self.role.value}


@dataclass(frozen=True, slots=True)
class GateExample:
    pinball_advantage: float
    history_depth: int
    missing_fraction: float
    specialist_better: bool
    fold_id: str

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value)
            for value in (
                self.pinball_advantage,
                float(self.history_depth),
                self.missing_fraction,
            )
        ):
            raise ValueError("gate inputs must be finite")
        if self.history_depth < 0 or not 0 <= self.missing_fraction <= 1:
            raise ValueError("gate support inputs are outside their bounds")
        if not isinstance(self.specialist_better, bool) or not self.fold_id:
            raise ValueError("gate examples require an OOF outcome and fold identity")


@dataclass(frozen=True, slots=True)
class OOFComponentPrediction:
    row_id: str
    fold_id: str
    universal_log_quantiles: tuple[float, ...]
    specialist_log_quantiles: tuple[float, ...] | None
    specialist_key: str
    history_depth: int
    missing_fraction: float

    def __post_init__(self) -> None:
        for values in (self.universal_log_quantiles, self.specialist_log_quantiles):
            if values is not None and (
                len(values) != len(QUANTILE_LEVELS)
                or any(not math.isfinite(item) for item in values)
            ):
                raise ValueError(
                    "OOF component predictions require seven finite log quantiles"
                )
        if self.history_depth < 0 or not 0 <= self.missing_fraction <= 1:
            raise ValueError("OOF support features are invalid")


def _member_digest(kind: str, values: Sequence[object]) -> str:
    return canonical_digest(
        {
            "schema_version": "strathmark-v3-ml-authorized-members-v1",
            "kind": kind,
            "values": values,
        }
    )


def _row_value(row: CausalTrainingRow) -> dict[str, object]:
    return {
        "row_id": row.row_id,
        "competitor_id": row.competitor_id,
        "tournament_id": row.tournament_id,
        "occurred_at_utc": row.occurred_at_utc,
        "observation_sequence": row.observation_sequence,
        "specialist_key": row.specialist_key,
        "features": list(row.features),
        "target_log_seconds": row.target_log_seconds,
        "source_packet_digest": row.source_packet_digest,
        "training_max_sequence": row.training_max_sequence,
        "training_max_occurred_at_utc": row.training_max_occurred_at_utc,
        "field_id": row.field_id,
        "taxonomy_version": row.taxonomy_version,
        "conversion_version": row.conversion_version,
    }


def _oof_value(item: OOFComponentPrediction) -> dict[str, object]:
    return {
        "row_id": item.row_id,
        "fold_id": item.fold_id,
        "universal": list(item.universal_log_quantiles),
        "specialist": (
            None
            if item.specialist_log_quantiles is None
            else list(item.specialist_log_quantiles)
        ),
        "specialist_key": item.specialist_key,
        "history_depth": item.history_depth,
        "missing_fraction": item.missing_fraction,
    }


def _gate_value(item: GateExample) -> dict[str, object]:
    return {
        "pinball_advantage": canonical_decimal_string(item.pinball_advantage),
        "history_depth": item.history_depth,
        "missing_fraction": canonical_decimal_string(item.missing_fraction),
        "specialist_better": item.specialist_better,
        "fold_id": item.fold_id,
    }


def _parse_role_manifest(
    signed_manifest: SignedManifest,
    trust_store: IntegrityTrustStore,
    *,
    kind: str,
) -> tuple[tuple[MLRoleAssignment, ...], str, str]:
    if not isinstance(signed_manifest, SignedManifest) or signed_manifest.kind != kind:
        raise ValueError(f"ML role authority requires the signed {kind} kind")
    try:
        value = verify_manifest(signed_manifest, trust_store)
    except IntegrityError as exc:
        raise ValueError("ML role manifest signature is invalid or untrusted") from exc
    if (
        set(value) != {"schema_version", "generation_digest", "assignments"}
        or value["schema_version"] != "strathmark-v3-ml-role-manifest-v3"
    ):
        raise ValueError("ML role manifest payload schema is invalid")
    raw_assignments = value["assignments"]
    if not isinstance(raw_assignments, list):
        raise ValueError("ML role assignments must be a closed list")
    try:
        assignments = tuple(
            MLRoleAssignment(item["tournament_id"], MLDataRole(item["role"]))
            for item in raw_assignments
            if isinstance(item, Mapping) and set(item) == {"tournament_id", "role"}
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("ML role assignments are invalid") from exc
    if len(assignments) != len(raw_assignments):
        raise ValueError("ML role assignments contain unknown fields")
    if not assignments or len({item.tournament_id for item in assignments}) != len(
        assignments
    ):
        raise ValueError("each whole tournament must have exactly one ML data role")
    _require_digest(value["generation_digest"], "ML role generation_digest")
    authority_digest = canonical_digest(
        {
            "schema_version": "strathmark-v3-ml-role-authority-binding-v1",
            "signed_manifest_digest": signed_manifest.body_digest,
            "signer_identity": trust_store.identity(signed_manifest.key_id).to_dict(),
        }
    )
    return tuple(sorted(assignments)), value["generation_digest"], authority_digest


class MLRoleManifest:
    __slots__ = ("_assignments", "_generation_digest", "_manifest_digest")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("ML role manifests require pinned signature verification")

    @property
    def assignments(self) -> tuple[MLRoleAssignment, ...]:
        return self._assignments

    @property
    def generation_digest(self) -> str:
        return self._generation_digest

    @property
    def manifest_digest(self) -> str:
        return self._manifest_digest

    def _validate(self) -> None:
        raise ValueError("ML role manifest is not signature-authorized")


class _AuthorizedSequence(Sequence):
    __slots__ = ("_members", "_authorization_envelope")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("authorized ML scopes are factory-only signed envelopes")

    def _payload(self) -> Mapping[str, Any]:
        try:
            payload = self._authorization_envelope.body()["payload"]
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("ML scope authorization envelope is malformed") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("ML scope authorization envelope is malformed")
        return payload

    @property
    def role(self) -> MLDataRole:
        try:
            return MLDataRole(self._payload()["role"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("ML scope authorization role is malformed") from exc

    @property
    def authority_digest(self) -> str:
        return str(self._payload().get("authority_manifest_digest", ""))

    @property
    def taxonomy_version(self) -> str:
        return str(self._payload().get("taxonomy_version", ""))

    @property
    def conversion_version(self) -> str:
        return str(self._payload().get("conversion_version", ""))

    def __len__(self) -> int:
        return len(self._members)

    def __iter__(self):
        return iter(self._members)

    def __getitem__(self, index):
        return self._members[index]


class AuthorizedMLPackets(_AuthorizedSequence):
    __slots__ = ()

    @property
    def packets(self) -> tuple[EvidencePacket, ...]:
        return self._members


class AuthorizedMLRows(_AuthorizedSequence):
    __slots__ = ()

    @property
    def rows(self) -> tuple[CausalTrainingRow, ...]:
        return self._members


class AuthorizedOOFPredictions(_AuthorizedSequence):
    __slots__ = ()

    @property
    def predictions(self) -> tuple[OOFComponentPrediction, ...]:
        return self._members


class AuthorizedGateExamples(_AuthorizedSequence):
    __slots__ = ()

    @property
    def examples(self) -> tuple[GateExample, ...]:
        return self._members


class TrustedMLRoleAuthority:
    __slots__ = ()

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("ML candidate authority requires pinned composition")


class TrustedMLAuditAuthority:
    __slots__ = ()

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("ML audit authority requires pinned composition")


def _scope_member_values(scope_type: type, members: tuple[object, ...]) -> list[object]:
    if scope_type is AuthorizedMLPackets:
        return [item.content_digest for item in members]
    if scope_type is AuthorizedMLRows:
        return [_row_value(item) for item in members]
    if scope_type is AuthorizedOOFPredictions:
        return [_oof_value(item) for item in members]
    if scope_type is AuthorizedGateExamples:
        return [_gate_value(item) for item in members]
    raise ValueError("unknown ML authorization scope type")


def _new_signed_scope(
    scope_type: type,
    members: tuple[object, ...],
    envelope: SignedManifest,
):
    instance = object.__new__(scope_type)
    object.__setattr__(instance, "_members", members)
    object.__setattr__(instance, "_authorization_envelope", envelope)
    return instance


def _compose_ml_authority(
    *,
    signed_manifest: SignedManifest,
    pinned_identity: IntegrityKeyIdentity,
    scope_signer: P256Signer,
    audit: bool,
    environment: MLAuthorityEnvironment,
) -> TrustedMLRoleAuthority | TrustedMLAuditAuthority:
    if environment is MLAuthorityEnvironment.PRODUCTION_CNG:
        try:
            observed_identity = require_production_cng_signer(scope_signer)
        except IntegrityError as exc:
            raise ValueError(
                "production ML authority requires a live OS-attested Windows CNG signer"
            ) from exc
    elif not isinstance(scope_signer, P256EphemeralSigner):
        raise ValueError("test ML authority requires an explicit ephemeral test signer")
    else:
        observed_identity = scope_signer.identity
    if observed_identity != pinned_identity:  # pragma: no cover - composition prechecks
        raise ValueError("ML scope signer differs from the composition-pinned identity")
    trust_store = IntegrityTrustStore((pinned_identity,))
    kind = "ml_audit_role_manifest" if audit else "ml_role_manifest"
    assignments, generation, authority_digest = _parse_role_manifest(
        signed_manifest, trust_store, kind=kind
    )
    roles = {item.role for item in assignments}
    if audit:
        if roles != {MLDataRole.LOCKED_AUDIT}:
            raise ValueError(
                "audit ML manifest may contain locked-audit tournaments only"
            )
    elif roles != {
        MLDataRole.TRAINING,
        MLDataRole.TUNING,
        MLDataRole.CALIBRATION,
    }:
        raise ValueError(
            "candidate ML manifest requires training, tuning, and calibration only"
        )
    assignment_map = {item.tournament_id: item.role for item in assignments}
    signer_identity = pinned_identity.to_dict()
    created_at = signed_manifest.body()["created_at"]
    manifest_body = {
        "schema_version": "strathmark-v3-ml-role-manifest-view-v1",
        "assignments": [item.to_dict() for item in assignments],
        "generation_digest": generation,
    }
    manifest = object.__new__(MLRoleManifest)
    object.__setattr__(manifest, "_assignments", assignments)
    object.__setattr__(manifest, "_generation_digest", generation)
    object.__setattr__(manifest, "_manifest_digest", canonical_digest(manifest_body))

    def sign_scope(
        scope_type: type,
        members: tuple[object, ...],
        *,
        role: MLDataRole,
        purpose: str,
        taxonomy_version: str,
        conversion_version: str,
        source_digest: str,
    ):
        payload = {
            "schema_version": "strathmark-v3-ml-scope-authorization-v1",
            "role": role.value,
            "purpose": purpose,
            "member_digest": _member_digest(
                purpose, _scope_member_values(scope_type, members)
            ),
            "taxonomy_version": taxonomy_version,
            "conversion_version": conversion_version,
            "authority_manifest_digest": authority_digest,
            "source_digest": source_digest,
            "signer_identity": signer_identity,
        }
        envelope = sign_manifest(
            "ml_scope_authorization",
            payload,
            signer=scope_signer,
            created_at=created_at,
        )
        return _new_signed_scope(scope_type, members, envelope)

    def verify_scope(
        scope: object,
        scope_type: type,
        *,
        purpose: str,
        roles: tuple[MLDataRole, ...],
    ) -> Mapping[str, Any]:
        allowed = (
            {
                "audit_packets": frozenset({MLDataRole.LOCKED_AUDIT}),
                "audit_rows": frozenset({MLDataRole.LOCKED_AUDIT}),
            }
            if audit
            else {
                "candidate_packets": frozenset(
                    {
                        MLDataRole.TRAINING,
                        MLDataRole.TUNING,
                        MLDataRole.CALIBRATION,
                    }
                ),
                "candidate_rows": frozenset(
                    {
                        MLDataRole.TRAINING,
                        MLDataRole.TUNING,
                        MLDataRole.CALIBRATION,
                    }
                ),
                "oof_predictions": frozenset(
                    {MLDataRole.TUNING, MLDataRole.CALIBRATION}
                ),
                "gate_examples": frozenset({MLDataRole.TUNING}),
            }
        )
        if purpose not in allowed or not frozenset(roles) <= allowed[purpose]:
            raise ValueError(
                "ML scope role and purpose are forbidden for this authority type"
            )
        if type(scope) is not scope_type:
            raise ValueError("ML entrypoint requires an authorized signed scope")
        try:
            payload = verify_manifest(scope._authorization_envelope, trust_store)
        except (AttributeError, IntegrityError, TypeError, ValueError) as exc:
            raise ValueError(
                "ML scope signature is invalid or not composition-authorized"
            ) from exc
        required = {
            "schema_version",
            "role",
            "purpose",
            "member_digest",
            "taxonomy_version",
            "conversion_version",
            "authority_manifest_digest",
            "source_digest",
            "signer_identity",
        }
        try:
            role = MLDataRole(payload["role"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("ML scope signed role is invalid") from exc
        if (
            set(payload) != required
            or payload["schema_version"] != "strathmark-v3-ml-scope-authorization-v1"
            or role not in roles
            or payload["purpose"] != purpose
            or payload["authority_manifest_digest"] != authority_digest
            or payload["signer_identity"] != signer_identity
            or payload["member_digest"]
            != _member_digest(purpose, _scope_member_values(scope_type, scope._members))
        ):
            raise ValueError(
                "ML scope signature, authority, role, purpose, or members differ"
            )
        _require_digest(payload["source_digest"], "ML scope source_digest")
        if not isinstance(payload["taxonomy_version"], str) or not isinstance(
            payload["conversion_version"], str
        ):
            raise ValueError("ML scope taxonomy or conversion binding is invalid")
        if scope_type in (AuthorizedMLPackets, AuthorizedMLRows) and scope._members:
            if (
                {item.taxonomy_version for item in scope._members}
                != {payload["taxonomy_version"]}
                or {item.conversion_version for item in scope._members}
                != {payload["conversion_version"]}
            ):
                raise ValueError(
                    "ML scope taxonomy or conversion differs from its signed members"
                )
        return payload

    def authorize_packets(role: MLDataRole, packets: tuple[EvidencePacket, ...]):
        expected = (
            (MLDataRole.LOCKED_AUDIT,)
            if audit
            else (
                MLDataRole.TRAINING,
                MLDataRole.TUNING,
                MLDataRole.CALIBRATION,
            )
        )
        if role not in expected:
            raise ValueError(
                "candidate authority cannot access locked audit"
                if not audit
                else "audit authority accepts locked audit only"
            )
        if (
            not isinstance(role, MLDataRole)
            or not isinstance(packets, tuple)
            or not packets
        ):
            raise ValueError(
                "ML role scope requires a closed role and immutable nonempty packets"
            )
        for packet in packets:
            _verify_packet(packet)
            tournaments = {str(item.tournament_id) for item in packet.observations}
            if not tournaments or any(
                assignment_map.get(item) is not role for item in tournaments
            ):
                raise ValueError(
                    "ML packets do not belong exclusively to the authorized role"
                )
        taxonomies = {item.taxonomy_version for item in packets}
        conversions = {item.conversion_version for item in packets}
        if len(taxonomies) != 1 or len(conversions) != 1:
            raise ValueError(
                "authorized ML packets require one taxonomy and conversion version"
            )
        return sign_scope(
            AuthorizedMLPackets,
            packets,
            role=role,
            purpose="audit_packets" if audit else "candidate_packets",
            taxonomy_version=next(iter(taxonomies)),
            conversion_version=next(iter(conversions)),
            source_digest=manifest.manifest_digest,
        )

    base = TrustedMLAuditAuthority if audit else TrustedMLRoleAuthority

    class PinnedAuthority(base):
        __slots__ = ()

        @property
        def manifest(self) -> MLRoleManifest:
            return manifest

        @property
        def signed_manifest_digest(self) -> str:
            return authority_digest

        @property
        def signer_key_id(self) -> str:
            return pinned_identity.key_id

        @property
        def authority_environment(self) -> MLAuthorityEnvironment:
            return environment

        @property
        def production_ready(self) -> bool:
            return environment is MLAuthorityEnvironment.PRODUCTION_CNG

        def require_production_ready(self) -> None:
            if not self.production_ready:
                raise ValueError(
                    "test-ephemeral ML authority is not production-authoritative"
                )

        def authorize_packets(self, *args):
            role, packets = (
                (MLDataRole.LOCKED_AUDIT, args[0]) if audit else (args[0], args[1])
            )
            return authorize_packets(role, packets)

        def build_causal_training_matrix(self, scoped_packets):
            payload = verify_scope(
                scoped_packets,
                AuthorizedMLPackets,
                purpose="candidate_packets",
                roles=(MLDataRole.TRAINING, MLDataRole.TUNING, MLDataRole.CALIBRATION),
            )
            rows = _build_causal_matrix_values(scoped_packets.packets)
            return sign_scope(
                AuthorizedMLRows,
                rows,
                role=MLDataRole(payload["role"]),
                purpose="candidate_rows",
                taxonomy_version=payload["taxonomy_version"],
                conversion_version=payload["conversion_version"],
                source_digest=scoped_packets._authorization_envelope.body_digest,
            )

        def build_locked_audit_replay_matrix(self, scoped_packets):
            payload = verify_scope(
                scoped_packets,
                AuthorizedMLPackets,
                purpose="audit_packets",
                roles=(MLDataRole.LOCKED_AUDIT,),
            )
            rows = _build_causal_matrix_values(scoped_packets.packets)
            return sign_scope(
                AuthorizedMLRows,
                rows,
                role=MLDataRole.LOCKED_AUDIT,
                purpose="audit_rows",
                taxonomy_version=payload["taxonomy_version"],
                conversion_version=payload["conversion_version"],
                source_digest=scoped_packets._authorization_envelope.body_digest,
            )

        def _verify_rows(self, rows, roles):
            purpose = (
                "audit_rows"
                if roles == (MLDataRole.LOCKED_AUDIT,)
                else "candidate_rows"
            )
            return verify_scope(rows, AuthorizedMLRows, purpose=purpose, roles=roles)

        def grouped_rolling_origin_splits(self, rows):
            self._verify_rows(rows, (MLDataRole.TUNING, MLDataRole.CALIBRATION))
            return _grouped_rolling_origin_splits_values(rows)

        def specialist_eligibility(self, rows):
            self._verify_rows(rows, (MLDataRole.TRAINING,))
            return _specialist_eligibility(rows)

        def train_catboost_hierarchy(self, rows, **kwargs):
            self._verify_rows(rows, (MLDataRole.TRAINING,))
            return _train_catboost_hierarchy(rows, **kwargs)

        def grouped_oof_component_predictions(self, rows, *, model_factory):
            payload = self._verify_rows(
                rows, (MLDataRole.TUNING, MLDataRole.CALIBRATION)
            )
            predictions = _grouped_oof_component_prediction_values(
                rows, model_factory=model_factory
            )
            return sign_scope(
                AuthorizedOOFPredictions,
                predictions,
                role=MLDataRole(payload["role"]),
                purpose="oof_predictions",
                taxonomy_version=payload["taxonomy_version"],
                conversion_version=payload["conversion_version"],
                source_digest=rows._authorization_envelope.body_digest,
            )

        def gate_examples_from_oof(self, predictions, rows):
            row_payload = self._verify_rows(rows, (MLDataRole.TUNING,))
            prediction_payload = verify_scope(
                predictions,
                AuthorizedOOFPredictions,
                purpose="oof_predictions",
                roles=(MLDataRole.TUNING,),
            )
            if (
                prediction_payload["source_digest"]
                != rows._authorization_envelope.body_digest
            ):
                raise ValueError(
                    "gate examples do not share the authorized tuning rows"
                )
            examples = _gate_examples_values(predictions, rows)
            return sign_scope(
                AuthorizedGateExamples,
                examples,
                role=MLDataRole.TUNING,
                purpose="gate_examples",
                taxonomy_version=row_payload["taxonomy_version"],
                conversion_version=row_payload["conversion_version"],
                source_digest=predictions._authorization_envelope.body_digest,
            )

        def fit_specialist_gate(self, examples, **kwargs):
            verify_scope(
                examples,
                AuthorizedGateExamples,
                purpose="gate_examples",
                roles=(MLDataRole.TUNING,),
            )
            return _fit_specialist_gate_values(examples, **kwargs)

        def fit_pit_calibrator(self, rows, predictions, gate):
            self._verify_rows(rows, (MLDataRole.CALIBRATION,))
            prediction_payload = verify_scope(
                predictions,
                AuthorizedOOFPredictions,
                purpose="oof_predictions",
                roles=(MLDataRole.CALIBRATION,),
            )
            if (
                prediction_payload["source_digest"]
                != rows._authorization_envelope.body_digest
            ):
                raise ValueError(
                    "PIT fitting requires one trusted calibration-role authority"
                )
            return _fit_pit_calibrator_values(rows, predictions, gate)

        def evaluate_frozen_replay(self, rows, bundle, consequence_evaluator):
            self._verify_rows(rows, (MLDataRole.LOCKED_AUDIT,))
            return _evaluate_frozen_replay(rows, bundle, consequence_evaluator)

    if audit:
        for method_name in (
            "build_causal_training_matrix",
            "fit_pit_calibrator",
            "fit_specialist_gate",
            "gate_examples_from_oof",
            "grouped_oof_component_predictions",
            "grouped_rolling_origin_splits",
            "specialist_eligibility",
            "train_catboost_hierarchy",
        ):
            delattr(PinnedAuthority, method_name)
        PinnedAuthority.__name__ = "PinnedMLAuditAuthority"
    else:
        delattr(PinnedAuthority, "build_locked_audit_replay_matrix")
        delattr(PinnedAuthority, "evaluate_frozen_replay")
        PinnedAuthority.__name__ = "PinnedMLCandidateAuthority"
    return object.__new__(PinnedAuthority)


def _compose_ml_candidate_authority(
    signed_manifest: SignedManifest,
    pinned_identity: IntegrityKeyIdentity,
    scope_signer: P256Signer,
    *,
    environment: MLAuthorityEnvironment,
) -> TrustedMLRoleAuthority:
    return _compose_ml_authority(
        signed_manifest=signed_manifest,
        pinned_identity=pinned_identity,
        scope_signer=scope_signer,
        audit=False,
        environment=environment,
    )


def _compose_ml_audit_authority(
    signed_manifest: SignedManifest,
    pinned_identity: IntegrityKeyIdentity,
    scope_signer: P256Signer,
    *,
    environment: MLAuthorityEnvironment,
) -> TrustedMLAuditAuthority:
    return _compose_ml_authority(
        signed_manifest=signed_manifest,
        pinned_identity=pinned_identity,
        scope_signer=scope_signer,
        audit=True,
        environment=environment,
    )


@dataclass(frozen=True, slots=True, order=True)
class MLReplaySlice:
    name: str
    row_count: int
    mean_pinball_loss: str
    median_absolute_error_seconds: str
    tail_absolute_error_seconds: str
    interval_90_coverage: str
    interval_90_sharpness_seconds: str
    calibration_error: str
    mean_absolute_mark_error_seconds: str
    counterfactual_spread_error_seconds: str
    win_probability_distortion: str
    class_context_bias_seconds: str
    gap_error_seconds: str
    breakout_exposure: str
    optimizer_repair_seconds: str


@dataclass(frozen=True, slots=True, order=True)
class MarkConsequenceOutcome:
    row_id: str
    mark_error_seconds: str
    spread_error_seconds: str
    win_probability_distortion: str
    class_context_bias_seconds: str
    gap_error_seconds: str
    breakout_exposure: str
    optimizer_repair_seconds: str

    def __post_init__(self) -> None:
        nonnegative = (
            self.mark_error_seconds,
            self.spread_error_seconds,
            self.win_probability_distortion,
            self.gap_error_seconds,
            self.breakout_exposure,
            self.optimizer_repair_seconds,
        )
        canonical = tuple(canonical_decimal_string(value) for value in nonnegative)
        if not self.row_id or canonical != nonnegative:
            raise ValueError(
                "mark consequence outcomes require canonical decimal metrics"
            )
        if any(float(value) < 0 for value in canonical):
            raise ValueError(
                "mark consequence outcomes require canonical nonnegative metrics"
            )
        if (
            canonical_decimal_string(self.class_context_bias_seconds)
            != self.class_context_bias_seconds
        ):
            raise ValueError(
                "mark consequence outcomes require canonical decimal metrics"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "row_id": self.row_id,
            "mark_error_seconds": self.mark_error_seconds,
            "spread_error_seconds": self.spread_error_seconds,
            "win_probability_distortion": self.win_probability_distortion,
            "class_context_bias_seconds": self.class_context_bias_seconds,
            "gap_error_seconds": self.gap_error_seconds,
            "breakout_exposure": self.breakout_exposure,
            "optimizer_repair_seconds": self.optimizer_repair_seconds,
        }


@dataclass(frozen=True, slots=True)
class MarkConsequenceFieldInput:
    field_id: str
    forecasts: tuple[tuple[str, PositiveTimeDistribution], ...]
    actual_raw_times_ms: tuple[tuple[str, int], ...]
    field_distribution_digest: str
    input_digest: str

    @classmethod
    def create(
        cls,
        *,
        field_id: str,
        forecasts: tuple[tuple[str, PositiveTimeDistribution], ...],
        actual_raw_times_ms: tuple[tuple[str, int], ...],
    ) -> MarkConsequenceFieldInput:
        if not field_id.startswith("field:") or not forecasts:
            raise ValueError("mark consequence input requires one exact nonempty field")
        if not isinstance(forecasts, tuple) or not all(
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and item[0]
            and isinstance(item[1], PositiveTimeDistribution)
            for item in forecasts
        ):
            raise ValueError(
                "mark consequence forecasts require immutable typed distributions"
            )
        if not isinstance(actual_raw_times_ms, tuple) or not all(
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and item[0]
            and isinstance(item[1], int)
            and not isinstance(item[1], bool)
            and item[1] > 0
            for item in actual_raw_times_ms
        ):
            raise ValueError(
                "mark consequence actual times require a positive integer millisecond"
            )
        forecast_ids = tuple(item[0] for item in forecasts)
        actual_ids = tuple(item[0] for item in actual_raw_times_ms)
        if (
            forecast_ids != tuple(sorted(forecast_ids))
            or actual_ids != forecast_ids
            or len(set(forecast_ids)) != len(forecast_ids)
        ):
            raise ValueError(
                "mark consequence forecasts and actuals require the same exact rows"
            )
        forecast_value = [
            {"row_id": row_id, "distribution": distribution.to_dict()}
            for row_id, distribution in forecasts
        ]
        distribution_digest = canonical_digest(forecast_value)
        body = {
            "schema_version": "strathmark-v3-ml-mark-consequence-input-v1",
            "field_id": field_id,
            "field_distribution_digest": distribution_digest,
            "actual_raw_times_ms": [
                {"row_id": row_id, "raw_time_ms": raw_time_ms}
                for row_id, raw_time_ms in actual_raw_times_ms
            ],
        }
        return cls(
            field_id,
            forecasts,
            actual_raw_times_ms,
            distribution_digest,
            canonical_digest(body),
        )


def _validate_consequence_receipt_fields(
    field_id: str,
    input_digest: str,
    outcomes: tuple[MarkConsequenceOutcome, ...],
) -> None:
    if not isinstance(field_id, str) or not field_id.startswith("field:"):
        raise ValueError("mark consequence receipt requires one exact field identity")
    _require_digest(input_digest, "mark consequence receipt input_digest")
    if (
        not isinstance(outcomes, tuple)
        or not outcomes
        or not all(isinstance(item, MarkConsequenceOutcome) for item in outcomes)
    ):
        raise ValueError("mark consequence receipt requires typed outcomes")
    row_ids = tuple(item.row_id for item in outcomes)
    if row_ids != tuple(sorted(row_ids)) or len(set(row_ids)) != len(row_ids):
        raise ValueError(
            "mark consequence receipt outcomes must be unique nonempty ordered rows"
        )


@dataclass(frozen=True, slots=True)
class MarkConsequenceReceipt:
    field_id: str
    input_digest: str
    outcomes: tuple[MarkConsequenceOutcome, ...]
    receipt_digest: str

    @classmethod
    def create(
        cls,
        *,
        field_id: str,
        input_digest: str,
        outcomes: tuple[MarkConsequenceOutcome, ...],
    ) -> MarkConsequenceReceipt:
        _validate_consequence_receipt_fields(field_id, input_digest, outcomes)
        body = {
            "schema_version": "strathmark-v3-ml-mark-consequence-receipt-v1",
            "field_id": field_id,
            "input_digest": input_digest,
            "outcomes": [item.to_dict() for item in outcomes],
        }
        return cls(field_id, input_digest, outcomes, canonical_digest(body))

    def __post_init__(self) -> None:
        _validate_consequence_receipt_fields(
            self.field_id, self.input_digest, self.outcomes
        )
        body = {
            "schema_version": "strathmark-v3-ml-mark-consequence-receipt-v1",
            "field_id": self.field_id,
            "input_digest": self.input_digest,
            "outcomes": [item.to_dict() for item in self.outcomes],
        }
        if canonical_digest(body) != self.receipt_digest:
            raise ValueError("mark consequence receipt digest differs")


class MarkConsequenceEvaluator(Protocol):
    def evaluate(
        self, field_input: MarkConsequenceFieldInput
    ) -> MarkConsequenceReceipt: ...


def _revalidate_consequence_receipt(
    receipt: object,
    field_input: MarkConsequenceFieldInput,
    *,
    expected_input_digest: str,
    expected_distribution_digest: str,
    expected_row_ids: tuple[str, ...],
) -> MarkConsequenceReceipt:
    """Recompute all evaluator-controlled values after the port returns."""

    try:
        rebuilt_input = MarkConsequenceFieldInput.create(
            field_id=field_input.field_id,
            forecasts=field_input.forecasts,
            actual_raw_times_ms=field_input.actual_raw_times_ms,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            "mark consequence receipt is not bound to the exact field input"
        ) from exc
    if (
        rebuilt_input != field_input
        or rebuilt_input.input_digest != expected_input_digest
        or rebuilt_input.field_distribution_digest != expected_distribution_digest
        or not isinstance(receipt, MarkConsequenceReceipt)
    ):
        raise ValueError(
            "mark consequence receipt is not bound to the exact field input"
        )
    try:
        rebuilt_outcomes = tuple(
            MarkConsequenceOutcome(**item.to_dict()) for item in receipt.outcomes
        )
        rebuilt_receipt = MarkConsequenceReceipt.create(
            field_id=receipt.field_id,
            input_digest=receipt.input_digest,
            outcomes=rebuilt_outcomes,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("mark consequence receipt values are not canonical") from exc
    if (
        rebuilt_receipt != receipt
        or receipt.field_id != field_input.field_id
        or receipt.input_digest != expected_input_digest
        or tuple(item.row_id for item in rebuilt_outcomes) != expected_row_ids
    ):
        raise ValueError(
            "mark consequence receipt digest or exact field input binding differs"
        )
    return rebuilt_receipt


@dataclass(frozen=True, slots=True)
class FrozenMLReplayReport:
    slices: tuple[MLReplaySlice, ...]
    grouped_field_count: int
    consequence_receipt_digests: tuple[tuple[str, str], ...]
    report_digest: str

    @classmethod
    def create(
        cls,
        *,
        slices: tuple[MLReplaySlice, ...],
        grouped_field_count: int,
        consequence_receipt_digests: tuple[tuple[str, str], ...],
    ) -> FrozenMLReplayReport:
        body = {
            "schema_version": "strathmark-v3-ml-frozen-replay-v1",
            "slices": [
                {
                    "name": item.name,
                    "row_count": item.row_count,
                    "mean_pinball_loss": item.mean_pinball_loss,
                    "median_absolute_error_seconds": item.median_absolute_error_seconds,
                    "tail_absolute_error_seconds": item.tail_absolute_error_seconds,
                    "interval_90_coverage": item.interval_90_coverage,
                    "interval_90_sharpness_seconds": item.interval_90_sharpness_seconds,
                    "calibration_error": item.calibration_error,
                    "mean_absolute_mark_error_seconds": item.mean_absolute_mark_error_seconds,
                    "counterfactual_spread_error_seconds": item.counterfactual_spread_error_seconds,
                    "win_probability_distortion": item.win_probability_distortion,
                    "class_context_bias_seconds": item.class_context_bias_seconds,
                    "gap_error_seconds": item.gap_error_seconds,
                    "breakout_exposure": item.breakout_exposure,
                    "optimizer_repair_seconds": item.optimizer_repair_seconds,
                }
                for item in slices
            ],
            "grouped_field_count": grouped_field_count,
            "consequence_receipt_digests": [
                {"field_id": field_id, "receipt_digest": digest}
                for field_id, digest in consequence_receipt_digests
            ],
        }
        return cls(
            slices,
            grouped_field_count,
            consequence_receipt_digests,
            canonical_digest(body),
        )


def _build_causal_matrix_values(
    packets: tuple[EvidencePacket, ...],
) -> tuple[CausalTrainingRow, ...]:
    rows: list[CausalTrainingRow] = []
    seen_evidence: set[str] = set()
    for packet in packets:
        _verify_packet(packet)
        for observation in packet.observations:
            admitted = admit_raw_completion(observation.result)
            if admitted is None:
                continue
            evidence_id = str(observation.evidence_id)
            if evidence_id in seen_evidence:
                raise ValueError(
                    "duplicate evidence cannot be multiplied across ML packets"
                )
            seen_evidence.add(evidence_id)
            history = tuple(
                candidate
                for candidate in packet.observations
                if candidate.observation_sequence < observation.observation_sequence
                and candidate.occurred_at_utc < observation.occurred_at_utc
                and admit_raw_completion(candidate.result) is not None
            )
            maximum = max((item.observation_sequence for item in history), default=0)
            features = _features(
                observation.context,
                history,
                eligible_sequence=observation.observation_sequence - 1,
            )
            maximum_time = max(
                (item.occurred_at_utc for item in history),
                default="0001-01-01T00:00:00.000Z",
            )
            rows.append(
                CausalTrainingRow(
                    row_id=evidence_id,
                    competitor_id=str(observation.competitor_id),
                    tournament_id=str(observation.tournament_id),
                    occurred_at_utc=observation.occurred_at_utc,
                    observation_sequence=observation.observation_sequence,
                    specialist_key=context_key(observation.context),
                    features=tuple((name, features[name]) for name in FEATURE_NAMES),
                    target_log_seconds=canonical_decimal_string(
                        math.log(admitted.raw_time_ms / 1000.0)
                    ),
                    source_packet_digest=packet.content_digest,
                    training_max_sequence=maximum,
                    training_max_occurred_at_utc=maximum_time,
                    field_id=str(observation.field_id),
                    taxonomy_version=packet.taxonomy_version,
                    conversion_version=packet.conversion_version,
                )
            )
    return tuple(sorted(rows, key=lambda item: (item.occurred_at_utc, item.row_id)))


def build_inference_features(packet: EvidencePacket) -> dict[str, object]:
    _verify_packet(packet)
    history = tuple(
        item
        for item in packet.observations
        if admit_raw_completion(item.result) is not None
    )
    return _features(
        packet.target_context,
        history,
        eligible_sequence=packet.tournament_event_sequence,
    )


def _grouped_rolling_origin_splits_values(
    rows: Sequence[CausalTrainingRow],
) -> tuple[RollingOriginSplit, ...]:
    """Build tournament/date validation groups with only earlier dates in training."""

    groups: dict[tuple[str, str], list[int]] = {}
    for index, row in enumerate(rows):
        groups.setdefault((row.occurred_at_utc[:10], row.tournament_id), []).append(
            index
        )
    splits: list[RollingOriginSplit] = []
    for date_key, tournament_id in sorted(groups):
        validation = tuple(groups[(date_key, tournament_id)])
        training = tuple(
            index
            for index, row in enumerate(rows)
            if row.occurred_at_utc[:10] < date_key
            and row.tournament_id != tournament_id
        )
        if not training:
            continue
        splits.append(
            RollingOriginSplit(
                fold_id=f"fold:{canonical_digest({'date': date_key, 'tournament': tournament_id})}",
                train_indices=training,
                validation_indices=validation,
                validation_date=date_key,
                validation_tournament_id=tournament_id,
            )
        )
    return tuple(splits)


def _specialist_eligibility(
    rows: Iterable[CausalTrainingRow],
) -> dict[str, SpecialistEligibility]:
    grouped: dict[str, list[CausalTrainingRow]] = {}
    for row in rows:
        grouped.setdefault(row.specialist_key, []).append(row)
    return {
        key: SpecialistEligibility(
            len(values),
            len({item.competitor_id for item in values}),
            len({item.tournament_id for item in values}),
        )
        for key, values in sorted(grouped.items())
    }


def _fit_specialist_gate_values(
    examples: Sequence[GateExample],
    *,
    iterations: int = 800,
    learning_rate: float = 0.1,
) -> SpecialistGate:
    if not examples or len({item.fold_id for item in examples}) < 2:
        raise ValueError("gate training requires at least two grouped OOF folds")
    feature_names = GATE_FEATURE_NAMES
    coefficients = [0.0] * (len(feature_names) + 1)
    for _ in range(iterations):
        gradients = [0.0] * len(coefficients)
        for item in examples:
            transformed = canonical_gate_features(
                history_depth=item.history_depth,
                missing_fraction=item.missing_fraction,
            )
            values = (1.0, *(transformed[name] for name in GATE_FEATURE_NAMES))
            score = max(
                -40.0, min(40.0, sum(c * x for c, x in zip(coefficients, values)))
            )
            error = 1.0 / (1.0 + math.exp(-score)) - float(item.specialist_better)
            importance = max(1e-6, min(1.0, abs(item.pinball_advantage)))
            for index, value in enumerate(values):
                gradients[index] += error * value * importance
        scale = learning_rate / len(examples)
        coefficients = [
            max(-40.0, min(40.0, value - scale * gradient))
            for value, gradient in zip(coefficients, gradients)
        ]
    return SpecialistGate(
        canonical_decimal_string(coefficients[0]),
        tuple(
            sorted(
                (name, canonical_decimal_string(value))
                for name, value in zip(GATE_FEATURE_NAMES, coefficients[1:])
            )
        ),
    )


def canonical_gate_features(
    *, history_depth: int | float, missing_fraction: float
) -> dict[str, float]:
    numeric_depth = float(history_depth)
    numeric_missing = float(missing_fraction)
    if (
        not math.isfinite(numeric_depth)
        or numeric_depth < 0
        or not math.isfinite(numeric_missing)
        or not 0 <= numeric_missing <= 1
    ):
        raise ValueError("canonical ML gate inputs are outside their finite bounds")
    return {
        "log_history_depth": math.log1p(numeric_depth),
        "missing_fraction": numeric_missing,
    }


def _grouped_oof_component_prediction_values(
    rows: Sequence[CausalTrainingRow],
    *,
    model_factory: Callable[..., Any],
) -> tuple[OOFComponentPrediction, ...]:
    """Generate component forecasts from tournament/date grouped prior-only folds."""

    predictions: list[OOFComponentPrediction] = []
    for split in _grouped_rolling_origin_splits_values(rows):
        training = tuple(rows[index] for index in split.train_indices)
        universal, specialists, _eligibility = _train_catboost_hierarchy(
            training, model_factory=model_factory
        )
        for index in split.validation_indices:
            row = rows[index]
            feature_row = [[row.feature_dict[name] for name in FEATURE_NAMES]]
            universal_values = _prediction_values(universal.predict(feature_row))
            specialist = specialists.get(row.specialist_key)
            specialist_values = (
                None
                if specialist is None
                else _prediction_values(specialist.predict(feature_row))
            )
            missing_flags = tuple(
                int(row.feature_dict[name])
                for name in NUMERIC_FEATURES
                if name.endswith("_missing")
            )
            predictions.append(
                OOFComponentPrediction(
                    row.row_id,
                    split.fold_id,
                    universal_values,
                    specialist_values,
                    row.specialist_key,
                    int(row.feature_dict["history_depth"]),
                    sum(missing_flags) / max(1, len(missing_flags)),
                )
            )
    return tuple(predictions)


def _gate_examples_values(
    predictions: Sequence[OOFComponentPrediction], rows: Sequence[CausalTrainingRow]
) -> tuple[GateExample, ...]:
    actual_log_seconds = {item.row_id: float(item.target_log_seconds) for item in rows}
    examples: list[GateExample] = []
    for item in predictions:
        if item.specialist_log_quantiles is None:
            continue
        if item.row_id not in actual_log_seconds:
            raise ValueError(
                "OOF gate targets must exactly cover each specialist prediction"
            )
        actual = float(actual_log_seconds[item.row_id])
        universal_loss = mean_pinball_loss(actual, item.universal_log_quantiles)
        specialist_loss = mean_pinball_loss(actual, item.specialist_log_quantiles)
        advantage = universal_loss - specialist_loss
        examples.append(
            GateExample(
                pinball_advantage=advantage,
                history_depth=item.history_depth,
                missing_fraction=item.missing_fraction,
                specialist_better=advantage > 0,
                fold_id=item.fold_id,
            )
        )
    return tuple(examples)


def _fit_pit_calibrator_values(
    rows: Sequence[CausalTrainingRow],
    predictions: Sequence[OOFComponentPrediction],
    gate: SpecialistGate,
) -> PITCalibrator:
    """Derive PIT values only from sealed calibration-role OOF forecasts."""

    if not isinstance(gate, SpecialistGate):
        raise ValueError("PIT fitting requires one trusted calibration-role authority")
    by_id = {item.row_id: item for item in predictions}
    row_ids = {item.row_id for item in rows}
    if not by_id or len(by_id) != len(predictions) or not set(by_id) <= row_ids:
        raise ValueError(
            "PIT calibration OOF forecasts must uniquely match calibration rows"
        )
    pits: list[float] = []
    for row in rows:
        if row.row_id not in by_id:
            continue
        prediction = by_id[row.row_id]
        available = prediction.specialist_log_quantiles is not None
        weight = gate.weight(
            canonical_gate_features(
                history_depth=prediction.history_depth,
                missing_fraction=prediction.missing_fraction,
            ),
            specialist_available=available,
        )
        specialist = (
            prediction.specialist_log_quantiles or prediction.universal_log_quantiles
        )
        combined = combine_quantiles(
            prediction.universal_log_quantiles, specialist, weight
        )
        pits.append(_quantile_probability(float(row.target_log_seconds), combined))
    source_digest = canonical_digest(
        {
            "schema_version": "strathmark-v3-ml-pit-fit-source-v1",
            "rows_digest": _member_digest(
                "calibration_rows", [_row_value(item) for item in rows]
            ),
            "rows": [item.row_id for item in rows],
            "predictions": [
                {
                    "row_id": item.row_id,
                    "fold_id": item.fold_id,
                    "universal": list(item.universal_log_quantiles),
                    "specialist": (
                        None
                        if item.specialist_log_quantiles is None
                        else list(item.specialist_log_quantiles)
                    ),
                }
                for item in predictions
            ],
            "gate": gate.to_dict(),
        }
    )
    return PITCalibrator._fit_authorized_values(pits, source_digest=source_digest)


def _quantile_probability(actual: float, quantiles: Sequence[float]) -> float:
    levels = tuple(float(item) for item in QUANTILE_LEVELS)
    if actual <= quantiles[0]:
        return levels[0]
    if actual >= quantiles[-1]:
        return levels[-1]
    index = bisect_left(quantiles, actual)
    left_value, right_value = quantiles[index - 1], quantiles[index]
    ratio = (actual - left_value) / (right_value - left_value)
    return levels[index - 1] + ratio * (levels[index] - levels[index - 1])


def mean_pinball_loss(actual: float, log_quantiles: Sequence[float]) -> float:
    if not math.isfinite(actual) or len(log_quantiles) != len(QUANTILE_LEVELS):
        raise ValueError("pinball loss requires one finite target and seven quantiles")
    losses = []
    for probability, predicted in zip(
        (float(item) for item in QUANTILE_LEVELS), log_quantiles
    ):
        if not math.isfinite(predicted):
            raise ValueError("pinball quantiles must be finite")
        residual = actual - predicted
        losses.append(max(probability * residual, (probability - 1) * residual))
    return sum(losses) / len(losses)


def _evaluate_frozen_replay(
    rows: Sequence[CausalTrainingRow],
    bundle: Any,
    consequence_evaluator: MarkConsequenceEvaluator,
) -> FrozenMLReplayReport:
    """Run the frozen hierarchy and bind exact-field optimizer consequence receipts."""

    from strathmark.v3.factory.ml_artifacts import LoadedMLBundle

    if not rows:
        raise ValueError("frozen replay requires nonempty locked-audit rows")
    if not isinstance(bundle, LoadedMLBundle) or not callable(
        getattr(consequence_evaluator, "evaluate", None)
    ):
        raise ValueError(
            "frozen replay requires a verified ML bundle and consequence evaluator"
        )
    if (
        bundle.metadata["taxonomy_version"] != rows[0].taxonomy_version
        or bundle.metadata["conversion_version"] != rows[0].conversion_version
        or any(
            row.taxonomy_version != rows[0].taxonomy_version
            or row.conversion_version != rows[0].conversion_version
            for row in rows
        )
    ):
        raise ValueError(
            "frozen replay bundle taxonomy or conversion differs from audit rows"
        )
    distribution_by_id: dict[str, Any] = {}
    for row in rows:
        features = row.feature_dict
        normalized, _unseen = bundle.normalize_features(features)
        ordered = [[normalized[name] for name in bundle.feature_names]]
        universal = _prediction_values(bundle.universal_model.predict(ordered))
        specialist_model = bundle.specialist_models.get(row.specialist_key)
        eligibility = bundle.specialist_eligibility.get(row.specialist_key)
        available = specialist_model is not None and bool(
            eligibility is not None and eligibility.available
        )
        missing_flags = tuple(
            int(features[name])
            for name in NUMERIC_FEATURES
            if name.endswith("_missing")
        )
        weight = bundle.gate.weight(
            canonical_gate_features(
                history_depth=float(features["history_depth"]),
                missing_fraction=sum(missing_flags) / max(1, len(missing_flags)),
            ),
            specialist_available=available,
        )
        specialist = (
            _prediction_values(specialist_model.predict(ordered))
            if available
            else universal
        )
        combined = combine_quantiles(universal, specialist, weight)
        distribution_by_id[row.row_id] = build_positive_distribution(
            combined, bundle.calibrator
        )

    fields: dict[str, list[CausalTrainingRow]] = {}
    for row in rows:
        fields.setdefault(row.field_id, []).append(row)
    consequences: dict[str, MarkConsequenceOutcome] = {}
    receipt_digests: list[tuple[str, str]] = []
    for field_id, field_rows in sorted(fields.items()):
        ordered_rows = sorted(field_rows, key=lambda item: item.row_id)
        field_input = MarkConsequenceFieldInput.create(
            field_id=field_id,
            forecasts=tuple(
                (row.row_id, distribution_by_id[row.row_id]) for row in ordered_rows
            ),
            actual_raw_times_ms=tuple(
                (row.row_id, round(math.exp(float(row.target_log_seconds)) * 1000))
                for row in ordered_rows
            ),
        )
        expected_input_digest = field_input.input_digest
        expected_distribution_digest = field_input.field_distribution_digest
        expected_row_ids = tuple(row.row_id for row in ordered_rows)
        receipt = consequence_evaluator.evaluate(field_input)
        receipt = _revalidate_consequence_receipt(
            receipt,
            field_input,
            expected_input_digest=expected_input_digest,
            expected_distribution_digest=expected_distribution_digest,
            expected_row_ids=expected_row_ids,
        )
        receipt_digests.append((field_id, receipt.receipt_digest))
        consequences.update({item.row_id: item for item in receipt.outcomes})
    members: dict[str, list[CausalTrainingRow]] = {}
    for row in rows:
        features = row.feature_dict
        depth = int(features["history_depth"])
        band = (
            "0"
            if depth == 0
            else "1-3" if depth <= 3 else "4-9" if depth <= 9 else "10+"
        )
        size = int(features["size_mm"])
        size_floor = (size // 25) * 25
        missing = (
            "yes"
            if any(
                int(features[name])
                for name in NUMERIC_FEATURES
                if name.endswith("_missing")
            )
            else "no"
        )
        labels = (
            "all",
            f"history:{band}",
            f"missing:{missing}",
            f"context:{row.specialist_key}",
            "fallback:global",
            f"event:{features['event_family']}",
            f"size_band:{size_floor}-{size_floor + 24}",
            f"species:{features['species']}",
        )
        for label in labels:
            members.setdefault(label, []).append(row)
    slices = []
    for name, selected in sorted(members.items()):
        losses = [
            _distribution_pinball_loss(row, distribution_by_id[row.row_id])
            for row in selected
        ]
        predictive = [
            _predictive_replay_metrics(row, distribution_by_id[row.row_id])
            for row in selected
        ]
        outcome_values = [consequences[row.row_id] for row in selected]
        slices.append(
            MLReplaySlice(
                name,
                len(selected),
                _canonical_mean(losses),
                _canonical_mean(item[0] for item in predictive),
                _canonical_mean(item[1] for item in predictive),
                _canonical_mean(item[2] for item in predictive),
                _canonical_mean(item[3] for item in predictive),
                _calibration_error(
                    (item[4] for item in predictive),
                    (item[2] for item in predictive),
                ),
                _canonical_mean(
                    float(item.mark_error_seconds) for item in outcome_values
                ),
                _canonical_mean(
                    float(item.spread_error_seconds) for item in outcome_values
                ),
                _canonical_mean(
                    float(item.win_probability_distortion) for item in outcome_values
                ),
                _canonical_mean(
                    float(item.class_context_bias_seconds) for item in outcome_values
                ),
                _canonical_mean(
                    float(item.gap_error_seconds) for item in outcome_values
                ),
                _canonical_mean(
                    float(item.breakout_exposure) for item in outcome_values
                ),
                _canonical_mean(
                    float(item.optimizer_repair_seconds) for item in outcome_values
                ),
            )
        )
    return FrozenMLReplayReport.create(
        slices=tuple(slices),
        grouped_field_count=len(fields),
        consequence_receipt_digests=tuple(receipt_digests),
    )


def _predictive_replay_metrics(
    row: CausalTrainingRow, distribution: Any
) -> tuple[float, ...]:
    actual_ms = math.exp(float(row.target_log_seconds)) * 1000
    points = {item.probability: item.time_ms for item in distribution.quantiles}
    lower, median_ms, upper = points["0.05"], points["0.5"], points["0.95"]
    tail_error = max(float(lower) - actual_ms, actual_ms - float(upper), 0.0) / 1000
    coverage = float(lower <= actual_ms <= upper)
    sharpness = (upper - lower) / 1000
    pit = _distribution_probability(distribution, actual_ms)
    return (
        abs(median_ms - actual_ms) / 1000,
        tail_error,
        coverage,
        sharpness,
        pit,
    )


def _distribution_pinball_loss(row: CausalTrainingRow, distribution: Any) -> float:
    actual_seconds = math.exp(float(row.target_log_seconds))
    levels = set(QUANTILE_LEVELS)
    losses = []
    for item in distribution.quantiles:
        if item.probability not in levels:
            continue
        probability = float(item.probability)
        residual = actual_seconds - item.time_ms / 1000
        losses.append(max(probability * residual, (probability - 1) * residual))
    return sum(losses) / len(losses)


def _calibration_error(pits: Iterable[float], coverages: Iterable[float]) -> str:
    ordered = tuple(sorted(float(item) for item in pits))
    coverage = tuple(float(item) for item in coverages)
    if not ordered or len(ordered) != len(coverage):
        raise ValueError(
            "calibration requires matched nonempty PIT and coverage observations"
        )
    count = len(ordered)
    pit_ks = max(
        max(abs((index + 1) / count - value), abs(value - index / count))
        for index, value in enumerate(ordered)
    )
    coverage_deviation = abs(sum(coverage) / count - 0.9)
    return canonical_decimal_string(max(pit_ks, coverage_deviation))


def _distribution_probability(distribution: Any, actual_ms: float) -> float:
    points = [
        (float(item.time_ms), float(item.probability))
        for item in distribution.quantiles
    ]
    if actual_ms <= points[0][0]:
        return points[0][1]
    if actual_ms >= points[-1][0]:
        return points[-1][1]
    index = bisect_left([item[0] for item in points], actual_ms)
    left_time, left_probability = points[index - 1]
    right_time, right_probability = points[index]
    ratio = (actual_ms - left_time) / (right_time - left_time)
    return left_probability + ratio * (right_probability - left_probability)


def _canonical_mean(values: Iterable[float]) -> str:
    material = tuple(float(item) for item in values)
    if not material or any(not math.isfinite(item) for item in material):
        raise ValueError("replay metric inputs must be finite and nonempty")
    return canonical_decimal_string(sum(material) / len(material))


def _train_catboost_hierarchy(
    rows: Sequence[CausalTrainingRow],
    *,
    model_factory: Callable[..., Any] | None = None,
    iterations: int = 400,
    depth: int = 6,
    learning_rate: float = 0.03,
    seed: int = 20260823,
) -> tuple[Any, dict[str, Any], dict[str, SpecialistEligibility]]:
    if not rows:
        raise ValueError("ML training requires at least one admitted causal row")
    factory = model_factory or _catboost_factory()
    settings = {
        "loss_function": "MultiQuantile:alpha=0.05,0.1,0.25,0.5,0.75,0.9,0.95",
        "iterations": int(iterations),
        "depth": int(depth),
        "learning_rate": float(learning_rate),
        "random_seed": int(seed),
        "verbose": False,
        "allow_writing_files": False,
        "thread_count": 1,
    }
    universal = factory(**settings)
    _fit_model(universal, rows)
    eligibility = _specialist_eligibility(rows)
    specialists: dict[str, Any] = {}
    for key, state in eligibility.items():
        if not state.available:
            continue
        selected = tuple(item for item in rows if item.specialist_key == key)
        model = factory(**settings)
        _fit_model(model, selected)
        specialists[key] = model
    return universal, specialists, eligibility


def context_key(context: TargetContext) -> str:
    return f"{context.event_code}|{context.size_mm}|{context.material_code}"


def _fit_model(model: Any, rows: Sequence[CausalTrainingRow]) -> None:
    import pandas as pd

    features = pd.DataFrame(
        [{name: item.feature_dict[name] for name in FEATURE_NAMES} for item in rows],
        columns=FEATURE_NAMES,
    )
    targets = [float(item.target_log_seconds) for item in rows]
    model.fit(features, targets, cat_features=list(CATEGORICAL_FEATURES))


def _prediction_values(value: Any) -> tuple[float, ...]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (list, tuple)) and len(value) == 1:
        value = value[0]
    if not isinstance(value, (list, tuple)) or len(value) != len(QUANTILE_LEVELS):
        raise ValueError("CatBoost OOF prediction must contain seven quantiles")
    result = tuple(float(item) for item in value)
    if any(not math.isfinite(item) for item in result):
        raise ValueError("CatBoost OOF prediction must contain finite quantiles")
    return result


def _catboost_factory() -> Callable[..., Any]:
    from catboost import CatBoostRegressor

    return CatBoostRegressor


def _verify_packet(packet: object) -> EvidencePacket:
    if not isinstance(packet, EvidencePacket):
        raise ValueError("ML inputs must be EvidencePacket values")
    try:
        if packet.recompute_digest() != packet.content_digest:
            raise ValueError
    except Exception as exc:
        raise ValueError("ML input is not a sealed evidence packet") from exc
    return packet


def _features(
    context: TargetContext,
    history: Sequence[ResultObservation],
    *,
    eligible_sequence: int,
) -> dict[str, object]:
    admitted = [
        (item, admit_raw_completion(item.result))
        for item in history
        if admit_raw_completion(item.result) is not None
    ]
    logs = [
        math.log(value.raw_time_ms / 1000.0)
        for _, value in admitted
        if value is not None
    ]
    exact_logs = [
        math.log(value.raw_time_ms / 1000.0)
        for item, value in admitted
        if value is not None and context_key(item.context) == context_key(context)
    ]
    center = median(logs) if logs else 0.0
    spread = median(abs(item - center) for item in logs) if logs else 0.0
    recent = median(logs[-3:]) if logs else 0.0
    if len(admitted) >= 2:
        first_sequence = admitted[0][0].observation_sequence
        last_sequence = admitted[-1][0].observation_sequence
        trend = (logs[-1] - logs[0]) / max(1, last_sequence - first_sequence)
    else:
        trend = 0.0
    last_sequence = (
        admitted[-1][0].observation_sequence if admitted else eligible_sequence
    )
    sequence_recency = max(0, eligible_sequence - last_sequence)
    context_distance = (
        sum(_context_distance(item.context, context) for item, _value in admitted)
        / len(admitted)
        if admitted
        else 0.0
    )
    density = next(
        (
            item.value
            for item in context.properties
            if item.code == "density" and item.unit == "kg_m3"
        ),
        None,
    )
    return {
        "event_family": context.event_code,
        "species": context.material_code,
        "size_mm": context.size_mm,
        "density": 0.0 if density is None else float(density),
        "density_missing": int(density is None),
        "history_depth": len(logs),
        "exact_history_depth": len(exact_logs),
        "history_log_median": center,
        "history_log_spread": spread,
        "history_missing": int(not logs),
        "sequence_recency": sequence_recency,
        "history_log_trend": trend,
        "context_distance": context_distance,
        "eligible_tournament_sequence": eligible_sequence,
        "current_form_log_seconds": recent,
    }


def _context_distance(source: TargetContext, target: TargetContext) -> float:
    size_distance = abs(math.log(source.size_mm / target.size_mm))
    event_distance = float(source.event_code != target.event_code)
    material_distance = float(source.material_code != target.material_code)
    return size_distance + event_distance + material_distance


__all__ = [
    "AuthorizedGateExamples",
    "AuthorizedMLPackets",
    "AuthorizedMLRows",
    "AuthorizedOOFPredictions",
    "CATEGORICAL_FEATURES",
    "FEATURE_NAMES",
    "GATE_FEATURE_NAMES",
    "GateExample",
    "FrozenMLReplayReport",
    "MLAuthorityEnvironment",
    "MLDataRole",
    "MLRoleAssignment",
    "MLRoleManifest",
    "MLReplaySlice",
    "MarkConsequenceEvaluator",
    "MarkConsequenceFieldInput",
    "MarkConsequenceOutcome",
    "MarkConsequenceReceipt",
    "NUMERIC_FEATURES",
    "OOFComponentPrediction",
    "QUANTILE_LEVELS",
    "CausalTrainingRow",
    "RollingOriginSplit",
    "SpecialistEligibility",
    "TrustedMLAuditAuthority",
    "TrustedMLRoleAuthority",
    "build_inference_features",
    "canonical_gate_features",
    "context_key",
    "mean_pinball_loss",
]
