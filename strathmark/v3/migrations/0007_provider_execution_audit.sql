CREATE TABLE v3_job_provider_executions (
    job_id TEXT NOT NULL,
    job_revision INTEGER NOT NULL CHECK (job_revision > 0),
    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
    lease_owner TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    member_pin_json TEXT NOT NULL,
    member_pin_digest TEXT NOT NULL CHECK (length(member_pin_digest) = 64),
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    reason TEXT,
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    execution_json TEXT NOT NULL,
    execution_digest TEXT NOT NULL CHECK (length(execution_digest) = 64),
    observed_at TEXT NOT NULL,
    auth_body_json TEXT NOT NULL,
    auth_body_digest TEXT NOT NULL CHECK (length(auth_body_digest) = 64),
    auth_key_id TEXT NOT NULL,
    auth_signature_der_b64 TEXT NOT NULL,
    PRIMARY KEY (job_id, job_revision, fencing_token),
    FOREIGN KEY (job_id, job_revision) REFERENCES v3_jobs(job_id, job_revision),
    CHECK ((status = 'succeeded' AND reason IS NULL)
        OR (status = 'failed' AND reason IS NOT NULL))
);

CREATE TABLE v3_job_provider_attempts (
    job_id TEXT NOT NULL,
    job_revision INTEGER NOT NULL,
    fencing_token INTEGER NOT NULL,
    attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal > 0),
    raw_digest TEXT NOT NULL CHECK (length(raw_digest) = 64),
    validator_code TEXT NOT NULL,
    accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
    PRIMARY KEY (job_id, job_revision, fencing_token, attempt_ordinal),
    FOREIGN KEY (job_id, job_revision, fencing_token)
        REFERENCES v3_job_provider_executions(job_id, job_revision, fencing_token)
);

CREATE TABLE v3_job_provider_storage_refs (
    job_id TEXT NOT NULL,
    job_revision INTEGER NOT NULL,
    fencing_token INTEGER NOT NULL,
    attempt_ordinal INTEGER NOT NULL,
    raw_digest TEXT NOT NULL CHECK (length(raw_digest) = 64),
    byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
    reference_json TEXT NOT NULL,
    reference_digest TEXT NOT NULL CHECK (length(reference_digest) = 64),
    PRIMARY KEY (job_id, job_revision, fencing_token, attempt_ordinal),
    FOREIGN KEY (job_id, job_revision, fencing_token, attempt_ordinal)
        REFERENCES v3_job_provider_attempts(job_id, job_revision, fencing_token, attempt_ordinal)
);

CREATE TRIGGER v3_job_provider_executions_no_update
BEFORE UPDATE ON v3_job_provider_executions
BEGIN
    SELECT RAISE(ABORT, 'provider execution audit is immutable');
END;

CREATE TRIGGER v3_job_provider_executions_no_delete
BEFORE DELETE ON v3_job_provider_executions
BEGIN
    SELECT RAISE(ABORT, 'provider execution audit is immutable');
END;

CREATE TRIGGER v3_job_provider_attempts_no_update
BEFORE UPDATE ON v3_job_provider_attempts
BEGIN
    SELECT RAISE(ABORT, 'provider attempt audit is immutable');
END;

CREATE TRIGGER v3_job_provider_attempts_no_delete
BEFORE DELETE ON v3_job_provider_attempts
BEGIN
    SELECT RAISE(ABORT, 'provider attempt audit is immutable');
END;

CREATE TRIGGER v3_job_provider_storage_refs_no_update
BEFORE UPDATE ON v3_job_provider_storage_refs
BEGIN
    SELECT RAISE(ABORT, 'provider storage audit is immutable');
END;

CREATE TRIGGER v3_job_provider_storage_refs_no_delete
BEFORE DELETE ON v3_job_provider_storage_refs
BEGIN
    SELECT RAISE(ABORT, 'provider storage audit is immutable');
END;
