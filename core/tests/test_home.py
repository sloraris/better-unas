"""Home dashboard tests (issue #9). Run from the repository root:

    python3 -m unittest discover -s core/tests

Fake plugins stand in for real tools and a fake core.updates for the update
checker; everything renders headless at 80x24. UPDATE_SNAPSHOTS=1 regenerates
the snapshots in core/tests/snapshots/.
"""

import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from test_tui import CLOCK, SnapshotMixin, write_tool            # noqa: E402
from core import plugins as P                                    # noqa: E402
from core.tui import home as H, widgets as W                     # noqa: E402
from core.tui.app import App                                     # noqa: E402
from core.tui.terminal import HeadlessTerminal                   # noqa: E402
from core.tui.theme import Theme                                 # noqa: E402

# A smart-scheduler-shaped tool: the data comes from module-level dicts the
# test fills in through a JSON file next to the plugin, so one plugin covers
# every scenario.
DRIVES_PLUGIN = '''
import json, os
CONTRACT = 1
HERE = os.path.dirname(os.path.abspath(__file__))

def _data():
    with open(os.path.join(HERE, "fake.json")) as f:
        return json.load(f)

def status(ctx):
    return _data()["status"]

def summary(ctx):
    d = _data()
    if d.get("summary_raises"):
        raise OSError(d["summary_raises"])
    return d["summary"]

def alerts(ctx):
    return _data()["alerts"]

class Placeholder:
    def __init__(self, ctx, title):
        self.title = title
    def keys(self):
        return []
    def render(self, canvas):
        canvas.text(0, 0, self.title + " screen")
    def key(self, k):
        return False

def screens(ctx):
    return [{"id": "calendar", "tab": "Calendar", "make": lambda c: Placeholder(c, "Calendar")},
            {"id": "drives", "tab": "Drives", "make": lambda c: Placeholder(c, "Drives")}]
'''

OK_STATUS = {"enabled": True, "state": "ok", "problems": [], "detail": []}

FRESH = {
    "status": OK_STATUS,
    "summary": {"state": "ok", "headline": "7 drives scheduled, no tests run yet", "items": [
        {"label": "Next test", "value": "Sun 22 Nov 01:00, slot 1", "tone": "warn", "tab": "Calendar"},
        {"label": "Last result", "value": "None yet", "tone": "dim", "tab": "Drives"},
        {"label": "Next scrub", "value": "Sun 6 Dec 02:30", "tab": "Calendar"},
        {"label": "Next backup", "value": "Mon 23 Nov 03:00, cloud backup (~1 h)", "tab": "Calendar"}]},
    "alerts": [],
}

ALERT = {
    "status": {"enabled": True, "state": "error", "problems": [], "detail": []},
    "summary": {"state": "error", "headline": "7 drives scheduled, 1 failed", "items": [
        {"label": "Next test", "value": "Mon 23 Nov 01:00, slot 4", "tone": "warn", "tab": "Calendar"},
        {"label": "Last result", "value": "Failed, slot 2, Fri 20 Nov", "tone": "bad", "tab": "Drives"},
        {"label": "Next scrub", "value": "Sun 6 Dec 02:30", "tab": "Calendar"},
        {"label": "Next backup", "value": "Mon 23 Nov 03:00, cloud backup (~1 h)", "tab": "Calendar"}]},
    "alerts": [
        {"id": "test:slot2", "level": "error", "active": True, "since": "2026-11-20T01:04:00-07:00",
         "message": "Extended test on slot 2 failed: read failure."},
        {"id": "temp:slot5", "level": "warn", "active": True, "since": "2026-11-21T14:00:00-07:00",
         "message": "Slot 5 reached 52 C during its test."},
        {"id": "hb", "level": "warn", "active": False, "since": "2026-11-18T03:00:00-07:00",
         "message": "Heartbeat URL did not answer."}],
}

RUNNING = {
    "status": {"enabled": True, "state": "ok", "problems": [], "detail": []},
    "summary": {"state": "warn", "headline": "Extended test running on slot 3 (41%)", "items": [
        {"label": "Running", "value": "Slot 3, started 01:00, ~6 h left", "tone": "warn", "tab": "Drives"},
        {"label": "Next test", "value": "Tue 24 Nov 01:00, slot 6", "tone": "warn", "tab": "Calendar"},
        {"label": "Last result", "value": "Passed, slot 5, Thu 19 Nov", "tone": "good", "tab": "Drives"},
        {"label": "Next scrub", "value": "Sun 6 Dec 02:30", "tab": "Calendar"},
        {"label": "Next backup", "value": "Mon 23 Nov 03:00, cloud backup (~1 h)", "tab": "Calendar"}]},
    "alerts": [],
}

