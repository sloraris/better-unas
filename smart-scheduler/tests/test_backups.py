"""Backup and replication windows (#5). Run from the smart-scheduler folder:

    python3 -m unittest discover -s tests

psql is faked with rows shaped like UniFi Drive's `remote_backup_tasks` and
`remote_backup_task_runs`, modelled on a UNAS whose only enabled task is a
weekly UniFi Cloud backup (Sundays 04:00; recent runs took 7 and 50 minutes).
Nothing touches a NAS.
"""

import datetime as dt
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from test_scheduling import NIGHT, TZ, Fake, ss

planner = ss.planner
backups = ss.backups


def at(*a):
    return dt.datetime(*a, tzinfo=TZ)


def epoch(*a):
    return str(at(*a).timestamp())


def patch(test, name, value):
    """Replace ss.<name> for one test."""
    test.addCleanup(setattr, ss, name, getattr(ss, name))
    setattr(ss, name, value)


def config(**values):
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
        f.write("".join(f"{k}={v}\n" for k, v in values.items()))
    try:
        return ss.Config(f.name)
    finally:
        os.unlink(f.name)


# information_schema rows. The credential columns are there, as on a real NAS,
# and must never end up in a query.
SHAPE = [[backups.TASKS_TABLE, c, t] for c, t in (
    ("id", "uuid"), ("name", "text"), ("remote_type", "text"), ("schedule_enable", "boolean"),
    ("schedule_first_run_time", "text"), ("schedule_weekdays", "text"), ("schedule_monthdays", "text"),
    ("schedule_interval", "integer"), ("bw_limit", "integer"), ("deleted_at", "timestamp with time zone"),
    ("remote_access_key", "text"), ("remote_encrypted_secret", "text"), ("remote_username", "text"),
    ("remote_ip", "text"))] + [[backups.RUNS_TABLE, c, t] for c, t in (
    ("id", "uuid"), ("task_id", "uuid"), ("started_at", "timestamp with time zone"),
    ("finished_at", "timestamp with time zone"), ("status", "text"), ("error_message", "text"))]

# id, remote_type, schedule_enable, first run time, weekdays, monthdays, interval, name, bw_limit
WEEKLY = ["t-weekly", "cloud", "t", "4:00", "0", "", "0", "", ""]
MONTHLY = ["t-monthly", "unas", "t", "2:00", "*", "1", "0", "Office", ""]
DISABLED = ["t-off", "cloud", "f", "1:00", "*", "", "0", "Old", ""]

RUNS = [  # task_id, started, finished, status (newest first, as the query orders them)
    ["t-weekly", epoch(2026, 11, 15, 4, 0), epoch(2026, 11, 15, 4, 7), "success"],
    ["t-weekly", epoch(2026, 11, 8, 4, 0), epoch(2026, 11, 8, 4, 50), "partial_success"],
    ["t-weekly", epoch(2026, 11, 1, 4, 0), epoch(2026, 11, 1, 9, 0), "failed"],   # failures don't count
]


class FakeDrive:
    def __init__(self, tasks, runs=RUNS, shape=SHAPE):
        self.tasks, self.runs, self.shape = tasks, runs, shape
        self.queries = []

    def psql(self, db, query):
        self.queries.append(query)
        if "information_schema" in query:
            return self.shape, None
        if backups.RUNS_TABLE in query:
            return self.runs, None
        if backups.TASKS_TABLE in query:
            return self.tasks, None
        return [], None


