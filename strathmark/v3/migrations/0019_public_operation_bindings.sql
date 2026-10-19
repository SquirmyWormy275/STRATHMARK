-- Durable public request identity precedes every preparatory lifecycle transition.
-- These rows are authority, authenticated by the independently retained witness.
CREATE TABLE v3_public_operation_bindings (
    idempotency_key TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    created_at TEXT NOT NULL
) WITHOUT ROWID;
