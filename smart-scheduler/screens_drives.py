"""smart-scheduler's Drives screen for the better-unas TUI (issue #11).

A list of the SMART-capable drives, and Enter on one for its details:

  - schedule: last passed extended test, next planned test, interval and why,
    snooze and exclusion
  - key SMART values: temperature, power-on hours and attributes S5, S187, S197,
    S198 and S199, with a rise since the last test marked
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
ATTRS = [(5, "Reallocated sectors"), (187, "Uncorrectable errors"), (197, "Pending sectors"),
         (198, "Offline uncorrectable"), (199, "Interface CRC errors")]


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
    done, since = {}, {}
    for day in sorted(data["days"]):
        for x in data["days"][day]["tests"]:
            if x["outcome"] == "running":
                if not x.get("outside"):
                    since[x["serial"]] = x["start"]
            else:
                done[x["serial"]] = {"at": x["end"] or x["start"], "outcome": x["outcome"]}
    snoozes = {z["serial"]: z for z in data["snoozes"]}
    drives = []
    for d in sorted(data["drives"], key=lambda d: _slot_key(d["slot"])):
        disk = disks.get(d["serial"], {})
        drives.append(dict(d, disk=disk, next=nxt.get(d["serial"]), snooze=snoozes.get(d["serial"]),
                           override=cfg.overrides.get(d["serial"]),
                           last_done=done.get(d["serial"]), running_since=since.get(d["serial"]),
                           last_failed=(done.get(d["serial"]) or {}).get("outcome") == "failed"))
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
            out["history"] = ss.get_history(drive["slot"], drive["serial"], user_id, drive.get("current_test"))
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
# Words follow docs/tui-style.md §8: a value answers its label's question,
# a fact is said once, and smart-scheduler's vocabulary for Why and the
# interval's source.

def _date(t, now=None):
    """'Thu 12 Nov', with the year outside the current one; 'never' for None."""
    if not t:
        return "never"
    s = f"{t:%a %d %b}"
    return s + f" {t.year}" if now is not None and t.year != now.year else s


def _when(t, now=None):
    return f"{_date(t, now)} {t:%H:%M}"


def _took(start, end):
    if not start or not end or end < start:
        return ""
    m = int((end - start).total_seconds() // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def _size(n):
    if not isinstance(n, (int, float)) or n <= 0:
        return ""
    for unit, k in (("TB", 1e12), ("GB", 1e9)):
        if n >= k:
            v = n / k
            num = f"{v:.0f}" if v >= 10 else f"{v:.1f}".removesuffix(".0")
            return f"{num} {unit}"
    return f"{n / 1e6:.0f} MB"


def last_test(ss, d, tz):
    """The last passed extended test's time (UniFi's history), or None."""
    t = ss.parse_time(d.get("last")) if d.get("last") else None
    return t.astimezone(tz) if t else None


def last_done(ss, d, tz):
    """(time, "passed" | "failed") of the last finished extended test, or None."""
    if d.get("last_done"):
        return d["last_done"]["at"], d["last_done"]["outcome"]
    t = last_test(ss, d, tz)
    return (t, "passed") if t else None


def percent_done(d):
    left = d.get("remaining_percent")
    return None if left is None else max(0, min(100, 100 - left))


def why(d):
    """Why the next test is when it is, in one word or a short phrase."""
    if d.get("excluded"):
        return "excluded"
    if d.get("error"):
        return "unknown"
    if d.get("snooze"):
        return "snoozed"
    return d.get("cause") or "interval"


def state(d, today):
    """(status kind or None, word) for the drive as a whole: the Drive page's
    top-right and the selection panel's glyph."""
    if d.get("error"):
        return "failed", "history unreadable"
    if d.get("excluded"):
        return None, "excluded"
    if d.get("running"):
        return "running", "test running"
    if d.get("last_failed"):
        return "failed", "last test failed"
    if d.get("snooze"):
        return None, "snoozed"
    if d.get("cause") == "warning signs" or d.get("source") == "warning signs":
        return "warning", "warning signs"
    if d.get("due") and d["due"].date() <= today:
        return "warning", "due now"
    return "ok", "up to date"


def last_cell(ss, d, now):
    """The list's Last test cell."""
    if d.get("running") and not d.get("excluded"):
        pct = percent_done(d)
        return ("status", "running", f"running {pct}%" if pct is not None else "running")
    if d.get("error"):
        return ("unknown", "dim")
    done = last_done(ss, d, now.tzinfo)
    if not done:
        return "never"
    return ("status", "ok" if done[1] == "passed" else "failed", _date(done[0], now))


