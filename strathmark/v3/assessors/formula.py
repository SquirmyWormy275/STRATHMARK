"""Frozen transparent Formula bootstrap over target-context log seconds."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import Any, Mapping

from strathmark.v3.assessors.base import (
    ArithmeticTraceRow,
    AssessmentResult,
    EvidenceQuality,
    FormulaInputPacket,
    ReviewClassification,
    TournamentRelevance,
)
from strathmark.v3.contracts.canonical import (
    canonical_bytes,
    canonical_decimal_string,
    canonical_digest,
)
from strathmark.v3.contracts.evidence import TargetContext, _require_digest
from strathmark.v3.contracts.forecasts import (
    ArtifactIdentity,
    AssessorForecast,
    AssessorKind,
    EvidenceSupport,
    ForecastState,
    ForecastWarning,
    PositiveTimeDistribution,
    QuantilePoint,
)
from strathmark.v3.contracts.identifiers import deterministic_identifier
from strathmark.v3.contracts.statuses import admit_raw_completion

FORMULA_MANIFEST_SCHEMA = "strathmark-v3-formula-manifest-v2"
_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True, slots=True, order=True)
class ContextPrior:
    """One causally frozen exact target-context prior."""

    event_code: str
    size_mm: int
    material_code: str
    median_seconds: str
    log_variance: str
    pseudo_count: int
    lineage_digest: str

    def __post_init__(self) -> None:
        if not self.event_code or not self.material_code:
            raise ValueError("context prior requires event and material codes")
        _validate_prior_values(
            self.size_mm,
            self.median_seconds,
            self.log_variance,
            self.pseudo_count,
            self.lineage_digest,
            "context prior",
        )

    @property
    def key(self) -> str:
        return f"{self.event_code}|{self.size_mm}|{self.material_code}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_code": self.event_code,
            "size_mm": self.size_mm,
            "material_code": self.material_code,
            "median_seconds": self.median_seconds,
            "log_variance": self.log_variance,
            "pseudo_count": self.pseudo_count,
            "lineage_digest": self.lineage_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextPrior:
        expected = {
            "event_code",
            "size_mm",
            "material_code",
            "median_seconds",
            "log_variance",
            "pseudo_count",
            "lineage_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("context prior fields are not closed")
        return cls(**value)


@dataclass(frozen=True, slots=True, order=True)
class DisciplinePrior:
    """One causally frozen declared-discipline fallback prior."""

    discipline: str
    median_seconds: str
    log_variance: str
    pseudo_count: int
    lineage_digest: str

    def __post_init__(self) -> None:
        if not self.discipline:
            raise ValueError("discipline prior requires a discipline code")
        _validate_prior_values(
            1,
            self.median_seconds,
            self.log_variance,
            self.pseudo_count,
            self.lineage_digest,
            "discipline prior",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "discipline": self.discipline,
            "median_seconds": self.median_seconds,
            "log_variance": self.log_variance,
            "pseudo_count": self.pseudo_count,
            "lineage_digest": self.lineage_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DisciplinePrior:
        expected = {
            "discipline",
            "median_seconds",
            "log_variance",
            "pseudo_count",
            "lineage_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("discipline prior fields are not closed")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class _PriorSelection:
    tier: str
    key: str
    median_seconds: Decimal
    log_variance: Decimal
    pseudo_count: int
    lineage_digest: str


@dataclass(frozen=True, slots=True)
class FormulaZeroHistoryPrior:
    """Exact no-observation Formula prior selected from one pinned manifest."""

    target_context_digest: str
    distribution: PositiveTimeDistribution
    prior_tier: str
    prior_key: str
    prior_lineage_digest: str
    manifest_digest: str

    def __post_init__(self) -> None:
        _require_digest(self.target_context_digest, "zero-history target context")
        if not isinstance(self.distribution, PositiveTimeDistribution):
            raise ValueError("zero-history Formula prior requires a distribution")
        if self.prior_tier not in {"exact_context", "discipline", "population"}:
            raise ValueError("zero-history Formula prior tier is invalid")
        if not isinstance(self.prior_key, str) or not self.prior_key:
            raise ValueError("zero-history Formula prior key is invalid")
        _require_digest(self.prior_lineage_digest, "zero-history prior lineage")
        _require_digest(self.manifest_digest, "zero-history Formula manifest")


@dataclass(frozen=True, slots=True)
class FormulaManifest:
    """Complete immutable parameter bundle for the first V3 Formula candidate."""

    version: str
    time_quantum_ms: int
    prior_median_seconds: str
    prior_log_sigma: str
    prior_log_variance: str
    prior_pseudo_count: int
    prior_lineage_digest: str
    context_priors: tuple[ContextPrior, ...]
    discipline_priors: tuple[DisciplinePrior, ...]
    huber_tuning: str
    irls_max_iterations: int
    irls_tolerance: str
    mad_consistency: str
    minimum_robust_scale: str
    exact_context_factor: str
    same_discipline_factor: str
    cross_discipline_factor: str
    diameter_decay: str
    recency_half_life_days: str
    issued_official_quality: str
    verified_historical_quality: str
    active_tournament_factor: str
    authoritative_tournament_factor: str
    legacy_tournament_factor: str
    event_scales: tuple[tuple[str, str], ...]
    event_size_exponents: tuple[tuple[str, str], ...]
    event_disciplines: tuple[tuple[str, str], ...]
    declared_event_relations: tuple[tuple[str, str], ...]
    density_exponent: str
    same_discipline_variance: str
    cross_discipline_variance: str
    size_variance_coefficient: str
    material_variance_coefficient: str
    minimum_log_sigma: str
    maximum_log_sigma: str
    minimum_time_ms: int
    maximum_time_ms: int
    dense_effective_sample_size: str
    quantiles: tuple[tuple[str, str], ...]
    digest: str
    schema_version: str = FORMULA_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != FORMULA_MANIFEST_SCHEMA or self.version != "formula:v2-bootstrap":
            raise ValueError("unsupported formula bootstrap schema or version")
        for value, label in (
            (self.time_quantum_ms, "time_quantum_ms"),
            (self.prior_pseudo_count, "prior_pseudo_count"),
            (self.irls_max_iterations, "irls_max_iterations"),
            (self.minimum_time_ms, "minimum_time_ms"),
            (self.maximum_time_ms, "maximum_time_ms"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        if self.time_quantum_ms != 1 or self.prior_pseudo_count != 3:
            raise ValueError("bootstrap requires one-millisecond time and three prior observations")
        if self.irls_max_iterations != 20 or self.maximum_time_ms <= self.minimum_time_ms:
            raise ValueError("bootstrap iteration or positive time bounds are invalid")
        decimal_fields = (
            "prior_median_seconds",
            "prior_log_sigma",
            "prior_log_variance",
            "huber_tuning",
            "irls_tolerance",
            "mad_consistency",
            "minimum_robust_scale",
            "exact_context_factor",
            "same_discipline_factor",
            "cross_discipline_factor",
            "diameter_decay",
            "recency_half_life_days",
            "issued_official_quality",
            "verified_historical_quality",
            "active_tournament_factor",
            "authoritative_tournament_factor",
            "legacy_tournament_factor",
            "density_exponent",
            "same_discipline_variance",
            "cross_discipline_variance",
            "size_variance_coefficient",
            "material_variance_coefficient",
            "minimum_log_sigma",
            "maximum_log_sigma",
            "dense_effective_sample_size",
        )
        for field in decimal_fields:
            value = getattr(self, field)
            if canonical_decimal_string(value) != value or Decimal(value) <= 0:
                raise ValueError(f"{field} must be a positive canonical decimal")
        if Decimal(self.prior_log_sigma) ** 2 != Decimal(self.prior_log_variance):
            raise ValueError("population prior sigma and variance must agree")
        _require_digest(self.prior_lineage_digest, "population prior lineage digest")
        if (
            not isinstance(self.context_priors, tuple)
            or not self.context_priors
            or not all(isinstance(item, ContextPrior) for item in self.context_priors)
        ):
            raise ValueError("context priors must be a nonempty immutable typed tuple")
        context_keys = tuple(item.key for item in self.context_priors)
        if context_keys != tuple(sorted(context_keys)) or len(context_keys) != len(
            set(context_keys)
        ):
            raise ValueError("context prior keys must be unique and sorted")
        if (
            not isinstance(self.discipline_priors, tuple)
            or not self.discipline_priors
            or not all(isinstance(item, DisciplinePrior) for item in self.discipline_priors)
        ):
            raise ValueError("discipline priors must be a nonempty immutable typed tuple")
        discipline_keys = tuple(item.discipline for item in self.discipline_priors)
        if discipline_keys != tuple(sorted(discipline_keys)) or len(discipline_keys) != len(
            set(discipline_keys)
        ):
            raise ValueError("discipline prior keys must be unique and sorted")
        frozen_values = {
            "huber_tuning": "1.5",
            "irls_tolerance": "0.0000000001",
            "exact_context_factor": "1",
            "same_discipline_factor": "0.6",
            "cross_discipline_factor": "0.25",
            "diameter_decay": "2",
            "recency_half_life_days": "730",
            "issued_official_quality": "1",
            "verified_historical_quality": "0.85",
            "active_tournament_factor": "1",
            "authoritative_tournament_factor": "0.9",
            "legacy_tournament_factor": "0.75",
        }
        if any(getattr(self, field) != expected for field, expected in frozen_values.items()):
            raise ValueError("formula bootstrap constants do not match the frozen plan")
        for table, label in (
            (self.event_scales, "event_scales"),
            (self.event_size_exponents, "event_size_exponents"),
            (self.event_disciplines, "event_disciplines"),
            (self.declared_event_relations, "declared_event_relations"),
            (self.quantiles, "quantiles"),
        ):
            _validate_table(table, label)
        if any(Decimal(item) <= 0 for _, item in self.event_scales + self.event_size_exponents):
            raise ValueError("event scales and size exponents must be positive")
        if any(
            item not in {"same_discipline", "cross_discipline"}
            for _, item in self.declared_event_relations
        ):
            raise ValueError("declared event relations are closed")
        probabilities = tuple(Decimal(probability) for probability, _ in self.quantiles)
        if probabilities != tuple(sorted(probabilities)) or Decimal("0.5") not in probabilities:
            raise ValueError("formula quantiles must be ordered and include 0.5")
        _require_digest(self.digest, "formula manifest digest")
        if self.digest != canonical_digest(self._content_value()):
            raise ValueError("formula manifest digest mismatch")

    @property
    def prior_median_ms(self) -> int:
        return _round_ms(Decimal(self.prior_median_seconds) * 1000)

    @property
    def prior_sigma_ms(self) -> int:
        with localcontext() as context:
            context.prec = 64
            context.rounding = ROUND_HALF_EVEN
            median = Decimal(self.prior_median_seconds)
            sigma = Decimal(self.prior_log_sigma)
            z = max(abs(Decimal(item)) for _, item in self.quantiles)
            return _round_ms(((_exp(z * sigma) - _exp(-z * sigma)) * median * 1000) / 2)

    def _content_value(self) -> dict[str, Any]:
        value = self.to_dict()
        del value["digest"]
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "time_quantum_ms": self.time_quantum_ms,
            "population_prior": {
                "median_seconds": self.prior_median_seconds,
                "log_sigma": self.prior_log_sigma,
                "log_variance": self.prior_log_variance,
                "pseudo_count": self.prior_pseudo_count,
                "lineage_digest": self.prior_lineage_digest,
            },
            "context_priors": [item.to_dict() for item in self.context_priors],
            "discipline_priors": [item.to_dict() for item in self.discipline_priors],
            "robust_center": {
                "huber_tuning": self.huber_tuning,
                "max_iterations": self.irls_max_iterations,
                "tolerance": self.irls_tolerance,
                "mad_consistency": self.mad_consistency,
                "minimum_scale": self.minimum_robust_scale,
            },
            "context": {
                "exact": self.exact_context_factor,
                "same_discipline": self.same_discipline_factor,
                "cross_discipline": self.cross_discipline_factor,
                "diameter_decay": self.diameter_decay,
            },
            "recency_half_life_days": self.recency_half_life_days,
            "quality": {
                "issued_official": self.issued_official_quality,
                "verified_historical": self.verified_historical_quality,
            },
            "tournament": {
                "active": self.active_tournament_factor,
                "other_authoritative": self.authoritative_tournament_factor,
                "legacy": self.legacy_tournament_factor,
            },
            "event_scales": dict(self.event_scales),
            "event_size_exponents": dict(self.event_size_exponents),
            "event_disciplines": dict(self.event_disciplines),
            "declared_event_relations": dict(self.declared_event_relations),
            "density_exponent": self.density_exponent,
            "conversion_variance": {
                "same_discipline": self.same_discipline_variance,
                "cross_discipline": self.cross_discipline_variance,
                "size_coefficient": self.size_variance_coefficient,
                "material_coefficient": self.material_variance_coefficient,
            },
            "positive_bounds": {
                "minimum_log_sigma": self.minimum_log_sigma,
                "maximum_log_sigma": self.maximum_log_sigma,
                "minimum_time_ms": self.minimum_time_ms,
                "maximum_time_ms": self.maximum_time_ms,
            },
            "dense_effective_sample_size": self.dense_effective_sample_size,
            "quantiles": dict(self.quantiles),
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FormulaManifest:
        expected = {
            "schema_version",
            "version",
            "time_quantum_ms",
            "population_prior",
            "context_priors",
            "discipline_priors",
            "robust_center",
            "context",
            "recency_half_life_days",
            "quality",
            "tournament",
            "event_scales",
            "event_size_exponents",
            "event_disciplines",
            "declared_event_relations",
            "density_exponent",
            "conversion_variance",
            "positive_bounds",
            "dense_effective_sample_size",
            "quantiles",
            "digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("formula manifest fields are not closed")
        prior = _closed_mapping(
            value["population_prior"],
            {
                "median_seconds",
                "log_sigma",
                "log_variance",
                "pseudo_count",
                "lineage_digest",
            },
            "population_prior",
        )
        context_priors = value["context_priors"]
        discipline_priors = value["discipline_priors"]
        if not isinstance(context_priors, list):
            raise ValueError("context_priors must be a JSON array")
        if not isinstance(discipline_priors, list):
            raise ValueError("discipline_priors must be a JSON array")
        robust = _closed_mapping(
            value["robust_center"],
            {"huber_tuning", "max_iterations", "tolerance", "mad_consistency", "minimum_scale"},
            "robust_center",
        )
        context = _closed_mapping(
            value["context"],
            {"exact", "same_discipline", "cross_discipline", "diameter_decay"},
            "context",
        )
        quality = _closed_mapping(
            value["quality"], {"issued_official", "verified_historical"}, "quality"
        )
        tournament = _closed_mapping(
            value["tournament"], {"active", "other_authoritative", "legacy"}, "tournament"
        )
        variance = _closed_mapping(
            value["conversion_variance"],
            {"same_discipline", "cross_discipline", "size_coefficient", "material_coefficient"},
            "conversion_variance",
        )
        bounds = _closed_mapping(
            value["positive_bounds"],
            {"minimum_log_sigma", "maximum_log_sigma", "minimum_time_ms", "maximum_time_ms"},
            "positive_bounds",
        )
        return cls(
            schema_version=value["schema_version"],
            version=value["version"],
            time_quantum_ms=value["time_quantum_ms"],
            prior_median_seconds=prior["median_seconds"],
            prior_log_sigma=prior["log_sigma"],
            prior_log_variance=prior["log_variance"],
            prior_pseudo_count=prior["pseudo_count"],
            prior_lineage_digest=prior["lineage_digest"],
            context_priors=tuple(ContextPrior.from_dict(item) for item in context_priors),
            discipline_priors=tuple(DisciplinePrior.from_dict(item) for item in discipline_priors),
            huber_tuning=robust["huber_tuning"],
            irls_max_iterations=robust["max_iterations"],
            irls_tolerance=robust["tolerance"],
            mad_consistency=robust["mad_consistency"],
            minimum_robust_scale=robust["minimum_scale"],
            exact_context_factor=context["exact"],
            same_discipline_factor=context["same_discipline"],
            cross_discipline_factor=context["cross_discipline"],
            diameter_decay=context["diameter_decay"],
            recency_half_life_days=value["recency_half_life_days"],
            issued_official_quality=quality["issued_official"],
            verified_historical_quality=quality["verified_historical"],
            active_tournament_factor=tournament["active"],
            authoritative_tournament_factor=tournament["other_authoritative"],
            legacy_tournament_factor=tournament["legacy"],
            event_scales=_mapping_table(value["event_scales"], "event_scales"),
            event_size_exponents=_mapping_table(
                value["event_size_exponents"], "event_size_exponents"
            ),
            event_disciplines=_mapping_table(value["event_disciplines"], "event_disciplines"),
            declared_event_relations=_mapping_table(
                value["declared_event_relations"], "declared_event_relations"
            ),
            density_exponent=value["density_exponent"],
            same_discipline_variance=variance["same_discipline"],
            cross_discipline_variance=variance["cross_discipline"],
            size_variance_coefficient=variance["size_coefficient"],
            material_variance_coefficient=variance["material_coefficient"],
            minimum_log_sigma=bounds["minimum_log_sigma"],
            maximum_log_sigma=bounds["maximum_log_sigma"],
            minimum_time_ms=bounds["minimum_time_ms"],
            maximum_time_ms=bounds["maximum_time_ms"],
            dense_effective_sample_size=value["dense_effective_sample_size"],
            quantiles=_mapping_table(value["quantiles"], "quantiles"),
            digest=value["digest"],
        )

    @classmethod
    def load(cls, path: Path | str) -> FormulaManifest:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True, slots=True)
class _Row:
    sequence: int
    admitted: bool
    raw_ms: int | None
    log_seconds: Decimal | None
    context_factor: Decimal
    diameter_similarity: Decimal
    recency: Decimal
    quality: Decimal
    tournament: Decimal
    conversion_variance: Decimal
    weight: Decimal
    conversion_status: str
    event_factor: Decimal
    size_factor: Decimal
    material_factor: Decimal
    exact: bool


@dataclass(frozen=True, slots=True)
class _Iteration:
    number: int
    start: Decimal
    end: Decimal
    delta: Decimal
    effective_weight: Decimal


def _select_prior(target: TargetContext, manifest: FormulaManifest) -> _PriorSelection:
    exact_key = f"{target.event_code}|{target.size_mm}|{target.material_code}"
    exact = next((item for item in manifest.context_priors if item.key == exact_key), None)
    if exact is not None:
        return _PriorSelection(
            "exact_context",
            exact.key,
            Decimal(exact.median_seconds),
            Decimal(exact.log_variance),
            exact.pseudo_count,
            exact.lineage_digest,
        )
    discipline = dict(manifest.event_disciplines).get(target.event_code)
    fallback = next(
        (item for item in manifest.discipline_priors if item.discipline == discipline), None
    )
    if fallback is not None:
        return _PriorSelection(
            "discipline",
            fallback.discipline,
            Decimal(fallback.median_seconds),
            Decimal(fallback.log_variance),
            fallback.pseudo_count,
            fallback.lineage_digest,
        )
    return _PriorSelection(
        "population",
        "population",
        Decimal(manifest.prior_median_seconds),
        Decimal(manifest.prior_log_variance),
        manifest.prior_pseudo_count,
        manifest.prior_lineage_digest,
    )


def resolve_zero_history_prior(
    target: TargetContext, manifest: FormulaManifest
) -> FormulaZeroHistoryPrior:
    """Resolve the exact broad Formula basis used when no history is eligible."""

    if not isinstance(target, TargetContext) or not isinstance(manifest, FormulaManifest):
        raise ValueError("zero-history Formula prior requires typed context and manifest")
    with localcontext() as context:
        context.prec = 64
        context.rounding = ROUND_HALF_EVEN
        prior = _select_prior(target, manifest)
        distribution = _distribution(_ln(prior.median_seconds), _sqrt(prior.log_variance), manifest)
    return FormulaZeroHistoryPrior(
        target.digest,
        distribution,
        prior.tier,
        prior.key,
        prior.lineage_digest,
        manifest.digest,
    )


def assess_formula(packet: FormulaInputPacket, manifest: FormulaManifest) -> AssessmentResult:
    """Run the frozen bootstrap with no field, identity, or assessor side channel."""

    if not isinstance(packet, FormulaInputPacket):
        raise ValueError("formula assessor requires one FormulaInputPacket")
    if not isinstance(manifest, FormulaManifest):
        raise ValueError("formula assessor requires one FormulaManifest")
    with localcontext() as context:
        context.prec = 64
        context.rounding = ROUND_HALF_EVEN
        return _assess(packet, manifest)


def _assess(packet: FormulaInputPacket, manifest: FormulaManifest) -> AssessmentResult:
    rows = _rows(packet, manifest)
    prior = _select_prior(packet.evidence.target_context, manifest)
    usable = tuple(
        row for row in rows if row.admitted and row.weight > 0 and row.log_seconds is not None
    )
    personal_weight = sum((row.weight for row in usable), _ZERO)
    prior_log = _ln(prior.median_seconds)
    values = tuple(row.log_seconds for row in usable) + (prior_log,) * prior.pseudo_count
    weights = tuple(row.weight for row in usable) + (_ONE,) * prior.pseudo_count
    initial = _weighted_median(values, weights)
    mad = _weighted_median(tuple(abs(value - initial) for value in values), weights)
    scale = max(Decimal(manifest.minimum_robust_scale), Decimal(manifest.mad_consistency) * mad)
    center, iterations = _irls(values, weights, initial, scale, manifest)
    neff = _effective_sample_size(tuple(row.weight for row in usable))
    if not usable:
        center = prior_log
        log_scale = _sqrt(prior.log_variance)
        components = (_ZERO, _ZERO, prior.log_variance, _ONE)
    else:
        residual_variance = _robust_residual_variance(usable, center, scale, manifest)
        conversion_variance = (
            sum((row.weight * row.conversion_variance for row in usable), _ZERO) / personal_weight
        )
        prior_fraction = Decimal(prior.pseudo_count) / (
            Decimal(prior.pseudo_count) + personal_weight
        )
        prior_variance = prior_fraction * prior.log_variance
        scarcity = _ONE + _ONE / max(neff, Decimal("0.25"))
        variance = (residual_variance + conversion_variance + prior_variance) * scarcity
        log_scale = max(
            Decimal(manifest.minimum_log_sigma),
            min(Decimal(manifest.maximum_log_sigma), _sqrt(variance)),
        )
        components = (residual_variance, conversion_variance, prior_variance, scarcity)
    distribution = _distribution(center, log_scale, manifest)
    uncertainty_ms = (distribution.quantiles[-1].time_ms - distribution.quantiles[0].time_ms) // 2
    unsupported = any(row.admitted and row.weight == 0 for row in rows)
    review, warnings = _review(usable, neff, unsupported, prior, manifest)
    evidence = packet.evidence
    forecast = AssessorForecast.create(
        forecast_id=deterministic_identifier(
            "forecast", {"assessor": "formula", "input": packet.digest, "manifest": manifest.digest}
        ),
        assessor=AssessorKind.FORMULA,
        state=ForecastState.COMMITTED,
        evidence_digest=packet.digest,
        distribution=distribution,
        support=EvidenceSupport(
            eligible_count=sum(row.admitted for row in rows),
            effective_weight=canonical_decimal_string(personal_weight),
            exact_context_count=sum(row.admitted and row.exact for row in rows),
            max_historical_key=evidence.historical_cutoff_key,
            tournament_event_sequence=evidence.tournament_event_sequence,
        ),
        warnings=warnings,
        artifacts=(ArtifactIdentity("formula_manifest", manifest.version, manifest.digest),),
        abstention_code=None,
    )
    trace = _trace(
        packet,
        manifest,
        prior,
        rows,
        initial,
        mad,
        scale,
        iterations,
        center,
        log_scale,
        neff,
        components,
        distribution,
    )
    return AssessmentResult.create(
        forecast=forecast,
        review=review,
        center_ms=distribution.median_ms,
        uncertainty_ms=max(1, uncertainty_ms),
        log_center=canonical_decimal_string(center),
        log_scale=canonical_decimal_string(log_scale),
        effective_sample_size=canonical_decimal_string(neff),
        personal_weight=canonical_decimal_string(personal_weight),
        manifest_digest=manifest.digest,
        trace=trace,
    )


def _rows(packet: FormulaInputPacket, manifest: FormulaManifest) -> tuple[_Row, ...]:
    rows = []
    for observation, facts in zip(packet.evidence.observations, packet.observation_facts):
        admitted = admit_raw_completion(observation.result)
        conversion = _conversion(observation.context, packet.evidence.target_context, manifest)
        (
            context_factor,
            diameter,
            variance,
            status,
            event_factor,
            size_factor,
            material_factor,
            exact,
        ) = conversion
        recency = _power(
            Decimal("2"), -Decimal(facts.age_days) / Decimal(manifest.recency_half_life_days)
        )
        quality = Decimal(
            manifest.issued_official_quality
            if facts.quality is EvidenceQuality.ISSUED_OFFICIAL
            else manifest.verified_historical_quality
        )
        tournament_values = {
            TournamentRelevance.ACTIVE: manifest.active_tournament_factor,
            TournamentRelevance.OTHER_AUTHORITATIVE: manifest.authoritative_tournament_factor,
            TournamentRelevance.LEGACY: manifest.legacy_tournament_factor,
        }
        tournament = Decimal(tournament_values[facts.tournament])
        if admitted is None:
            raw_ms, log_seconds, weight = None, None, _ZERO
        else:
            raw_ms = admitted.raw_time_ms
            log_seconds = _ln(Decimal(raw_ms) / 1000 * event_factor * size_factor * material_factor)
            weight = context_factor * diameter * recency * quality * tournament / (_ONE + variance)
            if status.startswith("unsupported"):
                weight = _ZERO
        rows.append(
            _Row(
                observation.observation_sequence,
                admitted is not None,
                raw_ms,
                log_seconds,
                context_factor,
                diameter,
                recency,
                quality,
                tournament,
                variance,
                weight,
                status,
                event_factor,
                size_factor,
                material_factor,
                exact,
            )
        )
    return tuple(rows)


def _conversion(
    source: TargetContext, target: TargetContext, manifest: FormulaManifest
) -> tuple[Decimal, Decimal, Decimal, str, Decimal, Decimal, Decimal, bool]:
    scales, exponents, relations = (
        dict(manifest.event_scales),
        dict(manifest.event_size_exponents),
        dict(manifest.declared_event_relations),
    )
    exact = (
        source.event_code == target.event_code
        and source.size_mm == target.size_mm
        and source.material_code == target.material_code
    )
    pair = f"{source.event_code}->{target.event_code}"
    if source.event_code == target.event_code and source.event_code in scales:
        relation, event_factor = "exact_event", _ONE
    elif pair in relations and source.event_code in scales and target.event_code in scales:
        relation = relations[pair]
        event_factor = Decimal(scales[target.event_code]) / Decimal(scales[source.event_code])
    else:
        relation, event_factor = "unsupported_event", _ONE
    exponent = Decimal(exponents.get(target.event_code, "1"))
    diameter_ratio = Decimal(source.size_mm) / Decimal(target.size_mm)
    size_factor = _power(_ONE / diameter_ratio, exponent)
    diameter = _exp(-Decimal(manifest.diameter_decay) * abs(_ln(diameter_ratio)))
    source_density, target_density = _density(source), _density(target)
    if source.material_code == target.material_code:
        material_factor, material_status = _ONE, "exact_material"
    elif source_density is not None and target_density is not None:
        material_factor = _power(
            target_density / source_density, Decimal(manifest.density_exponent)
        )
        material_status = "density_conversion"
    else:
        material_factor, material_status = _ONE, "unsupported_material_density"
    if relation == "unsupported_event" or material_status.startswith("unsupported"):
        context_factor = _ZERO
        status = relation if relation == "unsupported_event" else material_status
    elif exact:
        context_factor, status = Decimal(manifest.exact_context_factor), "exact"
    elif relation in {"exact_event", "same_discipline"}:
        context_factor, status = (
            Decimal(manifest.same_discipline_factor),
            "declared_same_discipline",
        )
    else:
        context_factor, status = (
            Decimal(manifest.cross_discipline_factor),
            "declared_cross_discipline",
        )
    if relation == "exact_event":
        event_variance = _ZERO
    elif relation == "same_discipline":
        event_variance = Decimal(manifest.same_discipline_variance)
    elif relation == "cross_discipline":
        event_variance = Decimal(manifest.cross_discipline_variance)
    else:
        event_variance = _ZERO
    size_variance = (Decimal(manifest.size_variance_coefficient) * abs(_ln(diameter_ratio))) ** 2
    material_variance = (
        Decimal(manifest.material_variance_coefficient) * abs(_ln(material_factor))
    ) ** 2
    return (
        context_factor,
        diameter,
        event_variance + size_variance + material_variance,
        status,
        event_factor,
        size_factor,
        material_factor,
        exact,
    )


def _irls(
    values: tuple[Decimal, ...],
    weights: tuple[Decimal, ...],
    initial: Decimal,
    scale: Decimal,
    manifest: FormulaManifest,
) -> tuple[Decimal, tuple[_Iteration, ...]]:
    center, iterations, tuning = initial, [], Decimal(manifest.huber_tuning)
    for number in range(1, manifest.irls_max_iterations + 1):
        effective = tuple(
            weight * _huber_weight((value - center) / scale, tuning)
            for value, weight in zip(values, weights)
        )
        total = sum(effective, _ZERO)
        updated = sum((weight * value for value, weight in zip(values, effective)), _ZERO) / total
        delta = abs(updated - center)
        iterations.append(_Iteration(number, center, updated, delta, total))
        center = updated
        if delta <= Decimal(manifest.irls_tolerance):
            break
    return center, tuple(iterations)


def _weighted_median(values: tuple[Decimal, ...], weights: tuple[Decimal, ...]) -> Decimal:
    ordered, halfway, cumulative = (
        sorted(zip(values, weights), key=lambda item: item[0]),
        sum(weights, _ZERO) / 2,
        _ZERO,
    )
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= halfway:
            return value
    raise ValueError("weighted median requires positive weight")


def _huber_weight(residual: Decimal, tuning: Decimal) -> Decimal:
    magnitude = abs(residual)
    return _ONE if magnitude <= tuning else tuning / magnitude


def _effective_sample_size(weights: tuple[Decimal, ...]) -> Decimal:
    if not weights:
        return _ZERO
    total = sum(weights, _ZERO)
    return total**2 / sum((weight**2 for weight in weights), _ZERO)


def _robust_residual_variance(
    rows: tuple[_Row, ...], center: Decimal, scale: Decimal, manifest: FormulaManifest
) -> Decimal:
    tuning, total = Decimal(manifest.huber_tuning), sum((row.weight for row in rows), _ZERO)
    return (
        sum(
            (
                row.weight * min(abs(row.log_seconds - center), tuning * scale) ** 2
                for row in rows
                if row.log_seconds is not None
            ),
            _ZERO,
        )
        / total
    )


def _distribution(
    center: Decimal, scale: Decimal, manifest: FormulaManifest
) -> PositiveTimeDistribution:
    points = []
    for probability, z_score in manifest.quantiles:
        milliseconds = _round_ms(_exp(center + Decimal(z_score) * scale) * 1000)
        points.append(
            QuantilePoint(
                probability,
                max(manifest.minimum_time_ms, min(manifest.maximum_time_ms, milliseconds)),
            )
        )
    return PositiveTimeDistribution(tuple(points))


def _review(
    rows: tuple[_Row, ...],
    neff: Decimal,
    unsupported: bool,
    prior: _PriorSelection,
    manifest: FormulaManifest,
) -> tuple[ReviewClassification, tuple[ForecastWarning, ...]]:
    warnings: set[ForecastWarning] = set()
    if neff < Decimal(manifest.dense_effective_sample_size):
        warnings.add(ForecastWarning.SPARSE_EVIDENCE)
    if not rows:
        warnings.update({ForecastWarning.INSUFFICIENT_SUPPORT, ForecastWarning.PRIOR_ONLY})
    if unsupported or prior.tier != "exact_context":
        warnings.add(ForecastWarning.MISSING_CONTEXT)
    ordered = tuple(sorted(warnings, key=lambda item: item.value))
    if not rows or unsupported or prior.tier == "population":
        return ReviewClassification.RED, ordered
    if warnings:
        return ReviewClassification.AMBER, ordered
    return ReviewClassification.GREEN, ordered


def _trace(
    packet: FormulaInputPacket,
    manifest: FormulaManifest,
    prior: _PriorSelection,
    rows: tuple[_Row, ...],
    initial: Decimal,
    mad: Decimal,
    scale: Decimal,
    iterations: tuple[_Iteration, ...],
    center: Decimal,
    log_scale: Decimal,
    neff: Decimal,
    components: tuple[Decimal, Decimal, Decimal, Decimal],
    distribution: PositiveTimeDistribution,
) -> tuple[ArithmeticTraceRow, ...]:
    trace = [
        _trace_row(
            "manifest",
            "frozen Formula bootstrap",
            1,
            "bundle",
            manifest_digest=manifest.digest,
            input_digest=packet.digest,
            governor_receipt_digest=packet.governor_receipt.receipt_digest,
            huber_tuning=manifest.huber_tuning,
            max_iterations=manifest.irls_max_iterations,
            tolerance=manifest.irls_tolerance,
        )
    ]
    prior_log = _ln(prior.median_seconds)
    trace.append(
        _trace_row(
            "prior_selection",
            "causal target-context prior lookup",
            prior.median_seconds,
            "seconds",
            prior_tier=prior.tier,
            prior_key=prior.key,
            prior_log_variance=prior.log_variance,
            pseudo_strength=prior.pseudo_count,
            lineage_digest=prior.lineage_digest,
        )
    )
    for number in range(1, prior.pseudo_count + 1):
        trace.append(
            _trace_row(
                "prior",
                f"{prior.tier} pseudo-observation {number}",
                prior_log,
                "log_seconds",
                pseudo_weight=1,
                prior_log_variance=prior.log_variance,
                prior_tier=prior.tier,
                lineage_digest=prior.lineage_digest,
            )
        )
    for row in rows:
        trace.append(
            _trace_row(
                "observation",
                f"observation {row.sequence}",
                row.log_seconds or 0,
                "log_seconds",
                observation_sequence=row.sequence,
                admitted=str(row.admitted).lower(),
                raw_time_ms=row.raw_ms or 0,
                transformed_time_ms=0
                if row.log_seconds is None
                else _round_ms(_exp(row.log_seconds) * 1000),
                context_factor=row.context_factor,
                diameter_similarity=row.diameter_similarity,
                recency_factor=row.recency,
                quality_factor=row.quality,
                tournament_factor=row.tournament,
                conversion_variance=row.conversion_variance,
                total_weight=row.weight,
                conversion_status=row.conversion_status,
                event_factor=row.event_factor,
                size_factor=row.size_factor,
                material_factor=row.material_factor,
                weight_formula="context*recency*quality*tournament/(1+conversion_variance)",
            )
        )
    trace.append(
        _trace_row(
            "irls_initialization",
            "weighted median and MAD",
            initial,
            "log_seconds",
            weighted_mad=mad,
            mad_consistency=manifest.mad_consistency,
            scale=scale,
        )
    )
    for item in iterations:
        trace.append(
            _trace_row(
                "irls_iteration",
                f"IRLS iteration {item.number}",
                item.end,
                "log_seconds",
                iteration=item.number,
                center_start=item.start,
                center_end=item.end,
                delta=item.delta,
                scale=scale,
                effective_weight=item.effective_weight,
            )
        )
    residual, conversion, prior, scarcity = components
    trace.append(
        _trace_row(
            "center",
            "final robust log center",
            center,
            "log_seconds",
            effective_sample_size=neff,
            personal_weight=sum((row.weight for row in rows), _ZERO),
        )
    )
    trace.append(
        _trace_row(
            "scale",
            "lognormal predictive scale",
            log_scale,
            "log_sigma",
            robust_residual_variance=residual,
            weighted_conversion_variance=conversion,
            prior_variance=prior,
            scarcity_inflation=scarcity,
        )
    )
    for point in distribution.quantiles:
        trace.append(
            _trace_row(
                "distribution",
                f"quantile {point.probability}",
                point.time_ms,
                "ms",
                probability=point.probability,
            )
        )
    input_bytes, manifest_bytes = (
        canonical_bytes(packet.to_dict()),
        canonical_bytes(manifest.to_dict()),
    )
    trace.append(
        _trace_row(
            "canonical_bytes",
            "sealed Formula canonical bytes",
            len(input_bytes),
            "bytes",
            canonical_hex=input_bytes.hex(),
            manifest_canonical_hex=manifest_bytes.hex(),
        )
    )
    return tuple(trace)


def _trace_row(stage: str, label: str, value: Any, unit: str, **terms: Any) -> ArithmeticTraceRow:
    return ArithmeticTraceRow(
        stage,
        label,
        canonical_decimal_string(value),
        unit,
        tuple(sorted((key, _render(item)) for key, item in terms.items())),
    )


def _render(value: Any) -> str:
    return (
        canonical_decimal_string(value)
        if isinstance(value, (Decimal, int, float)) and not isinstance(value, bool)
        else str(value)
    )


def _density(context: TargetContext) -> Decimal | None:
    matches = [
        item for item in context.properties if item.code == "density" and item.unit == "kg_m3"
    ]
    return None if not matches or matches[0].value is None else Decimal(matches[0].value)


def _power(base: Decimal, exponent: Decimal) -> Decimal:
    return _exp(_ln(base) * exponent)


def _ln(value: Decimal) -> Decimal:
    return value.ln()


def _exp(value: Decimal) -> Decimal:
    return value.exp()


def _sqrt(value: Decimal) -> Decimal:
    return value.sqrt()


def _round_ms(value: Decimal) -> int:
    return int(value.quantize(_ONE, rounding=ROUND_HALF_EVEN))


def _validate_prior_values(
    size_mm: object,
    median_seconds: object,
    log_variance: object,
    pseudo_count: object,
    lineage_digest: object,
    label: str,
) -> None:
    if isinstance(size_mm, bool) or not isinstance(size_mm, int) or size_mm <= 0:
        raise ValueError(f"{label} size must be a positive integer")
    for value, field in ((median_seconds, "median"), (log_variance, "variance")):
        if (
            not isinstance(value, str)
            or canonical_decimal_string(value) != value
            or Decimal(value) <= 0
        ):
            raise ValueError(f"{label} {field} must be a positive canonical decimal")
    if isinstance(pseudo_count, bool) or not isinstance(pseudo_count, int) or pseudo_count <= 0:
        raise ValueError(f"{label} pseudo strength must be a positive integer")
    _require_digest(lineage_digest, f"{label} lineage digest")


def _closed_mapping(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"formula manifest {label} fields are not closed")
    return value


def _mapping_table(value: Any, label: str) -> tuple[tuple[str, str], ...]:
    if (
        not isinstance(value, Mapping)
        or not value
        or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items())
    ):
        raise ValueError(f"formula manifest {label} must be a nonempty string object")
    return tuple(sorted(value.items()))


def _validate_table(value: Any, label: str) -> None:
    if (
        not isinstance(value, tuple)
        or not value
        or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(part, str) and part for part in item)
            for item in value
        )
    ):
        raise ValueError(f"formula manifest {label} must be nonempty immutable string pairs")
    keys = tuple(key for key, _ in value)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise ValueError(f"formula manifest {label} keys must be unique and sorted")


__all__ = [
    "FORMULA_MANIFEST_SCHEMA",
    "ContextPrior",
    "DisciplinePrior",
    "FormulaManifest",
    "FormulaZeroHistoryPrior",
    "assess_formula",
    "resolve_zero_history_prior",
]
