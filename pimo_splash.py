#!/usr/bin/env python3
import os
import sys
import re
import json
import time
import socket
import shutil
import urllib.request
import urllib.parse
from datetime import datetime

LOG_PATH = "/var/log/pimo/pimo_splash.log"
CONFIG_PATH = os.path.expanduser("~/.config/pimo/splash.conf")

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_CYAN = "\033[36m"
ANSI_MAGENTA = "\033[35m"
ANSI_YELLOW = "\033[33m"
ANSI_GREEN = "\033[32m"
ANSI_BLUE = "\033[34m"


def log_line(message: str) -> None:
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {message}\n"
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def read_config(path: str) -> dict:
    cfg = {}
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                cfg[key] = val
    except Exception as e:
        log_line(f"config read error: {e}")
    return cfg


def http_get(url: str, timeout: float = 2.5) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "pimo-splash/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_lat_lon_from_ip():
    try:
        body = http_get("https://ipinfo.io/loc", timeout=2.0).decode("utf-8").strip()
        if "," in body:
            lat_s, lon_s = body.split(",", 1)
            return float(lat_s), float(lon_s)
    except Exception as e:
        log_line(f"ipinfo locate failed: {e}")
    return None, None


def get_weather(lat, lon):
    try:
        params = urllib.parse.urlencode({
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "current_weather": "true",
            "hourly": "temperature_2m,relative_humidity_2m,windspeed_10m,apparent_temperature,precipitation",
            "timezone": "auto",
        })
        url = f"https://api.open-meteo.com/v1/forecast?{params}"
        data = json.loads(http_get(url, timeout=2.5))
        return data
    except Exception as e:
        log_line(f"weather fetch failed: {e}")
        return None


def get_recent_tracks(user, api_key, limit=3):
    try:
        params = urllib.parse.urlencode({
            "method": "user.getrecenttracks",
            "user": user,
            "api_key": api_key,
            "format": "json",
            "limit": str(limit),
        })
        url = f"https://ws.audioscrobbler.com/2.0/?{params}"
        data = json.loads(http_get(url, timeout=2.5))
        tracks = data.get("recenttracks", {}).get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        out = []
        for t in tracks:
            artist = (t.get("artist") or {}).get("#text") or ""
            name = t.get("name") or ""
            album = (t.get("album") or {}).get("#text") or ""
            nowplaying = (t.get("@attr") or {}).get("nowplaying") == "true"
            ts = t.get("date", {}).get("uts")
            out.append({
                "artist": artist,
                "name": name,
                "album": album,
                "nowplaying": nowplaying,
                "uts": int(ts) if ts else None,
            })
        return out
    except Exception as e:
        log_line(f"lastfm fetch failed: {e}")
        return []


def get_weekly_scrobbles(user: str, api_key: str) -> int | None:
    try:
        # 1) Get weekly chart periods
        params_list = urllib.parse.urlencode({
            "method": "user.getweeklychartlist",
            "user": user,
            "api_key": api_key,
            "format": "json",
        })
        url_list = f"https://ws.audioscrobbler.com/2.0/?{params_list}"
        data_list = json.loads(http_get(url_list, timeout=2.5))
        charts = data_list.get("weeklychartlist", {}).get("chart", [])
        if not charts:
            return None
        last = charts[-1]
        frm = last.get("from")
        to = last.get("to")
        if not frm or not to:
            return None

        # 2) Fetch weekly track chart and sum playcounts
        params_chart = urllib.parse.urlencode({
            "method": "user.getweeklytrackchart",
            "user": user,
            "api_key": api_key,
            "format": "json",
            "from": frm,
            "to": to,
            "limit": "1000",
        })
        url_chart = f"https://ws.audioscrobbler.com/2.0/?{params_chart}"
        data_chart = json.loads(http_get(url_chart, timeout=2.5))
        tracks = data_chart.get("weeklytrackchart", {}).get("track", [])
        total = 0
        for t in tracks:
            try:
                total += int(t.get("playcount") or 0)
            except Exception:
                continue
        return total
    except Exception as e:
        log_line(f"lastfm weekly fetch failed: {e}")
        return None


def get_now_playing(user: str, api_key: str) -> dict | None:
    tracks = get_recent_tracks(user, api_key, limit=3)
    if not tracks:
        return None
    for t in tracks:
        if t.get("nowplaying"):
            return t
    return tracks[0]


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def visible_len(s: str) -> int:
    return len(strip_ansi(s))


def truncate_ansi(s: str, max_cols: int) -> str:
    if max_cols <= 0:
        return ""
    out_parts: list[str] = []
    cols = 0
    pos = 0
    for m in ANSI_RE.finditer(s):
        if m.start() > pos:
            seg = s[pos:m.start()]
            need = max_cols - cols
            if need <= 0:
                break
            if len(seg) > need:
                out_parts.append(seg[:need])
                cols += need
                pos = m.start()
                break
            else:
                out_parts.append(seg)
                cols += len(seg)
        out_parts.append(m.group(0))
        pos = m.end()
    if cols < max_cols and pos < len(s):
        need = max_cols - cols
        seg = s[pos:pos + need]
        out_parts.append(seg)
        cols += len(seg)
    out = "".join(out_parts)
    if not out.endswith(ANSI_RESET):
        out += ANSI_RESET
    return out


