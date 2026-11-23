"""Deduplicated alerts with an outbox, so nothing is lost while the webhook
is down. State lives in the tool's state dict:

    state["alerts"]    {key: {"msg", "level", "sent", "since"}}: open alerts
    state["resolved"]  the last RESOLVED_KEEP cleared ones, for the TUI's Alerts screen
    state["outbox"]    messages not yet delivered (the newest OUTBOX_KEEP)

Standard library only.
"""

import datetime as dt
import socket
import syslog

from core.lib import notify, util

RESOLVED_KEEP = 20
OUTBOX_KEEP = 50


class Alerts:
    """name is the tool's (it tags the log and titles the webhook). now and
    log can be swapped for tests."""

    def __init__(self, cfg, state, dry, name, now=None, log=None, send=None):
        self.cfg, self.state, self.dry, self.name = cfg, state, dry, name
        self.active = state.setdefault("alerts", {})
        self.outbox = state.setdefault("outbox", [])
        self.host = socket.gethostname()
        self._now = now or util.now
        self._log = log or (lambda msg, level=syslog.LOG_INFO: util.log(name, msg, level))
        self._send = send or notify.send_webhook

    def raise_(self, key, msg, level="error"):
        """Open or refresh an alert. The same message at the same level is
        sent again only after ALERT_REPEAT_HOURS."""
        t = self._now()
        a = self.active.get(key)
        repeat = dt.timedelta(hours=self.cfg["ALERT_REPEAT_HOURS"])
        if a and a["msg"] == msg and a["level"] == level and t - util.parse_time(a["sent"]) < repeat:
            return
        self._log(f"[{level}] {msg}", syslog.LOG_ERR if level == "error" else syslog.LOG_WARNING)
        self.active[key] = {"msg": msg, "level": level, "sent": t.isoformat(),
                            "since": (a or {}).get("since", t.isoformat())}
        self._queue(level, msg)

    def clear(self, key, msg=None):
        a = self.active.pop(key, None)
        if a:
            recent = self.state.setdefault("resolved", [])
            recent.append({"key": key, "msg": a["msg"], "level": a["level"],
                           "since": a.get("since", a["sent"]), "sent": a["sent"],
                           "resolved": self._now().isoformat()})
            del recent[:-RESOLVED_KEEP]
            text = msg or f"Resolved: {a['msg']}"
            self._log(f"[resolved] {text}")
            self._queue("resolved", text)

    def info(self, msg):
        self._log(f"[info] {msg}")
        self._queue("info", msg)

    def has_errors(self):
        return any(a["level"] == "error" for a in self.active.values())

    def _queue(self, level, msg):
        self.outbox.append({"level": level, "msg": msg, "at": self._now().isoformat()})
        del self.outbox[:-OUTBOX_KEEP]

    def flush(self):
        url = self.cfg["WEBHOOK_URL"]
        if self.dry:
            for m in self.outbox:
                print(f"(dry-run) would notify: [{m['level']}] {m['msg']}")
            return
        if not url:
            self.outbox.clear()
            return
        while self.outbox:
            if self._send(url, self.cfg["WEBHOOK_FORMAT"], self.outbox[0], self.host, self.name) is not None:
                self._log(f"webhook unreachable; {len(self.outbox)} message(s) kept for the next run",
                          syslog.LOG_WARNING)
                return
            self.outbox.pop(0)
