CREATE TABLE v3_rolling_council_authorities (
    manifest_digest TEXT PRIMARY KEY CHECK (length(manifest_digest) = 64),
    bundle_digest TEXT NOT NULL UNIQUE CHECK (length(bundle_digest) = 64),
    manifest_json TEXT NOT NULL CHECK (
        length(CAST(manifest_json AS BLOB)) <= 65536
    ),
    installed_at TEXT NOT NULL
);

CREATE TABLE v3_rolling_card_publications (
    publication_digest TEXT PRIMARY KEY CHECK (length(publication_digest) = 64),
    card_digest TEXT NOT NULL UNIQUE CHECK (length(card_digest) = 64),
    competitor_id TEXT NOT NULL,
    target_context_digest TEXT NOT NULL CHECK (length(target_context_digest) = 64),
    dependency_revision INTEGER NOT NULL CHECK (dependency_revision > 0),
    tournament_epoch_id TEXT NOT NULL,
    bundle_digest TEXT NOT NULL CHECK (length(bundle_digest) = 64),
    evidence_digest TEXT NOT NULL CHECK (length(evidence_digest) = 64),
    hard_deadline_at TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    authority_json TEXT NOT NULL CHECK (
        length(CAST(authority_json AS BLOB)) <= 1048576
    ),
    authority_digest TEXT NOT NULL UNIQUE CHECK (length(authority_digest) = 64),
    component_refs_json TEXT NOT NULL CHECK (
        length(CAST(component_refs_json AS BLOB)) <= 65536
    ),
    component_refs_digest TEXT NOT NULL CHECK (length(component_refs_digest) = 64),
    availability_json TEXT NOT NULL CHECK (
        length(CAST(availability_json AS BLOB)) <= 4096
    ),
    availability_digest TEXT NOT NULL CHECK (length(availability_digest) = 64),
    council_manifest_digest TEXT NOT NULL CHECK (length(council_manifest_digest) = 64),
    council_aggregate_manifest_json TEXT NOT NULL CHECK (
        length(CAST(council_aggregate_manifest_json AS BLOB)) <= 65536
    ),
    publication_manifest_json TEXT NOT NULL CHECK (
        length(CAST(publication_manifest_json AS BLOB)) <= 65536
    )
);

CREATE INDEX v3_rolling_card_subject_idx
    ON v3_rolling_card_publications(
        competitor_id, target_context_digest, dependency_revision
    );

CREATE TABLE v3_rolling_card_status_history (
    status_sequence INTEGER PRIMARY KEY CHECK (status_sequence > 0),
    publication_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('current', 'superseded', 'sealed')),
    reason_code TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    prior_status_digest TEXT NOT NULL CHECK (length(prior_status_digest) = 64),
    status_digest TEXT NOT NULL UNIQUE CHECK (length(status_digest) = 64),
    status_manifest_json TEXT NOT NULL CHECK (
        length(CAST(status_manifest_json AS BLOB)) <= 65536
    ),
    FOREIGN KEY (publication_digest)
        REFERENCES v3_rolling_card_publications(publication_digest)
);

CREATE TABLE v3_rolling_card_current (
    competitor_id TEXT NOT NULL,
    target_context_digest TEXT NOT NULL CHECK (length(target_context_digest) = 64),
    publication_digest TEXT NOT NULL UNIQUE,
    dependency_revision INTEGER NOT NULL CHECK (dependency_revision > 0),
    status_digest TEXT NOT NULL CHECK (length(status_digest) = 64),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (competitor_id, target_context_digest),
    FOREIGN KEY (publication_digest)
        REFERENCES v3_rolling_card_publications(publication_digest)
);

CREATE TABLE v3_rolling_epoch_closures (
    epoch_id TEXT PRIMARY KEY,
    source_event_digest TEXT NOT NULL UNIQUE CHECK (length(source_event_digest) = 64),
    source_global_sequence INTEGER NOT NULL CHECK (source_global_sequence > 0),
    source_event_kind TEXT NOT NULL CHECK (
        source_event_kind IN ('round_closed', 'tournament_closed')
    ),
    closed_at TEXT NOT NULL,
    closure_manifest_json TEXT NOT NULL CHECK (
        length(CAST(closure_manifest_json AS BLOB)) <= 65536
    )
);

