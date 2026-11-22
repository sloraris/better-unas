"""Calendar screen and plugin module (#10). Run from the smart-scheduler folder:

    python3 -m unittest discover -s tests

The whole TUI runs headless (the core's text canvas, 80x28) with the tool's
real plugin, planner and backup code. UniFi Drive's API, smartctl, psql and
systemctl are faked as in test_scheduling, so nothing touches a NAS.

The clock is Sun 22 Nov 2026 01:00 (-07:00), inside the default start window.
Fixture facts (see test_planner): slot 6 has a warning attribute (30 days),
slots 4, 5, 7 are old (60 days), slots 1-3 healthy (90 days); the pool scrubs
on the last day of each month at 02:30 (next Mon 30 Nov).

Snapshots are in tests/snapshots/. After an intended change, regenerate and
review the diff:

    UPDATE_SNAPSHOTS=1 python3 -m unittest discover -s tests
"""

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

from test_scheduling import NIGHT, TZ, Fake, ext, rose6, ss

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.normpath(os.path.join(HERE, ".."))
ROOT = os.path.dirname(TOOL)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import plugins as P                    # noqa: E402
from core.tui import widgets as W                # noqa: E402
from core.tui.app import App                     # noqa: E402
from core.tui.terminal import HeadlessTerminal   # noqa: E402
from core.tui.theme import Theme                 # noqa: E402

SNAPSHOTS = os.path.join(HERE, "snapshots")


def at(*a):
    return dt.datetime(*a, tzinfo=TZ)


def patch(test, name, value):
    test.addCleanup(setattr, ss, name, getattr(ss, name))
    setattr(ss, name, value)


BACKUP_TASK = {"id": "task1", "name": "Offsite", "remote_type": "ucs", "enabled": True, "time": "01:00",
               "weekdays": "1", "monthdays": "", "interval": "", "bw_limit": ""}   # Mondays 01:00


class CalendarFixture:
    def setUp(self):
        self.fake = Fake()
        self.fake.install()
        self.fake.attrs["SERIAL6"] = {"198": 14}
        self.state = rose6()
        self.backups = {"tasks": [], "runs": [], "problems": []}
        patch(self, "now", lambda: NIGHT)
        patch(self, "load_state", lambda: json.loads(json.dumps(self.state)))
        patch(self, "resolve_identity", lambda cfg: ("uid", "the console owner", None))
        patch(self, "read_backups", lambda cfg, tz: self.backups)
        patch(self, "trigger_problems", lambda: [])
        patch(self, "psql", lambda *a: self.fail("psql must not run in tests"))
        # A private copy of the tool, so its config is ours and nothing lands in the repo.
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        self.tool_dir = os.path.join(self.home, "smart-scheduler")
        shutil.copytree(TOOL, self.tool_dir, ignore=shutil.ignore_patterns(
            "tests", "__pycache__", "config", "state.json", ".lock", "manifest.sha256"))
        self.addCleanup(W.reset_editing)

    def write_config(self, text):
        with open(os.path.join(self.tool_dir, "config"), "w") as f:
            f.write(text)

    def make_app(self):
        tool = P.load_tool(self.tool_dir)
        self.term = HeadlessTerminal(80, 28)
        app = App(self.term, [tool], {}, clock=lambda: NIGHT, theme=Theme(True),
                  log_path=os.path.join(self.home, "tui.log"))
        self.plugin = app.plugins["smart-scheduler"]
        self.plugin.ss = ss          # the faked module (the copy's own is real)
        self.ctx = app.contexts["smart-scheduler"]
        app.start()
        app.draw()
        return app

    def calendar(self, *keys):
        """Open the Calendar tab (the 2nd) and press keys; returns the screen text."""
        self.app = self.make_app()
        return self.app.press("2", *keys)

    @property
    def screen(self):
        return self.app.screen

    def planned(self):
        return sorted((d.isoformat(), e["serial"]) for d, v in self.screen.data["days"].items()
                      for e in v["planned"])

    def assertSnapshot(self, name, text):
        path = os.path.join(SNAPSHOTS, name + ".txt")
        if os.environ.get("UPDATE_SNAPSHOTS"):
            os.makedirs(SNAPSHOTS, exist_ok=True)
            with open(path, "w") as f:
                f.write(text + "\n")
        if not os.path.exists(path):
            self.fail(f"no snapshot {name}; run with UPDATE_SNAPSHOTS=1 to create it")
        with open(path) as f:
            want = f.read()[:-1]
        self.assertEqual(text, want, f"snapshot {name} differs; UPDATE_SNAPSHOTS=1 regenerates it")


