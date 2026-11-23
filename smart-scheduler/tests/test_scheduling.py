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
        self.progress = {}     # serial -> % left of a self-test the drive reports
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
        ss.self_test_progress = lambda dev: self.progress.get(dev[5:])
        ss.log = lambda *a, **k: None


def rose6():
    """State in which Drive 6's attribute 198 (14 in the fixture) rose and no
    clean test has followed: the fixture's warning drive."""
    return {"drives": {"SERIAL6": {"rose": {"at": "2026-11-20T00:00:00-07:00", "ids": ["198"]}}}}


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.fake.install()
        self.fake.attrs["SERIAL6"] = {"198": 14}
        self.cfg = ss.Config("/nonexistent")
        self.state = rose6()
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

    def test_a_count_that_stays_put_is_not_a_warning(self):
        # 14 offline-uncorrectable sectors, never risen: an old drive like any other.
        self.state = {}
        self.alerts = ss.Alerts(self.cfg, self.state, True)
        d = self.drives()["SERIAL6"]
        self.assertEqual((d["interval"], d["source"]), (60, "old drive"))
        self.assertEqual(d["cause"], "baseline")
        self.assertNotIn("rose", self.state["drives"]["SERIAL6"])

    def test_a_rise_makes_the_drive_due_and_short_until_a_clean_test(self):
        self.addCleanup(setattr, ss, "now", ss.now)
        ss.now = lambda: NIGHT
        passed = ext(61, "successful", "optimal", "2026-11-10T09:00:00-07:00")
        self.fake.history["6"] = [passed]
        self.state = {"drives": {"SERIAL6": {"attrs_at_last_test": {"198": 12}}}}
        self.alerts = ss.Alerts(self.cfg, self.state, True)
        d = self.drives()["SERIAL6"]
        self.assertEqual((d["cause"], d["source"], d["interval"]), ("warning signs", "warning signs", 30))
        self.assertTrue(d["reason"].startswith("SMART attribute rose since last test: S198 12->14"))
        self.assertEqual(self.state["drives"]["SERIAL6"]["rose"]["ids"], ["198"])
        # The test it gets starts at 14 and passes with no further rise: back to normal.
        self.state["drives"]["SERIAL6"]["attrs_at_last_test"] = {"198": 14}
        ss.now = lambda: NIGHT + dt.timedelta(days=1)
        self.fake.history["6"] = [ext(62, "successful", "optimal", "2026-11-22T14:00:00-07:00",
                                      "2026-11-22T01:00:00-07:00"), passed]
        d = self.drives()["SERIAL6"]
        self.assertEqual((d["source"], d["interval"]), ("old drive", 60))
        self.assertNotIn("rose", self.state["drives"]["SERIAL6"])

    def test_a_rise_during_the_test_keeps_the_short_interval(self):
        self.state = {"drives": {"SERIAL6": {"attrs_at_last_test": {"198": 13},
                                             "rose": {"at": "2026-11-20T00:00:00-07:00", "ids": ["198"]}}}}
        self.alerts = ss.Alerts(self.cfg, self.state, True)
        self.fake.history["6"] = [ext(62, "successful", "optimal", "2026-11-21T14:00:00-07:00",
                                      "2026-11-21T01:00:00-07:00")]
        d = self.drives()["SERIAL6"]                       # passed, but 13 -> 14 while it ran
        self.assertEqual((d["source"], d["interval"]), ("warning signs", 30))
        self.assertIn("rose", self.state["drives"]["SERIAL6"])

    def test_a_look_from_the_tui_writes_no_rise(self):
        self.state = {"drives": {"SERIAL6": {"attrs_at_last_test": {"198": 12}}}}
        drives = ss.evaluate_drives(self.cfg, self.fake.storage, "uid", self.state,
                                    ss.Alerts(self.cfg, self.state, True), report=False)
        self.assertEqual(next(d for d in drives if d["serial"] == "SERIAL6")["source"], "warning signs")
        self.assertNotIn("rose", self.state["drives"]["SERIAL6"])

    def test_old_healthy_drive_gets_old_interval(self):
        self.fake.history["5"] = [ext(11, "successful", "optimal", "2026-11-01T09:00:00-07:00")]
        d = self.drives()["SERIAL5"]
        self.assertEqual(d["interval"], 60)
        self.assertEqual(d["due"].date(), dt.date(2026, 12, 31))

    def test_attribute_increase_makes_drive_due_now(self):
        self.fake.history["5"] = [ext(11, "successful", "optimal", "2026-11-01T09:00:00-07:00")]
        self.state["drives"] = {"SERIAL5": {"attrs_at_last_test": {"197": 0}}}
        self.fake.attrs["SERIAL5"] = {"197": 3}
        self.assertIn("rose since last test", self.drives()["SERIAL5"]["reason"])

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

    def ui_test(self, slot="4", progress=80):
        disk = next(d for d in self.fake.storage["disks"] if str(d["slotId"]) == slot)
        disk["smartTest"] = {"id": 4, "type": "extended", "status": "inProgress", "result": "",
                             "createdAt": "2026-11-21T13:14:43-07:00", "finishedAt": None, "progress": progress}

    def test_test_running_in_drive_comes_from_storage_not_history(self):
        # Started in the UniFi UI: the history endpoint lists finished tests only,
        # the running one is the disk's smartTest field in GET /storage.
        self.ui_test()
        self.fake.progress["SERIAL4"] = 20     # the drive agrees
        d = self.drives()["SERIAL4"]
        self.assertTrue(d["running"])
        self.assertNotIn("outside_drive", d)
        self.assertEqual(d["remaining_percent"], 20)
        self.assertEqual(ss.start_gate(self.cfg, self.fake.storage, list(self.drives().values()), self.state, NIGHT),
                         "a SMART test is already running")

    def test_finished_current_test_is_not_counted_twice(self):
        self.fake.history["4"] = [ext(4, "successful", "optimal", "2026-11-21T22:40:00-07:00")]
        disk = next(d for d in self.fake.storage["disks"] if str(d["slotId"]) == "4")
        disk["smartTest"] = dict(self.fake.history["4"][0], progress=100)
        hist = ss.get_history("4", "SERIAL4", "uid", disk["smartTest"])
        self.assertEqual(len(hist), 1)
        self.assertFalse(self.drives()["SERIAL4"]["running"])

    def test_test_started_outside_drive_counts_as_running(self):
        # Drive's history is empty, but the drive itself says a test is 80% done.
        self.fake.progress["SERIAL4"] = 20
        d = self.drives()["SERIAL4"]
        self.assertTrue(d["running"])
        self.assertTrue(d["outside_drive"])
        self.assertIn("outside UniFi Drive (20% left)", d["reason"])
        self.assertEqual(ss.start_gate(self.cfg, self.fake.storage, list(self.drives().values()), self.state, NIGHT),
                         "a SMART test is already running")

    def test_outside_test_on_excluded_drive_still_counts(self):
        self.fake.progress["SERIAL4"] = 50
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("EXCLUDE_SERIALS=SERIAL4\n")
        try:
            self.cfg = ss.Config(f.name)
        finally:
            os.unlink(f.name)
        d = self.drives()["SERIAL4"]
        self.assertTrue(d["excluded"] and d["running"])

    def test_drive_test_listed_by_drive_is_not_marked_outside(self):
        self.fake.history["4"] = [ext(9, "running", None, None, "2026-11-22T00:10:00-07:00")]
        self.fake.progress["SERIAL4"] = 60
        d = self.drives()["SERIAL4"]
        self.assertTrue(d["running"])
        self.assertNotIn("outside_drive", d)

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


