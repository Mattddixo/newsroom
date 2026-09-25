-- Feed dates are no longer used as publication dates: some feeds state the wrong time
-- zone (CTV's says "22:50 -0400" for 6:50 p.m. EDT). Remove them; recent articles get
-- their date from the article page, like GDELT's. They stay visible meanwhile.
UPDATE articles
SET outlet_published_at = NULL, pubdate_method = NULL, pubdate_attempts = 0,
    pubdate_checked_at = coalesce(pubdate_checked_at, retrieved_at)
WHERE pubdate_method LIKE 'feed %';
