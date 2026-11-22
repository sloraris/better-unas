"""The Alerts tab (issue #13): every tool's alerts.

AlertsScreen merges each tool's alerts(ctx) (docs/plugin-contract.md,
section 4): active first, newest first, with when each started and, for the
recent ones, when it resolved. The header says where alerts are sent (the
webhook in Settings › Notifications, core/notify.py).
"""

import datetime as dt

from .. import notify as N
from .. import plugins as P
from .. import updates as U
from . import widgets as W
from .screens import DetailScreen


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


class AlertsScreen:
    """Every tool's alerts merged: active first, newest first."""

    title = "Alerts"
    LEVEL_TONE = {"error": "bad", "warn": "warn", "info": "normal"}

    def __init__(self, ctx):
        self.ctx = ctx
        self.alerts = []
        self.table = W.Table([("Level", 6), ("Tool", 15), ("Since", 12), ("Resolved", 12),
                              ("Message", None)], empty="No alerts.")

    def keys(self):
        return [("Enter", "details"), ("r", "refresh")] + self.table.keys()

    def sent_to(self):
        """Where alerts go: the webhook's host, from better-unas.conf."""
        from .suite import home_of
        try:
            return N.sent_to(U.core_settings(home_of(self.ctx.app)))
        except Exception:
            return None

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
        canvas.text(0, 0, f"{active} active · {len(self.alerts) - active} recent",
                    "bad" if active else "dim", bold=bool(active))
        host = self.sent_to()
        label, value = "Sent to: ", host or "no webhook set"
        room = max(0, canvas.width // 2 - len(label))
        value = W.fit(value, room)
        x = canvas.width - len(label) - len(value)
        canvas.styled(0, x, label, "label")
        canvas.styled(0, x + len(label), value, "value" if host else "label")
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
        if k == "r":
            self.show()
            return True
        return self.table.key(k)