def next_cell(d, now):
    """The list's Next test cell."""
    if d.get("excluded"):
        return ("excluded", "dim")
    if d.get("error"):
        return ("unknown", "dim")
    nxt = d["next"]["start"] if d.get("next") else None
    if nxt and d.get("running"):
        return _date(nxt, now)                    # after the running test: nothing to flag
    if nxt:
        return ("status", "planned", _date(nxt, now))
    if d.get("snooze"):
        return f"snoozed to {d['snooze']['until']:%d %b}"
    if d.get("due") and d["due"].date() <= now.date():
        return ("status", "warning", "due now")
    if d.get("due"):
        return _date(d["due"], now)
    return ("none", "dim")


def panel_line(ss, d, now, mid="·"):
    """(status kind or None, sentence, tone) for the list's selection panel."""
    tz = now.tzinfo
    nxt = d["next"]["start"] if d.get("next") else None
    then = f"Next: {_date(nxt, now)}." if nxt else ""
    if d.get("error"):
        return "failed", f"History unreadable: {d.get('reason', 'unknown error')}.", "bad"
    if d.get("excluded"):
        return None, "Excluded from testing (EXCLUDE_SERIALS). Run now still works.", "dim"
    if d.get("running"):
        pct = percent_done(d)
        if d.get("outside_drive"):
            s = "Test running outside UniFi Drive" + (f", {pct}%" if pct is not None else "") + "."
        else:
            started = d.get("running_since")
            s = ("Extended test running" + (f", {pct}%" if pct is not None else "")
                 + (f", started {started:%a %H:%M}" if started else "") + ".")
        return "running", f"{s} {then}".strip(), "normal"
    done = last_done(ss, d, tz)
    plan = f"{_when(nxt, now)}" if nxt else "not in the plan yet"
    if d.get("last_failed") and done:
        return "failed", f"Last test failed {_date(done[0], now)}. Retry: {plan}.", "normal"
    if d.get("snooze"):
        z = d["snooze"]
        note = f" ({z['note']})" if z.get("note") else ""
        later = f"Next test {_date(nxt, now)}." if nxt else "Not in the plan yet."
        return None, f"Snoozed to {_date(z['until'], now)}{note}. {later}", "normal"
    kind = "planned" if nxt else "warning"
    source = d.get("source", "default")
    every = f"every {d.get('interval')} days" + ("" if source in (why(d), "default") else f" ({source})")
    return kind, f" {mid} ".join([f"Next test {plan}", why(d), every]), "normal"


def schedule_rows(ss, d, now):
    """KeyValue rows for the Drive page's Schedule panel."""
    tz = now.tzinfo
    done = last_done(ss, d, tz)
    if done:
        rows = [("Last test", ("status", "ok" if done[1] == "passed" else "failed", _date(done[0], now)),
                 done[1])]
    else:
        rows = [("Last test", "never")]
    nxt = d["next"]["start"] if d.get("next") else None
    if d.get("excluded"):
        rows.append(("Next test", ""))
    elif d.get("running"):
        pct = percent_done(d)
        rows.append(("Next test", ("status", "running", "running now"),
                     f"{pct}% done" if pct is not None else None))
    elif nxt:
        rows.append(("Next test", ("status", "planned", _when(nxt, now))))
    elif d.get("due") and d["due"].date() <= now.date():
        rows.append(("Next test", ("status", "warning", "due now"), "not in the plan yet"))
    else:
        rows.append(("Next test", ""))
    rows.append(("Why", why(d)))
    if d.get("interval"):
        rows.append(("Interval", f"{d['interval']} days", d.get("source")))
    else:
        rows.append(("Interval", ""))
    z = d.get("snooze")
    rows.append(("Snoozed", f"until {_date(z['until'], now)}" if z else "no"))
    if z and z.get("note"):
        rows.append(("note", z["note"]))
    rows.append(("Excluded", "yes" if d.get("excluded") else "no", "EXCLUDE_SERIALS" if d.get("excluded") else None))
    return rows


def health_rows(d, extra):
    """[(label, value, tone, rise, attr id)] for the Health panel; rise is the
    increase since the last test (or 0), attr id None for non-attributes."""
    disk = d.get("disk") or {}
    attrs = (extra or {}).get("attrs")
    base = (extra or {}).get("baseline")
    out = []
    for a, name in ATTRS:
        if attrs is None:
            out.append((name, "unknown", "dim", 0, a))
            continue
        v = attrs.get(a)
        if v is None:
            out.append((name, "none", "dim", 0, a))
            continue
        was = None if base is None else base.get(str(a), base.get(a))
        rise = v - was if was is not None and v > was else 0
        out.append((name, f"{v:,}", "bad" if rise else "normal", rise, a))
    return disk.get("temperature"), disk.get("powerOnHours"), out