CREATE TABLE v3_rolling_reaction_obligations (
    reaction_id TEXT PRIMARY KEY CHECK (length(reaction_id) = 64),
    source_command_id TEXT NOT NULL,
    first_global_sequence INTEGER NOT NULL UNIQUE
        REFERENCES v3_events(global_sequence) CHECK (first_global_sequence > 0),
    last_global_sequence INTEGER NOT NULL UNIQUE
        REFERENCES v3_events(global_sequence) CHECK (last_global_sequence >= first_global_sequence),
    event_ids_json TEXT NOT NULL CHECK (
        length(CAST(event_ids_json AS BLOB)) <= 65536
    ),
    event_set_digest TEXT NOT NULL UNIQUE CHECK (length(event_set_digest) = 64),
    registered_at TEXT NOT NULL
);

CREATE TABLE v3_rolling_reaction_completions (
    reaction_id TEXT PRIMARY KEY
        REFERENCES v3_rolling_reaction_obligations(reaction_id),
    plan_digest TEXT NOT NULL CHECK (length(plan_digest) = 64),
    completed_at TEXT NOT NULL,
    completion_digest TEXT NOT NULL UNIQUE CHECK (length(completion_digest) = 64),
    completion_manifest_json TEXT NOT NULL CHECK (
        length(CAST(completion_manifest_json AS BLOB)) <= 65536
    )
);

CREATE TRIGGER v3_rolling_council_authorities_no_update
BEFORE UPDATE ON v3_rolling_council_authorities
BEGIN
    SELECT RAISE(ABORT, 'rolling council authority is immutable');
END;

CREATE TRIGGER v3_rolling_council_authorities_no_delete
BEFORE DELETE ON v3_rolling_council_authorities
BEGIN
    SELECT RAISE(ABORT, 'rolling council authority is immutable');
END;

CREATE TRIGGER v3_rolling_card_publications_no_update
BEFORE UPDATE ON v3_rolling_card_publications
BEGIN
    SELECT RAISE(ABORT, 'rolling card publication is immutable');
END;

CREATE TRIGGER v3_rolling_card_publications_no_delete
BEFORE DELETE ON v3_rolling_card_publications
BEGIN
    SELECT RAISE(ABORT, 'rolling card publication is immutable');
END;

CREATE TRIGGER v3_rolling_card_status_history_no_update
BEFORE UPDATE ON v3_rolling_card_status_history
BEGIN
    SELECT RAISE(ABORT, 'rolling card status history is immutable');
END;

CREATE TRIGGER v3_rolling_card_status_history_no_delete
BEFORE DELETE ON v3_rolling_card_status_history
BEGIN
    SELECT RAISE(ABORT, 'rolling card status history is immutable');
END;

CREATE TRIGGER v3_rolling_epoch_closures_no_update
BEFORE UPDATE ON v3_rolling_epoch_closures
BEGIN
    SELECT RAISE(ABORT, 'rolling epoch closure is immutable');
END;

CREATE TRIGGER v3_rolling_epoch_closures_no_delete
BEFORE DELETE ON v3_rolling_epoch_closures
BEGIN
    SELECT RAISE(ABORT, 'rolling epoch closure is immutable');
END;

CREATE TRIGGER v3_rolling_reaction_obligations_no_update
BEFORE UPDATE ON v3_rolling_reaction_obligations
BEGIN
    SELECT RAISE(ABORT, 'rolling reaction obligation is immutable');
END;

CREATE TRIGGER v3_rolling_reaction_obligations_no_delete
BEFORE DELETE ON v3_rolling_reaction_obligations
BEGIN
    SELECT RAISE(ABORT, 'rolling reaction obligation is immutable');
END;

CREATE TRIGGER v3_rolling_reaction_completions_no_update
BEFORE UPDATE ON v3_rolling_reaction_completions
BEGIN
    SELECT RAISE(ABORT, 'rolling reaction completion is immutable');
END;

CREATE TRIGGER v3_rolling_reaction_completions_no_delete
BEFORE DELETE ON v3_rolling_reaction_completions
BEGIN
    SELECT RAISE(ABORT, 'rolling reaction completion is immutable');
END;
