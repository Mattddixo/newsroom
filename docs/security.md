# Security notes

## Network exposure (Tailscale only)

### 1. The Compose bind address is the control that matters

`compose.yaml` publishes the web port as `${TAILSCALE_IP}:8091:8000`. Docker binds
only to that address, so connections to the LAN IP or `127.0.0.1` on 8091 are refused.
Compose refuses to start if `TAILSCALE_IP` is empty, so it cannot fall back to `0.0.0.0`.

**Why UFW alone is not enough:** Docker inserts its own iptables rules for published ports.
That traffic goes through the `FORWARD`/`DOCKER` chains, not UFW's `INPUT` rules, so a
plain `ufw deny 8091` does **not** block a published Docker port. That is why the bind
address, not UFW, is the primary control.

### 2. UFW rules (host policy and documentation)

```bash
sudo ufw allow in on tailscale0 to any port 8091 proto tcp comment 'newsroom via Tailscale'
sudo ufw deny 8091/tcp comment 'newsroom: not on LAN/WAN'
sudo ufw status numbered
```

The allow rule must come before the deny rule in `ufw status numbered`.

### 3. Optional defense in depth: filter Docker-forwarded traffic too

This drops new connections to host port 8091 unless they arrived on `tailscale0`, even if
someone later changes the bind to `0.0.0.0`. Append to the **end** of `/etc/ufw/after.rules`:

```
# BEGIN newsroom
*filter
:DOCKER-USER - [0:0]
-I DOCKER-USER -p tcp -m conntrack --ctorigdstport 8091 --ctstate NEW ! -i tailscale0 -j DROP
COMMIT
# END newsroom
```

Then run `sudo ufw reload && sudo iptables -L DOCKER-USER -n -v --line-numbers` and
check that the DROP rule is listed first.

### 4. Make Docker wait for Tailscale at boot

Docker cannot bind to `100.x.y.z` before `tailscaled` has brought up `tailscale0`. If Docker
starts first, the `web` container fails to start after a reboot. Add a drop-in:

```bash
sudo systemctl edit docker.service
```

```ini
[Unit]
After=tailscaled.service
Wants=tailscaled.service

[Service]
ExecStartPre=/bin/sh -c 'for i in $(seq 1 60); do ip -4 addr show dev tailscale0 2>/dev/null | grep -q "inet 100\\." && exit 0; sleep 1; done; exit 0'
```

The drop-in waits up to 60 s for the address and never blocks Docker permanently.
Test it with `sudo reboot`, then `make verify`.

### 5. Verify

```bash
make verify                                  # from the host
curl -m 3 http://<LAN-IP>:8091/healthz       # from another LAN device, Tailscale OFF: must fail
curl -m 3 http://<TAILSCALE-IP>:8091/healthz # from a tailnet device: prints "ok"
```

## Container hardening (compose.yaml, Dockerfile)

- Base images pinned by digest. Non-root UID 10001. Dependencies locked with hashes (`uv.lock`).
- `read_only: true` root filesystem. Writable locations are `/tmp` (16 MB tmpfs,
  `noexec`) and the `/storage/newsroom` bind mounts.
- `cap_drop: [ALL]`, `no-new-privileges`, `init: true`, CPU/memory/PID limits, healthchecks,
  `restart: unless-stopped`, and rotated `json-file` logs (5 × 10 MB per container).
- Only `web` publishes a port. The worker has no inbound exposure.

## Application

- Read-only site: `ReadOnlyMethodsMiddleware` answers anything but GET/HEAD with 405 before
  routing. The web process opens SQLite with `mode=ro` and `query_only`. There are no
  accounts and no forms that write. OpenAPI/docs endpoints are disabled.
- Headers on every response, including static files and error pages:
  - a strict CSP (`default-src 'none'`, scripts and styles only from `'self'`, no inline code)
  - `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`
  - `frame-ancestors 'none'` and `X-Frame-Options: DENY`
  - `Permissions-Policy` and COOP/CORP
  - HSTS only when `ENABLE_HSTS=true` (turn it on only behind HTTPS)
- htmx is vendored and served locally. It's configured through a meta tag, so it doesn't
  inject inline styles and has eval and script tags turned off. That lets the CSP stay strict.
