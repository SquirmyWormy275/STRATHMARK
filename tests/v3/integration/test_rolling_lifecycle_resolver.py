from __future__ import annotations

import json
from contextlib import contextmanager

from strathmark.v3.application.capacity import CapacityUse
from strathmark.v3.contracts.events import EventEnvelope, EventKind
from strathmark.v3.infrastructure.integrity import IntegrityTrustStore, P256EphemeralSigner
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.projections import SQLiteRollingLifecycleResolver
from tests.v3.integration.test_derivation_barrier import _bootstrap_empty_closure


class _EmptyCursor:
    def fetchall(self):
        return []


class _CapturingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, statement: str, parameters: tuple[object, ...]):
        self.calls.append((statement, parameters))
        return _EmptyCursor()


def test_frozen_successor_epoch_filters_cards_by_payload_round_id(tmp_path, monkeypatch) -> None:
    service, _tournament, _root, successor, _closure = _bootstrap_empty_closure(tmp_path)
    database = service.projections.database_path
    with open_v3_connection(database, read_only=True) as connection:
        row = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE event_kind=? "
            "ORDER BY global_sequence DESC LIMIT 1",
            (EventKind.ROUND_EPOCH_FROZEN.value,),
        ).fetchone()
    assert row is not None
    event = EventEnvelope.from_dict(json.loads(str(row[0])))
    assert str(event.aggregate_id).startswith("epoch:")
    assert event.command.payload.to_value()["epoch"]["round_id"] == str(successor)

    captured = _CapturingConnection()

    @contextmanager
    def fake_open(*_args, **_kwargs):
        yield captured

    monkeypatch.setattr(
        "strathmark.v3.infrastructure.sqlite.projections.open_v3_connection", fake_open
    )
    signer = P256EphemeralSigner.generate("integrity-key:rolling-round-payload")
    resolver = SQLiteRollingLifecycleResolver(
        database,
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        council_manifest_digest="a" * 64,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )

    resolver.resolve((event,))

    field_query = next(
        parameters
        for statement, parameters in captured.calls
        if "FROM v3_ingress_snapshots ingress" in statement
    )
    assert str(successor) in field_query
    assert str(event.aggregate_id) not in field_query
