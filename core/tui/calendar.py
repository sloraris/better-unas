"""The core's Calendar tab: one month grid for every tool with a calendar hook.

Each tool's plugin returns its days through `calendar(ctx)`
(docs/plugin-contract.md §4); the core merges them and draws the month as a
full box grid (docs/tui-style.md §10): lines between every day and every
week, two lines per day. The first line holds the day number and the day's
marks (scrubs, backups), the second its chips (a status glyph and a short
label, like a drive's slot). The selected day gets heavy side bars and
today's number is reversed. Enter opens the day's details.

Everything is read again each time the screen opens and on `r`. Standard
library only.
"""

import calendar
import datetime as dt

from .. import plugins as P
from .theme import STATUS

DAY = dt.timedelta(days=1)
WEEK_ROWS = 3         # a border line, the day line, the chip line
MAX_CHIPS = 3


def _fit(s, width, more="…"):
    s = str(s)
    return s if len(s) <= width else s[: max(0, width - 1)] + more


def _date_y(d, today):
    """'Sun 21 Feb', with the year outside today's."""
    return f"{d:%a %d %b}" + (f" {d.year}" if d.year != today.year else "")


def shift_month(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


# --------------------------------------------------------------------------- data

def gather(sources, now):
    """Read every source and merge them.

    sources: [(tool name, read)] where read() returns the tool's calendar(ctx).
    Returns {today, until, until_note, legend, days: {date: [entry]},
    problems, error}; error is set only when no source had anything to show.
    """
    out = {"today": None, "until": None, "until_note": "", "legend": [], "days": {}, "problems": [],
           "error": None}
    errors, shown, many = [], 0, len(sources) > 1
    for name, read in sources:
        tag = f"{name}: " if many else ""
        try:
            c = read()
        except Exception as e:
            errors.append(f"{tag}{str(e) or type(e).__name__}")
            continue
        problems = P.check_calendar(c)
        if problems:
            errors.append(f"{name}: " + "; ".join(problems[:3]))
            continue
        if c.get("error"):
            errors.append(tag + c["error"])
            continue
        shown += 1
        if out["today"] is None and c.get("today"):
            out["today"] = c["today"]
        if out["until"] is None and c.get("until"):
            out["until"], out["until_note"] = c["until"], c.get("until_note", "")
        for kind, label in c.get("legend", []):
            if kind not in [k for k, _ in out["legend"]]:
                out["legend"].append((kind, label))
        for d, entries in c["days"].items():
            out["days"].setdefault(d, []).extend(dict(e, tool=name) for e in entries)
        out["problems"] += [tag + p for p in c.get("problems", [])]
    if not shown and errors:
        out["error"] = "\n".join(errors)
    else:
        out["problems"] = errors + out["problems"]
    out["today"] = out["today"] or now.date()
    return out


def chips(entries):
    return [e for e in entries if e.get("place") == "chip"]


def marks(entries):
    """The status kinds of the day's marks, in order, once each."""
    out = []
    for e in entries:
        if e.get("place") == "mark" and e["status"] not in out:
            out.append(e["status"])
    return out


def entry_row(e):
    """A day panel row: ("status", kind, text, past), ("reason", text) or ("label", text)."""
    if e.get("status") is None:
        return ("label", e["text"])
    if e["status"] == "warning":
        return ("reason", e["text"])
    return ("status", e["status"], e["text"], bool(e.get("past")))


def empty_text(data, d):
    if data["until"] is not None and d > data["until"]:
        return f"Beyond the plan, which ends {_date_y(data['until'], data['today'])}."
    if d < data["today"]:
        return "Nothing on record."
    return "Nothing planned."


def day_rows(data, d):
    rows = [entry_row(e) for e in data["days"].get(d, [])]
    return rows or [("label", empty_text(data, d))]


def detail_rows(data, d):
    """The day details: ("heading", text), rows as in day_rows with their
    ("note", text) lines, and ("",) between sections."""
    sections = {}
    for e in data["days"].get(d, []):
        sections.setdefault(e.get("section") or "", []).append(e)
    out = []
    for heading, entries in sections.items():
        if heading:
            out.append(("heading", heading))
        for e in entries:
            row = entry_row(e)
            if e.get("detail"):
                row = ((row[0], row[1], e["detail"], row[3]) if row[0] == "status"
                       else ("reason", e["detail"]) if row[0] == "reason" else ("value", e["detail"]))
            out.append(row)
            out += [("note", n) for n in e.get("notes", [])]
        out.append(("",))
    if data["until"] is not None and d > data["until"]:
        out.append(("label", data["until_note"] or f"The plan stops on {_date_y(data['until'], data['today'])}."))
        out.append(("",))
    elif not out:
        out.append(("label", "Nothing on this day."))
        out.append(("",))
    if data["problems"]:
        out.append(("heading", "Problems"))
        out += [("reason", p) for p in data["problems"]]
    while out and out[-1] == ("",):
        out.pop()
    return out


# --------------------------------------------------------------------------- drawing

def draw_row(c, y, x, row, width):
    """One day_rows / detail_rows row at (y, x): ("status", kind, text, past),
    ("reason" / "note" / "heading" / "value" / "label", text)."""
    def fit(s, w):
        return _fit(s, w, c.symbols["more"])
    if row[0] == "status":
        end = c.status(y, x, row[1]) + 1
        c.text(y, end, fit(row[2], width - (end - x)), "dim" if row[3] else "normal")
    elif row[0] == "reason":
        c.styled(y, x, fit(row[1], width - 2), "reason")
    elif row[0] == "note":
        c.styled(y, x + 2, fit(row[1], width - 4), "note")
    elif row[0] == "heading":
        c.styled(y, x, fit(row[1], width), "heading")
    elif len(row) == 2:            # ("value" or "label", text)
        c.styled(y, x, fit(row[1], width), row[0])


class CalendarScreen:
    title = "Calendar"

    def __init__(self, ctx, sources):
        """sources: [(tool name, read)]; read() returns that tool's calendar(ctx)."""
        self.ctx = ctx
        self.sources = sources
        self.data = None
        self.sel = None

    def keys(self):
        return [("Enter", "open"), ("t", "today"), ("r", "recalculate"),
                ("←→↑↓", "day"), ("PgUp/Dn", "month")]

    def show(self):
        self.data = self.ctx.run_slow(lambda: gather(self.sources, self.ctx.now()), "Reading the calendar")
        if self.sel is None:
            self.sel = self.data["today"]

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
            self.sel = self.data["today"]
        elif k == "r":
            self.ctx.app.forget_cache()      # recalculate from live reads, not the shared cache
            self.show()
        elif k == "enter" and not self.data["error"]:
            self.ctx.push(DayScreen(self.ctx, self.data, self.sel))
        else:
            return False
        return True

    # ------------------------------------------------------------ drawing

    def render(self, c):
        if self.sel is None:
            return
        sym, data = c.symbols, self.data
        month = dt.date(self.sel.year, self.sel.month, 1)
        c.styled(0, 0, f"{sym['left']} {month:%B %Y} {sym['right']}", "heading")
        if data["error"]:
            lines = data["error"].split("\n")
            for i, line in enumerate(lines):
                c.text(2 + i, 0, _fit(line, c.width - 1), "bad", bold=True)
            c.text(3 + len(lines), 0, "Press r to try again.", "dim")
            return
        right = f"planned through {_date_y(data['until'], data['today'])}" if data["until"] else ""
        x = c.width - len(right)
        if right:
            c.styled(0, x, right, "label")
        if data["problems"]:
            n = len(data["problems"])
            c.styled(0, x - 3 - len(f"{n} problem{'s' * (n != 1)}") - 2,
                     f"{n} problem{'s' * (n != 1)}", "reason")

        weeks = calendar.Calendar().monthdatescalendar(month.year, month.month)
        cw = max(8, (c.width - 1) // 7)
        self.grid(c, 1, weeks, cw, month)
        top = 1 + len(weeks) * WEEK_ROWS + 1
        if c.height - top >= 2:
            self.panel(c.sub(top, 0, c.height - top, c.width))

    def grid(self, c, top, weeks, cw, month):
        sym = c.symbols
        right = 7 * cw
        for w in range(len(weeks) + 1):
            y = top + w * WEEK_ROWS
            a, j, b = (("tl", "tt", "tr") if w == 0 else
                       ("bl", "bt", "br") if w == len(weeks) else ("lt", "x", "rt"))
            line = sym[a] + (sym["h"] * (cw - 1) + sym[j]) * 6 + sym["h"] * (cw - 1) + sym[b]
            c.text(y, 0, line, "dim")
        for i, name in enumerate(calendar.day_abbr):
            c.text(top, i * cw + 2, f" {name} ", "dim")
        bottom = top + len(weeks) * WEEK_ROWS
        x = 2
        for kind, label in self.data["legend"]:
            item = f" {sym[kind]} {label} "
            if x + len(item) + 1 >= right:
                break
            c.text(bottom, x, item, "dim")
            x += len(item) + 1
        for w, week in enumerate(weeks):
            y = top + w * WEEK_ROWS + 1
            for i in range(8):
                for dy in (0, 1):
                    c.text(y + dy, i * cw, sym["v"], "dim")
            for i, d in enumerate(week):
                if d.month != month.month:
                    continue
                if d == self.sel:
                    for dy in (0, 1):
                        c.text(y + dy, i * cw, sym["vh"], "normal", bold=True)
                        c.text(y + dy, (i + 1) * cw, sym["vh"], "normal", bold=True)
                self.cell(c.sub(y, i * cw + 1, 2, cw - 1), d)

    def cell(self, c, d):
        data = self.data
        entries = data["days"].get(d, [])
        past = d < data["today"]
        today = d == data["today"]
        chosen = d == self.sel
        c.text(0, 1, f"{d.day:>2}", "selected" if today else ("dim" if past else "normal"), bold=chosen)
        x = 4
        for kind in marks(entries):
            c.text(0, x, c.symbols[kind], "dim" if past else STATUS[kind])
            x += 2
        if today and x + 5 <= c.width:
            c.text(0, x, "today", "accent")
        items = chips(entries)
        if not items:
            if not past and any(e.get("place") == "flag" for e in entries):
                c.text(1, 1, c.symbols["warning"], "warn")
            return
        x = 1
        for i, e in enumerate(items):
            left = len(items) - i
            text = f"{c.symbols[e['status']]}{e['chip']}"
            more = f"+{left}"
            if i == MAX_CHIPS or x + len(text) > c.width or (left > 1 and x + len(text) + 1 + len(more) > c.width):
                c.text(1, x, more, "dim")
                return
            dim = e.get("past") and e["status"] != "failed"
            c.text(1, x, text, "dim" if dim else STATUS[e["status"]], bold=e["status"] == "failed")
            x += len(text) + 1

    def panel(self, c):
        """The selected day under the grid, when the month leaves room."""
        data = self.data
        head = f"{self.sel:%A %d %B}" + (f" {self.sel.year}" if self.sel.year != data["today"].year else "")
        end = c.styled(0, 0, head, "heading")
        if self.sel == data["today"]:
            c.styled(0, end + 2, "today", "tag")
        rows = day_rows(data, self.sel) + [("reason", p) for p in data["problems"]]
        room = c.height - 1
        if len(rows) > room:
            rows = rows[: max(0, room - 1)] + [("label", f"{len(rows) - room + 1} more: Enter for day details")]
        for i, row in enumerate(rows):
            draw_row(c, 1 + i, 2, row, c.width - 3)


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
                indent, text = 0, row[1]
            parts = wrap(text, max(10, width - indent)) or [""]
            first = (row[0], row[1], parts[0], row[3]) if row[0] == "status" else (row[0], parts[0])
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


# --------------------------------------------------------------------------- wiring

def sources_for(plugins, contexts):
    """[(tool name, read)] for every loaded plugin with a calendar hook."""
    out = []
    for name, mod in plugins.items():
        if callable(getattr(mod, "calendar", None)):
            ctx = contexts[name]
            out.append((name, lambda mod=mod, ctx=ctx: mod.calendar(ctx)))
    return out


def preview(ctx, plugin):
    """A Calendar of one tool planned with ctx's (unsaved) values, for the
    Settings screen's `p`."""
    screen = CalendarScreen(ctx, [(ctx.tool.name, lambda: plugin.calendar(ctx))])
    screen.title = "Calendar preview (unsaved settings)"
    return screen
