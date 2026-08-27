DROP TRIGGER v3_historical_imports_no_update;
DROP TRIGGER v3_historical_imports_no_delete;
DROP TRIGGER v3_historical_import_rows_no_update;
DROP TRIGGER v3_historical_import_rows_no_delete;
DROP INDEX idx_v3_historical_rows_group;

ALTER TABLE v3_historical_import_rows RENAME TO v3_historical_import_rows_pre_cutover;
ALTER TABLE v3_historical_imports RENAME TO v3_historical_imports_pre_cutover;

CREATE TABLE v3_historical_imports (
    import_id TEXT PRIMARY KEY,
    source_profile_json TEXT NOT NULL,
    source_catalog_digest TEXT NOT NULL CHECK (length(source_catalog_digest) = 64),
    source_tip_digest TEXT NOT NULL UNIQUE CHECK (length(source_tip_digest) = 64),
    source_cutoff TEXT NOT NULL,
    source_manifest_json TEXT NOT NULL,
    imported_row_count INTEGER NOT NULL CHECK (imported_row_count >= 0),
    imported_at TEXT NOT NULL,
    cutover_manifest_digest TEXT CHECK (
        cutover_manifest_digest IS NULL OR length(cutover_manifest_digest) = 64
    ),
    eligible INTEGER NOT NULL DEFAULT 0 CHECK (eligible IN (0, 1)),
    CHECK (
        (eligible = 0 AND cutover_manifest_digest IS NULL)
        OR (eligible = 1 AND cutover_manifest_digest IS NOT NULL)
    )
);

CREATE TABLE v3_historical_import_rows (
    import_id TEXT NOT NULL REFERENCES v3_historical_imports(import_id),
    source_group TEXT NOT NULL,
    source_table TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    canonical_json TEXT NOT NULL,
    eligible INTEGER NOT NULL DEFAULT 0 CHECK (eligible IN (0, 1)),
    PRIMARY KEY (import_id, source_table, ordinal),
    UNIQUE (import_id, row_digest)
);

INSERT INTO v3_historical_imports
SELECT * FROM v3_historical_imports_pre_cutover;
INSERT INTO v3_historical_import_rows
SELECT * FROM v3_historical_import_rows_pre_cutover;
DROP TABLE v3_historical_import_rows_pre_cutover;
DROP TABLE v3_historical_imports_pre_cutover;

CREATE INDEX idx_v3_historical_rows_group
    ON v3_historical_import_rows(import_id, source_group, source_table, ordinal);

CREATE TABLE v3_historical_cutovers (
    import_id TEXT PRIMARY KEY REFERENCES v3_historical_imports(import_id),
    signed_manifest_json TEXT NOT NULL,
    signed_manifest_digest TEXT NOT NULL UNIQUE CHECK (length(signed_manifest_digest) = 64),
    activated_at TEXT NOT NULL
);

CREATE TRIGGER v3_historical_cutovers_no_update
BEFORE UPDATE ON v3_historical_cutovers
BEGIN SELECT RAISE(ABORT, 'append-only authority'); END;
CREATE TRIGGER v3_historical_cutovers_no_delete
BEFORE DELETE ON v3_historical_cutovers
BEGIN SELECT RAISE(ABORT, 'append-only authority'); END;
CREATE TRIGGER v3_historical_imports_no_update
BEFORE UPDATE ON v3_historical_imports
BEGIN SELECT RAISE(ABORT, 'append-only authority'); END;
CREATE TRIGGER v3_historical_imports_no_delete
BEFORE DELETE ON v3_historical_imports
BEGIN SELECT RAISE(ABORT, 'append-only authority'); END;
CREATE TRIGGER v3_historical_import_rows_no_update
BEFORE UPDATE ON v3_historical_import_rows
BEGIN SELECT RAISE(ABORT, 'append-only authority'); END;
CREATE TRIGGER v3_historical_import_rows_no_delete
BEFORE DELETE ON v3_historical_import_rows
BEGIN SELECT RAISE(ABORT, 'append-only authority'); END;
