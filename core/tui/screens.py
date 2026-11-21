"""The core's own screens.

Home, Settings and Alerts here are first versions built only on the
plugin contract; #9, #12 and #13 grow them into the real thing. The Tools
screen lives in tools.py (#14). The small helpers (detail, help, chooser, error, too-small) stay.
"""

import datetime as dt

from .. import plugins as P
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
    def __init__(self, ctx, screen_keys, core_keys):
        theme = ctx.theme
        lines = [("Keys on this screen", "accent")]
        lines += [f"  {k:<12} {lbl}" for k, lbl in screen_keys] or [("  (none)", "dim")]
        lines += ["", ("Keys everywhere", "accent")]
        lines += [f"  {k:<12} {lbl}" for k, lbl in core_keys]
        lines += ["  Enter        open the selected item", "  Esc          go back", ""]
        lines += [("Colors", "accent")]
        for tone, meaning in (("good", "good, done, passed"), ("warn", "planned, warning"),
                              ("bad", "failed, alert"), ("dim", "past, disabled")):
            lines.append((f"  {meaning:<24} ({theme.describe(tone)})", tone))
        if not theme.color:
            lines.append(("  Colors are off (NO_COLOR is set or the terminal has none).", "dim"))
        super().__init__("Help", lines)


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


class HomeScreen:
    """Every tool's card from summary(ctx), read when the tab opens."""

    title = "Home"

    def __init__(self, ctx):
        self.ctx = ctx
        self.text = W.ScrollText([])

    def keys(self):
        return [("r", "refresh")] + self.text.keys()

    def show(self):
        app = self.ctx.app
        lines = []
        for tool in app.tools:
            mod = app.plugins.get(tool.name)
            head = f"{tool.name}  {tool.manifest.get('VERSION', '')}"
            if tool.name in app.broken:
                lines += [(f"{head}  [broken]", "bad"), ("  See the Tools tab.", "dim"), ""]
                continue
            if mod is None:
                lines += [(head, "normal"), ("  No status available.", "dim"), ""]
                continue
            tctx = app.contexts[tool.name]
            try:
                s = tctx.run_slow(lambda: mod.summary(tctx), f"Reading {tool.name}")
                problems = P.check_summary(s)
            except Exception as e:
                s, problems = None, [f"{type(e).__name__}: {e}"]
            if problems:
                lines += [(f"{head}  [error]", "bad")] + [(f"  {p}", "bad") for p in problems] + [""]
                continue
            lines.append((f"{head}  [{s['state']}]  {s['headline']}", state_tone(s["state"])))
            for it in s.get("items", [])[:6]:
                lines.append((f"  {it['label']:<16} {it['value']}", it.get("tone", "normal")))
            lines.append("")
        if not app.tools:
            lines = [("No tools are installed yet.", "dim")]
        self.text.set_text(lines)

    def render(self, canvas):
        self.text.render(canvas)

    def key(self, k):
        if k == "r":
            self.show()
            return True
        return self.text.key(k)


