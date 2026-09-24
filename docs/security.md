# Security notes

## Network exposure (Tailscale only)

### 1. The Compose bind address is the control that matters

`compose.yaml` publishes the web port as `${TAILSCALE_IP}:8081:8000`. Docker binds
only to that address, so connections to the LAN IP or `127.0.0.1` on 8081 are refused.
Compose refuses to start if `TAILSCALE_IP` is empty, so it cannot fall back to `0.0.0.0`.

**Why UFW alone is not enough:** Docker inserts its own iptables rules for published ports.
That traffic goes through the `FORWARD`/`DOCKER` chains, not UFW's `INPUT` rules, so a
plain `ufw deny 8081` does **not** block a published Docker port. That is why the bind
address, not UFW, is the primary control.

### 2. UFW rules (host policy and documentation)

```bash
sudo ufw allow in on tailscale0 to any port 8081 proto tcp comment 'newsroom via Tailscale'
sudo ufw deny 8081/tcp comment 'newsroom: not on LAN/WAN'
sudo ufw status numbered
```

The allow rule must come before the deny rule in `ufw status numbered`.

### 3. Optional defense in depth: filter Docker-forwarded traffic too

This drops new connections to host port 8081 unless they arrived on `tailscale0`, even if
someone later changes the bind to `0.0.0.0`. Append to the **end** of `/etc/ufw/after.rules`:

```
# BEGIN newsroom
*filter
:DOCKER-USER - [0:0]
-I DOCKER-USER -p tcp -m conntrack --ctorigdstport 8081 --ctstate NEW ! -i tailscale0 -j DROP
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
curl -m 3 http://<LAN-IP>:8081/healthz       # from another LAN device, Tailscale OFF: must fail
curl -m 3 http://<TAILSCALE-IP>:8081/healthz # from a tailnet device: prints "ok"
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

## Dev workflow checks

```bash
make test    # ruff check + ruff format --check + pytest, in a Docker build stage
make audit   # pip-audit against the hashed, locked requirements
```

Run both before deploying and after `uv lock --upgrade`.
