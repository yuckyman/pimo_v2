PROJECT: PiMO "appliance" pack — lean services that rotate, with nice login UX

GOALS
- weather + last.fm login splash (stdlib-only, fast)
- MQTT-based "sync mode" that triggers syncthing across boxes for a short window
- RSS → Discord relay via webhook (idempotent, cron-friendly)
- service-rotator wrapper to keep RAM low (only one major service at a time)

HOSTS
- PiMO (raspi zero 2 w): broker (mosquitto), rotator, webhook fetcher, splash
- yuckbox (linux server): syncthing listener (start/stop)
- eva01 (laptop or linux box): syncthing listener (start/stop)

SECURITY + NETWORK
- SSH only from LAN/Tailscale
- mosquitto bound to 127.0.0.1 by default; enable LAN only if needed + pw
- webhook URLs stored in root-readable file (0600)
- cron/systemd used for reliability; logs to /var/log/pimo/*.log

COMPONENTS
1) LOGIN SPLASH (PiMO)
   - ~/.local/bin/pimo_splash.py (ANSI box; open-meteo + last.fm)
   - hook in ~/.profile for SSH sessions

2) MQTT SYNC MODE
   - broker: mosquitto on PiMO (localhost-only or LAN-limited)
   - topics: pimo/sync  (payload: "start:<minutes>" | "stop")
   - publisher: /usr/local/bin/pimo-sync-pub
   - listeners (yuckbox, eva01): /usr/local/bin/pimo-sync-sub + systemd service
   - behavior: on start:N → start syncthing, set a timer to stop after N
               on stop → stop syncthing immediately

3) SERVICE ROTATOR (PiMO)
   - /etc/pimo/services.rotate (newline list)
   - /usr/local/bin/pimo-rotate (stops all, starts next; stores index)
   - cron: every 2h by default; can be “sync window” aware (publishes start/stop)

4) RSS → DISCORD RELAY (PiMO)
   - /usr/local/bin/rss2discord.py (stdlib: urllib + xml.etree)
   - state file: /var/lib/pimo/rss2discord.seen (hashes)
   - config: /etc/pimo/rss2discord.conf  (WEBHOOK_URL, FEEDS=..., MAX_PER_RUN)
   - cron: every 10 min

TEST PLAN
- splash: ssh in → see weather + last.fm
- mqtt: publish "start:15", watch syncthing start everywhere; auto-stop after 15m
- rotator: run manually, verify only the intended service is up
- rss relay: add one feed, new items appear in Discord

ROLLBACK & SAFE-OPS
- disable any component by masking its systemd unit or commenting cron line
- mosquitto off by default; enable only when MQTT flow is desired
- all scripts log to /var/log/pimo/ with timestamps