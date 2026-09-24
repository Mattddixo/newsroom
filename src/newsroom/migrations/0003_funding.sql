-- Phase 4: funding records from public filings and registries.

CREATE TABLE funding_records (
    id            INTEGER PRIMARY KEY,
    entity_id     INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN (
                      'public_filing', 'nonprofit_revenue', 'government_appropriation',
                      'grant', 'charity_registration')),
    label         TEXT NOT NULL,                 -- what the figure or link is
    amount        REAL CHECK (amount IS NULL OR amount >= 0),
    currency      TEXT,                          -- ISO 4217, only with an amount
    period        TEXT,                          -- fiscal/tax year or report date, as stated
    funder        TEXT,                          -- who provides the money, when stated
    source        TEXT NOT NULL,                 -- sec_edgar | propublica | cra | curated
    source_url    TEXT NOT NULL CHECK (source_url LIKE 'https://%' OR source_url LIKE 'http://%'),
    retrieved_at  TEXT NOT NULL
);
CREATE INDEX funding_records_entity ON funding_records (entity_id, source);

-- When each identifier's funding source was last queried (NULL = never / identifier re-added).
ALTER TABLE entity_identifiers ADD COLUMN funding_checked_at TEXT;
