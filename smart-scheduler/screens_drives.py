"""smart-scheduler's Drives screen for the better-unas TUI (issue #11).

A list of the SMART-capable drives, and Enter on one for its details:

  - schedule: last passed extended test, next planned test, interval and why,
    snooze and exclusion
  - key SMART values: temperature, power-on hours and attributes 5, 187, 197,
    198 and 199, with a rise since the last test marked
  - UniFi Drive's own test history (quick and extended)

Actions, on the list and in the details:

  t  run a test now (then e extended / k quick). check_run_now's checks are
     shown first; blocking ones stop it, overridable ones need a second confirm
  z  snooze ("not before <date>"), u  remove the snooze
  x  exclude the drive, or include it again
  i  set the drive's interval (empty: the normal rules)

Everything goes through actions.py and smart_scheduler's snooze code; this
file has no rules of its own. With ctx.dry_run nothing changes and the
message says what would have happened.

Loaded by plugin.py, which hands it `env()` returning (ss, actions, calendar
module). It imports nothing from the tool or the core itself. Standard library only.
"""

import contextlib
import copy
import datetime as dt

DAY = dt.timedelta(days=1)

# The SMART attributes the details show, with short names.
ATTRS = [(5, "Reallocated"), (187, "Uncorrectable"), (197, "Pending"),
         (198, "Offline uncorr."), (199, "CRC errors")]

OUTCOME_TONE = {"running": "warn", "passed": "good", "failed": "bad"}


class DrivesError(Exception):
    """Nothing to show (no API, no usable identity)."""


# --------------------------------------------------------------------------- data

def gather(env, ctx):
    """The drive list: one dict per SMART-capable drive, plus problems.

    Reuses the calendar's gather (the same evaluation and plan the calendar
    shows) and adds the storage facts (temperature, power-on hours)."""
    ss, _, cal = env()
    try:
        data = cal.gather(ss, ctx)
        storage = ss.get_storage()
    except cal.CalendarError as e:
        raise DrivesError(str(e))
    except ss.ApiError as e:
        raise DrivesError(f"UniFi Drive API problem: {e.detail}")
    cfg = cal.config_from(ss, ctx.config())
    disks = {d.get("serial"): d for d in storage["disks"]}
    nxt = {}
    for v in data["days"].values():
        for e in v["planned"]:
            if e["serial"] not in nxt or e["start"] < nxt[e["serial"]]["start"]:
                nxt[e["serial"]] = e
    failed = {}
    for day in sorted(data["days"]):
        for x in data["days"][day]["tests"]:
            if x["outcome"] != "running":
                failed[x["serial"]] = x["outcome"] == "failed"
    snoozes = {z["serial"]: z for z in data["snoozes"]}
    drives = []
    for d in sorted(data["drives"], key=lambda d: _slot_key(d["slot"])):
        disk = disks.get(d["serial"], {})
        drives.append(dict(d, disk=disk, next=nxt.get(d["serial"]), snooze=snoozes.get(d["serial"]),
                           override=cfg.overrides.get(d["serial"]),
                           last_failed=failed.get(d["serial"], False)))
    return {"now": data["now"], "drives": drives, "problems": data["problems"]}


def details(env, ctx, drive):
    """What the details add for one drive: its full history and SMART values.

    Returns {"history": [...] or None, "history_error", "attrs": {id: raw} or
    None, "baseline": {id: raw} or None}."""
    ss, _, cal = env()
    cfg = cal.config_from(ss, ctx.config())
    state = ss.load_state()
    out = {"history": None, "history_error": None, "attrs": None,
           "baseline": ((state.get("drives") or {}).get(drive["serial"]) or {}).get("attrs_at_last_test")}
    user_id, who, err = ss.resolve_identity(cfg)
    user_id = user_id or state.get("user_id")
    if not user_id:
        out["history_error"] = f"can't read it as {who}: {err or 'it no longer exists'}"
    else:
        try:
            out["history"] = ss.get_history(drive["slot"], drive["serial"], user_id)
        except ss.ApiError as e:
            out["history_error"] = e.detail
    dev = ss.serial_to_device().get(drive["serial"])
    if dev:
        _, attrs = ss.smart_facts(dev, {a for a, _ in ATTRS})
        out["attrs"] = {int(k): v for k, v in attrs.items()}
    return out


def _slot_key(slot):
    s = str(slot)
    return (0, int(s), s) if s.isdigit() else (1, 0, s)


# --------------------------------------------------------------------------- text

