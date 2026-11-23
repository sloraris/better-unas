"""The busy registry: when each tool's jobs run, so the others stay clear.

Each tool writes its own `busy.json` in its folder and reads everyone
else's. A window is a span of time a job needs the disks to itself: a SMART
test, a scrub, a UniFi backup. A tool's planner treats the other tools'
windows like its own blocking windows and never starts a job inside one.

    busy.json  {"tool": name, "updated": ISO time,
                "windows": [{"start", "end", "label", "kind"}, ...]}

`kind` is a status kind from docs/tui-style.md §3 ("running", "planned",
"scrub", "backup"). `label` is said in reasons, so it reads on its own:
"smart-scheduler test on Drive 3", "UniFi backup Photos to cloud".

A file not rewritten for STALE is ignored: its tool stopped running (turned
off, broken or uninstalled without --purge), so its plan no longer holds.

Standard library only.
"""

import datetime as dt
import json
import os
from collections import namedtuple

from core.lib import util

FILE = "busy.json"
STALE = dt.timedelta(days=2)
KINDS = ("running", "planned", "scrub", "backup")

# tool: the tool that published it
Busy = namedtuple("Busy", "start end label kind tool")


def publish(base, name, windows, now, dry=False):
    """Replace a tool's windows. `windows` holds (start, end, label, kind)
    tuples or anything with those attributes; times must carry a zone."""
    out = []
    for w in windows:
        start, end, label, kind = (w.start, w.end, w.label, w.kind) if hasattr(w, "start") else w
        if kind not in KINDS:
            raise ValueError(f"busy window kind must be one of {', '.join(KINDS)}, not {kind!r}")
        if end > start:
            out.append({"start": start.isoformat(), "end": end.isoformat(), "label": str(label), "kind": kind})
    out.sort(key=lambda w: w["start"])
    if dry:
        return out
    util.write_atomic(os.path.join(base, FILE),
                      json.dumps({"tool": name, "updated": now.isoformat(), "windows": out}, indent=1) + "\n",
                      tag=name)
    return out


def withdraw(base, name, now, dry=False):
    """Publish no windows (the tool is turned off or uninstalled)."""
    if os.path.exists(os.path.join(base, FILE)):
        publish(base, name, (), now, dry)


def read_file(path, now):
    """The windows in one busy.json that haven't ended, or [] if it is stale
    or unreadable."""
    try:
        with open(path) as f:
            data = json.load(f)
        updated = util.parse_time(data.get("updated"))
        if updated is None or now - updated > STALE:
            return []
        tool = str(data.get("tool") or os.path.basename(os.path.dirname(path)))
        out = []
        for w in data.get("windows") or []:
            start, end = util.parse_time(w.get("start")), util.parse_time(w.get("end"))
            if start is None or end is None or end <= now or w.get("kind") not in KINDS:
                continue
            out.append(Busy(start, end, str(w.get("label") or tool), w["kind"], tool))
        return out
    except (OSError, ValueError, AttributeError, TypeError):
        return []


def read(home, now, exclude=None):
    """Every tool's windows under home (the suite folder) that haven't ended,
    sorted by start. `exclude` is the reading tool's own name."""
    out = []
    try:
        entries = sorted(os.listdir(home))
    except OSError:
        return out
    for entry in entries:
        path = os.path.join(home, entry, FILE)
        if entry == exclude or not os.path.isfile(path):
            continue
        out += [w for w in read_file(path, now) if w.tool != exclude]
    out.sort(key=lambda w: (w.start, w.end))
    return out


def overlapping(windows, start, end):
    """The windows that overlap [start, end)."""
    return [w for w in windows if w.start < end and w.end > start]
