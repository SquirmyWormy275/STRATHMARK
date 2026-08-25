from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from strathmark.v3.application.manual_actions import (
    ManualActionConflict,
    ManualActionEntrant,
    ManualActionKind,
    ManualActionRequirement,
    create_manual_action_requirement,
    create_manual_action_resolution,
)
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.forecasts import AssessorKind
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
)
from strathmark.v3.infrastructure.sqlite.connection import (
    immediate_transaction,
    open_v3_connection,
)
from strathmark.v3.infrastructure.sqlite.manual_actions import (
    SQLiteManualActionRequirementStore,
)
from strathmark.v3.infrastructure.sqlite.migrations import migrate_connection

NOW = "2026-08-24T22:00:00.000Z"
DEADLINE = "2026-08-24T21:59:59.000Z"


def _integrity():
    signer = P256EphemeralSigner.generate("integrity-key:manual-actions")
    return signer, IntegrityTrustStore((signer.identity,))


def _entrant(
    name: str,
    available: tuple[AssessorKind, ...],
    *,
    basis: str | None = None,
) -> ManualActionEntrant:
    return ManualActionEntrant(
        StableIdentifier(f"competitor:{name}"),
        available,
        canonical_digest({"publication": name}),
        basis,
    )


def _requirement(
    signer,
    *,
    revision: int = 1,
    entrants: tuple[ManualActionEntrant, ...] | None = None,
) -> ManualActionRequirement:
    return create_manual_action_requirement(
        field_id=StableIdentifier("field:manual-final"),
        upstream_field_revision=revision,
        field_revision_digest=canonical_digest({"field_revision": revision}),
        target_context_digest=canonical_digest({"context": "uh-300-gum"}),
        historical_cutoff_key=StableIdentifier("history:manual-cutoff"),
        tournament_epoch_id=StableIdentifier("epoch:manual-round"),
        bundle_digest=canonical_digest({"bundle": "v3"}),
        hard_deadline_at=DEADLINE,
        entrants=(
            entrants
            if entrants is not None
            else (
                _entrant(
                    "a",
                    (AssessorKind.FORMULA,),
                    basis=canonical_digest({"basis": "a"}),
                ),
                _entrant(
                    "b",
                    (AssessorKind.FORMULA,),
                    basis=canonical_digest({"basis": "b"}),
                ),
            )
        ),
        signer=signer,
        created_at=NOW,
    )


def test_derivation_has_no_manual_default_and_closes_the_availability_matrix() -> None:
    signer, trust = _integrity()
    single = _requirement(signer)
    assert single.action is ManualActionKind.ACCEPT_SINGLE_SURVIVOR
    assert single.binding.action is ManualActionKind.ACCEPT_SINGLE_SURVIVOR
    assert single.binding.requirement_digest == single.requirement_digest
    assert single.verify(trust) == single.content_value()

    complete = _requirement(
        signer,
        entrants=(
            _entrant("a", ()),
            _entrant("b", (AssessorKind.FORMULA, AssessorKind.ML)),
        ),
    )
    assert complete.action is ManualActionKind.COMPLETE_EXPECTED_TIME
    assert all(item.candidate_basis_digest is None for item in complete.entrants)

    with pytest.raises(ManualActionConflict, match="ordinary"):
        _requirement(
            signer,
            entrants=(
                _entrant("a", (AssessorKind.FORMULA, AssessorKind.ML)),
                _entrant("b", (AssessorKind.FORMULA, AssessorKind.ML)),
            ),
        )
    with pytest.raises(ManualActionConflict, match="candidate basis"):
        _requirement(
            signer,
            entrants=(
                _entrant("a", (AssessorKind.FORMULA,)),
                _entrant("b", (AssessorKind.FORMULA,)),
            ),
        )
    with pytest.raises(ManualActionConflict, match="deadline"):
        create_manual_action_requirement(
            **{
                **single.creation_arguments(),
                "signer": signer,
                "created_at": "2026-08-24T21:59:58.000Z",
            }
        )