def history_rows(ss, history, tz, now=None):
    """Table rows, newest first: type, started, took, result."""
    rows = []

    def when(h):
        return ss.parse_time(h.get("createdAt")) or ss.parse_time(h.get("finishedAt"))

    stamped = [(when(h), h) for h in history]
    stamped.sort(key=lambda p: p[0] or dt.datetime.min.replace(tzinfo=tz), reverse=True)
    for start, h in stamped:
        fin = ss.parse_time(h.get("finishedAt"))
        if ss.is_running(h):
            left = ss.drive_progress_left(h)
            result = ("status", "running", "running", f"{100 - left}% done" if left is not None else "")
            took = ""
        else:
            good = ss.is_good(h)
            detail = "" if good else "/".join(str(h.get(k) or "?") for k in ("status", "result"))
            result = ("status", "ok" if good else "failed", "passed" if good else "failed",
                      "" if detail == "failed/failed" else detail)
            took = _took(start, fin)
        rows.append([str(h.get("type", "?")).lower(),
                     _when(start.astimezone(tz), now) if start else ("unknown", "dim"),
                     took, result])
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

    def keys(self, drive=None):
        if self.prompt:
            return self.prompt[1].keys()
        if self.picking:
            return [("e", "extended"), ("k", "quick"), ("Esc", "cancel")]
        out = [("t", "test"), ("z", "snooze"), ("x", "include" if (drive or {}).get("excluded") else "exclude"),
               ("i", "interval")]
        return out + ([("u", "unsnooze")] if (drive or {}).get("snooze") else [])

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
            c.text(y, 1, f"Run which test on Drive {self.picking['slot']}?  e extended  k quick  Esc cancel",
                   bold=True)

    # ------------------------------------------------------------ helpers

    def label(self, d):
        return f"Drive {d['slot']} ({d['serial']})"

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


class DrivesScreen:
    title = "Drives"

    COLUMNS = [("Drive", 5, "right"), ("Model", None), ("Temp", 5, "right"), ("Last test", 17),
               ("Next test", 17), ("Every", 5, "right")]
    PANEL_H = 3

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
        d = self.current
        lead = [("Enter", "open")] if d else []
        return lead + self.actions.keys(d) + [("r", "refresh"), ("↑↓", "move")]

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

    def rows(self, symbols=None):
        if not self.data:
            return []
        deg = (symbols or {}).get("deg", "°")
        ss = self.env()[0]
        now = self.data["now"]
        out = []
        for d in self.data["drives"]:
            temp = (d.get("disk") or {}).get("temperature")
            out.append([str(d["slot"]), d.get("model", "?"),
                        f"{temp}{deg}C" if temp is not None else ("unknown", "dim"),
                        last_cell(ss, d, now), next_cell(d, now),
                        "" if d.get("excluded") or not d.get("interval") else f"{d['interval']}d"])
        return out

    @property
    def current(self):
        if not self.data or self.table.index is None:
            return None
        return self.data["drives"][self.table.index]

    def open(self, serial):
        """Select a drive by serial and open its page: a Home card item's
        target "Drives:<serial>" lands here. False if there's no such drive."""
        if self.data is None:
            self.load()
        for i, d in enumerate((self.data or {}).get("drives", [])):
            if d["serial"] == serial:
                self.table.scroll.index = i
                self.ctx.push(DriveScreen(self.ctx, self.env, serial))
                return True
        self.ctx.message(f"No drive {serial} here any more.", "dim")
        return False

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
        W = self.ctx.widgets
        if self.error:
            c.text(0, 0, W.fit(self.error, c.width - 1), "bad", bold=True)
            c.text(1, 0, "Press r to try again.", "dim")
            return
        if not self.data:
            return
        self.table.set_rows(self.rows(c.symbols))
        problems = self.data["problems"]
        d = self.current
        panel_top = c.height - self.PANEL_H if d else c.height
        room = max(0, panel_top - 1 - (len(self.data["drives"]) + 1) - 1)
        problems = problems[:room]
        table_h = max(3, panel_top - len(problems) - (1 if problems else 0))
        self.table.render(c.sub(0, 0, table_h, c.width))
        y = panel_top - len(problems)
        for p in problems:
            c.styled(y, 0, W.fit(p, c.width - 3), "reason")
            y += 1
        if d:
            mid = f" {c.symbols['mid']} "
            title = mid.join(x for x in (f"Drive {d['slot']}", d.get("model"), d["serial"]) if x)
            inner = W.panel(c, panel_top, 0, self.PANEL_H, c.width, title)
            kind, text, tone = panel_line(self.env()[0], d, self.data["now"], c.symbols["mid"])
            x = inner.status(0, 0, kind) + 1 if kind else 0
            inner.text(0, x, W.fit(text, inner.width - x), tone)
        self.actions.render(c)


