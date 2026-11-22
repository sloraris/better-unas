"""Planner tests: night-by-night simulation against the anonymized fixture.

    python3 -m unittest discover -s tests

In the fixture, slot 6 has attribute 198 at 14 (warning, 30-day interval),
slots 4, 5 and 7 are past OLD_DRIVE_HOURS (60 days), slots 1-3 are healthy
(90 days) and slots 1 and 2 are busy copying. No drive has a test on record,
so all of them are due for a baseline. The scrub runs on the last day of each
month at 02:30-07:00 (next: Mon 30 Nov). Nothing touches a NAS.
"""

import datetime as dt
import os
import tempfile
import unittest

from test_scheduling import NIGHT, TZ, Fake, ext, ss

planner = ss.planner


def at(*a):
    return dt.datetime(*a, tzinfo=TZ)


def config(**values):
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
        f.write("".join(f"{k}={v}\n" for k, v in values.items()))
    try:
        return ss.Config(f.name)
    finally:
        os.unlink(f.name)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.fake.install()
        self.fake.attrs["SERIAL6"] = {"198": 14}
        self.cfg = ss.Config("/nonexistent")
        self.state = {}
        self.addCleanup(setattr, ss, "now", ss.now)

    def drives(self, t=NIGHT):
        ss.now = lambda: t
        alerts = ss.Alerts(self.cfg, self.state, True)
        return ss.evaluate_drives(self.cfg, self.fake.storage, "uid", self.state, alerts, report=False)

    def plan(self, t=NIGHT, **kw):
        return planner.plan(self.fake.storage, self.drives(t), self.state, self.cfg, t, **kw)

    def test_multi_night_plan_from_fixture(self):
        p = self.plan()
        self.assertEqual([(e["night"], e["serial"]) for e in p], [
            ("2026-11-22", "SERIAL6"),   # warning drive first; tonight, starting now
            ("2026-11-23", "SERIAL4"),   # old drives (60 days) next
            ("2026-11-24", "SERIAL5"),
            ("2026-11-25", "SERIAL7"),
            ("2026-11-26", "SERIAL1"),   # healthy drives (90 days) last, in slot order
            ("2026-11-27", "SERIAL2"),
            ("2026-11-28", "SERIAL3"),
        ])
        self.assertEqual(p[0]["start"], NIGHT)
        for e in p:
            self.assertTrue(ss.in_window(self.cfg, e["start"]))
            self.assertEqual(e["end"] - e["start"], dt.timedelta(minutes=571 * 1.5))
            self.assertEqual(e["reason"], "never tested; first test sets a baseline")
        self.assertEqual(p[1]["delayed_by"], ["waiting its turn (1 start(s) per night, most urgent first)"])
        self.assertEqual(p.skipped, [])

    def test_hourly_run_starts_what_the_plan_puts_first(self):
        drives = self.drives()
        decision = planner.decide(self.cfg, self.fake.storage, drives, self.state, NIGHT)
        self.assertEqual(decision.start["serial"], "SERIAL6")
        self.assertEqual(decision.start["serial"], self.plan()[0]["serial"])

    def test_busy_drives_wait_only_tonight(self):
        self.fake.attrs["SERIAL6"] = {}
        for disk in self.fake.storage["disks"]:   # make every drive young and healthy
            disk["powerOnHours"] = 100
        p = self.plan()
        self.assertEqual(p[0]["serial"], "SERIAL3")   # 1 and 2 are busy right now
        self.assertEqual([e["serial"] for e in p[1:3]], ["SERIAL1", "SERIAL2"])
        self.assertIn("drive busy (160128 KB/s)", p[1]["delayed_by"])

    def test_scrub_night_is_skipped(self):
        p = self.plan(at(2026, 11, 26, 1, 0))
        nights = [e["night"] for e in p]
        self.assertNotIn("2026-11-30", nights)
        self.assertEqual(nights[-3:], ["2026-12-01", "2026-12-02", "2026-12-03"])
        self.assertEqual(p.skipped, [{"night": "2026-11-30",
                                      "reasons": ["would overlap pool 2 scrub at Mon 30 Nov 02:30"]}])
        self.assertIn("would overlap pool 2 scrub at Mon 30 Nov 02:30", p[4]["delayed_by"])

    def test_long_test_ending_inside_scrub_margin_skips_the_night_before(self):
        # 1000 min x 1.5 = 25 h: from the 29th at 00:00 it would end inside the 2 h margin.
        self.fake.storage["disks"] = [d for d in self.fake.storage["disks"] if d["serial"] == "SERIAL3"]
        ss.smart_facts = lambda dev, ids: (1000, {})
        ss.self_test_progress = lambda dev: None
        p = self.plan(at(2026, 11, 29, 0, 0))
        self.assertEqual(p[0]["night"], "2026-12-01")
        self.assertEqual([s["night"] for s in p.skipped], ["2026-11-29", "2026-11-30"])

    def test_later_scrubs_are_projected_from_the_schedule(self):
        runs = planner.scrub_runs(self.fake.storage["pools"][0]["dataScrubbing"], at(2027, 3, 1))
        self.assertEqual([r.date() for r in runs],
                         [dt.date(2026, 11, 30), dt.date(2026, 12, 31), dt.date(2027, 1, 31), dt.date(2027, 2, 28)])

    def test_warning_drive_goes_first_and_comes_back_sooner(self):
        for slot in "1234567":
            self.fake.history[slot] = [ext(1, "successful", "optimal", "2026-10-01T09:00:00-07:00")]
        self.fake.history["3"] = [ext(1, "successful", "optimal", "2026-10-05T09:00:00-07:00")]
        self.fake.attrs["SERIAL3"] = {"5": 8}   # a second warning drive
        p = self.plan(horizon=at(2027, 2, 1))
        first = [e["serial"] for e in p[:2]]
        # Both 30-day drives come before every other overdue drive; slot 6 has waited longer.
        self.assertEqual(first, ["SERIAL6", "SERIAL3"])
        self.assertEqual(p[0]["interval"], 30)
        # The planned test counts as passed, so the next one is 30 days after it ends.
        again = [e for e in p if e["serial"] == "SERIAL6"]
        self.assertEqual(again[1]["due"], again[0]["end"] + dt.timedelta(days=30))
        self.assertEqual(again[1]["reason"], "every 30 days (SMART attribute 198 is 14)")

    def test_horizon_stops_once_every_drive_has_a_test(self):
        p = self.plan()
        self.assertEqual(sorted(e["serial"] for e in p), [f"SERIAL{i}" for i in range(1, 8)])
        self.cfg = config(EXCLUDE_SERIALS="SERIAL3")
        p = self.plan()
        self.assertEqual(len(p), 6)
        self.assertNotIn("SERIAL3", [e["serial"] for e in p])

    def test_horizon_waits_for_drives_not_due_yet(self):
        self.fake.history["3"] = [ext(1, "successful", "optimal", "2026-11-20T09:00:00-07:00")]
        p = self.plan()
        self.assertEqual(p[-1]["serial"], "SERIAL3")
        self.assertEqual(p[-1]["night"], "2027-02-19")   # 90 days after 20 Nov, 09:00
        self.assertEqual(p[-1]["reason"], "every 90 days (healthy)")

    def test_explicit_horizon(self):
        p = self.plan(horizon=at(2026, 11, 24, 12, 0))
        self.assertEqual([e["night"] for e in p], ["2026-11-22", "2026-11-23", "2026-11-24"])

    def test_paused_yields_no_plan(self):
        self.cfg = config(MAX_STARTS_PER_NIGHT=0)
        p = self.plan()
        self.assertEqual(list(p), [])
        self.assertEqual(p.skipped[0]["reasons"], ["paused (MAX_STARTS_PER_NIGHT=0 in config)"])

    def test_several_starts_per_night(self):
        self.cfg = config(MAX_STARTS_PER_NIGHT=2, MAX_CONCURRENT=2)
        p = self.plan(at(2026, 11, 22, 0, 0))
        self.assertEqual([e["night"] for e in p[:4]], ["2026-11-22", "2026-11-22", "2026-11-23", "2026-11-23"])
        self.assertEqual([e["start"].hour for e in p[:2]], [0, 1])   # one start per hourly tick

    def test_concurrency_limit_holds_back_a_second_start(self):
        self.cfg = config(MAX_STARTS_PER_NIGHT=2)   # MAX_CONCURRENT stays 1, tests take ~14 h
        p = self.plan(at(2026, 11, 22, 0, 0))
        self.assertEqual(len({e["night"] for e in p}), len(p))
        self.assertIn("a SMART test is already running", p[1]["delayed_by"])

    def test_tests_already_started_tonight_count(self):
        self.state["starts"] = {"2026-11-22": 1}
        p = self.plan()
        self.assertEqual(p[0]["night"], "2026-11-23")

    def test_running_test_blocks_tonight_and_moves_that_drive_back(self):
        self.fake.history["6"] = [ext(9, "running", None, None, "2026-11-22T00:10:00-07:00")]
        p = self.plan()
        self.assertEqual(p[0]["night"], "2026-11-23")
        self.assertEqual(p.skipped, [{"night": "2026-11-22", "reasons": ["a SMART test is already running"]}])
        six = [e for e in p if e["serial"] == "SERIAL6"][0]
        # Assumed to end ~14 h after now (15:16 on the 22nd); due 30 days later, so the next night.
        self.assertEqual(six["night"], "2026-12-23")

    def test_unhealthy_pool_blocks_tonight_only(self):
        self.fake.storage["pools"][0]["status"] = "degraded"
        p = self.plan()
        self.assertEqual(p[0]["night"], "2026-11-23")
        self.assertEqual(p.skipped, [{"night": "2026-11-22", "reasons": ["pool 2 is degraded"]}])

    def test_extra_blocking_windows(self):
        backup = planner.Window(at(2026, 11, 23, 2, 0), at(2026, 11, 24, 6, 0), "cloud backup at Mon 23 Nov 02:00",
                                "backup")
        p = self.plan(windows=[backup])
        self.assertEqual([e["night"] for e in p[:3]], ["2026-11-22", "2026-11-25", "2026-11-26"])
        self.assertEqual([s["night"] for s in p.skipped], ["2026-11-23", "2026-11-24", "2026-11-30"])
        self.assertIn("would overlap cloud backup at Mon 23 Nov 02:00", p[1]["delayed_by"])
        drives = self.drives(at(2026, 11, 23, 0, 0))
        self.assertIsNone(planner.decide(self.cfg, self.fake.storage, drives, {}, at(2026, 11, 23, 0, 0),
                                         windows=[backup]).start)

    def test_not_before_dates(self):
        p = self.plan(not_before={"SERIAL6": (at(2026, 11, 25), "snoozed until Wed 25 Nov")})
        six = [e for e in p if e["serial"] == "SERIAL6"][0]
        self.assertEqual(six["night"], "2026-11-25")
        self.assertIn("snoozed until Wed 25 Nov", six["delayed_by"])
        self.assertEqual(p[0]["serial"], "SERIAL4")


if __name__ == "__main__":
    unittest.main()
