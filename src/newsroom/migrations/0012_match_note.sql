-- How an outlet's Wikidata item was found (shown on the outlet page), and whether it
-- was set in outlets.yaml (`wikidata:`), so removing that line goes back to automatic.
ALTER TABLE outlets ADD COLUMN match_note TEXT NOT NULL DEFAULT '';