def test_requirement_tamper_or_untrusted_signature_fails_closed() -> None:
    signer, trust = _integrity()
    requirement = _requirement(signer)
    with pytest.raises(ManualActionConflict, match="digest"):
        replace(requirement, requirement_digest="0" * 64)
    other = P256EphemeralSigner.generate("integrity-key:other-manual-actions")
    with pytest.raises(Exception, match="trusted"):
        requirement.verify(IntegrityTrustStore((other.identity,)))
    with pytest.raises(Exception, match="digest"):
        replace(
            requirement,
            manifest=requirement.manifest.__class__.from_dict(
                {
                    **requirement.manifest.to_dict(),
                    "body_digest": "0" * 64,
                }
            ),
        )


def test_sqlite_requirement_is_idempotent_restart_safe_and_revision_monotonic(
    tmp_path: Path,
) -> None:
    signer, trust = _integrity()
    database = tmp_path / "manual-actions.sqlite3"
    store = SQLiteManualActionRequirementStore(database, signer=signer, trust_store=trust)
    first = _requirement(signer)
    assert store.publish(first) == first
    assert store.publish(first) == first
    re_signed = _requirement(signer)
    assert re_signed.manifest.signature_der_b64 != first.manifest.signature_der_b64
    assert store.publish(re_signed) == first
    assert store.current(first.field_id) == first
    restarted = SQLiteManualActionRequirementStore(database, signer=signer, trust_store=trust)
    assert restarted.require_current(first.binding) == first

    changed_same_revision = _requirement(
        signer,
        entrants=(
            _entrant(
                "a",
                (AssessorKind.ML,),
                basis=canonical_digest({"basis": "a-ml"}),
            ),
            _entrant(
                "b",
                (AssessorKind.ML,),
                basis=canonical_digest({"basis": "b-ml"}),
            ),
        ),
    )
    with pytest.raises(ManualActionConflict, match="same field revision"):
        store.publish(changed_same_revision)

    successor = _requirement(signer, revision=2)
    assert store.publish(successor) == successor
    assert store.current(first.field_id) == successor
    with pytest.raises(ManualActionConflict, match="current"):
        store.require_current(first.binding)
    with open_v3_connection(database, read_only=True) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM v3_manual_action_requirements").fetchone()[0]
            == 2
        )


def test_resolution_is_atomic_exact_and_removes_only_the_current_requirement(
    tmp_path: Path,
) -> None:
    signer, trust = _integrity()
    database = tmp_path / "manual-resolution.sqlite3"
    store = SQLiteManualActionRequirementStore(database, signer=signer, trust_store=trust)
    requirement = store.publish(_requirement(signer))
    resolution = store.resolve(
        requirement.binding,
        receipt_id=StableIdentifier("receipt:manual-final"),
        receipt_digest=canonical_digest({"receipt": "manual-final"}),
        actor_id=StableIdentifier("actor:judge"),
        resolved_at="2026-08-24T22:00:01.000Z",
    )
    assert resolution.requirement_digest == requirement.requirement_digest
    assert resolution.verify(trust) == resolution.content_value()
    assert store.current(requirement.field_id) is None
    assert (
        store.resolve(
            requirement.binding,
            receipt_id=StableIdentifier("receipt:manual-final"),
            receipt_digest=canonical_digest({"receipt": "manual-final"}),
            actor_id=StableIdentifier("actor:judge"),
            resolved_at="2026-08-24T22:00:01.000Z",
        )
        == resolution
    )
    re_signed_resolution = resolution.__class__.from_dict(
        {
            **resolution.to_dict(),
            "manifest": create_manual_action_resolution(
                requirement,
                receipt_id=StableIdentifier("receipt:manual-final"),
                receipt_digest=canonical_digest({"receipt": "manual-final"}),
                actor_id=StableIdentifier("actor:judge"),
                resolved_at="2026-08-24T22:00:01.000Z",
                signer=signer,
            ).manifest.to_dict(),
        }
    )
    with open_v3_connection(database) as connection:
        with immediate_transaction(connection):
            store.resolve_connection(connection, requirement.binding, re_signed_resolution)
    with pytest.raises(ManualActionConflict, match="different resolution"):
        store.resolve(
            requirement.binding,
            receipt_id=StableIdentifier("receipt:other"),
            receipt_digest=canonical_digest({"receipt": "other"}),
            actor_id=StableIdentifier("actor:judge"),
            resolved_at="2026-08-24T22:00:02.000Z",
        )


