"""The core Calendar (core/tui/calendar.py): tools' calendar hooks merged
into one month grid. Run from the repository root:

    python3 -m unittest discover -s core/tests

Two throwaway tools return their days from a module variable the tests set.
The clock is Sun 22 Nov 2026 01:00 (a six-week month); October is five weeks.
"""

import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import plugins as P                       # noqa: E402
from core.tui import calendar as cal                # noqa: E402
from core.tui import widgets as W                   # noqa: E402
from core.tui.app import App                        # noqa: E402
from core.tui.terminal import HeadlessTerminal      # noqa: E402
from core.tui.theme import Theme                    # noqa: E402
from test_tui import CLOCK, write_tool              # noqa: E402

PLUGIN = '''
CONTRACT = 1
DAYS = {"days": {}}

def status(ctx):
    return {"enabled": True, "state": "ok", "problems": [], "detail": []}

def summary(ctx):
    return {"state": "ok", "headline": "fine", "items": []}

def alerts(ctx):
    return []

def calendar(ctx):
    if isinstance(DAYS, Exception):
        raise DAYS
    return DAYS
'''

D = dt.date


def chip(kind, label, text=None, **kw):
    return dict({"status": kind, "place": "chip", "chip": label, "text": text or f"{kind} {label}",
                 "section": "Jobs"}, **kw)


