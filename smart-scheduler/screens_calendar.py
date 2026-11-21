"""smart-scheduler's Calendar screen for the better-unas TUI (issue #10).

A month grid of extended SMART tests. The past comes from UniFi Drive's test
history, the future from the shared planner (planner.plan), so the calendar
shows what the hourly run will do. Scrub days and UniFi backup windows are
marked, and Enter on a day explains it: why each test is planned, why nights
were skipped and what blocked them.

Settings are read only through ctx.config(), never from the config file, so
the Settings screen can show the effect of unsaved values (the core's
Settings preview). Everything is recalculated each time the screen opens.

This file is loaded by plugin.py, which hands it the smart_scheduler module
(`ss`); it imports nothing from the tool or the core itself. Standard library only.
"""

import calendar
import datetime as dt
import json

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
            problems.append(f"Slot {d['slot']}: {d.get('reason', 'history unavailable')}")
            continue
        try:
            history = ss.get_history(d["slot"], d["serial"], user_id)
        except ss.ApiError as e:
            problems.append(f"Slot {d['slot']}: history unavailable ({e.detail})")
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
                "status": h.get("status"), "result": h.get("result")})
    for v in days.values():
        v["tests"].sort(key=lambda x: x["start"])

    # Blocking windows: UniFi backups and scrubs, past and planned.
    horizon = t + dt.timedelta(days=planner.MAX_NIGHTS + 2)
    windows, info = ss.backup_windows(cfg, t, horizon)
    problems += [msg for _, msg in (info or {}).get("problems", [])]
    for w in windows:
        for d in _dates(w.start, w.end):
            day(d)["blocks"].append({
                "kind": "backup", "past": False, "short": "backup " + _hm(w.start + dt.timedelta(
                    hours=cfg.int("BACKUP_MARGIN_HOURS"))),
                "text": f"{w.label}. No test may be running {_hm(w.start, d)} to {_hm(w.end, d)} "
                        f"(including the {cfg.int('BACKUP_MARGIN_HOURS')} h margin)."})
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
    margin = cfg.int("SCRUB_MARGIN_HOURS")
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
    plan = planner.plan(storage, drives, state, cfg, t, windows=windows, not_before=ss.not_before(state, t))
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
        "max_starts": cfg.int("MAX_STARTS_PER_NIGHT"),
    }


# --------------------------------------------------------------------------- text

MARKS = {"failed": "!", "running": "*", "passed": ""}
TONES = {"failed": "bad", "running": "warn", "passed": "good"}


def tokens(info):
    """[(text, tone)] for a day cell's slot line: history first, then the plan."""
    out = [(f"S{x['slot']}{MARKS[x['outcome']]}", TONES[x["outcome"]]) for x in info["tests"]]
    out += [(f"S{e['slot']}", "warn") for e in info["planned"]]
    return out


def marks(info):
    kinds = {b["kind"] for b in info["blocks"]}
    return [m for k, m in (("scrub", "sc"), ("backup", "bk")) if k in kinds]


def outcome_text(x):
    word = {"passed": "passed", "failed": "failed", "running": "running"}[x["outcome"]]
    detail = f" ({x['status']}/{x['result']})" if x["outcome"] == "failed" else ""
    span = f"{x['start']:%H:%M}" + (f"-{_hm(x['end'], x['start'].date())}" if x["end"] else "")
    return word + detail, span


def summary_lines(data, d):
    """Short [(text, tone)] lines about one day, for the area under the grid."""
    info = data["days"].get(d) or {"tests": [], "planned": [], "skipped": [], "blocks": []}
    out = []
    for x in info["tests"]:
        word, span = outcome_text(x)
        out.append((f"S{x['slot']} test {word}, {span}", TONES[x["outcome"]]))
    for e in info["planned"]:
        out.append((f"S{e['slot']} planned {e['start']:%H:%M}: {e['reason']}", "warn"))
    if info["skipped"]:
        out.append(("No test: " + "; ".join(info["skipped"]), "normal"))
    for b in info["blocks"]:
        out.append((b["short"][0].upper() + b["short"][1:] + (" (done)" if b["past"] else ""), "dim"))
    if not out:
        if data["plan_until"] is not None and d > data["plan_until"]:
            out.append(("Beyond the plan, which ends once every drive has a test.", "dim"))
        elif d < data["today"]:
            out.append(("No test on record.", "dim"))
        else:
            out.append(("Nothing planned.", "dim"))
    return out


