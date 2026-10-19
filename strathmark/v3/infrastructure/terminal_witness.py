"""Required external, authenticated monotonic state; no filesystem fallback.

An installation supplies a durable, linearizable implementation whose credentials
and rollback protection are independent of the database and registry files. A
successful compare-and-swap must survive process/machine restart. Missing scopes
are errors: only an explicit provisioning operation may create generation zero.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from strathmark.v3.contracts.canonical import canonical_bytes


class WitnessError(RuntimeError):
    """Independent current state cannot be established."""


@dataclass(frozen=True, slots=True)
class WitnessState:
    generation: int
    value: bytes

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 0:
            raise WitnessError("witness generation is invalid")
        if not isinstance(self.value, bytes) or (self.generation == 0 and self.value):
            raise WitnessError("witness state is invalid")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.value).hexdigest()


class MonotonicWitness(Protocol):
    """Authenticated remote/WORM-backed service; never a colocated sidecar.

    read must return the latest committed state, not an eventually consistent
    replica or cached prefix. CAS must atomically reject stale expectations and
    enforce exactly one generation advance. Provision/reset privileges must not
    be available to the runtime service or a database/registry-file writer.
    """

    def read(self, scope: str) -> WitnessState: ...

    def compare_and_swap(
        self, scope: str, expected: WitnessState, proposed: WitnessState
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class WitnessBinding:
    witness: MonotonicWitness
    scope: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, str) or not self.scope.strip():
            raise WitnessError("a provisioned witness scope is required")
        if not callable(getattr(self.witness, "read", None)) or not callable(
            getattr(self.witness, "compare_and_swap", None)
        ):
            raise WitnessError("an independent monotonic witness is required")

    def read(self) -> WitnessState:
        try:
            state = self.witness.read(self.scope)
        except Exception as exc:
            raise WitnessError("independent terminal witness is unavailable") from exc
        if not isinstance(state, WitnessState):
            raise WitnessError("independent terminal witness returned invalid state")
        return state

    def require(self, expected: WitnessState) -> None:
        if self.read() != expected:
            raise WitnessError("independent terminal witness differs: rollback or fork")

    def advance(self, expected: WitnessState, proposed: WitnessState) -> None:
        if proposed.generation != expected.generation + 1:
            raise WitnessError("terminal witness must advance exactly one generation")
        try:
            accepted = self.witness.compare_and_swap(self.scope, expected, proposed)
        except Exception as exc:
            raise WitnessError(
                "terminal witness publication is uncertain; recovery required"
            ) from exc
        if accepted is not True:
            raise WitnessError("terminal witness changed concurrently; recovery required")
        self.require(proposed)


def registry_state(sequence: int, digest: str) -> WitnessState:
    return WitnessState(
        sequence,
        b"" if sequence == 0 else canonical_bytes({"sequence": sequence, "digest": digest}),
    )
