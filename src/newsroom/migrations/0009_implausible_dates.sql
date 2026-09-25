-- Clear publication dates the old logic could store wrongly:
--  * dates later than the article was first seen (+1 h for clock skew). A feed is read
--    again every run, so a time in the future (a feed stating the wrong time zone) was
--    rejected at first and then accepted once it had passed;
--  * Atom <updated> times, which are the last edit, not publication.
-- Recent articles get their page checked again; the rest show when they were seen.
UPDATE articles
SET outlet_published_at = NULL, pubdate_method = NULL, pubdate_attempts = 0
WHERE outlet_published_at IS NOT NULL
  AND (
    outlet_published_at > strftime('%Y-%m-%dT%H:%M:%SZ', published_at, '+1 hour')
    OR pubdate_method = 'feed updated'
  );
