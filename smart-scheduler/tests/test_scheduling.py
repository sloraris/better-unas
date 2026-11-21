"""Scheduling logic tests. Run from the smart-scheduler folder:

    python3 -m unittest discover -s tests

Uses a real (anonymized) GET /api/v2/storage response from a UNAS Pro with
seven 4 TB drives; the API, smartctl and lsblk are faked. Nothing touches a NAS.
"""

import datetime as dt
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ss", os.path.join(HERE, "..", "smart_scheduler.py"))
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)

TZ = dt.timezone(dt.timedelta(hours=-7))
NIGHT = dt.datetime(2026, 11, 22, 1, 0, tzinfo=TZ)   # inside the default window
# Fixture scrub: monthly on the last day, nextRun 2026-11-30T02:30-07:00.


def ext(id_, status, result, finished, created=None):
    return {"id": id_, "type": "extended", "status": status, "result": result,
            "createdAt": created or finished, "finishedAt": finished}


class Fake:
    def __init__(self):
        with open(os.path.join(HERE, "fixtures", "storage.json")) as f:
            self.storage = json.load(f)
        self.history = {}      # slot -> list of tests
        self.attrs = {}        # serial -> {attr id: raw}
        self.calls = []

    def api(self, method, path, user_id=None, body=None, timeout=15):
        self.calls.append((method, path, body))
        if path == "/storage":
            return 200, self.storage
        if "/smart-tests" in path and method == "GET":
            return 200, self.history.get(path.split("/")[3], [])
        if method == "POST":
            return 202, None
        return 404, None

    def install(self):
        ss.api = self.api
        ss.serial_to_device = lambda: {d["serial"]: "/dev/" + d["serial"] for d in self.storage["disks"]}
        ss.smart_facts = lambda dev, ids: (571, self.attrs.get(dev[5:], {}))
        ss.log = lambda *a, **k: None


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.fake.install()
        self.fake.attrs["SERIAL6"] = {"198": 14}
        self.cfg = ss.Config("/nonexistent")
        self.state = {}
        self.alerts = ss.Alerts(self.cfg, self.state, True)

    def drives(self):
        return {d["serial"]: d for d in ss.evaluate_drives(self.cfg, self.fake.storage, "uid", self.state, self.alerts)}

    def test_every_drive_without_history_is_due_for_a_baseline(self):
        drives = self.drives()
        self.assertEqual(len(drives), 7)
        self.assertTrue(all("baseline" in d["reason"] for d in drives.values()))

    def test_quick_tests_do_not_count(self):
        self.fake.history["3"] = [{"id": 1, "type": "quick", "status": "successful", "result": "optimal",
                                   "createdAt": "2026-11-21T00:41:58-07:00", "finishedAt": "2026-11-21T00:43:56-07:00"}]
        self.assertIsNone(self.drives()["SERIAL3"]["last"])

    def test_warning_attribute_gives_short_interval_and_goes_first(self):
        drives = self.drives()
        self.assertEqual(drives["SERIAL6"]["interval"], 30)
        first = sorted(drives.values(), key=lambda d: (d["interval"], d["due"]))[0]
        self.assertEqual(first["serial"], "SERIAL6")

    def test_old_healthy_drive_gets_old_interval(self):
        self.fake.history["5"] = [ext(11, "successful", "optimal", "2026-11-01T09:00:00-07:00")]
        d = self.drives()["SERIAL5"]
        self.assertEqual(d["interval"], 60)
        self.assertEqual(d["due"].date(), dt.date(2026, 12, 31))

    def test_attribute_increase_makes_drive_due_now(self):
        self.fake.history["5"] = [ext(11, "successful", "optimal", "2026-11-01T09:00:00-07:00")]
        self.state["drives"] = {"SERIAL5": {"attrs_at_last_test": {"197": 0}}}
        self.fake.attrs["SERIAL5"] = {"197": 3}
        self.assertIn("increased", self.drives()["SERIAL5"]["reason"])

    def test_failed_test_retries_and_alerts_once(self):
        self.fake.history["4"] = [ext(10, "failed", "failed", "2026-11-10T05:00:00-07:00"),
                                  ext(8, "successful", "optimal", "2026-08-10T09:00:00-07:00")]
        self.assertTrue(self.drives()["SERIAL4"]["reason"].startswith("retry"))
        self.assertTrue(any(k.startswith("test:SERIAL4") for k in self.state["alerts"]))
        sent = len(self.alerts.outbox)
        self.drives()
        self.assertEqual(len(self.alerts.outbox), sent)

    def test_status_view_has_no_side_effects(self):
        self.fake.history["4"] = [ext(10, "failed", "failed", "2026-11-10T05:00:00-07:00")]
        ss.evaluate_drives(self.cfg, self.fake.storage, "uid", self.state, self.alerts, report=False)
        self.assertEqual(self.alerts.outbox, [])
        self.assertNotIn("reported_tests", self.state)

    def test_gate(self):
        drives = list(self.drives().values())
        self.assertIsNone(ss.start_gate(self.cfg, self.fake.storage, drives, self.state, NIGHT))
        self.assertEqual(ss.start_gate(self.cfg, self.fake.storage, drives, self.state, NIGHT.replace(hour=13)),
                         "outside start window")

    def test_running_test_closes_gate(self):
        self.fake.history["4"] = [ext(9, "running", None, None, "2026-11-22T00:10:00-07:00")]
        drives = list(self.drives().values())
        self.assertEqual(ss.start_gate(self.cfg, self.fake.storage, drives, self.state, NIGHT),
                         "a SMART test is already running")

    def test_unhealthy_pool_closes_gate(self):
        self.fake.storage["pools"][0]["status"] = "degraded"
        drives = list(self.drives().values())
        self.assertIn("degraded", ss.start_gate(self.cfg, self.fake.storage, drives, self.state, NIGHT))

    def test_scrub_avoidance(self):
        at = lambda *a: dt.datetime(*a, tzinfo=TZ)
        conflict = lambda t: ss.scrub_conflict(self.cfg, self.fake.storage, 571, t)
        self.assertIsNone(conflict(at(2026, 11, 29, 1, 0)))
        self.assertIsNotNone(conflict(at(2026, 11, 29, 11, 0)))
        self.assertIsNotNone(conflict(at(2026, 11, 30, 0, 30)))
        self.assertIsNone(conflict(at(2026, 12, 1, 1, 0)))

    def test_busy_drives_are_visible(self):
        # In the fixture, slots 1 and 2 were mid-copy at ~155 MB/s.
        drives = self.drives()
        self.assertGreater(drives["SERIAL1"]["kbps"], self.cfg.int("BUSY_KBPS"))
        self.assertLess(drives["SERIAL6"]["kbps"], self.cfg.int("BUSY_KBPS"))

    def test_start_posts_extended_and_limits_per_night(self):
        drive = self.drives()["SERIAL6"]
        state = {}
        ss.start_test(self.cfg, drive, "uid", state, ss.Alerts(self.cfg, state, False), False, NIGHT)
        self.assertEqual(self.fake.calls[-1], ("POST", "/storage/disks/6/smart-tests", {"type": "extended"}))
        self.assertEqual(state["starts"], {"2026-11-22": 1})
        self.assertEqual(state["drives"]["SERIAL6"]["attrs_at_last_test"], {"198": 14})
        self.assertEqual(ss.start_gate(self.cfg, self.fake.storage, [], state, NIGHT.replace(hour=2)),
                         "already started tonight's test(s)")


class AlertTests(unittest.TestCase):
    def test_dedupe_and_resolve(self):
        cfg, state = ss.Config("/nonexistent"), {}
        ss.log = lambda *a, **k: None
        a = ss.Alerts(cfg, state, True)
        a.raise_("k", "boom")
        a.raise_("k", "boom")
        self.assertEqual(len(a.outbox), 1)
        a.clear("k")
        self.assertEqual(a.outbox[-1]["level"], "resolved")
        self.assertFalse(a.has_errors())


class ConfigTests(unittest.TestCase):
    def test_parsing_and_fallbacks(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("INTERVAL_DAYS=abc\nNOPE=1\nINTERVAL_OVERRIDES=SERIAL6:14,bad\nSTART_WINDOW=22:00-04:00\n")
        try:
            c = ss.Config(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(len(c.errors), 3)
        self.assertEqual(c.overrides, {"SERIAL6": 14})
        self.assertEqual(c.int("INTERVAL_DAYS"), 90)
        self.assertTrue(ss.in_window(c, NIGHT.replace(hour=23)))
        self.assertEqual(ss.night_key(c, NIGHT.replace(hour=2)), "2026-11-21")


if __name__ == "__main__":
    unittest.main()
