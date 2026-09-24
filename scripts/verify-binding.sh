#!/usr/bin/env bash
# Checks from the host that WEB_PORT (default 8091) answers on Tailscale only.
# Also test from another LAN device with Tailscale OFF: http://<lan-ip>:<port> must fail.
set -uo pipefail
cd "$(dirname "$0")/.."

PORT="$(grep -E '^WEB_PORT=' .env | cut -d= -f2- || true)"
PORT="${PORT:-8091}"
ts_ip="$(grep -E '^TAILSCALE_IP=' .env | cut -d= -f2-)"
lan_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") print $(i+1)}')"
fail=0

pass() { printf '  PASS  %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1"; fail=1; }

echo "Listening sockets on :${PORT}"
ss -Htln "sport = :${PORT}" | awk '{print "  " $4}'
if ss -Htln "sport = :${PORT}" | awk '{print $4}' | grep -qE '^(0\.0\.0\.0|\*|\[::\]):'; then
  bad "port ${PORT} is bound to a wildcard address"
else
  pass "no wildcard bind on ${PORT}"
fi

echo "Requests"
if [[ "$(curl -fsS -m 3 "http://${ts_ip}:${PORT}/healthz" 2>/dev/null)" == "ok" ]]; then
  pass "Tailscale ${ts_ip}:${PORT} answers /healthz"
else
  bad "Tailscale ${ts_ip}:${PORT} did not answer (is the stack up? make ps)"
fi
for addr in "$lan_ip" 127.0.0.1; do
  [[ -z "$addr" ]] && continue
  if curl -fsS -m 3 -o /dev/null "http://${addr}:${PORT}/healthz" 2>/dev/null; then
    bad "${addr}:${PORT} is reachable (must not be)"
  else
    pass "${addr}:${PORT} refused"
  fi
done

echo "Security headers"
headers="$(curl -fsSI -m 3 "http://${ts_ip}:${PORT}/" 2>/dev/null || true)"
for h in content-security-policy x-content-type-options referrer-policy permissions-policy; do
  if grep -qi "^${h}:" <<<"$headers"; then pass "$h"; else bad "missing $h"; fi
done

exit "$fail"
