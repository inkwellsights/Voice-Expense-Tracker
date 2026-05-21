#!/usr/bin/env bash
# =============================================================================
# cloudflared-watchdog installer
# =============================================================================
#
# WHAT THIS DOES
#   Installs a small Python watchdog that runs every 5 minutes via systemd and
#   restores a Cloudflare Tunnel ingress rule (hostname -> origin) if it ever
#   goes missing. The Cloudflare dashboard's "Public Hostname" form rewrites
#   the entire ingress list on save and can silently drop entries — this script
#   is the safety net.
#
# WHAT IT DOES NOT DO
#   - It never deletes a rule.
#   - It never duplicates the watched rule (idempotent — silent if present).
#   - It does not depend on the `cloudflared` binary; it talks to the Cloudflare
#     API directly over HTTPS.
#
# WHAT YOU NEED FIRST
#   1. A Cloudflare API token with scope:
#        Account -> Cloudflare Tunnel -> Edit
#      scoped to your account, no TTL. (You'll be prompted for it interactively.)
#   2. These four identifiers (you'll be prompted for them too):
#        CF_ACCOUNT_ID      your Cloudflare account id (32-char hex)
#        CF_TUNNEL_ID       your tunnel id (UUID, visible in the tunnel's dashboard URL)
#        EXPECTED_HOSTNAME  e.g. expenses.example.com
#        EXPECTED_SERVICE   e.g. http://localhost:5006
#
# HOW TO USE
#   1. scp this whole folder to the box:
#        scp -r watchdog-bundle/ <HOST>:/tmp/
#   2. ssh in:
#        ssh <HOST>
#   3. As root:
#        sudo bash /tmp/watchdog-bundle/install.sh
#   4. When prompted, paste the Cloudflare API token (input is hidden).
#
# HOW TO INSPECT
#   Recent runs:         systemctl status cloudflared-watchdog.timer
#   Next scheduled run:  systemctl list-timers cloudflared-watchdog.timer
#   Service history:     journalctl -u cloudflared-watchdog --since "24 hours ago"
#   Restore-only log:    tail -f /var/log/cloudflared-watchdog.log
#   Manual one-shot run: systemctl start cloudflared-watchdog.service
#
# HOW TO UNINSTALL
#   sudo systemctl disable --now cloudflared-watchdog.timer
#   sudo rm -rf /etc/cloudflared-watchdog \
#               /etc/systemd/system/cloudflared-watchdog.{service,timer} \
#               /var/log/cloudflared-watchdog.log
#   sudo systemctl daemon-reload
#
# =============================================================================

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: run this as root (sudo bash $0)" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
ETC_DIR="/etc/cloudflared-watchdog"
SYSTEMD_DIR="/etc/systemd/system"
LOG_FILE="/var/log/cloudflared-watchdog.log"
TOKEN_FILE="${ETC_DIR}/token"
CONFIG_FILE="${ETC_DIR}/config"

echo "==> Creating ${ETC_DIR}"
install -d -m 0700 -o root -g root "${ETC_DIR}"

echo "==> Installing watchdog.py"
install -m 0750 -o root -g root "${SRC_DIR}/watchdog.py" "${ETC_DIR}/watchdog.py"

if [[ ! -s "${CONFIG_FILE}" ]]; then
  echo
  echo "==> Watchdog configuration — enter the four values for your tunnel:"
  read -r -p "    CF_ACCOUNT_ID:     " CF_ACCOUNT_ID
  read -r -p "    CF_TUNNEL_ID:      " CF_TUNNEL_ID
  read -r -p "    EXPECTED_HOSTNAME: " EXPECTED_HOSTNAME
  read -r -p "    EXPECTED_SERVICE:  " EXPECTED_SERVICE
  for v in CF_ACCOUNT_ID CF_TUNNEL_ID EXPECTED_HOSTNAME EXPECTED_SERVICE; do
    if [[ -z "${!v}" ]]; then
      echo "ERROR: ${v} cannot be empty" >&2
      exit 1
    fi
  done
  umask 077
  cat > "${CONFIG_FILE}" <<EOF
CF_ACCOUNT_ID=${CF_ACCOUNT_ID}
CF_TUNNEL_ID=${CF_TUNNEL_ID}
EXPECTED_HOSTNAME=${EXPECTED_HOSTNAME}
EXPECTED_SERVICE=${EXPECTED_SERVICE}
EOF
  chmod 0640 "${CONFIG_FILE}"
  chown root:root "${CONFIG_FILE}"
  echo "==> Config saved to ${CONFIG_FILE}"
else
  echo "==> Config already present at ${CONFIG_FILE} — leaving it alone"
fi

echo "==> Installing systemd unit + timer"
install -m 0644 -o root -g root "${SRC_DIR}/cloudflared-watchdog.service" \
  "${SYSTEMD_DIR}/cloudflared-watchdog.service"
install -m 0644 -o root -g root "${SRC_DIR}/cloudflared-watchdog.timer" \
  "${SYSTEMD_DIR}/cloudflared-watchdog.timer"

echo "==> Preparing log file at ${LOG_FILE}"
touch "${LOG_FILE}"
chmod 0640 "${LOG_FILE}"
chown root:adm "${LOG_FILE}"

if [[ ! -s "${TOKEN_FILE}" ]]; then
  echo
  echo "Paste the Cloudflare API token (input hidden, then press Enter):"
  # -s = silent (no echo); read into TOKEN
  read -r -s TOKEN
  echo
  if [[ -z "${TOKEN}" ]]; then
    echo "ERROR: empty token; aborting before enabling timer" >&2
    exit 1
  fi
  umask 077
  printf '%s\n' "${TOKEN}" > "${TOKEN_FILE}"
  unset TOKEN
  chmod 0600 "${TOKEN_FILE}"
  chown root:root "${TOKEN_FILE}"
  echo "==> Token saved to ${TOKEN_FILE} (chmod 600)"
else
  echo "==> Token already present at ${TOKEN_FILE} — leaving it alone"
  chmod 0600 "${TOKEN_FILE}"
fi

echo "==> Running watchdog once to validate token + connectivity"
if /usr/bin/python3 "${ETC_DIR}/watchdog.py"; then
  echo "    OK — exit 0"
else
  echo "    FAIL — check journal or ${LOG_FILE}, then re-run installer" >&2
  exit 1
fi

echo "==> Enabling timer"
systemctl daemon-reload
systemctl enable --now cloudflared-watchdog.timer

echo
echo "==> Done. Status:"
systemctl --no-pager status cloudflared-watchdog.timer | head -n 10 || true
echo
echo "    Next runs:           systemctl list-timers cloudflared-watchdog.timer"
echo "    Service history:     journalctl -u cloudflared-watchdog --since '24 hours ago'"
echo "    Restore-action log:  tail -f ${LOG_FILE}"
