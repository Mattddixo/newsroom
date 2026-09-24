# Going public (Cloudflare Tunnel)

Today the site is reachable only over Tailscale. This document describes how to publish it at,
for example, `news.matt-lab.ca` **without opening any router port**. It is written to be a
configuration change only: no application code changes are needed. None of this is active
until you do it.

## How it works

```
visitor ──HTTPS──▶ Cloudflare edge ──(outbound tunnel)──▶ cloudflared container ──HTTP──▶ web:8000
                                                          (on phatboislim)
you ──Tailscale──▶ ${TAILSCALE_IP}:8081 ──────────────────────────────────────────────▶ web:8000
```

`cloudflared` makes an outbound connection to Cloudflare; nothing listens on the LAN or WAN.
The existing Tailscale binding stays as it is.

## What the app already does for this

| Concern | Setting / behaviour |
|---|---|
| Host header spoofing | `ALLOWED_HOSTS` allowlist (TrustedHostMiddleware) |
| Rate limiting per real visitor | `TRUSTED_PROXIES`: only requests arriving *from* these addresses may set the client IP via `CF-Connecting-IP` / `X-Forwarded-For`; everyone else is keyed by their own address |
| HTTPS pinning | `ENABLE_HSTS=true` adds `Strict-Transport-Security` |
| Read-only surface | GET/HEAD only, no forms that write, no accounts, DB opened read-only |
| Privacy | No access log, no visitor IPs logged, no cookies, no analytics, no third-party assets |
| Images | Article images are never shown; logos are served from this site only |
| Crawlers | `/robots.txt` keeps crawlers off `/fragments/` |

## Steps

### 1. Create the tunnel (Cloudflare dashboard)

1. Zero Trust → Networks → Tunnels → **Create a tunnel** → type *Cloudflared*, name `newsroom`.
2. Copy the **tunnel token** (you only need the token, not the install command).
3. Public hostname → add `news.matt-lab.ca` → service **HTTP**, URL **`web:8000`**.

### 2. Add the override file

Create `~/docker/newsroom/compose.public.yaml`:

```yaml
services:
  web:
    networks: [default, edge]

  cloudflared:
    # Pinned by digest. Update: docker buildx imagetools inspect cloudflare/cloudflared:<tag>
    image: cloudflare/cloudflared:2026.9.1@sha256:b269e8abd07a5bf6f3f4be65d5050b2174eca89c56a0241a8ff32a16aec454e4
    command: ["tunnel", "--no-autoupdate", "run"]
    environment:
      TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN:?set CLOUDFLARE_TUNNEL_TOKEN in .env}
    networks: [edge]
    depends_on: [web]
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    restart: unless-stopped
    deploy:
      resources:
        limits: {cpus: "0.5", memory: 128M, pids: 64}
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "3"}

networks:
  edge:
    ipam:
      config:
        - subnet: 172.31.250.0/24
```

### 3. Update `.env`

```bash
micro .env
```

```ini
CLOUDFLARE_TUNNEL_TOKEN=<token from step 1>
# Public name, plus the names you use over Tailscale (IP and/or MagicDNS name):
ALLOWED_HOSTS=news.matt-lab.ca,100.x.y.z,phatboislim
# Only the tunnel network may tell us the visitor's IP:
TRUSTED_PROXIES=172.31.250.0/24
# Leave false until step 5 confirms HTTPS works end to end:
ENABLE_HSTS=false
```

### 4. Start it

```bash
docker compose -f compose.yaml -f compose.public.yaml up -d
docker compose -f compose.yaml -f compose.public.yaml logs -f cloudflared   # "Registered tunnel connection"
```

To avoid typing both files every time, add `COMPOSE_FILE=compose.yaml:compose.public.yaml`
to `.env`; plain `docker compose` / `make` then includes the tunnel.

### 5. Verify

```bash
curl -sI https://news.matt-lab.ca/ | grep -iE 'content-security|strict-transport|x-content'
curl -s  https://news.matt-lab.ca/healthz        # ok
make verify                                      # Tailscale still works, LAN still refused
sudo ss -tlnp | grep -E ':(80|443|8081)\b'       # nothing new listening
```

Then set `ENABLE_HSTS=true` and `docker compose up -d` again.

### 6. Cloudflare settings (dashboard)

- **SSL/TLS**: *Full*; **Always Use HTTPS** on; minimum TLS 1.2.
- **Turn off anything that injects scripts or rewrites HTML**, because the site's CSP blocks it
  and it would add tracking: *Rocket Loader*, *Email Address Obfuscation*, *Web Analytics /
  RUM beacon*, *Zaraz*. The site intentionally has no analytics.
- **Security → WAF → Rate limiting rules**: a coarse edge limit (e.g. 300 requests/minute per IP)
  in front of the app's own per-IP limits.
- **Caching**: a cache rule for `/static/*` and `/logos/*` is safe; do not cache `/` or
  `/fragments/*` (content changes hourly).
- Optionally **Bot Fight Mode**.

## Rolling back

```bash
docker compose -f compose.yaml -f compose.public.yaml down cloudflared
# remove COMPOSE_FILE from .env if you added it
docker compose up -d
```

Delete the tunnel in the Cloudflare dashboard to revoke its token. `ALLOWED_HOSTS`,
`TRUSTED_PROXIES` and `ENABLE_HSTS` can stay; they are harmless behind Tailscale.
Note that browsers remember HSTS for a year once they have seen it over HTTPS.

## Before going public: check

- [ ] `CONTACT_EMAIL` set; `make status` shows recent ingestion and a recent backup.
- [ ] `make test` and `make audit` pass on the current checkout.
- [ ] Outlet matches reviewed (`make unmatched` is empty or deliberately so).
- [ ] `config/public_funding.yaml` entries each re-checked against their source.
- [ ] About page reviewed; it is the public statement of method.
