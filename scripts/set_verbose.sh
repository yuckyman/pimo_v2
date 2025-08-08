#!/usr/bin/env bash
set -euo pipefail

CONF="/etc/pimo/rss2discord.conf"

if [ ! -f "$CONF" ]; then
  echo "config not found: $CONF" >&2
  exit 1
fi

# Remove existing VERBOSE lines, then append VERBOSE=1
sed -i.bak '/^VERBOSE=/d' "$CONF"
echo 'VERBOSE=1' >> "$CONF"
echo "set VERBOSE=1 in $CONF"


