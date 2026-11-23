"""smart-scheduler's days for the core Calendar (issue #10, docs/plugin-contract.md §4).

The past comes from UniFi Drive's test history, the future from the shared
planner (planner.plan), so the calendar shows what the hourly run will do.
Scrub days and UniFi backup windows are marked, and the day details explain
each day: why each test is planned, why nights were skipped and what blocked
them. The core draws the month (core/tui/calendar.py); this file only says
what is on each day.

Settings are read only through ctx.config(), never from the config file, so
the Settings preview shows the effect of unsaved values. Everything is
recalculated each time the calendar reads.

This file is loaded by plugin.py, which hands it the smart_scheduler module
(`ss`); it imports nothing from the tool or the core itself. Standard library only.
"""

import datetime as dt
import json
import re

DAY = dt.timedelta(days=1)


class CalendarError(Exception):
    """A problem that leaves nothing to show (no API, no usable identity)."""


# --------------------------------------------------------------------------- data

def config_from(ss, values):
    """An ss.Config built from typed values ({KEY: value}, as ctx.config() returns
    them) instead of from the config file, so a Settings preview plans with the
    unsaved values. Missing or unreadable values take the schema default."""
    settings = ss.settings
    cfg = ss.Config.__new__(ss.Config)
    cfg.schema = ss.SCHEMA
    fields = settings.fields(cfg.schema)
    raw = {}
    for f in fields:
        if f["key"] in values:
            try:
                raw[f["key"]] = settings.format_value(f, values[f["key"]])
            except (TypeError, ValueError, AttributeError, IndexError):
                continue
    cfg.typed, cfg.field_errors = settings.validate(cfg.schema, raw)
    cfg.errors = [f"{k}: {m}" for k, m in cfg.field_errors.items()]
    cfg.values = {f["key"]: settings.format_value(f, cfg.typed[f["key"]]) for f in fields}
    cfg.overrides = dict(cfg.typed["INTERVAL_OVERRIDES"])
    cfg.window = cfg.typed["START_WINDOW"]
    return cfg


def _night(planner, cfg, t):
    return dt.date.fromisoformat(planner.night_key(cfg, t))


def _dates(start, end):
    """Every calendar date a [start, end) span touches."""
    d, last = start.date(), (end - dt.timedelta(microseconds=1)).date() if end > start else start.date()
    while d <= last:
        yield d
        d += DAY


def _hm(t, ref=None):
    """HH:MM, with the day added when it isn't ref's date."""
    if ref is not None and t.date() != ref:
        return f"{t:%a %d %b %H:%M}"
    return f"{t:%H:%M}"


