"""Causal credibility scoring, coverage accounting, and bounded outer weights.

The module is pure and deterministic.  It deliberately keeps predictive loss,
coverage health, and handicap-consequence guardrails as separate persisted facts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, localcontext
from enum import Enum
from math import exp
from typing import Any, Iterable, Mapping, cast

from strathmark.v3.contracts.canonical import canonical_decimal_string, canonical_digest
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.forecasts import AssessorKind, PositiveTimeDistribution
from strathmark.v3.contracts.identifiers import require_identifier

_OUTER_ASSESSORS = (
    AssessorKind.FORMULA,
    AssessorKind.ML,
    AssessorKind.LLM_COUNCIL,
)
_PRECISION = 50


class ScoreScope(str, Enum):
    OPERATIONAL = "operational"
    CANDIDATE = "candidate"


class OpportunityOutcome(str, Enum):
    SUCCESSFUL = "successful"
    PRINCIPLED_ABSTENTION = "principled_abstention"
    SCHEMA_INVALID = "schema_invalid"
    TRANSPORT_FAILURE = "transport_failure"
    RUNTIME_FAILURE = "runtime_failure"
    DEADLINE_MISS = "deadline_miss"
    UNAVAILABLE = "unavailable"
    INELIGIBLE = "ineligible"


class ConsequenceStatus(str, Enum):
    VERIFIED = "verified"
    PENDING = "pending"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True, slots=True, order=True)
class ContextNode:
    event_code: str | None = None
    size_band: str | None = None
    material_group: str | None = None
    history_depth: str | None = None

    def __post_init__(self) -> None:
        values = (
            self.event_code,
            self.size_band,
            self.material_group,
            self.history_depth,
        )
        seen_none = False
        for value in values:
            if value is None:
                seen_none = True
            elif not isinstance(value, str) or not value:
                raise ValueError("context dimensions must be nonempty strings or null")
            elif seen_none:
                raise ValueError("context hierarchy cannot skip a parent dimension")

    @property
    def parent(self) -> ContextNode | None:
        values = [
            self.event_code,
            self.size_band,
            self.material_group,
            self.history_depth,
        ]
        for index in range(3, -1, -1):
            if values[index] is not None:
                values[index] = None
                return ContextNode(*values)
        return None

    def contains(self, other: ContextNode) -> bool:
        return all(
            expected is None or expected == observed
            for expected, observed in zip(self.to_tuple(), other.to_tuple())
        )

    def to_tuple(self) -> tuple[str | None, ...]:
        return self.event_code, self.size_band, self.material_group, self.history_depth

    def to_dict(self) -> dict[str, str | None]:
        return dict(
            zip(
                ("event_code", "size_band", "material_group", "history_depth"),
                self.to_tuple(),
            )
        )


@dataclass(frozen=True, slots=True)
class PredictiveMetrics:
    crps_ms: str
    normalized_crps: str
    median_absolute_error_ms: int
    median_bias_ms: int
    tail_loss_ms: int
    central_interval_covered: bool
    sharpness_ms: int
    calibration_residual: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.crps_ms, "crps_ms"),
            (self.normalized_crps, "normalized_crps"),
        ):
            _nonnegative_decimal(value, label)
        _decimal(self.calibration_residual, "calibration_residual")
        for value, label in (
            (self.median_absolute_error_ms, "median_absolute_error_ms"),
            (self.tail_loss_ms, "tail_loss_ms"),
            (self.sharpness_ms, "sharpness_ms"),
        ):
            _nonnegative_int(value, label)
        if isinstance(self.median_bias_ms, bool) or not isinstance(self.median_bias_ms, int):
            raise ValueError("median_bias_ms must be an integer")
        if not isinstance(self.central_interval_covered, bool):
            raise ValueError("central interval coverage must be explicit")

    def to_dict(self) -> dict[str, object]:
        return {
            "crps_ms": self.crps_ms,
            "normalized_crps": self.normalized_crps,
            "median_absolute_error_ms": self.median_absolute_error_ms,
            "median_bias_ms": self.median_bias_ms,
            "tail_loss_ms": self.tail_loss_ms,
            "central_interval_covered": self.central_interval_covered,
            "sharpness_ms": self.sharpness_ms,
            "calibration_residual": self.calibration_residual,
        }


@dataclass(frozen=True, slots=True)
class HandicapConsequenceMetrics:
    spread_ms: int
    win_probability_distortion: str
    class_context_bias_ms: int
    gap_error_ms: int
    breakout_exposure: str
    optimizer_repair: bool

    def __post_init__(self) -> None:
        _nonnegative_int(self.spread_ms, "spread_ms")
        _probability(self.win_probability_distortion, "win_probability_distortion")
        if isinstance(self.class_context_bias_ms, bool) or not isinstance(
            self.class_context_bias_ms, int
        ):
            raise ValueError("class_context_bias_ms must be an integer")
        _nonnegative_int(self.gap_error_ms, "gap_error_ms")
        _probability(self.breakout_exposure, "breakout_exposure")
        if not isinstance(self.optimizer_repair, bool):
            raise ValueError("optimizer_repair must be explicit")

    def to_dict(self) -> dict[str, object]:
        return {
            "spread_ms": self.spread_ms,
            "win_probability_distortion": self.win_probability_distortion,
            "class_context_bias_ms": self.class_context_bias_ms,
            "gap_error_ms": self.gap_error_ms,
            "breakout_exposure": self.breakout_exposure,
            "optimizer_repair": self.optimizer_repair,
        }


@dataclass(frozen=True, slots=True)
class OptimizerConsequenceReceipt:
    evaluator_port: str
    forecast_digest: str
    result_revision_digest: str
    field_receipt_digest: str
    scoring_input_digest: str
    optimizer_bundle_digest: str
    status: ConsequenceStatus
    metrics: HandicapConsequenceMetrics | None
    authority_manifest_digest: str | None
    receipt_digest: str

    def __post_init__(self) -> None:
        if self.evaluator_port != "shared_optimizer_evaluator_v1":
            raise ValueError("consequence receipt requires the shared optimizer evaluator port")
        for value, label in (
            (self.forecast_digest, "forecast_digest"),
            (self.result_revision_digest, "result_revision_digest"),
            (self.field_receipt_digest, "field_receipt_digest"),
            (self.scoring_input_digest, "scoring_input_digest"),
            (self.optimizer_bundle_digest, "optimizer_bundle_digest"),
            (self.receipt_digest, "receipt_digest"),
        ):
            _digest(value, label)
        if not isinstance(self.status, ConsequenceStatus):
            raise ValueError("consequence status must use the closed vocabulary")
        if self.status is ConsequenceStatus.PENDING:
            if self.metrics is not None or self.authority_manifest_digest is not None:
                raise ValueError("pending consequence cannot claim metrics or authority")
        else:
            if not isinstance(self.metrics, HandicapConsequenceMetrics):
                raise ValueError("completed consequence metrics must be typed")
            if self.status is ConsequenceStatus.VERIFIED:
                _digest(self.authority_manifest_digest, "optimizer authority manifest digest")
            elif self.authority_manifest_digest is not None:
                raise ValueError("diagnostic consequence cannot claim operational authority")
        if self.receipt_digest != canonical_digest(self._content_value()):
            raise ValueError("consequence receipt digest mismatch")

    @classmethod
    def create(cls, **arguments: Any) -> OptimizerConsequenceReceipt:
        arguments = {
            "evaluator_port": "shared_optimizer_evaluator_v1",
            "status": ConsequenceStatus.DIAGNOSTIC,
            "authority_manifest_digest": None,
            **arguments,
        }
        content = cls._content_from_arguments(**arguments)
        return cls(**arguments, receipt_digest=canonical_digest(content))

    @classmethod
    def pending(cls, **arguments: Any) -> OptimizerConsequenceReceipt:
        return cls.create(
            **arguments,
            status=ConsequenceStatus.PENDING,
            metrics=None,
            authority_manifest_digest=None,
        )

    @classmethod
    def verified(cls, **arguments: Any) -> OptimizerConsequenceReceipt:
        return cls.create(**arguments, status=ConsequenceStatus.VERIFIED)

    @staticmethod
    def _content_from_arguments(**arguments: Any) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-optimizer-consequence-v1",
            "evaluator_port": arguments["evaluator_port"],
            "forecast_digest": arguments["forecast_digest"],
            "result_revision_digest": arguments["result_revision_digest"],
            "field_receipt_digest": arguments["field_receipt_digest"],
            "scoring_input_digest": arguments["scoring_input_digest"],
            "optimizer_bundle_digest": arguments["optimizer_bundle_digest"],
            "status": arguments["status"].value,
            "metrics": (None if arguments["metrics"] is None else arguments["metrics"].to_dict()),
            "authority_manifest_digest": arguments["authority_manifest_digest"],
        }

    def _content_value(self) -> dict[str, object]:
        return self._content_from_arguments(
            evaluator_port=self.evaluator_port,
            forecast_digest=self.forecast_digest,
            result_revision_digest=self.result_revision_digest,
            field_receipt_digest=self.field_receipt_digest,
            scoring_input_digest=self.scoring_input_digest,
            optimizer_bundle_digest=self.optimizer_bundle_digest,
            status=self.status,
            metrics=self.metrics,
            authority_manifest_digest=self.authority_manifest_digest,
        )

    def to_dict(self) -> dict[str, object]:
        return {**self._content_value(), "receipt_digest": self.receipt_digest}


@dataclass(frozen=True, slots=True)
class Opportunity:
    opportunity_id: str
    scope: ScoreScope
    assessor: AssessorKind
    forecast_digest: str
    result_id: str
    result_revision: int
    source_sequence: int
    context: ContextNode
    eligible_at_forecast: bool
    outcome: OpportunityOutcome
    difficulty: str
    event_digest: str
    member_id: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.opportunity_id, expected_namespace="opportunity")
        _common_score_fields(self)
        if not isinstance(self.eligible_at_forecast, bool):
            raise ValueError("forecast eligibility must be explicit")
        if self.assessor is AssessorKind.LLM_MEMBER:
            if not isinstance(self.member_id, str) or not self.member_id.strip():
                raise ValueError("LLM member opportunity requires member identity")
        elif self.member_id is not None:
            raise ValueError("only an LLM member opportunity may carry member identity")
        if not isinstance(self.outcome, OpportunityOutcome):
            raise ValueError("opportunity outcome must use the closed vocabulary")
        _nonnegative_decimal(self.difficulty, "difficulty")
        if not self.eligible_at_forecast and self.outcome is not OpportunityOutcome.INELIGIBLE:
            raise ValueError("ineligible forecasts require the ineligible outcome")
        if self.eligible_at_forecast and self.outcome is OpportunityOutcome.INELIGIBLE:
            raise ValueError("eligible forecasts cannot use the ineligible outcome")
        _digest(self.event_digest, "event_digest")
        if self.event_digest != canonical_digest(self._content_value()):
            raise ValueError("opportunity event digest mismatch")

    @classmethod
    def create(cls, **arguments: Any) -> Opportunity:
        content = _opportunity_content(**arguments)
        return cls(**arguments, event_digest=canonical_digest(content))

    def _content_value(self) -> dict[str, object]:
        return _opportunity_content(**{name: getattr(self, name) for name in _OPPORTUNITY_FIELDS})

    def to_dict(self) -> dict[str, object]:
        return {**self._content_value(), "event_digest": self.event_digest}


_OPPORTUNITY_FIELDS = (
    "opportunity_id",
    "scope",
    "assessor",
    "forecast_digest",
    "result_id",
    "result_revision",
    "source_sequence",
    "context",
    "eligible_at_forecast",
    "outcome",
    "difficulty",
    "member_id",
)


def _opportunity_content(**value: Any) -> dict[str, object]:
    return {
        "schema_version": "strathmark-v3-coverage-opportunity-v1",
        "opportunity_id": value["opportunity_id"],
        "scope": value["scope"].value,
        "assessor": value["assessor"].value,
        "forecast_digest": value["forecast_digest"],
        "result_id": value["result_id"],
        "result_revision": value["result_revision"],
        "source_sequence": value["source_sequence"],
        "context": value["context"].to_dict(),
        "eligible_at_forecast": value["eligible_at_forecast"],
        "outcome": value["outcome"].value,
        "difficulty": value["difficulty"],
        "member_id": value.get("member_id"),
    }


@dataclass(frozen=True, slots=True)
class PredictiveScore:
    score_id: str
    scope: ScoreScope
    assessor: AssessorKind
    forecast_digest: str
    result_id: str
    result_revision: int
    source_sequence: int
    context: ContextNode
    evidence_weight: str
    metrics: PredictiveMetrics
    consequence: OptimizerConsequenceReceipt
    settled_at_utc: str
    event_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.score_id, expected_namespace="score")
        _common_score_fields(self)
        _nonnegative_decimal(self.evidence_weight, "evidence_weight")
        if Decimal(self.evidence_weight) == 0:
            raise ValueError("score evidence weight must be positive")
        if not isinstance(self.metrics, PredictiveMetrics):
            raise ValueError("predictive metrics must be typed")
        if not isinstance(self.consequence, OptimizerConsequenceReceipt):
            raise ValueError("consequence receipt is mandatory")
        if self.consequence.forecast_digest != self.forecast_digest:
            raise ValueError("consequence receipt forecast binding differs")
        if (
            self.scope is ScoreScope.OPERATIONAL
            and self.consequence.status is ConsequenceStatus.DIAGNOSTIC
        ):
            raise ValueError("diagnostic consequence cannot alter operational credibility")
        require_utc_milliseconds(self.settled_at_utc)
        _digest(self.event_digest, "event_digest")
        if self.event_digest != canonical_digest(self._content_value()):
            raise ValueError("score event digest mismatch")

    @classmethod
    def create(cls, **arguments: Any) -> PredictiveScore:
        content = _score_content(**arguments)
        return cls(**arguments, event_digest=canonical_digest(content))

    def _content_value(self) -> dict[str, object]:
        names = (
            "score_id",
            "scope",
            "assessor",
            "forecast_digest",
            "result_id",
            "result_revision",
            "source_sequence",
            "context",
            "evidence_weight",
            "metrics",
            "consequence",
            "settled_at_utc",
        )
        return _score_content(**{name: getattr(self, name) for name in names})

    def to_dict(self) -> dict[str, object]:
        return {**self._content_value(), "event_digest": self.event_digest}


def _score_content(**value: Any) -> dict[str, object]:
    return {
        "schema_version": "strathmark-v3-predictive-score-v1",
        "score_id": value["score_id"],
        "scope": value["scope"].value,
        "assessor": value["assessor"].value,
        "forecast_digest": value["forecast_digest"],
        "result_id": value["result_id"],
        "result_revision": value["result_revision"],
        "source_sequence": value["source_sequence"],
        "context": value["context"].to_dict(),
        "evidence_weight": value["evidence_weight"],
        "metrics": value["metrics"].to_dict(),
        "consequence": value["consequence"].to_dict(),
        "settled_at_utc": value["settled_at_utc"],
    }


@dataclass(frozen=True, slots=True)
class LedgerReversal:
    reversal_id: str
    target_kind: str
    target_id: str
    original_result_revision: int
    replacement_result_revision: int
    source_sequence: int
    event_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.reversal_id, expected_namespace="reversal")
        if self.target_kind not in {"opportunity", "score"}:
            raise ValueError("reversal target kind is closed")
        require_identifier(self.target_id, expected_namespace=self.target_kind)
        for value in (
            self.original_result_revision,
            self.replacement_result_revision,
            self.source_sequence,
        ):
            _positive_int(value, "reversal sequence/revision")
        if self.replacement_result_revision <= self.original_result_revision:
            raise ValueError("replacement revision must advance")
        if self.event_digest != canonical_digest(self._content_value()):
            raise ValueError("reversal event digest mismatch")

    @classmethod
    def create(cls, **arguments: Any) -> LedgerReversal:
        content = {
            "schema_version": "strathmark-v3-score-reversal-v1",
            **arguments,
        }
        return cls(**arguments, event_digest=canonical_digest(content))

    def _content_value(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-score-reversal-v1",
            "reversal_id": self.reversal_id,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "original_result_revision": self.original_result_revision,
            "replacement_result_revision": self.replacement_result_revision,
            "source_sequence": self.source_sequence,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._content_value(), "event_digest": self.event_digest}


@dataclass(frozen=True, slots=True)
class CredibilityLedger:
    opportunities: tuple[Opportunity, ...] = ()
    scores: tuple[PredictiveScore, ...] = ()
    reversals: tuple[LedgerReversal, ...] = ()

    def __post_init__(self) -> None:
        if not all(isinstance(item, Opportunity) for item in self.opportunities):
            raise ValueError("opportunity ledger must be typed and immutable")
        if not all(isinstance(item, PredictiveScore) for item in self.scores):
            raise ValueError("score ledger must be typed and immutable")
        if not all(isinstance(item, LedgerReversal) for item in self.reversals):
            raise ValueError("reversal ledger must be typed and immutable")
        _unique(tuple(item.opportunity_id for item in self.opportunities), "opportunity")
        _unique(tuple(item.score_id for item in self.scores), "score")
        _unique(tuple(item.reversal_id for item in self.reversals), "reversal")

    @property
    def active_opportunities(self) -> tuple[Opportunity, ...]:
        reversed_ids = {
            item.target_id for item in self.reversals if item.target_kind == "opportunity"
        }
        return tuple(item for item in self.opportunities if item.opportunity_id not in reversed_ids)

    @property
    def active_scores(self) -> tuple[PredictiveScore, ...]:
        reversed_ids = {item.target_id for item in self.reversals if item.target_kind == "score"}
        return tuple(item for item in self.scores if item.score_id not in reversed_ids)

    @property
    def candidate_scores(self) -> tuple[PredictiveScore, ...]:
        return tuple(item for item in self.active_scores if item.scope is ScoreScope.CANDIDATE)

    @property
    def current_projection_digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": "strathmark-v3-credibility-current-projection-v1",
                "opportunities": [item.to_dict() for item in self.active_opportunities],
                "scores": [item.to_dict() for item in self.active_scores],
            }
        )

    def append_opportunity(self, item: Opportunity) -> CredibilityLedger:
        if not isinstance(item, Opportunity):
            raise ValueError("append requires a typed opportunity")
        if any(existing.opportunity_id == item.opportunity_id for existing in self.opportunities):
            raise ValueError("opportunity identity already exists")
        key = (
            item.scope,
            item.assessor,
            item.member_id,
            item.result_id,
            item.result_revision,
        )
        if any(
            (row.scope, row.assessor, row.member_id, row.result_id, row.result_revision) == key
            for row in self.active_opportunities
        ):
            raise ValueError("exactly one active outcome is allowed for an opportunity")
        return replace(self, opportunities=(*self.opportunities, item))

    def append_score(self, item: PredictiveScore) -> CredibilityLedger:
        if not isinstance(item, PredictiveScore):
            raise ValueError("append requires a typed score")
        if any(existing.score_id == item.score_id for existing in self.scores):
            raise ValueError("score identity already exists")
        matching = [
            row
            for row in self.active_opportunities
            if row.scope is item.scope
            and row.assessor is item.assessor
            and row.forecast_digest == item.forecast_digest
            and row.result_id == item.result_id
            and row.result_revision == item.result_revision
        ]
        if len(matching) != 1 or matching[0].outcome is not OpportunityOutcome.SUCCESSFUL:
            raise ValueError("a score requires exactly one successful active opportunity")
        return replace(self, scores=(*self.scores, item))

    def append_reversal(self, item: LedgerReversal) -> CredibilityLedger:
        if not isinstance(item, LedgerReversal):
            raise ValueError("append requires a typed reversal")
        active = (
            {row.opportunity_id for row in self.active_opportunities}
            if item.target_kind == "opportunity"
            else {row.score_id for row in self.active_scores}
        )
        if item.target_id not in active:
            raise ValueError("reversal target is not active")
        return replace(self, reversals=(*self.reversals, item))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-credibility-ledger-v1",
            "opportunities": [item.to_dict() for item in self.opportunities],
            "scores": [item.to_dict() for item in self.scores],
            "reversals": [item.to_dict() for item in self.reversals],
        }


@dataclass(frozen=True, slots=True)
class CredibilityPolicy:
    prior_strength: str = "12"
    temperature: str = "0.25"
    weight_floor: str = "0.05"
    weight_cap: str = "0.8"
    consequence_cap: str = "0.45"
    consequence_distortion_limit: str = "0.2"
    minimum_coverage: str = "0.9"
    learning_rate: str = "0.25"
    one_result_influence_bound: str = "0.05"
    recency_half_life_days: str = "730"
    live_ballast: str = "24"
    live_influence_cap: str = "0.25"

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            _positive_decimal(value, name)
        for name in (
            "weight_floor",
            "weight_cap",
            "consequence_cap",
            "consequence_distortion_limit",
            "minimum_coverage",
            "learning_rate",
            "one_result_influence_bound",
            "live_influence_cap",
        ):
            if Decimal(getattr(self, name)) > 1:
                raise ValueError(f"{name} cannot exceed one")
        if Decimal(self.weight_floor) * 3 >= 1 or Decimal(self.weight_cap) <= Decimal("0.333"):
            raise ValueError("outer floors/caps cannot prevent normalization")


@dataclass(frozen=True, slots=True)
class WeightComponent:
    assessor: AssessorKind
    predictive_loss: str
    shrunk_loss: str
    raw_credibility: str
    n_eff: str
    coverage_rate: str
    effective_floor: str
    effective_cap: str
    health: str


@dataclass(frozen=True, slots=True)
class WeightReceipt:
    context: ContextNode
    weights: tuple[tuple[AssessorKind, str], ...]
    components: tuple[WeightComponent, ...]
    calibration_cutoff_at_utc: str
    policy_digest: str
    receipt_digest: str


def calibrate_baseline(
    ledger: CredibilityLedger,
    context: ContextNode,
    policy: CredibilityPolicy,
    *,
    calibration_cutoff_at_utc: str,
) -> WeightReceipt:
    """Replay causal result batches so every single-result update is literally bounded."""

    if not isinstance(ledger, CredibilityLedger) or not isinstance(context, ContextNode):
        raise ValueError("baseline calibration requires a typed ledger and context")
    if not isinstance(policy, CredibilityPolicy):
        raise ValueError("baseline calibration requires a frozen policy")
    require_utc_milliseconds(calibration_cutoff_at_utc)
    cutoff = _utc_datetime(calibration_cutoff_at_utc)
    active_opportunities = tuple(
        row
        for row in ledger.active_opportunities
        if row.scope is ScoreScope.OPERATIONAL and row.assessor in _OUTER_ASSESSORS
    )
    result_order, opportunities_by_result, scores_by_result = _index_result_batches(
        active_opportunities,
        (
            row
            for row in ledger.active_scores
            if row.scope is ScoreScope.OPERATIONAL and row.assessor in _OUTER_ASSESSORS
        ),
    )
    if not result_order:
        return _cold_weight_receipt(context, policy, calibration_cutoff_at_utc)
    previous = dict(_equal_weights_decimal())
    receipt: WeightReceipt | None = None
    chains = tuple(reversed(_context_chain(context)))
    summaries = {
        (assessor, node): _StreamingStat() for assessor in _OUTER_ASSESSORS for node in chains
    }
    for key in result_order:
        for row in opportunities_by_result.get(key, ()):
            if not row.eligible_at_forecast:
                continue
            for node in chains:
                if node.contains(row.context):
                    summary = summaries[(row.assessor, node)]
                    difficulty = Decimal(row.difficulty)
                    summary.opportunity_mass += difficulty
                    if row.outcome is OpportunityOutcome.SUCCESSFUL:
                        summary.successful_mass += difficulty
        for row in scores_by_result.get(key, ()):
            settled = _utc_datetime(row.settled_at_utc)
            if settled > cutoff:
                raise ValueError("score settlement timestamp exceeds calibration cutoff")
            age_days = Decimal(str((cutoff - settled).total_seconds())) / Decimal(86_400)
            with localcontext() as decimal_context:
                decimal_context.prec = _PRECISION
                decay = Decimal(2) ** (-age_days / Decimal(policy.recency_half_life_days))
            for node in chains:
                if node.contains(row.context):
                    summary = summaries[(row.assessor, node)]
                    weight = Decimal(row.evidence_weight) * decay
                    summary.total_weight += weight
                    summary.square_weight += weight * weight
                    summary.weighted_loss += weight * Decimal(row.metrics.normalized_crps)
                    summary.consequence_breach = summary.consequence_breach or (
                        row.consequence.status is ConsequenceStatus.VERIFIED
                        and Decimal(
                            cast(
                                HandicapConsequenceMetrics, row.consequence.metrics
                            ).win_probability_distortion
                        )
                        > Decimal(policy.consequence_distortion_limit)
                    )
        target = _calibrate_streaming_target(
            context, policy, chains, summaries, calibration_cutoff_at_utc
        )
        target_values = {item: Decimal(value) for item, value in target.weights}
        caps = {
            component.assessor: (
                Decimal(component.effective_cap)
                if component.health == "consequence_breach"
                else Decimal(policy.weight_cap)
            )
            for component in target.components
        }
        with localcontext() as decimal_context:
            decimal_context.prec = _PRECISION
            lower = {
                item: max(
                    Decimal(policy.weight_floor),
                    previous[item] - Decimal(policy.one_result_influence_bound),
                )
                for item in _OUTER_ASSESSORS
            }
            bounded = (
                previous
                if target_values == previous
                else _project_bounded_simplex(
                    target_values,
                    previous,
                    lower=lower,
                    upper={
                        item: max(
                            lower[item],
                            min(
                                caps[item],
                                previous[item] + Decimal(policy.one_result_influence_bound),
                            ),
                        )
                        for item in _OUTER_ASSESSORS
                    },
                )
            )
        receipt = _weight_receipt(
            context,
            tuple((item, _ds(bounded[item])) for item in _OUTER_ASSESSORS),
            target.components,
            policy,
            calibration_cutoff_at_utc,
        )
        previous = bounded
    assert receipt is not None
    return receipt


def _index_result_batches(
    opportunities: Iterable[Opportunity], scores: Iterable[PredictiveScore]
) -> tuple[
    tuple[tuple[str, int], ...],
    dict[tuple[str, int], list[Opportunity]],
    dict[tuple[str, int], list[PredictiveScore]],
]:
    """Index each immutable ledger stream once, then order compact result keys."""

    opportunities_by_result: dict[tuple[str, int], list[Opportunity]] = {}
    scores_by_result: dict[tuple[str, int], list[PredictiveScore]] = {}
    result_sequences: dict[tuple[str, int], int] = {}
    for row in opportunities:
        key = row.result_id, row.result_revision
        opportunities_by_result.setdefault(key, []).append(row)
        result_sequences[key] = max(result_sequences.get(key, 0), row.source_sequence)
    for row in scores:
        scores_by_result.setdefault((row.result_id, row.result_revision), []).append(row)
    ordered = tuple(sorted(result_sequences, key=lambda key: (result_sequences[key], key)))
    return ordered, opportunities_by_result, scores_by_result


@dataclass(slots=True)
class _StreamingStat:
    total_weight: Decimal = Decimal(0)
    square_weight: Decimal = Decimal(0)
    weighted_loss: Decimal = Decimal(0)
    opportunity_mass: Decimal = Decimal(0)
    successful_mass: Decimal = Decimal(0)
    consequence_breach: bool = False


def _calibrate_streaming_target(
    context: ContextNode,
    policy: CredibilityPolicy,
    chains: tuple[ContextNode, ...],
    summaries: dict[tuple[AssessorKind, ContextNode], _StreamingStat],
    calibration_cutoff_at_utc: str,
) -> WeightReceipt:
    if not any(item.total_weight or item.opportunity_mass for item in summaries.values()):
        return _cold_weight_receipt(context, policy, calibration_cutoff_at_utc)
    stats: dict[AssessorKind, tuple[Decimal, Decimal, Decimal, Decimal, bool]] = {}
    for assessor in _OUTER_ASSESSORS:
        result = (Decimal(1), Decimal(1), Decimal(0), Decimal(0), False)
        for node in chains:
            summary = summaries[(assessor, node)]
            if not summary.total_weight and not summary.opportunity_mass:
                continue
            loss = (
                summary.weighted_loss / summary.total_weight if summary.total_weight else result[1]
            )
            n_eff = (
                summary.total_weight * summary.total_weight / summary.square_weight
                if summary.square_weight
                else Decimal(0)
            )
            coverage = (
                summary.successful_mass / summary.opportunity_mass
                if summary.opportunity_mass
                else Decimal(0)
            )
            shrunk = (n_eff * loss + Decimal(policy.prior_strength) * result[1]) / (
                n_eff + Decimal(policy.prior_strength)
            )
            result = loss, shrunk, n_eff, coverage, summary.consequence_breach
        stats[assessor] = result
    return _weight_receipt_from_stats(context, policy, stats, calibration_cutoff_at_utc)


def _project_bounded_simplex(
    target: dict[AssessorKind, Decimal],
    previous: dict[AssessorKind, Decimal],
    *,
    lower: dict[AssessorKind, Decimal],
    upper: dict[AssessorKind, Decimal],
) -> dict[AssessorKind, Decimal]:
    if sum(lower.values()) > 1 or sum(upper.values()) < 1:
        raise ValueError("weight movement and health caps have no feasible simplex")
    low = min(lower[item] - target[item] for item in _OUTER_ASSESSORS) - 1
    high = max(upper[item] - target[item] for item in _OUTER_ASSESSORS) + 1
    values: dict[AssessorKind, Decimal] = previous
    for _ in range(256):
        shift = (low + high) / 2
        values = {
            item: min(max(target[item] + shift, lower[item]), upper[item])
            for item in _OUTER_ASSESSORS
        }
        if sum(values.values()) < 1:
            low = shift
        else:
            high = shift
    residual = Decimal(1) - sum(values.values())
    for item in _OUTER_ASSESSORS:
        room = upper[item] - values[item] if residual > 0 else values[item] - lower[item]
        delta = min(abs(residual), room)
        values[item] += delta if residual > 0 else -delta
        residual += -delta if residual > 0 else delta
    return values


def _cold_weight_receipt(
    context: ContextNode, policy: CredibilityPolicy, calibration_cutoff_at_utc: str
) -> WeightReceipt:
    weights = _equal_weights()
    components = tuple(
        WeightComponent(
            item,
            "0",
            "0",
            "1",
            "0",
            "0",
            policy.weight_floor,
            policy.weight_cap,
            "cold",
        )
        for item in _OUTER_ASSESSORS
    )
    return _weight_receipt(context, weights, components, policy, calibration_cutoff_at_utc)


def _weight_receipt_from_stats(
    context: ContextNode,
    policy: CredibilityPolicy,
    stats: Mapping[AssessorKind, tuple[Decimal, Decimal, Decimal, Decimal, bool]],
    calibration_cutoff_at_utc: str,
) -> WeightReceipt:
    minimum_loss = min(value[1] for value in stats.values())
    bounded: dict[AssessorKind, Decimal] = {}
    components = []
    for assessor in _OUTER_ASSESSORS:
        loss, shrunk, n_eff, coverage, consequence_breach = stats[assessor]
        raw = Decimal(str(exp(float(-(shrunk - minimum_loss) / Decimal(policy.temperature)))))
        coverage_cap = Decimal(policy.weight_cap) * min(
            Decimal(1), coverage / Decimal(policy.minimum_coverage)
        )
        cap = max(Decimal(policy.weight_floor), coverage_cap)
        health = "healthy"
        if consequence_breach:
            cap = min(cap, Decimal(policy.consequence_cap))
            health = "consequence_breach"
        elif coverage < Decimal(policy.minimum_coverage):
            health = "coverage_below_minimum"
        bounded[assessor] = min(max(raw, Decimal(policy.weight_floor)), cap)
        components.append(
            WeightComponent(
                assessor,
                _ds(loss),
                _ds(shrunk),
                _ds(raw),
                _ds(n_eff),
                _ds(coverage),
                policy.weight_floor,
                _ds(cap),
                health,
            )
        )
    learned = _normalize(bounded)
    maturity = min(value[2] for value in stats.values())
    alpha = min(
        Decimal(policy.learning_rate) * maturity / (maturity + Decimal(policy.prior_strength)),
        Decimal(policy.one_result_influence_bound) * max(Decimal(1), maturity),
        Decimal(1),
    )
    equal = dict(_equal_weights_decimal())
    weights = _normalize(
        {
            assessor: equal[assessor] * (1 - alpha) + learned[assessor] * alpha
            for assessor in _OUTER_ASSESSORS
        }
    )
    return _weight_receipt(
        context,
        tuple((item, _ds(weights[item])) for item in _OUTER_ASSESSORS),
        tuple(components),
        policy,
        calibration_cutoff_at_utc,
    )


@dataclass(frozen=True, slots=True)
class DegradedWeights:
    baseline_weights: tuple[tuple[AssessorKind, str], ...]
    effective_weights: tuple[tuple[AssessorKind, str], ...]
    normalization_denominator: str
    missing_mass: str


def effective_degraded_weights(
    baseline: WeightReceipt, available: tuple[AssessorKind, ...]
) -> DegradedWeights:
    if not available or len(set(available)) != len(available):
        raise ValueError("available assessor set must be nonempty and unique")
    if any(item not in _OUTER_ASSESSORS for item in available):
        raise ValueError("degraded availability accepts outer assessors only")
    original = {item: Decimal(value) for item, value in baseline.weights}
    with localcontext() as context:
        context.prec = _PRECISION
        denominator = sum((original[item] for item in available), Decimal(0))
        effective = tuple(
            (item, _ds(original[item] / denominator))
            for item in _OUTER_ASSESSORS
            if item in available
        )
        missing_mass = Decimal(1) - denominator
    return DegradedWeights(
        baseline.weights,
        effective,
        _ds(denominator),
        _ds(missing_mass),
    )


@dataclass(frozen=True, slots=True)
class RoundWeightFreeze:
    round_id: str
    completed_round_id: str
    weights: tuple[tuple[AssessorKind, str], ...]
    influence: str


@dataclass(frozen=True, slots=True)
class LiveControlEvent:
    action: str
    reason: str
    before_digest: str
    after_digest: str


@dataclass(frozen=True, slots=True)
class LiveOverlay:
    tournament_id: str
    baseline: WeightReceipt
    current_weights: tuple[tuple[AssessorKind, str], ...]
    enabled: bool = True
    suspended: bool = False
    emergency_stopped: bool = False
    expired: bool = False
    rounds: tuple[RoundWeightFreeze, ...] = ()

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "tournament_id": self.tournament_id,
                "weights": [(a.value, v) for a, v in self.current_weights],
                "enabled": self.enabled,
                "suspended": self.suspended,
                "emergency_stopped": self.emergency_stopped,
                "expired": self.expired,
                "rounds": [row.round_id for row in self.rounds],
            }
        )


def initial_live_overlay(tournament_id: str, baseline: WeightReceipt) -> LiveOverlay:
    require_identifier(tournament_id, expected_namespace="tournament")
    return LiveOverlay(tournament_id, baseline, baseline.weights)


def freeze_live_round(
    overlay: LiveOverlay,
    *,
    round_id: str,
    completed_round_id: str,
    live_ledger: CredibilityLedger,
    context: ContextNode,
    policy: CredibilityPolicy,
    calibration_cutoff_at_utc: str,
) -> LiveOverlay:
    require_identifier(round_id, expected_namespace="round")
    require_identifier(completed_round_id, expected_namespace="round")
    if overlay.expired:
        raise ValueError("expired live overlay cannot freeze a round")
    if any(row.round_id == round_id for row in overlay.rounds):
        raise ValueError("round weights are already frozen")
    if not overlay.enabled or overlay.suspended or overlay.emergency_stopped:
        weights = overlay.baseline.weights
        influence = Decimal(0)
    else:
        live = calibrate_baseline(
            live_ledger,
            context,
            policy,
            calibration_cutoff_at_utc=calibration_cutoff_at_utc,
        )
        chain = _context_chain(context)
        supported = Decimal(
            len(
                [
                    row
                    for row in live_ledger.active_scores
                    if row.scope is ScoreScope.OPERATIONAL and row.context == context
                ]
            )
        )
        consistency = _live_consistency(live_ledger, context)
        influence = min(
            Decimal(policy.live_influence_cap),
            Decimal(policy.live_influence_cap)
            * supported
            / (supported + Decimal(policy.live_ballast))
            * consistency,
        )
        if influence == 0:
            freeze = RoundWeightFreeze(round_id, completed_round_id, overlay.baseline.weights, "0")
            return replace(
                overlay,
                current_weights=overlay.baseline.weights,
                rounds=(*overlay.rounds, freeze),
            )
        baseline_values = {item: Decimal(value) for item, value in overlay.baseline.weights}
        live_values = {item: Decimal(value) for item, value in live.weights}
        combined = {
            item: baseline_values[item] * (1 - influence) + live_values[item] * influence
            for item in _OUTER_ASSESSORS
        }
        normalized = _normalize(combined)
        weights = tuple((item, _ds(normalized[item])) for item in _OUTER_ASSESSORS)
    freeze = RoundWeightFreeze(round_id, completed_round_id, weights, _ds(influence))
    return replace(overlay, current_weights=weights, rounds=(*overlay.rounds, freeze))


def set_live_control(
    overlay: LiveOverlay, *, action: str, reason: str
) -> tuple[LiveOverlay, LiveControlEvent]:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("live controls require an explicit reason")
    if overlay.expired:
        raise ValueError("expired live overlay cannot be controlled")
    before = overlay.digest
    if action == "suspend":
        changed = replace(overlay, suspended=True)
    elif action == "emergency_stop":
        changed = replace(overlay, emergency_stopped=True, enabled=False)
    elif action == "re_enable":
        changed = replace(overlay, enabled=True, suspended=False, emergency_stopped=False)
    else:
        raise ValueError("unknown live control action")
    return changed, LiveControlEvent(action, reason.strip(), before, changed.digest)


def close_live_overlay(overlay: LiveOverlay, *, reason: str) -> LiveOverlay:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("live overlay expiry requires an explicit reason")
    return replace(
        overlay,
        enabled=False,
        suspended=False,
        emergency_stopped=False,
        expired=True,
        current_weights=overlay.baseline.weights,
    )


def compute_predictive_metrics(
    distribution: PositiveTimeDistribution,
    *,
    actual_time_ms: int,
    robust_context_scale_ms: int,
) -> PredictiveMetrics:
    if not isinstance(distribution, PositiveTimeDistribution):
        raise ValueError("predictive score requires a positive-time distribution")
    _positive_int(actual_time_ms, "actual_time_ms")
    _positive_int(robust_context_scale_ms, "robust_context_scale_ms")
    crps = _quantile_crps(distribution, Decimal(actual_time_ms))
    lower, upper = distribution.central_interval("0.1", "0.9")
    tail_lower, tail_upper = distribution.central_interval("0.05", "0.95")
    median = distribution.median_ms
    cdf = _cdf_at(distribution, actual_time_ms)
    return PredictiveMetrics(
        _ds(crps),
        _ds(crps / Decimal(robust_context_scale_ms)),
        abs(median - actual_time_ms),
        median - actual_time_ms,
        max(tail_lower - actual_time_ms, actual_time_ms - tail_upper, 0),
        lower <= actual_time_ms <= upper,
        upper - lower,
        _ds(cdf - Decimal("0.5")),
    )


def _quantile_crps(distribution: PositiveTimeDistribution, actual: Decimal) -> Decimal:
    points = [(Decimal(item.probability), Decimal(item.time_ms)) for item in distribution.quantiles]
    segments = [(Decimal(0), points[0][0], points[0][1], Decimal(0))]
    for (left_p, left_q), (right_p, right_q) in zip(points, points[1:]):
        slope = (right_q - left_q) / (right_p - left_p)
        intercept = left_q - slope * left_p
        segments.append((left_p, right_p, intercept, slope))
    segments.append((points[-1][0], Decimal(1), points[-1][1], Decimal(0)))
    total = Decimal(0)
    for left, right, intercept, slope in segments:
        cuts = [left, right]
        if slope:
            crossing = (actual - intercept) / slope
            if left < crossing < right:
                cuts.insert(1, crossing)
        for start, end in zip(cuts, cuts[1:]):
            midpoint = (start + end) / 2
            predicted = intercept + slope * midpoint
            if actual >= predicted:
                total += _integral_below(actual, intercept, slope, end) - _integral_below(
                    actual, intercept, slope, start
                )
            else:
                total += _integral_above(actual, intercept, slope, end) - _integral_above(
                    actual, intercept, slope, start
                )
    return total


def _integral_below(actual: Decimal, intercept: Decimal, slope: Decimal, p: Decimal) -> Decimal:
    return (actual - intercept) * p * p - Decimal(2) * slope * p**3 / Decimal(3)


def _integral_above(actual: Decimal, intercept: Decimal, slope: Decimal, p: Decimal) -> Decimal:
    return Decimal(2) * (
        (intercept - actual) * p
        + (slope - intercept + actual) * p * p / Decimal(2)
        - slope * p**3 / Decimal(3)
    )


def _cdf_at(distribution: PositiveTimeDistribution, actual: int) -> Decimal:
    points = [(Decimal(item.probability), item.time_ms) for item in distribution.quantiles]
    if actual < points[0][1]:
        return Decimal(0)
    if actual >= points[-1][1]:
        return Decimal(1)
    equal_probabilities = [probability for probability, time in points if time == actual]
    if equal_probabilities:
        return max(equal_probabilities)
    left_p, left_t, right_p, right_t = next(
        (left_p, left_t, right_p, right_t)
        for (left_p, left_t), (right_p, right_t) in zip(points, points[1:])
        if left_t <= actual <= right_t
    )
    return left_p + (right_p - left_p) * Decimal(actual - left_t) / Decimal(right_t - left_t)


def _weight_receipt(
    context: ContextNode,
    weights: tuple[tuple[AssessorKind, str], ...],
    components: tuple[WeightComponent, ...],
    policy: CredibilityPolicy,
    calibration_cutoff_at_utc: str,
) -> WeightReceipt:
    policy_digest = canonical_digest(
        {name: getattr(policy, name) for name in policy.__dataclass_fields__}
    )
    content = {
        "context": context.to_dict(),
        "weights": [(item.value, value) for item, value in weights],
        "components": [
            {
                name: (getattr(row, name).value if name == "assessor" else getattr(row, name))
                for name in row.__dataclass_fields__
            }
            for row in components
        ],
        "calibration_cutoff_at_utc": calibration_cutoff_at_utc,
        "policy_digest": policy_digest,
    }
    return WeightReceipt(
        context,
        weights,
        components,
        calibration_cutoff_at_utc,
        policy_digest,
        canonical_digest(content),
    )


def _context_chain(context: ContextNode) -> tuple[ContextNode, ...]:
    chain: list[ContextNode] = []
    node: ContextNode | None = context
    while node is not None:
        chain.append(node)
        node = node.parent
    return tuple(chain)


def _live_consistency(ledger: CredibilityLedger, context: ContextNode) -> Decimal:
    values = [
        Decimal(row.metrics.normalized_crps)
        for row in ledger.active_scores
        if row.scope is ScoreScope.OPERATIONAL and row.context == context
    ]
    if len(values) < 2:
        return Decimal(1) if values else Decimal(0)
    mean = sum(values, Decimal(0)) / Decimal(len(values))
    deviation = sum((abs(value - mean) for value in values), Decimal(0)) / Decimal(len(values))
    return Decimal(1) / (Decimal(1) + deviation)


def _equal_weights_decimal() -> tuple[tuple[AssessorKind, Decimal], ...]:
    with localcontext() as context:
        context.prec = _PRECISION
        third = Decimal(1) / Decimal(3)
        remainder = Decimal(1) - third - third
    return (
        (AssessorKind.FORMULA, third),
        (AssessorKind.ML, third),
        (AssessorKind.LLM_COUNCIL, remainder),
    )


def _equal_weights() -> tuple[tuple[AssessorKind, str], ...]:
    return tuple((item, _ds(value)) for item, value in _equal_weights_decimal())


def _normalize(values: Mapping[AssessorKind, Decimal]) -> dict[AssessorKind, Decimal]:
    with localcontext() as context:
        context.prec = _PRECISION
        total = sum(values.values(), Decimal(0))
        if total <= 0:
            return dict(_equal_weights_decimal())
        result: dict[AssessorKind, Decimal] = {}
        running = Decimal(0)
        for assessor in _OUTER_ASSESSORS[:-1]:
            result[assessor] = values[assessor] / total
            running += result[assessor]
        result[_OUTER_ASSESSORS[-1]] = Decimal(1) - running
    return result


def _common_score_fields(value: Opportunity | PredictiveScore) -> None:
    if not isinstance(value.scope, ScoreScope):
        raise ValueError("score scope must use the closed vocabulary")
    if not isinstance(value.assessor, AssessorKind):
        raise ValueError("assessor must use the closed vocabulary")
    _digest(value.forecast_digest, "forecast_digest")
    require_identifier(value.result_id, expected_namespace="result")
    _positive_int(value.result_revision, "result_revision")
    _positive_int(value.source_sequence, "source_sequence")
    if not isinstance(value.context, ContextNode):
        raise ValueError("score context must be typed")
    if value.scope is ScoreScope.CANDIDATE and value.assessor not in {
        AssessorKind.LLM_MEMBER,
        AssessorKind.LLM_COUNCIL,
    }:
        raise ValueError("candidate ledger accepts LLM diagnostics only")


def _ds(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = _PRECISION
        text = format(+value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _utc_datetime(value: str) -> datetime:
    require_utc_milliseconds(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _decimal(value: str, label: str) -> Decimal:
    if not isinstance(value, str) or canonical_decimal_string(value) != value:
        raise ValueError(f"{label} must be a canonical decimal string")
    return Decimal(value)


def _nonnegative_decimal(value: str, label: str) -> Decimal:
    number = _decimal(value, label)
    if number < 0:
        raise ValueError(f"{label} must be nonnegative")
    return number


def _positive_decimal(value: str, label: str) -> Decimal:
    number = _decimal(value, label)
    if number <= 0:
        raise ValueError(f"{label} must be positive")
    return number


def _probability(value: str, label: str) -> None:
    number = _nonnegative_decimal(value, label)
    if number > 1:
        raise ValueError(f"{label} cannot exceed one")


def _positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _nonnegative_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")


def _digest(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _unique(values: Iterable[str], label: str) -> None:
    material = tuple(values)
    if len(material) != len(set(material)):
        raise ValueError(f"{label} ledger identities must be unique")


__all__ = [
    "ContextNode",
    "CredibilityLedger",
    "CredibilityPolicy",
    "DegradedWeights",
    "HandicapConsequenceMetrics",
    "LedgerReversal",
    "LiveControlEvent",
    "LiveOverlay",
    "Opportunity",
    "OpportunityOutcome",
    "OptimizerConsequenceReceipt",
    "PredictiveMetrics",
    "PredictiveScore",
    "RoundWeightFreeze",
    "ScoreScope",
    "WeightComponent",
    "WeightReceipt",
    "calibrate_baseline",
    "close_live_overlay",
    "compute_predictive_metrics",
    "effective_degraded_weights",
    "freeze_live_round",
    "initial_live_overlay",
    "set_live_control",
]
