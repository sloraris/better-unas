"""Alerts screen and Settings › Notifications tests (issue #13). Run from
the repository root:

    python3 -m unittest discover -s core/tests

Test sends go through core/notify.py (or a fixture tool's send_test) to a
throwaway http.server on 127.0.0.1.
"""

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "smart-scheduler", "tests"))

from core import plugins as P                     # noqa: E402
from core.tui import widgets as W                 # noqa: E402
from core.tui import settings as S                # noqa: E402
from core.tui.app import App                      # noqa: E402
from core.tui.terminal import HeadlessTerminal    # noqa: E402
from core.tui.theme import Theme                  # noqa: E402
from test_tui import CLOCK, SS_SCHEMA, SnapshotMixin, write_tool  # noqa: E402
from test_notify import FakeEndpoint, closed_port_url             # noqa: E402

PLUGIN = '''
CONTRACT = 1

def status(ctx):
    return {"enabled": True, "state": "ok"}

def summary(ctx):
    return {"state": "ok", "headline": "fine"}

def alerts(ctx):
    return [
        {"id": "backups:db", "level": "warn", "active": False, "since": "2026-11-19T02:00:00-07:00",
         "resolved": "2026-11-21T03:10:00-07:00", "message": "Couldn't read the backup tasks."},
        {"id": "hb", "level": "warn", "active": True, "since": "2026-11-21T00:30:00-07:00",
         "message": "Heartbeat URL did not answer."},
        {"id": "test:ZL2", "level": "error", "active": True, "since": "2026-11-20T01:04:00-07:00",
         "last": "2026-11-21T01:04:00-07:00",
         "message": "Extended SMART test on Drive 2 (ZL2) failed: read failure."},
        {"id": "triggers", "level": "error", "active": False, "since": "2026-11-15T01:00:00-07:00",
         "resolved": "2026-11-16T01:00:00-07:00", "message": "Triggers broken (not healed)."},
    ]

'''

CONFIG = """# smart-scheduler config
ENABLED=yes

# --- Notifications ---
WEBHOOK_FORMAT=text
"""


def write_notify_tool(home, name="notify", plugin=PLUGIN):
    d = write_tool(home, name, plugin, summary="Notification fixture")
    with open(os.path.join(d, "tool.conf"), "a") as f:
        f.write("SCHEMA=settings.json\nCONFIG=config\n")
    shutil.copy(SS_SCHEMA, os.path.join(d, "settings.json"))
    with open(os.path.join(d, "config"), "w") as f:
        f.write(CONFIG)
    return d


class AlertsFixture(SnapshotMixin):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        self.addCleanup(W.reset_editing)
        self.ep = FakeEndpoint()
        self.addCleanup(self.ep.close)

    def make_app(self, **kw):
        tools, broken = P.discover(self.home)
        self.assertEqual(broken, {})
        self.term = HeadlessTerminal(80, 28)
        self.frames = []
        real_end = self.term.end_frame

        def end_frame():
            self.frames.append(self.term.text())
            real_end()

        self.term.end_frame = end_frame
        app = App(self.term, tools, broken, clock=lambda: CLOCK, theme=Theme(True),
                  log_path=os.path.join(self.home, "tui.log"), **kw)
        app.start()
        app.draw()
        return app

    def alerts_tab(self, app):
        return str([t.title for t in app.tabs].index("Alerts") + 1)


