"""The Home dashboard (issue #9).

Built only on the plugin contract (docs/plugin-contract.md): each tool's
status(ctx) gives its badge, summary(ctx) its card (next test, last result,
next scrub, next backup window: whatever the tool reports), and alerts(ctx)
the active alerts at the top. The suite's update badge comes from
core.updates.cached_status(home), imported lazily; no module, an error or
None all mean "no update info yet".

Every row leads somewhere: up/down picks a row and Enter switches to its tab
(an alert to Alerts, the update line to Tools, a card row to the tab the item
names or else the tool's first own tab). `a` and `t` jump straight to Alerts
and Tools.
"""

import importlib
import os

from .. import plugins as P
from . import widgets as W
from .screens import state_tone, when

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEVEL_TONE = {"error": "bad", "warn": "warn", "info": "normal"}
MAX_ALERTS = 3


def update_status(home):
    """core.updates.cached_status(home), or None when there's no update info."""
    try:
        mod = importlib.import_module("..updates", __package__)
        info = mod.cached_status(home)
    except Exception:
        return None
    return info if isinstance(info, dict) else None


class Row:
    """One dashboard line: [(text, tone, bold)] segments and the tab it leads to."""

    def __init__(self, segments=(), target=None):
        self.segments = list(segments)
        self.target = target


def seg(text, tone="normal", bold=False):
    return (text, tone, bold)


