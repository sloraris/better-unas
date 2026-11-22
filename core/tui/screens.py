"""The core's own screens.

Home lives in home.py (#9), Settings in settings.py (#12), Alerts in
alerts.py (#13) and Tools in tools.py (#14). The small helpers (detail, help,
chooser, error, too-small) stay.
"""

import datetime as dt

from . import widgets as W


def state_tone(state):
    return {"ok": "good", "warn": "warn", "error": "bad", "off": "dim"}.get(state, "dim")


def render_too_small(canvas, min_w, min_h):
    lines = [
        ("Please widen the terminal.", "warn"),
        (f"better-unas needs at least {min_w}x{min_h}; this one is {canvas.width}x{canvas.height}.", "normal"),
        ("Make the window bigger, or press q to quit.", "dim"),
    ]
    rows = []
    for text, tone in lines:
        rows += [(line, tone) for line in W.wrap(text, max(1, canvas.width - 2))]
    y0 = max(0, (canvas.height - len(rows)) // 2)
    for i, (line, tone) in enumerate(rows):
        canvas.text(y0 + i, max(0, (canvas.width - len(line)) // 2), line, tone, bold=tone == "warn")


def when(iso):
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return str(iso or "")
    return f"{t:%a %d %b %H:%M}"


class DetailScreen:
    """Lines of text that scroll: drill-down details, problem lists."""

    def __init__(self, title, lines):
        self.title = title
        self.text = W.ScrollText(lines)

    def keys(self):
        return self.text.keys()

    def render(self, canvas):
        self.text.render(canvas)

    def key(self, k):
        return self.text.key(k)


class HelpScreen(DetailScreen):
    """The keys of the screen underneath, the keys everywhere, and what each
    status glyph and color means (docs/tui-style.md §3). The legend is built at
    the first draw, when the canvas says whether glyphs are Unicode or ASCII."""

    STATUSES = (("ok", "good, done, passed"), ("planned", "planned"),
                ("warning", "warning, reason"), ("failed", "failed, alert"),
                ("running", "running"), ("scrub", "scrub"), ("backup", "backup window"),
                ("update", "update available"))

    def __init__(self, ctx, screen_keys, core_keys):
        self.theme = ctx.theme
        self.screen_keys, self.core_keys = screen_keys, core_keys
        super().__init__("Help", self.lines(None))
        self.built = False

    def lines(self, symbols):
        from .theme import STATUS
        lines = [("Keys on this screen", "accent")]
        lines += [f"  {k:<12} {lbl}" for k, lbl in self.screen_keys] or [("  (none)", "dim")]
        lines += ["", ("Keys everywhere", "accent")]
        lines += [f"  {k:<12} {lbl}" for k, lbl in self.core_keys]
        lines += ["  Enter        open the selected item", "  Esc          go back", ""]
        lines += [("Status marks", "accent")]
        for kind, meaning in self.STATUSES:
            glyph = symbols[kind] if symbols else " "
            tone = STATUS[kind]
            lines.append((f"  {glyph}  {meaning:<22} ({self.theme.describe(tone)})", tone))
        if not self.theme.color:
            lines.append(("  Colors are off (NO_COLOR is set or the terminal has none);", "dim"))
            lines.append(("  the marks carry the meaning.", "dim"))
        return lines

    def render(self, canvas):
        if not self.built:
            self.text = W.ScrollText(self.lines(canvas.symbols))
            self.built = True
        super().render(canvas)


class ErrorScreen:
    def __init__(self, title, exc):
        self.title = title
        self.msg = f"{type(exc).__name__}: {exc}"

    def keys(self):
        return []

    def render(self, canvas):
        canvas.text(0, 0, "This screen could not be opened.", "bad", bold=True)
        for i, line in enumerate(W.wrap(self.msg, canvas.width - 2)):
            canvas.text(2 + i, 0, line)
        canvas.text(canvas.height - 1, 0, "The rest of better-unas keeps working; see the Tools tab.", "dim")

    def key(self, k):
        return False


class ChooserScreen:
    """A tab that two tools both asked for: pick which tool's screen to open."""

    def __init__(self, ctx, tab):
        self.ctx, self.tab = ctx, tab
        self.title = tab.title
        self.list = W.ListView([label for label, _, _ in tab.entries])

    def keys(self):
        return [("Enter", "open")] + self.list.keys()

    def render(self, canvas):
        canvas.text(0, 0, f"More than one tool has a {self.title} screen.", "dim")
        self.list.render(canvas.sub(2, 0, canvas.height - 2, canvas.width))

    def key(self, k):
        if k == "enter":
            app = self.ctx.app
            label, ctx, make = self.tab.entries[self.list.index]
            self.ctx.push(app.make(self.tab, label, ctx, make))
            return True
        return self.list.key(k)
