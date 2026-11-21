"""The Alerts tab (issue #13): every tool's alerts, and notification setup.

AlertsScreen merges each tool's alerts(ctx) (docs/plugin-contract.md,
section 4): active first, newest first, with when each started and, for the
recent ones, when it resolved. `n` opens NotifyScreen for a tool: the schema
groups that hold `url` fields (smart-scheduler's Notifications group: webhook
URL and format, heartbeat and fail URLs) as a form, where `t` sends a test
through the tool's optional send_test(ctx, key, values) and `s` saves through
the config writer.
"""

import datetime as dt
import importlib.util
import os
import urllib.parse

from .. import plugins as P
from . import widgets as W
from .screens import DetailScreen

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The comment-keeping config writer from #7. It is generic (it needs only the
# schema), so the core borrows it until the Settings screen (#12) moves it into
# the core; then only config_writer() changes.
WRITER = os.path.join(ROOT, "smart-scheduler", "settings.py")
_writer = None


def config_writer():
    global _writer
    if _writer is None:
        spec = importlib.util.spec_from_file_location("better_unas_config_writer", WRITER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _writer = mod
    return _writer


def parse_time(iso):
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def short(iso):
    """'20 Nov 01:04', or '' for no time."""
    t = parse_time(iso)
    return f"{t:%d %b %H:%M}" if t else ""


def long(iso):
    t = parse_time(iso)
    return f"{t:%a %d %b %Y %H:%M}" if t else str(iso or "")


def where(url):
    """scheme://host of a URL, for a confirm: never the path, which often holds a token."""
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{u.hostname}" if u.hostname else "the URL on screen"


def url_groups(schema):
    """The schema's groups that hold at least one url field."""
    if not schema:
        return []
    return [g for g in schema["groups"] if any(f["type"] == "url" for f in g["fields"])]


class AlertsScreen:
    """Every tool's alerts merged: active first, newest first."""

    title = "Alerts"
    LEVEL_TONE = {"error": "bad", "warn": "warn", "info": "normal"}

    def __init__(self, ctx):
        self.ctx = ctx
        self.alerts = []
        self.table = W.Table([("Level", 6), ("Tool", 15), ("Since", 12), ("Resolved", 12),
                              ("Message", None)], empty="No alerts.")

    def notify_tools(self):
        app = self.ctx.app
        return [t for t in app.tools if url_groups(t.schema) and t.name in app.contexts]

    def keys(self):
        ks = [("Enter", "details")]
        if self.notify_tools():
            ks.append(("n", "setup"))
        return ks + [("r", "refresh")] + self.table.keys()

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
                continue
            found += [(name, a) for a in alerts]

        def newest(na):
            a = na[1]
            t = parse_time(a.get("since") if a["active"] else (a.get("resolved") or a.get("since")))
            return t.timestamp() if t else float("inf")

        found.sort(key=newest, reverse=True)
        found.sort(key=lambda na: not na[1]["active"])
        self.alerts = found
        rows = []
        for name, a in found:
            tone = self.LEVEL_TONE[a["level"]]
            if a["active"]:
                rows.append([(a["level"], tone), name, short(a["since"]), ("active", tone),
                             a["message"]])
            else:
                rows.append([(a["level"], "dim"), (name, "dim"), (short(a["since"]), "dim"),
                             (short(a.get("resolved")) or "resolved", "good"), (a["message"], "dim")])
        self.table.set_rows(rows)

    def render(self, canvas):
        active = sum(1 for _, a in self.alerts if a["active"])
        canvas.text(0, 0, f"{active} active, {len(self.alerts) - active} recent",
                    "bad" if active else "dim", bold=bool(active))
        if self.notify_tools():
            hint = "n: webhook and heartbeat setup"
            canvas.text(0, canvas.width - len(hint), hint, "dim")
        self.table.render(canvas.sub(2, 0, canvas.height - 2, canvas.width))

    def key(self, k):
        if k == "enter" and self.alerts:
            name, a = self.alerts[self.table.index]
            tone = self.LEVEL_TONE[a["level"]] if a["active"] else "dim"
            lines = [(a["message"], tone), "",
                     f"Tool:      {name}", f"Level:     {a['level']}",
                     f"Since:     {long(a['since'])}"]
            if a.get("last"):
                lines.append(f"Last sent: {long(a['last'])}")
            if a["active"]:
                lines.append(("Resolved:  no, still active", tone))
            else:
                lines.append((f"Resolved:  {long(a.get('resolved')) or 'yes'}", "good"))
            self.ctx.push(DetailScreen(f"Alert {a['id']}", lines))
            return True
        if k == "n":
            tools = self.notify_tools()
            if len(tools) == 1:
                self.ctx.push(self.notify_screen(tools[0]))
            elif tools:
                self.ctx.push(NotifyChooser(self))
            else:
                self.ctx.message("No installed tool has notification settings.", "dim")
            return True
        if k == "r":
            self.show()
            return True
        return self.table.key(k)

    def notify_screen(self, tool):
        app = self.ctx.app
        return NotifyScreen(app.contexts[tool.name], app.plugins.get(tool.name))


class NotifyChooser:
    title = "Notifications"

    def __init__(self, alerts):
        self.alerts = alerts
        self.tools = alerts.notify_tools()
        self.list = W.ListView([t.name for t in self.tools])

    def keys(self):
        return [("Enter", "open")] + self.list.keys()

    def render(self, canvas):
        canvas.text(0, 0, "Notification settings by tool.", "dim")
        self.list.render(canvas.sub(2, 0, canvas.height - 2, canvas.width))

    def key(self, k):
        if k == "enter":
            self.alerts.ctx.push(self.alerts.notify_screen(self.tools[self.list.index]))
            return True
        return self.list.key(k)


class NotifyScreen:
    """One tool's notification settings: edit, send a test, save."""

    def __init__(self, ctx, plugin=None):
        self.ctx, self.plugin = ctx, plugin
        self.title = "Notifications"
        self.schema = {"groups": url_groups(ctx.tool.schema)}
        self.last = None            # (text, tone) of the last test, shown under the form
        self.load()

    def load(self):
        try:
            raw, _ = P.read_keyvalue(self.ctx.tool.file("CONFIG"))
        except FileNotFoundError:
            raw = {}
        self.form = W.Form(self.schema, raw)
        self.revalidate()

    def keys(self):
        ks = self.form.keys()
        if self.form.editing:
            return ks
        move, edit = ks[:1], ks[1:]
        test = [("t", "test")] if self.form.current.spec["type"] == "url" else []
        return edit + test + [("s", "save")] + move

    def values(self):
        """Typed settings with the form's unsaved edits applied."""
        values = dict(self.ctx.config())
        values.update(self.form.values())
        return values

    def revalidate(self):
        if self.plugin is None or not hasattr(self.plugin, "validate"):
            return
        try:
            errors = self.plugin.validate(self.values()) or {}
        except Exception as e:
            self.ctx.message(f"validate: {type(e).__name__}: {e}", "bad")
            return
        mine = {f.key_name for f in self.form.fields}
        self.form.set_extra_errors({k: v for k, v in errors.items() if k in mine})

    def render(self, canvas):
        self.form.render(canvas.sub(0, 0, canvas.height - 1, canvas.width))
        if self.last:
            canvas.text(canvas.height - 1, 0, W.fit(self.last[0], canvas.width - 1), self.last[1])

    def key(self, k):
        if not self.form.editing:
            if k == "t" and self.form.current.spec["type"] == "url":
                self.send_test(self.form.current)
                return True
            if k == "s":
                self.save()
                return True
        used = self.form.key(k)
        if used:
            self.revalidate()
        return used

    def send_test(self, field):
        label = field.label
        if field.error:
            return self.ctx.message(f"{label}: fix it first ({field.error}).", "bad")
        url = field.value
        if not url:
            return self.ctx.message(f"{label}: enter a URL first.", "warn")
        fn = getattr(self.plugin, "send_test", None)
        if not callable(fn):
            return self.ctx.message(f"{self.ctx.tool.name} can't send a test yet.", "warn")
        if not self.ctx.confirm(f"Send a test to {label}?",
                                [f"To {where(url)}, using the settings on screen (saved or not)."]):
            return
        values = self.values()
        try:
            ok = self.ctx.run_slow(lambda: fn(self.ctx, field.key_name, values),
                                   f"Sending a test to {label}")
        except Exception as e:
            self.last = (f"Last test, {label}: failed: {str(e) or type(e).__name__}", "bad")
            return
        if ok is None:
            return self.ctx.message(f"{label}: this setting has no test.", "dim")
        self.last = (f"Last test, {label}: {ok}", "good")

    def save(self):
        errors = self.form.errors()
        if errors:
            return self.ctx.message(f"Fix {len(errors)} setting(s) first.", "bad")
        edits = self.form.changed()
        if not edits:
            return self.ctx.message("Nothing to save.", "dim")
        names = ", ".join(sorted(edits))
        if self.ctx.dry_run:
            return self.ctx.message(f"Dry run: would save {names}.", "warn")
        writer = config_writer()
        path = self.ctx.tool.file("CONFIG")
        try:
            with self.ctx.lock():
                writer.save(path, edits, self.ctx.tool.schema)
        except writer.ValidationError as e:
            self.form.set_extra_errors(e.errors)
            return self.ctx.message(f"Not saved: {e}", "bad")
        self.load()
        self.ctx.log(f"saved {names}")     # names only: values may be secret
        self.ctx.message(f"Saved {len(edits)} setting(s) to {os.path.basename(path)}.", "good")

    def can_leave(self):
        n = len(self.form.changed())
        if not n:
            return True
        return self.ctx.confirm("Discard your changes?", [f"{n} setting(s) were edited and not saved."])
