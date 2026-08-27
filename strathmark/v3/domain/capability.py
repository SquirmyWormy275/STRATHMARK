"""Pure BOCPD capability state and shared post-assessor adjustment.

Every admitted value and its original Student-t likelihood remain auditable.
Only the signed standardized innovation used for state update is bounded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from strathmark.v3.contracts.canonical import canonical_decimal_string, canonical_digest
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.evidence import _require_digest, require_utc_milliseconds
from strathmark.v3.contracts.forecasts import AssessorKind, PositiveTimeDistribution, QuantilePoint
from strathmark.v3.contracts.identifiers import StableIdentifier, require_identifier
from strathmark.v3.domain.evidence import AdmissionReason, EvidenceSource

CAPABILITY_OPERATOR_VERSION = "capability-bocpd:v2"
CAPABILITY_STATE_SCHEMA_VERSION = "strathmark-v3-capability-state-v2"
CAPABILITY_EVIDENCE_SCHEMA_VERSION = "strathmark-v3-capability-evidence-v2"
BOCPD_HAZARD = "0.05"
BOCPD_RUN_LENGTH_CAP = 64
ZERO_DIGEST = "0" * 64
_LOG_HAZARD = math.log(1 / 20)
_LOG_GROWTH = math.log(19 / 20)


def _int(value: object, label: str, *, positive: bool = True) -> int:
    boundary = 0 if positive else -1
    if isinstance(value, bool) or not isinstance(value, int) or value <= boundary:
        qualifier = "positive" if positive else "non-negative"
        raise ContractError(f"{label} must be a {qualifier} integer")
    return value


def _dec(value: object, label: str, low: str, high: str) -> str:
    try:
        result = canonical_decimal_string(value)  # type: ignore[arg-type]
        number = Decimal(result)
    except Exception as exc:
        raise ContractError(f"{label} must be a canonical decimal") from exc
    if not Decimal(low) <= number <= Decimal(high):
        raise ContractError(f"{label} is outside its closed range")
    return result


def _fs(value: float) -> str:
    if not math.isfinite(value):
        raise ContractError("capability calculation produced a non-finite number")
    return canonical_decimal_string(Decimal(format(0.0 if abs(value) < 5e-16 else value, ".15g")))


def _f(value: str) -> float:
    return float(Decimal(value))


@dataclass(frozen=True, slots=True)
class CapabilityPrior:
    population_log_median: str
    calibrated_beta: str
    schema_version: str = "strathmark-v3-capability-prior-v1"

    def __post_init__(self) -> None:
        _dec(self.population_log_median, "population log median", "-20", "20")
        _dec(self.calibrated_beta, "calibrated beta", "0.000000000001", "100")
        if self.schema_version != "strathmark-v3-capability-prior-v1":
            raise ContractError("unsupported capability prior schema")

    @classmethod
    def from_median_seconds(cls, seconds: str, *, calibrated_beta: str) -> CapabilityPrior:
        value = Decimal(_dec(seconds, "population median seconds", "0.001", "1000000"))
        return cls(canonical_decimal_string(value.ln()), calibrated_beta)

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "population_log_median": self.population_log_median,
            "calibrated_beta": self.calibrated_beta,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityPrior:
        if set(value) != {"schema_version", "population_log_median", "calibrated_beta"}:
            raise ContractError("capability prior fields are not closed")
        return cls(
            value["population_log_median"], value["calibrated_beta"], value["schema_version"]
        )


@dataclass(frozen=True, slots=True)
class HistoricalImportBinding:
    import_id: str
    row_digest: str
    source_cutoff: str
    cutover_manifest_digest: str
    provenance_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.import_id, str) or not self.import_id.startswith("v2import:"):
            raise ContractError("historical binding requires an import identity")
        _require_digest(self.row_digest, "historical row digest")
        require_utc_milliseconds(self.source_cutoff)
        _require_digest(self.cutover_manifest_digest, "historical cutover manifest digest")
        _require_digest(self.provenance_digest, "historical provenance digest")

    def to_dict(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> HistoricalImportBinding:
        if set(value) != set(cls.__dataclass_fields__):
            raise ContractError("historical binding fields are not closed")
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    result_key: StableIdentifier
    result_revision: int
    supersedes_revision: int | None
    competitor_id: StableIdentifier
    context_digest: str
    source_global_sequence: int
    observed_at_utc: str
    raw_time_ms: int | None
    source: EvidenceSource
    numeric_eligible: bool
    admission_reason: AdmissionReason
    observation_digest: str
    authority_digest: str
    prior: CapabilityPrior
    evidence_log_variance: str
    conversion_log_variance: str
    effective_weight: str
    historical_binding: HistoricalImportBinding | None

    def __post_init__(self) -> None:
        require_identifier(self.result_key, expected_namespace="result")
        require_identifier(self.competitor_id, expected_namespace="competitor")
        _int(self.result_revision, "result revision")
        if (self.result_revision == 1 and self.supersedes_revision is not None) or (
            self.result_revision > 1 and self.supersedes_revision != self.result_revision - 1
        ):
            raise ContractError("capability revision must supersede its exact predecessor")
        _require_digest(self.context_digest, "capability context digest")
        _int(self.source_global_sequence, "source sequence")
        require_utc_milliseconds(self.observed_at_utc)
        if not isinstance(self.source, EvidenceSource) or not isinstance(
            self.admission_reason, AdmissionReason
        ):
            raise ContractError("capability evidence uses unknown closed vocabulary")
        if not isinstance(self.numeric_eligible, bool):
            raise ContractError("capability numeric eligibility must be explicit")
        _require_digest(self.observation_digest, "observation digest")
        _require_digest(self.authority_digest, "authority digest")
        if not isinstance(self.prior, CapabilityPrior):
            raise ContractError("capability evidence requires its population prior")
        _dec(self.evidence_log_variance, "evidence variance", "0", "100")
        _dec(self.conversion_log_variance, "conversion variance", "0", "100")
        _dec(self.effective_weight, "effective weight", "0.000001", "1")
        reason = (
            AdmissionReason.ELIGIBLE_COMPLETION
            if self.source is EvidenceSource.LIVE_ISSUED_RACE
            else AdmissionReason.HISTORICAL_CUTOVER
        )
        if self.numeric_eligible:
            if self.raw_time_ms is None:
                raise ContractError("capability numeric eligibility requires raw time")
            _int(self.raw_time_ms, "raw time")
            if self.admission_reason is not reason:
                raise ContractError("capability numeric eligibility contradicts admission")
        elif (
            self.raw_time_ms is not None
            or self.admission_reason is not AdmissionReason.STATUS_INELIGIBLE
        ):
            raise ContractError("ineligible capability evidence cannot carry numeric time")
        if self.source is EvidenceSource.HISTORICAL_IMPORT:
            if not isinstance(self.historical_binding, HistoricalImportBinding):
                raise ContractError("historical evidence requires exact row membership")
        elif self.historical_binding is not None:
            raise ContractError("live evidence cannot carry historical membership")

    @property
    def observation_variance(self) -> float:
        return _f(self.evidence_log_variance) + _f(self.conversion_log_variance)

    def semantic_value(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-capability-semantic-v2",
            "result_key": str(self.result_key),
            "competitor_id": str(self.competitor_id),
            "context_digest": self.context_digest,
            "observed_at_utc": self.observed_at_utc,
            "raw_time_ms": self.raw_time_ms,
            "source": self.source.value,
            "numeric_eligible": self.numeric_eligible,
            "admission_reason": self.admission_reason.value,
            "observation_digest": self.observation_digest,
            "authority_digest": self.authority_digest,
            "prior": self.prior.to_dict(),
            "evidence_log_variance": self.evidence_log_variance,
            "conversion_log_variance": self.conversion_log_variance,
            "effective_weight": self.effective_weight,
            "historical_binding": None
            if self.historical_binding is None
            else self.historical_binding.to_dict(),
        }

    @property
    def semantic_digest(self) -> str:
        return canonical_digest(self.semantic_value())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": CAPABILITY_EVIDENCE_SCHEMA_VERSION,
            "result_key": str(self.result_key),
            "result_revision": self.result_revision,
            "supersedes_revision": self.supersedes_revision,
            "competitor_id": str(self.competitor_id),
            "context_digest": self.context_digest,
            "source_global_sequence": self.source_global_sequence,
            "observed_at_utc": self.observed_at_utc,
            "raw_time_ms": self.raw_time_ms,
            "source": self.source.value,
            "numeric_eligible": self.numeric_eligible,
            "admission_reason": self.admission_reason.value,
            "observation_digest": self.observation_digest,
            "authority_digest": self.authority_digest,
            "prior": self.prior.to_dict(),
            "evidence_log_variance": self.evidence_log_variance,
            "conversion_log_variance": self.conversion_log_variance,
            "effective_weight": self.effective_weight,
            "historical_binding": None
            if self.historical_binding is None
            else self.historical_binding.to_dict(),
            "semantic_digest": self.semantic_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityEvidence:
        expected = set(cls.__dataclass_fields__) | {"schema_version", "semantic_digest"}
        if (
            set(value) != expected
            or value.get("schema_version") != CAPABILITY_EVIDENCE_SCHEMA_VERSION
        ):
            raise ContractError("capability evidence fields are not closed")
        prior, binding = value["prior"], value["historical_binding"]
        if not isinstance(prior, Mapping) or (
            binding is not None and not isinstance(binding, Mapping)
        ):
            raise ContractError("capability evidence nested provenance is invalid")
        try:
            result = cls(
                require_identifier(value["result_key"], expected_namespace="result"),
                value["result_revision"],
                value["supersedes_revision"],
                require_identifier(value["competitor_id"], expected_namespace="competitor"),
                value["context_digest"],
                value["source_global_sequence"],
                value["observed_at_utc"],
                value["raw_time_ms"],
                EvidenceSource(value["source"]),
                value["numeric_eligible"],
                AdmissionReason(value["admission_reason"]),
                value["observation_digest"],
                value["authority_digest"],
                CapabilityPrior.from_dict(prior),
                value["evidence_log_variance"],
                value["conversion_log_variance"],
                value["effective_weight"],
                None if binding is None else HistoricalImportBinding.from_dict(binding),
            )
        except (TypeError, ValueError) as exc:
            raise ContractError("capability evidence vocabulary is invalid") from exc
        if value["semantic_digest"] != result.semantic_digest:
            raise ContractError("capability semantic digest mismatch")
        return result


@dataclass(frozen=True, slots=True)
class RunLengthHypothesis:
    run_length: int
    probability: str
    mean_log_seconds: str
    kappa: str
    alpha: str
    beta: str

    def __post_init__(self) -> None:
        _int(self.run_length, "run length", positive=False)
        if self.run_length > BOCPD_RUN_LENGTH_CAP:
            raise ContractError("run length exceeds deterministic cap")
        _dec(self.probability, "posterior probability", "0", "1")
        _dec(self.mean_log_seconds, "posterior mean", "-20", "20")
        for value, label in ((self.kappa, "kappa"), (self.alpha, "alpha"), (self.beta, "beta")):
            _dec(value, label, "0.000000000001", "1000000")

    def to_dict(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RunLengthHypothesis:
        if set(value) != set(cls.__dataclass_fields__):
            raise ContractError("run hypothesis fields are not closed")
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class FastCapabilityRegime:
    mean_log_seconds: str
    kappa: str
    alpha: str
    beta: str
    n_fast: str
    n_supported_slower: str
    last_fast_at_utc: str
    lineage: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        RunLengthHypothesis(0, "1", self.mean_log_seconds, self.kappa, self.alpha, self.beta)
        _dec(self.n_fast, "fast count", "0.000001", "1000000")
        _dec(self.n_supported_slower, "supported slower count", "0", "1000000")
        require_utc_milliseconds(self.last_fast_at_utc)
        if not isinstance(self.lineage, tuple) or not self.lineage:
            raise ContractError("fast regime requires immutable lineage")
        for key, digest in self.lineage:
            require_identifier(key, expected_namespace="result")
            _require_digest(digest, "fast regime lineage digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "mean_log_seconds": self.mean_log_seconds,
            "kappa": self.kappa,
            "alpha": self.alpha,
            "beta": self.beta,
            "n_fast": self.n_fast,
            "n_supported_slower": self.n_supported_slower,
            "last_fast_at_utc": self.last_fast_at_utc,
            "lineage": [list(item) for item in self.lineage],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FastCapabilityRegime:
        if set(value) != set(cls.__dataclass_fields__) or not isinstance(value["lineage"], list):
            raise ContractError("fast regime fields are not closed")
        return cls(
            value["mean_log_seconds"],
            value["kappa"],
            value["alpha"],
            value["beta"],
            value["n_fast"],
            value["n_supported_slower"],
            value["last_fast_at_utc"],
            tuple((item[0], item[1]) for item in value["lineage"]),
        )


@dataclass(frozen=True, slots=True)
class NumericAnomalyPattern:
    absolute_standardized_residual: str
    direction_run: int
    alternation_count: int
    alternation_ratio: str

    def __post_init__(self) -> None:
        _dec(self.absolute_standardized_residual, "standardized residual", "0", "1000000")
        _int(self.direction_run, "direction run")
        _int(self.alternation_count, "alternation count", positive=False)
        _dec(self.alternation_ratio, "alternation ratio", "0", "1")

    def to_dict(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> NumericAnomalyPattern:
        if set(value) != set(cls.__dataclass_fields__):
            raise ContractError("numeric anomaly fields are not closed")
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class CapabilityTransition:
    evidence_digest: str
    before_state_digest: str
    after_current_form_digest: str
    after_demonstrated_capability_digest: str
    change_point_probability: str
    influence: str
    source_authority_digest: str
    evidence_log_likelihood: str
    original_standardized_innovation: str
    state_update_innovation: str
    faster_candidate_probability: str
    faster_candidate_opened: bool
    three_sd_triggered: bool
    persistence_weight: str
    supported_slower_probability: str
    posterior_hypothesis_count: int
    anomaly: NumericAnomalyPattern

    def __post_init__(self) -> None:
        for digest in (
            self.evidence_digest,
            self.before_state_digest,
            self.after_current_form_digest,
            self.after_demonstrated_capability_digest,
            self.source_authority_digest,
        ):
            _require_digest(digest, "capability transition digest")
        for value, label, low, high in (
            (self.change_point_probability, "change probability", "0", "1"),
            (self.influence, "influence", "-1", "1"),
            (self.evidence_log_likelihood, "log likelihood", "-1000000000", "1000000000"),
            (self.original_standardized_innovation, "original innovation", "-1000000", "1000000"),
            (self.state_update_innovation, "update innovation", "-4", "4"),
            (self.faster_candidate_probability, "candidate probability", "0", "1"),
            (self.persistence_weight, "persistence", "0", "0.65"),
            (self.supported_slower_probability, "slower probability", "0", "1"),
        ):
            _dec(value, label, low, high)
        if not isinstance(self.faster_candidate_opened, bool) or not isinstance(
            self.three_sd_triggered, bool
        ):
            raise ContractError("candidate threshold decisions must be explicit")
        _int(self.posterior_hypothesis_count, "posterior hypothesis count")
        if self.posterior_hypothesis_count > 65 or not isinstance(
            self.anomaly, NumericAnomalyPattern
        ):
            raise ContractError("capability transition posterior is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            **{
                field: getattr(self, field)
                for field in self.__dataclass_fields__
                if field != "anomaly"
            },
            "anomaly": self.anomaly.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityTransition:
        if set(value) != set(cls.__dataclass_fields__) or not isinstance(value["anomaly"], Mapping):
            raise ContractError("transition fields are not closed")
        return cls(**{**value, "anomaly": NumericAnomalyPattern.from_dict(value["anomaly"])})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class CapabilityState:
    competitor_id: StableIdentifier
    context_digest: str
    state_revision: int
    current_form: PositiveTimeDistribution
    demonstrated_capability: PositiveTimeDistribution
    observation_count: int
    last_observed_at_utc: str
    last_direction: int
    direction_run: int
    alternation_count: int
    lineage: tuple[tuple[str, str], ...]
    run_length_hypotheses: tuple[RunLengthHypothesis, ...]
    fast_regime: FastCapabilityRegime
    persistence_weight: str
    last_transition: CapabilityTransition
    state_digest: str
    schema_version: str = CAPABILITY_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        _require_digest(self.context_digest, "state context digest")
        _int(self.state_revision, "state revision")
        if self.state_revision != self.observation_count:
            raise ContractError("state revision must equal observation count")
        if not isinstance(self.current_form, PositiveTimeDistribution) or not isinstance(
            self.demonstrated_capability, PositiveTimeDistribution
        ):
            raise ContractError("state requires positive distributions")
        require_utc_milliseconds(self.last_observed_at_utc)
        if self.last_direction not in {-1, 0, 1}:
            raise ContractError("state direction must be closed")
        _int(self.direction_run, "direction run")
        _int(self.alternation_count, "alternation count", positive=False)
        if not isinstance(self.lineage, tuple) or len(self.lineage) != self.observation_count:
            raise ContractError("state lineage must cover active observations")
        for key, digest in self.lineage:
            require_identifier(key, expected_namespace="result")
            _require_digest(digest, "state lineage digest")
        hypotheses = self.run_length_hypotheses
        if (
            not isinstance(hypotheses, tuple)
            or not hypotheses
            or any(not isinstance(item, RunLengthHypothesis) for item in hypotheses)
        ):
            raise ContractError("state requires immutable BOCPD hypotheses")
        lengths = tuple(item.run_length for item in hypotheses)
        if (
            lengths != tuple(sorted(set(lengths)))
            or lengths[0] != 0
            or abs(sum(_f(item.probability) for item in hypotheses) - 1) > 1e-10
        ):
            raise ContractError("BOCPD posterior is not normalized and capped")
        if not isinstance(self.fast_regime, FastCapabilityRegime) or not isinstance(
            self.last_transition, CapabilityTransition
        ):
            raise ContractError("state requires sealed regime and transition")
        _dec(self.persistence_weight, "state persistence", "0", "0.65")
        _require_digest(self.state_digest, "state digest")
        if (
            self.schema_version != CAPABILITY_STATE_SCHEMA_VERSION
            or self.state_digest != canonical_digest(self.content_value())
        ):
            raise ContractError("capability state digest or schema differs")

    def content_value(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operator_version": CAPABILITY_OPERATOR_VERSION,
            "competitor_id": str(self.competitor_id),
            "context_digest": self.context_digest,
            "state_revision": self.state_revision,
            "current_form": self.current_form.to_dict(),
            "demonstrated_capability": self.demonstrated_capability.to_dict(),
            "observation_count": self.observation_count,
            "last_observed_at_utc": self.last_observed_at_utc,
            "last_direction": self.last_direction,
            "direction_run": self.direction_run,
            "alternation_count": self.alternation_count,
            "lineage": [list(item) for item in self.lineage],
            "run_length_hypotheses": [item.to_dict() for item in self.run_length_hypotheses],
            "fast_regime": self.fast_regime.to_dict(),
            "persistence_weight": self.persistence_weight,
            "last_transition": self.last_transition.to_dict(),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.content_value(), "state_digest": self.state_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityState:
        expected = set(cls.__dataclass_fields__) | {"operator_version"}
        if set(value) != expected or value.get("operator_version") != CAPABILITY_OPERATOR_VERSION:
            raise ContractError("state fields or operator version differ")
        lineage, hypotheses = value["lineage"], value["run_length_hypotheses"]
        if not isinstance(lineage, list) or not isinstance(hypotheses, list):
            raise ContractError("state arrays are invalid")
        for key in ("current_form", "demonstrated_capability", "fast_regime", "last_transition"):
            if not isinstance(value[key], Mapping):
                raise ContractError("state nested values are invalid")
        return cls(
            require_identifier(value["competitor_id"], expected_namespace="competitor"),
            value["context_digest"],
            value["state_revision"],
            PositiveTimeDistribution.from_dict(value["current_form"]),
            PositiveTimeDistribution.from_dict(value["demonstrated_capability"]),
            value["observation_count"],
            value["last_observed_at_utc"],
            value["last_direction"],
            value["direction_run"],
            value["alternation_count"],
            tuple((item[0], item[1]) for item in lineage),
            tuple(RunLengthHypothesis.from_dict(item) for item in hypotheses),
            FastCapabilityRegime.from_dict(value["fast_regime"]),
            value["persistence_weight"],
            CapabilityTransition.from_dict(value["last_transition"]),
            value["state_digest"],
            value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class _Nig:
    mean: float
    kappa: float
    alpha: float
    beta: float


def _nig(value: RunLengthHypothesis | FastCapabilityRegime) -> _Nig:
    return _Nig(_f(value.mean_log_seconds), _f(value.kappa), _f(value.alpha), _f(value.beta))


def _prior(value: CapabilityPrior) -> _Nig:
    return _Nig(_f(value.population_log_median), 1.0, 3.0, _f(value.calibrated_beta))


def _predictive(state: _Nig, variance: float = 0) -> tuple[float, float, float]:
    df = 2 * state.alpha
    scale = math.sqrt(
        max(1e-15, state.beta * (state.kappa + 1) / (state.alpha * state.kappa) + variance)
    )
    return df, state.mean, scale


def _log_pdf(value: float, state: _Nig, variance: float) -> float:
    df, mean, scale = _predictive(state, variance)
    z = (value - mean) / scale
    return (
        math.lgamma((df + 1) / 2)
        - math.lgamma(df / 2)
        - 0.5 * math.log(df * math.pi)
        - math.log(scale)
        - (df + 1) / 2 * math.log1p(z * z / df)
    )


def _beta_fraction(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > 1e-300 else 1e-300)
    result = d
    for index in range(1, 201):
        twice = 2 * index
        for coefficient in (
            index * (b - index) * x / ((qam + twice) * (a + twice)),
            -(a + index) * (qab + index) * x / ((a + twice) * (qap + twice)),
        ):
            d = 1 + coefficient * d
            d = 1 / (d if abs(d) > 1e-300 else 1e-300)
            c = 1 + coefficient / c
            c = c if abs(c) > 1e-300 else 1e-300
            delta = d * c
            result *= delta
        if abs(delta - 1) < 3e-14:
            break
    return result


def _ibeta(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    return (
        front * _beta_fraction(a, b, x) / a
        if x < (a + 1) / (a + b + 2)
        else 1 - front * _beta_fraction(b, a, 1 - x) / b
    )


def _t_cdf(value: float, df: float, mean: float, scale: float) -> float:
    t = (value - mean) / scale
    tail = 0.5 * _ibeta(df / 2, 0.5, df / (df + t * t))
    return tail if t < 0 else 1 - tail


def _update(
    state: _Nig, original: float, variance: float, weight: float
) -> tuple[_Nig, float, float]:
    _df, mean, scale = _predictive(state, variance)
    innovation = (original - mean) / scale
    bounded = max(-4.0, min(4.0, innovation))
    value = mean + bounded * scale
    kappa = state.kappa + weight
    return (
        _Nig(
            (state.kappa * mean + weight * value) / kappa,
            kappa,
            state.alpha + weight / 2,
            max(
                1e-15,
                state.beta
                + 0.5 * state.kappa * weight * (value - mean) ** 2 / kappa
                + 0.5 * weight * variance,
            ),
        ),
        innovation,
        bounded,
    )


def _hyp(run: int, probability: float, state: _Nig) -> RunLengthHypothesis:
    return RunLengthHypothesis(
        run, _fs(probability), _fs(state.mean), _fs(state.kappa), _fs(state.alpha), _fs(state.beta)
    )


def _lse(values: list[float]) -> float:
    maximum = max(values)
    return maximum + math.log(sum(math.exp(item - maximum) for item in values))


def _bocpd(
    previous: tuple[RunLengthHypothesis, ...] | None, evidence: CapabilityEvidence
) -> tuple[tuple[RunLengthHypothesis, ...], float, float, float, _Nig]:
    assert evidence.raw_time_ms is not None
    value, variance, weight = (
        math.log(evidence.raw_time_ms / 1000),
        evidence.observation_variance,
        _f(evidence.effective_weight),
    )
    population = _prior(evidence.prior)
    cp_likelihood = _log_pdf(value, population, variance)
    cp_state, innovation, _bounded = _update(population, value, variance, weight)
    material = previous or (_hyp(0, 1, population),)
    options: dict[int, list[tuple[float, _Nig]]] = {0: [(_LOG_HAZARD + cp_likelihood, cp_state)]}
    likelihoods = []
    for item in material:
        state = _nig(item)
        likelihood = _log_pdf(value, state, variance)
        log_probability = math.log(_f(item.probability))
        likelihoods.append(log_probability + likelihood)
        updated, _one, _two = _update(state, value, variance, weight)
        options.setdefault(min(64, item.run_length + 1), []).append(
            (log_probability + _LOG_GROWTH + likelihood, updated)
        )
    collapsed = [
        (run, _lse([entry[0] for entry in entries]), max(entries, key=lambda entry: entry[0])[1])
        for run, entries in sorted(options.items())
    ]
    normalizer = _lse([entry[1] for entry in collapsed])
    probabilities = [math.exp(entry[1] - normalizer) for entry in collapsed]
    probabilities[-1] += 1 - sum(probabilities)
    return (
        tuple(
            _hyp(entry[0], probability, entry[2])
            for entry, probability in zip(collapsed, probabilities)
        ),
        probabilities[0],
        _lse(likelihoods),
        innovation,
        cp_state,
    )


def _mix_cdf(value: float, hypotheses: tuple[RunLengthHypothesis, ...]) -> float:
    return sum(
        _f(item.probability) * _t_cdf(value, *_predictive(_nig(item))) for item in hypotheses
    )


def _quantile(probability: float, hypotheses: tuple[RunLengthHypothesis, ...]) -> float:
    states = [_nig(item) for item in hypotheses]
    low = min(item.mean - 16 * _predictive(item)[2] for item in states)
    high = max(item.mean + 16 * _predictive(item)[2] for item in states)
    for _ in range(96):
        middle = (low + high) / 2
        if _mix_cdf(middle, hypotheses) < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _distribution(hypotheses: tuple[RunLengthHypothesis, ...]) -> PositiveTimeDistribution:
    return PositiveTimeDistribution(
        tuple(
            QuantilePoint(
                probability,
                max(1, round(math.exp(_quantile(float(probability), hypotheses)) * 1000)),
            )
            for probability in ("0.1", "0.5", "0.9")
        )
    )


def _single_distribution(state: _Nig) -> PositiveTimeDistribution:
    return _distribution((_hyp(0, 1, state),))


def _mu_cdf(threshold: float, state: _Nig) -> float:
    return _t_cdf(
        threshold,
        2 * state.alpha,
        state.mean,
        math.sqrt(max(1e-15, state.beta / (state.alpha * state.kappa))),
    )


def _regime(
    state: _Nig, fast: float, slower: float, at: str, lineage: tuple[tuple[str, str], ...]
) -> FastCapabilityRegime:
    return FastCapabilityRegime(
        _fs(state.mean),
        _fs(state.kappa),
        _fs(state.alpha),
        _fs(state.beta),
        _fs(fast),
        _fs(slower),
        at,
        lineage,
    )


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _persistence(regime: FastCapabilityRegime, now: str) -> float:
    age = max(0.0, (_utc(now) - _utc(regime.last_fast_at_utc)).total_seconds() / 86400)
    fast, slower = _f(regime.n_fast), _f(regime.n_supported_slower)
    return min(0.65, fast / (fast + 3)) * 2 ** (-age / 730) * 2 ** (-slower / 4)


def _advance(previous: CapabilityState | None, evidence: CapabilityEvidence) -> CapabilityState:
    assert evidence.raw_time_ms is not None
    value, weight = math.log(evidence.raw_time_ms / 1000), _f(evidence.effective_weight)
    hypotheses, change, likelihood, innovation, candidate = _bocpd(
        None if previous is None else previous.run_length_hypotheses, evidence
    )
    current = _distribution(hypotheses)
    prior_median = (
        math.exp(_f(evidence.prior.population_log_median))
        if previous is None
        else previous.current_form.median_ms / 1000
    )
    threshold = math.log(max(0.001, prior_median - max(1, prior_median * 0.02)))
    candidate_probability = _mu_cdf(threshold, candidate)
    if previous is None:
        prior_sd = _predictive(_prior(evidence.prior), evidence.observation_variance)[2]
        opened = True
        fast = _regime(
            candidate,
            weight,
            0,
            evidence.observed_at_utc,
            ((str(evidence.result_key), evidence.semantic_digest),),
        )
        direction, run, alternating, before = (
            (-1 if value < math.log(prior_median) else 1 if value > math.log(prior_median) else 0),
            1,
            0,
            ZERO_DIGEST,
        )
        lineage = ((str(evidence.result_key), evidence.semantic_digest),)
    else:
        mean = sum(
            _f(item.probability) * _f(item.mean_log_seconds)
            for item in previous.run_length_hypotheses
        )
        variance = sum(
            _f(item.probability)
            * (
                _predictive(_nig(item), evidence.observation_variance)[2] ** 2
                + (_f(item.mean_log_seconds) - mean) ** 2
            )
            for item in previous.run_length_hypotheses
        )
        prior_sd = math.sqrt(max(1e-15, variance))
        old = _nig(previous.fast_regime)
        three_sd_triggered = value <= math.log(prior_median) - 3 * prior_sd
        opened = (candidate_probability >= 0.90 or three_sd_triggered) and candidate.mean < old.mean
        if opened:
            fast = _regime(
                candidate,
                weight,
                0,
                evidence.observed_at_utc,
                ((str(evidence.result_key), evidence.semantic_digest),),
            )
        elif value <= old.mean:
            updated, _one, _two = _update(old, value, evidence.observation_variance, weight)
            fast = _regime(
                updated,
                _f(previous.fast_regime.n_fast) + weight,
                0,
                evidence.observed_at_utc,
                (
                    *previous.fast_regime.lineage,
                    (str(evidence.result_key), evidence.semantic_digest),
                ),
            )
        else:
            probability = sum(
                _f(item.probability) * (1 - _mu_cdf(old.mean + math.log(1.02), _nig(item)))
                for item in hypotheses
            )
            fast = _regime(
                old,
                _f(previous.fast_regime.n_fast),
                _f(previous.fast_regime.n_supported_slower)
                + (weight if probability >= 0.80 else 0),
                previous.fast_regime.last_fast_at_utc,
                previous.fast_regime.lineage,
            )
        direction = (
            -1
            if evidence.raw_time_ms < previous.current_form.median_ms
            else 1
            if evidence.raw_time_ms > previous.current_form.median_ms
            else 0
        )
        run = (
            previous.direction_run + 1 if direction and direction == previous.last_direction else 1
        )
        alternating = previous.alternation_count + int(
            direction != 0 and previous.last_direction != 0 and direction != previous.last_direction
        )
        before, lineage = (
            previous.state_digest,
            (*previous.lineage, (str(evidence.result_key), evidence.semantic_digest)),
        )
    if previous is None:
        three_sd_triggered = False
    fast_state = _nig(fast)
    slower_probability = sum(
        _f(item.probability) * (1 - _mu_cdf(fast_state.mean + math.log(1.02), _nig(item)))
        for item in hypotheses
    )
    persistence = _persistence(fast, evidence.observed_at_utc)
    retained_fast = _single_distribution(fast_state)
    demonstrated = PositiveTimeDistribution(
        tuple(
            QuantilePoint(
                current_point.probability,
                max(
                    1,
                    round(
                        current_point.time_ms * (1 - persistence) + fast_point.time_ms * persistence
                    ),
                ),
            )
            for current_point, fast_point in zip(current.quantiles, retained_fast.quantiles)
        )
    )
    count = 1 if previous is None else previous.observation_count + 1
    bounded = max(-4.0, min(4.0, innovation))
    anomaly = NumericAnomalyPattern(
        _fs(abs(innovation)), run, alternating, _fs(alternating / max(1, count - 1))
    )
    transition = CapabilityTransition(
        evidence.semantic_digest,
        before,
        current.digest,
        demonstrated.digest,
        _fs(change),
        _fs(bounded / 4),
        evidence.authority_digest,
        _fs(likelihood),
        _fs(innovation),
        _fs(bounded),
        _fs(candidate_probability),
        opened,
        three_sd_triggered,
        _fs(persistence),
        _fs(slower_probability),
        len(hypotheses),
        anomaly,
    )
    content = {
        "competitor_id": evidence.competitor_id,
        "context_digest": evidence.context_digest,
        "state_revision": count,
        "current_form": current,
        "demonstrated_capability": demonstrated,
        "observation_count": count,
        "last_observed_at_utc": evidence.observed_at_utc,
        "last_direction": direction,
        "direction_run": run,
        "alternation_count": alternating,
        "lineage": lineage,
        "run_length_hypotheses": hypotheses,
        "fast_regime": fast,
        "persistence_weight": _fs(persistence),
        "last_transition": transition,
    }
    canonical = {
        "schema_version": CAPABILITY_STATE_SCHEMA_VERSION,
        "operator_version": CAPABILITY_OPERATOR_VERSION,
        "competitor_id": str(evidence.competitor_id),
        "context_digest": evidence.context_digest,
        "state_revision": count,
        "current_form": current.to_dict(),
        "demonstrated_capability": demonstrated.to_dict(),
        "observation_count": count,
        "last_observed_at_utc": evidence.observed_at_utc,
        "last_direction": direction,
        "direction_run": run,
        "alternation_count": alternating,
        "lineage": [list(item) for item in lineage],
        "run_length_hypotheses": [item.to_dict() for item in hypotheses],
        "fast_regime": fast.to_dict(),
        "persistence_weight": _fs(persistence),
        "last_transition": transition.to_dict(),
    }
    return CapabilityState(**content, state_digest=canonical_digest(canonical))  # type: ignore[arg-type]


def replay_capability(evidence: tuple[CapabilityEvidence, ...]) -> CapabilityState | None:
    if not isinstance(evidence, tuple) or any(
        not isinstance(item, CapabilityEvidence) for item in evidence
    ):
        raise ContractError("capability replay requires immutable typed evidence")
    if not evidence:
        return None
    if any(
        item.competitor_id != evidence[0].competitor_id
        or item.context_digest != evidence[0].context_digest
        for item in evidence
    ):
        raise ContractError("capability replay cannot mix competitors or contexts")
    revisions: dict[str, dict[int, CapabilityEvidence]] = {}
    for item in evidence:
        bucket = revisions.setdefault(str(item.result_key), {})
        if item.result_revision in bucket and bucket[item.result_revision] != item:
            raise ContractError("capability replay contains conflicting revisions")
        bucket[item.result_revision] = item
    active = []
    for bucket in revisions.values():
        ordered = sorted(bucket)
        if any(right != left + 1 for left, right in zip(ordered, ordered[1:])):
            raise ContractError("capability revision lineage contains a gap")
        active.append(bucket[ordered[-1]])
    state = None
    for item in sorted(
        (item for item in active if item.numeric_eligible),
        key=lambda item: (item.observed_at_utc, str(item.result_key)),
    ):
        state = _advance(state, item)
    return state


@dataclass(frozen=True, slots=True)
class CapabilityAdjustment:
    assessor: AssessorKind
    operator_version: str
    capability_state_digest: str
    original_distribution_digest: str
    adjusted_distribution: PositiveTimeDistribution
    adjustment_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.assessor, AssessorKind)
            or self.operator_version != CAPABILITY_OPERATOR_VERSION
        ):
            raise ContractError("capability adjustment identity is invalid")
        _require_digest(self.capability_state_digest, "adjustment state digest")
        _require_digest(self.original_distribution_digest, "original distribution digest")
        if not isinstance(self.adjusted_distribution, PositiveTimeDistribution):
            raise ContractError("adjustment requires a positive distribution")
        _require_digest(self.adjustment_digest, "adjustment digest")
        if self.adjustment_digest != canonical_digest(self.content_value()):
            raise ContractError("adjustment digest mismatch")

    def content_value(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-capability-adjustment-v2",
            "assessor": self.assessor.value,
            "operator_version": self.operator_version,
            "capability_state_digest": self.capability_state_digest,
            "original_distribution_digest": self.original_distribution_digest,
            "adjusted_distribution": self.adjusted_distribution.to_dict(),
        }


def _at(distribution: PositiveTimeDistribution, probability: Decimal) -> int:
    points = [(Decimal(item.probability), item.time_ms) for item in distribution.quantiles]
    if probability <= points[0][0]:
        return points[0][1]
    if probability >= points[-1][0]:
        return points[-1][1]
    left, right = next(
        pair for pair in zip(points, points[1:]) if pair[0][0] <= probability <= pair[1][0]
    )
    return round(
        left[1] + float((probability - left[0]) / (right[0] - left[0])) * (right[1] - left[1])
    )


def apply_capability_operator(
    assessor: AssessorKind, original: PositiveTimeDistribution, state: CapabilityState
) -> CapabilityAdjustment:
    if not isinstance(assessor, AssessorKind) or assessor is AssessorKind.LLM_MEMBER:
        raise ContractError("capability operator accepts Formula, ML, or LLM council")
    if not isinstance(original, PositiveTimeDistribution) or not isinstance(state, CapabilityState):
        raise ContractError("capability operator requires typed inputs")
    adjusted = PositiveTimeDistribution(
        tuple(
            QuantilePoint(
                point.probability,
                max(
                    1,
                    (
                        point.time_ms * 600
                        + _at(state.demonstrated_capability, Decimal(point.probability)) * 400
                    )
                    // 1000,
                ),
            )
            for point in original.quantiles
        )
    )
    content = {
        "schema_version": "strathmark-v3-capability-adjustment-v2",
        "assessor": assessor.value,
        "operator_version": CAPABILITY_OPERATOR_VERSION,
        "capability_state_digest": state.state_digest,
        "original_distribution_digest": original.digest,
        "adjusted_distribution": adjusted.to_dict(),
    }
    return CapabilityAdjustment(
        assessor,
        CAPABILITY_OPERATOR_VERSION,
        state.state_digest,
        original.digest,
        adjusted,
        canonical_digest(content),
    )


@dataclass(frozen=True, slots=True)
class PromotionScoreRetention:
    assessor: AssessorKind
    original_forecast_digest: str
    adjusted_forecast_digest: str
    original_score: str
    adjusted_score: str

    def __post_init__(self) -> None:
        if not isinstance(self.assessor, AssessorKind):
            raise ContractError("promotion score assessor is invalid")
        _require_digest(self.original_forecast_digest, "original forecast digest")
        _require_digest(self.adjusted_forecast_digest, "adjusted forecast digest")
        _dec(self.original_score, "original score", "0", "1000000000")
        _dec(self.adjusted_score, "adjusted score", "0", "1000000000")


def retain_promotion_scores(
    adjustment: CapabilityAdjustment, *, original_score: str, adjusted_score: str
) -> PromotionScoreRetention:
    if not isinstance(adjustment, CapabilityAdjustment):
        raise ContractError("promotion scores require an adjustment")
    return PromotionScoreRetention(
        adjustment.assessor,
        adjustment.original_distribution_digest,
        adjustment.adjusted_distribution.digest,
        original_score,
        adjusted_score,
    )


@dataclass(frozen=True, slots=True)
class CapabilityPromotionPolicy:
    """Frozen tolerance for the shared capability operator in factory replay."""

    max_adjusted_score_regression: str

    def __post_init__(self) -> None:
        _dec(
            self.max_adjusted_score_regression,
            "maximum adjusted score regression",
            "0",
            "1000000000",
        )

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": "strathmark-v3-capability-promotion-policy-v1",
                "max_adjusted_score_regression": self.max_adjusted_score_regression,
            }
        )


@dataclass(frozen=True, slots=True)
class CapabilityPromotionEvaluation:
    candidate_digest: str
    retentions: tuple[PromotionScoreRetention, ...]
    operator_application_counts: tuple[tuple[AssessorKind, int], ...]
    policy: CapabilityPromotionPolicy
    passed: bool
    failure_codes: tuple[str, ...]
    evaluation_digest: str

    def __post_init__(self) -> None:
        _require_digest(self.candidate_digest, "capability promotion candidate")
        if (
            not isinstance(self.retentions, tuple)
            or len(self.retentions) != 3
            or {item.assessor for item in self.retentions}
            != {AssessorKind.FORMULA, AssessorKind.ML, AssessorKind.LLM_COUNCIL}
        ):
            raise ContractError("capability promotion requires all three outer assessors")
        if self.retentions != tuple(sorted(self.retentions, key=lambda item: item.assessor.value)):
            raise ContractError("capability promotion retentions must be canonically sorted")
        expected_counts = tuple(
            (item, 1) for item in (AssessorKind.FORMULA, AssessorKind.LLM_COUNCIL, AssessorKind.ML)
        )
        if self.operator_application_counts != expected_counts:
            raise ContractError("capability operator must be applied exactly once per assessor")
        if not isinstance(self.policy, CapabilityPromotionPolicy):
            raise ContractError("capability promotion policy must be frozen")
        expected_failures = tuple(
            sorted(
                f"overprotection:{item.assessor.value}"
                for item in self.retentions
                if Decimal(item.adjusted_score) - Decimal(item.original_score)
                > Decimal(self.policy.max_adjusted_score_regression)
            )
        )
        if self.failure_codes != expected_failures or self.passed is not (not expected_failures):
            raise ContractError("capability promotion outcome differs from retained scores")
        _require_digest(self.evaluation_digest, "capability promotion evaluation")
        if self.evaluation_digest != canonical_digest(self.body()):
            raise ContractError("capability promotion evaluation digest differs")

    def body(self) -> dict[str, object]:
        return _capability_promotion_body(
            self.candidate_digest,
            self.retentions,
            self.operator_application_counts,
            self.policy,
            self.passed,
            self.failure_codes,
        )


def _capability_promotion_body(
    candidate_digest: str,
    retentions: tuple[PromotionScoreRetention, ...],
    operator_application_counts: tuple[tuple[AssessorKind, int], ...],
    policy: CapabilityPromotionPolicy,
    passed: bool,
    failure_codes: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema_version": "strathmark-v3-capability-promotion-evaluation-v1",
        "candidate_digest": candidate_digest,
        "retentions": [
            {
                "assessor": item.assessor.value,
                "original_forecast_digest": item.original_forecast_digest,
                "adjusted_forecast_digest": item.adjusted_forecast_digest,
                "original_score": item.original_score,
                "adjusted_score": item.adjusted_score,
            }
            for item in retentions
        ],
        "operator_application_counts": [
            [item.value, count] for item, count in operator_application_counts
        ],
        "policy_digest": policy.digest,
        "passed": passed,
        "failure_codes": list(failure_codes),
    }


def evaluate_capability_promotion(
    *,
    candidate_digest: str,
    retentions: tuple[PromotionScoreRetention, ...],
    operator_application_counts: tuple[tuple[AssessorKind, int], ...],
    policy: CapabilityPromotionPolicy,
) -> CapabilityPromotionEvaluation:
    """Score immutable original/adjusted pairs and reject repeat application."""

    if not isinstance(retentions, tuple) or not all(
        isinstance(item, PromotionScoreRetention) for item in retentions
    ):
        raise ContractError("capability promotion requires typed retained scores")
    ordered = tuple(sorted(retentions, key=lambda item: item.assessor.value))
    if not isinstance(operator_application_counts, tuple):
        raise ContractError("capability operator application counts must be immutable")
    counts = tuple(sorted(operator_application_counts, key=lambda item: item[0].value))
    if any(count != 1 for _assessor, count in counts):
        raise ContractError("capability operator must be applied exactly once per assessor")
    failures = tuple(
        sorted(
            f"overprotection:{item.assessor.value}"
            for item in ordered
            if Decimal(item.adjusted_score) - Decimal(item.original_score)
            > Decimal(policy.max_adjusted_score_regression)
        )
    )
    body = _capability_promotion_body(
        candidate_digest, ordered, counts, policy, not failures, failures
    )
    return CapabilityPromotionEvaluation(
        candidate_digest,
        ordered,
        counts,
        policy,
        not failures,
        failures,
        canonical_digest(body),
    )


@dataclass(frozen=True, slots=True)
class CapabilityCapacityEnvelope:
    maximum_lineage_rows: int = 256
    maximum_invalidated_work: int = 128
    maximum_mandatory_reactions: int = 512

    def __post_init__(self) -> None:
        for value, label in (
            (self.maximum_lineage_rows, "lineage capacity"),
            (self.maximum_invalidated_work, "invalidation capacity"),
            (self.maximum_mandatory_reactions, "reaction capacity"),
        ):
            _int(value, label)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-capability-capacity-v1",
            "maximum_lineage_rows": self.maximum_lineage_rows,
            "maximum_invalidated_work": self.maximum_invalidated_work,
            "maximum_mandatory_reactions": self.maximum_mandatory_reactions,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityCapacityEnvelope:
        if (
            set(value)
            != {
                "schema_version",
                "maximum_lineage_rows",
                "maximum_invalidated_work",
                "maximum_mandatory_reactions",
            }
            or value.get("schema_version") != "strathmark-v3-capability-capacity-v1"
        ):
            raise ContractError("capacity envelope fields are not closed")
        return cls(
            value["maximum_lineage_rows"],
            value["maximum_invalidated_work"],
            value["maximum_mandatory_reactions"],
        )

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class RebaseCapacityDecision:
    admitted: bool
    evidence_preserved: bool
    next_round_barrier_open: bool
    reason: str
    lineage_rows: int
    invalidated_work: int
    mandatory_reactions: int
    envelope_digest: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(item, bool)
            for item in (self.admitted, self.evidence_preserved, self.next_round_barrier_open)
        ):
            raise ContractError("capacity decisions require booleans")
        if self.reason not in {
            "within_capacity",
            "lineage_capacity_exceeded",
            "invalidation_capacity_exceeded",
            "reaction_capacity_exceeded",
        }:
            raise ContractError("unknown capacity reason")
        for value in (self.lineage_rows, self.invalidated_work, self.mandatory_reactions):
            _int(value, "capacity count", positive=False)
        _require_digest(self.envelope_digest, "capacity envelope digest")


def evaluate_rebase_capacity(
    envelope: CapabilityCapacityEnvelope,
    *,
    lineage_rows: int,
    invalidated_work: int,
    mandatory_reactions: int,
) -> RebaseCapacityDecision:
    if not isinstance(envelope, CapabilityCapacityEnvelope):
        raise ContractError("rebase capacity requires a signed envelope")
    for value in (lineage_rows, invalidated_work, mandatory_reactions):
        _int(value, "capacity count", positive=False)
    reason = (
        "lineage_capacity_exceeded"
        if lineage_rows > envelope.maximum_lineage_rows
        else "invalidation_capacity_exceeded"
        if invalidated_work > envelope.maximum_invalidated_work
        else "reaction_capacity_exceeded"
        if mandatory_reactions > envelope.maximum_mandatory_reactions
        else "within_capacity"
    )
    admitted = reason == "within_capacity"
    return RebaseCapacityDecision(
        admitted,
        True,
        admitted,
        reason,
        lineage_rows,
        invalidated_work,
        mandatory_reactions,
        envelope.digest,
    )


__all__ = [
    "BOCPD_HAZARD",
    "BOCPD_RUN_LENGTH_CAP",
    "CAPABILITY_OPERATOR_VERSION",
    "CapabilityAdjustment",
    "CapabilityCapacityEnvelope",
    "CapabilityEvidence",
    "CapabilityPrior",
    "CapabilityPromotionEvaluation",
    "CapabilityPromotionPolicy",
    "CapabilityState",
    "CapabilityTransition",
    "FastCapabilityRegime",
    "HistoricalImportBinding",
    "NumericAnomalyPattern",
    "PromotionScoreRetention",
    "RebaseCapacityDecision",
    "RunLengthHypothesis",
    "apply_capability_operator",
    "evaluate_rebase_capacity",
    "evaluate_capability_promotion",
    "replay_capability",
    "retain_promotion_scores",
]
