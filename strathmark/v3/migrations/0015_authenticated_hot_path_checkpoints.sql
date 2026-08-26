CREATE INDEX v3_jobs_active_state_idx
ON v3_jobs(state)
WHERE state IN ('queued', 'leased', 'retryable-failed');

CREATE INDEX v3_jobs_expired_lease_idx
ON v3_jobs(state, lease_expires_at, job_id, job_revision)
WHERE state = 'leased';

CREATE INDEX v3_jobs_retry_ready_idx
ON v3_jobs(state, not_before_at, job_id, job_revision)
WHERE state = 'retryable-failed';

CREATE INDEX v3_jobs_queued_deadline_idx
ON v3_jobs(state, hard_deadline_at, job_id, job_revision)
WHERE state = 'queued';

ALTER TABLE v3_aggregate_heads ADD COLUMN lifecycle_status TEXT;
ALTER TABLE v3_aggregate_heads ADD COLUMN head_digest TEXT
    CHECK (head_digest IS NULL OR length(head_digest) = 64);

CREATE TABLE v3_event_authority_checkpoint (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    global_sequence INTEGER NOT NULL CHECK (global_sequence >= 0),
    event_digest TEXT NOT NULL CHECK (length(event_digest) = 64),
    aggregate_head_count INTEGER NOT NULL CHECK (aggregate_head_count >= 0),
    last_deep_verified_at TEXT NOT NULL,
    checkpoint_digest TEXT NOT NULL CHECK (length(checkpoint_digest) = 64)
);

CREATE TABLE v3_outbox_integrity_checkpoint (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    transition_sequence INTEGER NOT NULL CHECK (transition_sequence >= 0),
    transition_digest TEXT NOT NULL CHECK (length(transition_digest) = 64),
    last_deep_verified_at TEXT NOT NULL,
    checkpoint_manifest_json TEXT NOT NULL CHECK (
        length(CAST(checkpoint_manifest_json AS BLOB)) <= 65536
    )
);

CREATE TABLE v3_outbox_item_checkpoints (
    outbox_id TEXT PRIMARY KEY REFERENCES v3_outbox(outbox_id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    item_digest TEXT NOT NULL CHECK (length(item_digest) = 64),
    transition_sequence INTEGER NOT NULL CHECK (transition_sequence >= 0),
    transition_digest TEXT NOT NULL CHECK (length(transition_digest) = 64),
    checkpoint_manifest_json TEXT NOT NULL CHECK (
        length(CAST(checkpoint_manifest_json AS BLOB)) <= 65536
    )
);

CREATE TABLE v3_projection_integrity_checkpoints (
    projection_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    source_global_sequence INTEGER NOT NULL CHECK (source_global_sequence >= 0),
    source_event_digest TEXT NOT NULL CHECK (length(source_event_digest) = 64),
    projection_digest TEXT NOT NULL CHECK (length(projection_digest) = 64),
    last_deep_verified_at TEXT NOT NULL,
    checkpoint_digest TEXT NOT NULL CHECK (length(checkpoint_digest) = 64),
    PRIMARY KEY (projection_kind, subject_id)
);

CREATE TABLE v3_job_integrity_checkpoint (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    history_sequence INTEGER NOT NULL CHECK (history_sequence >= 0),
    history_digest TEXT NOT NULL CHECK (length(history_digest) = 64),
    last_deep_verified_at TEXT NOT NULL,
    checkpoint_manifest_json TEXT NOT NULL CHECK (
        length(CAST(checkpoint_manifest_json AS BLOB)) <= 65536
    )
);