def _date(t):
    return f"{t:%a %d %b}" if t else "never"


def status_cell(d, today):
    """(text, tone) for the list's status column. Says it in words, not color."""
    if d.get("excluded"):
        return "excluded", "dim"
    if d.get("error"):
        return "no history", "bad"
    if d.get("running"):
        return "test running", "warn"
    if d.get("last_failed"):
        return "last test failed", "bad"
    if d.get("snooze"):
        return f"snoozed to {d['snooze']['until']:%d %b}", "normal"
    if d.get("due") and d["due"].date() <= today:
        return "due", "warn"
    return "ok", "good"


def last_test(ss, d, tz):
    t = ss.parse_time(d.get("last")) if d.get("last") else None
    return t.astimezone(tz) if t else None


def schedule_lines(ss, d, tz):
    """[(label, text, tone)] for the details' schedule block."""
    last = last_test(ss, d, tz)
    out = [("Last test", f"{_date(last)} (passed)" if last else "none passed yet", "normal" if last else "dim")]
    if d.get("excluded"):
        out.append(("Next test", "none: excluded", "dim"))
    elif d.get("running"):
        how = ""
        if d.get("outside_drive"):
            left = d.get("remaining_percent")
            how = " (started outside UniFi Drive" + (f", {left}% left)" if left is not None else ")")
        out.append(("Next test", "a test is running now" + how, "warn"))
    elif d.get("next"):
        out.append(("Next test", f"{d['next']['start']:%a %d %b %H:%M} (planned)", "warn"))
    else:
        out.append(("Next test", "not in the plan yet", "dim"))
    if d.get("interval"):
        why = "set for this drive" if d.get("override") else d.get("why", "")
        out.append(("Interval", f"{d['interval']} days: {why}", "normal"))
    if d.get("reason") and not d.get("excluded"):
        out.append(("Why", d["reason"], "normal"))
    z = d.get("snooze")
    out.append(("Snooze", f"until {z['until']:%a %d %b %Y}" + (f" ({z['note']})" if z["note"] else "")
                if z else "none", "normal" if z else "dim"))
    out.append(("Excluded", "yes (EXCLUDE_SERIALS)" if d.get("excluded") else "no",
                "warn" if d.get("excluded") else "dim"))
    return out


def smart_lines(d, extra):
    """[(label, value, note, tone)] for the details' SMART block."""
    disk = d.get("disk") or {}
    temp = disk.get("temperature")
    hours = disk.get("powerOnHours")
    out = [("Temperature", f"{temp} C" if temp is not None else "?", "", "normal"),
           ("Power-on hours", f"{hours:,}" if isinstance(hours, int) else "?", "", "normal")]
    attrs = (extra or {}).get("attrs")
    base = (extra or {}).get("baseline")
    for a, name in ATTRS:
        label = f"{a:>3} {name}"
        if attrs is None:
            out.append((label, "?", "", "dim"))
            continue
        v = attrs.get(a)
        if v is None:
            out.append((label, "-", "", "dim"))
            continue
        was = None if base is None else base.get(str(a), base.get(a))
        if was is not None and v > was:
            out.append((label, str(v), f"+{v - was} since last test", "warn"))
        else:
            out.append((label, str(v), "", "normal"))
    return out


def history_rows(ss, history, tz):
    """Table rows, newest first: type, started, finished, result."""
    rows = []

    def when(h):
        return ss.parse_time(h.get("createdAt")) or ss.parse_time(h.get("finishedAt"))

    stamped = [(when(h), h) for h in history]
    stamped.sort(key=lambda p: p[0] or dt.datetime.min.replace(tzinfo=tz), reverse=True)
    for start, h in stamped:
        fin = ss.parse_time(h.get("finishedAt"))
        outcome = "running" if ss.is_running(h) else "passed" if ss.is_good(h) else "failed"
        word = outcome if outcome != "failed" else f"failed ({h.get('status')}/{h.get('result')})"
        rows.append([str(h.get("type", "?")).lower(),
                     f"{start.astimezone(tz):%a %d %b %H:%M}" if start else "?",
                     f"{fin.astimezone(tz):%a %d %b %H:%M}" if fin and outcome != "running" else "",
                     (word, OUTCOME_TONE[outcome])])
    return rows


