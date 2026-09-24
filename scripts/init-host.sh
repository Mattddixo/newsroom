#!/usr/bin/env bash
# One-time host setup: storage directories and .env. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

APP_UID=10001

if [[ -f .env ]]; then
  echo ".env already exists; leaving it unchanged."
else
  cp .env.example .env
  chmod 600 .env

  ts_ip="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  if [[ -z "$ts_ip" ]]; then
    ts_ip="$(ip -4 -o addr show dev tailscale0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1 || true)"
  fi
  if [[ ! "$ts_ip" =~ ^100\.([0-9]{1,3})\.[0-9]{1,3}\.[0-9]{1,3}$ ]] \
     || (( BASH_REMATCH[1] < 64 || BASH_REMATCH[1] > 127 )); then
    echo "Could not detect a Tailscale IPv4 (100.64.0.0/10). Is tailscaled up?" >&2
    rm -f .env
    exit 1
  fi
  sed -i "s/^TAILSCALE_IP=.*/TAILSCALE_IP=${ts_ip}/" .env
  echo "TAILSCALE_IP=${ts_ip}"

  email=""
  while [[ ! "$email" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; do
    read -r -p "Contact email for API User-Agent (Wikimedia/SEC policy): " email
  done
  sed -i "s/^CONTACT_EMAIL=.*/CONTACT_EMAIL=${email//\//\\/}/" .env
  echo "Wrote .env (mode 600)."
fi
chmod 600 .env

storage="$(grep -E '^STORAGE_DIR=' .env | cut -d= -f2-)"
storage="${storage:-/storage/newsroom}"
echo "Creating ${storage}/{db,backups,logos} owned by UID ${APP_UID} (needs sudo)"
sudo install -d -m 750 -o "$APP_UID" -g "$APP_UID" \
  "$storage" "$storage/db" "$storage/backups" "$storage/logos"
echo "Done. Next: make up && make verify"
