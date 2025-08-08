#!/usr/bin/env python3
"""
PiMO Service Rotator — keeps only one service active at a time and rotates evenly

Design
- Reads a newline-delimited list of systemd services from a services file
- Computes an even time-slice per service based on a rotation period and N services
- Uses a stable epoch to map current time to the desired index; cron can run at any cadence
- Ensures only the desired service is running; stops all others

Config (key=value), defaults shown:
  SERVICES_FILE=/etc/pimo/services.rotate
  ROTATION_PERIOD_MINUTES=120
  MIN_SLICE_SECONDS=300
  STATE_PATH=/var/lib/pimo/rotator.state
  LOG_PATH=/var/log/pimo/rotator.log
  SYSTEMCTL_CMD=/bin/systemctl

Notes
- If paths are not writable (e.g., when testing as a user), falls back to ~/.local/* paths
- Services file supports blank lines and # comments
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple


DEFAULT_CONFIG_PATH = "/etc/pimo/rotator.conf"
DEFAULT_SERVICES_FILE = "/etc/pimo/services.rotate"
DEFAULT_LOG_PATH = "/var/log/pimo/rotator.log"
DEFAULT_STATE_PATH = "/var/lib/pimo/rotator.state"
DEFAULT_SYSTEMCTL = "/bin/systemctl"


def ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def log_line(message: str, log_path: str) -> None:
    try:
        ensure_dir(log_path)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
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
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return cfg


def choose_paths(cfg: dict) -> Tuple[str, str, str, str]:
    services_file = cfg.get("SERVICES_FILE", DEFAULT_SERVICES_FILE)
    log_path = cfg.get("LOG_PATH", DEFAULT_LOG_PATH)
    state_path = cfg.get("STATE_PATH", DEFAULT_STATE_PATH)
    systemctl = cfg.get("SYSTEMCTL_CMD", DEFAULT_SYSTEMCTL)

    # fallbacks for log/state when not writable
    try:
        ensure_dir(log_path)
        with open(log_path, "a", encoding="utf-8"):
            pass
    except Exception:
        log_path = os.path.expanduser("~/.local/share/pimo/rotator.log")

    try:
        ensure_dir(state_path)
        if not os.path.exists(state_path):
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({}, f)
    except Exception:
        state_path = os.path.expanduser("~/.local/state/pimo/rotator.state")
        ensure_dir(state_path)
        if not os.path.exists(state_path):
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({}, f)

    return services_file, log_path, state_path, systemctl


def read_services(path: str) -> List[str]:
    # supports inline comments via trailing # (not in names) and skips blanks
    services: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "#" in line:
                    line = line.split("#", 1)[0].strip()
                if line:
                    services.append(line)
    except FileNotFoundError:
        pass
    return services


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_state(path: str, state: dict) -> None:
    ensure_dir(path)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except Exception:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f)
        except Exception:
            pass


def parse_int(value: Optional[str], default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def systemctl_cmd(systemctl: str, args: List[str]) -> Tuple[int, str]:
    cmd = [systemctl] + args
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        return 0, out.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError as e:
        return e.returncode, e.output.decode("utf-8", errors="replace")
    except FileNotFoundError:
        # for testing on non-systemd hosts, pretend success
        return 0, "(systemctl not found on this host)"


def ensure_only_running(desired: str, all_services: List[str], systemctl: str, log_path: str) -> None:
    for svc in all_services:
        if svc == desired:
            rc, out = systemctl_cmd(systemctl, ["start", svc])
            if rc != 0:
                log_line(f"failed to start {svc}: {out.strip()}", log_path)
        else:
            rc, out = systemctl_cmd(systemctl, ["stop", svc])
            if rc != 0:
                # stopping a non-running unit returns non-zero; not fatal
                pass


def main(argv: List[str]) -> int:
    cfg_path = os.environ.get("PIMO_ROTATOR_CONFIG", DEFAULT_CONFIG_PATH)
    cfg = read_config(cfg_path)

    services_file, log_path, state_path, systemctl = choose_paths(cfg)
    services = read_services(services_file)
    if not services:
        log_line("no services to rotate — exiting", log_path)
        return 0

    period_min = parse_int(cfg.get("ROTATION_PERIOD_MINUTES"), 120)
    min_slice_s = parse_int(cfg.get("MIN_SLICE_SECONDS"), 300)

    n = max(1, len(services))
    slice_s = max(min_slice_s, int(period_min * 60 / n))

    state = load_state(state_path)
    epoch = int(state.get("epoch_ts") or 0)
    # If epoch missing or services count changed, reset epoch to now to re-even
    if epoch <= 0 or int(state.get("services_count") or 0) != n or int(state.get("slice_seconds") or 0) != slice_s:
        epoch = int(time.time())
        state["epoch_ts"] = epoch
        state["services_count"] = n
        state["slice_seconds"] = slice_s
        save_state(state_path, state)

    now = int(time.time())
    if slice_s <= 0:
        idx = 0
    else:
        idx = int((now - epoch) // slice_s) % n

    desired = services[idx]
    ensure_only_running(desired, services, systemctl, log_path)
    log_line(f"active={desired} idx={idx}/{n} slice_s={slice_s}", log_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        pass