def with_history(fake):
    """Slot 3 passed early in November, slot 4 failed, slot 5 passed in October,
    and slot 1's test is running right now."""
    fake.history["3"] = [ext(31, "successful", "optimal", "2026-11-03T10:30:00-07:00", "2026-11-03T01:00:00-07:00")]
    fake.history["4"] = [ext(41, "failed", "failed", "2026-11-12T03:10:00-07:00", "2026-11-12T01:00:00-07:00")]
    fake.history["5"] = [ext(51, "successful", "optimal", "2026-10-20T11:00:00-07:00", "2026-10-20T01:00:00-07:00")]
    fake.history["1"] = [ext(11, "running", None, None, "2026-11-22T00:10:00-07:00")]


class CalendarScreenTests(CalendarFixture, unittest.TestCase):
    def test_test_running_in_drive_shows_with_progress(self):
        disk = next(d for d in self.fake.storage["disks"] if str(d["slotId"]) == "4")
        disk["smartTest"] = {"id": 4, "type": "extended", "status": "inProgress", "result": "",
                             "createdAt": "2026-11-21T13:14:43-07:00", "finishedAt": None, "progress": 80}
        self.calendar()
        tests = self.screen.data["days"][dt.date(2026, 11, 21)]["tests"]
        self.assertEqual([(x["slot"], x["outcome"], x["left"]) for x in tests], [("4", "running", 20)])

    def test_test_started_outside_drive_shows_as_running_today(self):
        # Nothing in Drive's history; the drive itself reports a test 10% done,
        # so it runs past tonight's window and holds the night.
        self.fake.progress["SERIAL4"] = 90
        text = self.calendar()
        today = self.screen.data["days"][dt.date(2026, 11, 22)]
        self.assertEqual([(x["slot"], x["outcome"]) for x in today["tests"]], [("4", "running")])
        self.assertIn("a SMART test is already running", today["skipped"])
        self.assertIn("▶ Slot 4  test running outside UniFi Drive, 10% done", text)
        self.assertIn("! No test tonight: one is already running", text)
        self.assertNotIn("SERIAL4", {s for n, s in self.planned() if n == "2026-11-22"})

    def test_month_with_history_and_plan(self):
        with_history(self.fake)
        text = self.calendar()
        self.assertSnapshot("calendar_history_plan", text)
        self.assertSnapshot("calendar_history_plan_tones", self.term.tones())
        days = self.screen.data["days"]
        self.assertEqual([x["outcome"] for x in days[dt.date(2026, 11, 3)]["tests"]], ["passed"])
        self.assertEqual([x["outcome"] for x in days[dt.date(2026, 11, 12)]["tests"]], ["failed"])
        self.assertEqual([x["outcome"] for x in days[dt.date(2026, 11, 22)]["tests"]], ["running"])
        # Slot 1's running test holds tonight; the plan starts tomorrow, most urgent first.
        self.assertIn("a SMART test is already running", days[dt.date(2026, 11, 22)]["skipped"])
        self.assertEqual(self.planned()[0], ("2026-11-23", "SERIAL6"))
        # The plan runs until every drive has a test (slot 3's is in February);
        # short-interval drives come round again before that.
        self.assertEqual({s for _, s in self.planned()}, {f"SERIAL{i}" for i in range(1, 8)})
        self.assertEqual(self.screen.data["plan_until"], dt.date(2027, 2, 21))
        self.assertEqual(self.planned()[-1], ("2027-02-21", "SERIAL1"))
        lines = text.splitlines()
        self.assertIn("✓3", lines[9])                          # chips: a glyph and the slot
        self.assertIn("✗4", lines[11])
        self.assertIn("[22] today", lines[12])                 # selected: brackets; today: tagged
        self.assertIn("▶1", lines[13])
        self.assertIn("○6         ○4         ○7         ○2", lines[15])
        self.assertIn("planned through Sun 21 Feb 2027", lines[4])   # the year: not this one
        self.assertRegex(lines[18], r"^ ── ○ planned  ✓ passed  ✗ failed  ▶ running  ↻ scrub  ⇡ backup ─+$")
        self.assertIn("Sunday 22 November", lines[19])
        self.assertIn("▶ Slot 1  extended test running, started 00:10", text)
        self.assertNotIn("S1", text)
        x = lines[12].index("[22]")
        self.assertEqual(self.term.surface.tone_at(12, x + 1), "selected")   # today's number is reversed
        self.assertEqual(self.term.surface.tone_at(18, 4), "dim")            # the legend is dim

    def test_scrub_day_blocks_the_night(self):
        # Slot 3 is snoozed until the scrub day, so the scrub is what holds it next.
        self.state["snoozes"] = {"SERIAL3": {"until": "2026-11-30", "set": "2026-11-20T10:00:00-07:00"}}
        self.calendar("down", "right")
        self.assertEqual(self.screen.sel, dt.date(2026, 11, 30))
        self.assertSnapshot("calendar_scrub", self.term.text())
        days = self.screen.data["days"]
        self.assertEqual([b["kind"] for b in days[dt.date(2026, 11, 30)]["blocks"]], ["scrub"])
        self.assertEqual(days[dt.date(2026, 11, 30)]["planned"], [])
        self.assertTrue(any("scrub" in r for r in days[dt.date(2026, 11, 30)]["skipped"]))
        self.assertIn(("2026-12-01", "SERIAL3"), self.planned())
        text = self.app.press("enter")
        self.assertSnapshot("calendar_scrub_details", text)
        self.assertIn("No test this night\n ! scrub on pool 2 at 02:30", text)
        self.assertIn("   margin), and no test starts again that night.", text)   # long rows wrap
        self.assertIn("Pool 2 data scrub starts 02:30", text)
        self.assertIn("Esc back", text)
        self.app.press("esc")
        self.assertIs(type(self.app.screen).__name__, "CalendarScreen")

    def test_backup_window_without_history_skips_the_night(self):
        self.backups = {"tasks": [BACKUP_TASK], "problems": [], "runs": []}
        self.calendar()
        monday = self.screen.data["days"][dt.date(2026, 11, 23)]
        self.assertEqual(monday["planned"], [])    # default 4 h + margins: 00:00-06:00
        self.assertEqual(monday["skipped"], ["would overlap UniFi Cloud backup “Offsite” at Mon 23 Nov 01:00"])
        text = self.app.press("right")
        self.assertIn("  !          ○4", text)                   # the cell's chip line: a skipped night
        self.assertIn("! No test this night: overlaps UniFi Cloud backup “Offsite”", text)

    def test_backup_window(self):
        self.backups = {"tasks": [BACKUP_TASK], "problems": [], "runs": [
            {"task_id": "task1", "started": at(2026, 11, 16, 1, 0), "finished": at(2026, 11, 16, 1, 50),
             "status": "succeeded"}]}
        text = self.calendar("right")
        self.assertEqual(self.screen.sel, dt.date(2026, 11, 23))
        self.assertSnapshot("calendar_backup", text)
        self.assertIn(" [23] ⇡ ", text)
        days = self.screen.data["days"]
        # The last run took 50 minutes, so the window (with its 1 h margin) is
        # 00:00-02:50 and Monday's test waits until 03:00.
        monday = days[dt.date(2026, 11, 23)]["planned"]
        self.assertEqual([(e["serial"], e["start"]) for e in monday], [("SERIAL4", at(2026, 11, 23, 3, 0))])
        self.assertIn("would overlap UniFi Cloud backup “Offsite” at Mon 23 Nov 01:00", monday[0]["delayed_by"])
        self.assertEqual([b["past"] for b in days[dt.date(2026, 11, 16)]["blocks"]], [True])
        self.assertTrue(days[dt.date(2026, 11, 30)]["blocks"])    # the next Monday is marked too
        text = self.app.press("enter")
        self.assertSnapshot("calendar_backup_details", text)
        self.assertIn("UniFi Cloud backup “Offsite” at Mon 23 Nov 01:00", text)

    def test_snoozed_drive(self):
        self.state["snoozes"] = {"SERIAL6": {"until": "2026-11-25", "note": "waiting for a spare",
                                             "set": "2026-11-21T10:00:00-07:00"}}
        self.assertIn("Slot 6 snoozed to Wed 25 Nov (waiting for a spare)", self.calendar())
        text = self.app.press("right", "right", "right")
        self.assertSnapshot("calendar_snoozed", text)
        self.assertNotIn("snoozed to", text)                   # the 25th is the day it may test again
        first = dict((s, d) for d, s in self.planned())
        self.assertEqual(first["SERIAL6"], "2026-11-25")
        self.assertEqual(self.planned()[0], ("2026-11-22", "SERIAL4"))
        text = self.app.press("enter")
        self.assertSnapshot("calendar_snoozed_details", text)
        self.assertIn("waited for: snoozed until Wed 25 Nov 2026 (waiting for a spare)", text)

    def test_month_navigation(self):
        with_history(self.fake)
        self.calendar("pgdn")
        self.assertSnapshot("calendar_december", self.term.text())
        self.assertIn("December 2026", self.term.text())
        self.assertEqual(self.screen.sel, dt.date(2026, 12, 22))
        text = self.app.press("pgup", "pgup")
        self.assertIn("October 2026", text)
        self.assertSnapshot("calendar_october", text)
        self.assertIn("✓5", text)                     # slot 5's October test
        self.assertIn("No test on record.", self.app.press("left"))   # 21 Oct
        # Arrows cross month edges; t comes back to today.
        self.app.press("t")
        self.assertEqual(self.screen.sel, dt.date(2026, 11, 22))
        self.app.press("down", "down")
        self.assertEqual(self.screen.sel, dt.date(2026, 12, 6))
        self.assertIn("December 2026", self.term.text())
        self.app.press("pgdn", "pgdn")
        self.assertEqual(self.screen.sel, dt.date(2027, 2, 6))
        self.app.press("home")
        self.assertEqual(self.screen.sel, dt.date(2026, 11, 22))

    def test_ascii_terminal(self):
        with_history(self.fake)
        self.app = self.make_app()
        self.term.ascii = self.term.surface.ascii = True
        text = self.app.press("2")
        self.assertIn("+3", text)
        self.assertIn("x4", text)
        self.assertIn("o6         o4", text)
        self.assertIn("-- o planned  + passed  x failed  > running  s scrub  b backup --", text)
        self.assertTrue(all(ord(ch) < 128 for ch in text.split("\n", 2)[2].rsplit("\n", 3)[0]))

    def test_pgdn_clamps_the_day(self):
        self.calendar()
        self.screen.sel = dt.date(2027, 1, 31)
        self.app.press("pgdn")
        self.assertEqual(self.screen.sel, dt.date(2027, 2, 28))

    def test_recalculated_each_time_it_opens(self):
        self.calendar()
        self.assertEqual(self.planned()[0], ("2026-11-22", "SERIAL6"))
        self.state["snoozes"] = {"SERIAL6": {"until": "2026-12-01", "set": "2026-11-21T10:00:00-07:00"}}
        self.app.press("1", "2")
        self.assertEqual(self.planned()[0], ("2026-11-22", "SERIAL4"))
        self.fake.history["4"] = [ext(41, "successful", "optimal", "2026-11-21T10:00:00-07:00")]
        self.app.press("r")
        self.assertNotIn(("2026-11-22", "SERIAL4"), self.planned())

    def test_api_problem_is_shown_not_raised(self):
        self.fake.api = lambda *a, **k: (500, None)
        ss.api = self.fake.api
        text = self.calendar()
        self.assertIn("UniFi Drive API problem: GET /storage returned HTTP 500", text)
        self.assertIn("Press r to try again.", text)
        self.app.press("right", "pgdn", "enter")      # still navigable, nothing to open

    def test_problems_are_listed(self):
        self.backups = {"tasks": [], "runs": [], "problems": [("backups:read", "Couldn't read UniFi Drive's backup tasks: psql failed")]}
        text = self.calendar()
        self.assertIn("! Couldn't read UniFi Drive's backup tasks", text)

    def test_settings_preview_uses_unsaved_values_only(self):
        self.write_config("MAX_STARTS_PER_NIGHT=1\n")
        self.app = self.make_app()
        values = dict(self.ctx.config())
        self.assertEqual(values["MAX_STARTS_PER_NIGHT"], 1)
        values["MAX_STARTS_PER_NIGHT"] = values["MAX_CONCURRENT"] = 2
        self.ctx.push(self.plugin.preview_calendar(self.ctx, values))
        self.app.draw()
        self.assertIn("Calendar preview (unsaved settings)", self.term.text())
        tonight = self.app.screen.data["days"][dt.date(2026, 11, 22)]["planned"]
        self.assertEqual([e["serial"] for e in tonight], ["SERIAL6", "SERIAL4"])
        self.app.press("r")                             # recalculating keeps the preview values
        self.assertEqual(len(self.app.screen.data["days"][dt.date(2026, 11, 22)]["planned"]), 2)
        # The core's own preview reaches the normal calendar the same way.
        with self.ctx.preview(values):
            self.app.press("2")
        self.assertEqual(len(self.app.screen.data["days"][dt.date(2026, 11, 22)]["planned"]), 2)
        self.app.press("r")                             # outside the preview: the saved file again
        self.assertEqual(len(self.app.screen.data["days"][dt.date(2026, 11, 22)]["planned"]), 1)

    def test_narrow_cells_show_how_many_more(self):
        self.write_config("MAX_STARTS_PER_NIGHT=4\nMAX_CONCURRENT=4\n")
        text = self.calendar()
        row = next(line for line in text.splitlines() if "○6" in line)
        self.assertIn("○6 ○4 +2", row)


