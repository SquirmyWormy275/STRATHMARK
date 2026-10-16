"""Real V2 synthetic event operations, never operator storage or a stub engine."""

from copy import deepcopy

import pytest

from strathmark.events import EventError, EventOperations, Principal
from strathmark.events.examples import synthetic_event


@pytest.fixture
def ops(tmp_path):
    return EventOperations(tmp_path / "events.sqlite3")


def principal(show="synthetic:missoula", actor="judge"):
    return Principal(actor, frozenset({show}), "director")


def command(ops, state, action, payload=None, *, editor="cli", request_id=None):
    return ops.execute(
        principal(state["show_id"]),
        state["event_id"],
        request_id or f"r{state['revision']}-{action}",
        state["revision"],
        editor,
        action,
        payload or {},
    )


def create(ops, show="synthetic:missoula"):
    return ops.create(principal(show), "create-" + show, synthetic_event(show), "cli")


def test_real_engine_preparation_and_two_clocks(ops):
    state = create(ops)
    state = command(
        ops,
        state,
        "fields",
        {
            "fields": [
                {
                    "heat_id": "q1",
                    "entries": ["e0", "e1", "e2", "e3"],
                    "stands": ["1", "2", "3", "4"],
                },
                {
                    "heat_id": "q2",
                    "entries": ["e4", "e5", "e6", "e7"],
                    "stands": ["1", "2", "3", "4"],
                },
            ]
        },
    )
    state = command(ops, state, "calculate", {"heat_id": "q1"})
    heat = state["rounds"][0]["fields"][0]
    assert heat["sheet"]["engine"] == "v2"
    assert all(row["model_version"] for row in heat["sheet"]["rows"])
    assert min(row["start_mark_seconds"] for row in heat["sheet"]["rows"]) == 3
    state = command(
        ops, state, "review", {"heat_id": "q1", "sheet_digest": heat["sheet"]["digest"]}
    )
    state = command(
        ops,
        state,
        "issue",
        {"heat_id": "q1", "sheet_digest": heat["sheet"]["digest"], "confirm": True},
    )
    heat = state["rounds"][0]["fields"][0]
    state = command(
        ops,
        state,
        "outcomes",
        {
            "heat_id": "q1",
            "issue_id": heat["issue_id"],
            "outcomes": [
                {
                    "entry_id": entry,
                    "status": "finished",
                    "timer1_seconds": "20.10",
                    "timer2_seconds": "20.30",
                }
                for entry in heat["entries"]
            ],
        },
    )
    outcomes = state["rounds"][0]["fields"][0]["result_revisions"][-1]["outcomes"]
    assert outcomes[0]["raw_duration_seconds"] == "20.2"
    assert float(outcomes[0]["common_count_seconds"]) == 20.2 + outcomes[0]["start_mark_seconds"]
    assert "display_name" not in str(heat["sheet"])


def test_retry_conflict_editor_handover_and_scope(ops):
    state = create(ops)
    payload = {"editor": "gui"}
    handed = command(ops, state, "handover", payload, request_id="handoff")
    assert command(ops, state, "handover", payload, request_id="handoff") == handed
    with pytest.raises(EventError, match="request_conflict"):
        command(ops, state, "handover", {"editor": "other"}, request_id="handoff")
    with pytest.raises(EventError, match="editor"):
        command(ops, handed, "forecast")
    with pytest.raises(EventError, match="forbidden"):
        ops.read(principal("synthetic:other"), state["event_id"])
    assert ops.read(principal(), state["event_id"])["revision"] == handed["revision"]


def test_identity_history_and_quality_fail_closed(ops):
    config = synthetic_event()
    config["roster"][1]["engine_id"] = config["roster"][0]["engine_id"]
    with pytest.raises(EventError, match="identity"):
        ops.create(principal(), "bad", config, "cli")
    config = synthetic_event()
    config["roster"][0]["history"][0]["result_date"] = config["prediction_as_of"]
    with pytest.raises(EventError, match="cutoff"):
        ops.create(principal(), "future", config, "cli")
    config = synthetic_event()
    config["quality_policy"] = "quality_sensitive"
    with pytest.raises(EventError, match="quality"):
        ops.create(principal(), "quality", config, "cli")


def test_second_show_and_restore_fence(ops, tmp_path):
    first = create(ops)
    second = create(ops, "synthetic:second-show")
    assert first["event_id"] != second["event_id"]
    backup = tmp_path / "backup.json"
    ops.backup(principal(), first["event_id"], backup)
    command(ops, first, "handover", {"editor": "gui"})
    with pytest.raises(EventError, match="stale"):
        ops.verify_backup(backup, first["event_id"])


