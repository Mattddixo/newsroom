-- What each story is about, from GDELT's reading of its text (articles from outlets'
-- feeds have none):
--  * article_places: the countries it mainly concerns (GDELT/FIPS codes), for the
--    Country filter;
--  * articles.about: the people it's about and those countries' names, searchable
--    alongside the headline ("Mark Carney · Canada · United States").
CREATE TABLE article_places (
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    country    TEXT NOT NULL,        -- FIPS 10-4 code, e.g. CA, US, GM
    mentions   INTEGER NOT NULL,
    PRIMARY KEY (article_id, country)
);
CREATE INDEX article_places_country ON article_places (country, article_id);

ALTER TABLE articles ADD COLUMN about TEXT NOT NULL DEFAULT '';

-- Search the headline and `about`; a headline match ranks higher.
DROP TRIGGER articles_fts_insert;
DROP TRIGGER articles_fts_delete;
DROP TRIGGER articles_fts_update;
DROP TABLE articles_fts;
CREATE VIRTUAL TABLE articles_fts USING fts5 (
    title,
    about,
    content = 'articles',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);
INSERT INTO articles_fts (articles_fts, rank) VALUES ('rank', 'bm25(10.0, 1.0)');
INSERT INTO articles_fts (articles_fts) VALUES ('rebuild');

CREATE TRIGGER articles_fts_insert AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts (rowid, title, about) VALUES (new.id, new.title, new.about);
END;
CREATE TRIGGER articles_fts_delete AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts (articles_fts, rowid, title, about)
    VALUES ('delete', old.id, old.title, old.about);
END;
CREATE TRIGGER articles_fts_update AFTER UPDATE OF title, about ON articles BEGIN
    INSERT INTO articles_fts (articles_fts, rowid, title, about)
    VALUES ('delete', old.id, old.title, old.about);
    INSERT INTO articles_fts (rowid, title, about) VALUES (new.id, new.title, new.about);
END;