def parse_snooze(text, today):
    """'2026-12-01 waiting for a spare' or '7 note' -> (date, note). Raises ValueError."""
    parts = text.strip().split(None, 1)
    if not parts:
        raise ValueError("give a date (YYYY-MM-DD) or a number of days")
    first, note = parts[0], parts[1] if len(parts) > 1 else ""
    if first.isdigit() and len(first) <= 4:
        return today + int(first) * DAY, note
    try:
        return dt.date.fromisoformat(first), note
    except ValueError:
        raise ValueError(f"not a date: {first!r} (want YYYY-MM-DD or a number of days)")


def check_lines(checks):
    return [f"{'can override' if c.overridable else 'BLOCKS':>12}  {c.reason}" for c in checks]


# --------------------------------------------------------------------------- actions

class Actions:
    """Run now, snooze, exclude and set interval for one screen. `reload()`
    is called after anything that may have changed what the screen shows."""

    def __init__(self, ctx, env, reload):
        self.ctx, self.env, self.reload = ctx, env, reload
        self.prompt = None        # (label, field, on_done) while asking for text
        self.picking = None       # the drive whose test kind is being picked

    # ------------------------------------------------------------ input state

    @property
    def busy(self):
        return self.prompt is not None or self.picking is not None

    def keys(self):
        if self.prompt:
            return self.prompt[1].keys()
        if self.picking:
            return [("e", "extended"), ("k", "quick"), ("Esc", "cancel")]
        return [("t", "test"), ("z", "snooze"), ("x", "exclude"), ("i", "interval"), ("u", "unsnooze")]

    def key(self, k, drive):
        """Handle an action key (or input for one in progress). True if used."""
        if self.prompt:
            label, field, done = self.prompt
            field.key(k)
            if not field.editing:
                self.prompt = None
                if k == "enter":
                    done(field.text)
            return True
        if self.picking:
            d, self.picking = self.picking, None
            if k in ("e", "k"):
                self.run_test(d, "extended" if k == "e" else "quick")
            return True
        if drive is None or k not in ("t", "z", "u", "x", "i"):
            return False
        if k == "t":
            self.picking = drive
        elif k == "z":
            default = (self.ctx.now().date() + 7 * DAY).isoformat()
            self.ask("Snooze until (YYYY-MM-DD or days, then a note)", default,
                     lambda text: self.snooze(drive, text))
        elif k == "u":
            self.unsnooze(drive)
        elif k == "x":
            self.exclude(drive, not drive.get("excluded"))
        elif k == "i":
            cur = "" if drive.get("override") is None else str(drive["override"])
            self.ask("Interval in days (empty: the normal rules)", cur,
                     lambda text: self.set_interval(drive, text))
        return True

    def cancel(self):
        """Drop a half-entered prompt (the core ends editing when the tab changes)."""
        if self.prompt:
            self.prompt[1].finish()
        self.prompt = self.picking = None

    def ask(self, label, default, done):
        field = self.ctx.widgets.TextField({"key": "PROMPT", "label": label, "type": "text", "default": default})
        field.key("enter")        # start editing at once; this registers it as the active editor
        self.prompt = (label, field, done)

    def render(self, c):
        """The prompt or kind picker, on the canvas's last line."""
        y = c.height - 1
        if self.prompt:
            label, field, _ = self.prompt
            c.fill(y, 0, 1, c.width)
            c.text(y, 1, label + ":", bold=True)
            x = len(label) + 3
            field.render(c.sub(y, x, 1, max(1, c.width - x - 1)))
        elif self.picking:
            c.fill(y, 0, 1, c.width)
            c.text(y, 1, f"Run which test on slot {self.picking['slot']}?  e extended  k quick  Esc cancel",
                   bold=True)

    # ------------------------------------------------------------ helpers

    def label(self, d):
        return f"slot {d['slot']} ({d['serial']})"

    def config_path(self):
        return self.ctx.tool.file("CONFIG")

    def guarded(self):
        """The tool's lock, except in a dry run (which writes nothing)."""
        return contextlib.nullcontext() if self.ctx.dry_run else self.ctx.lock()

    def config_error(self, e):
        errors = getattr(e, "errors", None) or {}
        return "; ".join(f"{k}: {m}" for k, m in errors.items()) or str(e)

    # ------------------------------------------------------------ run now

    def run_test(self, d, kind):
        ss, actions, cal = self.env()
        ctx = self.ctx
        cfg = cal.config_from(ss, ctx.config())
        checks = ctx.run_slow(lambda: actions.check_run_now(d["slot"], kind, cfg=cfg, t=ctx.now()),
                              "Checking whether a test can start")
        hard, soft = actions.blocking(checks), actions.overridable(checks)
        what = f"{'an' if kind == 'extended' else 'a'} {kind} test on {self.label(d)}"
        if hard:
            ctx.message(f"Can't start {what}: " + "; ".join(c.reason for c in hard) + ".", "bad")
            ctx.push(ChecksScreen(ctx, f"Can't start {what}", checks))
            return
        if soft:
            lines = ["These checks fail:"] + check_lines(soft)
            if not ctx.confirm(f"Start {what} anyway?", lines, second="Override these checks and start the test?"):
                ctx.message("Not started.")
                return
        else:
            if not ctx.confirm(f"Start {what}?", ["All safety checks pass."]):
                ctx.message("Not started.")
                return
        with self.guarded():
            r = ctx.run_slow(lambda: actions.run_now(d["slot"], kind, override=bool(soft), cfg=cfg,
                                                     dry_run=ctx.dry_run, lock=False, t=ctx.now()),
                             "Starting the test")
        ctx.message(r.message, "good" if r.started else "bad")
        if not r.started and actions.blocking(r.checks):
            ctx.push(ChecksScreen(ctx, f"Can't start {what}", r.checks))
            return
        if r.started and not ctx.dry_run:
            self.reload()

    # ------------------------------------------------------------ snooze

    def snooze(self, d, text):
        ss, _, _ = self.env()
        ctx = self.ctx
        t = ctx.now()
        try:
            until, note = parse_snooze(text, t.date())
        except ValueError as e:
            ctx.message(f"Not snoozed: {e}.", "bad")
            return
        try:
            with self.guarded():
                state = ss.load_state()
                z = ss.set_snooze(state if not ctx.dry_run else copy.deepcopy(state), d["serial"], until, note, t)
                if not ctx.dry_run:
                    ss.save_state(state, False)
        except ValueError as e:
            ctx.message(f"Not snoozed: {e}.", "bad")
            return
        what = f"{self.label(d)}: no test before {until:%a %d %b %Y}" + (f" ({z['note']})" if z.get("note") else "")
        if ctx.dry_run:
            ctx.message(f"(dry-run) would snooze {what}.")
            return
        ctx.message(f"Snoozed {what}.", "good")
        self.reload()

    def unsnooze(self, d):
        ss, _, _ = self.env()
        ctx = self.ctx
        if not d.get("snooze"):
            ctx.message(f"{_cap(self.label(d))} isn't snoozed.")
            return
        if not ctx.confirm(f"Remove the snooze on {self.label(d)}?",
                           [f"It is snoozed until {d['snooze']['until']:%a %d %b %Y}. "
                            "The plan will treat it normally again."]):
            return
        if ctx.dry_run:
            ctx.message(f"(dry-run) would remove the snooze on {self.label(d)}.")
            return
        with self.guarded():
            state = ss.load_state()
            gone = ss.clear_snooze(state, d["serial"])
            if gone:
                ss.save_state(state, False)
        ctx.message(f"Removed the snooze on {self.label(d)}." if gone else f"{self.label(d)} wasn't snoozed.",
                    "good" if gone else "normal")
        self.reload()

    # ------------------------------------------------------------ config edits

    def exclude(self, d, on):
        ss, actions, _ = self.env()
        ctx = self.ctx
        if on:
            q, lines = f"Exclude {self.label(d)} from testing?", [
                "Adds its serial to EXCLUDE_SERIALS: no scheduled tests until you include it again. "
                "Run now still works."]
        else:
            q, lines = f"Include {self.label(d)} in testing again?", [
                "Removes its serial from EXCLUDE_SERIALS; the normal schedule applies."]
        if not ctx.confirm(q, lines):
            return
        try:
            with self.guarded():
                changed = actions.exclude(d["serial"], on, path=self.config_path(), dry_run=ctx.dry_run, lock=False)
        except ss.settings.ValidationError as e:
            ctx.message(f"Not changed: {self.config_error(e)}.", "bad")
            return
        verb = "exclude" if on else "include"
        if not changed:
            ctx.message(f"{_cap(self.label(d))} was already {verb}d.")
        elif ctx.dry_run:
            ctx.message(f"(dry-run) would {verb} {self.label(d)} (EXCLUDE_SERIALS).")
        else:
            ctx.message(f"{_cap(verb)}d {self.label(d)}.", "good")
            self.reload()

    def set_interval(self, d, text):
        ss, actions, _ = self.env()
        ctx = self.ctx
        text = text.strip()
        if text and not text.isdigit():
            ctx.message(f"Not changed: {text!r} isn't a whole number of days.", "bad")
            return
        days = int(text) if text else None
        try:
            with self.guarded():
                changed = actions.set_interval(d["serial"], days, path=self.config_path(),
                                               dry_run=ctx.dry_run, lock=False)
        except ss.settings.ValidationError as e:
            ctx.message(f"Not changed: {self.config_error(e)}.", "bad")
            return
        what = f"every {days} days" if days else "the normal rules"
        if not changed:
            ctx.message(f"{_cap(self.label(d))} already uses {what}.")
        elif ctx.dry_run:
            ctx.message(f"(dry-run) would set {self.label(d)} to {what} (INTERVAL_OVERRIDES).")
        else:
            ctx.message(f"{_cap(self.label(d))} now uses {what}.", "good")
            self.reload()


