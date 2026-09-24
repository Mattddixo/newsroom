-- Phase 2: outlets, articles, tags, full-text search, ingestion bookkeeping.
-- Timestamps are UTC, stored as 'YYYY-MM-DDTHH:MM:SSZ' so text order is time order.

CREATE TABLE outlets (
    id            INTEGER PRIMARY KEY,
    domain        TEXT NOT NULL UNIQUE,           -- registrable host, e.g. cbc.ca
    display_name  TEXT NOT NULL,
    country       TEXT NOT NULL,                  -- ISO 3166-1 alpha-2
    language      TEXT NOT NULL,                  -- ISO 639-1
    active        INTEGER NOT NULL DEFAULT 1,     -- 0 = removed from outlets.yaml; articles kept
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE articles (
    id            INTEGER PRIMARY KEY,
    url           TEXT NOT NULL CHECK (url LIKE 'http://%' OR url LIKE 'https://%'),
    url_key       TEXT NOT NULL UNIQUE,           -- canonical form used for dedup
    title         TEXT NOT NULL,
    outlet_id     INTEGER NOT NULL REFERENCES outlets(id) ON DELETE CASCADE,
    published_at  TEXT NOT NULL,                  -- GDELT "seendate": when GDELT first saw it
    language      TEXT,
    image_url     TEXT,                           -- stored, never rendered or fetched
    source        TEXT NOT NULL,                  -- adapter name, e.g. 'gdelt'
    source_url    TEXT NOT NULL,                  -- the API request that returned this row
    retrieved_at  TEXT NOT NULL
);
CREATE INDEX articles_published ON articles (published_at DESC, id DESC);
CREATE INDEX articles_outlet_published ON articles (outlet_id, published_at DESC, id DESC);

CREATE TABLE tags (
    id     INTEGER PRIMARY KEY,
    slug   TEXT NOT NULL UNIQUE,
    label  TEXT NOT NULL
);

CREATE TABLE article_tags (
    article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    matched     TEXT NOT NULL,                    -- the keyword from tags.yaml that matched
    PRIMARY KEY (article_id, tag_id)
) WITHOUT ROWID;
CREATE INDEX article_tags_tag ON article_tags (tag_id, article_id);

CREATE VIRTUAL TABLE articles_fts USING fts5 (
    title,
    content = 'articles',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER articles_fts_insert AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts (rowid, title) VALUES (new.id, new.title);
END;

CREATE TRIGGER articles_fts_delete AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts (articles_fts, rowid, title) VALUES ('delete', old.id, old.title);
END;

CREATE TRIGGER articles_fts_update AFTER UPDATE OF title ON articles BEGIN
    INSERT INTO articles_fts (articles_fts, rowid, title) VALUES ('delete', old.id, old.title);
    INSERT INTO articles_fts (rowid, title) VALUES (new.id, new.title);
END;

CREATE TABLE ingest_runs (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    window_start  TEXT NOT NULL,
    window_end    TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    queries       INTEGER NOT NULL DEFAULT 0,
    query_errors  INTEGER NOT NULL DEFAULT 0,
    fetched       INTEGER NOT NULL DEFAULT 0,
    inserted      INTEGER NOT NULL DEFAULT 0,
    error         TEXT
);
CREATE INDEX ingest_runs_source_status ON ingest_runs (source, status, window_end);
