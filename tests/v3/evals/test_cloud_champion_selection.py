from __future__ import annotations

from dataclasses import replace

import pytest

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.factory.candidates import (
    REQUIRED_COMPONENT_ROLES,
    CandidateBuilder,
    CandidateBundle,
    FactoryRole,
    RoleSnapshot,
)
from strathmark.v3.factory.evaluator import (
    AuditGenerationRegistry,
    EvaluationError,
    EvaluationGate,
    FrozenEvaluationHarness,
    FrozenEvaluator,
    verify_cloud_champion_selection,
    verify_evaluation_report,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
)

NOW = "2026-08-25T08:00:00.000Z"
AUDIT_DIGEST = canonical_digest({"role": "locked-cloud-comparison"})
CLOUD_MODELS = (
    "anthropic:claude-sonnet-5@2026-08-01",
    "google:gemini-3.7-flash@2026-08-01",
    "openai:gpt-5.6-terra@2026-08-01",
)


def _candidates() -> tuple[CandidateBundle, ...]:
    builder = CandidateBuilder(
        allowed_local_models=(
            "ollama:ministral-3-8b@sha256:2222",
            "ollama:qwen3.5-9b@sha256:1111",
        ),
        allowed_cloud_models=CLOUD_MODELS,
    )
    roles = tuple(
        RoleSnapshot(role, "cloud-comparison-1", canonical_digest({"role": role.value}))
        for role in (FactoryRole.TRAIN, FactoryRole.TUNE, FactoryRole.CALIBRATION)
    )
    components = {role: canonical_digest({"component": role}) for role in REQUIRED_COMPONENT_ROLES}
    common = {
        "code_revision": "git:cloud-selection",
        "code_digest": canonical_digest({"code": "cloud-selection"}),
        "dependency_lock_digest": canonical_digest({"lock": "cloud-selection"}),
        "data_snapshot_digest": canonical_digest({"data": "candidate-only"}),
        "role_snapshots": roles,
        "component_digests": components,
        "artifact_payloads": {"llm/prompts.json": b'{"schema_version":"prompt-v1"}'},
        "local_model_ids": (
            "ollama:ministral-3-8b@sha256:2222",
            "ollama:qwen3.5-9b@sha256:1111",
        ),
        "compatibility_contract_digest": canonical_digest({"contract": "v3"}),
        "rollback_parent_digest": "0" * 64,
    }
    return tuple(
        builder.build(
            display_name=model.split(":", 1)[0],
            cloud_model_ids=(model,),
            **common,
        )
        for model in CLOUD_MODELS
    )


def _harness() -> FrozenEvaluationHarness:
    return FrozenEvaluationHarness.create(
        generation_id="cloud-champion-generation-1",
        audit_snapshot_digest=AUDIT_DIGEST,
        harness_code_digest=canonical_digest({"harness": "cloud-v1"}),
        precommit_digest=canonical_digest({"precommit": "cloud-v1"}),
        gates=(
            EvaluationGate("coverage", "gte", 0.90),
            EvaluationGate("normalized_crps", "lte", 0.25),
        ),
        selection_metric="normalized_crps",
        frozen_at=NOW,
    )


def _metrics(
    candidates: tuple[CandidateBundle, ...],
    *,
    values: tuple[float, float, float] = (0.18, 0.20, 0.22),
) -> dict[str, dict[str, float]]:
    return {
        candidate.candidate_digest: {"coverage": 0.95, "normalized_crps": value}
        for candidate, value in zip(candidates, values, strict=True)
    }


def test_three_pinned_provider_candidates_share_one_harness_and_select_deterministically(
    tmp_path,
) -> None:
    candidates = _candidates()
    signer = P256EphemeralSigner.generate("integrity-key:cloud-evaluator")
    trust = IntegrityTrustStore((signer.identity,))
    harness = _harness()
    evaluator = FrozenEvaluator(
        harness,
        AuditGenerationRegistry(tmp_path / "audit-consumption"),
        signer=signer,
    )
    metrics = _metrics(candidates, values=(0.01, 0.20, 0.22))
    metrics[candidates[0].candidate_digest]["coverage"] = 0.89

    selection = evaluator.select_cloud_champion(
        tuple(reversed(candidates)),
        metrics=metrics,
        abstentions={candidate.candidate_digest: None for candidate in candidates},
        observed_audit_snapshot_digest=AUDIT_DIGEST,
        created_at="2026-08-25T08:01:00.000Z",
    )

    assert selection.selected_provider == "google"
    assert selection.selected_model_id == CLOUD_MODELS[1]
    assert selection.selection_metric == "normalized_crps"
    assert selection.promotion_authorized is False
    assert [outcome.provider for outcome in selection.outcomes] == [
        "anthropic",
        "google",
        "openai",
    ]
    assert {outcome.harness_digest for outcome in selection.outcomes} == {harness.harness_digest}
    assert selection.selected_report is not None
    assert selection.selected_report.candidate_digest == selection.selected_candidate_digest
    verify_evaluation_report(
        selection.selected_report,
        trust_store=trust,
        expected_candidate=candidates[1],
        expected_harness=harness,
    )
    assert (
        verify_cloud_champion_selection(
            selection,
            trust_store=trust,
            expected_candidates=candidates,
            expected_harness=harness,
        )
        == selection
    )

    consumption = evaluator.registry.record(harness.generation_id)
    assert consumption is not None
    assert consumption["candidate_digests"] == sorted(
        candidate.candidate_digest for candidate in candidates
    )
    assert consumption["comparison_kind"] == "cloud_champion"


