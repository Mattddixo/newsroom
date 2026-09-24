-- Phase 3: Wikidata matching, entities, ownership edges, identifiers, logos.

CREATE TABLE entities (
    id            INTEGER PRIMARY KEY,
    qid           TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT '',   -- labels of "instance of" (P31), '; '-joined
    country       TEXT NOT NULL DEFAULT '',   -- labels of "country" (P17), '; '-joined
    website       TEXT,
    source        TEXT NOT NULL,
    source_url    TEXT NOT NULL,
    retrieved_at  TEXT NOT NULL
);

CREATE TABLE ownership_edges (
    id                INTEGER PRIMARY KEY,
    child_entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    parent_entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation          TEXT NOT NULL CHECK (relation IN ('owned_by', 'parent_org')),
    share             REAL,                   -- 0..1, only when the source states it
    start_date        TEXT,                   -- as precise as the source states it
    source            TEXT NOT NULL,          -- 'wikidata' | 'manual'
    source_url        TEXT NOT NULL,
    retrieved_at      TEXT NOT NULL,
    CHECK (child_entity_id <> parent_entity_id),
    UNIQUE (child_entity_id, parent_entity_id, relation, source)
);
CREATE INDEX ownership_edges_parent ON ownership_edges (parent_entity_id);

CREATE TABLE entity_identifiers (
    entity_id     INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    scheme        TEXT NOT NULL,              -- sec_cik | us_ein | ca_bn
    value         TEXT NOT NULL,
    source        TEXT NOT NULL,
    source_url    TEXT NOT NULL,
    retrieved_at  TEXT NOT NULL,
    PRIMARY KEY (entity_id, scheme, value)
) WITHOUT ROWID;

-- Candidates when several Wikidata items claim the same website; resolved via the CLI.
CREATE TABLE outlet_match_candidates (
    outlet_id     INTEGER NOT NULL REFERENCES outlets(id) ON DELETE CASCADE,
    qid           TEXT NOT NULL,
    label         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    website       TEXT NOT NULL,
    retrieved_at  TEXT NOT NULL,
    PRIMARY KEY (outlet_id, qid)
) WITHOUT ROWID;

ALTER TABLE outlets ADD COLUMN wikidata_qid TEXT;
ALTER TABLE outlets ADD COLUMN match_status TEXT NOT NULL DEFAULT 'unmatched'
    CHECK (match_status IN ('auto', 'confirmed', 'manual', 'ambiguous', 'unmatched'));
ALTER TABLE outlets ADD COLUMN match_source_url TEXT;
ALTER TABLE outlets ADD COLUMN matched_at TEXT;
ALTER TABLE outlets ADD COLUMN entity_id INTEGER REFERENCES entities(id) ON DELETE SET NULL;
ALTER TABLE outlets ADD COLUMN ownership_checked_at TEXT;
ALTER TABLE outlets ADD COLUMN logo_file TEXT;          -- Wikimedia Commons file name (P154)
ALTER TABLE outlets ADD COLUMN logo_path TEXT;          -- file name under /data/logos
ALTER TABLE outlets ADD COLUMN logo_source_url TEXT;    -- Commons file page (attribution)
ALTER TABLE outlets ADD COLUMN logo_retrieved_at TEXT;
CREATE INDEX outlets_entity ON outlets (entity_id);