def test_production_activation_is_not_a_create_flag(ops):
    config = deepcopy(synthetic_event())
    config["synthetic"] = False
    with pytest.raises(EventError, match="qualification"):
        ops.create(principal(), "live", config, "cli")


def test_children_inherit_the_root_numeric_runtime(ops, monkeypatch):
    state = create(ops)
    config = synthetic_event()
    config["event_id"] += ":second-event"
    child = ops.create(principal(), "second-event", config, "cli")
    assert child["root_selection"] == state["root_selection"]
    from strathmark.events import numerics

    monkeypatch.setattr(numerics, "numeric_identity", lambda: "changed-runtime")
    config["event_id"] += ":third-event"
    with pytest.raises(EventError, match="root_selection_immutable"):
        ops.create(principal(), "third-event", config, "cli")


def operate_round(ops, state, ordinal, *, boundary_tie=False):
    eligible = ops._eligible(state)
    capacity = state["config"]["round_rules"][ordinal]["capacity"]
    fields = [
        {
            "heat_id": f"r{ordinal}h{n // capacity}",
            "entries": eligible[n : n + capacity],
            "stands": state["config"]["stands"][: len(eligible[n : n + capacity])],
        }
        for n in range(0, len(eligible), capacity)
    ]
    state = command(ops, state, "fields", {"fields": fields})
    for spec in fields:
        state = command(ops, state, "calculate", {"heat_id": spec["heat_id"]})
        _, heat = ops._heat(state, spec["heat_id"])
        state = command(
            ops,
            state,
            "review",
            {"heat_id": heat["heat_id"], "sheet_digest": heat["sheet"]["digest"]},
        )
        state = command(
            ops,
            state,
            "issue",
            {"heat_id": heat["heat_id"], "sheet_digest": heat["sheet"]["digest"], "confirm": True},
        )
        _, heat = ops._heat(state, spec["heat_id"])
        rows = []
        for i, row in enumerate(heat["sheet"]["rows"]):
            completion = 100 + (1 if boundary_tie and i == 2 else i)
            raw = completion - row["start_mark_seconds"]
            rows.append(
                {"entry_id": row["entry_id"], "status": "scratched"}
                if ordinal == 0 and row["entry_id"] == "e7"
                else {
                    "entry_id": row["entry_id"],
                    "status": "finished",
                    "timer1_seconds": str(raw),
                    "timer2_seconds": str(raw),
                }
            )
        state = command(
            ops,
            state,
            "outcomes",
            {"heat_id": heat["heat_id"], "issue_id": heat["issue_id"], "outcomes": rows},
        )
        _, heat = ops._heat(state, spec["heat_id"])
        state = command(
            ops,
            state,
            "accept",
            {"heat_id": heat["heat_id"], "result_digest": heat["result_revisions"][-1]["digest"]},
        )
    return state


@pytest.mark.parametrize("show", ["synthetic:missoula", "synthetic:second-show"])
def test_complete_multiround_restart_correction_and_export(ops, show):
    state = create(ops, show)
    for ordinal in range(3):
        state = operate_round(ops, state, ordinal)
        ops = EventOperations(ops.store.path)
        state = ops.read(principal(show), state["event_id"])
        if ordinal < 2:
            state = command(ops, state, "advance")
    state = command(ops, state, "finalize")
    exported = ops.export(principal(show), state["event_id"])
    assert len(exported["summary"]) == 8
    assert len(exported["deliveries"]) == 14
    assert exported["summary_revision"] == 1
    heat = state["rounds"][-1]["fields"][0]
    old = heat["result_revisions"][-1]
    replacement = [{"entry_id": row["entry_id"], "status": "dnf"} for row in old["outcomes"]]
    state = command(
        ops,
        state,
        "correct",
        {
            "heat_id": heat["heat_id"],
            "issue_id": heat["issue_id"],
            "outcomes": replacement,
            "reason": "Synthetic timer correction",
            "supersedes": old["digest"],
        },
    )
    heat = state["rounds"][-1]["fields"][0]
    state = command(
        ops,
        state,
        "accept",
        {"heat_id": heat["heat_id"], "result_digest": heat["result_revisions"][-1]["digest"]},
    )
    state = command(ops, state, "finalize")
    assert state["summary_revision"] == 2
    assert len(state["exports"]) == 16
    assert state["rounds"][-1]["fields"][0]["issue_id"] == heat["issue_id"]


