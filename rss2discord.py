#!/usr/bin/env python3
"""
PiMO RSS → Discord relay (stdlib-only)

Features
- Reads config from /etc/pimo/rss2discord.conf (key=value)
- Fetches multiple RSS/Atom feeds
- Tracks seen items in a state file; idempotent and cron-friendly
- Posts new items to a Discord webhook as plain content messages

Config keys (example):
  WEBHOOK_URL=https://discord.com/api/webhooks/...
  FEEDS=https://example.com/feed.xml, https://another.example/feed
  FEEDS_FILE=/etc/pimo/feeds.list   (newline-separated; supports comments)
  FEEDS_DIR=/etc/pimo/feeds.d/      (reads all files in dir; newline-separated)
  MAX_PER_RUN=5
  USER_AGENT=pimo-rss2discord/1.0 (+https://example)
  TIMEOUT_SECONDS=5
  LOG_PATH=/var/log/pimo/rss2discord.log
  STATE_PATH=/var/lib/pimo/rss2discord.seen
  VERBOSE=0  (set to 1 for detailed per-feed logs)

Notes
- If LOG_PATH or STATE_PATH are not writable, the script will fall back to
  user-writable locations under ~/.local/state/pimo/ and ~/.local/share/pimo/.
- Intended to be run via cron, e.g. every 10 minutes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable, List, Optional, Tuple
import gzip
import zlib
import concurrent.futures as cf


DEFAULT_LOG_PATH = "/var/log/pimo/rss2discord.log"
DEFAULT_STATE_PATH = "/var/lib/pimo/rss2discord.seen"
DEFAULT_CONFIG_PATH = "/etc/pimo/rss2discord.conf"
DEFAULT_META_PATH = "/var/lib/pimo/rss2discord.meta.json"
DEFAULT_FETCH_BUDGET_SECONDS = 10.0


def ensure_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)


def log_line(message: str, log_path: str) -> None:
    try:
        ensure_dir(log_path)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        # Last-ditch: stderr
        try:
            sys.stderr.write(message + "\n")
        except Exception:
            pass


def read_config(path: str) -> dict:
    cfg: dict[str, str] = {}
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
    except Exception:
        pass
    return cfg


def split_feeds(value: str) -> List[str]:
    # Accept comma, space, or newline separated values
    if not value:
        return []
    raw = value.replace("\n", ",").replace(" ", ",")
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def read_feed_lines_file(path: str) -> List[str]:
    feeds: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # allow inline trailing comments
                if "#" in line:
                    line = line.split("#", 1)[0].strip()
                if line:
                    feeds.append(line)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return feeds


def read_feeds_dir(path: str) -> List[str]:
    entries: List[str] = []
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if not os.path.isfile(full):
                continue
            entries.extend(read_feed_lines_file(full))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return entries


def normalize_feed_entry(entry: str) -> str:
    # Short-form: r/<subreddit>
    if entry.startswith("r/") and "://" not in entry:
        sub = entry[2:].strip().strip('/')
        if sub:
            return f"https://www.reddit.com/r/{sub}/.rss"

    # If it looks like a URL, possibly patch Are.na to .rss
    try:
        parsed = urllib.parse.urlparse(entry)
        if parsed.scheme in ("http", "https"):
            if parsed.hostname and "are.na" in parsed.hostname:
                if not parsed.path.endswith(".rss") and not parsed.path.endswith("/rss"):
                    # append .rss to channel URLs
                    new_path = parsed.path + ".rss"
                    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, new_path, parsed.params, parsed.query, parsed.fragment))
            return entry
    except Exception:
        pass
    return entry


def parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _decompress_body(body: bytes, headers: dict) -> bytes:
    try:
        enc = (headers.get("Content-Encoding") or headers.get("content-encoding") or "").lower()
        if not enc:
            return body
        if "gzip" in enc:
            return gzip.decompress(body)
        if "deflate" in enc:
            try:
                return zlib.decompress(body)
            except Exception:
                return zlib.decompress(body, -zlib.MAX_WBITS)
        return body
    except Exception:
        return body


def http_get(url: str, timeout: float, user_agent: str) -> bytes:
    headers = {
        "User-Agent": user_agent or "pimo-rss2discord/1.0",
        "Accept-Encoding": "gzip, deflate",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        return _decompress_body(data, dict(resp.headers))


def http_get_conditional(url: str, timeout: float, user_agent: str, etag: Optional[str], last_modified: Optional[str]) -> Tuple[int, bytes, dict]:
    headers = {
        "User-Agent": user_agent or "pimo-rss2discord/1.0",
        "Accept-Encoding": "gzip, deflate",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            data = _decompress_body(raw, dict(resp.headers))
            return resp.getcode(), data, dict(resp.headers)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            # Not Modified
            return 304, b"", dict(e.headers)
        raw = e.read()
        # Do not attempt to decompress error bodies
        return e.code, raw, dict(e.headers)


def http_post_json(url: str, payload: dict, timeout: float, user_agent: str) -> Tuple[int, bytes, dict]:
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "User-Agent": user_agent or "pimo-rss2discord/1.0",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def parse_rfc2822_date(text: str) -> Optional[int]:
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def parse_iso8601_date(text: str) -> Optional[int]:
    try:
        # Make common forms acceptable to fromisoformat
        # Replace trailing Z with +00:00
        cleaned = text.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


@dataclass
class FeedItem:
    feed_url: str
    unique_id: str
    title: str
    link: str
    published_ts: Optional[int]


def parse_feed(feed_url: str, xml_bytes: bytes) -> List[FeedItem]:
    items: List[FeedItem] = []
    try:
        # Some feeds include odd leading characters or BOM; strip leading whitespace
        xml_clean = xml_bytes.lstrip()
        root = ET.fromstring(xml_clean)
    except Exception:
        return items

    # Detect Atom vs RSS by tag
    tag = root.tag.lower()
    if tag.endswith("feed") and ("atom" in tag or root.tag.startswith("{http://www.w3.org/2005/Atom}")):
        ns = "{http://www.w3.org/2005/Atom}"
        for entry in root.findall(f"{ns}entry"):
            title = (entry.findtext(f"{ns}title") or "").strip()
            link = ""
            for l in entry.findall(f"{ns}link"):
                rel = l.get("rel")
                href = l.get("href") or ""
                if href and (rel is None or rel == "alternate"):
                    link = href
                    break
            entry_id = (entry.findtext(f"{ns}id") or link or title).strip()
            pub = (entry.findtext(f"{ns}published") or entry.findtext(f"{ns}updated") or "").strip()
            ts = parse_iso8601_date(pub) if pub else None
            items.append(FeedItem(feed_url, entry_id or link or title, title, link, ts))
        return items

    # Assume RSS 2.0 or RDF; try namespace-agnostic finds
    channel = root.find("channel")
    if channel is None:
        # Some feeds put items at root
        candidates = root.findall("item") or root.findall("{*}item")
    else:
        candidates = channel.findall("item") or channel.findall("{*}item")
    for it in candidates:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link or title).strip()
        pub = (it.findtext("pubDate") or "").strip()
        ts = parse_rfc2822_date(pub) if pub else None
        items.append(FeedItem(feed_url, guid or link or title, title, link, ts))
    return items


def compute_seen_key(item: FeedItem) -> str:
    unique = f"{item.feed_url}\n{item.unique_id}\n{item.link}\n{item.title}"
    return hashlib.sha1(unique.encode("utf-8")).hexdigest()


def load_seen(path: str) -> set[str]:
    seen: set[str] = set()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    h = line.strip()
                    if h:
                        seen.add(h)
    except Exception:
        pass
    return seen


def save_seen(path: str, seen: Iterable[str]) -> None:
    ensure_dir(path)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for h in sorted(set(seen)):
                f.write(h + "\n")
        os.replace(tmp, path)
    except Exception:
        # Best-effort fallback without atomic replace
        try:
            with open(path, "w", encoding="utf-8") as f:
                for h in sorted(set(seen)):
                    f.write(h + "\n")
        except Exception:
            pass


def choose_paths(cfg: dict) -> Tuple[str, str]:
    log_path = cfg.get("LOG_PATH", DEFAULT_LOG_PATH)
    state_path = cfg.get("STATE_PATH", DEFAULT_STATE_PATH)

    # If not writable, choose user-local fallbacks
    try:
        ensure_dir(log_path)
        with open(log_path, "a", encoding="utf-8"):
            pass
    except Exception:
        home_log = os.path.expanduser("~/.local/share/pimo/rss2discord.log")
        log_path = home_log

    try:
        ensure_dir(state_path)
        if not os.path.exists(state_path):
            # create empty file
            with open(state_path, "w", encoding="utf-8"):
                pass
    except Exception:
        state_path = os.path.expanduser("~/.local/state/pimo/rss2discord.seen")
        ensure_dir(state_path)
        if not os.path.exists(state_path):
            with open(state_path, "w", encoding="utf-8"):
                pass

    return log_path, state_path


def load_meta(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_meta(path: str, meta: dict) -> None:
    ensure_dir(path)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, path)
    except Exception:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(meta, f)
        except Exception:
            pass


def post_with_retry(webhook_url: str, payload: dict, timeout: float, user_agent: str, log_path: str) -> bool:
    code, body, headers = http_post_json(webhook_url, payload, timeout, user_agent)
    if code == 204 or code == 200:
        return True
    if code == 429:
        retry_after = 0.0
        try:
            val = headers.get("Retry-After") or headers.get("retry-after")
            if val is not None:
                retry_after = float(val)
        except Exception:
            retry_after = 1.0
        time.sleep(min(max(retry_after, 0.0), 10.0))
        code2, body2, _ = http_post_json(webhook_url, payload, timeout, user_agent)
        if code2 == 204 or code2 == 200:
            return True
        log_line(f"webhook failed after retry: HTTP {code2} {body2[:200]!r}", log_path)
        return False
    log_line(f"webhook post error: HTTP {code} {body[:200]!r}", log_path)
    return False


def format_discord_message(item: FeedItem, feed_name: Optional[str] = None) -> str:
    title = item.title or "(untitled)"
    link = item.link or ""
    prefix = f"{feed_name}: " if feed_name else ""
    if link:
        return f"📰 {prefix}{title}\n{link}"
    return f"📰 {prefix}{title}"


def extract_feed_name(feed_url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(feed_url)
        host = parsed.hostname or "feed"
        path = (parsed.path or "/").rstrip("/")
        tail = path.split("/")[-1] or host
        return f"{host}/{tail}"
    except Exception:
        return feed_url


def main(argv: List[str]) -> int:
    cfg_path = os.environ.get("PIMO_RSS2DISCORD_CONFIG", DEFAULT_CONFIG_PATH)
    cfg = read_config(cfg_path)

    log_path, state_path = choose_paths(cfg)

    webhook_url = cfg.get("WEBHOOK_URL", "").strip()
    if not webhook_url:
        log_line("no WEBHOOK_URL configured — exiting", log_path)
        return 2

    # Build dynamic feeds list from FEEDS, FEEDS_FILE, FEEDS_DIR
    feeds: List[str] = []
    feeds += split_feeds(cfg.get("FEEDS", ""))
    ff = cfg.get("FEEDS_FILE", "").strip()
    if ff:
        feeds += read_feed_lines_file(ff)
    fd = cfg.get("FEEDS_DIR", "").strip()
    if fd:
        feeds += read_feeds_dir(fd)
    # normalize and de-dup while preserving order
    norm: List[str] = []
    seen_norm: set[str] = set()
    for e in feeds:
        ne = normalize_feed_entry(e)
        if ne and ne not in seen_norm:
            seen_norm.add(ne)
            norm.append(ne)
    feeds = norm
    if not feeds:
        log_line("no FEEDS configured — exiting", log_path)
        return 2

    max_per_run = 5
    try:
        if cfg.get("MAX_PER_RUN"):
            max_per_run = max(1, int(cfg["MAX_PER_RUN"]))
    except Exception:
        pass

    timeout_s = 5.0
    try:
        if cfg.get("TIMEOUT_SECONDS"):
            timeout_s = max(1.0, float(cfg["TIMEOUT_SECONDS"]))
    except Exception:
        pass

    user_agent = cfg.get("USER_AGENT", "pimo-rss2discord/1.0")
    verbose = parse_bool(cfg.get("VERBOSE"), False)

    # Concurrency and metadata cache
    try:
        max_conc = int(cfg.get("MAX_CONCURRENCY", "8"))
        if max_conc < 1:
            max_conc = 1
        if max_conc > 32:
            max_conc = 32
    except Exception:
        max_conc = 8

    meta_path = cfg.get("META_PATH", DEFAULT_META_PATH)
    meta = load_meta(meta_path)

    try:
        fetch_budget_s = float(cfg.get("FETCH_BUDGET_SECONDS", str(DEFAULT_FETCH_BUDGET_SECONDS)))
        if fetch_budget_s < 1.0:
            fetch_budget_s = 1.0
        if fetch_budget_s > 60.0:
            fetch_budget_s = 60.0
    except Exception:
        fetch_budget_s = DEFAULT_FETCH_BUDGET_SECONDS

    seen = load_seen(state_path)
    new_seen: set[str] = set(seen)

    # Collect items from all feeds in parallel with conditional GETs
    all_items: List[FeedItem] = []

    def worker(url: str) -> Tuple[str, List[FeedItem], dict]:
        etag = None
        last_mod = None
        try:
            m = meta.get(url) or {}
            etag = m.get("etag")
            last_mod = m.get("last_modified")
        except Exception:
            pass
        try:
            status, body, headers = http_get_conditional(url, timeout_s, user_agent, etag, last_mod)
            if status == 304:
                # not modified
                if verbose:
                    log_line(f"{url} — not modified (304)", log_path)
                return url, [], headers
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            if verbose:
                size_kb = len(body) / 1024.0
                log_line(f"{url} — fetched {size_kb:.1f}KB", log_path)
            items = parse_feed(url, body)
            if verbose:
                log_line(f"{url} — parsed {len(items)} items", log_path)
            return url, items, headers
        except Exception as e:
            log_line(f"fetch error for {url}: {e}", log_path)
            return url, [], {}

    with cf.ThreadPoolExecutor(max_workers=max_conc) as ex:
        futures = {ex.submit(worker, u): u for u in feeds}
        done, not_done = cf.wait(set(futures.keys()), timeout=fetch_budget_s)
        for fut in done:
            try:
                url, items, headers = fut.result()
                all_items.extend(items)
                if headers:
                    new_etag = headers.get("ETag") or headers.get("etag")
                    new_lm = headers.get("Last-Modified") or headers.get("last-modified")
                    if new_etag or new_lm:
                        meta[url] = {
                            "etag": new_etag or meta.get(url, {}).get("etag"),
                            "last_modified": new_lm or meta.get(url, {}).get("last_modified"),
                        }
                if verbose and items:
                    log_line(f"{url} — keeping {len(items)} items (pre-filter)", log_path)
            except Exception as e:
                log_line(f"worker error: {e}", log_path)
        # Attempt to cancel any futures that have not finished
        skipped = 0
        for fut in not_done:
            u = futures.get(fut)
            try:
                fut.cancel()
            except Exception:
                pass
            skipped += 1
            if u:
                log_line(f"fetch budget reached; skipped {u}", log_path)

    if not all_items:
        log_line("no items parsed from feeds", log_path)
        return 0

    # Sort newest-first; fallback to insertion order if missing timestamps
    def sort_key(it: FeedItem) -> Tuple[int, str]:
        ts = it.published_ts if it.published_ts is not None else 0
        return (ts, compute_seen_key(it))

    all_items.sort(key=sort_key, reverse=True)

    posted = 0
    for item in all_items:
        if posted >= max_per_run:
            break
        key = compute_seen_key(item)
        if key in seen:
            continue
        feed_name = extract_feed_name(item.feed_url)
        content = format_discord_message(item, feed_name=feed_name)
        if verbose:
            log_line(f"post → {feed_name}: {item.title[:80]}", log_path)
        ok = post_with_retry(webhook_url, {"content": content}, timeout_s, user_agent, log_path)
        if ok:
            new_seen.add(key)
            posted += 1
            # light rate-limit to be polite
            time.sleep(0.5)
        else:
            # Do not mark as seen if failed to post
            pass

    if posted > 0 and new_seen != seen:
        save_seen(state_path, new_seen)
    # Save meta even if no posts; cached headers help next time
    try:
        save_meta(meta_path, meta)
    except Exception:
        pass

    log_line(f"run complete: posted={posted}, total_items={len(all_items)}", log_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        pass