class PluginTests(CalendarFixture, unittest.TestCase):
    def test_contract_check_passes(self):
        tool = P.load_tool(self.tool_dir)
        mod = P.load_plugin(tool.path, tool.manifest)
        self.assertEqual(mod.CONTRACT, 1)

    def test_data_api_is_contract_correct(self):
        with_history(self.fake)
        self.state["alerts"] = {"test:SERIAL4:41": {"msg": "Extended SMART test on slot 4 failed.", "level": "error",
                                                    "sent": "2026-11-21T01:00:00-07:00",
                                                    "since": "2026-11-12T03:10:00-07:00"},
                                "backups:read": {"msg": "Couldn't read backups.", "level": "warning",
                                                 "sent": "2026-11-21T01:00:00-07:00"}}
        self.state["last_tick"] = "2026-11-22T00:00:00-07:00"
        self.state["snoozes"] = {"SERIAL6": {"until": "2026-11-25", "set": "2026-11-21T10:00:00-07:00"}}
        self.make_app()
        ctx = self.ctx
        st = self.plugin.status(ctx)
        self.assertEqual(P.check_status(st), [])
        self.assertEqual((st["state"], st["detail"]), ("error", ["Last run Sun 22 Nov 00:00"]))
        sm = self.plugin.summary(ctx)
        self.assertEqual(P.check_summary(sm), [])
        self.assertEqual(sm["headline"], "7 drives scheduled, 4 due now")
        self.assertEqual({i["label"]: i["value"] for i in sm["items"]}, {
            "Running": "slot 1 (SERIAL1)",
            "Next test": "Mon 23 Nov, slot 4 (SERIAL4)",
            "Last result": "failed, slot 4, Thu 12 Nov",
            "Next scrub": "Mon 30 Nov, pool 2 scrub 02:30",
            "Snoozed": "slot 6 until Wed 25 Nov",
        })
        by = {i["label"]: i for i in sm["items"]}
        self.assertEqual((by["Last result"]["target"], by["Last result"]["target_label"], by["Last result"]["status"]),
                         ("Drives:SERIAL4", "slot 4", "failed"))
        self.assertEqual(by["Running"]["target"], "Drives:SERIAL1")
        self.assertNotIn("target", by["Next test"])          # the card itself opens the Calendar
        al = self.plugin.alerts(ctx)
        self.assertEqual(P.check_alerts(al), [])
        self.assertEqual([(a["id"], a["level"]) for a in al], [("backups:read", "warn"), ("test:SERIAL4:41", "error")])
        self.assertEqual(al[0]["since"], "2026-11-21T01:00:00-07:00")
        self.assertEqual(P.check_screens(self.plugin.screens(ctx)), [])

    def test_home_last_result_opens_that_drive(self):
        with_history(self.fake)
        self.app = self.make_app()
        self.app.press("1")
        home = self.app.screen
        self.assertEqual([st.target for st in home.stops][-2:], ["Drives:SERIAL1", "Drives:SERIAL4"])
        text = self.app.press("end")
        self.assertIn("Enter open ", text.split("\n")[25])
        text = self.app.press("enter")
        self.assertEqual(self.app.tab.title, "Drives")
        self.assertIn("└─ Slot 4 ", text.split("\n")[2])
        self.assertNotIn("Slot", self.app.press("esc").split("\n")[2])   # back to the list

    def test_quiet_nas_is_ok(self):
        self.make_app()
        st = self.plugin.status(self.ctx)
        self.assertEqual(st["state"], "ok")
        self.assertEqual(self.plugin.alerts(self.ctx), [])
        self.assertEqual(self.plugin.summary(self.ctx)["state"], "ok")

    def test_turned_off(self):
        self.write_config("ENABLED=no\n")
        self.make_app()
        self.assertEqual(self.plugin.status(self.ctx)["state"], "off")
        self.assertEqual(self.plugin.summary(self.ctx), {"state": "off", "headline": "Turned off (ENABLED=no)",
                                                         "items": []})

    def test_summary_reports_api_problem(self):
        self.make_app()
        ss.api = lambda *a, **k: (500, None)
        sm = self.plugin.summary(self.ctx)
        self.assertEqual(P.check_summary(sm), [])
        self.assertEqual(sm["state"], "error")
        self.assertLessEqual(len(sm["headline"]), 60)

    def test_validate(self):
        self.make_app()
        self.assertEqual(self.plugin.validate({"INTERVAL_DAYS": 90, "WARN_INTERVAL_DAYS": 30,
                                               "OLD_INTERVAL_DAYS": 60}), {})
        self.assertEqual(set(self.plugin.validate({"INTERVAL_DAYS": 20, "WARN_INTERVAL_DAYS": 30,
                                                   "OLD_INTERVAL_DAYS": 60})),
                         {"WARN_INTERVAL_DAYS", "OLD_INTERVAL_DAYS"})

    def test_config_from_typed_values_matches_the_file_loader(self):
        self.make_app()
        cal = self.plugin.calendar_screen
        tool = P.load_tool(self.tool_dir)
        raw = {"START_WINDOW": "22:00-04:00", "INTERVAL_OVERRIDES": "SERIAL1:30", "WARN_ATTRIBUTES": "5,197",
               "DURATION_FACTOR": "2.5", "BACKUP_AVOID": "no"}
        typed, errors = P.validate_config(tool.schema, raw)
        self.assertEqual(errors, {})
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("".join(f"{k}={v}\n" for k, v in raw.items()))
        self.addCleanup(os.unlink, f.name)
        want = ss.Config(f.name)
        got = cal.config_from(ss, typed)
        own = lambda cfg: {k: cfg.values[k] for k in ss.DEFAULTS}     # the shared keys aside
        self.assertEqual(got.values, own(want))
        self.assertEqual((got.window, got.overrides), (want.window, want.overrides))
        self.assertEqual(cal.config_from(ss, {}).values, own(ss.Config("/nonexistent")))


if __name__ == "__main__":
    unittest.main()
