ALTER TABLE v3_field_receipts RENAME COLUMN field_revision TO receipt_revision;
ALTER TABLE v3_field_receipts ADD COLUMN upstream_field_revision INTEGER;
UPDATE v3_field_receipts
SET upstream_field_revision = receipt_revision
WHERE upstream_field_revision IS NULL;

CREATE TABLE v3_field_capacity_authorities (
    authority_digest TEXT PRIMARY KEY CHECK (length(authority_digest) = 64),
    bundle_digest TEXT NOT NULL CHECK (length(bundle_digest) = 64),
    capacity_manifest_json TEXT NOT NULL CHECK (
        length(CAST(capacity_manifest_json AS BLOB)) <= 65536
    ),
    signed_manifest_json TEXT NOT NULL CHECK (
        length(CAST(signed_manifest_json AS BLOB)) <= 65536
    ),
    installed_at TEXT NOT NULL
);

CREATE UNIQUE INDEX v3_field_capacity_bundle_idx
    ON v3_field_capacity_authorities(bundle_digest);

CREATE TRIGGER v3_field_capacity_authorities_no_update
BEFORE UPDATE ON v3_field_capacity_authorities
BEGIN
    SELECT RAISE(ABORT, 'field capacity authority is immutable');
END;

CREATE TRIGGER v3_field_capacity_authorities_no_delete
BEFORE DELETE ON v3_field_capacity_authorities
BEGIN
    SELECT RAISE(ABORT, 'field capacity authority is immutable');
END;

CREATE TABLE v3_field_disagreement_authority_blobs (
    receipt_digest TEXT PRIMARY KEY CHECK (length(receipt_digest) = 64),
    field_revision_digest TEXT NOT NULL CHECK (length(field_revision_digest) = 64),
    bundle_digest TEXT NOT NULL CHECK (length(bundle_digest) = 64),
    authority_blob_json TEXT NOT NULL CHECK (
        length(CAST(authority_blob_json AS BLOB)) <= 16777216
    ),
    authority_blob_digest TEXT NOT NULL UNIQUE CHECK (
        length(authority_blob_digest) = 64
    ),
    policy_manifest_digest TEXT NOT NULL CHECK (length(policy_manifest_digest) = 64),
    council_manifest_digest TEXT CHECK (
        council_manifest_digest IS NULL OR length(council_manifest_digest) = 64
    ),
    installed_at TEXT NOT NULL
);

CREATE INDEX v3_field_disagreement_revision_idx
    ON v3_field_disagreement_authority_blobs(field_revision_digest);

CREATE TRIGGER v3_field_disagreement_authority_blobs_no_update
BEFORE UPDATE ON v3_field_disagreement_authority_blobs
BEGIN
    SELECT RAISE(ABORT, 'field disagreement authority blob is immutable');
END;

CREATE TRIGGER v3_field_disagreement_authority_blobs_no_delete
BEFORE DELETE ON v3_field_disagreement_authority_blobs
BEGIN
    SELECT RAISE(ABORT, 'field disagreement authority blob is immutable');
END;
