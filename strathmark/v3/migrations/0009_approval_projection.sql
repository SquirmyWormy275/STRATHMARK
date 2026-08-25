CREATE TABLE v3_approval_projection_meta (
    tournament_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    source_global_sequence INTEGER NOT NULL CHECK (source_global_sequence >= 0),
    decision_global_sequence INTEGER NOT NULL CHECK (decision_global_sequence >= 0),
    lifecycle_state TEXT NOT NULL,
    preparation_completed INTEGER NOT NULL CHECK (preparation_completed >= 0),
    preparation_total INTEGER NOT NULL CHECK (preparation_total >= preparation_completed),
    preparing_count INTEGER NOT NULL CHECK (preparing_count >= 0),
    ready_count INTEGER NOT NULL CHECK (ready_count >= 0),
    blocked_count INTEGER NOT NULL CHECK (blocked_count >= 0),
    issued_count INTEGER NOT NULL CHECK (issued_count >= 0),
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    counts_json TEXT NOT NULL,
    projection_digest TEXT NOT NULL CHECK (length(projection_digest) = 64),
    rebuilt_at TEXT NOT NULL
);

CREATE TABLE v3_approval_queue_rows (
    tournament_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    field_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL UNIQUE REFERENCES v3_field_receipts(receipt_id),
    receipt_revision INTEGER NOT NULL CHECK (receipt_revision > 0),
    upstream_field_revision INTEGER NOT NULL CHECK (upstream_field_revision > 0),
    call_order INTEGER NOT NULL CHECK (call_order >= 0),
    deadline_at TEXT NOT NULL,
    lane TEXT NOT NULL,
    consequence_color TEXT NOT NULL,
    decision_state TEXT NOT NULL,
    ordinary_batch_eligible INTEGER NOT NULL CHECK (ordinary_batch_eligible IN (0, 1)),
    degraded_batch_eligible INTEGER NOT NULL CHECK (degraded_batch_eligible IN (0, 1)),
    row_json TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    detail_digest TEXT NOT NULL CHECK (length(detail_digest) = 64),
    source_global_sequence INTEGER NOT NULL REFERENCES v3_events(global_sequence),
    PRIMARY KEY (tournament_id, field_id)
);

CREATE INDEX v3_approval_queue_scan_idx
    ON v3_approval_queue_rows(tournament_id, call_order, deadline_at, field_id);

CREATE TABLE v3_approval_details (
    receipt_id TEXT PRIMARY KEY REFERENCES v3_field_receipts(receipt_id),
    tournament_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    detail_digest TEXT NOT NULL CHECK (length(detail_digest) = 64),
    source_global_sequence INTEGER NOT NULL REFERENCES v3_events(global_sequence)
);

CREATE TABLE v3_approval_schedule (
    tournament_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    field_id TEXT NOT NULL,
    upstream_field_revision INTEGER NOT NULL CHECK (upstream_field_revision > 0),
    call_order INTEGER NOT NULL CHECK (call_order >= 0),
    scheduled_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    receipt_id TEXT UNIQUE REFERENCES v3_field_receipts(receipt_id),
    source_global_sequence INTEGER NOT NULL REFERENCES v3_events(global_sequence),
    PRIMARY KEY (tournament_id, field_id)
);

CREATE TABLE v3_approval_command_projection (
    source_global_sequence INTEGER PRIMARY KEY REFERENCES v3_events(global_sequence),
    tournament_id TEXT NOT NULL,
    caller_namespace TEXT NOT NULL,
    request_identity TEXT NOT NULL,
    command_digest TEXT NOT NULL CHECK (length(command_digest) = 64),
    action TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    command_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    decision_digest TEXT NOT NULL UNIQUE CHECK (length(decision_digest) = 64),
    source_event_digest TEXT NOT NULL UNIQUE CHECK (length(source_event_digest) = 64),
    decided_at TEXT NOT NULL,
    UNIQUE (caller_namespace, request_identity)
);

CREATE TABLE v3_approval_decision_projection (
    receipt_id TEXT PRIMARY KEY REFERENCES v3_field_receipts(receipt_id),
    tournament_id TEXT NOT NULL,
    field_id TEXT NOT NULL,
    receipt_revision INTEGER NOT NULL CHECK (receipt_revision > 0),
    upstream_field_revision INTEGER NOT NULL CHECK (upstream_field_revision > 0),
    decision_state TEXT NOT NULL,
    decision_digest TEXT NOT NULL REFERENCES v3_approval_command_projection(decision_digest),
    source_global_sequence INTEGER NOT NULL REFERENCES v3_events(global_sequence)
);

CREATE TABLE v3_approval_snapshot_history (
    snapshot_id TEXT PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL CHECK (length(snapshot_digest) = 64),
    source_global_sequence INTEGER NOT NULL CHECK (source_global_sequence >= 0)
);

CREATE TABLE v3_approval_snapshot_rows (
    snapshot_id TEXT NOT NULL REFERENCES v3_approval_snapshot_history(snapshot_id),
    tournament_id TEXT NOT NULL,
    field_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    receipt_revision INTEGER NOT NULL CHECK (receipt_revision > 0),
    upstream_field_revision INTEGER NOT NULL CHECK (upstream_field_revision > 0),
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    PRIMARY KEY (snapshot_id, field_id)
);
