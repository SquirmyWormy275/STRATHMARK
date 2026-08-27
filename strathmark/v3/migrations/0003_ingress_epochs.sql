CREATE TABLE v3_ingress_snapshots (
    entity_kind TEXT NOT NULL CHECK (entity_kind IN ('tournament', 'round', 'field')),
    entity_id TEXT NOT NULL,
    upstream_revision INTEGER NOT NULL CHECK (upstream_revision > 0),
    tournament_id TEXT NOT NULL,
    round_id TEXT,
    snapshot_json TEXT NOT NULL CHECK (length(CAST(snapshot_json AS BLOB)) <= 1048576),
    snapshot_digest TEXT NOT NULL CHECK (length(snapshot_digest) = 64),
    source_global_sequence INTEGER NOT NULL UNIQUE REFERENCES v3_events(global_sequence)
        CHECK (source_global_sequence > 0),
    PRIMARY KEY (entity_kind, entity_id, upstream_revision)
);

CREATE TABLE v3_result_revisions (
    result_key TEXT NOT NULL,
    tournament_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    source_global_sequence INTEGER NOT NULL UNIQUE REFERENCES v3_events(global_sequence)
        CHECK (source_global_sequence > 0),
    round_id TEXT NOT NULL,
    field_id TEXT NOT NULL,
    competitor_id TEXT NOT NULL,
    field_revision INTEGER NOT NULL CHECK (field_revision > 0),
    claimed_receipt_id TEXT NOT NULL,
    observation_json TEXT NOT NULL CHECK (length(CAST(observation_json AS BLOB)) <= 1048576),
    observation_digest TEXT NOT NULL CHECK (length(observation_digest) = 64),
    candidate_numeric_eligible INTEGER NOT NULL CHECK (candidate_numeric_eligible IN (0, 1)),
    numeric_eligible INTEGER NOT NULL CHECK (numeric_eligible IN (0, 1)),
    admission_reason TEXT NOT NULL,
    settled_global_sequence INTEGER REFERENCES v3_events(global_sequence),
    PRIMARY KEY (result_key, revision)
);

CREATE INDEX idx_v3_result_revisions_source ON v3_result_revisions(source_global_sequence);
CREATE INDEX idx_v3_result_revisions_round ON v3_result_revisions(round_id, source_global_sequence);
CREATE INDEX idx_v3_result_revisions_tournament_active
    ON v3_result_revisions(tournament_id, settled_global_sequence, result_key, revision);

CREATE TABLE v3_derivation_reactions (
    source_global_sequence INTEGER NOT NULL REFERENCES v3_events(global_sequence)
        CHECK (source_global_sequence > 0),
    reaction_type TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'completed')),
    output_digest TEXT CHECK (output_digest IS NULL OR length(output_digest) = 64),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (source_global_sequence, reaction_type, state),
    CHECK ((state = 'pending' AND output_digest IS NULL) OR
           (state = 'completed' AND output_digest IS NOT NULL))
);

CREATE TABLE v3_derivation_barrier (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    through_global_sequence INTEGER NOT NULL CHECK (through_global_sequence >= 0),
    barrier_digest TEXT NOT NULL CHECK (length(barrier_digest) = 64)
);

CREATE TABLE v3_derivation_sequence_completions (
    source_global_sequence INTEGER PRIMARY KEY REFERENCES v3_events(global_sequence),
    completion_global_sequence INTEGER NOT NULL UNIQUE REFERENCES v3_events(global_sequence),
    completion_digest TEXT NOT NULL CHECK (length(completion_digest) = 64)
);

INSERT INTO v3_derivation_barrier(singleton, through_global_sequence, barrier_digest)
VALUES (1, 0, '103f2d95d69369cb8160a2ee00ba1f7804b28a2e5d43e0c8f8768cc48a365c83');

CREATE TABLE v3_evidence_epochs (
    epoch_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    epoch_revision INTEGER NOT NULL CHECK (epoch_revision > 0),
    maximum_tournament_sequence INTEGER NOT NULL CHECK (maximum_tournament_sequence >= 0),
    historical_cutoff_key TEXT NOT NULL,
    epoch_json TEXT NOT NULL CHECK (length(CAST(epoch_json AS BLOB)) <= 1048576),
    epoch_digest TEXT NOT NULL UNIQUE CHECK (length(epoch_digest) = 64),
    frozen_global_sequence INTEGER NOT NULL UNIQUE REFERENCES v3_events(global_sequence),
    frozen_at TEXT NOT NULL,
    UNIQUE (round_id, epoch_revision)
);

CREATE TABLE v3_round_closures (
    closure_id TEXT PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    source_round_id TEXT NOT NULL,
    target_round_ids_json TEXT NOT NULL,
    closure_global_sequence INTEGER NOT NULL UNIQUE REFERENCES v3_events(global_sequence),
    result_set_json TEXT NOT NULL CHECK (length(CAST(result_set_json AS BLOB)) <= 1048576),
    result_set_digest TEXT NOT NULL CHECK (length(result_set_digest) = 64),
    closed_at TEXT NOT NULL,
    UNIQUE (source_round_id, closure_global_sequence)
);

CREATE TABLE v3_evidence_epoch_members (
    epoch_id TEXT NOT NULL REFERENCES v3_evidence_epochs(epoch_id),
    result_key TEXT NOT NULL,
    result_revision INTEGER NOT NULL CHECK (result_revision > 0),
    source_global_sequence INTEGER NOT NULL REFERENCES v3_events(global_sequence)
        CHECK (source_global_sequence > 0),
    numeric_eligible INTEGER NOT NULL CHECK (numeric_eligible IN (0, 1)),
    PRIMARY KEY (epoch_id, result_key)
);

CREATE TABLE v3_round_issue_seals (
    round_id TEXT PRIMARY KEY,
    epoch_id TEXT NOT NULL,
    first_issue_global_sequence INTEGER NOT NULL UNIQUE REFERENCES v3_events(global_sequence)
        CHECK (first_issue_global_sequence > 0),
    sealed_at TEXT NOT NULL
);

CREATE TABLE v3_prepared_field_dependencies (
    field_id TEXT NOT NULL,
    field_revision INTEGER NOT NULL CHECK (field_revision > 0),
    round_id TEXT NOT NULL,
    epoch_id TEXT NOT NULL,
    prepared_global_sequence INTEGER NOT NULL REFERENCES v3_events(global_sequence)
        CHECK (prepared_global_sequence > 0),
    invalidated_by_sequence INTEGER REFERENCES v3_events(global_sequence),
    PRIMARY KEY (field_id, field_revision),
    CHECK (invalidated_by_sequence IS NULL OR invalidated_by_sequence > prepared_global_sequence)
);