def detail_lines(data, d):
    """[(text, tone)] paragraphs for the day details screen."""
    info = data["days"].get(d) or {"tests": [], "planned": [], "skipped": [], "blocks": []}
    out = []
    if info["planned"]:
        out += [("Planned (assuming earlier tests pass)", "warn")]
        for e in info["planned"]:
            out.append((f"  Slot {e['slot']}  {e['model']} ({e['serial']})", "normal"))
            out.append((f"    starts {e['start']:%H:%M}, ends about {_hm(e['end'], e['start'].date())}", "normal"))
            out.append((f"    why: {e['reason']}", "normal"))
            if e["delayed_by"]:
                out.append(("    waited for: " + "; ".join(e["delayed_by"]), "normal"))
        out.append(("", "normal"))
    if info["tests"]:
        out += [("Test history (UniFi Drive)", "normal")]
        for x in info["tests"]:
            word, span = outcome_text(x)
            out.append((f"  Slot {x['slot']}  {x['model']} ({x['serial']}): extended test {word}, {span}",
                        TONES[x["outcome"]]))
        out.append(("", "normal"))
    if info["skipped"]:
        out += [("Why no test starts this night", "normal")]
        out += [(f"  - {r}", "normal") for r in info["skipped"]]
        out.append(("", "normal"))
    if info["blocks"]:
        out += [("Scrubs and backups", "normal")]
        out += [(f"  - {b['text']}", "dim" if b["past"] else "normal") for b in info["blocks"]]
        out.append(("", "normal"))
    held = [z for z in data["snoozes"] if z["until"] > d >= data["today"]]
    if held:
        out += [("Snoozed drives", "normal")]
        out += [(f"  - Slot {z['slot']} ({z['serial']}): {z['text']}", "normal") for z in held]
        out.append(("", "normal"))
    if data["plan_until"] is not None and d > data["plan_until"]:
        out.append((f"The plan stops on {data['plan_until']:%a %d %b}, once every drive has a planned "
                    "test. Later tests depend on how those go.", "dim"))
    elif not out:
        out.append(("Nothing tested, planned or blocked on this day.", "dim"))
    while out and not out[-1][0]:
        out.pop()
    return out


def _fit(s, width):
    return s if len(s) <= width else s[: max(0, width - 1)] + "~"


# --------------------------------------------------------------------------- screens

GRID_TOP = 2          # month line, weekday line
WEEK_ROWS = 2         # day line, slot line (a month spans at most 6 weeks)
LEGEND = [("S6", "warn", "planned"), ("S6", "good", "passed"), ("S6!", "bad", "failed"),
          ("S6*", "warn", "running"), ("sc", "normal", "scrub"), ("bk", "normal", "backup")]