def test_abstention_is_signed_and_ties_break_by_exact_model_identity(tmp_path) -> None:
    candidates = _candidates()
    signer = P256EphemeralSigner.generate("integrity-key:cloud-abstention")
    metrics = _metrics(candidates, values=(0.19, 0.19, 0.01))
    metrics[candidates[2].candidate_digest] = None  # type: ignore[assignment]
    selection = FrozenEvaluator(
        _harness(),
        AuditGenerationRegistry(tmp_path / "audit-consumption"),
        signer=signer,
    ).select_cloud_champion(
        candidates,
        metrics=metrics,
        abstentions={
            candidates[0].candidate_digest: None,
            candidates[1].candidate_digest: None,
            candidates[2].candidate_digest: "provider_unavailable",
        },
        observed_audit_snapshot_digest=AUDIT_DIGEST,
        created_at="2026-08-25T08:02:00.000Z",
    )

    assert selection.selected_model_id == CLOUD_MODELS[0]
    openai = next(outcome for outcome in selection.outcomes if outcome.provider == "openai")
    assert openai.abstention_reason == "provider_unavailable"
    assert openai.metrics is None
    assert openai.report_digest is None
    assert openai.passed is False
    assert "provider_unavailable" in selection.manifest.body_json


def test_cloud_selection_fails_closed_on_incomparable_or_reused_evidence(tmp_path) -> None:
    candidates = _candidates()
    signer = P256EphemeralSigner.generate("integrity-key:cloud-guards")
    evaluator = FrozenEvaluator(
        _harness(),
        AuditGenerationRegistry(tmp_path / "audit-consumption"),
        signer=signer,
    )
    abstentions = {candidate.candidate_digest: None for candidate in candidates}

    with pytest.raises(EvaluationError, match="exactly GPT, Claude, and Gemini"):
        evaluator.select_cloud_champion(
            candidates[:2],
            metrics={
                key: value
                for key, value in _metrics(candidates).items()
                if key != candidates[2].candidate_digest
            },
            abstentions={
                key: value
                for key, value in abstentions.items()
                if key != candidates[2].candidate_digest
            },
            observed_audit_snapshot_digest=AUDIT_DIGEST,
            created_at="2026-08-25T08:03:00.000Z",
        )

    invalid_metrics = _metrics(candidates)
    invalid_metrics[candidates[0].candidate_digest] = {"normalized_crps": 0.18}
    with pytest.raises(EvaluationError, match="exactly match frozen gates"):
        evaluator.select_cloud_champion(
            candidates,
            metrics=invalid_metrics,
            abstentions=abstentions,
            observed_audit_snapshot_digest=AUDIT_DIGEST,
            created_at="2026-08-25T08:04:00.000Z",
        )

    selection = evaluator.select_cloud_champion(
        candidates,
        metrics=_metrics(candidates),
        abstentions=abstentions,
        observed_audit_snapshot_digest=AUDIT_DIGEST,
        created_at="2026-08-25T08:05:00.000Z",
    )
    with pytest.raises(EvaluationError, match="consumed"):
        evaluator.select_cloud_champion(
            candidates,
            metrics=_metrics(candidates),
            abstentions=abstentions,
            observed_audit_snapshot_digest=AUDIT_DIGEST,
            created_at="2026-08-25T08:06:00.000Z",
        )

    trust = IntegrityTrustStore((signer.identity,))
    with pytest.raises(EvaluationError, match="selection"):
        verify_cloud_champion_selection(
            replace(selection, selected_model_id=CLOUD_MODELS[1]),
            trust_store=trust,
            expected_candidates=candidates,
            expected_harness=_harness(),
        )


def test_cloud_selection_requires_precommitted_metric_and_never_invents_a_default(
    tmp_path,
) -> None:
    candidates = _candidates()
    signer = P256EphemeralSigner.generate("integrity-key:cloud-no-default")
    without_selection_policy = FrozenEvaluationHarness.create(
        generation_id="cloud-no-policy",
        audit_snapshot_digest=AUDIT_DIGEST,
        harness_code_digest=canonical_digest({"harness": "no-policy"}),
        precommit_digest=canonical_digest({"precommit": "no-policy"}),
        gates=(EvaluationGate("coverage", "gte", 0.90),),
        frozen_at=NOW,
    )
    with pytest.raises(EvaluationError, match="selection metric"):
        FrozenEvaluator(
            without_selection_policy,
            AuditGenerationRegistry(tmp_path / "no-policy"),
            signer=signer,
        ).select_cloud_champion(
            candidates,
            metrics={candidate.candidate_digest: None for candidate in candidates},
            abstentions={
                candidate.candidate_digest: "provider_unavailable" for candidate in candidates
            },
            observed_audit_snapshot_digest=AUDIT_DIGEST,
            created_at="2026-08-25T08:07:00.000Z",
        )

    selection = FrozenEvaluator(
        _harness(),
        AuditGenerationRegistry(tmp_path / "all-abstain"),
        signer=signer,
    ).select_cloud_champion(
        candidates,
        metrics={candidate.candidate_digest: None for candidate in candidates},
        abstentions={
            candidate.candidate_digest: "provider_unavailable" for candidate in candidates
        },
        observed_audit_snapshot_digest=AUDIT_DIGEST,
        created_at="2026-08-25T08:08:00.000Z",
    )
    assert selection.selected_candidate_digest is None
    assert selection.selected_provider is None
    assert selection.selected_model_id is None
    assert selection.selected_report is None
    assert selection.promotion_authorized is False
