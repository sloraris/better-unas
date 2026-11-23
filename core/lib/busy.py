"""The busy registry: when each tool's jobs run, so the others stay clear.

Each tool writes its own `busy.json` in its folder and reads everyone
else's. A window is a span of time a job needs the disks to itself: a SMART
test, a scrub, a UniFi backup. A tool's planner treats the other tools'
windows like its own blocking windows and never starts a job inside one.

    busy.json  {"tool": name, "updated": ISO time,
                "windows": [{"start", "end", "label", "kind", "tier"}, ...]}

`kind` is a status kind from docs/tui-style.md §3 ("running", "planned",
"scrub", "backup"). `label` is said in reasons, so it reads on its own:
"smart-scheduler test on Drive 3", "UniFi backup Photos to cloud".

`tier` is how much the job matters, lowest first (TIERS):

    routine     interval work: a SMART test that is simply due
    scheduled   work on a calendar: scrub-repair's quarterly scrub
    urgent      a drive or the volume shows trouble: a watched SMART count
                rose, a failed test's retry, a scrub after checksum errors
    manual      started by hand (run-now, a scrub you start)
    fixed       UniFi's own scrubs, backups and rebuilds: nothing here can
                move them, so nothing runs into them

A job stays clear of every window at its own tier or above (`blocking`) and
ignores lower ones. A lower job already running when a higher one needs the
disks gives way (`must_yield`): it pauses if it can (a scrub), else stops and
is planned again (a SMART test). The higher tool nudges the lower one's
service so it notices at once, not at its next timer run.

A file not rewritten for STALE is ignored: its tool stopped running (turned
off, broken or uninstalled without --purge), so its plan no longer holds.

Standard library only.
"""

import datetime as dt
import json
import os
import re
from collections import namedtuple

from core.lib import util

FILE = "busy.json"
STALE = dt.timedelta(days=2)
KINDS = ("running", "planned", "scrub", "backup")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")    # a tool name, safe in a unit name
TIERS = ("routine", "scheduled", "urgent", "manual", "fixed")
# How soon a higher job's start counts as "needs the disks now" for must_yield.
SOON = dt.timedelta(minutes=15)

# tool: the tool that published it
Busy = namedtuple("Busy", "start end label kind tool tier")


def rank(tier):
    """A tier's place in TIERS (higher matters more)."""
    return TIERS.index(tier)


def blocking(windows, tier):
    """The windows a job of `tier` must stay clear of: its own tier and above.
    A window without a tier (a tool's own blocking windows) always blocks."""
    r = rank(tier)
    return [w for w in windows if rank(getattr(w, "tier", None) or "fixed") >= r]


def publish(base, name, windows, now, dry=False):
    """Replace a tool's windows. `windows` holds (start, end, label, kind,
    tier) tuples or anything with those attributes; times must carry a zone."""
    out = []
    for w in windows:
        start, end, label, kind, tier = (w.start, w.end, w.label, w.kind, w.tier) if hasattr(w, "start") else w
        if kind not in KINDS:
            raise ValueError(f"busy window kind must be one of {', '.join(KINDS)}, not {kind!r}")
        if tier not in TIERS:
            raise ValueError(f"busy window tier must be one of {', '.join(TIERS)}, not {tier!r}")
        if end > start:
            out.append({"start": start.isoformat(), "end": end.isoformat(), "label": str(label), "kind": kind,
                        "tier": tier})
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
            if (start is None or end is None or end <= now or w.get("kind") not in KINDS
                    or w.get("tier") not in TIERS):
                continue
            out.append(Busy(start, end, str(w.get("label") or tool), w["kind"], tool, w["tier"]))
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


def must_yield(mine, others, now):
    """[(my window, their window)] for each of my running jobs that another
    tool's higher-tier job needs the disks for now (it has started, or starts
    within SOON, and overlaps mine). The caller pauses or stops that job."""
    out = []
    for w in mine:
        for o in others:
            if (rank(o.tier) > rank(w.tier) and o.start <= now + SOON
                    and o.start < w.end and o.end > now):
                out.append((w, o))
                break
    return out


def in_the_way(others, start, end, tier):
    """The tools whose lower-tier running jobs overlap [start, end): a
    `tier` job starting then should nudge them to give way."""
    return sorted({o.tool for o in others
                   if o.kind == "running" and rank(o.tier) < rank(tier) and o.start < end and o.end > start})
