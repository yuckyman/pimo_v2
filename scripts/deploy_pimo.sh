#!/usr/bin/env bash
set -euo pipefail

# Usage: PIMO_TARGET=user@host ./scripts/deploy_pimo.sh

if [[ -z "${PIMO_TARGET:-}" ]]; then
  echo "Set PIMO_TARGET to user@host, e.g.: PIMO_TARGET=pi@pimo.local $0" >&2
  exit 2
fi

REMOTE_TMP="/tmp/pimo_deploy"

echo "[deploy] pushing files to $PIMO_TARGET:$REMOTE_TMP"
ssh -o BatchMode=yes "$PIMO_TARGET" "mkdir -p '$REMOTE_TMP'"

scp -q \
  rss2discord.py \
  pimo_rotator.py \
  configs/rss2discord.conf.example \
  configs/rotator.conf.example \
  configs/services.rotate.example \
  configs/cron.example \
  feeds/*.list \
  "$PIMO_TARGET":"$REMOTE_TMP/"

echo "[deploy] installing on remote (sudo required)"
ssh -t "$PIMO_TARGET" bash -lc "\
  set -euo pipefail; \
  sudo mkdir -p /usr/local/bin /etc/pimo /var/log/pimo /var/lib/pimo /etc/cron.d; \
  sudo install -Dm755 '$REMOTE_TMP/rss2discord.py' /usr/local/bin/rss2discord.py; \
  sudo install -Dm755 '$REMOTE_TMP/pimo_rotator.py' /usr/local/bin/pimo_rotator.py; \
  sudo install -Dm644 '$REMOTE_TMP/rss2discord.conf.example' /etc/pimo/rss2discord.conf; \
  sudo mkdir -p /etc/pimo/feeds.d; \
  for f in '$REMOTE_TMP'/*.list; do [ -f "$f" ] && sudo install -Dm644 "$f" "/etc/pimo/feeds.d/$(basename "$f")"; done; \
  sudo sed -i 's|^FEEDS=.*$||; $a FEEDS_DIR=/etc/pimo/feeds.d/' /etc/pimo/rss2discord.conf; \
  sudo install -Dm644 '$REMOTE_TMP/rotator.conf.example' /etc/pimo/rotator.conf; \
  sudo install -Dm644 '$REMOTE_TMP/services.rotate.example' /etc/pimo/services.rotate; \
  sudo install -Dm644 '$REMOTE_TMP/cron.example' /etc/cron.d/pimo; \
  sudo chmod 0644 /etc/cron.d/pimo; \
  sudo touch /var/log/pimo/rss2discord.log /var/log/pimo/rotator.log; \
  (sudo systemctl reload-or-restart cron || sudo service cron reload || true); \
  echo 'Installed: /usr/local/bin/rss2discord.py, /usr/local/bin/pimo_rotator.py'; \
  echo 'Configs:   /etc/pimo/rss2discord.conf, /etc/pimo/rotator.conf, /etc/pimo/services.rotate'; \
  echo 'Cron:      /etc/cron.d/pimo'; \
"

echo "[deploy] done. edit /etc/pimo/rss2discord.conf (WEBHOOK_URL, FEEDS) and /etc/pimo/services.rotate on the Pi."


