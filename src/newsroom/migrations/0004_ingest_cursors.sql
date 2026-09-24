-- Per-outlet ingestion progress. Each group of outlets advances on its own, so one
-- failing GDELT request no longer sends every outlet back to the start of the window.

CREATE TABLE ingest_cursors (
    source      TEXT NOT NULL,
    domain      TEXT NOT NULL,
    window_end  TEXT NOT NULL,   -- everything up to here was fetched without error
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (source, domain)
) WITHOUT ROWID;