- Jinja2 autoescaping is on for all templates (there's a test for it).
- Rate limiting (slowapi) is applied per client IP to all routes except `/healthz`.
- `TrustedHostMiddleware` checks the Host header against `ALLOWED_HOSTS` (set it when going public).
- **SSRF:** every server-side fetch of a URL that came from external data goes through
  `newsroom.net.safe_fetch`. It checks:
  - scheme (http/https only), port (80/443 only), and no credentials in the URL
  - the host is on an allowlist; IP-literal hosts are rejected
  - every resolved IP is public (private, loopback, link-local, CGNAT/Tailscale,
    metadata and multicast ranges are rejected)
  - the connection is pinned to the checked IP, which defeats DNS rebinding
  - all of the above again on each redirect (at most 3)
  - timeout, size cap and content-type allowlist; SVG is excluded
- Logging: JSON to stdout, with no access log and no visitor IPs. Secrets are never logged.
  There is no analytics or tracking.
- Client IP for rate limiting: forwarding headers (`CF-Connecting-IP`, `X-Forwarded-For`) are
  believed only from addresses in `TRUSTED_PROXIES`, which is empty by default. Anyone else is
  keyed by their own address, so a spoofed header can't dodge limits or burn someone else's.
- Templates use Jinja `StrictUndefined`: a missing variable is an error, never a silent blank
  that could turn into a false "Not publicly disclosed".
- Outbound API calls (GDELT, Wikidata, SEC, ProPublica) go only to fixed hosts, send a
  User-Agent with contact details, are paced, and back off on 429/5xx/`maxlag`. Anything
  fetched from a URL that came from external data (logos) goes through `safe_fetch`.

## Dev workflow checks

```bash
make test    # ruff check + ruff format --check + pytest, in a Docker build stage
make audit   # pip-audit against the hashed, locked requirements
```

Run both before deploying and after `uv lock --upgrade`.


## Checklist

Each requirement from the project brief, where it's implemented, and how it's verified.
"Verified in Docker" means it was checked against the built image running under
`compose.yaml` (read-only root, non-root user, capabilities dropped).

| Requirement | Implementation | Verified |
|---|---|---|
| Read-only from the browser | `ReadOnlyMethodsMiddleware` (GET/HEAD only); DB opened `mode=ro` + `query_only`; no forms that write; no accounts | Tests (`test_write_methods_rejected`); in Docker, a write through the web process's DB connection fails with "readonly database" |
| Port on Tailscale only | `${TAILSCALE_IP}:8091:8000`; Compose refuses an empty value | `docker inspect` shows a single HostIp binding; `make verify` on the host |
| UFW rules | This document, sections 2–3 (including the DOCKER-USER caveat) | On the host |
| Non-root | UID/GID 10001 in the image and in `compose.yaml` | In Docker: `id` shows `uid=10001` |
| Read-only rootfs | `read_only: true`; tmpfs `/tmp` (noexec); bind mounts only under `/storage/newsroom` | In Docker: writing to `/app` fails, `/tmp` works, `logos` is read-only for `web` |
| `cap_drop: [ALL]`, `no-new-privileges` | `compose.yaml` | In Docker: `CapEff: 0`, `NoNewPrivs: 1` |
| Resource limits, healthchecks, restart | `deploy.resources.limits` (CPU, memory, pids), healthchecks for both services, `restart: unless-stopped` | In Docker: both services `healthy`; limits shown by `docker inspect` |
| Base image pinned by digest | `Dockerfile` ARGs (python, uv); cloudflared in `going-public.md` | Review |
| Secrets only in `.env` | `.env` gitignored and dockerignored; `make init` writes it with mode 600; `.env.example` committed | Review |
| Strict CSP, no inline scripts | `SecurityHeadersMiddleware`; htmx served locally and configured by meta tag (no eval, no inline styles) | Tests; browser console shows no CSP errors on any page |
| nosniff, Referrer-Policy, frame-ancestors, Permissions-Policy | `SecurityHeadersMiddleware` on every response, including static files and errors | Tests; `curl -I` against the container |
| HSTS only behind HTTPS | `ENABLE_HSTS` (default off) | Tests |
| External data untrusted | Jinja autoescape; URLs checked to be http(s) at ingest and by a DB CHECK constraint; titles cleaned of control characters; no remote HTML rendered; all SQL parameterised | Tests (`test_article_links_are_safe`, `test_bad_url_rejected_by_schema`, autoescape) |
| SSRF guard | `net/safe_fetch.py`: allowlist, public-IP-only DNS with IP pinning, redirect re-checks, size/time/type caps; used for logos, article-page dates and robots.txt | 30+ tests in `test_safe_fetch.py` |
| Rate limiting | slowapi per client IP on all routes except `/healthz`; separate search limit; trusted-proxy aware | Tests; in Docker, through a simulated tunnel network: per-visitor buckets, spoofed headers ignored |
| `pip-audit` + ruff in workflow | `make test` (ruff + pytest) and `make audit` Docker stages; README "Development" | `make audit`: no known vulnerabilities at the time of writing |
| Logging | JSON, no access log, no IPs, no secrets, Docker rotation 5 × 10 MB | Review; container logs |
| No tracking | No cookies, analytics or third-party assets; `Referrer-Policy: no-referrer`; outbound links use `noopener noreferrer` | Tests; the axe/browser audit made no third-party requests |
| `/healthz` | Plain 200 `ok`, exempt from rate limits | Tests; container healthcheck |
| Idempotent, resumable ingestion | One transaction per query, cursor advances only on a fully OK run, file lock, crash-recovery tests | `test_ingest.py` |
| Consistent backups | SQLite online backup API, integrity check, atomic rename, pruning | `test_db_ops.py`; `newsroom backup` in Docker |
| Accessibility | Semantic HTML, skip link, visible focus, `aria-current`, `lang` on non-English headlines, table headers, contrast | axe-core (WCAG 2.1 A/AA + best practice): 0 violations on all pages, light and dark; keyboard-only walkthrough |

### Known limits

- **UFW and Docker:** UFW rules don't filter Docker-published ports unless you add the
  DOCKER-USER rule in section 3. The bind address is the real control.
- **Rate-limit state:** limits are kept in memory per `web` process, so they reset on restart.
  For a public site, also set the Cloudflare rate-limiting rule in `going-public.md`.
- **Egress:** the worker's outbound traffic isn't restricted at the network level. In code it
  contacts the fixed API hosts, Commons for logos, and (for publication dates) article pages
  and robots.txt on the configured outlets' own domains. The last two go through the
  allowlisted fetcher, limited per request to that outlet's domain.