# --------------------------------------------------------------------------- screens

def _cap(s):
    return s[:1].upper() + s[1:]


def _fit(s, width):
    return s if len(s) <= width else s[: max(0, width - 1)] + "~"


class DrivesScreen:
    title = "Drives"

    COLUMNS = [("Slot", 4, "right"), ("Serial", 12), ("Temp", 5, "right"), ("Last test", 10),
               ("Next test", 10), ("Every", 6, "right"), ("Status", None)]

    def __init__(self, ctx, env):
        """env() returns (ss, actions, calendar module); plugin.py binds it to the live tool."""
        self.ctx, self.env = ctx, env
        self.data = None
        self.error = None
        self.table = ctx.widgets.Table(self.COLUMNS, empty="No SMART-capable drives.")
        self.actions = Actions(ctx, env, self.load)

    def keys(self):
        if self.actions.busy:
            return self.actions.keys()
        return [("Enter", "details")] + self.actions.keys() + [("r", "refresh")]

    def show(self):
        self.actions.cancel()
        self.load()

    def load(self):
        try:
            self.data = self.ctx.run_slow(lambda: gather(self.env, self.ctx), "Reading drives")
            self.error = None
        except DrivesError as e:
            self.data, self.error = None, str(e)
        self.table.set_rows(self.rows())

    def rows(self):
        if not self.data:
            return []
        ss = self.env()[0]
        tz = self.data["now"].tzinfo
        today = self.data["now"].date()
        out = []
        for d in self.data["drives"]:
            temp = (d.get("disk") or {}).get("temperature")
            last = last_test(ss, d, tz)
            nxt = d["next"]["start"] if d.get("next") else None
            out.append([str(d["slot"]), d["serial"], f"{temp} C" if temp is not None else "?",
                        (_date(last), "dim") if last else ("never", "dim"),
                        (f"{nxt:%a %d %b}", "warn") if nxt else ("-", "dim"),
                        f"{d['interval']}d" if d.get("interval") else "-",
                        status_cell(d, today)])
        return out

    @property
    def current(self):
        if not self.data or self.table.index is None:
            return None
        return self.data["drives"][self.table.index]

    def key(self, k):
        if self.actions.busy:
            return self.actions.key(k, self.current)
        if k == "r":
            self.load()
        elif k == "enter" and self.current:
            self.ctx.push(DriveScreen(self.ctx, self.env, self.current["serial"]))
        elif self.table.key(k):
            pass
        else:
            return self.actions.key(k, self.current)
        return True

    def render(self, c):
        if self.error:
            c.text(0, 1, _fit(self.error, c.width - 2), "bad", bold=True)
            c.text(1, 1, "Press r to try again.", "dim")
            return
        if not self.data:
            return
        n = len(self.data["drives"])
        extra = [(f"Problem: {p}", "bad") for p in self.data["problems"]]
        d = self.current
        info = []
        if d:
            info.append((f"Slot {d['slot']}  {d.get('model', '?')}", "normal"))
            if d.get("reason"):
                info.append(("  " + d["reason"], "dim" if d.get("excluded") else "normal"))
        bottom = info + extra
        bottom = bottom[: max(0, c.height - 4 - min(n, 3))]
        table_h = max(3, min(n + 1, c.height - len(bottom) - 1))
        self.table.render(c.sub(0, 0, table_h, c.width))
        y = table_h + 1
        for text, tone in bottom:
            c.text(y, 1, _fit(text, c.width - 2), tone)
            y += 1
        self.actions.render(c)