def gather(ss, ctx):
    """Everything the calendar shows, read live. Raises CalendarError when
    there is nothing to show; smaller problems are listed in "problems"."""
    planner, backups = ss.planner, ss.backups
    cfg = config_from(ss, ctx.config())
    t = ctx.now()
    tz = t.tzinfo
    state = ss.load_state()
    try:
        storage = ss.get_storage()
    except ss.ApiError as e:
        raise CalendarError(f"UniFi Drive API problem: {e.detail}")
    user_id, who, err = ss.resolve_identity(cfg)
    user_id = user_id or state.get("user_id")
    if not user_id:
        raise CalendarError(f"Can't read SMART history as {who}: {err or 'it no longer exists'}.")

    scratch = json.loads(json.dumps(state))   # evaluate_drives may touch state; never ours
    drives = ss.evaluate_drives(cfg, storage, user_id, scratch, ss.Alerts(cfg, scratch, True), report=False)
    by_serial = {d["serial"]: d for d in drives}
    days, problems = {}, []

    def day(d):
        return days.setdefault(d, {"tests": [], "planned": [], "skipped": [], "blocks": []})

    # The past: UniFi's own test history, filed under the night it started.
    for d in drives:
        if d.get("error"):
            problems.append(f"Drive {d['slot']}: {d.get('reason', 'history unavailable')}")
            continue
        try:
            yielded = ss.yielded_ids(state, d["serial"])
            history = [h for h in ss.get_history(d["slot"], d["serial"], user_id, d.get("current_test"))
                       if str(h.get("id")) not in yielded]
        except ss.ApiError as e:
            problems.append(f"Drive {d['slot']}: history unavailable ({e.detail})")
            continue
        for h in history:
            if str(h.get("type")).lower() != "extended":
                continue
            start = planner.parse_time(h.get("createdAt")) or planner.parse_time(h.get("finishedAt"))
            if start is None:
                continue
            start = start.astimezone(tz)
            end = planner.parse_time(h.get("finishedAt"))
            outcome = ("running" if planner.is_running(h) else
                       "passed" if planner.is_good(h) else "failed")
            day(_night(planner, cfg, start))["tests"].append({
                "slot": d["slot"], "serial": d["serial"], "model": d.get("model", "?"),
                "start": start, "end": end.astimezone(tz) if end else None, "outcome": outcome,
                "left": ss.drive_progress_left(h) if outcome == "running" else None,
                "status": h.get("status"), "result": h.get("result")})
    # Tests the drive is running that Drive's history doesn't list (started with
    # smartctl or another tool). Their start time is unknown, so they sit on today.
    for d in drives:
        if d.get("outside_drive"):
            day(_night(planner, cfg, t))["tests"].append({
                "slot": d["slot"], "serial": d["serial"], "model": d.get("model", "?"),
                "start": t, "end": None, "outcome": "running", "outside": True,
                "left": d.get("remaining_percent"), "status": "outside UniFi Drive", "result": None})
    for v in days.values():
        v["tests"].sort(key=lambda x: x["start"])

    # Blocking windows: UniFi backups and scrubs, past and planned.
    horizon = t + dt.timedelta(days=planner.MAX_NIGHTS + 2)
    windows, info = ss.backup_windows(cfg, t, horizon)
    problems += [msg for _, msg in (info or {}).get("problems", [])]
    others = ss.busy_windows(t)
    for w in windows:
        for d in _dates(w.start, w.end):
            day(d)["blocks"].append({
                "kind": "backup", "past": False, "short": "backup " + _hm(w.start + dt.timedelta(
                    hours=cfg["BACKUP_MARGIN_HOURS"])),
                "text": f"{w.label}. No test may be running {_hm(w.start, d)} to {_hm(w.end, d)} "
                        f"(including the {cfg['BACKUP_MARGIN_HOURS']} h margin)."})
    names = {task["id"]: backups.label(task) for task in (info or {}).get("tasks", [])}
    for r in (info or {}).get("runs", []):
        start = r["started"].astimezone(tz)
        if start >= t or r["task_id"] not in names:
            continue
        fin = r["finished"].astimezone(tz) if r.get("finished") else None
        ran = f"ran {_hm(start)} to {_hm(fin, start.date())}" if fin else f"started {_hm(start)}"
        day(start.date())["blocks"].append({
            "kind": "backup", "past": True, "short": f"backup {start:%H:%M}",
            "text": f"{names[r['task_id']]} {ran} ({r.get('status') or 'no status'})."})
    margin = cfg["SCRUB_MARGIN_HOURS"]
    for p in storage["pools"]:
        sc = p.get("dataScrubbing") or {}
        name = p.get("number", p.get("id"))
        last = sc.get("lastTaskRun") or {}
        fin = planner.parse_time(last.get("finishedAt"))
        if fin is not None:
            fin = fin.astimezone(tz)
            day(fin.date())["blocks"].append({
                "kind": "scrub", "past": True, "short": f"pool {name} scrub",
                "text": f"Pool {name} data scrub finished {fin:%H:%M} ({last.get('status') or 'no status'})."})
        for run in planner.scrub_runs(sc, horizon):
            run = run.astimezone(tz)
            day(run.date())["blocks"].append({
                "kind": "scrub", "past": False, "short": f"pool {name} scrub {run:%H:%M}",
                "text": f"Pool {name} data scrub starts {run:%H:%M}. No test may be running from "
                        f"{_hm(run - dt.timedelta(hours=margin), run.date())} ({margin} h margin), "
                        f"and no test starts again that night."})

    # The future: the shared planner, with snoozes and backup windows.
    plan = planner.plan(storage, drives, state, cfg, t, windows=windows + others,
                        not_before=ss.not_before(state, t))
    for e in plan:
        day(dt.date.fromisoformat(e["night"]))["planned"].append(e)
    for s in plan.skipped:
        day(dt.date.fromisoformat(s["night"]))["skipped"].extend(s["reasons"])

    snoozes = []
    for serial, z in sorted(ss.active_snoozes(state, t.date()).items()):
        d = by_serial.get(serial, {})
        snoozes.append({"serial": serial, "slot": d.get("slot", "?"),
                        "until": dt.date.fromisoformat(z["until"]), "note": z.get("note") or "",
                        "text": ss.snooze_text(z)})
    planned = {e["serial"] for e in plan}
    return {
        "now": t,
        "today": _night(planner, cfg, t),
        "days": days,
        "plan_until": max((dt.date.fromisoformat(e["night"]) for e in plan), default=None),
        "unplanned": [d for d in drives if not d.get("excluded") and not d.get("error")
                      and d["serial"] not in planned],
        "drives": drives,
        "snoozes": snoozes,
        "problems": problems,
        "max_starts": cfg["MAX_STARTS_PER_NIGHT"],
    }


# --------------------------------------------------------------------------- entries
# docs/tui-style.md §10 (Calendar) and §8: drive chips are a status glyph plus
# the slot number; "No test tonight" reasons start with what blocks it.

KIND = {"passed": "ok", "failed": "failed", "running": "running"}
EMPTY = {"tests": [], "planned": [], "skipped": [], "blocks": []}
LEGEND = [("planned", "planned"), ("ok", "passed"), ("failed", "failed"), ("running", "running"),
          ("scrub", "scrub"), ("backup", "backup")]


