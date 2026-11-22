"""Drive actions (#8): run now, exclude, set interval.

    python3 -m unittest discover -s tests

Uses the anonymized storage fixture and the API fake from test_scheduling.
Nothing touches a NAS. In the fixture slots 1 and 2 are busy (~155 MB/s),
pool 1 is healthy and idle, and its scrub runs 2026-11-30 02:30.
"""

import datetime as dt
import fcntl
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from test_scheduling import NIGHT, TZ, Fake, ext, ss

import actions  # noqa: E402  (smart_scheduler.py put its folder on sys.path)

planner = ss.planner


def at(*a):
    return dt.datetime(*a, tzinfo=TZ)


def patch(test, obj, name, value):
    test.addCleanup(setattr, obj, name, getattr(obj, name))
    setattr(obj, name, value)


class Base(unittest.TestCase):
    def setUp(self):
        patch(self, actions, "ss", ss)
        self.fake = Fake()
        self.fake.install()
        self.cfg = ss.Config("/nonexistent")
        self.state = {}
        self.saved = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        patch(self, ss, "LOCK_FILE", os.path.join(self.dir, ".lock"))
        patch(self, ss, "CONFIG_FILE", os.path.join(self.dir, "config"))
        patch(self, ss, "now", lambda: NIGHT)
        patch(self, ss, "resolve_identity", lambda cfg: ("uid", "the console owner", None))
        patch(self, ss, "check_access", lambda storage, uid: None)
        patch(self, ss, "backup_windows", lambda cfg, t, until, alerts=None: ([], None))
        patch(self, ss, "load_state", lambda: self.state)
        patch(self, ss, "save_state", lambda state, dry: self.saved.append((dict(state), dry)))
        patch(self, ss, "take_lock", self.spy_lock(ss.take_lock))
        self.locks = []

    def spy_lock(self, real):
        def take(wait):
            f = real(wait)
            self.locks.append(f is not None)
            return f
        return take

    def lock_is_held(self):
        with open(ss.LOCK_FILE, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
            return False

    def codes(self, slot="6", kind="extended", t=NIGHT):
        return [(c.code, c.overridable) for c in actions.check_run_now(slot, kind, cfg=self.cfg, t=t)]

    def posts(self):
        return [c for c in self.fake.calls if c[0] == "POST"]


class CheckTests(Base):
    def test_clear_drive_has_no_checks(self):
        self.assertEqual(self.codes(), [])
        self.assertEqual(self.locks, [])          # checking takes no lock
        self.assertEqual(self.posts(), [])

    def test_busy_drive_is_overridable(self):
        self.assertEqual(self.codes("1"), [("busy", True)])

    def test_outside_start_window_is_overridable(self):
        self.assertEqual(self.codes(t=at(2026, 11, 22, 13)), [("window", True)])

    def test_upcoming_scrub_is_overridable(self):
        # An extended test started the evening before the scrub would run into it.
        self.assertEqual(self.codes(t=at(2026, 11, 29, 23)), [("window", True), ("scrub", True)])
        # A quick test that late is fine.
        self.assertEqual(self.codes(kind="quick", t=at(2026, 11, 29, 23)), [("window", True)])

    def test_scrub_in_progress_is_overridable(self):
        self.fake.storage["pools"][0]["dataScrubbing"]["status"] = "running"
        self.assertEqual(self.codes(), [("scrub", True)])

    def test_upcoming_backup_is_overridable(self):
        w = planner.Window(at(2026, 11, 22, 3), at(2026, 11, 22, 5), "backup Offsite", "backup")
        patch(self, ss, "backup_windows", lambda cfg, t, until, alerts=None: ([w], {"problems": []}))
        checks = actions.check_run_now("6", "extended", cfg=self.cfg)
        self.assertEqual([(c.code, c.overridable) for c in checks], [("backup", True)])
        self.assertIn("backup Offsite", checks[0].reason)

    def test_unreadable_backups_are_overridable(self):
        info = {"problems": [("backups:read", "psql failed")]}
        patch(self, ss, "backup_windows", lambda cfg, t, until, alerts=None: ([], info))
        self.assertEqual(self.codes(), [("backup", True)])

    def test_running_test_on_any_drive_blocks(self):
        self.fake.history["3"] = [ext(9, "running", None, None, "2026-11-22T00:10:00-07:00")]
        checks = actions.check_run_now("6", "quick", cfg=self.cfg)
        self.assertEqual([(c.code, c.overridable) for c in checks], [("running", False)])
        self.assertIn("slot 3", checks[0].reason)

    def test_running_test_on_excluded_drive_still_blocks(self):
        self.cfg.values["EXCLUDE_SERIALS"] = "SERIAL3"
        self.fake.history["3"] = [ext(9, "running", None, None, "2026-11-22T00:10:00-07:00")]
        self.assertEqual(self.codes(), [("running", False)])

    def test_test_started_outside_drive_blocks(self):
        self.fake.progress["SERIAL3"] = 20
        checks = actions.check_run_now("6", "quick", cfg=self.cfg)
        self.assertEqual([(c.code, c.overridable) for c in checks], [("running", False)])
        self.assertIn("slot 3", checks[0].reason)
        self.assertIn("outside UniFi Drive, 20% left", checks[0].reason)

    def test_drive_listed_test_is_not_reported_twice(self):
        self.fake.history["3"] = [ext(9, "running", None, None, "2026-11-22T00:10:00-07:00")]
        self.fake.progress["SERIAL3"] = 60
        self.assertEqual(self.codes(), [("running", False)])

    def test_unreadable_history_blocks(self):
        real = self.fake.api

        def api(method, path, user_id=None, body=None, timeout=15):
            if path.startswith("/storage/disks/4/"):
                return 500, None
            return real(method, path, user_id, body, timeout)
        ss.api = api
        self.assertEqual(self.codes(), [("history", False)])

    def test_degraded_pool_blocks(self):
        self.fake.storage["pools"][0]["status"] = "degraded"
        self.assertEqual(self.codes(), [("pool", False)])

    def test_resyncing_raid_blocks(self):
        self.fake.storage["pools"][0]["raidGroups"] = [{"currentProtection": 0, "expectedProtection": 1}]
        self.assertEqual(self.codes(), [("pool", False)])

    def test_not_smart_capable_blocks(self):
        self.fake.storage["disks"][5]["smartTestSupported"] = False
        self.assertEqual(self.codes(), [("smart", False)])

    def test_unknown_slot_blocks(self):
        self.assertEqual(self.codes("42"), [("drive", False)])

    def test_api_down_blocks(self):
        def down(*a, **k):
            raise ss.ApiError("unreachable", "connection refused")
        ss.api = down
        self.assertEqual(self.codes(), [("api", False)])

    def test_identity_lookup_failure_blocks_without_a_cached_id(self):
        patch(self, ss, "resolve_identity", lambda cfg: (None, "the console owner", "psql failed"))
        self.assertEqual(self.codes(), [("identity", False)])

    def test_identity_lookup_failure_uses_cached_id_like_the_run(self):
        patch(self, ss, "resolve_identity", lambda cfg: (None, "the console owner", "psql failed"))
        self.state["user_id"] = "cached"
        used = []
        patch(self, ss, "check_access", lambda storage, uid: used.append(uid))
        self.assertEqual(self.codes(), [])
        self.assertEqual(used, ["cached"])

    def test_missing_configured_user_blocks(self):
        patch(self, ss, "resolve_identity", lambda cfg: (None, "UniFi user 'x'", None))
        self.assertEqual(self.codes(), [("identity", False)])

    def test_access_refused_blocks(self):
        patch(self, ss, "check_access", lambda storage, uid: "UniFi Drive refused it (403)")
        self.assertEqual(self.codes(), [("access", False)])

    def test_bad_kind(self):
        with self.assertRaises(ValueError):
            actions.check_run_now("6", "conveyance", cfg=self.cfg)

    def test_blocking_and_overridable_together(self):
        self.fake.storage["pools"][0]["status"] = "degraded"
        checks = actions.check_run_now("1", "extended", cfg=self.cfg, t=at(2026, 11, 22, 13))
        self.assertEqual({c.code for c in actions.blocking(checks)}, {"pool"})
        self.assertEqual({c.code for c in actions.overridable(checks)}, {"busy", "window"})


class RunNowTests(Base):
    def test_starts_with_the_runs_identity_and_records_baseline(self):
        self.fake.attrs["SERIAL6"] = {"5": 0, "197": 2}
        r = actions.run_now("6", "extended", cfg=self.cfg)
        self.assertTrue(r.started, r.message)
        self.assertEqual(self.posts(), [("POST", "/storage/disks/6/smart-tests", {"type": "extended"})])
        self.assertEqual(self.locks, [True])
        self.assertFalse(self.lock_is_held())       # released afterwards
        self.assertEqual(self.saved[-1][0]["drives"]["SERIAL6"]["attrs_at_last_test"], {"5": 0, "197": 2})
        self.assertFalse(self.saved[-1][1])

    def test_quick_test_leaves_state_alone(self):
        r = actions.run_now("6", "quick", cfg=self.cfg)
        self.assertTrue(r.started)
        self.assertEqual(self.posts(), [("POST", "/storage/disks/6/smart-tests", {"type": "quick"})])
        self.assertEqual(self.saved, [])

    def test_lock_is_held_while_starting(self):
        held = []
        real = self.fake.api

        def api(method, path, user_id=None, body=None, timeout=15):
            if method == "POST":
                held.append(self.lock_is_held())
            return real(method, path, user_id, body, timeout)
        ss.api = api
        actions.run_now("6", "quick", cfg=self.cfg)
        self.assertEqual(held, [True])

    def test_busy_lock_raises_and_starts_nothing(self):
        with open(ss.LOCK_FILE, "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            with self.assertRaises(actions.LockBusy):
                actions.run_now("6", "quick", cfg=self.cfg, wait=0)
        self.assertEqual(self.posts(), [])

    def test_lock_false_for_callers_holding_it(self):
        actions.run_now("6", "quick", cfg=self.cfg, lock=False)
        self.assertEqual(self.locks, [])
        self.assertEqual(len(self.posts()), 1)

    def test_overridable_needs_override(self):
        r = actions.run_now("1", "extended", cfg=self.cfg)
        self.assertFalse(r.started)
        self.assertIn("Override", r.message)
        self.assertEqual(self.posts(), [])
        r = actions.run_now("1", "extended", cfg=self.cfg, override=True)
        self.assertTrue(r.started, r.message)
        self.assertEqual(len(self.posts()), 1)

    def test_blocking_ignores_override(self):
        self.fake.storage["pools"][0]["status"] = "degraded"
        r = actions.run_now("6", "extended", cfg=self.cfg, override=True)
        self.assertFalse(r.started)
        self.assertTrue(r.message.startswith("Can't start"))
        self.assertEqual(self.posts(), [])

    def test_running_test_ignores_override(self):
        self.fake.history["2"] = [ext(9, "running", None, None, "2026-11-22T00:10:00-07:00")]
        self.assertFalse(actions.run_now("6", "quick", cfg=self.cfg, override=True).started)
        self.assertEqual(self.posts(), [])

    def test_dry_run_changes_nothing(self):
        r = actions.run_now("1", "extended", cfg=self.cfg, override=True, dry_run=True)
        self.assertTrue(r.started)
        self.assertIn("dry-run", r.message)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.saved, [])
        self.assertEqual(self.locks, [])

    def test_api_errors_are_reported_like_the_run(self):
        for status, words in ((409, "already running"), (403, "HTTP 403")):
            real = self.fake.api
            ss.api = lambda m, p, u=None, b=None, timeout=15, s=status: (s, {"err": 1}) if m == "POST" else real(m, p, u, b)
            r = actions.run_now("6", "extended", cfg=self.cfg)
            self.assertFalse(r.started)
            self.assertIn(words, r.message)
            ss.api = real
        self.assertEqual(self.saved, [])

    def test_unreachable_on_start(self):
        real = self.fake.api

        def api(method, path, user_id=None, body=None, timeout=15):
            if method == "POST":
                raise ss.ApiError("unreachable", "POST: timed out")
            return real(method, path, user_id, body, timeout)
        ss.api = api
        r = actions.run_now("6", "quick", cfg=self.cfg)
        self.assertFalse(r.started)
        self.assertIn("timed out", r.message)
        self.assertFalse(self.lock_is_held())


class ConfigEditTests(Base):
    def setUp(self):
        super().setUp()
        self.path = ss.CONFIG_FILE
        with open(self.path, "w") as f:
            f.write("# --- Drives ---\nINTERVAL_DAYS=90\n")

    def text(self):
        with open(self.path) as f:
            return f.read()

    def test_exclude_and_include(self):
        self.assertTrue(actions.exclude("SERIAL3", True))
        self.assertTrue(actions.exclude("SERIAL5", True))
        self.assertFalse(actions.exclude("SERIAL5", True))
        self.assertIn("EXCLUDE_SERIALS=SERIAL3,SERIAL5\n", self.text())
        self.assertEqual(ss.Config(self.path).typed["EXCLUDE_SERIALS"], ["SERIAL3", "SERIAL5"])
        self.assertTrue(actions.exclude("SERIAL3", False))
        self.assertIn("EXCLUDE_SERIALS=SERIAL5\n", self.text())
        self.assertTrue(actions.exclude("SERIAL5", False))
        self.assertNotIn("EXCLUDE_SERIALS", self.text())   # empty: line removed, default applies
        self.assertFalse(actions.exclude("SERIAL5", False))
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")
        self.assertTrue(all(self.locks))
        self.assertEqual(len(self.locks), 6)

    def test_set_interval(self):
        self.assertTrue(actions.set_interval("SERIAL4", 30))
        self.assertTrue(actions.set_interval("SERIAL6", 14))
        self.assertTrue(actions.set_interval("SERIAL4", 45))
        self.assertEqual(ss.Config(self.path).overrides, {"SERIAL4": 45, "SERIAL6": 14})
        self.assertTrue(actions.set_interval("SERIAL4", None))
        self.assertFalse(actions.set_interval("SERIAL4", None))
        self.assertIn("INTERVAL_OVERRIDES=SERIAL6:14\n", self.text())
        self.assertTrue(actions.set_interval("SERIAL6", None))
        self.assertNotIn("INTERVAL_OVERRIDES", self.text())

    def test_bad_values_are_refused_before_writing(self):
        before = self.text()
        with self.assertRaises(ss.settings.ValidationError):
            actions.set_interval("SERIAL4", 0)
        with self.assertRaises(ss.settings.ValidationError):
            actions.exclude("not a serial!", True)
        self.assertEqual(self.text(), before)

    def test_unreadable_current_value_is_not_clobbered(self):
        with open(self.path, "a") as f:
            f.write("INTERVAL_OVERRIDES=SERIAL4:abc\n")
        before = self.text()
        with self.assertRaises(ss.settings.ValidationError):
            actions.set_interval("SERIAL6", 14)
        self.assertEqual(self.text(), before)

    def test_dry_run_changes_nothing(self):
        before = self.text()
        self.assertTrue(actions.exclude("SERIAL3", True, dry_run=True))
        self.assertTrue(actions.set_interval("SERIAL3", 30, dry_run=True))
        with self.assertRaises(ss.settings.ValidationError):
            actions.set_interval("SERIAL3", 0, dry_run=True)
        self.assertEqual(self.text(), before)
        self.assertEqual(self.locks, [])

    def test_busy_lock_writes_nothing(self):
        before = self.text()
        with open(ss.LOCK_FILE, "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            with self.assertRaises(actions.LockBusy):
                actions.exclude("SERIAL3", True, wait=0)
        self.assertEqual(self.text(), before)

    def test_lock_is_held_while_writing(self):
        held = []
        real = ss.settings.save
        patch(self, ss.settings, "save", lambda *a: (held.append(self.lock_is_held()), real(*a))[1])
        actions.set_interval("SERIAL3", 30)
        self.assertEqual(held, [True])


class CliTests(Base):
    def setUp(self):
        super().setUp()
        with open(ss.CONFIG_FILE, "w") as f:
            f.write("INTERVAL_DAYS=90\n")

    def cli(self, *argv):
        args = ss.argparse.Namespace(command=argv[0], target=argv[1] if len(argv) > 1 else None,
                                     date=argv[2] if len(argv) > 2 and not argv[2].startswith("--") else None,
                                     dry_run="--dry-run" in argv, override="--override" in argv,
                                     note="", source="manual", force=False)
        out, err = io.StringIO(), io.StringIO()
        fn = {"run-now": ss.cmd_run_now}.get(argv[0], ss.cmd_config_edit)
        with redirect_stdout(out), redirect_stderr(err):
            rc = fn(self.cfg, args)
        return rc, out.getvalue() + err.getvalue()

    def config(self):
        return ss.Config(ss.CONFIG_FILE)

    def test_run_now_by_serial_defaults_to_extended(self):
        rc, out = self.cli("run-now", "SERIAL6")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.posts(), [("POST", "/storage/disks/6/smart-tests", {"type": "extended"})])

    def test_run_now_needs_override(self):
        rc, out = self.cli("run-now", "1", "quick")
        self.assertEqual(rc, 1)
        self.assertIn("override  drive busy", out)
        rc, out = self.cli("run-now", "1", "quick", "--override")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.posts(), [("POST", "/storage/disks/1/smart-tests", {"type": "quick"})])

    def test_run_now_dry_run(self):
        rc, out = self.cli("run-now", "6", "quick", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("(dry-run) would start", out)
        self.assertEqual(self.posts(), [])

    def test_run_now_usage(self):
        self.assertEqual(self.cli("run-now", "6", "long")[0], 2)
        self.assertEqual(self.cli("run-now")[0], 2)

    def test_exclude_include_by_slot(self):
        self.assertEqual(self.cli("exclude", "3")[0], 0)
        self.assertEqual(self.config().typed["EXCLUDE_SERIALS"], ["SERIAL3"])
        rc, out = self.cli("exclude", "SERIAL3")
        self.assertIn("No change", out)
        self.assertEqual(self.cli("include", "3")[0], 0)
        self.assertEqual(self.config().typed["EXCLUDE_SERIALS"], [])

    def test_gone_drive_can_still_be_removed_by_serial(self):
        with open(ss.CONFIG_FILE, "a") as f:
            f.write("EXCLUDE_SERIALS=SERIAL9\n")
        self.assertEqual(self.cli("include", "SERIAL9")[0], 0)
        self.assertEqual(self.config().typed["EXCLUDE_SERIALS"], [])

    def test_unknown_slot(self):
        self.assertEqual(self.cli("exclude", "42")[0], 1)

    def test_interval(self):
        self.assertEqual(self.cli("interval", "4", "30")[0], 0)
        self.assertEqual(self.config().overrides, {"SERIAL4": 30})
        rc, out = self.cli("interval", "4", "0")
        self.assertEqual(rc, 1)
        self.assertIn("INTERVAL_OVERRIDES", out)
        self.assertEqual(self.cli("interval", "4", "default")[0], 0)
        self.assertEqual(self.config().overrides, {})
        self.assertEqual(self.cli("interval", "4")[0], 2)

    def test_config_dry_run(self):
        rc, out = self.cli("interval", "4", "30", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("(dry-run) would set", out)
        self.assertEqual(self.config().overrides, {})


if __name__ == "__main__":
    unittest.main()