def format_box_colored(colored_lines: list[str], term_cols: int) -> str:
    if not colored_lines:
        return ""
    if term_cols is None or term_cols <= 0:
        term_cols = 80
    max_content_cols = max(10, term_cols - 4)
    max_line_visible = 0
    for line in colored_lines:
        max_line_visible = max(max_line_visible, visible_len(line))
    content_cols = min(max_line_visible, max_content_cols)
    border_width = content_cols + 2
    top = "┌" + ("─" * border_width) + "┐"
    bottom = "└" + ("─" * border_width) + "┘"
    body_rows: list[str] = []
    for line in colored_lines:
        truncated = truncate_ansi(line, content_cols)
        pad = max(0, content_cols - visible_len(truncated))
        row = "│ " + truncated + (" " * pad) + " │"
        body_rows.append(row)
    return "\n".join([top] + body_rows + [bottom])


def human_temp_f(temp_c):
    if temp_c is None:
        return "?°F"
    f = (temp_c * 9.0 / 5.0) + 32.0
    return f"{f:.1f}°F"


def weather_code_to_text(code) -> str:
    try:
        c = int(code)
    except Exception:
        return "?"
    text_by_exact = {
        0: "clear",
        1: "mostly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "fog",
        48: "rime fog",
        51: "drizzle",
        53: "drizzle",
        55: "drizzle",
        56: "freezing drizzle",
        57: "freezing drizzle",
        61: "rain",
        63: "rain",
        65: "heavy rain",
        66: "freezing rain",
        67: "freezing rain",
        71: "snow",
        73: "snow",
        75: "heavy snow",
        77: "snow grains",
        80: "rain showers",
        81: "rain showers",
        82: "heavy showers",
        85: "snow showers",
        86: "snow showers",
        95: "thunderstorm",
        96: "t-storm hail",
        99: "t-storm hail",
    }
    return text_by_exact.get(c, str(c))


def main():
    start = time.time()
    host = socket.gethostname()

    cfg = read_config(CONFIG_PATH)

    city = cfg.get("CITY", "")
    lat_s = cfg.get("LAT", "").strip()
    lon_s = cfg.get("LON", "").strip()
    lat = float(lat_s) if lat_s else None
    lon = float(lon_s) if lon_s else None

    if lat is None or lon is None:
        lat2, lon2 = get_lat_lon_from_ip()
        if lat2 is not None and lon2 is not None:
            lat, lon = lat2, lon2

    weather_line = "weather: unavailable"
    if lat is not None and lon is not None:
        w = get_weather(lat, lon)
        if w and w.get("current_weather"):
            cw = w["current_weather"]
            temp_c = cw.get("temperature")
            wind_ms = cw.get("windspeed")
            code = cw.get("weathercode")
            city_part = f" in {city}" if city else ""
            # Convert to imperial: F and mph
            temp_part = human_temp_f(temp_c)
            wind_mph = (wind_ms * 2.23694) if isinstance(wind_ms, (int, float)) else None
            wind_part = f"wind {wind_mph:.1f} mph" if wind_mph is not None else "wind ? mph"
            cond = weather_code_to_text(code)
            weather_line = f"{temp_part} / {wind_part} / {cond}{city_part}"

    lastfm_lines: list[str] = []
    lf_user = cfg.get("LASTFM_USER", "").strip()
    lf_key = cfg.get("LASTFM_API_KEY", "").strip()
    if lf_user and lf_key:
        now = get_now_playing(lf_user, lf_key)
        weekly = get_weekly_scrobbles(lf_user, lf_key)
        if now:
            prefix = "now:"
            icon = "▶" if now.get("nowplaying") else "♪"
            lastfm_lines.append(f"{ANSI_MAGENTA}last.fm {ANSI_DIM}{prefix}{ANSI_RESET} {icon} {now.get('artist','')} — {now.get('name','')}")
        if weekly is not None:
            lastfm_lines.append(f"{ANSI_MAGENTA}last.fm {ANSI_DIM}this week:{ANSI_RESET} {weekly} scrobbles")

    lines = []
    lines.append(f"{ANSI_BOLD}{ANSI_CYAN}hello friend.{ANSI_RESET}")
    lines.append("")
    lines.append(f"{ANSI_YELLOW}{weather_line}{ANSI_RESET}")
    if lastfm_lines:
        lines.extend(lastfm_lines)
    # No tip line per user preference

    try:
        term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    except Exception:
        term_cols = 80

    out = format_box_colored(lines, term_cols)
    print(out)

    elapsed = (time.time() - start) * 1000
    log_line(f"rendered splash in {elapsed:.0f}ms")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
