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
            problems.append(f"Slot {d['slot']}: {d.get('reason', 'history unavailable')}")
            continue
        try:
            history = ss.get_history(d["slot"], d["serial"], user_id, d.get("current_test"))
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


def _fit(s, width, more="…"):
    s = str(s)
    return s if len(s) <= width else s[: max(0, width - 1)] + more


# --------------------------------------------------------------------------- text
# docs/tui-style.md §10 (Calendar) and §8: drive chips are a status glyph plus
# the slot number; "No test tonight" reasons start with what blocks it.

KIND = {"passed": "ok", "failed": "failed", "running": "running"}
EMPTY = {"tests": [], "planned": [], "skipped": [], "blocks": []}
LEGEND = [("planned", "planned"), ("ok", "passed"), ("failed", "failed"), ("running", "running"),
          ("scrub", "scrub"), ("backup", "backup")]


def chips(info):
    """[(status kind, slot)] for a day cell's second line: history, then the plan."""
    return ([(KIND[x["outcome"]], str(x["slot"])) for x in info["tests"]]
            + [("planned", str(e["slot"])) for e in info["planned"]])


def marks(info):
    """The status kinds of the day's scrubs and backups, in that order."""
    kinds = {b["kind"] for b in info["blocks"]}
    return [k for k in ("scrub", "backup") if k in kinds]


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


def day_rows(data, d):
    """The day panel's rows: ("status", kind, text), ("reason", text) or
    (text, role)."""
    info = data["days"].get(d) or EMPTY
    out = []
    for x in info["tests"]:
        out.append(("status", KIND[x["outcome"]], f"Slot {x['slot']}  {test_text(x)}"))
    for e in info["planned"]:
        out.append(("status", "planned",
                    f"Slot {e['slot']}  planned {e['start']:%H:%M}-{_hm(e['end'], e['start'].date())}, "
                    f"{e.get('cause') or 'due'}"))
    if info["skipped"]:
        out.append(("reason", f"{no_test(d, data['today'])}: " + "; ".join(short_reason(r) for r in info["skipped"])))
    for b in info["blocks"]:
        text = b["short"][0].upper() + b["short"][1:]
        out.append(("status", b["kind"], text + (" (done)" if b["past"] else "")))
    for z in data["snoozes"]:
        if z["until"] > d >= data["today"]:
            out.append((f"Slot {z['slot']} snoozed to {z['until']:%a %d %b}"
                        + (f" ({z['note']})" if z["note"] else ""), "label"))
    if not out:
        if data["plan_until"] is not None and d > data["plan_until"]:
            out.append(("Beyond the plan, which ends once every drive has a test.", "label"))
        elif d < data["today"]:
            out.append(("No test on record.", "label"))
        else:
            out.append(("Nothing planned.", "label"))
    return out


def detail_rows(data, d):
    """The day details screen: (heading,) rows, then rows as in day_rows plus
    ("note", text) lines under them."""
    info = data["days"].get(d) or EMPTY
    out = []
    if info["planned"]:
        out.append(("heading", "Planned (assuming earlier tests pass)"))
        for e in info["planned"]:
            out.append(("status", "planned", f"Slot {e['slot']}  {e['model']}  {e['serial']}"))
            out.append(("note", f"starts {e['start']:%H:%M}, ends about {_hm(e['end'], e['start'].date())}"))
            out.append(("note", f"why: {e.get('cause') or 'due'} ({e['reason']})"))
            if e["delayed_by"]:
                out.append(("note", "waited for: " + "; ".join(short_reason(r) for r in e["delayed_by"])))
        out.append(("",))
    if info["tests"]:
        out.append(("heading", "Test history (UniFi Drive)"))
        for x in info["tests"]:
            out.append(("status", KIND[x["outcome"]], f"Slot {x['slot']}  {x['model']}  {x['serial']}"))
            out.append(("note", test_text(x)))
        out.append(("",))
    if info["skipped"]:
        out.append(("heading", no_test(d, data["today"])))
        out += [("reason", short_reason(r)) for r in info["skipped"]]
        out.append(("",))
    if info["blocks"]:
        out.append(("heading", "Scrubs and backups"))
        for b in info["blocks"]:
            out.append(("status", b["kind"], b["text"]))
        out.append(("",))
    held = [z for z in data["snoozes"] if z["until"] > d >= data["today"]]
    if held:
        out.append(("heading", "Snoozed drives"))
        out += [("value", f"Slot {z['slot']}  {z['serial']}: {z['text']}") for z in held]
        out.append(("",))
    if data["plan_until"] is not None and d > data["plan_until"]:
        out.append(("label", f"The plan stops on {data['plan_until']:%a %d %b}, once every drive has a planned "
                             "test. Later tests depend on how those go."))
    elif not out:
        out.append(("label", "Nothing tested, planned or blocked on this day."))
    while out and out[-1] == ("",):
        out.pop()
    return out


