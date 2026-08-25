"""Atomic rolling-reaction cursor projection shared by every event writer."""

from __future__ import annotations

import sqlite3

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.errors import V3Error
from strathmark.v3.contracts.events import EventEnvelope, EventKind

ZERO_DIGEST = "0" * 64
_ROLLING_REACTION_EVENTS = {
    EventKind.FIELD_ROSTER_REVISED,
    EventKind.FIELD_SUPERSEDED,
    EventKind.RESULT_RECORDED,
    EventKind.RESULT_SUPERSEDED,
    EventKind.ROUND_EPOCH_FROZEN,
    EventKind.ROUND_CLOSED,
    EventKind.TOURNAMENT_CLOSED,
}


class RollingRestartIntegrityError(V3Error, RuntimeError):
    code = "rolling_restart_integrity_error"


def advance_rolling_reaction_cursor(
    connection: sqlite3.Connection, events: tuple[EventEnvelope, ...]
) -> str | None:
    """Verify any relevant obligation and advance the all-command cursor."""

    if not connection.in_transaction:
        raise RollingRestartIntegrityError("rolling reaction cursor requires a writer transaction")
    if not events:
        raise RollingRestartIntegrityError("rolling reaction cursor requires events")
    reaction_id = rolling_reaction_identity(events)
    if reaction_id is not None:
        event_refs = _event_refs(events)
        event_set_digest = canonical_digest(
            {
                "schema_version": "strathmark-v3-rolling-reaction-event-set-v1",
                "events": event_refs,
            }
        )
        material = (
            reaction_id,
            str(events[0].command.command_id),
            events[0].global_sequence,
            events[-1].global_sequence,
            canonical_bytes(event_refs).decode("utf-8"),
            event_set_digest,
            events[-1].occurred_at_utc,
        )
        obligation = connection.execute(
            "SELECT reaction_id,source_command_id,first_global_sequence,"
            "last_global_sequence,event_ids_json,event_set_digest,registered_at "
            "FROM v3_rolling_reaction_obligations WHERE reaction_id=?",
            (reaction_id,),
        ).fetchone()
        if obligation is None:
            connection.execute(
                "INSERT INTO v3_rolling_reaction_obligations VALUES (?,?,?,?,?,?,?)",
                material,
            )
        elif tuple(obligation) != material:
            raise RollingRestartIntegrityError(
                "rolling reaction cursor has a conflicting obligation"
            )
    row = connection.execute(
        "SELECT cursor_revision,through_global_sequence,through_event_digest,"
        "relevant_command_count,latest_reaction_id,cursor_digest,updated_at "
        "FROM v3_rolling_reaction_cursor WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise RollingRestartIntegrityError("rolling reaction cursor is missing")
    prior_value = {
        "schema_version": "strathmark-v3-rolling-reaction-cursor-v1",
        "cursor_revision": int(row[0]),
        "through_global_sequence": int(row[1]),
        "through_event_digest": str(row[2]),
        "relevant_command_count": int(row[3]),
        "latest_reaction_id": str(row[4]),
        "updated_at": str(row[6]),
    }
    if canonical_digest(prior_value) != str(row[5]):
        raise RollingRestartIntegrityError("rolling reaction cursor digest differs")
    if events[0].global_sequence != prior_value["through_global_sequence"] + 1:
        raise RollingRestartIntegrityError("rolling reaction cursor has a sequence gap")
    next_value = {
        "schema_version": "strathmark-v3-rolling-reaction-cursor-v1",
        "cursor_revision": prior_value["cursor_revision"] + 1,
        "through_global_sequence": events[-1].global_sequence,
        "through_event_digest": events[-1].event_digest,
        "relevant_command_count": prior_value["relevant_command_count"]
        + (1 if reaction_id is not None else 0),
        "latest_reaction_id": (
            prior_value["latest_reaction_id"] if reaction_id is None else reaction_id
        ),
        "updated_at": events[-1].occurred_at_utc,
    }
    changed = connection.execute(
        "UPDATE v3_rolling_reaction_cursor SET cursor_revision=?,"
        "through_global_sequence=?,through_event_digest=?,relevant_command_count=?,"
        "latest_reaction_id=?,cursor_digest=?,updated_at=? "
        "WHERE singleton=1 AND cursor_revision=? AND cursor_digest=?",
        (
            next_value["cursor_revision"],
            next_value["through_global_sequence"],
            next_value["through_event_digest"],
            next_value["relevant_command_count"],
            next_value["latest_reaction_id"],
            canonical_digest(next_value),
            next_value["updated_at"],
            prior_value["cursor_revision"],
            row[5],
        ),
    ).rowcount
    if changed != 1:
        raise RollingRestartIntegrityError("rolling reaction cursor update conflicted")
    return reaction_id


def reset_rolling_reaction_cursor(connection: sqlite3.Connection) -> None:
    """Reset only inside an explicit verified genesis replay transaction."""

    if not connection.in_transaction:
        raise RollingRestartIntegrityError("rolling reaction cursor requires a writer transaction")
    value = {
        "schema_version": "strathmark-v3-rolling-reaction-cursor-v1",
        "cursor_revision": 0,
        "through_global_sequence": 0,
        "through_event_digest": ZERO_DIGEST,
        "relevant_command_count": 0,
        "latest_reaction_id": ZERO_DIGEST,
        "updated_at": "1970-01-01T00:00:00.000Z",
    }
    changed = connection.execute(
        "UPDATE v3_rolling_reaction_cursor SET cursor_revision=0,"
        "through_global_sequence=0,through_event_digest=?,relevant_command_count=0,"
        "latest_reaction_id=?,cursor_digest=?,updated_at=? WHERE singleton=1",
        (
            ZERO_DIGEST,
            ZERO_DIGEST,
            canonical_digest(value),
            value["updated_at"],
        ),
    ).rowcount
    if changed != 1:
        raise RollingRestartIntegrityError("rolling reaction cursor reset failed")


def require_rolling_reaction_cursor_at_event_head(
    connection: sqlite3.Connection,
) -> None:
    """Fail before append when the projected cursor is not the current event tip."""

    if not connection.in_transaction:
        raise RollingRestartIntegrityError("rolling reaction cursor requires a writer transaction")
    row = connection.execute(
        "SELECT cursor_revision,through_global_sequence,through_event_digest,"
        "relevant_command_count,latest_reaction_id,cursor_digest,updated_at "
        "FROM v3_rolling_reaction_cursor WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise RollingRestartIntegrityError("rolling reaction cursor is missing")
    value = {
        "schema_version": "strathmark-v3-rolling-reaction-cursor-v1",
        "cursor_revision": int(row[0]),
        "through_global_sequence": int(row[1]),
        "through_event_digest": str(row[2]),
        "relevant_command_count": int(row[3]),
        "latest_reaction_id": str(row[4]),
        "updated_at": str(row[6]),
    }
    if canonical_digest(value) != str(row[5]):
        raise RollingRestartIntegrityError("rolling reaction cursor digest differs")
    event = connection.execute(
        "SELECT global_sequence,event_digest FROM v3_events ORDER BY global_sequence DESC LIMIT 1"
    ).fetchone()
    expected = (0, ZERO_DIGEST) if event is None else (int(event[0]), str(event[1]))
    observed = (value["through_global_sequence"], value["through_event_digest"])
    if observed == expected:
        return
    checkpoint = connection.execute(
        "SELECT 1 FROM v3_rolling_restart_checkpoints LIMIT 1"
    ).fetchone()
    if (
        expected[0] > 0
        and checkpoint is None
        and value
        == {
            "schema_version": "strathmark-v3-rolling-reaction-cursor-v1",
            "cursor_revision": 0,
            "through_global_sequence": 0,
            "through_event_digest": ZERO_DIGEST,
            "relevant_command_count": 0,
            "latest_reaction_id": ZERO_DIGEST,
            "updated_at": "1970-01-01T00:00:00.000Z",
        }
    ):
        raise RollingRestartIntegrityError("rolling reaction cursor cutover is required")
    raise RollingRestartIntegrityError("rolling reaction cursor differs from the event head")


def rolling_reaction_identity(
    events: tuple[EventEnvelope, ...],
) -> str | None:
    if not events:
        raise RollingRestartIntegrityError("rolling reaction identity requires events")
    if not any(event.kind in _ROLLING_REACTION_EVENTS for event in events):
        return None
    event_set_digest = canonical_digest(
        {
            "schema_version": "strathmark-v3-rolling-reaction-event-set-v1",
            "events": _event_refs(events),
        }
    )
    return canonical_digest(
        {
            "source_command_id": str(events[0].command.command_id),
            "event_set_digest": event_set_digest,
        }
    )


def _event_refs(events: tuple[EventEnvelope, ...]) -> list[dict[str, object]]:
    return [
        {
            "event_id": str(event.event_id),
            "event_digest": event.event_digest,
            "global_sequence": event.global_sequence,
        }
        for event in events
    ]


__all__ = [
    "RollingRestartIntegrityError",
    "ZERO_DIGEST",
    "advance_rolling_reaction_cursor",
    "require_rolling_reaction_cursor_at_event_head",
    "reset_rolling_reaction_cursor",
    "rolling_reaction_identity",
]
