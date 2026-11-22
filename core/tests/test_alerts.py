"""Alerts screen and notification setup tests (issue #13). Run from the
repository root:

    python3 -m unittest discover -s core/tests

The fixture tool uses smart-scheduler's real settings.json and its plugin's
send_test calls smart_scheduler.send_test, so the test sends here are the
tool's own payloads. They all go to a throwaway http.server on 127.0.0.1.
"""

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
from core.tui import alerts as A                  # noqa: E402
from core.tui.app import App                      # noqa: E402
from core.tui.terminal import HeadlessTerminal    # noqa: E402
from core.tui.theme import Theme                  # noqa: E402
from test_tui import CLOCK, SS_SCHEMA, SnapshotMixin, write_tool  # noqa: E402
from test_notify import FakeEndpoint, closed_port_url             # noqa: E402

SS = os.path.join(ROOT, "smart-scheduler", "smart_scheduler.py")

PLUGIN = '''
import importlib.util

CONTRACT = 1
SS = %r
_ss = None

def ss():
    global _ss
    if _ss is None:
        spec = importlib.util.spec_from_file_location("better_unas_test_ss", SS)
        _ss = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_ss)
    return _ss

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
         "message": "Extended SMART test on slot 2 (ZL2) failed: read failure."},
        {"id": "triggers", "level": "error", "active": False, "since": "2026-11-15T01:00:00-07:00",
         "resolved": "2026-11-16T01:00:00-07:00", "message": "Triggers broken (not healed)."},
    ]

def validate(values):
    if values["HEARTBEAT_FAIL_URL"] and not values["HEARTBEAT_URL"]:
        return {"HEARTBEAT_FAIL_URL": "set the heartbeat URL too"}
    return {}
''' % SS

SEND_TEST = '''
def send_test(ctx, key, values):
    return ss().send_test(values, key, host="nas", timeout=3)
'''

CONFIG = """# smart-scheduler config
ENABLED=yes

# --- Notifications ---
WEBHOOK_FORMAT=text
"""


def write_notify_tool(home, name="notify", plugin=PLUGIN + SEND_TEST):
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
        self.term = HeadlessTerminal(80, 24)
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


