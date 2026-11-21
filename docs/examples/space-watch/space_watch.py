#!/usr/bin/env python3
"""space-watch: the example tool from docs/plugin-contract.md.

    python3 space_watch.py status     show the current reading
    python3 space_watch.py run        take a reading and remember the peak

It only reads disk usage, so it is safe anywhere. It keeps its own tiny
config reader on purpose: a tool's scheduled run must work without the
better-unas core.
"""

import datetime as dt
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.realpath(__file__))
CONFIG_FILE = os.path.join(BASE, "config")
STATE_FILE = os.path.join(BASE, "state.json")
DEFAULTS = {"ENABLED": "yes", "WATCH_PATH": "/", "WARN_PERCENT": "90",
            "WEBHOOK_URL": "", "QUIET_HOURS": "22:00-07:00"}


def read_config(path=CONFIG_FILE):
    values = dict(DEFAULTS)
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return values


def measure(path):
    """Percent used of the volume holding path."""
    u = shutil.disk_usage(path)
    return round(100 * u.used / u.total, 1)


def load_state(path=STATE_FILE):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state, path=STATE_FILE):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def main(argv):
    if not argv or argv[0] not in ("status", "run"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    cfg = read_config()
    pct = measure(cfg["WATCH_PATH"])
    if argv[0] == "run":
        state = load_state()
        if pct > state.get("peak", 0):
            state.update(peak=pct, peak_at=dt.datetime.now().astimezone().isoformat())
            save_state(state)
    print(f"{cfg['WATCH_PATH']}: {pct}% used (warn above {cfg['WARN_PERCENT']}%)")
    return 1 if pct > int(cfg["WARN_PERCENT"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
