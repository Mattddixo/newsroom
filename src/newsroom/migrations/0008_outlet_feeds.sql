-- The outlet's own RSS/Atom feeds (outlets.yaml `feeds:`, space-separated), and an
-- optional note shown on its page (e.g. why no articles are available).
ALTER TABLE outlets ADD COLUMN feeds TEXT NOT NULL DEFAULT '';
ALTER TABLE outlets ADD COLUMN note TEXT NOT NULL DEFAULT '';