def short_reason(r):
    """A planner reason in the calendar's words: what blocks it, first."""
    m = re.match(r"would overlap pool (\S+) scrub at .*?(\d\d:\d\d)$", r)
    if m:
        return f"scrub on pool {m.group(1)} at {m.group(2)}"
    if r.startswith("would overlap "):
        return "overlaps " + r[len("would overlap "):]
    return {"a SMART test is already running": "one is already running",
            "outside start window": "outside the start window",
            "paused (MAX_STARTS_PER_NIGHT=0 in config)": "no starts allowed (MAX_STARTS_PER_NIGHT is 0)"}.get(r, r)


def _pct_done(left):
    return None if left is None else max(0, min(100, 100 - left))


def test_text(x):
    """'extended test running, 80% done, started 00:10' and the like."""
    word = {"passed": "passed", "failed": "failed", "running": "running"}[x["outcome"]]
    pct = _pct_done(x.get("left")) if x["outcome"] == "running" else None
    if x.get("outside"):
        return "test running outside UniFi Drive" + (f", {pct}% done" if pct is not None else "")
    parts = [f"extended test {word}"]
    if pct is not None:
        parts.append(f"{pct}% done")
    if x["outcome"] == "running":
        parts.append(f"started {x['start']:%H:%M}")
    else:
        parts.append(f"{x['start']:%H:%M}" + (f"-{_hm(x['end'], x['start'].date())}" if x["end"] else ""))
    detail = f"{x.get('status')}/{x.get('result')}"
    if x["outcome"] == "failed" and detail != "failed/failed":
        parts.append(detail)
    return ", ".join(parts)


def no_test(d, today):
    return "No test tonight" if d == today else "No test this night"


def _date_y(d, today):
    """'Sun 21 Feb', with the year outside today's."""
    return f"{d:%a %d %b}" + (f" {d.year}" if d.year != today.year else "")


def _cap(s):
    return s[:1].upper() + s[1:]


def entries(data, d):
    """The core Calendar's entries for day d (docs/plugin-contract.md §4)."""
    info = data["days"].get(d) or EMPTY
    today = data["today"]
    out = []
    for e in info["planned"]:
        notes = [f"starts {e['start']:%H:%M}, ends about {_hm(e['end'], e['start'].date())}",
                 f"why: {e.get('cause') or 'due'} ({e['reason']})"]
        if e["delayed_by"]:
            notes.append("waited for: " + "; ".join(short_reason(r) for r in e["delayed_by"]))
        out.append({"status": "planned", "place": "chip", "chip": str(e["slot"]),
                    "text": f"Drive {e['slot']}  planned {e['start']:%H:%M}-{_hm(e['end'], e['start'].date())}, "
                            f"{e.get('cause') or 'due'}",
                    "section": "Planned (assuming earlier tests pass)",
                    "detail": f"Drive {e['slot']}  {e['model']}  {e['serial']}", "notes": notes})
    for x in info["tests"]:
        out.append({"status": KIND[x["outcome"]], "place": "chip", "chip": str(x["slot"]),
                    "past": d < today and x["outcome"] != "running",
                    "text": f"Drive {x['slot']}  {test_text(x)}", "section": "Test history (UniFi Drive)",
                    "detail": f"Drive {x['slot']}  {x['model']}  {x['serial']}", "notes": [test_text(x)]})
    if info["skipped"]:
        reasons = "; ".join(short_reason(r) for r in info["skipped"])
        out.append({"status": "warning", "place": "flag", "text": f"{no_test(d, today)}: {reasons}",
                    "section": no_test(d, today), "detail": _cap(reasons)})
    for b in info["blocks"]:
        out.append({"status": b["kind"], "place": "mark", "past": b["past"],
                    "text": _cap(b["short"]) + (" (done)" if b["past"] else ""),
                    "section": "Scrubs and backups", "detail": b["text"]})
    for z in data["snoozes"]:
        if z["until"] > d >= today:
            out.append({"status": None, "place": "list", "section": "Snoozed drives",
                        "text": f"Drive {z['slot']} snoozed to {z['until']:%a %d %b}"
                                + (f" ({z['note']})" if z["note"] else ""),
                        "detail": f"Drive {z['slot']}  {z['serial']}: {z['text']}"})
    return out


def to_calendar(data):
    """gather()'s dict as the plugin contract's calendar(ctx) value."""
    days = set(data["days"])
    for z in data["snoozes"]:
        d = data["today"]
        while d < z["until"]:
            days.add(d)
            d += DAY
    out = {"today": data["today"], "until": data["plan_until"], "legend": LEGEND,
           "days": {d: e for d in sorted(days) for e in [entries(data, d)] if e},
           "problems": list(data["problems"])}
    if data["plan_until"] is not None:
        out["until_note"] = (f"The plan stops on {_date_y(data['plan_until'], data['today'])}, once every drive "
                             "has a planned test. Later tests depend on how those go.")
    return out