UPDATE = {"current": "v0.2.0", "latest": "v0.3.0", "available": True,
          "checked_at": "2026-11-21T18:00:00-07:00", "error": None}
CURRENT = dict(UPDATE, latest="v0.2.0", available=False)


def fake_updates(result):
    mod = types.ModuleType("core.updates")
    mod.calls = []

    def cached_status(home):
        mod.calls.append(home)
        if isinstance(result, Exception):
            raise result
        return result

    mod.cached_status = cached_status
    return mod


class HomeFixture(SnapshotMixin):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        self.addCleanup(W.reset_editing)

    def tool(self, data, name="smart-scheduler"):
        import json
        d = write_tool(self.home, name, plugin=DRIVES_PLUGIN, summary="Schedules SMART tests")
        with open(os.path.join(d, "fake.json"), "w") as f:
            json.dump(data, f)
        return d

    def make_app(self, updates=None):
        """updates: what cached_status returns (None, a dict or an exception),
        or the string "missing" for no core.updates module at all."""
        if updates == "missing":
            patch = mock.patch.dict(sys.modules, {"core.updates": None})   # import -> ImportError
        else:
            self.updates = fake_updates(updates)
            patch = mock.patch.dict(sys.modules, {"core.updates": self.updates})
        patch.start()
        self.addCleanup(patch.stop)
        tools, broken = P.discover(self.home)
        self.term = HeadlessTerminal(80, 24)
        app = App(self.term, tools, broken, clock=lambda: CLOCK, theme=Theme(True))
        app.start()
        app.draw()
        return app


class HomeSnapshotTests(HomeFixture, unittest.TestCase):
    def check(self, name, app):
        self.assertSnapshot(f"home_{name}", self.term.text())
        self.assertSnapshot(f"home_{name}_tones", self.term.tones())

    def test_fresh_install(self):
        self.tool(FRESH)
        app = self.make_app(updates=None)
        text = self.term.text()
        self.assertNotIn("Update", text)          # no notice until there is one
        self.assertIn("No active alerts.", text)
        self.assertIn("Next backup", text)
        self.check("fresh", app)

    def test_update_available(self):
        self.tool(FRESH)
        app = self.make_app(updates=UPDATE)
        self.assertIn("› ▲ Update available: v0.3.0   Settings › better-unas", self.term.text())
        self.assertEqual(self.updates.calls, [self.home])
        self.check("update", app)

    def test_active_alert(self):
        self.tool(ALERT)
        app = self.make_app(updates=CURRENT)
        text = self.term.text()
        self.assertIn("Alerts  2 active", text)
        self.assertIn("error smart-scheduler: Extended test on slot 2 failed", text)
        self.assertNotIn("Heartbeat", text)                   # resolved ones stay on the Alerts tab
        self.assertLess(text.index("Slot 5 reached"), text.index("slot 2 failed"))   # newest first
        self.check("alert", app)

    def test_test_running(self):
        self.tool(RUNNING)
        app = self.make_app(updates=CURRENT)
        self.assertIn("Extended test running on slot 3 (41%)", self.term.text())
        self.check("running", app)

    def test_no_tools(self):
        self.make_app(updates=None)
        text = self.term.text()
        self.assertIn("No tools are installed yet.", text)
        self.assertIn("Install tools in Settings.", text)