def test_current_pointer_tamper_and_cross_field_retarget_fail_closed(
    tmp_path: Path,
) -> None:
    signer, trust = _integrity()
    database = tmp_path / "manual-pointer-tamper.sqlite3"
    store = SQLiteManualActionRequirementStore(database, signer=signer, trust_store=trust)
    requirement = store.publish(_requirement(signer))
    forged_time = "2026-08-24T22:00:03.000Z"
    forged_digest = canonical_digest(
        {
            "schema_version": "strathmark-v3-manual-action-current-v1",
            "field_id": str(requirement.field_id),
            "requirement_digest": requirement.requirement_digest,
            "upstream_field_revision": requirement.upstream_field_revision,
            "updated_at": forged_time,
        }
    )
    with open_v3_connection(database) as connection:
        connection.execute(
            "UPDATE v3_manual_action_current SET updated_at=?,current_digest=? WHERE field_id=?",
            (forged_time, forged_digest, str(requirement.field_id)),
        )
        connection.commit()
    with pytest.raises(ManualActionConflict, match="current"):
        store.current(requirement.field_id)

    with open_v3_connection(database) as connection:
        connection.execute("DELETE FROM v3_manual_action_current")
        other_field = StableIdentifier("field:forged-target")
        cross_digest = canonical_digest(
            {
                "schema_version": "strathmark-v3-manual-action-current-v1",
                "field_id": str(requirement.field_id),
                "requirement_digest": requirement.requirement_digest,
                "upstream_field_revision": requirement.upstream_field_revision,
                "updated_at": requirement.created_at,
            }
        )
        connection.execute(
            "INSERT INTO v3_manual_action_current VALUES (?, ?, ?, ?, ?)",
            (
                str(other_field),
                requirement.requirement_digest,
                requirement.upstream_field_revision,
                cross_digest,
                requirement.created_at,
            ),
        )
        connection.commit()
    with pytest.raises(ManualActionConflict, match="current"):
        store.current(other_field)


def test_connection_operations_roll_back_with_the_callers_writer_transaction(
    tmp_path: Path,
) -> None:
    signer, trust = _integrity()
    database = tmp_path / "manual-transaction-rollback.sqlite3"
    store = SQLiteManualActionRequirementStore(database, signer=signer, trust_store=trust)
    requirement = _requirement(signer)
    with pytest.raises(RuntimeError, match="abort publication"):
        with open_v3_connection(database) as connection:
            with immediate_transaction(connection):
                store.publish_connection(connection, requirement)
                raise RuntimeError("abort publication")
    assert store.current(requirement.field_id) is None

    requirement = store.publish(requirement)
    resolution = create_manual_action_resolution(
        requirement,
        receipt_id=StableIdentifier("receipt:rolled-back"),
        receipt_digest=canonical_digest({"receipt": "rolled-back"}),
        actor_id=StableIdentifier("actor:judge"),
        resolved_at="2026-08-24T22:00:04.000Z",
        signer=signer,
    )
    with pytest.raises(RuntimeError, match="abort resolution"):
        with open_v3_connection(database) as connection:
            with immediate_transaction(connection):
                store.resolve_connection(connection, requirement.binding, resolution)
                raise RuntimeError("abort resolution")
    assert store.current(requirement.field_id) == requirement


def test_schema_is_forward_only_and_history_rows_are_immutable(tmp_path: Path) -> None:
    database = tmp_path / "manual-schema.sqlite3"
    with open_v3_connection(database) as connection:
        migrate_connection(connection)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'v3_manual_action_%'"
            )
        }
        assert tables == {
            "v3_manual_action_requirements",
            "v3_manual_action_current",
            "v3_manual_action_resolutions",
        }
        for table in (
            "v3_manual_action_requirements",
            "v3_manual_action_resolutions",
        ):
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                (f"{table}_no_update",),
            ).fetchone()
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                (f"{table}_no_delete",),
            ).fetchone()