class AlertsScreen:
    """Every tool's alerts merged: active first, newest first."""

    title = "Alerts"
    LEVEL_TONE = {"error": "bad", "warn": "warn", "info": "normal"}

    def __init__(self, ctx):
        self.ctx = ctx
        self.alerts = []
        self.table = W.Table([("Level", 7), ("Tool", 16), ("Since", 16), ("Message", None)],
                             empty="No alerts.")

    def keys(self):
        return [("Enter", "details"), ("r", "refresh")] + self.table.keys()

    def show(self):
        app = self.ctx.app
        found = []
        for name, mod in app.plugins.items():
            tctx = app.contexts[name]
            try:
                alerts = tctx.run_slow(lambda: mod.alerts(tctx), f"Reading {name}")
                problems = P.check_alerts(alerts)
            except Exception as e:
                alerts, problems = [], [f"{type(e).__name__}: {e}"]
            if problems:
                found.append((name, {"level": "error", "active": True, "since": "",
                                     "message": "; ".join(problems), "id": "plugin"}))
            found += [(name, a) for a in alerts]
        found.sort(key=lambda na: na[1].get("since") or "", reverse=True)
        found.sort(key=lambda na: not na[1]["active"])
        self.alerts = found
        self.table.set_rows([
            [(a["level"] if a["active"] else "past", self.LEVEL_TONE[a["level"]] if a["active"] else "dim"),
             name, when(a["since"]), (a["message"], "normal" if a["active"] else "dim")]
            for name, a in found])

    def render(self, canvas):
        active = sum(1 for _, a in self.alerts if a["active"])
        canvas.text(0, 0, f"{active} active, {len(self.alerts) - active} recent", "bad" if active else "dim")
        self.table.render(canvas.sub(2, 0, canvas.height - 2, canvas.width))

    def key(self, k):
        if k == "enter" and self.alerts:
            name, a = self.alerts[self.table.index]
            lines = [(a["message"], self.LEVEL_TONE[a["level"]]), "",
                     f"Tool:     {name}", f"Level:    {a['level']}",
                     f"Active:   {'yes' if a['active'] else 'no (resolved)'}",
                     f"Since:    {when(a['since'])}"]
            if a.get("last"):
                lines.append(f"Last:     {when(a['last'])}")
            self.ctx.push(DetailScreen(f"Alert {a['id']}", lines))
            return True
        if k == "r":
            self.show()
            return True
        return self.table.key(k)


class SettingsScreen:
    """Pick a tool to see its settings form. Saving arrives with #12."""

    title = "Settings"

    def __init__(self, ctx):
        self.ctx = ctx
        app = ctx.app
        self.tools = [t for t in app.tools if t.schema]
        self.list = W.ListView([f"{t.name:<20} {len(P.fields(t.schema))} settings" for t in self.tools],
                               empty="No installed tool has settings.")

    def keys(self):
        return [("Enter", "open")] + self.list.keys()

    def render(self, canvas):
        canvas.text(0, 0, "Settings by tool. The config files stay the source of truth.", "dim")
        self.list.render(canvas.sub(2, 0, canvas.height - 2, canvas.width))

    def key(self, k):
        if k == "enter" and self.tools:
            tool = self.tools[self.list.index]
            app = self.ctx.app
            self.ctx.push(FormScreen(app.contexts[tool.name], app.plugins.get(tool.name)))
            return True
        return self.list.key(k)


class FormScreen:
    """One tool's settings as a form (read-only for now: `s` explains)."""

    def __init__(self, ctx, plugin=None):
        self.ctx, self.plugin = ctx, plugin
        self.title = ctx.tool.name
        try:
            raw, _ = P.read_keyvalue(ctx.tool.file("CONFIG"))
        except FileNotFoundError:
            raw = {}
        self.form = W.Form(ctx.tool.schema, raw)
        self.revalidate()

    def keys(self):
        ks = self.form.keys()
        return ks if self.form.editing else ks + [("s", "save")]

    def revalidate(self):
        if self.plugin is not None and hasattr(self.plugin, "validate"):
            try:
                self.form.set_extra_errors(self.plugin.validate(self.form.values()))
            except Exception as e:
                self.ctx.message(f"validate: {type(e).__name__}: {e}", "bad")

    def render(self, canvas):
        self.form.render(canvas)

    def key(self, k):
        if not self.form.editing and k == "s":
            n = len(self.form.changed())
            self.ctx.message(f"Saving arrives with the Settings screen (#12); {n} field(s) changed.", "warn")
            return True
        used = self.form.key(k)
        if used:
            self.revalidate()
        return used

    def can_leave(self):
        if not self.form.changed():
            return True
        return self.ctx.confirm("Discard your changes?", [f"{len(self.form.changed())} setting(s) were edited and not saved."])
