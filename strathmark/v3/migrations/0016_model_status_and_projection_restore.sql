CREATE TABLE v3_model_status (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    active_bundle_digest TEXT NOT NULL CHECK (length(active_bundle_digest) = 64),
    active_candidate_id TEXT,
    source_global_sequence INTEGER NOT NULL CHECK (source_global_sequence >= 0),
    source_event_digest TEXT NOT NULL CHECK (length(source_event_digest) = 64),
    checkpoint_digest TEXT NOT NULL CHECK (length(checkpoint_digest) = 64)
);

CREATE TABLE v3_model_candidates (
    candidate_id TEXT PRIMARY KEY,
    candidate_digest TEXT NOT NULL CHECK (length(candidate_digest) = 64),
    lineage_digest TEXT NOT NULL CHECK (length(lineage_digest) = 64),
    evaluation_json TEXT,
    evaluation_digest TEXT CHECK (
        evaluation_digest IS NULL OR length(evaluation_digest) = 64
    ),
    promoted_bundle_digest TEXT CHECK (
        promoted_bundle_digest IS NULL OR length(promoted_bundle_digest) = 64
    ),
    source_global_sequence INTEGER NOT NULL CHECK (source_global_sequence > 0),
    source_event_digest TEXT NOT NULL CHECK (length(source_event_digest) = 64),
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64)
);

CREATE UNIQUE INDEX v3_model_candidates_bundle_idx
ON v3_model_candidates(promoted_bundle_digest)
WHERE promoted_bundle_digest IS NOT NULL;

CREATE TABLE v3_model_tournament_pins (
    tournament_id TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL CHECK (length(bundle_id) BETWEEN 1 AND 256),
    source_global_sequence INTEGER NOT NULL CHECK (source_global_sequence > 0),
    source_event_digest TEXT NOT NULL CHECK (length(source_event_digest) = 64),
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64)
);

CREATE INDEX v3_events_model_status_relevant_idx
ON v3_events(event_kind, global_sequence DESC);

CREATE TABLE v3_projection_restore_snapshots (
    projection_digest TEXT PRIMARY KEY CHECK (length(projection_digest) = 64),
    authority_sequence INTEGER NOT NULL CHECK (authority_sequence >= 0),
    authority_digest TEXT NOT NULL CHECK (length(authority_digest) = 64),
    snapshot_json TEXT NOT NULL CHECK (
        length(CAST(snapshot_json AS BLOB)) <= 16777216
    ),
    snapshot_digest TEXT NOT NULL CHECK (length(snapshot_digest) = 64),
    captured_at TEXT NOT NULL
);