def task(row, names=backups.TASK_COLUMNS[:-1] + backups.TASK_OPTIONAL):
    return backups.parse_tasks([row], list(names))[0]


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ss.Config("/nonexistent")

    def test_weekly_task_runs_on_sundays(self):
        times = backups.run_times(task(WEEKLY), at(2026, 11, 22), at(2026, 12, 14))
        self.assertEqual(times, [at(2026, 11, 22, 4), at(2026, 11, 29, 4), at(2026, 12, 6, 4), at(2026, 12, 13, 4)])

    def test_monthly_task_runs_on_its_day(self):
        times = backups.run_times(task(MONTHLY), at(2026, 11, 22), at(2027, 2, 2))
        self.assertEqual(times, [at(2026, 12, 1, 2), at(2027, 1, 1, 2), at(2027, 2, 1, 2)])

    def test_interval_repeats_through_the_day(self):
        t = task(["r", "unas", "t", "20:00", "*", "", "90", "", ""])
        times = backups.run_times(t, at(2026, 11, 22, 12), at(2026, 11, 22, 23, 59))
        self.assertEqual([x.strftime("%H:%M") for x in times], ["20:00", "21:30", "23:00"])

    def test_weekday_lists_ranges_and_sunday_as_7(self):
        t = task(["x", "cloud", "t", "3:30", "1-2,7", "", "", "", ""])
        times = backups.run_times(t, at(2026, 11, 22), at(2026, 11, 28))
        self.assertEqual([x.strftime("%a") for x in times], ["Sun", "Mon", "Tue"])

    def test_both_day_kinds_set_means_either(self):
        t = task(["x", "cloud", "t", "3:00", "3", "1", "", "", ""])   # Wednesdays or the 1st
        times = backups.run_times(t, at(2026, 11, 28), at(2026, 12, 3))
        self.assertEqual([x.date() for x in times], [dt.date(2026, 12, 1), dt.date(2026, 12, 2)])

    def test_unreadable_schedule(self):
        self.assertIn("first run time", backups.schedule_problem(task(["x", "cloud", "t", "soon", "*", "", "", "", ""])))
        self.assertIn("out of range", backups.schedule_problem(task(["x", "cloud", "t", "4:00", "9", "", "", "", ""])))
        self.assertIsNone(backups.schedule_problem(task(WEEKLY)))


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ss.Config("/nonexistent")
        self.runs = backups.parse_runs(RUNS, TZ)

    def test_history_estimate_is_longest_good_recent_run(self):
        length, how = backups.estimate(self.cfg, task(WEEKLY), self.runs)
        self.assertEqual(length, dt.timedelta(minutes=50))
        self.assertEqual(how, "longest of the last 2 run(s): 50 min")

    def test_task_with_no_history_uses_the_default(self):
        length, how = backups.estimate(self.cfg, task(MONTHLY), self.runs)
        self.assertEqual(length, dt.timedelta(hours=4))
        self.assertEqual(how, "no run history; default 4 h")
        cfg = config(BACKUP_DEFAULT_HOURS="6.5")
        self.assertEqual(backups.estimate(cfg, task(MONTHLY), [])[0], dt.timedelta(hours=6.5))

    def test_size_over_bandwidth(self):
        limited = task(["b", "cloud", "t", "4:00", "*", "", "", "", "100"])   # bw_limit 100 Mbit/s
        length, how = backups.estimate(self.cfg, limited, [], size_bytes=90e9)
        self.assertEqual(length, dt.timedelta(hours=2))
        self.assertIn("the task's limit", how)
        cfg = config(BACKUP_UPLOAD_MBPS="50")
        length, how = backups.estimate(cfg, task(MONTHLY), [], size_bytes=90e9)
        self.assertEqual(length, dt.timedelta(hours=4))
        self.assertIn("BACKUP_UPLOAD_MBPS", how)
        # No speed known: the default window.
        self.assertEqual(backups.estimate(self.cfg, task(MONTHLY), [], size_bytes=90e9)[0], dt.timedelta(hours=4))

    def test_millisecond_epochs(self):
        runs = backups.parse_runs([["t-weekly", str(at(2026, 11, 15, 4).timestamp() * 1000),
                                    str(at(2026, 11, 15, 4, 20).timestamp() * 1000), "SUCCESS"]], TZ)
        self.assertEqual(backups.estimate(self.cfg, task(WEEKLY), runs)[0], dt.timedelta(minutes=20))


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ss.Config("/nonexistent")
        self.runs = backups.parse_runs(RUNS, TZ)

    def test_window_spans_margin_estimate_margin(self):
        w = backups.windows(self.cfg, [task(WEEKLY)], self.runs, NIGHT, at(2026, 11, 23))
        self.assertEqual(w, [planner.Window(at(2026, 11, 22, 3), at(2026, 11, 22, 5, 50),
                                            "UniFi Cloud backup at Sun 22 Nov 04:00", "backup")])

    def test_disabled_task_makes_no_windows(self):
        self.assertEqual(backups.windows(self.cfg, [task(DISABLED)], self.runs, NIGHT, at(2027, 1, 1)), [])

    def test_repeating_task_merges_its_windows(self):
        # Every 3 hours from 00:00, 5-minute runs: 1 h margins don't touch, so one window per run.
        t = task(["r", "unas", "t", "0:00", "*", "", "180", "", ""])
        runs = backups.parse_runs([["r", epoch(2026, 11, 21, 3), epoch(2026, 11, 21, 3, 5), "success"]], TZ)
        w = backups.windows(self.cfg, [t], runs, NIGHT, at(2026, 11, 22, 23, 59))
        self.assertEqual([x.start.hour for x in w], [23, 2, 5, 8, 11, 14, 17, 20, 23])   # the last one is for 00:00 on the 23rd
        # Every hour: the margins overlap and the whole stretch is one window.
        t["interval"] = "60"
        w = backups.windows(self.cfg, [t], runs, NIGHT, at(2026, 11, 23, 12))
        self.assertEqual(len(w), 1)
        self.assertEqual((w[0].start, w[0].end), (at(2026, 11, 21, 23), at(2026, 11, 23, 14, 5)))   # the 13:00 run starts inside until + margin

    def test_run_in_progress_blocks_from_its_real_start(self):
        runs = self.runs + backups.parse_runs([["t-monthly", epoch(2026, 11, 22, 0, 30), "", "running"]], TZ)
        w = backups.windows(self.cfg, [task(MONTHLY)], runs, NIGHT, at(2026, 11, 23))
        self.assertEqual(w[0].label, "replication “Office” running since Sun 22 Nov 00:30")
        self.assertEqual((w[0].start, w[0].end), (at(2026, 11, 21, 23, 30), at(2026, 11, 22, 5, 30)))

    def test_stale_unfinished_run_is_ignored(self):
        runs = backups.parse_runs([["t-monthly", epoch(2026, 11, 2, 2), "", "running"]], TZ)
        self.assertEqual(backups.windows(self.cfg, [task(MONTHLY)], runs, NIGHT, at(2026, 11, 23)), [])


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ss.Config("/nonexistent")
        self.addCleanup(setattr, ss, "psql", ss.psql)

    def read(self, drive):
        ss.psql = drive.psql
        return ss.read_backups(self.cfg, TZ)

    def test_reads_tasks_and_runs_without_credential_columns(self):
        drive = FakeDrive([WEEKLY, MONTHLY, DISABLED])
        info = self.read(drive)
        self.assertEqual(info["problems"], [])
        self.assertEqual([t["id"] for t in info["tasks"]], ["t-weekly", "t-monthly", "t-off"])
        self.assertEqual([t["enabled"] for t in info["tasks"]], [True, True, False])
        self.assertEqual(len(info["runs"]), 3)
        for q in drive.queries:
            for secret in ("remote_access_key", "remote_encrypted_secret", "remote_username", "remote_ip", "*"):
                self.assertNotIn(secret, q.split("FROM")[0] if "information_schema" not in q else "")
        self.assertIn("WHERE deleted_at IS NULL", drive.queries[1])

    def test_missing_column_is_a_shape_problem(self):
        shape = [r for r in SHAPE if r[1] != "schedule_weekdays"]
        info = self.read(FakeDrive([WEEKLY], shape=shape))
        self.assertEqual(info["tasks"], [])
        self.assertEqual(info["problems"], [("backups:shape", "UniFi Drive's backup tables changed: "
                                             "remote_backup_tasks has no column schedule_weekdays")])

    def test_missing_table_is_a_shape_problem(self):
        info = self.read(FakeDrive([WEEKLY], shape=[r for r in SHAPE if r[0] != backups.RUNS_TABLE]))
        self.assertIn("table remote_backup_task_runs not found", info["problems"][0][1])

    def test_optional_columns_may_go(self):
        shape = [r for r in SHAPE if r[1] not in ("name", "bw_limit")]
        info = self.read(FakeDrive([WEEKLY[:7]], shape=shape))
        self.assertEqual(info["problems"], [])
        self.assertEqual(info["tasks"][0]["time"], "4:00")

    def test_psql_failure(self):
        drive = FakeDrive([])
        drive.psql = lambda db, q: (None, "psql on unifi-drive failed (exit 2)")
        info = self.read(drive)
        self.assertEqual(info["problems"][0][0], "backups:read")

    def test_problems_alert_once_and_clear(self):
        state = {}
        alerts = ss.Alerts(self.cfg, state, True)
        patch(self, "log", lambda *a, **k: None)
        ss.psql = FakeDrive([WEEKLY], shape=[r for r in SHAPE if r[1] != "status"]).psql
        windows, _ = ss.backup_windows(self.cfg, NIGHT, at(2026, 11, 23), alerts)
        self.assertEqual(windows, [])
        self.assertEqual(list(state["alerts"]), ["backups:shape"])
        self.assertEqual(state["alerts"]["backups:shape"]["level"], "warning")
        ss.psql = FakeDrive([WEEKLY]).psql
        windows, _ = ss.backup_windows(self.cfg, NIGHT, at(2026, 11, 23), alerts)
        self.assertEqual(len(windows), 1)
        self.assertEqual(state["alerts"], {})

    def test_bad_schedule_alerts_for_that_task_only(self):
        ss.psql = FakeDrive([WEEKLY, ["t-bad", "cloud", "t", "late", "*", "", "", "", ""]]).psql
        windows, info = ss.backup_windows(self.cfg, NIGHT, at(2026, 11, 23))
        self.assertEqual(len(windows), 1)
        self.assertEqual([k for k, _ in info["problems"]], ["backups:schedule:t-bad"])

    def test_turned_off(self):
        self.cfg = config(BACKUP_AVOID="no")
        ss.psql = lambda db, q: self.fail("must not query")
        self.assertEqual(ss.backup_windows(self.cfg, NIGHT, at(2026, 11, 23)), ([], None))


