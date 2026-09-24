-- Re-check recent articles whose date check ended with a verdict the old logic could get
-- wrong: "robots.txt disallows" was also recorded when robots.txt merely couldn't be
-- reached (and rules were matched first-rule-wins instead of RFC 9309 longest-match),
-- and "no date in page metadata" was decided before <time itemprop="datePublished">
-- was read. Articles a site really disallows are simply marked again.
UPDATE articles
SET pubdate_attempts = 0, pubdate_checked_at = NULL, pubdate_method = NULL
WHERE outlet_published_at IS NULL
  AND pubdate_method IN ('robots.txt disallows', 'no date in page metadata')
  AND published_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-3 days');
