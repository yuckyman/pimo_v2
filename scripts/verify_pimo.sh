#!/usr/bin/env bash
set -euo pipefail

echo "=== PiMO verification ==="
echo "host: $(hostname)"
echo "kernel: $(uname -sr)"
echo

echo "-- binaries --"
for f in /usr/local/bin/rss2discord.py /usr/local/bin/pimo_rotator.py; do
  printf "%-40s" "$f"
  if [ -x "$f" ]; then echo "OK"; else echo "MISSING"; fi
done
echo

echo "-- configs --"
for f in /etc/pimo/rss2discord.conf /etc/pimo/rotator.conf /etc/pimo/services.rotate; do
  printf "%-40s" "$f"
  if [ -f "$f" ]; then echo "OK"; else echo "MISSING"; fi
done
echo

echo "-- cron --"
if [ -f /etc/cron.d/pimo ]; then
  echo "/etc/cron.d/pimo present"
  grep -v "^[[:space:]]*#" /etc/cron.d/pimo | sed "/^$/d" || true
else
  echo "cron file missing"
fi
echo

echo "-- python syntax --"
for f in /usr/local/bin/rss2discord.py /usr/local/bin/pimo_rotator.py; do
  if python3 -m py_compile "$f" 2>/dev/null; then
    echo "syntax OK: $f"
  else
    echo "syntax ERROR: $f"
  fi
done
echo

echo "-- service list preview --"
if [ -f /etc/pimo/services.rotate ]; then
  nl -ba /etc/pimo/services.rotate | sed -n "1,50p" || true
else
  echo "(no services.rotate)"
fi
echo

echo "-- log tails --"
for log in /var/log/pimo/rss2discord.log /var/log/pimo/rotator.log; do
  echo "== $log =="
  tail -n 5 "$log" 2>/dev/null || echo "(no log yet)"
done
echo

echo "verification complete"


