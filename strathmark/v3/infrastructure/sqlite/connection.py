"""Versioned SQLite connection and transaction policy for V3.

The module opens a connection only when explicitly called.  It deliberately
keeps writer transactions as a tiny context boundary: callers must finish
inference, network I/O, blob encoding, and report construction before entering
``immediate_transaction``.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Iterator, Optional, Type


class SQLitePolicyError(ValueError):
    """A connection policy or transaction use is unsafe."""


class SQLiteDeadlineExceeded(TimeoutError):
    """A bounded SQLite operation expired or was cancelled."""


class ClosingConnection(sqlite3.Connection):
    """Match sqlite transaction context behavior and always close the handle."""

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


@dataclass(frozen=True, slots=True)
class SQLiteConnectionPolicy:
    """One explicit, replayable race-day SQLite policy."""

    version: str = "strathmark-v3-sqlite-policy-v1"
    journal_mode: str = "WAL"
    synchronous: str = "FULL"
    busy_timeout_ms: int = 5_000
    wal_autocheckpoint_pages: int = 256
    progress_opcode_interval: int = 1_000
    checkpoint_mode: str = "PASSIVE"

    def __post_init__(self) -> None:
        if self.version != "strathmark-v3-sqlite-policy-v1":
            raise SQLitePolicyError("unsupported SQLite connection policy version")
        if self.journal_mode != "WAL" or self.synchronous != "FULL":
            raise SQLitePolicyError("V3 requires WAL journal mode and FULL synchronous durability")
        if (
            isinstance(self.busy_timeout_ms, bool)
            or not isinstance(self.busy_timeout_ms, int)
            or not 1 <= self.busy_timeout_ms <= 30_000
        ):
            raise SQLitePolicyError("busy timeout must be between 1 and 30000 milliseconds")
        if (
            isinstance(self.wal_autocheckpoint_pages, bool)
            or not isinstance(self.wal_autocheckpoint_pages, int)
            or not 1 <= self.wal_autocheckpoint_pages <= 10_000
        ):
            raise SQLitePolicyError("WAL autocheckpoint must be between 1 and 10000 pages")
        if (
            isinstance(self.progress_opcode_interval, bool)
            or not isinstance(self.progress_opcode_interval, int)
            or not 1 <= self.progress_opcode_interval <= 100_000
        ):
            raise SQLitePolicyError("progress opcode interval must be between 1 and 100000")
        if self.checkpoint_mode != "PASSIVE":
            raise SQLitePolicyError("online checkpoints are bounded to PASSIVE mode")


DEFAULT_CONNECTION_POLICY = SQLiteConnectionPolicy()


class SQLiteDeadline:
    """Cooperative monotonic deadline/cancellation state for SQLite reads."""

    def __init__(self, *, timeout_seconds: float) -> None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise SQLitePolicyError("timeout_seconds must be a numeric value, not a coercion")
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
            raise SQLitePolicyError("timeout_seconds must be finite, positive, and at most 60")
        self._expires_at = time.monotonic() + timeout
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        if time.monotonic() >= self._expires_at:
            self._cancelled.set()
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()

    def remaining_milliseconds(self) -> int:
        self.raise_if_expired()
        return max(1, min(60_000, math.ceil((self._expires_at - time.monotonic()) * 1000)))

    def progress_handler(self) -> int:
        return 1 if self.cancelled else 0

    def raise_if_expired(self) -> None:
        if self.cancelled:
            raise SQLiteDeadlineExceeded("SQLite operation deadline expired or was cancelled")


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    """Bounded SQLite WAL checkpoint outcome."""

    mode: str
    busy: int
    log_frames: int
    checkpointed_frames: int


def open_v3_connection(
    database_path: Path | str,
    *,
    policy: SQLiteConnectionPolicy = DEFAULT_CONNECTION_POLICY,
    read_only: bool = False,
    deadline: SQLiteDeadline | None = None,
) -> sqlite3.Connection:
    """Open one explicitly configured V3 connection.

    Writable opens create only the database parent directory.  Read-only opens
    use SQLite's ``mode=ro`` URI, never an advisory application convention.
    """

    if not isinstance(policy, SQLiteConnectionPolicy):
        raise SQLitePolicyError("policy must be a SQLiteConnectionPolicy")
    if isinstance(database_path, bool) or not isinstance(database_path, (str, Path)):
        raise SQLitePolicyError("database_path must be a filesystem path")
    raw_target = str(database_path)
    if raw_target == ":memory:" or raw_target.casefold().startswith("file::memory:"):
        raise SQLitePolicyError("V3 authority cannot use an in-memory database")
    if not isinstance(read_only, bool):
        raise SQLitePolicyError("read_only must be an explicit boolean")
    if deadline is not None and not isinstance(deadline, SQLiteDeadline):
        raise SQLitePolicyError("deadline must be a SQLiteDeadline")
    path = Path(database_path).expanduser().resolve(strict=False)
    if deadline is not None:
        deadline.raise_if_expired()
        timeout_ms = min(policy.busy_timeout_ms, deadline.remaining_milliseconds())
    else:
        timeout_ms = policy.busy_timeout_ms

    if read_only:
        if not path.is_file():
            raise FileNotFoundError(path)
        target = f"{path.as_uri()}?mode=ro"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        target = str(path)

    connection = sqlite3.connect(
        target,
        uri=read_only,
        isolation_level=None,
        timeout=timeout_ms / 1000,
        factory=ClosingConnection,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        if read_only:
            connection.execute("PRAGMA query_only = ON")
        else:
            observed_mode = str(
                connection.execute(f"PRAGMA journal_mode = {policy.journal_mode}").fetchone()[0]
            ).upper()
            if observed_mode != policy.journal_mode:
                raise SQLitePolicyError(
                    f"SQLite refused required journal mode {policy.journal_mode}"
                )
            connection.execute(f"PRAGMA synchronous = {policy.synchronous}")
            connection.execute(f"PRAGMA wal_autocheckpoint = {policy.wal_autocheckpoint_pages}")
        if deadline is not None:
            connection.set_progress_handler(
                deadline.progress_handler, policy.progress_opcode_interval
            )
            deadline.raise_if_expired()
        return connection
    except BaseException:
        connection.close()
        raise


@contextmanager
def immediate_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a short, explicit ``BEGIN IMMEDIATE`` transaction."""

    if connection.in_transaction:
        raise SQLitePolicyError("nested V3 writer transactions are forbidden")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def bounded_checkpoint(
    connection: sqlite3.Connection,
    *,
    policy: SQLiteConnectionPolicy = DEFAULT_CONNECTION_POLICY,
) -> CheckpointResult:
    """Run the policy's non-blocking online checkpoint and return its counters."""

    if connection.in_transaction:
        raise SQLitePolicyError("checkpoint cannot run inside a writer transaction")
    row = connection.execute(f"PRAGMA wal_checkpoint({policy.checkpoint_mode})").fetchone()
    if row is None or len(row) != 3:
        raise SQLitePolicyError("SQLite returned a malformed checkpoint result")
    return CheckpointResult(
        mode=policy.checkpoint_mode,
        busy=int(row[0]),
        log_frames=int(row[1]),
        checkpointed_frames=int(row[2]),
    )


__all__ = [
    "CheckpointResult",
    "DEFAULT_CONNECTION_POLICY",
    "SQLiteConnectionPolicy",
    "SQLiteDeadline",
    "SQLiteDeadlineExceeded",
    "SQLitePolicyError",
    "bounded_checkpoint",
    "immediate_transaction",
    "open_v3_connection",
]