class IdentityTests(unittest.TestCase):
    OWNER = "11111111-2222-4333-8444-555555555555"

    def setUp(self):
        self.fake = Fake()
        self.fake.install()
        self.queries = []

    def fake_psql(self, rows):
        def psql(db, query):
            self.queries.append(db)
            return rows, None
        ss.psql = psql

    def test_default_is_console_owner(self):
        self.fake_psql([[self.OWNER]])
        uid, label, err = ss.resolve_identity(ss.Config("/nonexistent"))
        self.assertEqual((uid, label, err), (self.OWNER, "the console owner", None))
        self.assertEqual(self.queries, ["ulp-go"])

    def test_configured_user_is_looked_up_in_drive(self):
        self.fake_psql([[self.OWNER]])
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("DRIVE_USER=scheduler-admin\n")
        try:
            uid, label, err = ss.resolve_identity(ss.Config(f.name))
        finally:
            os.unlink(f.name)
        self.assertEqual(uid, self.OWNER)
        self.assertIn("scheduler-admin", label)
        self.assertEqual(self.queries, ["unifi-drive"])

    def test_deleted_configured_user_reads_as_missing(self):
        self.fake_psql([])
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("DRIVE_USER=gone\n")
        try:
            self.assertEqual(ss.resolve_identity(ss.Config(f.name))[::2], (None, None))
        finally:
            os.unlink(f.name)

    def test_access_check(self):
        self.assertIsNone(ss.check_access(self.fake.storage, self.OWNER))
        ss.api = lambda *a, **k: (403, {"err": {"msg": "Forbidden"}})
        self.assertIn("admin with Drive management", ss.check_access(self.fake.storage, self.OWNER))


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
        self.assertEqual(c.errors, [
            "INTERVAL_DAYS: want a whole number, got 'abc'",
            "INTERVAL_OVERRIDES: bad entry 'bad' (want SERIAL:DAYS)",
            "NOPE: unknown setting"])
        # A bad entry rejects the whole setting (the schema rule), so the default applies.
        self.assertEqual(c.overrides, {})
        self.assertEqual(c.int("INTERVAL_DAYS"), 90)
        self.assertTrue(ss.in_window(c, NIGHT.replace(hour=23)))
        self.assertEqual(ss.night_key(c, NIGHT.replace(hour=2)), "2026-11-21")


if __name__ == "__main__":
    unittest.main()
