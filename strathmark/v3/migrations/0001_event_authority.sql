CREATE TABLE v3_events (
    global_sequence INTEGER PRIMARY KEY CHECK (global_sequence > 0),
    event_id TEXT NOT NULL UNIQUE,
    aggregate_kind TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
    event_kind TEXT NOT NULL,
    envelope_json TEXT NOT NULL CHECK (length(CAST(envelope_json AS BLOB)) <= 1048576),
    event_digest TEXT NOT NULL UNIQUE CHECK (length(event_digest) = 64),
    prior_global_digest TEXT NOT NULL CHECK (length(prior_global_digest) = 64),
    prior_aggregate_digest TEXT NOT NULL CHECK (length(prior_aggregate_digest) = 64),
    occurred_at_utc TEXT NOT NULL,
    command_id TEXT NOT NULL,
    source_import_id TEXT,
    training_eligible INTEGER NOT NULL DEFAULT 0 CHECK (training_eligible IN (0, 1)),
    UNIQUE (aggregate_kind, aggregate_id, aggregate_version)
);

CREATE INDEX idx_v3_events_aggregate
    ON v3_events(aggregate_kind, aggregate_id, aggregate_version);
CREATE INDEX idx_v3_events_kind
    ON v3_events(event_kind, global_sequence);

CREATE TABLE v3_aggregate_heads (
    aggregate_kind TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
    event_digest TEXT,
    PRIMARY KEY (aggregate_kind, aggregate_id)
);

CREATE TABLE v3_idempotency_records (
    principal_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    command_digest TEXT NOT NULL CHECK (length(command_digest) = 64),
    result_schema_version TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK (length(CAST(result_json AS BLOB)) <= 1048576),
    result_digest TEXT NOT NULL CHECK (length(result_digest) = 64),
    first_global_sequence INTEGER NOT NULL REFERENCES v3_events(global_sequence),
    last_global_sequence INTEGER NOT NULL REFERENCES v3_events(global_sequence),
    event_set_digest TEXT NOT NULL CHECK (length(event_set_digest) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, idempotency_key),
    CHECK (first_global_sequence <= last_global_sequence)
);

CREATE TRIGGER v3_events_no_update
BEFORE UPDATE ON v3_events BEGIN SELECT RAISE(ABORT, 'append-only authority'); END;
CREATE TRIGGER v3_events_no_delete
BEFORE DELETE ON v3_events BEGIN SELECT RAISE(ABORT, 'append-only authority'); END;
CREATE TRIGGER v3_idempotency_records_no_update
BEFORE UPDATE ON v3_idempotency_records
BEGIN SELECT RAISE(ABORT, 'append-only authority'); END;
CREATE TRIGGER v3_idempotency_records_no_delete
BEFORE DELETE ON v3_idempotency_records
BEGIN SELECT RAISE(ABORT, 'append-only authority'); END;
