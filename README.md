# newsroom

A self-hosted news feed that shows, for every article's outlet, **who owns it** and,
where a public record exists, **who funds it**. Every ownership or funding claim links
to its source record. Where no public record exists, the page says
"Not publicly disclosed". No bias ratings, no sentiment scores, no editorializing.

**Pages:** the feed (filter by tag, outlet, country, owner or date; headline search; sort by
newest, oldest, outlet or best match; 25/50/100 per page with numbered pages; filters and sort
apply as soon as they change, and a notice appears when new articles arrive), **Outlets**, an outlet page (ownership chain, funding, recent articles), **Owners**
(top-level owners by number of outlets, which makes concentration visible), an owner page
(everything it holds, directly or through subsidiaries), and **About**, which explains the
method. On each article card, clicking the "Owned by …" line opens the ownership chain and
funding records, each with its source.

- [How it runs](#how-it-runs) · [Setup](#setup-from-scratch-ubuntu-2404-docker--compose-installed-tailscale-on-the-host)
  · [Tasks](#common-tasks) · [Configuration](#configuration-env) · [Articles](#what-gets-ingested)
  · [Ownership](#ownership) · [Funding](#funding) · [CLI](#cli-reference)
  · [Operations](#operations) · [Data sources and licences](#data-sources-and-licences)
  · [Development](#development)
- Also: [docs/security.md](docs/security.md) (firewall, hardening, checklist) and
  [docs/going-public.md](docs/going-public.md) (Cloudflare Tunnel).

## How it runs

Two containers from one image:

| Service  | Role                                                                  | Network                          |
|----------|-----------------------------------------------------------------------|----------------------------------|
| `web`    | FastAPI, server-rendered pages. Opens SQLite **read-only**.           | `${TAILSCALE_IP}:8091` only      |
| `worker` | Scheduler: migrations, 15-minute ingestion, nightly snapshot and pruning. | Outbound only, no published port |

All state lives under `/storage/newsroom/` (restic already backs that up):

```
/storage/newsroom/
  db/        newsroom.sqlite3 (+ -wal, -shm)
  backups/   newsroom-YYYYMMDDTHHMMSSZ.sqlite3  (nightly, consistent, last 14 kept)
  logos/     cached outlet logos (PNG, from Wikimedia Commons)
```

Repository layout:

```
compose.yaml  Dockerfile  Makefile  .env.example  pyproject.toml  uv.lock
config/       outlets.yaml  tags.yaml  public_funding.yaml   (edited by you, mounted read-only)
scripts/      init-host.sh  verify-binding.sh
docs/         security.md  going-public.md
src/newsroom/
  sources/    one adapter per source: gdelt, wikidata, funding (SEC, ProPublica, CRA)
  services/   ingest, tagging, ownership, funding (all database writes happen here)
  web/        FastAPI app, read-only queries, templates, static assets (htmx vendored)
  net/        http.py (API client: User-Agent, pacing, backoff), safe_fetch.py (SSRF guard)
  migrations/ plain versioned SQL, applied by the worker on start
  cli.py  worker.py  jobs.py  ownership_view.py  funding_view.py
tests/        pytest, with recorded-format fixtures (no live API calls)
```

## Setup from scratch (Ubuntu 24.04, Docker + Compose installed, Tailscale on the host)

```bash
cd ~/docker
git clone <this repo> newsroom
cd newsroom
make setup CONTACT_EMAIL=you@example.org
```

`make setup` runs the following steps. Each one is safe to re-run, and each is also a
target of its own:

1. **`make init`:** writes `.env` (mode 600) with `TAILSCALE_IP` from `tailscale ip -4` and your
   contact email, then creates `/storage/newsroom/{db,backups,logos}` owned by UID 10001.
   It never overwrites other values in an existing `.env`.
2. **`make host-setup`:** adds the UFW rules (allow 8091 on `tailscale0`, deny it elsewhere) and
   installs a systemd drop-in so Docker waits for Tailscale at boot. It needs sudo and doesn't
   restart Docker. The optional DOCKER-USER rule stays manual (docs/security.md, section 3).
3. **`make test`:** lint and tests in a Docker build stage.
4. **`up --wait`:** builds, starts, and waits until both containers are healthy.
5. **`make verify`:** checks 8091 answers on Tailscale and not on the LAN or localhost.
6. **`config check`:** validates the YAML files.

The contact email goes into the User-Agent of outbound API requests (GDELT, Wikidata/Commons,
SEC, ProPublica), as Wikimedia's and SEC's policies require. It's never shown on the site,
and it lives only in `.env`, which is gitignored.

Then add `http://<tailscale-ip>:8091/healthz` to your service-check script.

### Firewall and binding

See [docs/security.md](docs/security.md) for the UFW rules, why the Compose bind address
(not UFW) is the control that actually matters with Docker, and how to make Docker wait
for Tailscale at boot.

## Common tasks

| Command            | What it does                                                         |
|--------------------|----------------------------------------------------------------------|
| `make up`          | Build and start (`docker compose up -d --build`)                     |
| `make down`        | Stop                                                                 |
| `make logs`        | Follow logs from both containers                                     |
| `make ps`          | Container status and health                                          |
| `make status`      | Overview: last ingestion, match counts, funding records, last backup |
| `make ingest-now`  | Fetch new articles now (also runs every 15 min on its own)           |
| `make retag`       | Recompute tags after editing `config/tags.yaml`                      |
| `make config-check`| Validate `config/outlets.yaml` and `config/tags.yaml`                |
| `make outlets`     | Outlets with article counts and latest article time                  |
| `make unmatched`   | Outlets without a Wikidata match, with candidate items               |
| `make ownership`   | Re-resolve ownership for every outlet now                            |
| `make funding`     | Look up funding records now and apply `config/public_funding.yaml`   |
| `make backup-db`   | Write a SQLite snapshot now                                          |
| `make test`        | Lint + tests inside a throwaway build stage (no Python needed on the host) |
| `make audit`       | `pip-audit` of the locked dependencies                               |
| `make verify`      | Check the port binding from the host                                 |
| `make shell`       | Shell in the worker container                                        |

## Configuration (`.env`)

| Variable          | Default            | Purpose                                                      |
|-------------------|--------------------|--------------------------------------------------------------|
| `TAILSCALE_IP`    | (from `make init`) | Only address the web port is published on                    |
| `WEB_PORT`        | `8091`             | Host port for the web UI (bound to `TAILSCALE_IP` only)      |
| `CONTACT_EMAIL`   | (asked by `make init`) | Contact in the outbound User-Agent (Wikimedia/SEC policy) |
| `STORAGE_DIR`     | `/storage/newsroom`| Host path for all persistent state                           |
| `TZ`              | `America/Toronto`  | Date grouping and schedule timezone                          |
| `LOG_LEVEL`       | `INFO`             |                                                              |
| `RATE_LIMIT`      | `120/minute`       | Per-IP limit on pages                                        |
| `SEARCH_RATE_LIMIT` | `30/minute`      | Per-IP limit on search                                       |
| `ALLOWED_HOSTS`   | `*`                | Comma-separated Host header allowlist (set when going public)|
| `TRUSTED_PROXIES` | (empty)            | CIDRs whose `CF-Connecting-IP`/`X-Forwarded-For` is trusted (going public) |
| `ENABLE_HSTS`     | `false`            | Only turn on once served over HTTPS                          |
| `INGEST_INTERVAL_MINUTES` | `15`       | How often the worker ingests (GDELT updates every 15 min)    |
| `INGEST_OFFSET_MINUTES` | `3`          | Minutes after each GDELT update to run (→ :03, :18, :33, :48) |
| `INGEST_BACKFILL_HOURS`   | `48`       | How far back a new outlet's first fetch reaches (DOC API only; the file source uses `INGEST_CATCHUP_HOURS`) |
| `INGEST_SOURCE`           | `gkg`      | `gkg`: GDELT's 15-minute files (recommended). `doc`: the DOC 2.0 search API |
| `INGEST_CATCHUP_HOURS`    | `6`        | After downtime or errors, how far back an outlet resumes (older gaps are skipped) |
| `RETENTION_DAYS`  | `365`              | Articles older than this are deleted nightly (0 = keep forever) |
| `FEED_OUTLET_CAP` | `3`                | Balanced feed: most articles shown per outlet per hour ("Show: Everything" shows all) |
| `GDELT_GROUP_SIZE`| `8`                | Outlets per GDELT query                                      |
| `GDELT_MIN_INTERVAL` | `20`            | Seconds to wait after each GDELT response (GDELT asks for ≥ 5 but refuses at 10) |
| `OWNERSHIP_REFRESH_DAYS` | `7`         | Re-check each outlet's Wikidata chain after this many days   |
| `BACKUP_HOUR`     | `3`                | Local hour for the nightly snapshot                          |
| `BACKUP_KEEP`     | `14`               | Snapshots to keep                                            |

## What gets ingested

**Outlets** come from `config/outlets.yaml`, a draft list of about 75 Canadian and US outlets
for you to edit. **Tags** come from `config/tags.yaml`: keyword lists, in English and French,
matched against headlines as whole words, ignoring case and accents. Both files are mounted
into the worker, so you can edit them with `micro` without rebuilding the image:

```bash
micro config/outlets.yaml
make config-check              # validate
make ingest-now                # new outlets are picked up on the next run anyway
micro config/tags.yaml && make retag
```

Every 15 minutes the worker asks GDELT for articles from these outlets, 8 outlets per request,
at most one request every 6 seconds. It stores **only metadata**: title, URL, outlet, date,
language and the GDELT image URL. The image URL is stored but never shown or fetched.
There's no article text. Duplicates are removed by canonical URL, which ignores `www.`,
tracking parameters, fragments and trailing slashes.

- **Where articles come from:** GDELT publishes everything it processed in each 15-minute
  window as a file at a fixed address (the Global Knowledge Graph, `YYYYMMDDHHMMSS.gkg.csv.zip`,
  plus a `.translation` file for non-English sources). Each run downloads the files it
  hasn't read yet, usually one of each, and keeps only lines whose link belongs to an outlet
  in `outlets.yaml`: headline, link, time, language, image. These are static downloads with
  no per-request quota, so they don't trip GDELT's rate limiting the way repeated searches do.
  Downloads are HTTPS from `data.gdeltproject.org` only (redirects elsewhere are refused),
  capped at 200 MB, spooled to `db/tmp` and deleted after reading; the zip is read line by
  line with limits on total size and line length.
- **Failures:** progress is saved after every file. A download error stops the run and the
  next run resumes from there. A file that isn't published yet is simply picked up next
  time; one GDELT never published is passed over only once a later file exists. Outlets
  that fell behind resume at most `INGEST_CATCHUP_HOURS` back. `make status` lists any
  outlets that are behind.
- **DOC API (optional):** `INGEST_SOURCE=doc` uses GDELT's search API instead. It refuses
  repeated requests from one address within about 20 s, so it waits `GDELT_MIN_INTERVAL`
  after each response and ends the run at the first refusal.
- **Dates:** GDELT only reports when it first *saw* an article. Every 15 minutes the worker reads
  the publication time from each new article's page metadata (schema.org `datePublished`,
  `article:published_time`, a few other standard tags) and cards show **Published …**. If the
  page gives no usable date, cards show **Seen …** (GDELT's time). Limits: articles from the
  last `PUBDATE_MAX_AGE_DAYS` (1) only, at most `PUBDATE_PER_RUN` (150) pages per pass, one
  request per second, robots.txt obeyed, sites that answer 403/429/5xx left alone for the run,
  at most two tries per page, only the first 1.5 MB read and only the date kept. Turn it off
  with `PUBDATE_FETCH=false`. Run it by hand with `docker compose exec worker newsroom pubdates`.

## Ownership

Every 6 hours the worker re-checks outlets whose chain is more than `OWNERSHIP_REFRESH_DAYS`
old. For each one it does the following:

1. **Match** the domain to a Wikidata item by "official website" (P856), trying http and
   https, with and without `www.`. Exactly one item gives an `auto` match. Several items give
   `ambiguous`, and the candidates are stored for you to pick from. No item gives `unmatched`.
   Matches that are `confirmed` or `manual` are never changed automatically.
2. **Walk up** "owned by" (P127) and "parent organization" (P749) to the top, at most 10 levels.
   Only statements Wikidata treats as current are used: no deprecated rank, preferred rank
   wins, and no end date in the past. A stake percentage is shown only when Wikidata states
   one (P1107). Loops are cut and flagged. Every link stores its Wikidata URL and retrieval
   date, and the page links to it.
3. **Cache the logo** (P154) from Wikimedia Commons as a small PNG, through the SSRF-guarded fetcher.

Data is fetched first and written in a single transaction, so a failed refresh changes nothing.
Wikidata calls need `CONTACT_EMAIL` set (Wikimedia's User-Agent policy). Without it the job
logs an error and skips.

Two labels cover missing records, and both link to their definitions on the About page:

- **"Not publicly disclosed"**: the outlet has no Wikidata item.
- **"No owner recorded"**: the item exists but states no owner, which is common for
  independent nonprofits.

### Curating matches

```bash
make unmatched                                                    # what needs attention
docker compose exec worker newsroom outlets set-qid twonames.ca Q12345   # pin to an item
docker compose exec worker newsroom outlets set-qid tiny.news none       # no item exists
docker compose exec worker newsroom outlets confirm cbc.ca ctvnews.ca    # lock auto matches
docker compose exec worker newsroom outlets confirm                      # ...or all of them
docker compose exec worker newsroom ownership show cbc.ca                # chain with sources
docker compose exec worker newsroom ownership resolve cbc.ca             # refresh one now
```

If Wikidata lacks a link you can document, add it with its source, which is mandatory:

```bash
docker compose exec worker newsroom ownership add-edge Q111 Q222 \
    --relation owned_by --source-url https://example.org/annual-report.pdf
docker compose exec worker newsroom ownership remove-edge Q111 Q222
```

The better long-term fix is to add the statement to Wikidata itself, with a reference.

## Funding

Funding is shown **only where a public record exists**, and each record links to that record.
Records are attached to the outlet's own Wikidata item or to any owner above it. The panel
lists the outlet first, then its owners.

| Source | Applies to | Keyed by | What is shown |
|---|---|---|---|
| SEC EDGAR | Public companies filing with the SEC (including Canadian 40-F filers) | SEC CIK | Link to the latest annual report (10-K / 20-F / 40-F) and to the filing index. No figures are extracted. |
| ProPublica Nonprofit Explorer | US nonprofits | IRS EIN | Total revenue and "contributions, gifts and grants", last 3 tax years (Form 990) |
| Canada Revenue Agency | Canadian registered charities | Business number (`123456789RR0001`) | Link to the charity's CRA listing, which holds its T3010 returns |
| `config/public_funding.yaml` | Anything else with a published figure, e.g. government funding of CBC/Radio-Canada, CPB funding of NPR/PBS | Outlet domain or QID | Exactly what you enter, with its source URL and the date you checked it |

- **Where IDs come from:** CIKs (Wikidata P5531) and EINs (P1297) come from Wikidata when the
  owner's item has them. Add missing ones, and every CRA business number, by hand. The
  source recorded is the registry's own page for that ID:

  ```bash
  docker compose exec worker newsroom entities set-id Q12345 ca_bn 123456789RR0001
  docker compose exec worker newsroom entities set-id Q67890 us_ein 12-3456789
  make funding
  docker compose exec worker newsroom funding show thenarwhal.ca
  ```

- **When it runs:** lookups run with the ownership refresh, every 6 hours, for identifiers not
  checked within `OWNERSHIP_REFRESH_DAYS`. A failed lookup keeps the previous records.
- **Nothing found:** the panel says "Funding: Not publicly disclosed".
- **Contact email:** SEC's fair-access policy requires a User-Agent with a contact address,
  so `CONTACT_EMAIL` must be set.

## CLI reference

Run inside the worker: `docker compose exec worker newsroom <command>`.

| Command            | What it does                                                   |
|--------------------|----------------------------------------------------------------|
| `status`           | Overview of ingestion, ownership, funding and backups          |
| `pubdates [--limit N]` | Read publication dates from recent article pages now       |
| `ingest`           | Fetch new articles now. Exits 1 unless the run was fully OK     |
| `retag`            | Recompute all tags from `tags.yaml`                            |
| `prune`            | Delete articles older than `RETENTION_DAYS`                    |
| `config check`     | Validate both YAML files                                       |
| `outlets list`     | Outlets, match status, QID, article counts                     |
| `outlets unmatched`| Unmatched/ambiguous outlets with candidate items               |
| `outlets set-qid D Q` | Pin outlet D to item Q (or `none`), then resolve it         |
| `outlets confirm [D...]` | Lock automatic matches (all if none given)               |
| `ownership resolve [D...] [--all]` | Re-run resolution (due outlets by default)     |
| `ownership show D` | Print an outlet's chain with source links                      |
| `ownership add-edge C P --source-url URL` | Record a sourced link missing from Wikidata |
| `ownership remove-edge C P` | Remove a manual link                                  |
| `funding refresh [--all]` | Look up funding records that are due (or all)           |
| `funding show D`   | Print an outlet's funding records with sources                 |
| `entities set-id Q SCHEME VALUE` | Record a `sec_cik`, `us_ein` or `ca_bn` for item Q |
| `entities remove-id Q SCHEME VALUE` | Remove a manually recorded identifier        |
| `backup`           | Write a consistent SQLite snapshot now                         |
| `migrate`          | Apply pending schema migrations (the worker does this on start) |

## Operations

- **Health:** `GET /healthz` returns `200 ok` and is never rate limited. Add
  `http://<tailscale-ip>:8091/healthz` to your service-check script. Both containers also have
  Docker healthchecks: `web` probes `/healthz`, and `worker` checks a heartbeat file it
  refreshes every 30 s. `make ps` shows both.
- **Status:** `make status` prints the last ingestion run, outlet match counts, funding record
  counts and the age of the latest backup.
- **Logs:** JSON lines on stdout, rotated by Docker at 5 × 10 MB per container. There is no
  access log, and visitor IPs are never logged. View with `make logs`.
- **Schedule (worker):**

  | Job | When |
  |---|---|
  | Ingestion | :03, :18, :33, :48 each hour (3 min after each GDELT update), plus once 30 s after start |
  | Publication dates | :08, :23, :38, :53 (5 min after each ingest), plus once 1 min after start |
  | Ownership + funding | Every 6 h for records older than 7 days, first run 3 min after start |
  | Backup | 03:00 |
  | Retention prune | 03:30 |

  Each kind of job has its own lock: two ingests never overlap, but a slow GDELT run doesn't
  hold up publication dates or ownership.
- **Backups:** every night the worker writes a snapshot with SQLite's online backup API.
  The snapshot is integrity-checked and then atomically renamed, so restic never sees a
  half-written file. The last `BACKUP_KEEP` snapshots are kept. Run `make backup-db` for one on
  demand. You can exclude `db/` itself from restic, since the snapshots in `backups/` are the
  consistent copies.
- **Restore:**

  ```bash
  make down
  sudo cp /storage/newsroom/backups/newsroom-YYYYMMDDTHHMMSSZ.sqlite3 /storage/newsroom/db/newsroom.sqlite3
  sudo rm -f /storage/newsroom/db/newsroom.sqlite3-wal /storage/newsroom/db/newsroom.sqlite3-shm
  sudo chown 10001:10001 /storage/newsroom/db/newsroom.sqlite3
  make up && make status
  ```
- **Updating:** `git pull && make test && make up`. Migrations apply automatically when the
  worker starts.

## Data sources and licences

| Source | Used for | Terms / attribution |
|---|---|---|
| [GDELT Project](https://www.gdeltproject.org/) 15-minute GKG files (or DOC 2.0 API) | Article metadata | Free, no key. Credited on the About page. About two downloads per 15 minutes. |
| [Wikidata](https://www.wikidata.org/) | Outlet ↔ item matching, ownership chains, SEC CIK / IRS EIN | CC0. Credited anyway. Descriptive User-Agent with contact, per Wikimedia policy. `maxlag` respected. |
| [Wikimedia Commons](https://commons.wikimedia.org/) | Outlet logos | Per-file licences. Each outlet page links to its file page. |
| [SEC EDGAR](https://www.sec.gov/edgar) | Links to public-company filings | US government public data. Fair-access policy: contact User-Agent, ≤ 10 req/s (we use ≤ 5). |
| [ProPublica Nonprofit Explorer](https://projects.propublica.org/nonprofits/) API | US nonprofit Form 990 figures | Credited by name on every record and on the About page. |
| [CRA List of charities](https://www.canada.ca/en/revenue-agency/services/charities-giving/list-charities.html) | Links to Canadian charity listings | Open Government Licence – Canada. Attribution on the About page. |
| `config/public_funding.yaml` | Published figures (e.g. public broadcasters) | Each entry carries its own source URL. |
| [htmx](https://htmx.org) 2.0.11 | Progressive enhancement | BSD 2-Clause, vendored and served locally. |

**Not used / not implemented:**

- **NewsData.io:** not used; there's no key.
- **Euromedia Ownership Monitor:** not imported, because EU outlets are out of scope for
  Canada + US. An importer would be one more module in `sources/`, alongside the others.

## Development

```bash
uv sync                 # Python 3.12 + dev tools from uv.lock
uv run pytest           # ~180 tests, all offline (recorded-format fixtures)
uv run ruff check . && uv run ruff format --check .
uv export --frozen --no-emit-project -q > /tmp/req.txt && uv run pip-audit --disable-pip --require-hashes -r /tmp/req.txt
NEWSROOM_DATA_DIR=./data NEWSROOM_CONFIG_DIR=./config uv run newsroom worker        # worker
NEWSROOM_DATA_DIR=./data uv run uvicorn newsroom.web.app:create_app --factory --reload  # web, :8000
```

On the server you don't need Python installed. `make test` runs lint and tests in a Docker
build stage, and `make audit` runs `pip-audit` the same way.

Dependencies are pinned with hashes in `uv.lock`. Update them with `uv lock --upgrade`, then
run `make test audit`.

**Adding a source:** write one module in `src/newsroom/sources/` following the interfaces in
`sources/base.py` (articles) or the `FundingRecord` pattern in `sources/funding.py`. Record
real responses as fixtures under `tests/fixtures/`, and wire it into `jobs.py`. Every row
it writes must carry `source`, `source_url` and `retrieved_at`.

## Third-party assets

- [htmx](https://htmx.org) 2.0.11, BSD 2-Clause (`src/newsroom/web/static/js/htmx.LICENSE.txt`),
  vendored from the npm registry and served locally. SHA-512 of the release tarball matched
  the registry's published integrity value.

## Going public

See [docs/going-public.md](docs/going-public.md). It uses a Cloudflare Tunnel from a
`cloudflared` container, so no router ports are opened. Switching over is a configuration
change: an override compose file plus `ALLOWED_HOSTS`, `TRUSTED_PROXIES` and `ENABLE_HSTS`.
