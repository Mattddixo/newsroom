-- Upkeep of ownership records, shown by `newsroom status`:
--  * ownership_corrections: what became of each entry in config/ownership.yaml on the
--    last refresh: applied, no longer needed (Wikidata now agrees), or not found;
--  * ownership_changes: when an outlet's ownership line changed on a refresh.
CREATE TABLE ownership_corrections (
    key         TEXT PRIMARY KEY,    -- "<outlet or entity> <action>: <target>"
    state       TEXT NOT NULL CHECK (state IN ('applied', 'retired', 'not_found')),
    detail      TEXT NOT NULL DEFAULT '',
    checked     TEXT NOT NULL,       -- when the cited source was last checked
    updated_at  TEXT NOT NULL
);

CREATE TABLE ownership_changes (
    id          INTEGER PRIMARY KEY,
    outlet_id   INTEGER NOT NULL REFERENCES outlets(id) ON DELETE CASCADE,
    changed_at  TEXT NOT NULL,
    before      TEXT NOT NULL,
    after       TEXT NOT NULL
);
CREATE INDEX ownership_changes_at ON ownership_changes (changed_at);
