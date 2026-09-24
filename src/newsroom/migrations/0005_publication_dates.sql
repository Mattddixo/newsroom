-- The outlet's own publication time, read from the article page's metadata.
-- `published_at` keeps GDELT's first-seen time; the site shows and sorts by
-- coalesce(outlet_published_at, published_at).

ALTER TABLE articles ADD COLUMN outlet_published_at TEXT;   -- UTC, from the page
ALTER TABLE articles ADD COLUMN pubdate_method TEXT;        -- which tag it came from
ALTER TABLE articles ADD COLUMN pubdate_checked_at TEXT;    -- when the page was read
ALTER TABLE articles ADD COLUMN pubdate_attempts INTEGER NOT NULL DEFAULT 0;

CREATE INDEX articles_effective_date
    ON articles (coalesce(outlet_published_at, published_at) DESC, id DESC);
CREATE INDEX articles_pubdate_todo
    ON articles (published_at DESC) WHERE outlet_published_at IS NULL;
