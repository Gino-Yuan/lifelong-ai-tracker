-- Lifelong AI Tracker - SQLite schema (version 1)
-- Connection-level PRAGMAs (journal_mode=WAL, foreign_keys=ON, busy_timeout)
-- are applied by storage/db.py on every connection.

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Incremental scan progress, one row per (collector, source file).
CREATE TABLE IF NOT EXISTS sync_checkpoints (
    id           INTEGER PRIMARY KEY,
    tool         TEXT    NOT NULL,
    path         TEXT    NOT NULL,           -- absolute path of the scanned log file
    inode        INTEGER NOT NULL,
    size         INTEGER NOT NULL,           -- file size at last scan
    mtime_ns     INTEGER NOT NULL,
    offset       INTEGER NOT NULL,           -- byte offset just past the last complete line consumed
    head_len     INTEGER NOT NULL DEFAULT 0, -- number of leading bytes fingerprinted
    head_hash    TEXT    NOT NULL DEFAULT '',-- sha256 of those bytes (detects in-place rewrites)
    lines_read   INTEGER NOT NULL DEFAULT 0, -- cumulative complete lines consumed
    resets       INTEGER NOT NULL DEFAULT 0, -- times the file was detected truncated/rewritten/replaced
    updated_at   TEXT    NOT NULL,
    UNIQUE (tool, path),
    CHECK (offset >= 0 AND offset <= size)
);

-- One row per logical model response, deduplicated on (tool, request_id, message_id).
-- Streaming duplicates are merged by UPSERT taking MAX() per token field.
-- Only metadata is stored: no prompt / completion text ever.
CREATE TABLE IF NOT EXISTS events (
    id                   INTEGER PRIMARY KEY,
    tool                 TEXT    NOT NULL,
    request_id           TEXT    NOT NULL DEFAULT '',
    message_id           TEXT    NOT NULL DEFAULT '',
    timestamp            TEXT    NOT NULL,   -- UTC, 'YYYY-MM-DDTHH:MM:SS.fffZ' (lexically sortable)
    model                TEXT    NOT NULL DEFAULT 'unknown',
    project_name         TEXT    NOT NULL DEFAULT '',
    session_id           TEXT    NOT NULL DEFAULT '',
    input_tokens         INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens       >= 0),
    output_tokens        INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens      >= 0),
    thinking_tokens      INTEGER NOT NULL DEFAULT 0 CHECK (thinking_tokens    >= 0),
    cache_read_tokens    INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens  >= 0),
    cache_write_tokens   INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    cost_usd             REAL,               -- equivalent cost from pricing.json; NULL = model unpriced
    source_checkpoint_id INTEGER REFERENCES sync_checkpoints(id) ON DELETE SET NULL,
    first_seen_at        TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL,
    UNIQUE (tool, request_id, message_id),
    CHECK (request_id <> '' OR message_id <> '')
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp);
CREATE INDEX IF NOT EXISTS idx_events_model     ON events (model);
CREATE INDEX IF NOT EXISTS idx_events_project   ON events (project_name);
CREATE INDEX IF NOT EXISTS idx_events_session   ON events (session_id);

-- Per local day x tool x model roll-up. A view (always consistent with events,
-- no refresh step); readable from any SQLite client. Day boundaries follow the
-- process local timezone ('localtime' modifier).
CREATE VIEW IF NOT EXISTS daily_stats AS
SELECT
    date(timestamp, 'localtime')           AS day,
    tool,
    model,
    COUNT(*)                               AS events,
    SUM(input_tokens)                      AS input_tokens,
    SUM(output_tokens)                     AS output_tokens,
    SUM(thinking_tokens)                   AS thinking_tokens,
    SUM(cache_read_tokens)                 AS cache_read_tokens,
    SUM(cache_write_tokens)                AS cache_write_tokens,
    SUM(input_tokens + output_tokens + thinking_tokens
        + cache_read_tokens + cache_write_tokens) AS total_tokens,
    SUM(cost_usd)                          AS cost_usd
FROM events
GROUP BY day, tool, model;
