"""The Home dashboard (issue #9, docs/tui-style.md §5 and §10).

Built only on the plugin contract (docs/plugin-contract.md): each tool's
status(ctx) gives its card's state, summary(ctx) the card (headline and
items: next test, last result, next scrub, whatever the tool reports), and
alerts(ctx) the active alerts at the top. The suite's update notice comes
from core.updates.cached_status(home), imported lazily; no module, an error
or None all mean "no update info yet".

One stop per destination: the update notice (Settings › better-unas), the
alerts block (Alerts), and each tool's card (the Calendar when it has a
calendar hook, else its first own tab, or its page in Settings). Inside a card, an item is a stop of its own only when it names
a target the card doesn't already go to (e.g. "Last result" opening that
drive's page); its destination is shown dim at the right. up/down move
between stops, Enter opens, `a` jumps to Alerts, `r` reads everything again.
"""

import importlib
import os

from .. import plugins as P
from . import widgets as W
from .theme import STATUS

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAX_ALERTS = 3
LABEL_W = 14
LEVEL_KIND = {"error": "failed", "warn": "warning", "info": "ok"}
STATE_KIND = {"ok": ("ok", "ok"), "warn": ("warning", "warning"), "error": ("failed", "error")}


def update_status(home):
    """core.updates.cached_status(home), or None when there's no update info."""
    try:
        mod = importlib.import_module("..updates", __package__)
        info = mod.cached_status(home)
    except Exception:
        return None
    return info if isinstance(info, dict) else None


def item_target(item):
    """An item's target as "Tab" or "Tab:arg", or None ("tab" is the old name)."""
    t = item.get("target") or item.get("tab")
    return t if isinstance(t, str) and t else None


class Stop:
    """Somewhere Enter goes: target "Tab" or "Tab:arg", and how the action
    bar names it."""

    def __init__(self, target, label):
        self.target, self.label = target, label


class Line:
    """One row of the dashboard. draw(canvas, y, chosen) draws it; stop is the
    Stop the row belongs to (highlighted with it), card the card's own Stop
    for rows inside a card (its border turns accent when that is chosen)."""

    def __init__(self, draw, stop=None, card=None, lead=False):
        self.draw, self.stop, self.card, self.lead = draw, stop, card, lead


def blank():
    return Line(lambda c, y, chosen: None)


