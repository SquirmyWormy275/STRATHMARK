CREATE TABLE v3_expected_time_override_states (
    override_id TEXT PRIMARY KEY,
    competitor_id TEXT NOT NULL,
    tournament_id TEXT NOT NULL,
    target_context_digest TEXT NOT NULL CHECK (length(target_context_digest) = 64),
    scope TEXT NOT NULL CHECK (
        scope IN ('upcoming_race', 'remaining_event_configuration', 'remaining_tournament')
    ),
    scope_boundary_id TEXT NOT NULL,
    accepted_field_id TEXT NOT NULL,
    accepted_round_id TEXT NOT NULL,
    accepted_call_order INTEGER NOT NULL CHECK (accepted_call_order >= 0),
    accepted_capability_revision INTEGER NOT NULL CHECK (accepted_capability_revision >= 0),
    state_json TEXT NOT NULL,
    state_digest TEXT NOT NULL CHECK (length(state_digest) = 64),
    source_global_sequence INTEGER NOT NULL UNIQUE REFERENCES v3_events(global_sequence),
    source_event_digest TEXT NOT NULL CHECK (length(source_event_digest) = 64),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    superseded_by_override_id TEXT REFERENCES v3_expected_time_override_states(override_id)
);

CREATE INDEX v3_expected_time_override_active_idx
ON v3_expected_time_override_states(
    competitor_id,
    tournament_id,
    active,
    accepted_call_order,
    source_global_sequence DESC
);
