"""scrub-repair's plugin module for the better-unas TUI (docs/plugin-contract.md).

A Home card, the alerts, scrub nights on the core Calendar and the Damaged
files screen, all from state.json and the settings, so nothing here reads a
disk or the kernel log. The 15-minute run never imports this file.
"""

import datetime as dt
import importlib.util
import os
import sys

CONTRACT = 1
HERE = os.path.dirname(os.path.abspath(__file__))
LEVELS = {"error": "error", "warning": "warn", "warn": "warn"}


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sr = _load("better_unas_scrub_repair", "scrub_repair.py")


def _load_screen():
    # screens_damaged.py imports nothing from the tool; it gets `sr` from env().
    return _load("better_unas_scrub_repair_damaged", "screens_damaged.py")


damaged_screen = _load_screen()


def _level(active):
    if any(a.get("level") == "error" for a in active):
        return "error"
    return "warn" if active else "ok"


def status(ctx):
    """Cheap: config, state and trigger files only."""
    cfg = ctx.config()
    problems = [f"{k}: {msg}" for k, msg in ctx.config_errors().items()]
    if not cfg["ENABLED"]:
        return {"enabled": False, "state": "off", "problems": problems,
                "detail": ["Turned off (ENABLED=no): the kernel log isn't read and no scrub runs."]}
    problems += sr.trigger_problems()
    state = sr.load_state()
    detail = []
    last = sr.parse_time(state.get("last_tick"))
    if last:
        detail.append(f"Last check {last:%a %d %b %H:%M}")
    level = _level(list((state.get("alerts") or {}).values()))
    return {"enabled": True, "state": "warn" if level == "ok" and problems else level,
            "problems": problems, "detail": detail}


def counts(files):
    """'2 (1 staged, 1 no copy)' from the open file records."""
    by = {}
    for r in files.values():
        by[r["status"]] = by.get(r["status"], 0) + 1
    words = {"found": "waiting", "staged": "staged", "no copy": "no copy", "snapshot": "in snapshots",
             "changed": "changed", "error": "failed", "searching": "searching"}
    parts = [f"{n} {words.get(s, s)}" for s, n in sorted(by.items(), key=lambda p: -p[1])]
    return f"{len(files)} ({', '.join(parts)})"


def summary(ctx):
    """The Home card, from state.json."""
    cfg = ctx.config()
    if not cfg["ENABLED"]:
        return {"state": "off", "headline": "Turned off (ENABLED=no)", "items": []}
    state = sr.load_state()
    last = sr.parse_time(state.get("last_tick"))
    if not last:
        return {"state": "unknown", "headline": "No check yet; one runs every 15 minutes", "items": []}
    files = state.get("files") or {}
    s = state.get("scrub") or {}
    job = s.get("job")
    items = []
    if files:
        items.append({"label": "Damaged files", "value": counts(files), "status": "failed", "tone": "bad"})
    else:
        items.append({"label": "Damaged files", "value": "none found", "status": "ok", "tone": "normal"})
    if job:
        items.append({"label": "Scrub", "value": sr.scrub_line(s), "status": "running" if job.get("started")
                      and not job.get("paused") else "scrub", "tone": "normal"})
    nxt = sr.next_scheduled(cfg, s, ctx.now())
    items.append({"label": "Next scrub", "value": f"{nxt:%a %d %b %H:%M}" if nxt else "none scheduled",
                  "status": "scrub" if nxt else None, "tone": "normal" if nxt else "dim"})
    done = (s.get("history") or [])[-1:]
    if done:
        h = done[0]
        t = sr.parse_time(h["done"])
        errs = f"{h['errors']} errors" if h.get("errors") else "no errors"
        items.append({"label": "Last scrub", "value": f"{t:%a %d %b}, {errs}", "status": "failed" if h.get("errors")
                      else "ok", "tone": "warn" if h.get("errors") else "normal"})
    restored = [r for r in state.get("restored") or [] if r.get("status") == "restored"][-1:]
    if restored:
        r = restored[0]
        items.append({"label": "Last restored", "value": f"{os.path.basename(r['path'])}, "
                      f"{sr.parse_time(r['at']):%a %d %b}", "status": "ok", "tone": "good"})
    items.append({"label": "Last check", "value": f"{last:%a %d %b %H:%M}", "tone": "dim"})
    for i in items:
        if i.get("status") is None:
            i.pop("status", None)
    n = len(files)
    headline = f"{n} damaged file{'s' if n != 1 else ''}" if n else "No damaged files found"
    return {"state": _level(list((state.get("alerts") or {}).values())), "headline": headline, "items": items[:6]}


