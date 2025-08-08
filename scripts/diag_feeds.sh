#!/usr/bin/env bash
set -euo pipefail

CONF="/etc/pimo/rss2discord.conf"
FEEDDIR="/etc/pimo/feeds.d"

echo "=== conf ==="
sudo sed -n '1,200p' "$CONF" || true
echo

echo "=== feeds.d listing ==="
sudo ls -la "$FEEDDIR" || echo "(no feeds.d)"
echo

echo "=== preview files ==="
shopt -s nullglob
files=("$FEEDDIR"/*.list)
if (( ${#files[@]} == 0 )); then
  echo "(no *.list files)"
else
  for f in "${files[@]}"; do
    echo "---- $f ----"
    sudo sed -n '1,80p' "$f"
    echo
  done
fi

echo "=== header checks (first few) ==="
urls=()
for f in "${files[@]:-}"; do
  while IFS= read -r line; do
    line="${line%%#*}"
    line="${line//[$'\t\r\n ']*/}"
    [[ -z "$line" ]] && continue
    urls+=("$line")
    (( ${#urls[@]} >= 6 )) && break
  done < <(sudo sed -n '1,200p' "$f")
  (( ${#urls[@]} >= 6 )) && break
done

for url in "${urls[@]}"; do
  echo "-- $url"
  curl -fsSLI "$url" | sed -n '1,12p' || echo "curl failed"
done

echo "done"


