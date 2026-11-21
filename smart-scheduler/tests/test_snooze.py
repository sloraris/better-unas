"""Snoozes (#6): "not before <date>" per drive, kept in state.json.

    python3 -m unittest discover -s tests

Uses the same anonymized storage fixture and fakes as test_scheduling.
Nothing touches a NAS.
"""

import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from test_scheduling import NIGHT, TZ, Fake, ss

planner = ss.planner


def at(*a):
    return dt.datetime(*a, tzinfo=TZ)


def patch(test, name, value):
    test.addCleanup(setattr, ss, name, getattr(ss, name))
    setattr(ss, name, value)


class SnoozeStateTests(unittest.TestCase):
    def test_set_active_and_expire(self):
        state = {}
        ss.set_snooze(state, "SERIAL6", dt.date(2026, 11, 25), "  waiting for   a spare ", t=NIGHT)
        self.assertEqual(state["snoozes"]["SERIAL6"]["until"], "2026-11-25")
        self.assertEqual(state["snoozes"]["SERIAL6"]["note"], "waiting for a spare")
        self.assertIn("SERIAL6", ss.active_snoozes(state, dt.date(2026, 11, 24)))
        self.assertEqual(ss.active_snoozes(state, dt.date(2026, 11, 25)), {})   # the day itself is free
        self.assertEqual(ss.expire_snoozes(state, dt.date(2026, 11, 24)), [])
        self.assertEqual(ss.expire_snoozes(state, dt.date(2026, 11, 25)), ["SERIAL6"])
        self.assertNotIn("snoozes", state)

    def test_past_or_today_is_refused(self):
        with self.assertRaises(ValueError):
            ss.set_snooze({}, "SERIAL6", NIGHT.date(), t=NIGHT)

    def test_unreadable_entries_are_dropped(self):
        state = {"snoozes": {"A": {"until": "soon"}, "B": "x", "C": {"until": "2027-01-01"}}}
        self.assertEqual(ss.expire_snoozes(state, NIGHT.date()), ["A", "B"])
        self.assertEqual(list(state["snoozes"]), ["C"])

    def test_not_before_is_midnight_local(self):
        state = {"snoozes": {"SERIAL6": {"until": "2026-11-25", "note": "RMA"}}}
        self.assertEqual(ss.not_before(state, NIGHT),
                         {"SERIAL6": (at(2026, 11, 25), "snoozed until Wed 25 Nov 2026 (RMA)")})
        self.assertEqual(ss.not_before(state, at(2026, 11, 25, 1)), {})

    def test_clear(self):
        state = {"snoozes": {"SERIAL6": {"until": "2026-11-25"}}}
        self.assertIsNotNone(ss.clear_snooze(state, "SERIAL6"))
        self.assertEqual(state, {})
        self.assertIsNone(ss.clear_snooze(state, "SERIAL6"))


class SnoozeSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.fake.install()
        self.fake.attrs["SERIAL6"] = {"198": 14}   # most urgent drive in the fixture
        self.cfg = ss.Config("/nonexistent")
        self.state = {"snoozes": {"SERIAL6": {"until": "2026-11-25", "note": "RMA"}}}
        patch(self, "now", lambda: NIGHT)
        patch(self, "backup_windows", lambda *a, **k: ([], None))
        patch(self, "self_check", lambda *a, **k: (self.fake.storage, "uid"))
        patch(self, "load_state", lambda: self.state)
        self.saved = []
        patch(self, "save_state", lambda state, dry: self.saved.append((json.loads(json.dumps(state)), dry)))
        self.started = []
        patch(self, "start_test", lambda cfg, d, *a: self.started.append(d["serial"]))

    def run_at(self, t, dry=True):
        ss.now = lambda: t

        class Args:
            dry_run, source = dry, "manual"
        ss.cmd_run(self.cfg, Args)

    def drives(self, t=NIGHT):
        return ss.evaluate_drives(self.cfg, self.fake.storage, "uid", {}, ss.Alerts(self.cfg, {}, True), report=False)

    def test_snoozed_drive_is_skipped_by_the_run(self):
        self.run_at(NIGHT)
        self.assertEqual(self.started, ["SERIAL4"])   # next most urgent

    def test_snooze_expires_and_the_drive_goes_first(self):
        self.run_at(at(2026, 11, 25, 1), dry=False)
        self.assertEqual(self.started, ["SERIAL6"])
        self.assertNotIn("snoozes", self.saved[-1][0])

    def test_dry_run_keeps_expired_snoozes_on_disk(self):
        self.run_at(at(2026, 11, 25, 1), dry=True)
        self.assertTrue(all(dry for _, dry in self.saved))

    def test_planner_places_the_drive_after_the_snooze(self):
        p = planner.plan(self.fake.storage, self.drives(), {}, self.cfg, NIGHT,
                         not_before=ss.not_before(self.state, NIGHT))
        six = [e for e in p if e["serial"] == "SERIAL6"][0]
        self.assertEqual(six["night"], "2026-11-25")
        self.assertIn("snoozed until Wed 25 Nov 2026 (RMA)", six["delayed_by"])
        self.assertEqual(p[0]["serial"], "SERIAL4")

    def test_status_shows_the_snooze(self):
        patch(self, "get_storage", lambda: self.fake.storage)
        patch(self, "resolve_identity", lambda cfg: ("uid", "the console owner", None))
        out = io.StringIO()
        with redirect_stdout(out):
            ss.cmd_status(self.cfg, None)
        text = out.getvalue()
        self.assertIn("  Next test: not before Wed 25 Nov 2026 (snoozed)", text)
        self.assertIn("  Snoozed:   until Wed 25 Nov 2026 (RMA)", text)
        self.assertIn("Wed 25 Nov 00:00  slot 6 (SERIAL6)", text)


