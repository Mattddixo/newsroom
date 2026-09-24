# newsroom

A self-hosted news feed that shows, for every article's outlet, **who owns it** and,
where a public record exists, **who funds it**. Every ownership or funding claim links
to its source record. Where no public record exists, the page says
"Not publicly disclosed". No bias ratings, no sentiment scores, no editorializing.

> Status: **Phase 1 (skeleton).** The web app, hardened containers, backups and the
> SSRF-guarded fetcher are in place. Articles, ownership and funding arrive in later phases.

## How it runs

Two containers from one image:

| Service  | Role                                                                  | Network                          |
|----------|-----------------------------------------------------------------------|----------------------------------|
| `web`    | FastAPI, server-rendered pages. Opens SQLite **read-only**.           | `${TAILSCALE_IP}:8081` only      |
| `worker` | Scheduler: migrations, ingestion (phase 2+), nightly SQLite snapshot. | Outbound only, no published port |

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
| `BACKUP_HOUR`     | `3`                | Local hour for the nightly snapshot                          |
| `BACKUP_KEEP`     | `14`               | Snapshots to keep                                            |

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