def test_tie_requires_authorized_decision_not_alphabetical_order(ops):
    state = operate_round(ops, create(ops), 0, boundary_tie=True)
    with pytest.raises(EventError, match="tie_requires"):
        command(ops, state, "advance")
    state = command(
        ops,
        state,
        "advance",
        {
            "tie_selections": {"r0h0": ["e0", "e2"], "r0h1": ["e4", "e6"]},
            "reason": "Judge's synthetic throwoff decision",
        },
    )
    assert state["rounds"][0]["advancing"] == ["e0", "e2", "e4", "e6"]


def test_correction_invalidates_prepared_but_never_rewrites_issued_descendants(ops):
    state = operate_round(ops, create(ops), 0)
    state = command(ops, state, "advance")
    fields = [
        {"heat_id": "s1", "entries": ["e0", "e4"], "stands": ["1", "2"]},
        {"heat_id": "s2", "entries": ["e1", "e5"], "stands": ["1", "2"]},
    ]
    state = command(ops, state, "fields", {"fields": fields})
    state = command(ops, state, "calculate", {"heat_id": "s1"})
    heat = state["rounds"][0]["fields"][0]
    result = heat["result_revisions"][-1]
    outcomes = [{"entry_id": row["entry_id"], "status": "dnf"} for row in result["outcomes"]]
    state = command(
        ops,
        state,
        "correct",
        {
            "heat_id": heat["heat_id"],
            "issue_id": heat["issue_id"],
            "outcomes": outcomes,
            "reason": "Synthetic correction",
            "supersedes": result["digest"],
        },
    )
    assert len(state["rounds"]) == 1
    assert state["invalidated"][-1]["fields"][0]["sheet"]


def test_field_changes_invalidate_and_lock_handover_does_not(ops):
    state = create(ops)
    fields = [
        {"heat_id": "q1", "entries": ["e0", "e1", "e2", "e3"], "stands": ["1", "2", "3", "4"]},
        {"heat_id": "q2", "entries": ["e4", "e5", "e6", "e7"], "stands": ["1", "2", "3", "4"]},
    ]
    state = command(ops, state, "fields", {"fields": fields})
    state = command(ops, state, "calculate", {"heat_id": "q1"})
    sheet = state["rounds"][0]["fields"][0]["sheet"]
    state = command(ops, state, "handover", {"editor": "gui"})
    assert state["rounds"][0]["fields"][0]["sheet"] == sheet
    fields[0]["stands"].reverse()
    state = command(ops, state, "fields", {"fields": fields}, editor="gui")
    assert "sheet" not in state["rounds"][0]["fields"][0]


def test_precommit_exception_rolls_back_event_and_receipt(ops, monkeypatch):
    state = create(ops)
    append = ops.store.append

    def fail(*args):
        append(*args)
        raise RuntimeError("injected before commit")

    monkeypatch.setattr(ops.store, "append", fail)
    with pytest.raises(RuntimeError, match="before commit"):
        command(ops, state, "handover", {"editor": "gui"})
    reopened = EventOperations(ops.store.path)
    assert reopened.read(principal(), state["event_id"]) == state
    with reopened.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 1


def test_fresh_restore_resumes_one_fenced_authority_with_exact_receipts(ops, tmp_path):
    state = create(ops)
    state = operate_round(ops, state, 0)
    target = tmp_path / "recovered.sqlite3"
    recovered = ops.transfer_to(
        principal(),
        state["event_id"],
        target,
        request_id="transfer",
        expected_revision=state["revision"],
        editor="cli",
    )
    assert recovered["rounds"] == state["rounds"]
    with pytest.raises(EventError, match="fenced"):
        command(ops, ops.read(principal(), state["event_id"]), "advance")
    resumed = EventOperations(target)
    advanced = command(resumed, recovered, "advance")
    assert advanced["rounds"][0]["state"] == "advanced"
    with pytest.raises(EventError, match="another_destination"):
        ops.transfer_to(
            principal(),
            state["event_id"],
            tmp_path / "second.sqlite3",
            request_id="second",
            expected_revision=state["revision"],
            editor="cli",
        )


