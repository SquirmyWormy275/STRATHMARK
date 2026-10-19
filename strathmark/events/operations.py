"""Event coordination shared by CLI, GUI and hosts; no presentation imports."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from strathmark.private_storage import (
    private_directory,
    reject_links,
    verify_authority_parent,
)

from . import numerics
from .contracts import (
    CONTRACT,
    SOURCE_ID_VERSION,
    EventError,
    closed,
    decimal_text,
    digest,
    encode,
    identifier,
    integer,
    number,
    validate_config,
)
from .store import EventStore, atomic_json, publish_file_exclusive, sync_directory


class EventOperations:
    def __init__(self, database):
        self.store = EventStore(database)

    def create(self, principal, request_id, config, editor):
        validate_config(config)
        principal.authorize(config["show_id"], write=True)
        identifier(request_id)
        identifier(editor)
        binding = digest(["create", config, editor])
        with self.store.transaction() as db:
            previous = self.store.retry(
                db, config["event_id"], principal.actor_id, request_id, binding
            )
            if previous:
                return previous
            if db.execute(
                "SELECT 1 FROM events WHERE event_id=?", (config["event_id"],)
            ).fetchone():
                raise EventError("event_already_exists")
            identity = numerics.numeric_identity()
            root = db.execute(
                "SELECT selection FROM roots WHERE show_id=? AND tournament_id=?",
                (config["show_id"], config["tournament_id"]),
            ).fetchone()
            if root:
                selection = json.loads(root[0])
                if (
                    selection["engine"] != config["engine"]
                    or selection.get("mark_policy") != config["mark_policy"]
                    or selection.get("source_identity_version") != SOURCE_ID_VERSION
                    or selection["numeric_identity"] != identity
                ):
                    raise EventError(
                        "competition_root_selection_immutable: use its original selected runtime"
                    )
            else:
                selection = {
                    "engine": config["engine"],
                    "mark_policy": config["mark_policy"],
                    "source_identity_version": SOURCE_ID_VERSION,
                    "numeric_identity": identity,
                    "runtime": "local",
                    "contract": CONTRACT,
                    "selected_by": principal.actor_id,
                    "selected_at": datetime.now(timezone.utc).isoformat(),
                    "reason": config["selection_reason"],
                }
                db.execute(
                    "INSERT INTO roots VALUES (?,?,?)",
                    (config["show_id"], config["tournament_id"], encode(selection)),
                )
            state = {
                "schema_version": CONTRACT,
                "event_id": config["event_id"],
                "show_id": config["show_id"],
                "revision": 1,
                "source_identity_version": SOURCE_ID_VERSION,
                "config": deepcopy(config),
                "editor": editor,
                "status": "open",
                "numeric_identity": identity,
                "root_selection": selection,
                "rounds": [],
                "forecasts": [],
                "invalidated": [],
                "exports": [],
                "audit": [],
                "progression_review": False,
            }
            self._audit(state, principal, "create", request_id)
            self.store.append(db, state["event_id"], principal.actor_id, request_id, binding, state)
            return state

    def read(self, principal, event_id):
        with self.store.transaction() as db:
            state = self.store.read(db, event_id)
            principal.authorize(state["show_id"])
            return state

    def execute(self, principal, event_id, request_id, expected_revision, editor, action, payload):
        identifier(request_id)
        identifier(editor)
        integer(expected_revision, 1, 2**31 - 1)
        binding = digest([action, expected_revision, editor, payload])
        if len(encode(payload).encode()) > 4 * 1024 * 1024:
            raise EventError("command_capacity")
        with self.store.transaction() as db:
            state = self.store.read(db, event_id)
            principal.authorize(state["show_id"], write=True)
            previous = self.store.retry(db, event_id, principal.actor_id, request_id, binding)
            if previous:
                return previous
            if state.get("source_identity_version") != SOURCE_ID_VERSION:
                raise EventError(
                    "incompatible_managed_checkpoint: resume its original candidate; inspection only in this runtime"
                )
            if state["revision"] != expected_revision:
                raise EventError("stale_event_revision")
            if state["editor"] != editor:
                raise EventError("editor_handover_required")
            if state["status"] == "fenced":
                raise EventError("authority_fenced")
            if state["status"] == "finalized" and action not in {
                "correct",
                "handover",
                "delivery",
                "fence",
            }:
                raise EventError("event_finalized")
            self._transition(state, principal, action, payload)
            state["revision"] += 1
            self._audit(state, principal, action, request_id)
            self.store.append(db, event_id, principal.actor_id, request_id, binding, state)
            return state

    @staticmethod
    def _audit(state, principal, action, request_id):
        state["audit"].append(
            {
                "revision": state["revision"],
                "actor_id": principal.actor_id,
                "action": action,
                "request_id": request_id,
            }
        )

    @staticmethod
    def _round(state):
        if not state["rounds"]:
            raise EventError("exact_fields_required")
        return state["rounds"][-1]

    @staticmethod
    def _heat(state, heat_id):
        for round_ in state["rounds"]:
            for heat in round_["fields"]:
                if heat["heat_id"] == heat_id:
                    return round_, heat
        raise EventError("heat_not_found")

    @staticmethod
    def _eligible(state):
        if not state["rounds"]:
            return [r["entry_id"] for r in state["config"]["roster"]]
        round_ = state["rounds"][-1]
        if round_["state"] == "advanced":
            return round_["advancing"]
        return round_["eligible"]

    def _transition(self, state, principal, action, payload):
        if action == "handover":
            closed(payload, {"editor"})
            state["editor"] = identifier(payload["editor"])
        elif action == "configure":
            closed(payload, {"config"})
            config = validate_config(payload["config"])
            if any(h.get("issue_id") for r in state["rounds"] for h in r["fields"]):
                raise EventError("issued_configuration_immutable")
            for key in (
                "event_id",
                "show_id",
                "tournament_id",
                "source_show_id",
                "engine",
                "mark_policy",
                "publisher",
            ):
                if config[key] != state["config"][key]:
                    raise EventError("root_authority_immutable")
            state["invalidated"].extend(state["rounds"])
            state["rounds"] = []
            state["config"] = deepcopy(config)
        elif action == "forecast":
            closed(payload, set())
            self._numeric_ready(state)
            ids = self._eligible(state)
            rows = []
            for offset in range(0, len(ids), 64):
                rows.extend(
                    numerics.calculate(
                        state["config"], ids[offset : offset + 64], forecasts_only=True
                    )
                )
            state["forecasts"].append(
                {
                    "purpose": "seeding_only",
                    "issued_mark": False,
                    "rows": rows,
                    "input_digest": digest(state["config"]),
                    "numeric_identity": state["numeric_identity"],
                }
            )
        elif action == "fields":
            self._fields(state, payload)
        elif action == "calculate":
            closed(payload, {"heat_id"})
            self._numeric_ready(state)
            round_, heat = self._heat(state, payload["heat_id"])
            if round_ is not self._round(state) or heat.get("issue_id"):
                raise EventError("issued_or_prior_field_immutable")
            rows = numerics.calculate(state["config"], heat["entries"])
            sheet = {
                "engine": state["config"]["engine"],
                "mark_policy": state["config"]["mark_policy"],
                "numeric_identity": state["numeric_identity"],
                "field_digest": heat["field_digest"],
                "historical_snapshot_digest": digest(
                    [r["history"] for r in state["config"]["roster"]]
                ),
                "cutoff": state["config"]["prediction_as_of"],
                "rows": rows,
                "synthetic": True,
                "official_use": False,
            }
            sheet["digest"] = digest(sheet)
            heat.update(sheet=sheet, state="calculated")
        elif action in {"review", "issue"}:
            closed(payload, {"heat_id", "sheet_digest"}, {"confirm"})
            round_, heat = self._heat(state, payload["heat_id"])
            if round_ is not self._round(state) or heat.get("issue_id") or not heat.get("sheet"):
                raise EventError("reviewable_sheet_required")
            if payload["sheet_digest"] != heat["sheet"]["digest"]:
                raise EventError("stale_sheet")
            if action == "review":
                heat.update(state="reviewed", reviewed_by=principal.actor_id)
            else:
                if heat["state"] != "reviewed" or payload.get("confirm") is not True:
                    raise EventError("separate_issue_confirmation_required")
                heat.update(
                    state="issued",
                    issue_id=digest(
                        [state["event_id"], heat["field_digest"], heat["sheet"]["digest"]]
                    ),
                )
        elif action in {"outcomes", "correct"}:
            self._outcomes(state, payload, correction=action == "correct")
        elif action == "accept":
            closed(payload, {"heat_id", "result_digest"}, {"ack_partial_timers"})
            _, heat = self._heat(state, payload["heat_id"])
            if not heat["result_revisions"]:
                raise EventError("complete_outcomes_required")
            result = heat["result_revisions"][-1]
            if result["digest"] != payload["result_digest"] or result["accepted"]:
                raise EventError("stale_result")
            if result["partial_timers"] and payload.get("ack_partial_timers") is not True:
                raise EventError("partial_timer_review_required")
            result.update(accepted=True, accepted_by=principal.actor_id)
            heat["state"] = "accepted"
            self._queue_attempts(state, heat, result)
        elif action == "advance":
            self._advance(state, payload)
        elif action == "retain_downstream":
            closed(payload, {"reason"})
            if (
                principal.role != "director"
                or not isinstance(payload["reason"], str)
                or not payload["reason"].strip()
            ):
                raise EventError("director_reason_required")
            if not state["progression_review"]:
                raise EventError("no_progression_review_pending")
            state.setdefault("progression_decisions", []).append(
                {
                    "revision": state["revision"] + 1,
                    "actor_id": principal.actor_id,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "reason": payload["reason"],
                    "retained_issue_ids": [
                        h["issue_id"]
                        for r in state["rounds"]
                        for h in r["fields"]
                        if h.get("issue_id")
                    ],
                    "result_digests": [
                        h["result_revisions"][-1]["digest"]
                        for r in state["rounds"]
                        for h in r["fields"]
                        if h["result_revisions"]
                    ],
                }
            )
            state["progression_review"] = False
        elif action == "finalize":
            closed(payload, set())
            round_ = self._round(state)
            if (
                round_["ordinal"] != len(state["config"]["round_rules"]) - 1
                or state["progression_review"]
            ):
                raise EventError("final_or_progression_review_required")
            if len(round_["fields"]) != 1 or any(
                h["state"] != "accepted" for h in round_["fields"]
            ):
                raise EventError("complete_accepted_final_required")
            if any(
                not h["result_revisions"] or not h["result_revisions"][-1]["accepted"]
                for r in state["rounds"]
                for h in r["fields"]
            ):
                raise EventError("all_summary_results_must_be_accepted")
            state["status"] = "finalized"
            state["summary"] = self._summary(state)
            state["summary_revision"] = state.get("summary_revision", 0) + 1
        elif action == "delivery":
            closed(payload, {"delivery_id", "state", "receipt_digest"})
            if payload["state"] not in {"accepted", "conflicting", "pending"}:
                raise EventError("invalid_delivery_state")
            item = next(
                (e for e in state["exports"] if e["delivery_id"] == payload["delivery_id"]), None
            )
            if item is None:
                raise EventError("delivery_not_found")
            if (
                item["state"] == "accepted"
                and item.get("receipt_digest") != payload["receipt_digest"]
            ):
                raise EventError("delivery_receipt_conflict")
            item.update(state=payload["state"], receipt_digest=payload["receipt_digest"])
        elif action == "fence":
            closed(payload, {"reason"}, {"transfer_target"})
            if principal.role != "director" or not payload["reason"]:
                raise EventError("director_reason_required")
            state["fenced_from"] = state["status"]
            state["status"] = "fenced"
            if payload.get("transfer_target"):
                state["transfer_target"] = str(Path(payload["transfer_target"]).resolve())
        else:
            raise EventError("unsupported_command: " + str(action))

    @staticmethod
    def _numeric_ready(state):
        if numerics.numeric_identity() != state["numeric_identity"]:
            raise EventError(
                "selected_numeric_runtime_changed: resume original installed component"
            )

    def _fields(self, state, payload):
        closed(payload, {"fields"})
        rules = state["config"]["round_rules"]
        prior = state["rounds"][-1] if state["rounds"] else None
        replace = prior is not None and prior["state"] != "advanced"
        ordinal = prior["ordinal"] if replace else len(state["rounds"])
        if ordinal >= len(rules):
            raise EventError("no_later_round")
        if replace and any(h.get("issue_id") for h in prior["fields"]):
            raise EventError("issued_fields_immutable")
        eligible = self._eligible(state)
        fields = deepcopy(payload["fields"])
        if not isinstance(fields, list) or not fields:
            raise EventError("fields_required")
        if ordinal == len(rules) - 1 and len(fields) != 1:
            raise EventError("single_final_required")
        seen = []
        old_ids = {h["heat_id"] for r in state["rounds"][:ordinal] for h in r["fields"]}
        roster = {r["entry_id"]: r for r in state["config"]["roster"]}
        for heat in fields:
            closed(heat, {"heat_id", "entries", "stands"}, {"schedule_ref", "unavailable_stands"})
            identifier(heat["heat_id"])
            if heat["heat_id"] in old_ids:
                raise EventError("duplicate_heat_identity")
            old_ids.add(heat["heat_id"])
            if not 1 <= len(heat["entries"]) <= rules[ordinal]["capacity"]:
                raise EventError("field_capacity")
            if len(heat["stands"]) != len(heat["entries"]) or len(set(heat["stands"])) != len(
                heat["stands"]
            ):
                raise EventError("physical_stands_required")
            if set(heat["stands"]) - set(state["config"]["stands"]) or set(heat["stands"]) & set(
                heat.get("unavailable_stands", [])
            ):
                raise EventError("stand_unavailable")
            if set(heat["entries"]) - set(eligible):
                raise EventError("ineligible_entry")
            groups = [roster[e]["gear_group"] for e in heat["entries"] if roster[e]["gear_group"]]
            if len(groups) != len(set(groups)):
                raise EventError("gear_conflict")
            seen.extend(heat["entries"])
            heat["field_digest"] = digest(
                [state["event_id"], rules[ordinal], state["config"]["wood"], heat]
            )
            heat.update(state="configured", result_revisions=[])
        if len(seen) != len(set(seen)) or set(seen) != set(eligible):
            raise EventError("complete_entrant_accounting_required")
        round_ = {
            "round_id": rules[ordinal]["round_id"],
            "ordinal": ordinal,
            "fields": fields,
            "eligible": eligible,
            "state": "open",
        }
        if replace:
            state["invalidated"].append(state["rounds"].pop())
        state["rounds"].append(round_)

    def _outcomes(self, state, payload, *, correction):
        closed(
            payload,
            {"heat_id", "issue_id", "outcomes"},
            {"reason", "supersedes", "allow_partial_timers"},
        )
        round_, heat = self._heat(state, payload["heat_id"])
        if not heat.get("issue_id") or payload["issue_id"] != heat["issue_id"]:
            raise EventError("exact_issued_sheet_required")
        prior = heat["result_revisions"]
        if bool(prior) != correction:
            raise EventError("correction_required" if prior else "initial_outcomes_required")
        if correction and (
            not payload.get("reason") or payload.get("supersedes") != prior[-1]["digest"]
        ):
            raise EventError("correction_reason_and_current_digest_required")
        if {o.get("entry_id") for o in payload["outcomes"]} != set(heat["entries"]) or len(
            payload["outcomes"]
        ) != len(heat["entries"]):
            raise EventError("complete_outcomes_required")
        marks = {r["entry_id"]: r["start_mark_seconds"] for r in heat["sheet"]["rows"]}
        outcomes, partial = [], False
        for supplied in payload["outcomes"]:
            closed(supplied, {"entry_id", "status"}, {"timer1_seconds", "timer2_seconds"})
            status = supplied["status"]
            if status not in {"finished", "dnf", "dq", "dns", "scratched", "void"}:
                raise EventError("invalid_nonfinish_status")
            row = deepcopy(supplied)
            row["start_mark_seconds"] = marks[row["entry_id"]]
            if status == "finished":
                t1, t2 = supplied.get("timer1_seconds"), supplied.get("timer2_seconds")
                readings = [number(v) for v in (t1, t2) if v is not None]
                required = 2 if state["config"]["timer_policy"] == "dual_mean" else 1
                if len(readings) != required:
                    if (
                        required == 2
                        and len(readings) == 1
                        and payload.get("allow_partial_timers") is True
                    ):
                        partial = True
                    else:
                        raise EventError("timer_readings_required")
                raw = sum(readings, Decimal(0)) / len(readings)
                row.update(
                    raw_duration_seconds=decimal_text(raw),
                    common_count_seconds=decimal_text(raw + marks[row["entry_id"]]),
                )
            elif (
                supplied.get("timer1_seconds") is not None
                or supplied.get("timer2_seconds") is not None
            ):
                raise EventError("nonfinish_has_no_invented_time")
            else:
                row.update(raw_duration_seconds=None, common_count_seconds=None)
            outcomes.append(row)
        finishes = sorted(
            Decimal(o["common_count_seconds"]) for o in outcomes if o["status"] == "finished"
        )
        for row in outcomes:
            row["official_position"] = (
                1 + finishes.index(Decimal(row["common_count_seconds"]))
                if row["status"] == "finished"
                else None
            )
        result = {
            "source_revision": len(prior) + 1,
            "outcomes": outcomes,
            "issue_id": heat["issue_id"],
            "accepted": False,
            "partial_timers": partial,
            "reason": payload.get("reason"),
            "supersedes": prior[-1]["digest"] if prior else None,
        }
        result["digest"] = digest(
            {key: value for key, value in result.items() if key != "accepted"}
        )
        prior.append(result)
        heat["state"] = "outcomes_recorded"
        if correction:
            downstream = state["rounds"][round_["ordinal"] + 1 :]
            if any(h.get("issue_id") for r in downstream for h in r["fields"]):
                state["progression_review"] = True
            else:
                state["invalidated"].extend(downstream)
                del state["rounds"][round_["ordinal"] + 1 :]
                round_["state"] = "open"
                round_.pop("advancing", None)
            state["status"] = "open"
            state.pop("summary", None)

    def _advance(self, state, payload):
        closed(payload, set(), {"tie_selections", "reason"})
        round_ = self._round(state)
        rule = state["config"]["round_rules"][round_["ordinal"]]
        if (
            not rule["qualifiers_per_heat"]
            or round_["state"] == "advanced"
            or state["progression_review"]
        ):
            raise EventError("round_cannot_advance")
        advancing = []
        for heat in round_["fields"]:
            if heat["state"] != "accepted":
                raise EventError("all_fields_must_be_accepted")
            finishes = [
                o for o in heat["result_revisions"][-1]["outcomes"] if o["status"] == "finished"
            ]
            count = rule["qualifiers_per_heat"]
            winners = [o["entry_id"] for o in finishes if o["official_position"] <= count]
            if len(winners) > count and state["config"]["tie_policy"] == "judge":
                chosen = payload.get("tie_selections", {}).get(heat["heat_id"])
                if (
                    not chosen
                    or not payload.get("reason")
                    or len(chosen) != count
                    or len(set(chosen)) != count
                    or set(chosen) - set(winners)
                ):
                    raise EventError("advancement_tie_requires_judge_selection_and_reason")
                boundary = max(o["official_position"] for o in finishes if o["entry_id"] in winners)
                mandatory = {o["entry_id"] for o in finishes if o["official_position"] < boundary}
                if mandatory - set(chosen):
                    raise EventError("tie_selection_omits_clear_qualifier")
                winners = chosen
            advancing.extend(winners)
        if not advancing:
            raise EventError("no_finishers_to_advance: director must resolve event")
        round_.update(state="advanced", advancing=advancing, advancement_decision=deepcopy(payload))

    @staticmethod
    def _queue_attempts(state, heat, result):
        for outcome in result["outcomes"]:
            source_id = "attempt:" + digest(
                [
                    state["config"]["source_show_id"],
                    state["config"]["tournament_id"],
                    state["event_id"],
                    heat["heat_id"],
                    outcome["entry_id"],
                ]
            )
            predecessor = next(
                (
                    item
                    for item in reversed(state["exports"])
                    if item["payload"]["source_result_id"] == source_id
                ),
                None,
            )
            payload = {
                "schema_version": "strathmark.source-result.v1",
                "source_show_id": state["config"]["source_show_id"],
                "source_event_id": state["event_id"],
                "source_result_id": source_id,
                "source_revision": result["source_revision"],
                "supersedes_source_digest": predecessor["payload_digest"] if predecessor else None,
                "correction_reason": result["reason"],
                "heat_id": heat["heat_id"],
                "field_digest": heat["field_digest"],
                "issue_id": heat["issue_id"],
                "discipline": state["config"]["discipline"],
                "event_date": state["config"]["event_date"],
                "wood": state["config"]["wood"],
                "outcome": outcome,
                "result_digest": result["digest"],
                "synthetic": True,
            }
            state["exports"].append(
                {
                    "delivery_id": digest([source_id, result["source_revision"]]),
                    "state": "pending",
                    "publisher": state["config"]["publisher"],
                    "payload": payload,
                    "payload_digest": digest(payload),
                }
            )

    @staticmethod
    def _summary(state):
        # Finalists take their final positions. Eliminated entries share a
        # placing tier within their latest round; clocks from different rebased
        # heats are never compared to manufacture a cross-heat tiebreak.
        latest = {}
        for round_ in state["rounds"]:
            for heat in round_["fields"]:
                for row in heat["result_revisions"][-1]["outcomes"]:
                    latest[row["entry_id"]] = (round_["ordinal"], row)
        final_ordinal = len(state["config"]["round_rules"]) - 1
        summary = []
        for entry_id, (ordinal, row) in latest.items():
            ahead = sum(1 for other, (n, _) in latest.items() if n > ordinal)
            position = (
                row["official_position"]
                if ordinal == final_ordinal
                else (ahead + 1 if row["status"] == "finished" else None)
            )
            summary.append(
                {
                    "entry_id": entry_id,
                    "official_position": position,
                    "status": row["status"],
                    "round_ordinal": ordinal,
                    "raw_duration_seconds": row["raw_duration_seconds"],
                    "start_mark_seconds": row["start_mark_seconds"],
                    "common_count_seconds": row["common_count_seconds"],
                }
            )
        return summary

    def export(self, principal, event_id):
        state = self.read(principal, event_id)
        return {
            "schema_version": CONTRACT,
            "event_id": event_id,
            "event_revision": state["revision"],
            "source_identity_version": state.get("source_identity_version"),
            "show_id": state["show_id"],
            "source_show_id": state["config"]["source_show_id"],
            "status": state["status"],
            "synthetic": True,
            "official_use": False,
            "numeric_identity": state["numeric_identity"],
            "rounds": state["rounds"],
            "progression_decisions": state.get("progression_decisions", []),
            "summary": state.get("summary"),
            "summary_revision": state.get("summary_revision", 0),
            "deliveries": state["exports"],
        }

    def backup(self, principal, event_id, path):
        if Path(path).exists():
            raise EventError("backup_destination_exists")
        state = self.read(principal, event_id)
        package = {"schema_version": CONTRACT, "state": state}
        package["digest"] = digest(package)
        try:
            atomic_json(path, package, replace=False)
        except FileExistsError as exc:
            raise EventError("backup_destination_exists") from exc
        return package["digest"]

    def verify_backup(self, path, event_id):
        try:
            package = json.loads(Path(path).read_text())
            expected = package.pop("digest")
            if (
                expected != digest(package)
                or package["schema_version"] != CONTRACT
                or package["state"]["event_id"] != event_id
            ):
                raise EventError("corrupt_or_incompatible_backup")
            with self.store.transaction() as db:
                current = self.store.read(db, event_id)
                if digest(current) != digest(package["state"]):
                    raise EventError("stale_backup")
            return package["state"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise EventError("invalid_backup") from exc

    def transfer_to(
        self, principal, event_id, destination, *, request_id, expected_revision, editor
    ):
        """Fence the old authority before restoring into pristine local storage.

        This deliberate local procedure never promotes an arbitrary old backup.
        Machine-loss recovery still needs independent fencing/current-head proof.
        """
        if principal.role != "director":
            raise EventError("director_authority_required")
        destination = reject_links(destination).resolve()
        if not destination.parent.exists():
            private_directory(destination.parent)
        verify_authority_parent(destination)
        if destination == self.store.path:
            raise EventError("fresh_recovery_destination_required")
        state = self.read(principal, event_id)
        if state["status"] != "fenced":
            state = self.execute(
                principal,
                event_id,
                request_id,
                expected_revision,
                editor,
                "fence",
                {
                    "reason": "Deliberate local authority transfer",
                    "transfer_target": str(destination),
                },
            )
        if state.get("transfer_target") != str(destination):
            raise EventError("authority_already_fenced_to_another_destination")
        source_digest = digest(state)
        if destination.exists():
            recovered = EventOperations(destination).read(principal, event_id)
            if recovered.get("recovery_source") != source_digest:
                raise EventError("recovery_destination_conflict")
            return recovered
        stage = destination.parent / ("." + destination.name + ".transfer")
        try:
            stage.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise EventError(
                "incomplete_transfer_requires_reconciliation: source remains fenced"
            ) from exc
        private_directory(stage)
        target = EventOperations(stage / "authority.sqlite3")
        with self.store.transaction() as db:
            commands = db.execute(
                "SELECT actor_id,request_id,binding,response FROM commands WHERE event_id=? ORDER BY sequence",
                (event_id,),
            ).fetchall()
        with target.store.transaction() as db:
            db.execute(
                "INSERT INTO roots VALUES (?,?,?)",
                (
                    state["show_id"],
                    state["config"]["tournament_id"],
                    encode(state["root_selection"]),
                ),
            )
        for actor_id, original_id, binding, response in commands:
            with target.store.transaction() as db:
                target.store.append(
                    db, event_id, actor_id, original_id, binding, json.loads(response)
                )
        resumed = deepcopy(state)
        resumed["status"] = resumed.pop("fenced_from")
        resumed["revision"] += 1
        resumed["recovery_source"] = source_digest
        self._audit(resumed, principal, "restore_fenced_authority", request_id)
        with target.store.transaction() as db:
            target.store.append(
                db,
                event_id,
                principal.actor_id,
                "restore:" + request_id,
                digest([source_digest, str(destination)]),
                resumed,
            )
        # The final DB has no valid head until the complete pair is published;
        # an interrupted pair is refused, never opened as a fresh authority.
        head = json.loads(target.store.head.read_text())
        head["database_path"] = str(destination)
        atomic_json(target.store.head, head)
        try:
            publish_file_exclusive(target.store.path, destination)
            publish_file_exclusive(
                target.store.head, destination.with_suffix(destination.suffix + ".head.json")
            )
        except FileExistsError as exc:
            raise EventError(
                "recovery_destination_collision: source remains fenced; explicit reconciliation required"
            ) from exc
        except OSError as exc:
            raise EventError(
                "recovery_publication_failed: source remains fenced; explicit reconciliation required"
            ) from exc
        target.store.path.unlink()
        target.store.head.unlink()
        stage.rmdir()
        sync_directory(destination.parent)
        return EventOperations(destination).read(principal, event_id)
