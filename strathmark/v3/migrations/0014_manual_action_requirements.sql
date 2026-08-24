CREATE TABLE v3_manual_action_requirements (
    requirement_digest TEXT PRIMARY KEY CHECK (length(requirement_digest) = 64),
    requirement_id TEXT NOT NULL UNIQUE,
    field_id TEXT NOT NULL,
    upstream_field_revision INTEGER NOT NULL CHECK (upstream_field_revision > 0),
    field_revision_digest TEXT NOT NULL CHECK (length(field_revision_digest) = 64),
    action TEXT NOT NULL CHECK (
        action IN ('accept_single_survivor', 'complete_expected_time')
    ),
    hard_deadline_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    requirement_json TEXT NOT NULL CHECK (
        length(CAST(requirement_json AS BLOB)) <= 1048576
    ),
    requirement_manifest_json TEXT NOT NULL CHECK (
        length(CAST(requirement_manifest_json AS BLOB)) <= 2097152
    )
);

CREATE TABLE v3_manual_action_current (
    field_id TEXT PRIMARY KEY,
    requirement_digest TEXT NOT NULL UNIQUE,
    upstream_field_revision INTEGER NOT NULL CHECK (upstream_field_revision > 0),
    current_digest TEXT NOT NULL UNIQUE CHECK (length(current_digest) = 64),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (requirement_digest)
        REFERENCES v3_manual_action_requirements(requirement_digest)
);

CREATE TABLE v3_manual_action_resolutions (
    requirement_digest TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL UNIQUE,
    receipt_digest TEXT NOT NULL UNIQUE CHECK (length(receipt_digest) = 64),
    actor_id TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    resolution_digest TEXT NOT NULL UNIQUE CHECK (length(resolution_digest) = 64),
    resolution_json TEXT NOT NULL CHECK (
        length(CAST(resolution_json AS BLOB)) <= 1048576
    ),
    resolution_manifest_json TEXT NOT NULL CHECK (
        length(CAST(resolution_manifest_json AS BLOB)) <= 2097152
    ),
    FOREIGN KEY (requirement_digest)
        REFERENCES v3_manual_action_requirements(requirement_digest)
);

CREATE INDEX v3_manual_action_requirement_field_idx
ON v3_manual_action_requirements(
    field_id, upstream_field_revision, created_at, requirement_digest
);

CREATE TRIGGER v3_manual_action_requirements_no_update
BEFORE UPDATE ON v3_manual_action_requirements
BEGIN
    SELECT RAISE(ABORT, 'manual-action requirements are immutable');
END;

CREATE TRIGGER v3_manual_action_requirements_no_delete
BEFORE DELETE ON v3_manual_action_requirements
BEGIN
    SELECT RAISE(ABORT, 'manual-action requirements are immutable');
END;

CREATE TRIGGER v3_manual_action_resolutions_no_update
BEFORE UPDATE ON v3_manual_action_resolutions
BEGIN
    SELECT RAISE(ABORT, 'manual-action resolutions are immutable');
END;

CREATE TRIGGER v3_manual_action_resolutions_no_delete
BEFORE DELETE ON v3_manual_action_resolutions
BEGIN
    SELECT RAISE(ABORT, 'manual-action resolutions are immutable');
END;