def test_local_clock_marks_min_max_integer_and_overflow(ops, monkeypatch):
    from strathmark.events import numerics

    config = synthetic_event()
    config["roster"] = config["roster"][:2]
    from strathmark.calculator import MarkResult

    times = [200, 22.1]

    def predictions(*args, **kwargs):
        return [
            MarkResult(
                name=r["engine_id"],
                competitor_id=r["engine_id"],
                mark=3,
                predicted_time=time,
                method_used="test",
                confidence="HIGH",
                explanation="policy-only fixture",
            )
            for r, time in zip(config["roster"], times)
        ]

    monkeypatch.setattr(numerics.HandicapCalculator, "calculate", predictions)
    with pytest.raises(EventError, match="exceeds_180"):
        numerics.calculate(config, ["e0", "e1"])
    times[1] = 23.1
    rows = numerics.calculate(config, ["e0", "e1"])
    assert [row["start_mark_seconds"] for row in rows] == [3, 180]
    assert all(type(row["start_mark_seconds"]) is int for row in rows)
    times[1] = 200
    assert [row["start_mark_seconds"] for row in numerics.calculate(config, ["e0", "e1"])] == [3, 3]
    config["mark_policy"] = "v2-optimizer.v1"
    assert [r["start_mark_seconds"] for r in numerics.calculate(config, ["e0", "e1"])] == [3, 3]


def test_v2_known_zero_quality_and_unknown_quality_do_not_change_numbers():
    from strathmark.events.numerics import calculate

    config = synthetic_event()
    unknown = calculate(config, ["e0", "e1"])
    config["wood"]["quality"] = 0
    assert calculate(config, ["e0", "e1"]) == unknown


def test_copying_database_and_head_cannot_create_parallel_authority(ops, tmp_path):
    import shutil

    create(ops)
    duplicate = tmp_path / "parallel.sqlite3"
    shutil.copyfile(ops.store.path, duplicate)
    shutil.copyfile(ops.store.head, duplicate.with_suffix(".sqlite3.head.json"))
    with pytest.raises(EventError, match="authority_location_changed"):
        EventOperations(duplicate)


def test_child_cannot_change_root_mark_policy(ops):
    state = create(ops)
    config = synthetic_event()
    config["event_id"] += ":other-policy"
    config["mark_policy"] = "v2-optimizer.v1"
    with pytest.raises(EventError, match="root_selection_immutable"):
        ops.create(principal(), "other-policy", config, "cli")
    assert ops.read(principal(), state["event_id"]) == state


def corrected_issued_progression(ops, *, retain=True):
    config = synthetic_event()
    config["roster"] = config["roster"][:4]
    config["round_rules"] = [
        {"round_id": "qualifying", "capacity": 2, "qualifiers_per_heat": 1},
        {"round_id": "final", "capacity": 2, "qualifiers_per_heat": 0},
    ]
    state = ops.create(principal(), "create", config, "cli")
    state = operate_round(ops, state, 0)
    state = command(ops, state, "advance")
    state = operate_round(ops, state, 1)
    state = command(ops, state, "finalize")
    heat = state["rounds"][0]["fields"][0]
    result = heat["result_revisions"][-1]
    state = command(
        ops,
        state,
        "correct",
        {
            "heat_id": heat["heat_id"],
            "issue_id": heat["issue_id"],
            "supersedes": result["digest"],
            "reason": "Synthetic qualifier correction after final issue",
            "outcomes": [
                {"entry_id": row["entry_id"], "status": "dnf"} for row in result["outcomes"]
            ],
        },
    )
    assert state["progression_review"]
    if not retain:
        return state
    return command(
        ops,
        state,
        "retain_downstream",
        {"reason": "Director retains completed final under the show appeal decision"},
    )


def test_refinalization_requires_accepting_every_summary_revision(ops):
    state = corrected_issued_progression(ops)
    with pytest.raises(EventError, match="all_summary_results_must_be_accepted"):
        command(ops, state, "finalize")
    heat = state["rounds"][0]["fields"][0]
    state = command(
        ops,
        state,
        "accept",
        {"heat_id": heat["heat_id"], "result_digest": heat["result_revisions"][-1]["digest"]},
    )
    state = command(ops, state, "finalize")
    assert state["summary_revision"] == 2 and len(state["exports"]) == 8


def test_retention_reason_and_bindings_survive_restart_and_export(ops):
    state = corrected_issued_progression(ops)
    reopened = EventOperations(ops.store.path)
    decision = reopened.export(principal(), state["event_id"])["progression_decisions"][-1]
    assert decision["reason"] == "Director retains completed final under the show appeal decision"
    assert decision["actor_id"] == principal().actor_id
    assert state["rounds"][-1]["fields"][0]["issue_id"] in decision["retained_issue_ids"]
    assert (
        state["rounds"][0]["fields"][0]["result_revisions"][-1]["digest"]
        in decision["result_digests"]
    )


