"""Transactional operational state, append-only command receipts and restore fence."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from strathmark.private_storage import open_private_exclusive, private_file, reject_links

from .contracts import CONTRACT, EventError, digest, encode


def sync_directory(path):
    if hasattr(os, "O_DIRECTORY"):
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def publish_file_exclusive(source, destination):
    # Staging and destination share a filesystem. link() atomically refuses
    # an existing target; neither a concurrent authority nor its head is replaced.
    private_file(source)
    destination = reject_links(destination)
    os.link(source, destination)
    sync_directory(Path(destination).parent)


def atomic_json(path, value, *, replace=True):
    path = reject_links(path)
    if path.exists():
        private_file(path)
    name = path.with_name(".event-head-" + uuid4().hex)
    fd = open_private_exclusive(name)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(encode(value))
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(name, path)
        else:
            os.link(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class EventStore:
    def __init__(self, path):
        self.path = reject_links(path).resolve()
        self.head = self.path.with_suffix(self.path.suffix + ".head.json")
        existed = self.path.exists()
        if existed and not self.head.exists():
            raise EventError(
                "authority_head_missing: restore requires independent current-head proof"
            )
        if not existed and self.head.exists():
            raise EventError("authority_database_missing")
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, show_id TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS roots (
                    show_id TEXT NOT NULL, tournament_id TEXT NOT NULL,
                    selection TEXT NOT NULL, PRIMARY KEY(show_id,tournament_id));
                CREATE TABLE IF NOT EXISTS commands (
                    sequence INTEGER PRIMARY KEY, event_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                    request_id TEXT NOT NULL, binding TEXT NOT NULL, response TEXT NOT NULL,
                    previous_tip TEXT NOT NULL, tip TEXT NOT NULL UNIQUE,
                    UNIQUE(event_id, actor_id, request_id));
            """)
            schema = db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
            if schema and schema[0] != CONTRACT:
                raise EventError("incompatible_event_storage")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema', ?)", (CONTRACT,))
            db.commit()
            if not existed:
                atomic_json(self.head, {"tip": self.tip(db), "database_path": str(self.path)})
            self.verify(db)

    @contextmanager
    def connection(self):
        private_file(self.path)
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @staticmethod
    def tip(db):
        row = db.execute("SELECT tip FROM commands ORDER BY sequence DESC LIMIT 1").fetchone()
        return row[0] if row else "0" * 64

    def verify(self, db):
        try:
            reject_links(self.head)
            if not self.head.is_file():
                raise FileNotFoundError(self.head)
            private_file(self.head)
            head = json.loads(self.head.read_text())
            tip = self.tip(db)
            if head.get("database_path") != str(self.path):
                raise EventError("authority_location_changed: explicit fenced transfer required")
            if tip not in {head["tip"], head.get("pending_tip")}:
                raise EventError(
                    "stale_authority: database differs from independently retained head"
                )
            previous = "0" * 64
            for (
                sequence,
                event_id,
                actor_id,
                request_id,
                binding,
                response,
                prior,
                recorded,
            ) in db.execute("SELECT * FROM commands ORDER BY sequence"):
                actual = digest(
                    [sequence, event_id, actor_id, request_id, binding, response, previous]
                )
                if prior != previous or recorded != actual:
                    raise EventError("corrupt_command_chain")
                previous = recorded
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise EventError("corrupt_event_storage")
            for event_id, state in db.execute("SELECT event_id,state FROM events"):
                latest = db.execute(
                    "SELECT response FROM commands WHERE event_id=? ORDER BY sequence DESC LIMIT 1",
                    (event_id,),
                ).fetchone()
                if not latest or latest[0] != state:
                    raise EventError("corrupt_event_projection")
                snapshot = json.loads(state)
                root = db.execute(
                    "SELECT selection FROM roots WHERE show_id=? AND tournament_id=?",
                    (snapshot["show_id"], snapshot["config"]["tournament_id"]),
                ).fetchone()
                if not root or root[0] != encode(snapshot["root_selection"]):
                    raise EventError("corrupt_competition_root_selection")
            if "pending_tip" in head:
                atomic_json(self.head, {"tip": tip, "database_path": str(self.path)})
        except (OSError, KeyError, TypeError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
            raise EventError("invalid_authority_head_or_storage") from exc

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self.verify(db)
                yield db
                db.commit()
                atomic_json(self.head, {"tip": self.tip(db), "database_path": str(self.path)})
            except BaseException:
                db.rollback()
                # A pending head records both possible atomic outcomes. The
                # next open reconciles it against the actual committed tip.
                raise

    def append(self, db, event_id, actor_id, request_id, binding, state):
        previous = self.tip(db)
        sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM commands").fetchone()[0]
        response = encode(state)
        tip = digest([sequence, event_id, actor_id, request_id, binding, response, previous])
        atomic_json(
            self.head, {"tip": previous, "pending_tip": tip, "database_path": str(self.path)}
        )
        db.execute(
            "INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)",
            (sequence, event_id, actor_id, request_id, binding, response, previous, tip),
        )
        db.execute(
            "INSERT INTO events VALUES (?,?,?) ON CONFLICT(event_id) DO UPDATE SET state=excluded.state",
            (event_id, state["show_id"], response),
        )

    @staticmethod
    def retry(db, event_id, actor_id, request_id, binding):
        row = db.execute(
            "SELECT binding,response FROM commands WHERE event_id=? AND actor_id=? AND request_id=?",
            (event_id, actor_id, request_id),
        ).fetchone()
        if row:
            if row[0] != binding:
                raise EventError("request_conflict: changed payload under accepted identity")
            return json.loads(row[1])

    @staticmethod
    def read(db, event_id):
        row = db.execute("SELECT state FROM events WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            raise EventError("event_not_found")
        return json.loads(row[0])
