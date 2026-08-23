CREATE TABLE v3_jobs (
    job_id TEXT NOT NULL,
    job_revision INTEGER NOT NULL CHECK (job_revision > 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    job_kind TEXT NOT NULL,
    lane TEXT NOT NULL CHECK (lane IN (
        'hot_field', 'inference', 'lookup_recovery', 'maintenance'
    )),
    resource_class TEXT NOT NULL CHECK (resource_class IN (
        'local_cpu', 'local_gpu', 'cloud', 'storage_io'
    )),
    base_priority INTEGER NOT NULL CHECK (base_priority BETWEEN 1 AND 1000),
    capacity_use_json TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (length(CAST(payload_json AS BLOB)) <= 1048576),
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    evidence_digest TEXT NOT NULL CHECK (length(evidence_digest) = 64),
    bundle_digest TEXT NOT NULL CHECK (length(bundle_digest) = 64),
    retry_policy_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'leased', 'succeeded', 'invalid', 'stale', 'cancelled',
        'retryable-failed', 'permanent-failed'
    )),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 32),
    initial_not_before_at TEXT NOT NULL,
    not_before_at TEXT,
    hard_deadline_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_acquired_at TEXT,
    lease_expires_at TEXT,
    fencing_token INTEGER NOT NULL CHECK (fencing_token >= 0),
    terminal_reason TEXT,
    result_digest TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, job_revision),
    CHECK ((state = 'leased' AND lease_owner IS NOT NULL
            AND lease_acquired_at IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (state != 'leased' AND lease_owner IS NULL
            AND lease_acquired_at IS NULL AND lease_expires_at IS NULL)),
    CHECK ((state = 'succeeded' AND result_digest IS NOT NULL)
        OR (state != 'succeeded' AND result_digest IS NULL))
);

CREATE INDEX v3_jobs_claim_idx
ON v3_jobs(lane, state, not_before_at, hard_deadline_at, base_priority, created_at, job_id);

CREATE INDEX v3_jobs_lease_idx
ON v3_jobs(lane, state, lease_expires_at);

CREATE TRIGGER v3_jobs_spec_immutable
BEFORE UPDATE ON v3_jobs
WHEN NEW.job_id != OLD.job_id
    OR NEW.job_revision != OLD.job_revision
    OR NEW.idempotency_key != OLD.idempotency_key
    OR NEW.job_kind != OLD.job_kind
    OR NEW.lane != OLD.lane
    OR NEW.resource_class != OLD.resource_class
    OR NEW.base_priority != OLD.base_priority
    OR NEW.capacity_use_json != OLD.capacity_use_json
    OR NEW.payload_json != OLD.payload_json
    OR NEW.payload_digest != OLD.payload_digest
    OR NEW.evidence_digest != OLD.evidence_digest
    OR NEW.bundle_digest != OLD.bundle_digest
    OR NEW.retry_policy_version != OLD.retry_policy_version
    OR NEW.max_attempts != OLD.max_attempts
    OR NEW.initial_not_before_at != OLD.initial_not_before_at
    OR NEW.hard_deadline_at != OLD.hard_deadline_at
    OR NEW.created_at != OLD.created_at
    OR NEW.attempt_count < OLD.attempt_count
    OR NEW.fencing_token < OLD.fencing_token
BEGIN
    SELECT RAISE(ABORT, 'v3 job specification and monotonic counters are immutable');
END;

CREATE TRIGGER v3_jobs_no_delete
BEFORE DELETE ON v3_jobs
BEGIN
    SELECT RAISE(ABORT, 'v3 jobs are append-preserved');
END;

CREATE TABLE v3_job_history (
    history_sequence INTEGER PRIMARY KEY CHECK (history_sequence > 0),
    transition_id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL,
    job_revision INTEGER NOT NULL,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN (
        'queued', 'leased', 'heartbeat', 'lease_expired', 'requeued', 'succeeded',
        'invalid', 'stale', 'cancelled', 'retryable-failed', 'permanent-failed'
    )),
    from_state TEXT,
    result_state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    fencing_token INTEGER NOT NULL CHECK (fencing_token >= 0),
    lease_owner TEXT,
    lease_acquired_at TEXT,
    lease_expires_at TEXT,
    not_before_at TEXT,
    terminal_reason TEXT,
    result_digest TEXT,
    observed_at TEXT NOT NULL,
    prior_history_digest TEXT NOT NULL CHECK (length(prior_history_digest) = 64),
    history_digest TEXT NOT NULL UNIQUE CHECK (length(history_digest) = 64),
    job_material_digest TEXT NOT NULL CHECK (length(job_material_digest) = 64),
    auth_body_json TEXT NOT NULL,
    auth_body_digest TEXT NOT NULL CHECK (length(auth_body_digest) = 64),
    auth_key_id TEXT NOT NULL,
    auth_signature_der_b64 TEXT NOT NULL,
    FOREIGN KEY (job_id, job_revision) REFERENCES v3_jobs(job_id, job_revision)
);

CREATE INDEX v3_job_history_job_idx
ON v3_job_history(job_id, job_revision, history_sequence);

CREATE TRIGGER v3_job_history_no_update
BEFORE UPDATE ON v3_job_history
BEGIN
    SELECT RAISE(ABORT, 'v3 job history is immutable');
END;

CREATE TRIGGER v3_job_history_no_delete
BEFORE DELETE ON v3_job_history
BEGIN
    SELECT RAISE(ABORT, 'v3 job history is immutable');
END;

CREATE TABLE v3_job_publications (
    job_id TEXT NOT NULL,
    job_revision INTEGER NOT NULL,
    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
    result_digest TEXT NOT NULL CHECK (length(result_digest) = 64),
    published_at TEXT NOT NULL,
    auth_body_json TEXT NOT NULL,
    auth_body_digest TEXT NOT NULL CHECK (length(auth_body_digest) = 64),
    auth_key_id TEXT NOT NULL,
    auth_signature_der_b64 TEXT NOT NULL,
    PRIMARY KEY (job_id, job_revision),
    FOREIGN KEY (job_id, job_revision) REFERENCES v3_jobs(job_id, job_revision)
);

CREATE TRIGGER v3_job_publications_no_update
BEFORE UPDATE ON v3_job_publications
BEGIN
    SELECT RAISE(ABORT, 'v3 job publications are immutable');
END;

CREATE TRIGGER v3_job_publications_no_delete
BEFORE DELETE ON v3_job_publications
BEGIN
    SELECT RAISE(ABORT, 'v3 job publications are immutable');
END;