def test_delimiter_bearing_identifiers_cannot_collide_in_source_streams(ops):
    config = synthetic_event()
    config["roster"] = config["roster"][:2]
    config["roster"][0]["entry_id"] = "b"
    config["roster"][1]["entry_id"] = "a:b"
    config["round_rules"] = [
        {"round_id": "qualifying", "capacity": 1, "qualifiers_per_heat": 1},
        {"round_id": "final", "capacity": 2, "qualifiers_per_heat": 0},
    ]
    state = ops.create(principal(), "create", config, "cli")
    state = command(
        ops,
        state,
        "fields",
        {
            "fields": [
                {"heat_id": "h:a", "entries": ["b"], "stands": ["1"]},
                {"heat_id": "h", "entries": ["a:b"], "stands": ["1"]},
            ]
        },
    )
    for heat_id, entry in [("h:a", "b"), ("h", "a:b")]:
        state = command(ops, state, "calculate", {"heat_id": heat_id})
        _, heat = ops._heat(state, heat_id)
        state = command(
            ops, state, "review", {"heat_id": heat_id, "sheet_digest": heat["sheet"]["digest"]}
        )
        state = command(
            ops,
            state,
            "issue",
            {"heat_id": heat_id, "sheet_digest": heat["sheet"]["digest"], "confirm": True},
        )
        _, heat = ops._heat(state, heat_id)
        state = command(
            ops,
            state,
            "outcomes",
            {
                "heat_id": heat_id,
                "issue_id": heat["issue_id"],
                "outcomes": [
                    {
                        "entry_id": entry,
                        "status": "finished",
                        "timer1_seconds": "20",
                        "timer2_seconds": "20",
                    }
                ],
            },
        )
        _, heat = ops._heat(state, heat_id)
        state = command(
            ops,
            state,
            "accept",
            {"heat_id": heat_id, "result_digest": heat["result_revisions"][-1]["digest"]},
        )
    deliveries = ops.export(principal(), state["event_id"])["deliveries"]
    assert len({item["delivery_id"] for item in deliveries}) == 2
    assert len({item["payload"]["source_result_id"] for item in deliveries}) == 2
    assert all(item["payload"]["supersedes_source_digest"] is None for item in deliveries)


def test_late_transfer_destination_collision_preserves_other_authority(ops, tmp_path, monkeypatch):
    import hashlib

    from strathmark.events import operations

    state = create(ops)
    destination = tmp_path / "late-target.sqlite3"
    original = EventOperations
    other_state = None
    before = None

    def initialize(path):
        nonlocal other_state, before
        target = original(path)
        if ".transfer" in str(path):
            other = original(destination)
            other_state = other.create(
                principal("synthetic:second-show"),
                "other",
                synthetic_event("synthetic:second-show"),
                "cli",
            )
            before = [
                hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (other.store.path, other.store.head)
            ]
        return target

    monkeypatch.setattr(operations, "EventOperations", initialize)
    with pytest.raises(EventError, match="collision"):
        ops.transfer_to(
            principal(),
            state["event_id"],
            destination,
            request_id="transfer",
            expected_revision=state["revision"],
            editor="cli",
        )
    other = original(destination)
    assert other.read(principal("synthetic:second-show"), other_state["event_id"]) == other_state
    assert [
        hashlib.sha256(p.read_bytes()).hexdigest() for p in (other.store.path, other.store.head)
    ] == before
    assert ops.read(principal(), state["event_id"])["status"] == "fenced"


def test_older_unpublished_identity_scheme_is_inspection_only(ops):
    state = create(ops)
    state.pop("source_identity_version", None)
    with ops.store.transaction() as db:
        ops.store.append(
            db,
            state["event_id"],
            principal().actor_id,
            "older-checkpoint",
            "synthetic-legacy-fixture",
            state,
        )
    assert ops.read(principal(), state["event_id"]) == state
    with pytest.raises(EventError, match="incompatible_managed_checkpoint"):
        command(ops, state, "handover", {"editor": "gui"})


def test_root_projection_cannot_diverge_from_immutable_command_evidence(ops):
    state = create(ops)
    with ops.store.connection() as db:
        changed = dict(state["root_selection"], mark_policy="v2-optimizer.v1")
        from strathmark.events.contracts import encode

        db.execute("UPDATE roots SET selection=?", (encode(changed),))
        db.commit()
    with pytest.raises(EventError, match="corrupt_competition_root"):
        EventOperations(ops.store.path)