class PlannerTests(unittest.TestCase):
    """The weekly Sunday 04:00 backup, fed through the planner and the run."""

    def setUp(self):
        self.fake = Fake()
        self.fake.install()
        self.cfg = ss.Config("/nonexistent")
        self.addCleanup(setattr, ss, "now", ss.now)
        self.addCleanup(setattr, ss, "psql", ss.psql)
        ss.psql = FakeDrive([WEEKLY, DISABLED]).psql

    def drives(self, t=NIGHT):
        ss.now = lambda: t
        return ss.evaluate_drives(self.cfg, self.fake.storage, "uid", {}, ss.Alerts(self.cfg, {}, True), report=False)

    def test_plan_skips_sunday_nights(self):
        windows, _ = ss.backup_windows(self.cfg, NIGHT, NIGHT + dt.timedelta(days=60))
        p = planner.plan(self.fake.storage, self.drives(), {}, self.cfg, NIGHT, windows=windows)
        nights = [e["night"] for e in p]
        self.assertEqual(nights, ["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27",
                                  "2026-11-28", "2026-12-01"])
        self.assertEqual([s["night"] for s in p.skipped], ["2026-11-22", "2026-11-29", "2026-11-30"])
        # Tonight slots 1 and 2 are also busy; the backup is what stops the rest.
        self.assertIn("would overlap UniFi Cloud backup at Sun 22 Nov 04:00", p.skipped[0]["reasons"])
        self.assertEqual(p.skipped[1]["reasons"], ["would overlap UniFi Cloud backup at Sun 29 Nov 04:00"])
        self.assertEqual(p.skipped[2]["reasons"], ["would overlap pool 2 scrub at Mon 30 Nov 02:30"])

    def test_monthly_task_blocks_its_night(self):
        ss.psql = FakeDrive([WEEKLY, MONTHLY]).psql
        windows, _ = ss.backup_windows(self.cfg, NIGHT, NIGHT + dt.timedelta(days=60))
        p = planner.plan(self.fake.storage, self.drives(), {}, self.cfg, NIGHT, windows=windows)
        self.assertEqual(p[-1]["night"], "2026-12-02")
        self.assertIn({"night": "2026-12-01", "reasons": ["would overlap replication “Office” at Tue 01 Dec 02:00"]},
                      p.skipped)

    def test_run_does_not_start_into_a_backup(self):
        self.fake.attrs["SERIAL6"] = {}
        state = {}
        sent = []
        patch(self, "start_test", lambda cfg, d, *a: sent.append(d["serial"]))
        patch(self, "self_check", lambda *a, **k: (self.fake.storage, "uid"))
        patch(self, "load_state", lambda: state)

        class Args:
            dry_run, source = True, "manual"
        ss.now = lambda: NIGHT   # Sunday 01:00: a test would run into the 04:00 backup
        ss.cmd_run(self.cfg, Args)
        self.assertEqual(sent, [])
        ss.now = lambda: at(2026, 11, 23, 1)   # Monday: free
        ss.cmd_run(self.cfg, Args)
        self.assertEqual(len(sent), 1)

    def test_status_lists_tasks_and_skipped_nights(self):
        patch(self, "get_storage", lambda: self.fake.storage)
        patch(self, "resolve_identity", lambda cfg: ("uid", "the console owner", None))
        patch(self, "load_state", lambda: {})
        ss.now = lambda: NIGHT
        out = io.StringIO()
        with redirect_stdout(out):
            ss.cmd_status(self.cfg, None)
        text = out.getvalue()
        self.assertIn("Sun 22 Nov        skipped: would overlap UniFi Cloud backup at Sun 22 Nov 04:00", text)
        self.assertIn("UniFi backup tasks (tests avoid them):", text)
        self.assertIn("UniFi Cloud backup: next blocks Sun 22 Nov 03:00-05:50; estimate longest of the last 2 "
                      "run(s): 50 min", text)
        self.assertIn("UniFi Cloud backup “Old”: schedule off, ignored", text)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        cfg = ss.Config("/nonexistent")
        self.assertEqual((cfg["BACKUP_AVOID"], cfg.int("BACKUP_MARGIN_HOURS"), cfg.float("BACKUP_DEFAULT_HOURS"),
                          cfg.int("BACKUP_UPLOAD_MBPS")), ("yes", 1, 4.0, 0))
        cfg = config(BACKUP_MARGIN_HOURS="x", BACKUP_DEFAULT_HOURS="-1", BACKUP_UPLOAD_MBPS="fast")
        self.assertEqual(len(cfg.errors), 3)
        self.assertEqual(cfg["BACKUP_UPLOAD_MBPS"], "0")


if __name__ == "__main__":
    unittest.main()
