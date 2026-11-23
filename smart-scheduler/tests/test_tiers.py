"""Tiers (core/lib/busy.py): urgent tests start at once, other tools' jobs
are avoided or ignored by tier, and a running test gives way to a higher
tier. Run from the smart-scheduler folder:

    python3 -m unittest discover -s tests

Every drive passed an extended test on Fri 20 Nov, so nothing is due unless
a test makes it so. Drive 6's offline-uncorrectable count rose from 10 to 14
since then: warning signs, so its test is urgent.
"""

import datetime as dt
import io
import unittest
from contextlib import redirect_stdout

from test_scheduling import TZ, Fake, ext, ss
from core.lib import busy

NOON = dt.datetime(2026, 11, 22, 14, 0, tzinfo=TZ)     # outside the 00:00-05:00 start window


def patch(test, obj, name, value):
    test.addCleanup(setattr, obj, name, getattr(obj, name))
    setattr(obj, name, value)


class TierTests(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.fake.install()
        for d in self.fake.storage["disks"]:
            d["smartTest"] = None
            self.fake.history[str(d["slotId"])] = [
                ext(int(d["slotId"]) * 10, "successful", "optimal", "2026-11-20T10:00:00-07:00",
                    "2026-11-20T01:00:00-07:00")]
        self.fake.attrs["SERIAL6"] = {"198": 14}
        self.cfg = ss.Config("/nonexistent")
        self.state = {"drives": {"SERIAL6": {"attrs_at_last_test": {"198": 10}}}}
        self.others, self.nudged, self.commands = [], [], []
        patch(self, ss, "backup_windows", lambda *a, **k: ([], None))
        patch(self, ss, "self_check", lambda *a, **k: (self.fake.storage, "uid"))
        patch(self, ss, "load_state", lambda: self.state)
        patch(self, ss, "save_state", lambda state, dry: None)
        patch(self, ss, "sh", lambda *cmd, **k: (self.commands.append(cmd), (0, ""))[1])
        patch(self, ss.TOOL, "others_busy", lambda t: list(self.others))
        patch(self, ss.TOOL, "nudge", lambda tools, dry=False: self.nudged.extend(tools))
        patch(self, ss.TOOL, "publish_busy", lambda windows, t, dry=False: setattr(self, "published", windows))
        patch(self, ss, "now", ss.now)
        patch(self, ss.lib_alerts.Alerts, "flush", lambda alerts: None)     # keep the outbox to read

    def run_at(self, t, dry=False):
        ss.now = lambda: t

        class Args:
            dry_run, source = dry, "manual"
        self.alerts_seen = []
        ss.cmd_run(self.cfg, Args)

    def posts(self):
        return [path.split("/")[3] for method, path, _ in self.fake.calls if method == "POST"]

    def other(self, kind, tier, start=-1, hours=6, tool="scrub-repair"):
        return busy.Busy(NOON + dt.timedelta(hours=start), NOON + dt.timedelta(hours=start + hours),
                         f"{tool} scrub", kind, tool, tier)

    def test_urgent_test_starts_outside_the_window(self):
        self.run_at(NOON)
        self.assertEqual(self.posts(), ["6"])
        self.assertEqual(self.state["drives"]["SERIAL6"]["running_tier"], "urgent")
        self.assertNotIn("starts", self.state)          # the night's count is for routine tests
        self.assertIn((NOON, NOON + ss.planner.test_length(self.cfg, 571), "smart-scheduler test on Drive 6",
                       "running", "urgent"), self.published)

    def test_routine_test_waits_for_the_window(self):
        self.fake.attrs["SERIAL6"] = {"198": 10}        # no rise
        self.fake.history["4"] = []                      # never tested: a routine baseline
        self.run_at(NOON)
        self.assertEqual(self.posts(), [])
        self.run_at(NOON.replace(hour=1) + dt.timedelta(days=1))
        self.assertEqual(self.posts(), ["4"])
        self.assertEqual(self.state["drives"]["SERIAL4"]["running_tier"], "routine")

    def test_urgent_ignores_lower_tiers_and_nudges_them(self):
        self.others = [self.other("running", "scheduled")]
        self.run_at(NOON)
        self.assertEqual(self.posts(), ["6"])
        self.assertEqual(self.nudged, ["scrub-repair"])

    def test_urgent_waits_for_its_tier_and_above(self):
        for tier in ("urgent", "manual", "fixed"):
            self.fake.calls.clear()
            self.others = [self.other("running", tier)]
            self.run_at(NOON)
            self.assertEqual(self.posts(), [], tier)
        self.assertEqual(self.nudged, [])

    def test_routine_avoids_every_tier(self):
        self.fake.attrs["SERIAL6"] = {"198": 10}
        self.fake.history["4"] = []
        night = NOON.replace(hour=1) + dt.timedelta(days=1)
        self.others = [busy.Busy(night - dt.timedelta(hours=1), night + dt.timedelta(hours=5), "x", "planned",
                                 "other-tool", "routine")]
        self.run_at(night)
        self.assertEqual(self.posts(), [])

    def running(self, slot, test_id=33):
        disk = next(d for d in self.fake.storage["disks"] if str(d["slotId"]) == slot)
        test = {"id": test_id, "type": "extended", "status": "inProgress", "result": "",
                "createdAt": "2026-11-22T13:00:00-07:00", "finishedAt": None, "progress": 50}
        disk["smartTest"] = test
        self.fake.history[slot] = [test] + self.fake.history[slot]
        return disk

    def test_running_routine_test_gives_way_to_a_higher_tier(self):
        self.fake.attrs["SERIAL6"] = {"198": 10}
        self.running("3")
        self.state["drives"]["SERIAL3"] = {"running_tier": "routine"}
        self.others = [self.other("planned", "urgent", start=0)]
        self.run_at(NOON)
        self.assertEqual(self.commands, [("smartctl", "-X", "/dev/SERIAL3")])
        self.assertEqual(self.state["drives"]["SERIAL3"]["yielded"], ["33"])
        self.assertNotIn("running_tier", self.state["drives"]["SERIAL3"])
        info = [m["msg"] for m in self.state["outbox"] if m["level"] == "info"]
        self.assertTrue(any("Stopped the routine test on Drive 3" in m and "make way for scrub-repair scrub (urgent)"
                            in m for m in info), info)
        # Drive's record of the stopped test isn't a failure: no retry, no alert.
        disk = next(d for d in self.fake.storage["disks"] if str(d["slotId"]) == "3")
        disk["smartTest"]["status"], disk["smartTest"]["finishedAt"] = "aborted", "2026-11-22T14:01:00-07:00"
        disk["smartTest"] = None
        drives = {d["serial"]: d for d in ss.evaluate_drives(self.cfg, self.fake.storage, "uid", self.state,
                                                              ss.Alerts(self.cfg, self.state, True))}
        self.assertNotEqual(drives["SERIAL3"]["cause"], "retry")
        self.assertFalse(any(k.startswith("test:SERIAL3") for k in self.state.get("alerts", {})))

    def test_running_test_keeps_going_for_lower_or_later_jobs(self):
        self.fake.attrs["SERIAL6"] = {"198": 10}
        self.running("3")
        self.state["drives"]["SERIAL3"] = {"running_tier": "urgent"}
        self.others = [self.other("planned", "scheduled", start=0),
                       self.other("planned", "manual", start=3)]      # not yet: it starts in 3 hours
        self.run_at(NOON)
        self.assertEqual(self.commands, [])

    def test_a_test_started_elsewhere_counts_as_manual(self):
        self.fake.attrs["SERIAL6"] = {"198": 10}
        self.running("3")                                # no running_tier: started in UniFi Drive
        self.others = [self.other("running", "urgent", start=0)]
        self.run_at(NOON)
        self.assertEqual(self.commands, [])
        self.assertIn("manual", [getattr(w, "tier", None) or w[4] for w in self.published
                                 if (getattr(w, "kind", None) or w[3]) == "running"])

    def test_dry_run_stops_nothing(self):
        self.fake.attrs["SERIAL6"] = {"198": 10}
        self.running("3")
        self.state["drives"]["SERIAL3"] = {"running_tier": "routine"}
        self.others = [self.other("running", "urgent", start=0)]
        with redirect_stdout(io.StringIO()) as out:
            self.run_at(NOON, dry=True)
        self.assertIn("would stop the test on Drive 3: smartctl -X /dev/SERIAL3", out.getvalue())
        self.assertEqual(self.commands, [])
        self.assertEqual(self.state["drives"]["SERIAL3"], {"running_tier": "routine"})


class PlanTests(unittest.TestCase):
    def test_conflict_counts_only_its_tier_and_above(self):
        cfg = ss.Config("/nonexistent")
        w = [ss.planner.Window(NOON, NOON + dt.timedelta(hours=2), "a scheduled scrub", "scrub", "scheduled")]
        self.assertEqual(ss.planner.conflict(cfg, w, 60, NOON, "routine"), "would overlap a scheduled scrub")
        self.assertIsNone(ss.planner.conflict(cfg, w, 60, NOON, "urgent"))
        fixed = [ss.planner.Window(NOON, NOON + dt.timedelta(hours=2), "UniFi backup", "backup")]
        self.assertEqual(ss.planner.conflict(cfg, fixed, 60, NOON, "manual"), "would overlap UniFi backup")


if __name__ == "__main__":
    unittest.main()
