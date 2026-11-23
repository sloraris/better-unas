#!/usr/bin/env python3
"""space-watch: the example tool from docs/plugin-contract.md.

    python3 space_watch.py status     show the current reading
    python3 space_watch.py run        take a reading and remember the peak

It only reads disk usage, so it is safe anywhere. Its config comes through
better-unas's core library (core/lib, docs/plugin-contract.md section 11),
which also reads the suite's shared settings from better-unas.conf.
"""

import datetime as dt
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.realpath(__file__))
# An installed tool sits next to core/ (<home>/<tool>/), so its parent is
# enough. This example also runs from docs/examples/ in a checkout, so it
# looks further up for the folder that holds core/lib.
_up = BASE
while os.path.dirname(_up) != _up and not os.path.isfile(os.path.join(_up, "core", "lib", "__init__.py")):
    _up = os.path.dirname(_up)
sys.path.insert(1, _up)
from core.lib.tool import Tool  # noqa: E402

TOOL = Tool("space-watch", __file__)
STATE_FILE = TOOL.state_file


def read_config(path=None, core_path=None):
    """The config file plus the shared settings (core.lib.config); cfg[KEY] is the parsed value."""
    return TOOL.config(path, core_path)


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
    return 1 if pct > cfg["WARN_PERCENT"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