def alerts(ctx):
    """state["alerts"] and state["resolved"] mapped onto the contract."""
    state = sr.load_state()
    active = state.get("alerts") or {}
    out = []
    for key, a in sorted(active.items()):
        since = a.get("since") or a.get("sent")
        if since:
            out.append({"id": key, "level": LEVELS.get(a.get("level"), "info"), "message": a.get("msg", ""),
                        "active": True, "since": since, "last": a.get("sent")})
    for r in state.get("resolved") or []:
        if r.get("key") in active or not r.get("since"):
            continue
        out.append({"id": r["key"], "level": LEVELS.get(r.get("level"), "info"), "message": r.get("msg", ""),
                    "active": False, "since": r["since"], "last": r.get("sent"), "resolved": r.get("resolved")})
    return out


def validate(values):
    """FOLDER_SOURCES: full paths, each listed once."""
    out = {}
    raw = values.get("FOLDER_SOURCES") or ""
    paths = [p.strip() for p in raw.split(",") if p.strip()]
    if len(set(paths)) != len(paths):
        out["FOLDER_SOURCES"] = "a folder is listed twice"
    elif any(not p.startswith("/") for p in paths):
        out["FOLDER_SOURCES"] = "each folder must be a full path starting with /"
    return out


# ------------------------------------------------------------------ calendar

def calendar(ctx):
    """Scrub nights, past and planned, and the days damaged files were found."""
    cfg = ctx.config()
    t = ctx.now()
    state = sr.load_state()
    s = state.get("scrub") or {}
    days = {}

    def add(day, entry):
        days.setdefault(day, []).append(entry)

    for h in s.get("history") or []:
        began, done = sr.parse_time(h["began"]), sr.parse_time(h["done"])
        if not began or not done:
            continue
        errs = f"{h['errors']} checksum errors" if h.get("errors") else "no errors"
        day = began.date()
        while day <= done.date():
            add(day, {"status": "scrub", "place": "mark", "past": True, "section": "Scrubs",
                      "text": f"scrub-repair {h['tier']} scrub" + (f", finished {done:%H:%M}: {errs}"
                                                                    if day == done.date() else "")})
            day += dt.timedelta(days=1)
    job = s.get("job")
    if job:
        add(t.date(), {"status": "running" if job.get("started") and not job.get("paused") else "scrub",
                       "place": "mark", "section": "Scrubs", "text": "scrub-repair " + sr.scrub_line(s)})
    if cfg["SCRUB_SCHEDULE"]:
        nights = sr.detect.scheduled_nights(t, t + dt.timedelta(days=120), cfg["SCRUB_FREQUENCY"],
                                            cfg["SCRUB_START_DAY"], cfg["SCRUB_WINDOW"],
                                            sr.parse_time(s.get("last_done")), sr.nights_needed(cfg, s))
        for a, b in nights:
            if job and a.date() == t.date():
                continue
            add(a.date(), {"status": "scrub", "place": "mark", "section": "Scrubs",
                           "text": f"scrub-repair scheduled scrub {a:%H:%M}-{b:%H:%M}",
                           "notes": ["read-only; pauses at the end of the night and carries on the next"]})
    for rec in (state.get("files") or {}).values():
        found = sr.parse_time(rec.get("found"))
        if found:
            add(found.date(), {"status": "failed", "place": "list", "past": True, "section": "Damaged files",
                               "text": f"damaged file found by a {rec['how']}: {rec['path']}"})
    for r in state.get("restored") or []:
        at = sr.parse_time(r.get("at"))
        if at and r.get("status") == "restored":
            add(at.date(), {"status": "ok", "place": "list", "past": True, "section": "Damaged files",
                            "text": f"restored {r['path']}" + (f" from {r['label']}" if r.get("label") else "")})
    return {"today": t.date(), "legend": [("scrub", "scrub night"), ("failed", "damaged file")],
            "problems": [], "days": days}


# ------------------------------------------------------------------ screens

def _env():
    # Looked up on each use, so tests can swap in a faked `sr`.
    return sr


def _damaged(ctx):
    return damaged_screen.DamagedScreen(ctx, _env)


def screens(ctx):
    return [{"id": "damaged", "tab": "Damaged", "make": _damaged}]


if __name__ == "__main__":
    sys.exit("This is the TUI's plugin module; run scrub_repair.py instead.")
