#!/usr/bin/env bash
# One-time host setup: storage directories and .env. Safe to re-run.
#
# Non-interactive:  CONTACT_EMAIL=you@example.org ./scripts/init-host.sh
# (or: make init CONTACT_EMAIL=you@example.org). TAILSCALE_IP may also be given;
# otherwise it is detected. An existing .env is never overwritten, but an empty
# CONTACT_EMAIL in it is filled in when one is supplied.
set -euo pipefail
cd "$(dirname "$0")/.."

APP_UID=10001
EMAIL_RE='^[^[:space:]@/]+@[^[:space:]@/]+\.[^[:space:]@/]+$'

set_var() {  # set_var NAME VALUE  -> replace the NAME= line in .env
  local name=$1 value=$2
  sed -i "s/^${name}=.*/${name}=${value//\//\\/}/" .env
}

get_var() {
  grep -E "^$1=" .env | cut -d= -f2- || true
}

detect_ts_ip() {
  local ip
  ip="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  if [[ -z "$ip" ]]; then
    ip="$(ip -4 -o addr show dev tailscale0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1 || true)"
  fi
  echo "$ip"
}

valid_ts_ip() {  # 100.64.0.0/10
  [[ "$1" =~ ^100\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] \
    && (( BASH_REMATCH[1] >= 64 && BASH_REMATCH[1] <= 127 \
          && BASH_REMATCH[2] <= 255 && BASH_REMATCH[3] <= 255 ))
}

# 1. Work out every value before writing anything, so a failed run leaves no half-made .env.
existing_email=""
[[ -f .env ]] && existing_email="$(get_var CONTACT_EMAIL)"
email="${CONTACT_EMAIL:-$existing_email}"
while [[ ! "$email" =~ $EMAIL_RE ]]; do
  if [[ ! -t 0 ]]; then
    echo "CONTACT_EMAIL missing or invalid; run: make init CONTACT_EMAIL=you@example.org" >&2
    exit 1
  fi
  read -r -p "Contact email for API User-Agent (Wikimedia/SEC policy): " email
done

ts_ip=""
if [[ -n "${TAILSCALE_IP:-}" ]]; then
  ts_ip="$TAILSCALE_IP"
elif [[ ! -f .env ]]; then
  ts_ip="$(detect_ts_ip)"
fi
if [[ -n "$ts_ip" || ! -f .env ]] && ! valid_ts_ip "$ts_ip"; then
  echo "Not a Tailscale IPv4 (100.64.0.0/10): '${ts_ip}'. Is tailscaled up?" >&2
  echo "You can also pass it: make init TAILSCALE_IP=100.x.y.z" >&2
  exit 1
fi

# 2. Write.
if [[ -f .env ]]; then
  echo ".env already exists; keeping its other values."
else
  cp .env.example .env
fi
chmod 600 .env
if [[ -n "$ts_ip" && "$(get_var TAILSCALE_IP)" != "$ts_ip" ]]; then
  set_var TAILSCALE_IP "$ts_ip"
  echo "TAILSCALE_IP=${ts_ip}"
fi
if [[ "$(get_var CONTACT_EMAIL)" != "$email" ]]; then
  set_var CONTACT_EMAIL "$email"
  echo "CONTACT_EMAIL=${email}"
fi

storage="$(get_var STORAGE_DIR)"
storage="${storage:-/storage/newsroom}"
echo "Creating ${storage}/{db,backups,logos} owned by UID ${APP_UID} (needs sudo)"
sudo install -d -m 750 -o "$APP_UID" -g "$APP_UID" \
  "$storage" "$storage/db" "$storage/backups" "$storage/logos"
echo ".env ready (mode 600)."
