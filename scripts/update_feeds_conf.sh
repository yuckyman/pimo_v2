#!/usr/bin/env bash
set -euo pipefail

CONF="/etc/pimo/rss2discord.conf"

sudo mkdir -p /etc/pimo/feeds.d

# Install any lists staged in /tmp
shopt -s nullglob
lists=(/tmp/*.list)
if (( ${#lists[@]} > 0 )); then
  for f in "${lists[@]}"; do
    sudo install -Dm644 "$f" "/etc/pimo/feeds.d/$(basename "$f")"
  done
fi

# Ensure FEEDS= lines are removed (we prefer FEEDS_DIR)
if [ -f "$CONF" ]; then
  sudo sed -i.bak '/^FEEDS=/d' "$CONF"
else
  echo "WEBHOOK_URL=" | sudo tee "$CONF" >/dev/null
fi

# Ensure FEEDS_DIR is set to /etc/pimo/feeds.d/
if grep -q '^FEEDS_DIR=' "$CONF"; then
  sudo sed -i 's|^FEEDS_DIR=.*$|FEEDS_DIR=/etc/pimo/feeds.d/|' "$CONF"
else
  echo 'FEEDS_DIR=/etc/pimo/feeds.d/' | sudo tee -a "$CONF" >/dev/null
fi

echo "feeds configured: /etc/pimo/feeds.d/*.list and FEEDS_DIR set"


