from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.evidence import TargetContext
from strathmark.v3.contracts.forecasts import PositiveTimeDistribution, QuantilePoint
from strathmark.v3.contracts.pre_field_forecasts import (
    ForecastSetSnapshot,
    PreFieldCompetitorForecast,
    PreFieldForecastReceipt,
)
from strathmark.v3.contracts.receipts import EngineAuthorityBinding
from strathmark.v3.infrastructure.integrity import IntegrityTrustStore, P256EphemeralSigner


def _context() -> TargetContext:
    return TargetContext("underhand", 300, "radiata_pine", "taxonomy:v1", "conversion:v1")


def _engine() -> EngineAuthorityBinding:
    return EngineAuthorityBinding.from_dict(
        {
            "scope_id": "tournament:show",
            "engine": "v3",
            "mode": "rehearsal",
            "selection_digest": "1" * 64,
            "consumer_contract_digest": "2" * 64,
            "source_commit": "9270e93",
        }
    )


def _snapshot() -> ForecastSetSnapshot:
    return ForecastSetSnapshot.create(
        tournament_id="tournament:show",
        round_id="round:heats",
        forecast_set_revision=1,
        ordered_competitor_ids=("competitor:alice", "competitor:bob"),
        target_context=_context(),
        historical_cutoff_key="history:before-show",
        tournament_epoch_id="epoch:heats-1",
        epoch_digest="3" * 64,
        maximum_tournament_sequence=12,
        bundle_digest="4" * 64,
        hard_deadline_at="2026-08-27T12:05:00.000Z",
        engine_authority=_engine(),
    )


def _distribution(median_ms: int) -> PositiveTimeDistribution:
    return PositiveTimeDistribution(
        (
            QuantilePoint("0.1", median_ms - 5000),
            QuantilePoint("0.5", median_ms),
            QuantilePoint("0.9", median_ms + 7000),
        )
    )


def _entry(competitor_id: str, median_ms: int) -> PreFieldCompetitorForecast:
    return PreFieldCompetitorForecast.create(
        competitor_id=competitor_id,
        distribution=_distribution(median_ms),
        basis_kind="capability_pool",
        evidence_digest="5" * 64,
        publication_digest="6" * 64,
        card_digest="7" * 64,
        capability_binding_digest="8" * 64,
        pool_receipt_digest="9" * 64,
        component_forecast_digests=("a" * 64, "b" * 64, "c" * 64),
    )


def test_pre_field_receipt_is_signed_marginal_seed_evidence_without_marks() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:pre-field")
    snapshot = _snapshot()
    receipt = PreFieldForecastReceipt.create(
        snapshot=snapshot,
        forecasts=(
            _entry("competitor:alice", 30_000),
            _entry("competitor:bob", 42_000),
        ),
        signer=signer,
        created_at="2026-08-27T12:01:00.000Z",
    )

    assert receipt.purpose == "pre_field_seeding_only"
    assert receipt.issued_mark is False
    assert [item.p50_seed_time_ms for item in receipt.forecasts] == [30_000, 42_000]
    serialized = receipt.to_dict()
    forbidden = {
        "field_id",
        "field_revision",
        "stand_ids",
        "marks",
        "placing_probability",
        "win_probability",
        "rebase",
        "optimizer",
        "disagreement_color",
    }
    assert forbidden.isdisjoint(serialized)
    assert (
        PreFieldForecastReceipt.from_dict(
            serialized, trust_store=IntegrityTrustStore((signer.identity,))
        )
        == receipt
    )


def test_pre_field_receipt_rejects_roster_order_tamper_and_non_formula_zero_history() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:pre-field-tamper")
    snapshot = _snapshot()
    with pytest.raises(ContractError, match="ordered roster"):
        PreFieldForecastReceipt.create(
            snapshot=snapshot,
            forecasts=(
                _entry("competitor:bob", 42_000),
                _entry("competitor:alice", 30_000),
            ),
            signer=signer,
            created_at="2026-08-27T12:01:00.000Z",
        )

    valid_prior = PreFieldCompetitorForecast.create(
        competitor_id="competitor:alice",
        distribution=_distribution(30_000),
        basis_kind="zero_history_formula_prior",
        evidence_digest="5" * 64,
        publication_digest="6" * 64,
        card_digest="7" * 64,
        capability_binding_digest=None,
        pool_receipt_digest=None,
        component_forecast_digests=("a" * 64,),
    )
    assert valid_prior.p50_seed_time_ms == 30_000
    assert "mark" not in valid_prior.to_dict()


def test_pre_field_receipt_rejects_signed_payload_tamper() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:pre-field-signature-tamper")
    receipt = PreFieldForecastReceipt.create(
        snapshot=_snapshot(),
        forecasts=(
            _entry("competitor:alice", 30_000),
            _entry("competitor:bob", 42_000),
        ),
        signer=signer,
        created_at="2026-08-27T12:01:00.000Z",
    )
    tampered = deepcopy(receipt.to_dict())
    tampered["forecasts"][0]["distribution"]["quantiles"][1]["time_ms"] = 30_001
    tampered["forecasts"][0]["p50_seed_time_ms"] = 30_001

    with pytest.raises(ContractError, match="authority|manifest|digest|signature"):
        PreFieldForecastReceipt.from_dict(
            tampered, trust_store=IntegrityTrustStore((signer.identity,))
        )

    entry = _entry("competitor:alice", 30_000)
    with pytest.raises(ContractError, match="zero-history"):
        replace(
            entry,
            basis_kind="zero_history_formula_prior",
            capability_binding_digest="8" * 64,
            pool_receipt_digest="9" * 64,
        )


def test_forecast_set_identity_changes_with_epoch_bundle_or_cutoff() -> None:
    original = _snapshot()
    changed_epoch = ForecastSetSnapshot.create(
        **{
            **original.creation_values(),
            "tournament_epoch_id": "epoch:heats-2",
            "epoch_digest": "d" * 64,
        }
    )
    changed_bundle = ForecastSetSnapshot.create(
        **{**original.creation_values(), "bundle_digest": "e" * 64}
    )
    changed_cutoff = ForecastSetSnapshot.create(
        **{**original.creation_values(), "historical_cutoff_key": "history:later"}
    )

    assert (
        len(
            {
                original.forecast_set_id,
                changed_epoch.forecast_set_id,
                changed_bundle.forecast_set_id,
                changed_cutoff.forecast_set_id,
            }
        )
        == 4
    )