def _date_y(d, today):
    """'Sun 21 Feb', with the year outside today's."""
    return f"{d:%a %d %b}" + (f" {d.year}" if d.year != today.year else "")


# --------------------------------------------------------------------------- screens

GRID_TOP = 2          # month line, weekday line
WEEK_ROWS = 2         # day line, chip line (a month spans at most 6 weeks)
MAX_CHIPS = 3


def draw_row(c, y, x, row, width):
    """One day_rows / detail_rows row at (y, x)."""
    def W_fit(s, w):
        return _fit(s, w, c.symbols["more"])
    if row[0] == "status":
        end = c.status(y, x, row[1]) + 1
        c.text(y, end, W_fit(row[2], width - (end - x)), "dim" if row[2].endswith("(done)") else "normal")
    elif row[0] == "reason":
        c.styled(y, x, W_fit(row[1], width - 2), "reason")
    elif row[0] == "note":
        c.styled(y, x + 2, W_fit(row[1], width - 4), "note")
    elif row[0] == "heading":
        c.styled(y, x, W_fit(row[1], width), "heading")
    elif len(row) == 2:
        role, text = (row[0], row[1]) if row[0] in ("value", "label") else (row[1], row[0])
        c.styled(y, x, W_fit(text, width), role)


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
        return [("Enter", "open"), ("t", "today"), ("r", "recalculate"),
                ("←→↑↓", "day"), ("PgUp/Dn", "month")]

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
        c.styled(0, 0, f"{sym['left']} {month:%B %Y} {sym['right']}", "heading")
        if self.error:
            c.text(2, 0, _fit(self.error, c.width - 1), "bad", bold=True)
            c.text(3, 0, "Press r to try again.", "dim")
            return
        data = self.data
        right = (f"planned through {_date_y(data['plan_until'], data['today'])}" if data["plan_until"]
                 else "nothing planned")
        c.styled(0, c.width - len(right), right, "label")

        cw = max(10, c.width // 7)
        for i, name in enumerate(calendar.day_abbr):
            c.styled(1, i * cw + 1, name, "label")
        weeks = calendar.Calendar().monthdatescalendar(month.year, month.month)
        for w, week in enumerate(weeks):
            for i, d in enumerate(week):
                if d.month == month.month:
                    self.cell(c.sub(GRID_TOP + w * WEEK_ROWS, i * cw, WEEK_ROWS, cw - 1), d)

        # The legend in the rule, then the selected day.
        rule = GRID_TOP + len(weeks) * WEEK_ROWS
        c.text(rule, 0, sym["h"] * c.width, "dim")
        x = 3
        for kind, label in LEGEND:
            item = f"{sym[kind]} {label}"
            if x + len(item) + 2 >= c.width:
                break
            c.text(rule, x - 1, f" {item} ", "dim")
            x += len(item) + 2
        top = rule + 1
        head = f"{self.sel:%A %d %B}" + (f" {self.sel.year}" if self.sel.year != data["today"].year else "")
        end = c.styled(top, 0, head, "heading")
        if self.sel == data["today"]:
            c.styled(top, end + 2, "today", "tag")
        rows = day_rows(data, self.sel)
        rows += [("reason", p) for p in data["problems"]]
        room = c.height - top - 1
        if len(rows) > room:
            rows = rows[: max(0, room - 1)] + [(f"{len(rows) - room + 1} more: Enter for day details", "label")]
        for i, row in enumerate(rows):
            draw_row(c, top + 1 + i, 2, row, c.width - 3)

    def cell(self, c, d):
        data = self.data
        info = data["days"].get(d) or EMPTY
        past = d < data["today"]
        today = d == data["today"]
        chosen = d == self.sel
        # Day number: brackets when selected, reversed digits for today.
        if chosen:
            c.text(0, 0, "[", "normal", bold=True)
            c.text(0, 3, "]", "normal", bold=True)
        c.text(0, 1, f"{d.day:>2}", "selected" if today else ("dim" if past else "normal"), bold=chosen)
        x = 5
        for kind in marks(info):
            c.text(0, x, c.symbols[kind], "dim" if past else "accent")
            x += 2
        if today and x + 5 <= c.width:
            c.text(0, x, "today", "accent")
        items = chips(info)
        if not items:
            if info["skipped"] and not past:
                c.text(1, 1, c.symbols["warning"], "warn")
            return
        x = 1
        for i, (kind, slot) in enumerate(items):
            left = len(items) - i
            text = f"{c.symbols[kind]}{slot}"
            more = f"+{left}"
            if i == MAX_CHIPS or x + len(text) > c.width or (left > 1 and x + len(text) + 1 + len(more) > c.width):
                c.text(1, x, more, "dim")
                return
            c.text(1, x, text, "dim" if past and kind == "ok" else STATUS_TONE[kind], bold=kind == "failed")
            x += len(text) + 1


STATUS_TONE = {"ok": "good", "failed": "bad", "running": "accent", "planned": "warn"}


class DayScreen:
    """One day's details, pushed by Enter on the calendar. Long rows wrap."""

    def __init__(self, ctx, data, day):
        self.ctx = ctx
        self.title = f"{day:%a %d %b %Y}"
        self.rows = detail_rows(data, day)
        self.top = 0
        self.page = 10
        self.total = len(self.rows)

    def keys(self):
        return [("↑↓", "scroll")]

    def lines(self, width):
        """Each row wrapped: [(row for the first line or None, indent, text)]."""
        wrap = self.ctx.widgets.wrap
        out = []
        for row in self.rows:
            if row == ("",):
                out.append((None, 0, ""))
                continue
            if row[0] == "status":
                indent, text = 2, row[2]
            elif row[0] in ("reason", "note"):
                indent, text = (2 if row[0] == "reason" else 4), row[1]
            elif row[0] == "heading":
                indent, text = 0, row[1]
            else:
                indent, text = 0, row[1] if row[0] in ("value", "label") else row[0]
            parts = wrap(text, max(10, width - indent)) or [""]
            first = (row[0], row[1], parts[0]) if row[0] == "status" else (row[0], parts[0])
            out.append((first, 0, ""))
            out += [(None, indent, p) for p in parts[1:]]
        return out

    def render(self, c):
        lines = self.lines(c.width - 1)
        self.total, self.page = len(lines), max(1, c.height)
        self.top = max(0, min(self.top, max(0, self.total - self.page)))
        for y, (row, indent, text) in enumerate(lines[self.top: self.top + c.height]):
            if row is not None:
                draw_row(c, y, 0, row, c.width - 1)
            elif text:
                c.text(y, indent, text, "dim" if indent == 4 else "normal")
        if self.top + c.height < self.total:
            c.text(c.height - 1, c.width - 1, c.symbols["down"], "dim")
        if self.top:
            c.text(0, c.width - 1, c.symbols["up"], "dim")

    def key(self, k):
        step = {"up": -1, "down": 1, "pgup": -self.page, "pgdn": self.page}
        if k in step:
            self.top += step[k]
        elif k == "home":
            self.top = 0
        elif k == "end":
            self.top = self.total
        else:
            return False
        self.top = max(0, min(self.top, max(0, self.total - self.page)))
        return True


def shift_month(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))