class NotifyTests(AlertsFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tool = write_notify_tool(self.home)

    def open_notify(self, **kw):
        app = self.make_app(**kw)
        app.press(self.alerts_tab(app))
        text = app.press("n")
        self.assertIn("Notifications", text)
        return app

    def goto(self, app, key):
        screen = app.screen
        screen.form.scroll.index = [f.key_name for f in screen.form.fields].index(key)
        return screen

    def type_url(self, app, key, url):
        self.goto(app, key)
        return app.press("enter", *url, "enter")

    def test_shows_only_the_notification_group(self):
        app = self.open_notify()
        keys = [f.key_name for f in app.screen.form.fields]
        self.assertEqual(keys, ["WEBHOOK_URL", "WEBHOOK_FORMAT", "NOTIFY_SUCCESS",
                                "ALERT_REPEAT_HOURS", "HEARTBEAT_URL", "HEARTBEAT_FAIL_URL"])
        self.assertIn("t test  s save", self.term.text().split("\n")[22])

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
        self.assertIn("Last test, Webhook URL: test alert sent (slack format)", text)
        (got,) = self.ep.requests
        self.assertEqual(got["method"], "POST")
        self.assertTrue(got["body"].startswith('{"text": "*nas smart-scheduler: info*\\n'))
        # testing saves nothing
        with open(os.path.join(self.tool, "config")) as f:
            self.assertEqual(f.read(), CONFIG)

    def test_send_test_failure_shows_the_http_error(self):
        self.ep.status = 404
        app = self.open_notify()
        self.type_url(app, "HEARTBEAT_URL", self.ep.url("/ping/abc"))
        text = app.press("t", "y")
        self.assertIn("Last test, Heartbeat URL: failed: HTTP 404 Not Found", text)
        self.assertSnapshot("alerts_notify_failed", text)
        self.assertEqual([(r["method"], r["path"]) for r in self.ep.requests], [("GET", "/ping/abc")])

    def test_send_test_unreachable(self):
        app = self.open_notify()
        self.type_url(app, "WEBHOOK_URL", closed_port_url())
        text = app.press("t", "y")
        self.assertIn("Last test, Webhook URL: failed:", text)
        self.assertIn("refused", text.lower())

    def test_no_test_without_confirm_url_or_hook(self):
        app = self.open_notify()
        self.goto(app, "WEBHOOK_URL")
        self.assertIn("Webhook URL: enter a URL first.", app.press("t"))
        self.type_url(app, "WEBHOOK_URL", "ftp://x")
        self.assertIn("Webhook URL: fix it first", app.press("t"))
        self.type_url(app, "WEBHOOK_URL", self.ep.url())
        app.press("t", "n")                                # confirm defaults to no
        self.assertEqual(self.ep.requests, [])

    def test_tool_without_send_test(self):
        shutil.rmtree(self.tool)
        write_notify_tool(self.home, plugin=PLUGIN)
        app = self.open_notify()
        self.type_url(app, "WEBHOOK_URL", self.ep.url())
        self.assertIn("notify can't send a test yet.", app.press("t"))
        self.assertEqual(self.ep.requests, [])

    def test_save_goes_through_the_config_writer(self):
        app = self.open_notify()
        url = self.ep.url("/secret-token")
        self.type_url(app, "WEBHOOK_URL", url)
        self.goto(app, "WEBHOOK_FORMAT")
        app.press("right", "right", "right")              # discord
        text = app.press("s")
        self.assertIn("Saved 2 setting(s) to config.", text)
        path = os.path.join(self.tool, "config")
        with open(path) as f:
            saved = f.read()
        self.assertTrue(saved.startswith("# smart-scheduler config\nENABLED=yes\n"))
        self.assertIn("WEBHOOK_FORMAT=discord\n", saved)
        self.assertIn(f"WEBHOOK_URL={url}\n", saved)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(app.screen.form.changed(), {})   # reloaded from the file
        with open(os.path.join(self.home, "tui.log")) as f:
            self.assertNotIn("secret-token", f.read())
        self.assertIn("Nothing to save.", app.press("s"))

    def test_save_refuses_errors_and_cross_field_rules(self):
        app = self.open_notify()
        self.type_url(app, "HEARTBEAT_FAIL_URL", self.ep.url("/fail"))
        text = app.press("s")
        self.assertIn("set the heartbeat URL too", text)
        self.assertIn("Fix 1 setting(s) first.", text)
        with open(os.path.join(self.tool, "config")) as f:
            self.assertEqual(f.read(), CONFIG)

    def test_dry_run_saves_nothing(self):
        app = self.open_notify(dry_run=True)
        self.type_url(app, "WEBHOOK_URL", self.ep.url())
        self.assertIn("Dry run: would save WEBHOOK_URL.", app.press("s"))
        with open(os.path.join(self.tool, "config")) as f:
            self.assertEqual(f.read(), CONFIG)

    def test_leaving_with_changes_asks(self):
        app = self.open_notify()
        self.type_url(app, "WEBHOOK_URL", self.ep.url())
        app.press("esc", "n")
        self.assertTrue(any("Discard your changes?" in f for f in self.frames))
        self.assertIsInstance(app.screen, A.NotifyScreen)
        app.press("esc", "y")
        self.assertIsInstance(app.screen, A.AlertsScreen)

    def test_two_tools_get_a_chooser(self):
        write_notify_tool(self.home, "notify-two")
        app = self.make_app()
        app.press(self.alerts_tab(app))
        text = app.press("n")
        self.assertIn("notify-two", text)
        app.press("down", "enter")
        self.assertEqual(app.screen.ctx.tool.name, "notify-two")


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