class HomeScreen:
    title = "Home"

    def __init__(self, ctx):
        self.ctx = ctx
        self.lines = []
        self.stops = []
        self.index = 0      # into self.stops
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
        """Call a plugin function for every tool: {tool name: (value, problems)}."""
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
        """The Calendar when the tool has a calendar hook, else the title of
        its first own tab, or None."""
        if callable(getattr(self.app.plugins.get(tool_name), "calendar", None)) and self.has_tab("Calendar"):
            return "Calendar"
        for t in self.app.tabs:
            if any(ctx.tool is not None and ctx.tool.name == tool_name for _, ctx, _ in t.entries):
                return t.title
        return None

    def has_tab(self, target):
        title = target.partition(":")[0]
        return any(t.title == title for t in self.app.tabs)

    @property
    def selected(self):
        return self.stops[self.index] if self.stops else None

    # ------------------------------------------------------------ building

    def show(self):
        """Read everything again: on opening the tab and on `r`."""
        statuses = self.call("status", "Checking", P.check_status, None)
        summaries = self.call("summary", "Reading", P.check_summary, None)
        alerts = self.call("alerts", "Reading alerts from", P.check_alerts, [])
        keep = self.selected.target if self.selected else None
        self.lines, self.stops = [], []
        for block in (self.update_block(update_status(self.home())), self.alerts_block(alerts),
                      *self.tool_blocks(statuses, summaries)):
            if not block:
                continue
            if self.lines:
                self.lines.append(blank())
            self.lines += block
        targets = [s.target for s in self.stops]
        self.index = targets.index(keep) if keep in targets else 0

    def add_stop(self, target, label):
        stop = Stop(target, label)
        self.stops.append(stop)
        return stop

    def update_block(self, info):
        """The update notice, only when an update is known (the installed
        version is always in the tab bar)."""
        if not info or not info.get("available") or not info.get("latest"):
            return []
        latest = info["latest"]
        if info.get("branch") and info.get("latest_ref"):
            latest = f"new commit {info['latest_ref'][:7]}"
        stop = self.add_stop("Settings:better-unas", "open Settings › better-unas")
        text = f"Update available: {latest}"

        def draw(c, y, chosen):
            x = c.status(y, 2, "update", "") + 1
            c.text(y, x, W.fit(text, c.width - x - 1), "selected" if chosen else "warn", bold=True)
            hint = "Settings › better-unas"
            if x + len(text) + len(hint) + 4 < c.width:
                c.text(y, c.width - len(hint) - 1, hint, "selected" if chosen else "dim")
        return [Line(draw, stop, lead=True)]

    def alerts_block(self, by_tool):
        """The active alerts as one stop that opens the Alerts tab."""
        active, problems = [], []
        for name, (alerts, probs) in by_tool.items():
            active += [(name, a) for a in alerts if a["active"]]
            problems += [(name, p) for p in probs]
        active.sort(key=lambda na: na[1].get("since") or "", reverse=True)
        stop = self.add_stop("Alerts", "open Alerts")
        n = len(active)

        def head(c, y, chosen):
            if n:
                x = c.status(y, 2, "warning" if all(a["level"] != "error" for _, a in active) else "failed", "")
                c.text(y, x + 1, f"{n} active alert{'s' if n != 1 else ''}", "selected" if chosen else "normal",
                       bold=True)
            else:
                x = c.status(y, 2, "ok", "")
                c.text(y, x + 1, "No active alerts.", "selected" if chosen else "normal")
        lines = [Line(head, stop, lead=True)]
        for name, a in active[:MAX_ALERTS]:
            lines.append(Line(self.alert_row(name, a), stop))
        if n > MAX_ALERTS:
            more = f"…and {n - MAX_ALERTS} more on the Alerts tab"
            lines.append(Line(lambda c, y, chosen: c.text(y, 4, more, "selected" if chosen else "dim"), stop))
        for name, p in problems:
            msg = f"{name}: alerts could not be read ({p})"
            lines.append(Line(lambda c, y, chosen, msg=msg: (
                c.status(y, 4, "failed", ""),
                c.text(y, 6, W.fit(msg, c.width - 7), "selected" if chosen else "bad")), stop))
        return lines

    def alert_row(self, name, a):
        from .alerts import short
        since = short(a.get("since"))

        def draw(c, y, chosen):
            x = c.status(y, 4, LEVEL_KIND[a["level"]], "") + 1
            room = c.width - x - len(since) - 3
            c.text(y, x, W.fit(f"{name}: {a['message']}", max(0, room)), "selected" if chosen else "normal")
            c.text(y, c.width - len(since) - 1, since, "selected" if chosen else "dim")
        return draw

    def tool_blocks(self, statuses, summaries):
        if not self.app.tools and not self.app.broken:
            stop = self.add_stop("Settings", "open Settings")
            return [[Line(lambda c, y, chosen: c.text(y, 2, "No tools are installed yet.", "dim")),
                     Line(lambda c, y, chosen: c.text(y, 2, "Install tools in Settings.",
                                                      "selected" if chosen else "normal"), stop, lead=True)]]
        blocks = [self.card(tool, statuses, summaries) for tool in self.app.tools]
        for name in self.app.broken:
            if name not in self.app.contexts:
                blocks.append(self.broken_card(name, None))
        return blocks

    # ------------------------------------------------------------ cards

    def card_lines(self, title, state, rows, stop):
        """A card drawn as a panel, line by line so it scrolls: the tool's
        name in the top border, its state at the right of it, then rows of
        (draw(c, y, x, w, chosen), own stop or None)."""
        def top(c, y, chosen, card_chosen=False):
            tone = "accent" if card_chosen else "dim"
            s = c.symbols
            c.text(y, 1, s["tl"] + s["h"] * (c.width - 3) + s["tr"], tone)
            name = f" {W.fit(title, c.width - 24)} "
            c.text(y, 3, name, "selected" if card_chosen else "accent", bold=True)
            if state:
                kind, word = state
                text = f" {c.symbols[kind]} {word} " if kind else f" {word} "
                x = c.width - len(text) - 3
                if kind:
                    c.text(y, x, " ", tone)
                    c.status(y, x + 1, kind, word)
                    c.text(y, x + len(text) - 1, " ", tone)
                else:
                    c.text(y, x, text, "dim")

        def side(draw, own):
            def line(c, y, chosen, card_chosen=False):
                tone = "accent" if card_chosen else "dim"
                c.text(y, 1, c.symbols["v"], tone)
                c.text(y, c.width - 1, c.symbols["v"], tone)
                inner = c.sub(y, 3, 1, c.width - 5)
                if chosen and own is not None:
                    inner.fill(0, 0, 1, inner.width, tone="selected")
                draw(inner, 0, 0, inner.width, chosen and own is not None)
            return line

        def bottom(c, y, chosen, card_chosen=False):
            s = c.symbols
            c.text(y, 1, s["bl"] + s["h"] * (c.width - 3) + s["br"], "accent" if card_chosen else "dim")

        lines = [Line(top, stop, card=stop, lead=True)]
        for draw, own in rows:
            lines.append(Line(side(draw, own), own or stop, card=stop, lead=own is not None))
        lines.append(Line(bottom, stop, card=stop))
        return lines

    def card(self, tool, statuses, summaries):
        name = tool.name
        if name in self.app.broken:
            return self.broken_card(name, tool)
        if name not in self.app.plugins:
            stop = self.add_stop(f"Settings:{name}", f"open Settings › {name}")
            return self.card_lines(name, None, [(text_row("No status available.", "dim"), None)], stop)
        own = self.tool_tab(name) or f"Settings:{name}"
        stop = self.add_stop(own, f"open {own.replace(':', ' › ')}")
        status, s_probs = statuses[name]
        summary, sum_probs = summaries[name]
        if status is None:
            state = ("failed", "error")
        elif not status["enabled"]:
            state = (None, "off")
        else:
            state = STATE_KIND.get(status["state"], (None, status["state"]))
        rows = []
        seen = {own}            # one stop per destination: a repeat target isn't a new stop
        if summary is not None:
            rows.append((text_row(summary["headline"], "normal", bold=True), None))
            for it in summary.get("items", [])[:6]:
                target = item_target(it)
                item_stop = None
                hint = None
                if target and target not in seen and self.has_tab(target):
                    seen.add(target)
                    hint = it.get("target_label") or target.partition(":")[0]
                    item_stop = Stop(target, f"open {hint}")
                rows.append((item_row(it, hint), item_stop))
        if status is not None:
            rows += [(reason_row(p), None) for p in status.get("problems", [])[:2]]
        rows += [(text_row(p, "bad"), None) for p in s_probs + sum_probs]
        if not rows:
            rows.append((text_row("Nothing to report.", "dim"), None))
        lines = self.card_lines(f"{name} {tool.manifest.get('VERSION', '')}".strip(), state, rows, stop)
        # item stops come after the card's own, in reading order
        at = self.stops.index(stop) + 1
        for ln in lines:
            if ln.stop is not stop and ln.stop not in self.stops:
                self.stops.insert(at, ln.stop)
                at += 1
        return lines

    def broken_card(self, name, tool):
        stop = self.add_stop(f"Settings:{name}", f"open Settings › {name}")
        title = f"{name} {tool.manifest.get('VERSION', '')}".strip() if tool else name
        return self.card_lines(title, ("failed", "broken"),
                               [(text_row("It couldn't be loaded. See Settings.", "dim"), None)], stop)

    # ------------------------------------------------------------ cursor

    def move(self, step):
        if self.stops:
            self.index = max(0, min(len(self.stops) - 1, self.index + step))

    def jump(self, target):
        """Switch to a tab by title; "Tab:arg" also opens arg there through
        the tab's screen's open(arg) ("Settings:better-unas", "Drives:SERIAL4")."""
        title, _, page = target.partition(":")
        for i, t in enumerate(self.app.tabs):
            if t.title == title:
                if page and t.stack:
                    del t.stack[1:]
                self.app.switch(i)
                if page and self.app.current == i:
                    opener = getattr(self.app.screen, "open", None)
                    if opener:
                        opener(page)
                return True
        return False

    # ------------------------------------------------------------ screen protocol

    def keys(self):
        ks = [("Enter", "open")] if self.selected else []   # where it goes: the stop's › hint
        return ks + [("a", "alerts"), ("r", "refresh"), ("↑↓", "move")]

    def render(self, canvas):
        h = canvas.height
        sel = self.selected
        rows = [i for i, ln in enumerate(self.lines) if ln.stop is sel]
        if rows:
            first, last = rows[0], rows[-1]
            if first < self.top:
                self.top = first
            elif last >= self.top + h:
                self.top = max(first if last - first >= h else last - h + 1, 0)
        self.top = max(0, min(self.top, max(0, len(self.lines) - h)))
        for y, ln in enumerate(self.lines[self.top:self.top + h]):
            chosen = sel is not None and ln.stop is sel
            if ln.card is not None:
                ln.draw(canvas, y, chosen, card_chosen=sel is ln.card)
            else:
                if chosen:          # a plain stop: reversed across the row
                    canvas.fill(y, 0, 1, canvas.width, tone="selected")
                ln.draw(canvas, y, chosen)
            if chosen and ln.lead:
                canvas.text(y, 0, canvas.symbols["marker"], "accent" if ln.card is not None else "selected",
                            bold=True)
        if self.top + h < len(self.lines):
            canvas.text(h - 1, canvas.width - 1, canvas.symbols["more"], "dim")

    def key(self, k):
        if k == "up":
            self.move(-1)
        elif k == "down":
            self.move(1)
        elif k == "home":
            self.move(-len(self.stops))
        elif k == "end":
            self.move(len(self.stops))
        elif k == "pgup":
            self.move(-5)
        elif k == "pgdn":
            self.move(5)
        elif k == "enter":
            if self.selected:
                self.jump(self.selected.target)
        elif k == "a":
            self.jump("Alerts")
        elif k == "r":
            self.app.forget_cache()          # read every tool live again
            self.show()
        else:
            return False
        return True


# ------------------------------------------------------------ card rows

def text_row(text, tone="normal", bold=False):
    def draw(c, y, x, w, chosen):
        c.text(y, x, W.fit(text, w), "selected" if chosen else tone, bold=bold)
    return draw


def reason_row(text):
    def draw(c, y, x, w, chosen):
        c.styled(y, x, W.fit(text, w - 2), "reason")
    return draw


def item_row(it, hint=None):
    """label, then [glyph] value in the item's tone, then `› hint` dim at the
    right when the item opens somewhere of its own."""
    def draw(c, y, x, w, chosen):
        c.text(y, x, W.pad(it["label"], LABEL_W - 1), "selected" if chosen else "dim")
        vx = x + LABEL_W
        tail = f"{c.symbols['marker']} {hint}" if hint else ""
        room = w - LABEL_W - (len(tail) + 2 if tail else 0)
        kind = it.get("status")
        if kind in STATUS:
            vx = c.status(y, vx, kind, "") + 1
            room -= 2
        c.text(y, vx, W.fit(it["value"], max(0, room)), "selected" if chosen else it.get("tone", "normal"))
        if tail:
            c.text(y, x + w - len(tail), tail, "selected" if chosen else "dim")
    return draw