class HomeBehaviourTests(HomeFixture, unittest.TestCase):
    def tabs(self, app):
        return [t.title for t in app.tabs]

    def test_enter_jumps_to_the_rows_tab(self):
        self.tool(ALERT)
        app = self.make_app(updates=CURRENT)
        self.assertEqual(self.tabs(app), ["Home", "Calendar", "Drives", "Alerts", "Settings"])
        app.press("enter")                          # first row: the alerts heading
        self.assertEqual(app.tab.title, "Alerts")
        app.press("1", "down", "enter")             # the first alert
        self.assertEqual(app.tab.title, "Alerts")

        def goto(label):
            app.press("1", "home")
            screen = app.screen
            for _ in range(len(screen.rows)):
                row = screen.rows[screen.index]
                if any(label in text for text, _, _ in row.segments):
                    break
                app.press("down")
            app.press("enter")
            return app.tab.title

        self.assertEqual(goto("1.2.3"), "Calendar")    # card head: the tool's first tab
        self.assertEqual(goto("Last result"), "Drives")             # item names its tab
        self.assertEqual(goto("Next scrub"), "Calendar")

    def test_cursor_skips_blank_rows_and_stops_at_the_ends(self):
        self.tool(FRESH)
        app = self.make_app()
        screen = app.screen
        app.press("up")
        self.assertEqual(screen.index, 0)
        app.press("down")
        self.assertEqual(screen.index, 2)          # row 1 is the blank spacer
        app.press("end")
        self.assertEqual(screen.index, len(screen.rows) - 1)
        app.press("pgup", "pgup")
        self.assertEqual(screen.index, 0)
        self.assertIn("› Alerts", self.term.text())

    def test_a_jumps_and_t_is_gone(self):
        self.tool(FRESH)
        app = self.make_app()
        app.press("a")
        self.assertEqual(app.tab.title, "Alerts")
        app.press("1", "t")
        self.assertEqual(app.tab.title, "Home")

    def test_update_notice_opens_settings_better_unas(self):
        self.tool(FRESH)
        app = self.make_app(updates=UPDATE)
        app.press("enter")
        self.assertEqual(app.tab.title, "Settings")
        self.assertIn("Settings › better-unas", self.term.text().split("\n")[1])

    def test_unknown_item_tab_falls_back_to_the_tools_first_tab(self):
        data = dict(FRESH, summary=dict(FRESH["summary"], items=[
            {"label": "Odd", "value": "x", "tab": "Nowhere"}]))
        self.tool(data)
        app = self.make_app()
        rows = [r for r in app.screen.rows if r.segments and "Odd" in r.segments[0][0]]
        self.assertEqual(rows[0].target, "Calendar")

    def test_refresh_rereads(self):
        d = self.tool(FRESH)
        app = self.make_app()
        import json
        with open(os.path.join(d, "fake.json"), "w") as f:
            json.dump(RUNNING, f)
        self.assertNotIn("running on slot 3", self.term.text())
        self.assertIn("running on slot 3", app.press("r"))
        self.assertEqual(len(self.updates.calls), 2)

    def test_reopening_the_tab_rereads(self):
        d = self.tool(FRESH)
        app = self.make_app()
        import json
        with open(os.path.join(d, "fake.json"), "w") as f:
            json.dump(ALERT, f)
        self.assertIn("2 active", app.press("2", "1"))

    def test_no_update_info_means_no_notice(self):
        self.tool(FRESH)
        for updates in ("missing", RuntimeError("cache unreadable"), CURRENT,
                        dict(CURRENT, error="GitHub did not answer")):
            self.make_app(updates=updates)
            self.assertNotIn("Update", self.term.text(), updates)
            self.assertIn("› Alerts", self.term.text())

    def test_summary_failure_and_bad_alerts_show_on_the_card(self):
        self.tool(dict(FRESH, summary_raises="drive api down", alerts=[{"id": "x"}]))
        app = self.make_app()
        text = self.term.text()
        self.assertIn("summary: OSError: drive api down", text)
        self.assertIn("alerts could not be read", text)
        self.assertIn("smart-scheduler  1.2.3  ok", text)    # status still gives the badge

    def test_status_problems_and_disabled_tool(self):
        self.tool(dict(FRESH, status={"enabled": False, "state": "off",
                                      "problems": ["Timer is missing; the next run restores it."]}))
        text = self.make_app() and self.term.text()
        self.assertIn("smart-scheduler  1.2.3  off", text)
        self.assertIn("! Timer is missing", text)

    def test_broken_and_plugin_less_tools(self):
        write_tool(self.home, "broken-one", plugin="CONTRACT = 1\nraise ImportError('missing helper')\n")
        d = write_tool(self.home, "plain")
        with open(os.path.join(d, "tool.conf")) as f:
            conf = f.read().replace("PLUGIN=plugin.py\n", "")
        with open(os.path.join(d, "tool.conf"), "w") as f:
            f.write(conf)
        app = self.make_app()
        text = self.term.text()
        self.assertIn("broken-one  1.2.3  broken  See Settings.", text)
        self.assertIn("No status available.", text)
        self.assertEqual({r.target for r in app.screen.rows if r.segments and "broken-one" in r.segments[0][0]},
                         {"Settings:broken-one"})

    def test_many_rows_scroll_with_the_cursor(self):
        for i in range(4):
            self.tool(RUNNING, name=f"tool-{i}")
        app = self.make_app()
        self.assertIn("…", self.term.text().splitlines()[21])
        app.press("end")
        lines = self.term.text().splitlines()
        self.assertTrue(any(line.lstrip().startswith("›") and "Next backup" in line for line in lines))
        self.assertNotIn("better-unas", lines[2])

    def test_home_is_resolved_from_the_tools_folder(self):
        self.tool(FRESH)
        app = self.make_app(updates=None)
        self.assertEqual(self.updates.calls, [self.home])
        app.tools = []
        self.assertEqual(app.screen.home(), H.ROOT)


if __name__ == "__main__":
    unittest.main()