class HomeScreen:
    title = "Home"

    def __init__(self, ctx):
        self.ctx = ctx
        self.rows = []
        self.index = 0      # into self.rows; always a row with a target when any has one
        self.top = 0

    # ------------------------------------------------------------ data

    @property
    def app(self):
        return self.ctx.app

    def home(self):
        home = getattr(self.app, "home", None)
        if home:
            return home
        if self.app.tools:
            return os.path.dirname(self.app.tools[0].path)
        return ROOT

    def call(self, name, label, check, fallback):
        """Call a plugin function for every tool; [(tool name, value, problems)]."""
        out = {}
        for tool_name, mod in self.app.plugins.items():
            tctx = self.app.contexts[tool_name]
            fn = getattr(mod, name)
            try:
                value = tctx.run_slow(lambda: fn(tctx), f"{label} {tool_name}")
                problems = check(value)
            except Exception as e:
                value, problems = None, [f"{name}: {type(e).__name__}: {e}"]
            out[tool_name] = (fallback if problems else value, problems)
        return out

    def tool_tab(self, tool_name):
        """Title of the tool's first own tab, or None."""
        for t in self.app.tabs:
            if any(ctx.tool is not None and ctx.tool.name == tool_name for _, ctx, _ in t.entries):
                return t.title
        return None

    def has_tab(self, title):
        return any(t.title == title for t in self.app.tabs)

    # ------------------------------------------------------------ building rows

    def show(self):
        """Read everything again: on opening the tab and on `r`."""
        statuses = self.call("status", "Checking", P.check_status, None)
        summaries = self.call("summary", "Reading", P.check_summary, None)
        alerts = self.call("alerts", "Reading alerts from", P.check_alerts, [])
        rows = self.update_rows(update_status(self.home()))
        rows.append(Row())
        rows += self.alert_rows(alerts)
        rows.append(Row())
        rows += self.tool_rows(statuses, summaries)
        self.set_rows(rows)

    def update_rows(self, info):
        head = seg("better-unas", "accent", True)
        if info is None:
            return [Row([head, seg("  Updates not checked yet.", "dim")], "Tools")]
        current = info.get("current") or "?"
        parts = [head, seg(f" {current}  ")]
        if info.get("available") and info.get("latest"):
            parts.append(seg(f"Update available: {info['latest']}", "warn", True))
        elif info.get("error"):
            parts.append(seg(f"Update check failed: {info['error']}", "warn"))
        else:
            parts.append(seg("Up to date", "good"))
        if info.get("checked_at"):
            parts.append(seg(f"  checked {when(info['checked_at'])}", "dim"))
        return [Row(parts, "Tools")]

    def alert_rows(self, by_tool):
        active, problems = [], []
        for name, (alerts, probs) in by_tool.items():
            active += [(name, a) for a in alerts if a["active"]]
            problems += [(name, p) for p in probs]
        active.sort(key=lambda na: na[1].get("since") or "", reverse=True)
        if not active and not problems:
            return [Row([seg("Alerts", "accent", True), seg("  No active alerts.", "good")], "Alerts")]
        n = len(active)
        rows = [Row([seg("Alerts", "accent", True),
                     seg(f"  {n} active", "bad" if n else "warn", bool(n))], "Alerts")]
        for name, a in active[:MAX_ALERTS]:
            rows.append(Row([seg(f"  {a['level']:<6}", LEVEL_TONE[a["level"]], a["level"] == "error"),
                             seg(f"{name}: ", "dim"), seg(a["message"])], "Alerts"))
        if n > MAX_ALERTS:
            rows.append(Row([seg(f"  ...and {n - MAX_ALERTS} more on the Alerts tab", "dim")], "Alerts"))
        for name, p in problems:
            rows.append(Row([seg("  error ", "bad", True), seg(f"{name}: ", "dim"),
                             seg(f"alerts could not be read ({p})", "bad")], "Tools"))
        return rows

    def tool_rows(self, statuses, summaries):
        rows = []
        if not self.app.tools and not self.app.broken:
            return [Row([seg("No tools are installed yet.", "dim")]),
                    Row([seg("Pick tools to install on the Tools tab (press t).", "normal")], "Tools")]
        for tool in self.app.tools:
            rows += self.card(tool, statuses, summaries)
            rows.append(Row())
        for name in self.app.broken:
            if name not in self.app.contexts:
                rows += [Row([seg(name, "normal", True), seg("  broken", "bad", True),
                              seg("  See the Tools tab.", "dim")], "Tools"), Row()]
        while rows and not rows[-1].segments:
            rows.pop()
        return rows

    def card(self, tool, statuses, summaries):
        name = tool.name
        head = [seg(name, "normal", True), seg(f"  {tool.manifest.get('VERSION', '')}", "dim")]
        if name in self.app.broken:
            return [Row(head + [seg("  broken", "bad", True), seg("  See the Tools tab.", "dim")], "Tools")]
        if name not in self.app.plugins:
            return [Row(head, "Tools"), Row([seg("  No status available.", "dim")], "Tools")]
        own_tab = self.tool_tab(name) or "Tools"
        status, s_probs = statuses[name]
        summary, sum_probs = summaries[name]
        if status is None:
            badge = seg("  error", "bad", True)
        elif not status["enabled"]:
            badge = seg("  off", "dim", True)
        else:
            badge = seg(f"  {status['state']}", state_tone(status["state"]), True)
        rows = []
        if summary is not None:
            rows.append(Row(head + [badge, seg(f"  {summary['headline']}", state_tone(summary["state"]))], own_tab))
            for it in summary.get("items", [])[:6]:
                tab = it.get("tab") if self.has_tab(it.get("tab")) else own_tab
                rows.append(Row([seg(f"  {it['label']:<16} ", "dim"),
                                 seg(it["value"], it.get("tone", "normal"))], tab))
        else:
            rows.append(Row(head + [badge], own_tab))
        if status is not None:
            rows += [Row([seg(f"  ! {p}", "warn")], "Tools") for p in status.get("problems", [])[:2]]
        rows += [Row([seg(f"  {p}", "bad")], "Tools") for p in s_probs + sum_probs]
        return rows

    # ------------------------------------------------------------ cursor

    def set_rows(self, rows):
        keep = self.index
        self.rows = rows
        targets = self.selectable()
        if not targets:
            self.index = 0
        elif keep not in targets:
            self.index = min(targets, key=lambda i: abs(i - keep))

    def selectable(self):
        return [i for i, r in enumerate(self.rows) if r.target]

    def move(self, step):
        targets = self.selectable()
        if not targets:
            return
        pos = targets.index(self.index) if self.index in targets else 0
        self.index = targets[max(0, min(len(targets) - 1, pos + step))]

    def jump(self, title):
        for i, t in enumerate(self.app.tabs):
            if t.title == title:
                self.app.switch(i)
                return True
        return False

    # ------------------------------------------------------------ screen protocol

    def keys(self):
        return [("↑↓", "select"), ("Enter", "open"), ("a", "alerts"), ("t", "tools"),
                ("r", "refresh")]

    def render(self, canvas):
        h = canvas.height
        targets = self.selectable()
        if self.index < self.top:
            self.top = self.index
        elif self.index >= self.top + h:
            self.top = self.index - h + 1
        self.top = max(0, min(self.top, max(0, len(self.rows) - h)))
        for y, row in enumerate(self.rows[self.top:self.top + h]):
            i = self.top + y
            chosen = i == self.index and i in targets
            x = 2
            if chosen:      # the selection role: reversed across the row, with the marker
                canvas.fill(y, 0, 1, canvas.width, tone="selected")
                canvas.text(y, 0, canvas.symbols["marker"], "selected", bold=True)
            for text, tone, bold in row.segments:
                room = canvas.width - x - 1
                if room <= 0:
                    break
                text = W.fit(text, room) if len(text) > room else text
                canvas.text(y, x, text, "selected" if chosen else tone, bold=bold)
                x += len(text)
        if self.top + h < len(self.rows):
            canvas.text(h - 1, canvas.width - 1, canvas.symbols["more"], "dim")

    def key(self, k):
        if k == "up":
            self.move(-1)
        elif k == "down":
            self.move(1)
        elif k == "home":
            self.move(-len(self.rows))
        elif k == "end":
            self.move(len(self.rows))
        elif k == "pgup":
            self.move(-10)
        elif k == "pgdn":
            self.move(10)
        elif k == "enter":
            if self.index in self.selectable():
                self.jump(self.rows[self.index].target)
        elif k == "a":
            self.jump("Alerts")
        elif k == "t":
            self.jump("Tools")
        elif k == "r":
            self.show()
        else:
            return False
        return True
