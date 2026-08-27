CREATE TABLE v3_field_weight_authorities (
    binding_digest TEXT PRIMARY KEY CHECK (length(binding_digest) = 64),
    binding_json TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    installed_at TEXT NOT NULL
);

CREATE TABLE v3_field_dependence_authorities (
    artifact_digest TEXT PRIMARY KEY CHECK (length(artifact_digest) = 64),
    artifact_json TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    installed_at TEXT NOT NULL
);

CREATE TABLE v3_field_receipts (
    receipt_id TEXT PRIMARY KEY,
    field_id TEXT NOT NULL,
    field_revision INTEGER NOT NULL CHECK (field_revision > 0),
    supersedes_receipt_id TEXT REFERENCES v3_field_receipts(receipt_id),
    caller_namespace TEXT NOT NULL,
    request_identity TEXT NOT NULL,
    field_revision_digest TEXT NOT NULL CHECK (length(field_revision_digest) = 64),
    pipeline_digest TEXT NOT NULL CHECK (length(pipeline_digest) = 64),
    receipt_json TEXT NOT NULL,
    receipt_digest TEXT NOT NULL CHECK (length(receipt_digest) = 64),
    crn_assignments_json TEXT NOT NULL,
    source_global_sequence INTEGER NOT NULL UNIQUE REFERENCES v3_events(global_sequence),
    superseded_by_sequence INTEGER REFERENCES v3_events(global_sequence),
    created_at TEXT NOT NULL,
    UNIQUE (field_id, field_revision),
    UNIQUE (caller_namespace, request_identity)
);

CREATE UNIQUE INDEX v3_field_current_receipt_idx
    ON v3_field_receipts(field_id)
    WHERE superseded_by_sequence IS NULL;

CREATE INDEX v3_field_receipt_sequence_idx
    ON v3_field_receipts(source_global_sequence);
