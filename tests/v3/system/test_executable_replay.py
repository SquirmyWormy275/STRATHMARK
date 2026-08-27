from __future__ import annotations

import pytest

from scripts.replay_v3 import ReplayEvidenceError, ReplayFaultPlan, build_replay_report


def test_whole_domain_replay_executes_every_claim_against_isolated_state() -> None:
    report = build_replay_report()

    assert report["result"] == "passed"
    assert report["race_day"]["stage_count"] == 5
    assert report["race_day"]["same_round_epochs_verified"] is True
    assert report["race_day"]["between_round_updates_verified"] is True
    assert report["race_day"]["mark_three_rebasing_verified"] is True
    assert report["race_day"]["immutable_winners_verified"] is True
    assert report["recovery"]["zero_duplicate_forecasts"] is True
    assert report["recovery"]["immutable_receipts"] is True


def test_replay_refuses_to_credit_a_named_fault_that_was_not_injected() -> None:
    with pytest.raises(ReplayEvidenceError, match="cloud_timeout was not injected"):
        build_replay_report(
            fault_plan=ReplayFaultPlan(disabled_faults=frozenset({"cloud_timeout"}))
        )


def test_replay_refuses_to_credit_receipt_immutability_when_probe_differs() -> None:
    with pytest.raises(ReplayEvidenceError, match="immutable receipt changed"):
        build_replay_report(fault_plan=ReplayFaultPlan(receipt_probe_override_after="f" * 64))