class CalendarScreen:
    title = "Calendar"

    def __init__(self, ctx, source):
        """source() returns gather()'s dict; plugin.py binds it to the live tool."""
        self.ctx = ctx
        self.source = source
        self.data = None
        self.error = None
        self.sel = None

    def keys(self):
        return [("Arrows", "day"), ("PgUp/Dn", "month"), ("Enter", "details"),
                ("t", "today"), ("r", "recalculate")]

    def show(self):
        try:
            self.data = self.ctx.run_slow(self.source, "Planning tests")
            self.error = None
        except CalendarError as e:
            self.data, self.error = None, str(e)
        if self.sel is None:
            self.sel = self.data["today"] if self.data else self.ctx.now().date()

    # ------------------------------------------------------------ keys

    def key(self, k):
        if self.sel is None:
            return False
        step = {"left": -1, "right": 1, "up": -7, "down": 7}
        if k in step:
            self.sel += step[k] * DAY
        elif k in ("pgup", "pgdn"):
            self.sel = shift_month(self.sel, -1 if k == "pgup" else 1)
        elif k in ("t", "home"):
            self.sel = self.data["today"] if self.data else self.ctx.now().date()
        elif k == "r":
            self.show()
        elif k == "enter" and self.data:
            self.ctx.push(DayScreen(self.ctx, self.data, self.sel))
        else:
            return False
        return True

    # ------------------------------------------------------------ drawing

    def render(self, c):
        if self.sel is None:
            return
        sym = c.symbols
        month = dt.date(self.sel.year, self.sel.month, 1)
        c.text(0, 1, f"{sym['left']} {month:%B %Y} {sym['right']}", bold=True)
        if self.error:
            c.text(2, 1, _fit(self.error, c.width - 2), "bad", bold=True)
            c.text(3, 1, "Press r to try again.", "dim")
            return
        data = self.data
        right = (f"plan until {data['plan_until']:%a %d %b}" if data["plan_until"]
                 else "nothing planned")
        c.text(0, c.width - len(right) - 1, right, "dim")

        cw = max(10, (c.width - 2) // 7)
        for i, name in enumerate(calendar.day_abbr):
            c.text(1, 1 + i * cw, f" {name}", "dim")
        weeks = calendar.Calendar().monthdatescalendar(month.year, month.month)
        for w, week in enumerate(weeks):
            for i, d in enumerate(week):
                if d.month == month.month:
                    self.cell(c.sub(GRID_TOP + w * WEEK_ROWS, 1 + i * cw, WEEK_ROWS, cw - 1), d)

        top = GRID_TOP + len(weeks) * WEEK_ROWS + 1
        legend_y = c.height - 1
        extra = [(f"Snoozed: S{z['slot']} until {z['until']:%a %d %b}" + (f" ({z['note']})" if z["note"] else ""),
                  "normal") for z in data["snoozes"]]
        extra += [(f"Problem: {p}", "bad") for p in data["problems"]]
        room = legend_y - top - 1            # rows for the selected day, below its heading
        extra = extra[: max(0, room - 1)]
        head = f"{self.sel:%a %d %b %Y}" + ("  today" if self.sel == data["today"] else "")
        c.text(top, 1, head, bold=True)
        lines = summary_lines(data, self.sel)
        room -= len(extra)
        if len(lines) > room:
            lines = lines[: room - 1] + [(f"{sym['more']} {len(lines) - room + 1} more: Enter for details", "dim")]
        for i, (text, tone) in enumerate(lines):
            c.text(top + 1 + i, 3, _fit(text, c.width - 4), tone)
        for i, (text, tone) in enumerate(extra):
            c.text(legend_y - len(extra) + i, 1, _fit(text, c.width - 2), tone)
        x = 1
        for sample, tone, label in LEGEND:
            c.text(legend_y, x, sample, tone, bold=tone in ("warn", "bad"))
            c.text(legend_y, x + len(sample) + 1, label, "dim")
            x += len(sample) + len(label) + 4

    def cell(self, c, d):
        data = self.data
        info = data["days"].get(d) or {"tests": [], "planned": [], "skipped": [], "blocks": []}
        today = d == data["today"]
        num = f"[{d.day:>2}]" if today else f" {d.day:>2} "
        tone = "selected" if d == self.sel else ("dim" if d < data["today"] else "normal")
        c.text(0, 0, num, tone, bold=today)
        c.text(0, 5, " ".join(marks(info)), "dim" if d < data["today"] else "normal")
        toks = tokens(info)
        if not toks:
            if info["skipped"]:
                c.text(1, 1, "skipped", "dim")
            return
        x, width = 1, c.width
        for i, (text, tone) in enumerate(toks):
            left = len(toks) - i
            more = f"+{left}"
            if x + len(text) > width or (left > 1 and x + len(text) + 1 + len(more) > width):
                c.text(1, x, more, "dim")
                return
            c.text(1, x, text, tone, bold=tone in ("warn", "bad"))
            x += len(text) + 1


class DayScreen:
    """One day's details, pushed by Enter on the calendar."""

    def __init__(self, ctx, data, day):
        self.ctx = ctx
        self.title = f"{day:%a %d %b %Y}"
        self.text = ctx.widgets.ScrollText(detail_lines(data, day))

    def keys(self):
        return self.text.keys()

    def render(self, c):
        self.text.render(c.sub(0, 1, c.height, c.width - 1))

    def key(self, k):
        return self.text.key(k)


def shift_month(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))
