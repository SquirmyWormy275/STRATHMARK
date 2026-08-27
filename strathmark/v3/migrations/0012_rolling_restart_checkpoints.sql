CREATE TABLE v3_rolling_restart_checkpoints (
    checkpoint_sequence INTEGER PRIMARY KEY CHECK (checkpoint_sequence > 0),
    prior_checkpoint_digest TEXT NOT NULL CHECK (length(prior_checkpoint_digest) = 64),
    capacity_manifest_digest TEXT NOT NULL CHECK (length(capacity_manifest_digest) = 64),
    source_global_sequence INTEGER NOT NULL CHECK (source_global_sequence >= 0),
    source_event_digest TEXT NOT NULL CHECK (length(source_event_digest) = 64),
    aggregate_heads_json TEXT NOT NULL CHECK (
        length(CAST(aggregate_heads_json AS BLOB)) <= 1048576
    ),
    aggregate_head_count INTEGER NOT NULL CHECK (aggregate_head_count >= 0),
    aggregate_heads_digest TEXT NOT NULL CHECK (length(aggregate_heads_digest) = 64),
    reaction_cursor_digest TEXT NOT NULL CHECK (length(reaction_cursor_digest) = 64),
    reaction_cursor_revision INTEGER NOT NULL CHECK (reaction_cursor_revision >= 0),
    reaction_relevant_command_count INTEGER NOT NULL CHECK (
        reaction_relevant_command_count >= 0
    ),
    reaction_latest_reaction_id TEXT NOT NULL CHECK (
        length(reaction_latest_reaction_id) = 64
    ),
    job_history_sequence INTEGER NOT NULL CHECK (job_history_sequence >= 0),
    job_history_digest TEXT NOT NULL CHECK (length(job_history_digest) = 64),
    status_sequence INTEGER NOT NULL CHECK (status_sequence >= 0),
    status_digest TEXT NOT NULL CHECK (length(status_digest) = 64),
    current_subjects_json TEXT NOT NULL CHECK (
        length(CAST(current_subjects_json AS BLOB)) <= 1048576
    ),
    current_subject_count INTEGER NOT NULL CHECK (current_subject_count >= 0),
    current_subject_digest TEXT NOT NULL CHECK (length(current_subject_digest) = 64),
    active_job_count INTEGER NOT NULL CHECK (active_job_count >= 0),
    active_job_digest TEXT NOT NULL CHECK (length(active_job_digest) = 64),
    pending_reactions_json TEXT NOT NULL CHECK (
        length(CAST(pending_reactions_json AS BLOB)) <= 1048576
    ),
    pending_reaction_count INTEGER NOT NULL CHECK (pending_reaction_count >= 0),
    pending_reaction_digest TEXT NOT NULL CHECK (length(pending_reaction_digest) = 64),
    checkpoint_digest TEXT NOT NULL UNIQUE CHECK (length(checkpoint_digest) = 64),
    checkpoint_manifest_json TEXT NOT NULL CHECK (
        length(CAST(checkpoint_manifest_json AS BLOB)) <= 65536
    ),
    created_at TEXT NOT NULL
);

CREATE TABLE v3_rolling_restart_tip (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    checkpoint_sequence INTEGER NOT NULL UNIQUE,
    checkpoint_digest TEXT NOT NULL UNIQUE CHECK (length(checkpoint_digest) = 64),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (checkpoint_sequence)
        REFERENCES v3_rolling_restart_checkpoints(checkpoint_sequence)
);

CREATE TABLE v3_rolling_reaction_cursor (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    cursor_revision INTEGER NOT NULL CHECK (cursor_revision >= 0),
    through_global_sequence INTEGER NOT NULL CHECK (through_global_sequence >= 0),
    through_event_digest TEXT NOT NULL CHECK (length(through_event_digest) = 64),
    relevant_command_count INTEGER NOT NULL CHECK (relevant_command_count >= 0),
    latest_reaction_id TEXT NOT NULL CHECK (length(latest_reaction_id) = 64),
    cursor_digest TEXT NOT NULL CHECK (length(cursor_digest) = 64),
    updated_at TEXT NOT NULL
);

INSERT INTO v3_rolling_reaction_cursor VALUES (
    1,
    0,
    0,
    '0000000000000000000000000000000000000000000000000000000000000000',
    0,
    '0000000000000000000000000000000000000000000000000000000000000000',
    'f5f55f7db76dee16f31b14c9c64824c0f4bab2514c0826a26e5c6a8b6821062b',
    '1970-01-01T00:00:00.000Z'
);

CREATE INDEX v3_jobs_rolling_card_component_idx
ON v3_jobs(
    json_extract(payload_json, '$.card_key.card_digest'),
    CAST(json_extract(payload_json, '$.component_ordinal') AS INTEGER),
    job_id,
    job_revision
)
WHERE json_extract(payload_json, '$.schema_version') =
    'strathmark-v3-rolling-component-job-v1';

CREATE INDEX v3_jobs_rolling_subject_revision_idx
ON v3_jobs(
    json_extract(payload_json, '$.card_key.competitor_id'),
    json_extract(payload_json, '$.card_key.target_context_digest'),
    CAST(json_extract(payload_json, '$.card_key.dependency_revision') AS INTEGER) DESC,
    job_id,
    job_revision
)
WHERE json_extract(payload_json, '$.schema_version') =
    'strathmark-v3-rolling-component-job-v1';

CREATE INDEX v3_jobs_rolling_epoch_state_idx
ON v3_jobs(
    json_extract(payload_json, '$.card_key.tournament_epoch_id'),
    state,
    job_id,
    job_revision
)
WHERE json_extract(payload_json, '$.schema_version') =
    'strathmark-v3-rolling-component-job-v1';

CREATE INDEX v3_jobs_rolling_recombination_epoch_state_idx
ON v3_jobs(
    json_extract(payload_json, '$.tournament_epoch_id'),
    state,
    job_id,
    job_revision
)
WHERE json_extract(payload_json, '$.schema_version') =
    'strathmark-v3-weight-only-recombination-v1';

CREATE INDEX v3_rolling_status_publication_idx
ON v3_rolling_card_status_history(publication_digest, status_sequence DESC);

CREATE TRIGGER v3_rolling_restart_checkpoints_no_update
BEFORE UPDATE ON v3_rolling_restart_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'rolling restart checkpoints are immutable');
END;

CREATE TRIGGER v3_rolling_restart_checkpoints_no_delete
BEFORE DELETE ON v3_rolling_restart_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'rolling restart checkpoints are immutable');
END;
