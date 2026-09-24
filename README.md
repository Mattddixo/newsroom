# newsroom

A self-hosted news feed that shows, for every article's outlet, **who owns it** and,
where a public record exists, **who funds it**. Every ownership or funding claim links
to its source record. Where no public record exists, the page says
"Not publicly disclosed". No bias ratings, no sentiment scores, no editorializing.

> Status: **Phase 4 (funding).** Articles (GDELT), ownership chains (Wikidata) and funding
> records (SEC EDGAR, ProPublica, CRA, curated public-broadcaster figures) are in place.

## How it runs

Two containers from one image:

| Service  | Role                                                                  | Network                          |
|----------|-----------------------------------------------------------------------|----------------------------------|
| `web`    | FastAPI, server-rendered pages. Opens SQLite **read-only**.           | `${TAILSCALE_IP}:8081` only      |
| `worker` | Scheduler: migrations, hourly ingestion, nightly snapshot and pruning. | Outbound only, no published port |

All state lives under `/storage/newsroom/` (restic already backs that up):

```
/storage/newsroom/
  db/        newsroom.sqlite3 (+ -wal, -shm)
  backups/   newsroom-YYYYMMDDTHHMMSSZ.sqlite3  (nightly, consistent, last 14 kept)
  logos/     cached outlet logos (phase 3)
```

## Setup from scratch (Ubuntu 24.04, Docker + Compose installed, Tailscale on the host)

```bash
cd ~/docker
git clone <this repo> newsroom
cd newsroom
make init        # creates /storage/newsroom/*, chowns to UID 10001, writes .env (asks for sudo)
make up          # builds and starts both containers
make verify      # checks 8081 answers on Tailscale and NOT on the LAN
```

`make init` fills `TAILSCALE_IP` from `tailscale ip -4` and asks for a contact
email (required by Wikimedia and SEC for API use, sent only in the User-Agent to those APIs).
Re-running it never overwrites an existing `.env`.

Then add `http://<tailscale-ip>:8081/healthz` to your service-check script.

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
| `make ingest-now`  | Fetch new articles now (also runs hourly on its own)                 |
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
| `CONTACT_EMAIL`   | (asked by `make init`) | Contact in the outbound User-Agent (Wikimedia/SEC policy) |
| `STORAGE_DIR`     | `/storage/newsroom`| Host path for all persistent state                           |
| `TZ`              | `America/Toronto`  | Date grouping and schedule timezone                          |
| `LOG_LEVEL`       | `INFO`             |                                                              |
| `RATE_LIMIT`      | `120/minute`       | Per-IP limit on pages                                        |
| `SEARCH_RATE_LIMIT` | `30/minute`      | Per-IP limit on search (phase 2)                             |
| `ALLOWED_HOSTS`   | `*`                | Comma-separated Host header allowlist (set when going public)|
| `ENABLE_HSTS`     | `false`            | Only turn on once served over HTTPS                          |
| `INGEST_INTERVAL_MINUTES` | `60`       | How often the worker ingests                                 |
| `INGEST_BACKFILL_HOURS`   | `72`       | How far back the very first run reaches                      |
| `RETENTION_DAYS`  | `365`              | Articles older than this are deleted nightly (0 = keep forever) |
| `GDELT_GROUP_SIZE`| `8`                | Outlets per GDELT query                                      |
| `GDELT_MIN_INTERVAL` | `6`             | Seconds between GDELT requests (GDELT asks for ≥ 5)          |
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

Once an hour the worker asks GDELT for articles from these outlets, 8 outlets per request,
at most one request every 6 seconds. It stores **only metadata**: title, URL, outlet, date,
language and the GDELT image URL. The image URL is stored but never shown or fetched.
There's no article text. Duplicates are removed by canonical URL, which ignores `www.`,
tracking parameters, fragments and trailing slashes.

- **Failures:** each GDELT request is committed separately. If a request fails after
  retries, the run is marked `partial`, and the next run re-covers the same time window.
- **Dates:** the date shown is GDELT's "first seen" time, usually minutes after publication.

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

Where no record exists, the site says **"Not publicly disclosed"**, linked to its definition
on the About page.

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

## Development

```bash
uv sync                 # Python 3.12 + dev tools from uv.lock
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pip-audit
uv run uvicorn newsroom.web.app:create_app --factory --reload   # http://127.0.0.1:8000
```

Dependencies are pinned with hashes in `uv.lock`. Update with `uv lock --upgrade`,
then `make test audit`.

## Third-party assets

- [htmx](https://htmx.org) 2.0.11, BSD 2-Clause (`src/newsroom/web/static/js/htmx.LICENSE.txt`),
  vendored from the npm registry and served locally. SHA-512 of the release tarball matched
  the registry's published integrity value.
