ALTER TABLE v3_rolling_card_current RENAME TO v3_rolling_card_current_unscoped;

CREATE TABLE v3_rolling_card_current (
    competitor_id TEXT NOT NULL,
    target_context_digest TEXT NOT NULL CHECK (length(target_context_digest) = 64),
    tournament_epoch_id TEXT NOT NULL,
    bundle_digest TEXT NOT NULL CHECK (length(bundle_digest) = 64),
    publication_digest TEXT NOT NULL UNIQUE,
    dependency_revision INTEGER NOT NULL CHECK (dependency_revision > 0),
    status_digest TEXT NOT NULL CHECK (length(status_digest) = 64),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (
        competitor_id,
        target_context_digest,
        tournament_epoch_id,
        bundle_digest
    ),
    FOREIGN KEY (publication_digest)
        REFERENCES v3_rolling_card_publications(publication_digest)
);

INSERT INTO v3_rolling_card_current
SELECT current.competitor_id,current.target_context_digest,
       publication.tournament_epoch_id,publication.bundle_digest,
       current.publication_digest,current.dependency_revision,
       current.status_digest,current.updated_at
FROM v3_rolling_card_current_unscoped current
JOIN v3_rolling_card_publications publication
  ON publication.publication_digest=current.publication_digest;

DROP TABLE v3_rolling_card_current_unscoped;

ALTER TABLE v3_rolling_restart_current_subjects
RENAME TO v3_rolling_restart_current_subjects_unscoped;

CREATE TABLE v3_rolling_restart_current_subjects (
    checkpoint_sequence INTEGER NOT NULL CHECK (checkpoint_sequence > 0),
    competitor_id TEXT NOT NULL,
    target_context_digest TEXT NOT NULL CHECK (length(target_context_digest) = 64),
    tournament_epoch_id TEXT NOT NULL,
    bundle_digest TEXT NOT NULL CHECK (length(bundle_digest) = 64),
    publication_digest TEXT NOT NULL CHECK (length(publication_digest) = 64),
    dependency_revision INTEGER NOT NULL CHECK (dependency_revision > 0),
    status_digest TEXT NOT NULL CHECK (length(status_digest) = 64),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (
        checkpoint_sequence,
        competitor_id,
        target_context_digest,
        tournament_epoch_id,
        bundle_digest
    ),
    FOREIGN KEY (checkpoint_sequence)
        REFERENCES v3_rolling_restart_checkpoints(checkpoint_sequence)
);

INSERT INTO v3_rolling_restart_current_subjects
SELECT old.checkpoint_sequence,old.competitor_id,old.target_context_digest,
       old.tournament_epoch_id,publication.bundle_digest,old.publication_digest,
       old.dependency_revision,old.status_digest,old.updated_at
FROM v3_rolling_restart_current_subjects_unscoped old
JOIN v3_rolling_card_publications publication
  ON publication.publication_digest=old.publication_digest;

DROP TABLE v3_rolling_restart_current_subjects_unscoped;

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

CREATE TABLE v3_pre_field_forecast_receipts (
    forecast_set_id TEXT PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    forecast_set_revision INTEGER NOT NULL CHECK (forecast_set_revision > 0),
    request_namespace TEXT NOT NULL,
    request_identity TEXT NOT NULL,
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    snapshot_digest TEXT NOT NULL UNIQUE CHECK (length(snapshot_digest) = 64),
    receipt_digest TEXT NOT NULL UNIQUE CHECK (length(receipt_digest) = 64),
    receipt_json TEXT NOT NULL CHECK (length(CAST(receipt_json AS BLOB)) <= 1048576),
    created_at TEXT NOT NULL,
    UNIQUE (request_namespace, request_identity)
);

CREATE INDEX v3_pre_field_forecast_round_idx
ON v3_pre_field_forecast_receipts(tournament_id, round_id, forecast_set_revision);

CREATE TRIGGER v3_pre_field_forecast_receipts_no_update
BEFORE UPDATE ON v3_pre_field_forecast_receipts
BEGIN
    SELECT RAISE(ABORT, 'pre-field forecast receipt is immutable');
END;

CREATE TRIGGER v3_pre_field_forecast_receipts_no_delete
BEFORE DELETE ON v3_pre_field_forecast_receipts
BEGIN
    SELECT RAISE(ABORT, 'pre-field forecast receipt is immutable');
END;
