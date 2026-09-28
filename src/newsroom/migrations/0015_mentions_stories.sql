-- Owners named in the news, and stories covered by several outlets.

-- Other names a Wikidata item goes by (labels in other languages, aliases), so a mention
-- of "BCE" in an article is recognized as BCE Inc. Refreshed with the item.
CREATE TABLE entity_aliases (
    entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias      TEXT NOT NULL,
    PRIMARY KEY (entity_id, alias)
) WITHOUT ROWID;

-- Items in the ownership records that an article names, per GDELT's reading of the text,
-- matched by exact name when the article is collected. Whether an item is in the outlet's
-- own ownership chain is decided when the page is shown, so it follows ownership changes.
CREATE TABLE article_mentions (
    article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    qid         TEXT NOT NULL,
    mentions    INTEGER NOT NULL,
    PRIMARY KEY (article_id, qid)
) WITHOUT ROWID;
CREATE INDEX article_mentions_qid ON article_mentions (qid, article_id);

-- The people and organizations an article names most (at least twice), ' · '-joined, as
-- GDELT spells them. Used to tell which articles cover the same story.
ALTER TABLE articles ADD COLUMN names TEXT NOT NULL DEFAULT '';

-- Which story each article belongs to: the id of the story's first article (itself, for
-- the first), and the words and names it shares with the article it was linked to.
CREATE TABLE article_stories (
    article_id  INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    story_id    INTEGER NOT NULL,
    linked_to   INTEGER,
    shared      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX article_stories_story ON article_stories (story_id);