class SnoozeCliTests(unittest.TestCase):
    """Through main(): argument parsing, the lock and state.json on disk."""

    def setUp(self):
        self.fake = Fake()
        self.fake.install()
        tmp = tempfile.mkdtemp()
        patch(self, "STATE_FILE", os.path.join(tmp, "state.json"))
        patch(self, "LOCK_FILE", os.path.join(tmp, ".lock"))
        patch(self, "CONFIG_FILE", os.path.join(tmp, "config"))
        patch(self, "get_storage", lambda: self.fake.storage)
        patch(self, "now", lambda: NIGHT)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["smart_scheduler.py", *argv]), \
                mock.patch.object(ss.os, "geteuid", return_value=0), redirect_stdout(out), redirect_stderr(err):
            rc = ss.main()
        return rc, out.getvalue() + err.getvalue()

    def state(self):
        with open(ss.STATE_FILE) as f:
            return json.load(f)

    def test_round_trip_by_slot_and_serial(self):
        rc, out = self.main("snooze", "6", "2026-12-01", "--note", "waiting for RMA")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "Snoozed slot 6 (SERIAL6): no test before Tue 01 Dec 2026 (waiting for RMA).")
        self.assertEqual(self.state()["snoozes"]["SERIAL6"]["until"], "2026-12-01")
        rc, out = self.main("unsnooze", "SERIAL6")
        self.assertEqual((rc, out.strip()), (0, "Removed the snooze on slot 6 (SERIAL6)."))
        self.assertNotIn("snoozes", self.state())
        rc, out = self.main("unsnooze", "6")
        self.assertEqual((rc, out.strip()), (0, "slot 6 (SERIAL6) isn't snoozed."))

    def test_bad_input(self):
        self.assertEqual(self.main("snooze", "6")[0], 2)
        self.assertEqual(self.main("snooze", "6", "1 Dec")[0], 2)
        rc, out = self.main("snooze", "99", "2026-12-01")
        self.assertEqual((rc, out.strip()), (1, "no drive with slot or serial '99'."))
        rc, out = self.main("snooze", "6", "2026-11-01")
        self.assertEqual(rc, 1)
        self.assertIn("isn't after today", out)
        self.assertFalse(os.path.exists(ss.STATE_FILE))

    def test_dry_run_writes_nothing(self):
        rc, out = self.main("snooze", "6", "2026-12-01", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("(dry-run) would snooze slot 6"))
        self.assertFalse(os.path.exists(ss.STATE_FILE))

    def test_unsnooze_a_removed_drive_while_the_api_is_down(self):
        ss.save_state({"snoozes": {"GONE1": {"until": "2026-12-01"}}}, False)

        def down():
            raise ss.ApiError("unreachable", "connection refused")
        ss.get_storage = down
        rc, out = self.main("unsnooze", "GONE1")
        self.assertEqual((rc, out.strip()), (0, "Removed the snooze on GONE1."))
        rc, out = self.main("snooze", "6", "2026-12-01")
        self.assertEqual(rc, 1)
        self.assertIn("can't read the drive list (connection refused)", out)

    def test_waits_for_the_run_lock(self):
        held = ss.take_lock(0)
        self.addCleanup(held.close)
        self.assertIsNone(ss.take_lock(0))
        with mock.patch.object(ss, "take_lock", wraps=ss.take_lock) as take:
            take.side_effect = lambda wait: None   # still busy after the wait
            rc, out = self.main("snooze", "6", "2026-12-01")
        take.assert_called_once_with(120)
        self.assertEqual((rc, out.strip()), (1, "Another run is in progress; skipping this one."))
        self.assertFalse(os.path.exists(ss.STATE_FILE))


if __name__ == "__main__":
    unittest.main()