class DriveScreen:
    """One drive's details, pushed by Enter on the list."""

    def __init__(self, ctx, env, serial):
        self.ctx, self.env, self.serial = ctx, env, serial
        self.title = f"Drive {serial}"
        self.drive = None
        self.extra = None
        self.error = None
        self.now = ctx.now()
        self.history = ctx.widgets.Table([("Type", 9), ("Started", 16), ("Finished", 16), ("Result", None)],
                                         selectable=False, empty="No tests on record.")
        self.actions = Actions(ctx, env, self.load)

    def keys(self):
        if self.actions.busy:
            return self.actions.keys()
        return self.actions.keys() + [("↑↓", "history"), ("r", "refresh")]

    def show(self):
        self.actions.cancel()
        self.load()

    def load(self):
        def read():
            data = gather(self.env, self.ctx)
            d = next((x for x in data["drives"] if x["serial"] == self.serial), None)
            return data, d, (details(self.env, self.ctx, d) if d else None)
        try:
            data, self.drive, self.extra = self.ctx.run_slow(read, "Reading drive")
            self.now = data["now"]
            self.error = None if self.drive else f"Drive {self.serial} is no longer in the drive list."
        except DrivesError as e:
            self.drive, self.extra, self.error = None, None, str(e)
        if self.drive:
            self.title = f"Slot {self.drive['slot']}: {self.drive.get('model', '?')} ({self.serial})"
        ss = self.env()[0]
        hist = (self.extra or {}).get("history")
        self.history.set_rows(history_rows(ss, hist, self.now.tzinfo) if hist else [])
        if self.extra and self.extra.get("history_error"):
            self.history.empty = f"History unavailable: {self.extra['history_error']}"

    def key(self, k):
        if self.actions.busy:
            return self.actions.key(k, self.drive)
        if k == "r":
            self.load()
            return True
        if self.history.key(k):
            return True
        return self.actions.key(k, self.drive)

    def render(self, c):
        if self.error:
            c.text(0, 1, _fit(self.error, c.width - 2), "bad", bold=True)
            c.text(1, 1, "Press r to try again, Esc to go back.", "dim")
            return
        if not self.drive:
            return
        ss = self.env()[0]
        d = self.drive
        left_w = c.width - 33
        rx = left_w + 1
        c.text(0, 1, "Schedule", bold=True)
        lines = schedule_lines(ss, d, self.now.tzinfo)
        spare = 8 - len(lines)          # rows 1-8; long lines wrap while there's room
        rows = []
        for label, text, tone in lines:
            parts = self.ctx.widgets.wrap(text, left_w - 12)
            if len(parts) > 1 and spare > 0:
                rows += [(label, parts[0], tone), ("", " ".join(parts[1:]), tone)]
                spare -= 1
            else:
                rows.append((label, text, tone))
        for i, (label, text, tone) in enumerate(rows[:8]):
            c.text(1 + i, 1, f"{label:<10}", "dim")
            c.text(1 + i, 12, _fit(text, left_w - 12), tone, bold=tone == "warn")
        c.text(0, rx, "SMART", bold=True)
        for i, (label, value, note, tone) in enumerate(smart_lines(d, self.extra)):
            c.text(1 + i, rx, f"{label:<19}", "dim")
            c.text(1 + i, rx + 19, f"{value:>6}", tone, bold=bool(note))
            if note:
                c.text(1 + i, rx + 26, note.split()[0], tone, bold=True)
        rises = [n for _, _, n, _ in smart_lines(d, self.extra) if n]
        y = 9
        if rises:
            c.text(y, rx, "+N: rose since last test", "warn")
        if (self.extra or {}).get("attrs") is None:
            c.text(y, rx, "smartctl values unavailable", "dim")
        y += 1
        c.text(y, 1, "UniFi test history", bold=True)
        self.history.render(c.sub(y + 1, 0, max(2, c.height - y - 2), c.width))
        self.actions.render(c)


class ChecksScreen:
    """The full check list when a test can't start."""

    def __init__(self, ctx, title, checks):
        self.title = title
        lines = [("Safety checks for starting a test now:", "normal"), ("", "normal")]
        for ch in checks:
            if ch.overridable:
                lines.append((f"  can override  {ch.reason}", "warn"))
            else:
                lines.append((f"  BLOCKS        {ch.reason}", "bad"))
        lines += [("", "normal"),
                  ("A running test, an unhealthy pool, or a drive or identity problem always blocks; "
                   "fix that first. A busy drive, a coming scrub or backup, or the start window can be "
                   "overridden with a second confirm.", "dim")]
        self.text = ctx.widgets.ScrollText(lines)

    def keys(self):
        return self.text.keys()

    def render(self, c):
        self.text.render(c.sub(0, 1, c.height, c.width - 1))

    def key(self, k):
        return self.text.key(k)