class CalendarFixture(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        write_tool(self.home, "alpha", PLUGIN)
        write_tool(self.home, "beta", PLUGIN)
        self.addCleanup(W.reset_editing)
        no_updates = mock.patch.dict(sys.modules, {"core.updates": None})
        no_updates.start()
        self.addCleanup(no_updates.stop)

    def make_app(self, alpha, beta=None, width=80, height=28):
        tools, broken = P.discover(self.home)
        self.term = HeadlessTerminal(width, height)
        app = App(self.term, tools, broken, clock=lambda: CLOCK, log_path=os.path.join(self.home, "tui.log"),
                  theme=Theme(True))
        app.plugins["alpha"].DAYS = alpha
        app.plugins["beta"].DAYS = beta if beta is not None else {"days": {}}
        app.start()
        app.draw()
        self.app = app
        return app

    def lines(self):
        return self.term.text().splitlines()


class MergeTests(CalendarFixture):
    def test_tab_and_merged_days(self):
        app = self.make_app(
            {"today": D(2026, 11, 22), "until": D(2026, 12, 20), "legend": [("planned", "planned"), ("ok", "passed")],
             "problems": ["alpha is unsure"],
             "days": {D(2026, 11, 23): [chip("planned", "3")], D(2026, 11, 3): [chip("ok", "1", past=True)]}},
            {"legend": [("scrub", "scrub"), ("planned", "ignored twice")],
             "days": {D(2026, 11, 23): [chip("planned", "S"),
                                        {"status": "scrub", "place": "mark", "text": "Scrub night",
                                         "section": "Scrubs"}]}})
        self.assertEqual([t.title for t in app.tabs], ["Home", "Calendar", "Alerts", "Settings"])
        text = app.press("2")
        self.assertIn("planned through Sun 20 Dec", text)
        self.assertIn("! 1 problem   planned through", text)
        lines = self.lines()
        self.assertIn("│ 23 ↻     │", lines[18])
        self.assertIn("│ ○3 ○S    │", lines[19])         # both tools' chips on one day
        self.assertIn("└─ ○ planned ─ ✓ passed ─ ↻ scrub ─", lines[23])
        data = app.screen.data
        self.assertEqual([e["tool"] for e in data["days"][D(2026, 11, 23)]], ["alpha", "beta", "beta"])
        self.assertEqual(data["problems"], ["alpha: alpha is unsure"] + data["problems"][1:])
        app.press("right", "enter")
        text = self.term.text()
        self.assertIn(" Jobs\n ○ planned 3\n ○ planned S\n\n Scrubs\n ↻ Scrub night", text)
        self.assertIn(" Problems\n ! alpha: alpha is unsure", text)

    def test_no_calendar_hook_no_tab(self):
        shutil.rmtree(os.path.join(self.home, "alpha"))
        shutil.rmtree(os.path.join(self.home, "beta"))
        write_tool(self.home, "plain", PLUGIN.replace("def calendar", "def other"))
        tools, broken = P.discover(self.home)
        app = App(HeadlessTerminal(80, 28), tools, broken, clock=lambda: CLOCK, theme=Theme(True))
        self.assertEqual([t.title for t in app.tabs], ["Home", "Alerts", "Settings"])

    def test_one_tool_failing_is_a_problem_both_failing_is_the_screen(self):
        self.make_app(RuntimeError("boom"), {"days": {D(2026, 11, 23): [chip("planned", "3")]}})
        text = self.app.press("2")
        self.assertIn("○3", text)
        self.assertEqual(self.app.screen.data["problems"], ["alpha: boom"])
        self.make_app(RuntimeError("boom"), {"error": "UniFi Drive API problem"})
        text = self.app.press("2")
        self.assertIn("alpha: boom\n beta: UniFi Drive API problem\n\n Press r to try again.", text)
        self.app.press("right", "enter")                # nothing to open
        self.assertEqual(type(self.app.screen).__name__, "CalendarScreen")

    def test_a_bad_value_is_reported_not_drawn(self):
        self.make_app({"days": {"2026-11-23": []}}, {"days": {D(2026, 11, 23): [chip("planned", "3")]}})
        self.app.press("2")
        self.assertIn("alpha: calendar: day '2026-11-23' must be a date", self.app.screen.data["problems"])

    def test_today_defaults_to_the_clock(self):
        self.make_app({"days": {}}, {"days": {}})
        self.app.press("2")
        self.assertEqual(self.app.screen.sel, D(2026, 11, 22))
        self.assertNotIn("planned through", self.term.text())


class GridTests(CalendarFixture):
    def days(self):
        return {"today": D(2026, 11, 22), "days": {
            D(2026, 10, 22): [chip("ok", "1", past=True), chip("failed", "2", past=True), chip("ok", "3", past=True),
                              chip("ok", "4", past=True), chip("ok", "5", past=True)],
            D(2026, 12, 23): [{"status": "warning", "place": "flag", "text": "No test: busy", "section": "Skipped"},
                              {"status": None, "text": "A note"}],
            D(2026, 11, 22): [chip("running", "1")],
        }}

    def test_selected_day_and_today(self):
        self.make_app(self.days())
        self.app.press("2")
        lines = self.lines()
        self.assertIn("│ 21       ┃ 22 today ┃", lines[15])
        self.assertIn("┃ ▶1       ┃", lines[16])
        x = lines[15].index("22 today")
        self.assertEqual(self.term.surface.tone_at(15, x), "selected")
        self.assertEqual(self.term.surface.tone_at(15, x + 3), "accent")
        self.app.press("right")
        lines = self.lines()
        self.assertIn("│ 22 today │", lines[15])
        self.assertIn("┃ 23       ┃", lines[18])

    def test_five_week_month_has_a_day_panel(self):
        self.make_app(self.days())
        self.app.press("2", "pgup")                     # Thu 22 Oct
        lines = self.lines()
        self.assertEqual(lines[20], " └" + "┴".join(["─" * 10] * 7) + "┘")
        self.assertIn("┃ ✓1 ✗2 +3 ┃", lines[16])        # chips that fit; the rest counted
        self.assertEqual(self.term.surface.tone_at(16, lines[16].index("✗2")), "bad")
        self.assertEqual(self.term.surface.tone_at(16, lines[16].index("✓1")), "dim")
        self.assertIn(" Thursday 22 October", lines[21])
        self.assertEqual(lines[22], "   ✓ ok 1")
        self.assertEqual(lines[23], "   4 more: Enter for day details")
        self.assertIn("   Nothing on record.", self.app.press("right"))      # Fri 23 Oct
        text = self.app.press("pgdn", "pgdn")           # Wed 23 Dec: a flag, no chips
        lines = self.lines()
        self.assertIn("┃ !        ┃", lines[16])
        self.assertIn("   ! No test: busy\n   A note", text)
        self.assertIn("   Nothing planned.", self.app.press("right"))

    def test_wider_terminal_grows_the_cells(self):
        self.make_app(self.days(), width=120)
        self.app.press("2")
        lines = self.lines()
        cw = (118 - 1) // 7
        self.assertEqual(lines[5].index("┬") - lines[5].index("┌"), cw)

    def test_ascii(self):
        self.make_app(self.days())
        self.term.ascii = self.term.surface.ascii = True
        text = self.app.press("2")
        self.assertIn("| 21       # 22 today #", text)
        self.assertIn("+----------+----------+", text)
        body = text.split("\n", 3)[3].rsplit("\n", 4)[0]
        self.assertTrue(all(ord(ch) < 128 for ch in body))


class DetailTests(unittest.TestCase):
    def test_detail_rows(self):
        data = cal.gather([("t", lambda: {"until": D(2026, 11, 25), "until_note": "The plan stops here.", "days": {
            D(2026, 11, 23): [chip("planned", "3", "Drive 3 at 01:00", detail="Drive 3  MODEL  SERIAL3",
                                   notes=["why: due"]),
                              {"status": None, "text": "Drive 6 snoozed", "detail": "Drive 6: until Wed",
                               "section": "Snoozed"}]}})], CLOCK)
        self.assertEqual(cal.detail_rows(data, D(2026, 11, 23)), [
            ("heading", "Jobs"), ("status", "planned", "Drive 3  MODEL  SERIAL3", False), ("note", "why: due"), ("",),
            ("heading", "Snoozed"), ("value", "Drive 6: until Wed")])
        self.assertEqual(cal.day_rows(data, D(2026, 11, 23)),
                         [("status", "planned", "Drive 3 at 01:00", False), ("label", "Drive 6 snoozed")])
        self.assertEqual(cal.detail_rows(data, D(2026, 11, 30)), [("label", "The plan stops here.")])
        self.assertEqual(cal.day_rows(data, D(2026, 11, 30)), [("label", "Beyond the plan, which ends Wed 25 Nov.")])
        self.assertEqual(cal.detail_rows(data, D(2026, 11, 24)), [("label", "Nothing on this day.")])

    def test_shift_month_clamps(self):
        self.assertEqual(cal.shift_month(D(2027, 1, 31), 1), D(2027, 2, 28))
        self.assertEqual(cal.shift_month(D(2026, 1, 15), -1), D(2025, 12, 15))


if __name__ == "__main__":
    unittest.main()
