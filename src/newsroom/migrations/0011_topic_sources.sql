-- Topic tags now come from GDELT's theme coding of the article text and the outlet's own
-- section labels, not keywords in the headline. What they're computed from is kept so
-- tags can be recomputed after tags.yaml changes (theme names and labels only).
ALTER TABLE articles ADD COLUMN gdelt_themes TEXT NOT NULL DEFAULT '';  -- 'THEME:count ...'
ALTER TABLE articles ADD COLUMN sections TEXT NOT NULL DEFAULT '';      -- one label per line
-- Headline-keyword tags are dropped; new articles are tagged the new way.
DELETE FROM article_tags;
