CREATE TABLE v3_job_specs (
    job_id TEXT NOT NULL,
    job_revision INTEGER NOT NULL CHECK (job_revision > 0),
    spec_json TEXT NOT NULL CHECK (length(CAST(spec_json AS BLOB)) <= 1048576),
    spec_digest TEXT NOT NULL UNIQUE CHECK (length(spec_digest) = 64),
    spec_manifest_json TEXT NOT NULL CHECK (
        length(CAST(spec_manifest_json AS BLOB)) <= 2097152
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, job_revision)
);

CREATE TABLE v3_job_spec_cutovers (
    cutover_sequence INTEGER PRIMARY KEY CHECK (cutover_sequence = 1),
    legacy_history_sequence INTEGER NOT NULL CHECK (legacy_history_sequence > 0),
    legacy_history_digest TEXT NOT NULL CHECK (length(legacy_history_digest) = 64),
    job_spec_count INTEGER NOT NULL CHECK (job_spec_count > 0),
    job_spec_root_digest TEXT NOT NULL CHECK (length(job_spec_root_digest) = 64),
    cutover_digest TEXT NOT NULL UNIQUE CHECK (length(cutover_digest) = 64),
    cutover_manifest_json TEXT NOT NULL CHECK (
        length(CAST(cutover_manifest_json AS BLOB)) <= 2097152
    ),
    created_at TEXT NOT NULL
);

ALTER TABLE v3_job_history ADD COLUMN job_spec_digest TEXT NOT NULL DEFAULT
    '0000000000000000000000000000000000000000000000000000000000000000'
    CHECK (length(job_spec_digest) = 64);

ALTER TABLE v3_rolling_restart_checkpoints ADD COLUMN
    absorbed_delta_sequence INTEGER NOT NULL DEFAULT 0
    CHECK (absorbed_delta_sequence >= 0);
ALTER TABLE v3_rolling_restart_checkpoints ADD COLUMN
    absorbed_delta_digest TEXT NOT NULL DEFAULT
    '0000000000000000000000000000000000000000000000000000000000000000'
    CHECK (length(absorbed_delta_digest) = 64);

DROP TRIGGER v3_jobs_spec_immutable;
DROP TRIGGER v3_jobs_no_delete;

CREATE TABLE v3_rolling_restart_deltas (
    delta_sequence INTEGER PRIMARY KEY CHECK (delta_sequence > 0),
    prior_delta_digest TEXT NOT NULL CHECK (length(prior_delta_digest) = 64),
    base_checkpoint_sequence INTEGER NOT NULL CHECK (base_checkpoint_sequence > 0),
    operation_kind TEXT NOT NULL,
    authority_kind TEXT NOT NULL,
    authority_sequence INTEGER NOT NULL CHECK (authority_sequence >= 0),
    authority_digest TEXT NOT NULL CHECK (length(authority_digest) = 64),
    delta_digest TEXT NOT NULL UNIQUE CHECK (length(delta_digest) = 64),
    delta_manifest_json TEXT NOT NULL CHECK (
        length(CAST(delta_manifest_json AS BLOB)) <= 16384
    ),
    created_at TEXT NOT NULL,
    FOREIGN KEY (base_checkpoint_sequence)
        REFERENCES v3_rolling_restart_checkpoints(checkpoint_sequence)
);

CREATE TABLE v3_rolling_restart_delta_tip (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    delta_sequence INTEGER NOT NULL UNIQUE CHECK (delta_sequence > 0),
    delta_digest TEXT NOT NULL UNIQUE CHECK (length(delta_digest) = 64),
    base_checkpoint_sequence INTEGER NOT NULL CHECK (base_checkpoint_sequence > 0),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (delta_sequence)
        REFERENCES v3_rolling_restart_deltas(delta_sequence),
    FOREIGN KEY (base_checkpoint_sequence)
        REFERENCES v3_rolling_restart_checkpoints(checkpoint_sequence)
);

CREATE TABLE v3_rolling_restart_aggregate_heads (
    checkpoint_sequence INTEGER NOT NULL CHECK (checkpoint_sequence > 0),
    aggregate_kind TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
    event_digest TEXT NOT NULL CHECK (length(event_digest) = 64),
    lifecycle_status TEXT,
    PRIMARY KEY (checkpoint_sequence, aggregate_kind, aggregate_id),
    FOREIGN KEY (checkpoint_sequence)
        REFERENCES v3_rolling_restart_checkpoints(checkpoint_sequence)
);