class DriveScreen:
    """One drive's page, pushed by Enter on the list."""

    PANEL_H = 10

    def __init__(self, ctx, env, serial):
        self.ctx, self.env, self.serial = ctx, env, serial
        self.title = serial
        self.drive = None
        self.extra = None
        self.error = None
        self.now = ctx.now()
        self.history = ctx.widgets.Table([("Type", 9), ("Started", 17), ("Took", 7), ("Result", None)],
                                         selectable=False, empty="No tests on record.")
        self.actions = Actions(ctx, env, self.load)

    def keys(self):
        if self.actions.busy:
            return self.actions.keys()
        return self.actions.keys(self.drive) + [("r", "refresh"), ("↑↓", "history")]

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
            self.title = f"Drive {self.drive['slot']}"
        ss = self.env()[0]
        hist = (self.extra or {}).get("history")
        self.history.set_rows(history_rows(ss, hist, self.now.tzinfo, self.now) if hist else [])
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
        W = self.ctx.widgets
        if self.error:
            c.text(0, 0, W.fit(self.error, c.width - 1), "bad", bold=True)
            c.text(1, 0, "Press r to try again, Esc to go back.", "dim")
            return
        if not self.drive:
            return
        ss = self.env()[0]
        d = self.drive
        # Identity on the first line, the drive's state at its right.
        kind, word = state(d, self.now.date())
        right = len(word) + (2 if kind else 0)
        x = c.styled(0, 0, W.fit(d.get("model", "?"), c.width - right - 2), "strong")
        for part, role in ((d["serial"], "id"), (_size((d.get("disk") or {}).get("size")), "value")):
            if part and x + 3 + len(part) < c.width - right - 1:
                x = c.styled(0, x + 3, part, role)
        if kind:
            c.status(0, c.width - right, kind, word)
        else:
            c.styled(0, c.width - right, word, "label")
        # Schedule and Health, side by side.
        lw = (c.width - 2) // 2
        rw = c.width - lw - 2
        h = min(self.PANEL_H, max(4, c.height - 6))
        inner = W.panel(c, 2, 0, h, lw, "Schedule")
        W.KeyValue(schedule_rows(ss, d, self.now)).render(inner)
        self.render_health(W.panel(c, 2, lw + 2, h, rw, "Health"), d)
        # History fills the rest.
        y = 2 + h + 1
        W.heading(c, y, 0, "Test history (UniFi Drive)")
        self.history.render(c.sub(y + 1, 0, max(2, c.height - y - 1), c.width))
        self.actions.render(c)

    def render_health(self, c, d):
        W = self.ctx.widgets
        temp, hours, attrs = health_rows(d, self.extra)
        w = c.width

        def right(y, text, role="value", end=w):
            c.styled(y, max(0, end - len(text)), text, role)

        c.styled(0, 0, "Temperature", "label")
        right(0, f"{temp}{c.symbols['deg']}C" if temp is not None else "unknown",
              "value" if temp is not None else "label")
        c.styled(1, 0, "Power-on", "label")
        if isinstance(hours, int):
            right(1, f"{hours / 8766:.1f}y", "label")
            right(1, f"{hours:,} h", end=w - 6)
        else:
            right(1, "unknown", "label")
        if (self.extra or {}).get("attrs") is None:
            c.styled(3, 0, "SMART counts", "label")
            c.styled(4, 0, W.fit("unknown: no smartctl reading", w - 2), "note")
            return
        for i, (name, value, tone, rise, a) in enumerate(attrs):
            y = 3 + i
            c.styled(y, 0, W.fit(name, max(1, w - 10 - len(value))), "label")
            right(y, f"S{a}", "id")
            if rise:
                c.text(y, w - 8, f"{c.symbols['up']}{rise}", "dim")
                c.text(y, w - 9 - len(value), value, "bad", bold=True)
            else:
                c.text(y, w - 9 - len(value), value, tone)


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
