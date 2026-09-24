#!/usr/bin/env bash
# Host settings from docs/security.md, applied idempotently (needs sudo):
#   1. UFW: allow 8090 on tailscale0, deny 8090 elsewhere (skipped if ufw is absent/inactive)
#   2. systemd drop-in so Docker starts after Tailscale has its address at boot
# It does not restart Docker or touch /etc/ufw/after.rules (the optional
# DOCKER-USER rule stays a manual step; see docs/security.md section 3).
set -euo pipefail

PORT=8090
DROPIN_DIR=${DROPIN_DIR:-/etc/systemd/system/docker.service.d}
DROPIN=$DROPIN_DIR/10-wait-for-tailscale.conf

echo "== UFW"
if command -v ufw >/dev/null && sudo ufw status | grep -q "^Status: active"; then
  rules="$(sudo ufw status)"
  if grep -qE "^${PORT}/tcp on tailscale0 +ALLOW" <<<"$rules"; then
    echo "allow ${PORT}/tcp on tailscale0: already present"
  else
    # Insert at the top so it is evaluated before any existing deny for this port.
    if sudo ufw status numbered | grep -q '^\['; then
      sudo ufw insert 1 allow in on tailscale0 to any port "$PORT" proto tcp comment 'newsroom via Tailscale'
    else
      sudo ufw allow in on tailscale0 to any port "$PORT" proto tcp comment 'newsroom via Tailscale'
    fi
  fi
  if grep -qE "^${PORT}/tcp +DENY" <<<"$rules"; then
    echo "deny ${PORT}/tcp: already present"
  else
    sudo ufw deny "$PORT"/tcp comment 'newsroom: not on LAN/WAN'
  fi
  sudo ufw status numbered | grep -E "${PORT}" || true
else
  echo "ufw not installed or not active; skipping (the Compose bind address is the main control)."
fi

echo "== Docker waits for Tailscale at boot"
wanted="$(cat <<'EOF'
# Installed by newsroom scripts/host-setup.sh: Docker binds the web port to the
# Tailscale IP, which only exists once tailscaled is up.
[Unit]
After=tailscaled.service
Wants=tailscaled.service

[Service]
ExecStartPre=/bin/sh -c 'for i in $(seq 1 60); do ip -4 addr show dev tailscale0 2>/dev/null | grep -q "inet 100\." && exit 0; sleep 1; done; exit 0'
EOF
)"
if [[ -f "$DROPIN" ]] && [[ "$(sudo cat "$DROPIN")" == "$wanted" ]]; then
  echo "$DROPIN: already in place"
else
  sudo install -d -m 755 "$DROPIN_DIR"
  printf '%s\n' "$wanted" | sudo tee "$DROPIN" >/dev/null
  sudo systemctl daemon-reload
  echo "$DROPIN: installed (takes effect at next Docker start; no restart done now)"
fi