CREATE TABLE v3_rolling_restart_pending_reactions (
    checkpoint_sequence INTEGER NOT NULL CHECK (checkpoint_sequence > 0),
    reaction_id TEXT NOT NULL CHECK (length(reaction_id) = 64),
    first_global_sequence INTEGER NOT NULL CHECK (first_global_sequence > 0),
    last_global_sequence INTEGER NOT NULL CHECK (
        last_global_sequence >= first_global_sequence
    ),
    event_set_digest TEXT NOT NULL CHECK (length(event_set_digest) = 64),
    PRIMARY KEY (checkpoint_sequence, reaction_id),
    FOREIGN KEY (checkpoint_sequence)
        REFERENCES v3_rolling_restart_checkpoints(checkpoint_sequence)
);

CREATE TABLE v3_rolling_restart_current_subjects (
    checkpoint_sequence INTEGER NOT NULL CHECK (checkpoint_sequence > 0),
    competitor_id TEXT NOT NULL,
    target_context_digest TEXT NOT NULL CHECK (length(target_context_digest) = 64),
    tournament_epoch_id TEXT NOT NULL,
    publication_digest TEXT NOT NULL CHECK (length(publication_digest) = 64),
    dependency_revision INTEGER NOT NULL CHECK (dependency_revision > 0),
    status_digest TEXT NOT NULL CHECK (length(status_digest) = 64),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (checkpoint_sequence, competitor_id, target_context_digest),
    FOREIGN KEY (checkpoint_sequence)
        REFERENCES v3_rolling_restart_checkpoints(checkpoint_sequence)
);

CREATE INDEX v3_rolling_restart_delta_base_idx
ON v3_rolling_restart_deltas(base_checkpoint_sequence, delta_sequence);

CREATE TRIGGER v3_job_specs_no_update
BEFORE UPDATE ON v3_job_specs
BEGIN
    SELECT RAISE(ABORT, 'job specs are immutable');
END;

CREATE TRIGGER v3_job_specs_no_delete
BEFORE DELETE ON v3_job_specs
BEGIN
    SELECT RAISE(ABORT, 'job specs are immutable');
END;

CREATE TRIGGER v3_job_spec_cutovers_no_update
BEFORE UPDATE ON v3_job_spec_cutovers
BEGIN
    SELECT RAISE(ABORT, 'job spec cutovers are immutable');
END;

CREATE TRIGGER v3_job_spec_cutovers_no_delete
BEFORE DELETE ON v3_job_spec_cutovers
BEGIN
    SELECT RAISE(ABORT, 'job spec cutovers are immutable');
END;

CREATE TRIGGER v3_rolling_restart_deltas_no_update
BEFORE UPDATE ON v3_rolling_restart_deltas
BEGIN
    SELECT RAISE(ABORT, 'rolling restart deltas are immutable');
END;

CREATE TRIGGER v3_rolling_restart_deltas_no_delete
BEFORE DELETE ON v3_rolling_restart_deltas
BEGIN
    SELECT RAISE(ABORT, 'rolling restart deltas are immutable');
END;

CREATE TRIGGER v3_rolling_restart_aggregate_heads_no_update
BEFORE UPDATE ON v3_rolling_restart_aggregate_heads
BEGIN
    SELECT RAISE(ABORT, 'rolling restart aggregate heads are immutable');
END;

CREATE TRIGGER v3_rolling_restart_aggregate_heads_no_delete
BEFORE DELETE ON v3_rolling_restart_aggregate_heads
BEGIN
    SELECT RAISE(ABORT, 'rolling restart aggregate heads are immutable');
END;

CREATE TRIGGER v3_rolling_restart_pending_reactions_no_update
BEFORE UPDATE ON v3_rolling_restart_pending_reactions
BEGIN
    SELECT RAISE(ABORT, 'rolling restart pending reactions are immutable');
END;

CREATE TRIGGER v3_rolling_restart_pending_reactions_no_delete
BEFORE DELETE ON v3_rolling_restart_pending_reactions
BEGIN
    SELECT RAISE(ABORT, 'rolling restart pending reactions are immutable');
END;

CREATE TRIGGER v3_rolling_restart_current_subjects_no_update
BEFORE UPDATE ON v3_rolling_restart_current_subjects
BEGIN
    SELECT RAISE(ABORT, 'rolling restart current subjects are immutable');
END;

CREATE TRIGGER v3_rolling_restart_current_subjects_no_delete
BEFORE DELETE ON v3_rolling_restart_current_subjects
BEGIN
    SELECT RAISE(ABORT, 'rolling restart current subjects are immutable');
END;
