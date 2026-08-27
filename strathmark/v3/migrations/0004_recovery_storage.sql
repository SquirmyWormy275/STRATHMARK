CREATE TABLE v3_outbox (
    outbox_id TEXT PRIMARY KEY,
    destination TEXT NOT NULL,
    source_global_sequence INTEGER REFERENCES v3_events(global_sequence),
    payload_json TEXT NOT NULL CHECK (length(CAST(payload_json AS BLOB)) <= 1048576),
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    state TEXT NOT NULL CHECK (state IN (
        'pending', 'transient', 'permanent', 'quarantined', 'repaired', 'acknowledged'
    )),
    revision INTEGER NOT NULL CHECK (revision > 0),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    next_attempt_at TEXT,
    terminal_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((state IN ('pending', 'transient', 'repaired') AND next_attempt_at IS NOT NULL)
        OR (state IN ('permanent', 'quarantined', 'acknowledged') AND next_attempt_at IS NULL))
);

CREATE INDEX v3_outbox_due_idx
ON v3_outbox(state, next_attempt_at, created_at, outbox_id);

CREATE TRIGGER v3_outbox_no_delete
BEFORE DELETE ON v3_outbox
BEGIN
    SELECT RAISE(ABORT, 'v3 outbox rows are append-preserved');
END;

CREATE TRIGGER v3_outbox_payload_immutable
BEFORE UPDATE ON v3_outbox
WHEN NEW.outbox_id != OLD.outbox_id
    OR NEW.destination != OLD.destination
    OR NEW.source_global_sequence IS NOT OLD.source_global_sequence
    OR NEW.payload_json != OLD.payload_json
    OR NEW.payload_digest != OLD.payload_digest
    OR NEW.created_at != OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'v3 outbox authority payload is immutable');
END;

CREATE TABLE v3_outbox_transitions (
    transition_sequence INTEGER PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    outbox_id TEXT NOT NULL REFERENCES v3_outbox(outbox_id),
    expected_revision INTEGER NOT NULL CHECK (expected_revision > 0),
    operation_kind TEXT NOT NULL CHECK (operation_kind IN (
        'transient', 'permanent', 'acknowledged', 'quarantine', 'repair'
    )),
    material_digest TEXT NOT NULL CHECK (length(material_digest) = 64),
    from_state TEXT NOT NULL,
    result_state TEXT NOT NULL,
    result_revision INTEGER NOT NULL CHECK (result_revision > 1),
    result_attempt_count INTEGER NOT NULL CHECK (result_attempt_count >= 0),
    result_next_attempt_at TEXT,
    result_terminal_reason TEXT,
    reason TEXT,
    observed_at TEXT NOT NULL,
    prior_transition_digest TEXT NOT NULL CHECK (length(prior_transition_digest) = 64),
    transition_digest TEXT NOT NULL UNIQUE CHECK (length(transition_digest) = 64),
    signed_transition_json TEXT NOT NULL CHECK (
        length(CAST(signed_transition_json AS BLOB)) <= 1048576
    )
);

CREATE INDEX v3_outbox_transition_history_idx
ON v3_outbox_transitions(outbox_id, transition_sequence);

CREATE TRIGGER v3_outbox_transitions_no_update
BEFORE UPDATE ON v3_outbox_transitions
BEGIN
    SELECT RAISE(ABORT, 'v3 outbox transition history is immutable');
END;

CREATE TRIGGER v3_outbox_transitions_no_delete
BEFORE DELETE ON v3_outbox_transitions
BEGIN
    SELECT RAISE(ABORT, 'v3 outbox transition history is immutable');
END;

CREATE TABLE v3_archive_index (
    tournament_id TEXT PRIMARY KEY,
    package_digest TEXT NOT NULL CHECK (length(package_digest) = 64),
    manifest_json TEXT NOT NULL CHECK (length(CAST(manifest_json AS BLOB)) <= 1048576),
    signed_manifest_digest TEXT NOT NULL CHECK (length(signed_manifest_digest) = 64),
    copy_one_identity TEXT NOT NULL,
    copy_two_identity TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    primary_removal_eligible INTEGER NOT NULL CHECK (primary_removal_eligible IN (0, 1))
);

CREATE TRIGGER v3_archive_index_no_update
BEFORE UPDATE ON v3_archive_index
BEGIN
    SELECT RAISE(ABORT, 'v3 archive proof index is immutable');
END;

CREATE TRIGGER v3_archive_index_no_delete
BEFORE DELETE ON v3_archive_index
BEGIN
    SELECT RAISE(ABORT, 'v3 archive proof index is immutable');
END;