class AlertListTests(AlertsFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tool = write_notify_tool(self.home)

    def test_alert_list_80x24(self):
        app = self.make_app()
        text = app.press(self.alerts_tab(app))
        self.assertSnapshot("alerts_80x24", text)
        rows = [line for line in text.splitlines() if "notify" in line]
        # active first (newest first), then resolved (most recently resolved first)
        self.assertEqual([r.strip(" ›").split()[0] for r in rows], ["warn", "error", "warn", "error"])
        self.assertIn("active", rows[0])
        self.assertIn("21 Nov 03:10", rows[2])
        self.assertIn("16 Nov 01:00", rows[3])

    def test_details_show_resolve_time(self):
        app = self.make_app()
        app.press(self.alerts_tab(app))
        text = app.press("down", "down", "enter")
        self.assertIn("Resolved:  Sat 21 Nov 2026 03:10", text)
        text = app.press("esc", "up", "enter")
        self.assertIn("Resolved:  no, still active", text)
        self.assertIn("Last sent: Sat 21 Nov 2026 01:04", text)

    def test_bad_alerts_show_as_a_plugin_error(self):
        with open(os.path.join(self.tool, "plugin.py"), "a") as f:
            f.write("\ndef alerts(ctx):\n    return [{'id': 'x', 'level': 'warn', 'active': False,"
                    " 'since': '2026-11-20T00:00:00', 'resolved': 'yesterday', 'message': 'm'}]\n")
        app = self.make_app()
        app.press(self.alerts_tab(app))
        text = app.press("enter")
        self.assertIn("since/last/resolved as ISO 8601", text)


class AlertsHeaderTests(AlertsFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        write_notify_tool(self.home)

    def test_sent_to_names_the_webhook_host_only(self):
        app = self.make_app()
        app.home = self.home
        self.assertIn("Sent to: no webhook set", app.press(self.alerts_tab(app)))
        with open(os.path.join(self.home, "better-unas.conf"), "w") as f:
            f.write("WEBHOOK_URL=https://ntfy.sh/secret-topic\n")
        text = app.press("r")
        self.assertIn("Sent to: ntfy.sh", text.split("\n")[4])
        self.assertNotIn("secret-topic", text)
        self.assertNotIn(" n ", text.split("\n")[25])          # the old setup key is gone


class NotifyTests(AlertsFixture, unittest.TestCase):
    """Settings › Notifications: the shared webhook and heartbeat, saved in
    better-unas.conf, with `t` sending a test through core/notify.py."""

    def setUp(self):
        super().setUp()
        self.tool = write_notify_tool(self.home)
        self.conf = os.path.join(self.home, "better-unas.conf")

    def open_notify(self, **kw):
        app = self.make_app(**kw)
        app.home = self.home
        app.press(str([t.title for t in app.tabs].index("Settings") + 1))
        self.assertTrue(app.screen.open("Notifications"))
        text = app.press()
        self.assertIn("└─ Notifications ", text)
        return app

    def goto(self, app, key):
        screen = app.screen
        screen.form.scroll.index = [f.key_name for f in screen.form.fields].index(key)
        return screen

    def type_url(self, app, key, url):
        self.goto(app, key)
        return app.press("enter", *url, "enter")

    def saved(self):
        try:
            with open(self.conf) as f:
                return f.read()
        except FileNotFoundError:
            return None

    def test_shows_only_the_notification_settings(self):
        app = self.open_notify()
        keys = [f.key_name for f in app.screen.form.fields]
        self.assertEqual(keys, ["WEBHOOK_URL", "WEBHOOK_FORMAT", "NOTIFY_SUCCESS",
                                "ALERT_REPEAT_HOURS", "HEARTBEAT_URL", "HEARTBEAT_FAIL_URL"])
        self.assertIn("t send test", self.term.text().split("\n")[25])
        self.goto(app, "WEBHOOK_FORMAT")
        self.assertNotIn("send test", app.press().split("\n")[25])   # only on URL settings

    def test_send_test_success_uses_the_format_on_screen(self):
        app = self.open_notify()
        self.type_url(app, "WEBHOOK_URL", self.ep.url())
        self.goto(app, "WEBHOOK_FORMAT")
        app.press("right", "right")                       # text -> json -> slack
        self.goto(app, "WEBHOOK_URL")
        text = app.press("t", "y")
        asked = next(f for f in self.frames if "Send a test to Webhook URL?" in f)
        self.assertIn("http://127.0.0.1", asked)
        self.assertNotIn("/hook", asked)                  # the path may hold a token
        self.assertIn("Webhook URL: test alert sent (slack format).", text)
        (got,) = self.ep.requests
        self.assertEqual(got["method"], "POST")
        self.assertIn(' better-unas: info*\\n', got["body"])
        self.assertIsNone(self.saved())                   # testing saves nothing

    def test_send_test_failure_shows_the_http_error(self):
        self.ep.status = 404
        app = self.open_notify()
        self.type_url(app, "HEARTBEAT_URL", self.ep.url("/ping/abc"))
        text = app.press("t", "y")
        self.assertIn("Heartbeat URL: test failed: HTTP 404 Not Found", text)
        self.assertSnapshot("settings_notify_failed", text)
        self.assertEqual([(r["method"], r["path"]) for r in self.ep.requests], [("GET", "/ping/abc")])

    def test_send_test_unreachable(self):
        app = self.open_notify()
        self.type_url(app, "WEBHOOK_URL", closed_port_url())
        text = app.press("t", "y")
        self.assertIn("Webhook URL: test failed:", text)
        self.assertIn("refused", text.lower())

    def test_no_test_without_confirm_or_url(self):
        app = self.open_notify()
        self.goto(app, "WEBHOOK_URL")
        self.assertIn("Webhook URL: enter a URL first.", app.press("t"))
        self.type_url(app, "WEBHOOK_URL", "ftp://x")
        self.assertIn("Webhook URL: fix it first", app.press("t"))
        self.type_url(app, "WEBHOOK_URL", self.ep.url())
        app.press("t", "n")                                # confirm defaults to no
        self.assertEqual(self.ep.requests, [])

    def test_save_writes_better_unas_conf(self):
        app = self.open_notify()
        url = self.ep.url("/secret-token")
        self.type_url(app, "WEBHOOK_URL", url)
        self.goto(app, "WEBHOOK_FORMAT")
        app.press("right", "right", "right")              # discord
        text = app.press("s")
        self.assertIn("Saved 2 setting(s) to better-unas/better-unas.conf.", text)
        saved = self.saved()
        self.assertIn("WEBHOOK_FORMAT=discord\n", saved)
        self.assertIn(f"WEBHOOK_URL={url}\n", saved)
        self.assertEqual(stat.S_IMODE(os.stat(self.conf).st_mode), 0o600)
        with open(os.path.join(self.tool, "config")) as f:
            self.assertEqual(f.read(), CONFIG)            # the tool's config is left alone
        with open(os.path.join(self.home, "tui.log")) as f:
            self.assertNotIn("secret-token", f.read())
        self.assertIn("Nothing to save", app.press("s"))
        app.press("esc")
        self.assertIn("webhook: 127.0.0.1 · heartbeat: off", app.press())

    def test_dry_run_saves_nothing(self):
        app = self.open_notify(dry_run=True)
        self.type_url(app, "WEBHOOK_URL", self.ep.url())
        self.assertIn("Dry run: 1 setting(s) are valid", app.press("s"))
        self.assertIsNone(self.saved())

    def test_leaving_with_changes_asks(self):
        app = self.open_notify()
        self.type_url(app, "WEBHOOK_URL", self.ep.url())
        app.press("esc", "n")
        self.assertTrue(any("Discard your changes?" in f for f in self.frames))
        self.assertIsInstance(app.screen, S.NotifyScreen)
        app.press("esc", "y")
        self.assertIsInstance(app.screen, S.SettingsScreen)


class ToolUrlTestTests(AlertsFixture, unittest.TestCase):
    """A tool's own URL setting gets `t` through its plugin's send_test."""

    SCHEMA = {"contract": 1, "groups": [{"id": "general", "title": "General", "fields": [
        {"key": "PUSH_URL", "type": "url", "label": "Push URL", "default": ""}]}]}

    def make_tool(self, plugin):
        d = write_tool(self.home, "pusher", plugin, summary="URL fixture")
        with open(os.path.join(d, "tool.conf"), "a") as f:
            f.write("SCHEMA=settings.json\nCONFIG=config\n")
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump(self.SCHEMA, f)
        with open(os.path.join(d, "config"), "w") as f:
            f.write("PUSH_URL=%s\n" % self.ep.url("/push"))

    def open_form(self):
        app = self.make_app()
        app.home = self.home
        app.press(str([t.title for t in app.tabs].index("Settings") + 1))
        self.assertTrue(app.screen.open("pusher"))
        return app

    def test_send_test_through_the_plugin(self):
        self.make_tool(PLUGIN + "\ndef send_test(ctx, key, values):\n    return 'pushed ' + values[key][-5:]\n")
        app = self.open_form()
        self.assertIn("t send test", app.press().split("\n")[25])
        self.assertIn("Push URL: pushed /push.", app.press("t", "y"))

    def test_no_send_test_no_key(self):
        self.make_tool(PLUGIN)
        app = self.open_form()
        self.assertNotIn("send test", app.press().split("\n")[25])


class ContractTests(unittest.TestCase):
    def test_send_test_is_optional_and_must_be_callable(self):
        self.assertIn("send_test", P.PLUGIN_OPTIONAL)
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        d = write_tool(home, "bad", PLUGIN + "\nsend_test = 3\n")
        with self.assertRaises(P.ContractError) as e:
            P.load_plugin(d, P.load_manifest(d))
        self.assertIn("plugin: send_test must be a function", e.exception.problems)

    def test_resolved_is_optional_iso(self):
        base = {"id": "a", "level": "warn", "message": "m", "active": False,
                "since": "2026-11-20T00:00:00-07:00"}
        self.assertEqual(P.check_alerts([base]), [])
        self.assertEqual(P.check_alerts([dict(base, resolved="2026-11-21T00:00:00-07:00")]), [])
        self.assertEqual(len(P.check_alerts([dict(base, resolved="soon")])), 1)


if __name__ == "__main__":
    unittest.main()
