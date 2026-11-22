"""Drives screen (#11). Run from the smart-scheduler folder:

    python3 -m unittest discover -s tests

The TUI runs headless at 80x28 with the tool's real plugin, as in
test_calendar (same fixture: UniFi Drive's API, smartctl and psql faked,
clock Sun 22 Nov 2026 01:00). Action flows swap the plugin's `actions` for
FakeActions, so nothing starts a test or writes a config; a few go through
the real actions.py against the fixture's private copy of the tool.

Snapshots: tests/snapshots/drives_*.txt. Regenerate after an intended change:

    UPDATE_SNAPSHOTS=1 python3 -m unittest discover -s tests
"""

import os
import re
import unittest

from test_calendar import CalendarFixture, patch, with_history
from test_scheduling import ss

import actions as real_actions  # noqa: E402  (smart_scheduler.py put its folder on sys.path)

Check = real_actions.Check
RUNNING = Check("running", "a SMART test is already running on slot 1", False)
BUSY = Check("busy", "drive busy (160128 KB/s)", True)
SCRUB = Check("scrub", "would overlap pool 2's data scrub", True)


class FakeActions:
    """Stands in for actions.py: records calls, answers with set checks."""

    Check = Check
    blocking = staticmethod(real_actions.blocking)
    overridable = staticmethod(real_actions.overridable)

    def __init__(self):
        self.checks = []
        self.calls = []
        self.error = None

    def check_run_now(self, slot, kind, cfg=None, t=None, state=None):
        self.calls.append(("check", slot, kind))
        return list(self.checks)

    def run_now(self, slot, kind, override=False, cfg=None, dry_run=False, lock=True, wait=10, t=None):
        self.calls.append(("run", slot, kind, override, dry_run, lock))
        hard, soft = self.blocking(self.checks), self.overridable(self.checks)
        if hard or (soft and not override):
            return real_actions.Result(False, self.checks, "Can't start.")
        word = "(dry-run) would start" if dry_run else "Started"
        article = "an" if kind == "extended" else "a"
        return real_actions.Result(True, self.checks, f"{word} {article} {kind} test on slot {slot}.")

    def exclude(self, serial, on, path=None, dry_run=False, lock=True, wait=10):
        self.calls.append(("exclude", serial, on, os.path.basename(path), dry_run, lock))
        if self.error:
            raise self.error
        return True

    def set_interval(self, serial, days, path=None, dry_run=False, lock=True, wait=10):
        self.calls.append(("interval", serial, days, os.path.basename(path), dry_run, lock))
        if self.error:
            raise self.error
        return True


class DrivesFixture(CalendarFixture):
    def setUp(self):
        super().setUp()
        self.saved = []
        patch(self, "save_state", lambda state, dry: self.saved.append((state, dry)))

    def drives(self, *keys, dry_run=False, fake_actions=True):
        """Open the Drives tab (the 3rd) and press keys; returns the screen text."""
        self.app = self.make_app()
        self.app.dry_run = dry_run
        if fake_actions:
            self.actions = FakeActions()
            patch_obj(self, self.plugin, "actions", self.actions)
        return self.app.press("3", *keys)

    def to_slot(self, slot):
        """From the list's first row, move to `slot` (fixture slots are 1-7)."""
        return self.app.press(*["down"] * (int(slot) - 1))

    def type(self, text, clear=12):
        """Replace a prompt's prefilled text with `text` and press Enter."""
        return self.app.press(*["backspace"] * clear, *text, "enter")

    @property
    def toast(self):
        return self.app.toast.text if self.app.toast else ""


def patch_obj(test, obj, name, value):
    test.addCleanup(setattr, obj, name, getattr(obj, name))
    setattr(obj, name, value)


def rich(test):
    """History, a snooze and a SMART attribute that rose since the last test."""
    with_history(test.fake)
    test.fake.attrs["SERIAL5"] = {"5": 0, "187": 0, "197": 3, "198": 0, "199": 2}
    # Like smartctl, answer only for the attribute IDs asked for (the shared fake answers all).
    attrs = test.fake.attrs
    patch(test, "smart_facts", lambda dev, ids: (571, {k: v for k, v in attrs.get(dev[5:], {}).items()
                                                       if int(k) in ids}))
    test.state["drives"] = {"SERIAL5": {"attrs_at_last_test": {"5": 0, "187": 0, "197": 0, "198": 0}}}
    test.state["snoozes"] = {"SERIAL6": {"until": "2026-11-25", "note": "waiting for a spare",
                                         "set": "2026-11-21T10:00:00-07:00"}}
    test.fake.history["5"].append({"id": 52, "type": "quick", "status": "successful", "result": "optimal",
                                   "createdAt": "2026-11-21T09:00:00-07:00",
                                   "finishedAt": "2026-11-21T09:02:00-07:00"})


# --------------------------------------------------------------------------- screens

def list_rows(text):
    """{slot: line} for the Drives list's rows."""
    out = {}
    for line in text.splitlines()[3:]:
        m = re.match(r"^ [›> ]\s+(\d+) ", line)
        if m:
            out[m.group(1)] = line
    return out


class DrivesScreenTests(DrivesFixture, unittest.TestCase):
    def test_list(self):
        rich(self)
        text = self.drives()
        self.assertSnapshot("drives_list", text)
        self.assertSnapshot("drives_list_tones", self.term.tones())
        self.assertIn("3 Drives", text)
        rows = list_rows(text)
        self.assertEqual(sorted(rows), list("1234567"))
        self.assertNotIn("Status", text)                       # its meaning is in the glyphs now
        self.assertNotIn("SERIAL3", text)                      # serials sit in the selection panel
        self.assertIn("▶ running", rows["1"])
        self.assertIn("✗ Thu 12 Nov", rows["4"])               # the last test, failed
        self.assertIn("✓ Tue 03 Nov", rows["3"])               # the last test, passed
        self.assertIn("never", rows["2"])
        self.assertIn("○ Wed 25 Nov", rows["6"])               # next planned: after the snooze
        self.assertIn("Sun 21 Feb 2027", rows["1"])            # after the running test; year shown
        self.assertIn("48°C", rows["4"])
        self.assertIn("┌─ Slot 1 · ST4000VN006-3CW104 · SERIAL1 ", text)
        self.assertIn("▶ Extended test running, started Sun 00:10. Next: Sun 21 Feb 2027.", text)
        self.assertIn("Enter slot 1", text)

    def test_list_follows_the_selection(self):
        rich(self)
        self.drives()
        text = self.to_slot(5)
        self.assertIn("┌─ Slot 5 · WDC WD4002FYYZ-01B7CB0 · SERIAL5 ", text)
        self.assertIn("○ Next test Mon 23 Nov 00:00 · warning signs · every 30 days", text)
        self.assertIn("Enter slot 5", text)
        text = self.to_slot(2)                                 # 5 + 1 = slot 6, snoozed
        self.assertIn("Snoozed to Wed 25 Nov (waiting for a spare). Next test Wed 25 Nov.", text)
        self.assertIn(("u", "unsnooze"), self.app.screen.keys())   # only offered on a snoozed drive
        self.app.press("up")
        self.assertNotIn(("u", "unsnooze"), self.app.screen.keys())

    def test_list_under_ascii(self):
        rich(self)
        self.app = self.make_app()
        self.term.ascii = self.term.surface.ascii = True
        text = self.app.press("3")
        self.assertIn("41C", text)
        self.assertIn("+- Slot 1 - ST4000VN006-3CW104 - SERIAL1 ", text)
        self.assertIn("> running", text)

    def test_excluded_drive(self):
        self.write_config("EXCLUDE_SERIALS=SERIAL7\n")
        text = self.drives()
        self.assertIn("excluded", list_rows(text)["7"])
        text = self.to_slot(7)
        self.assertIn("Excluded from testing (EXCLUDE_SERIALS). Run now still works.", text)
        self.assertIn("x include", text)
        text = self.app.press("enter")
        self.assertRegex(text, r"Next test +none")
        self.assertRegex(text, r"Why +excluded")
        self.assertRegex(text, r"Excluded +yes  EXCLUDE_SERIALS")
        self.assertIn("excluded", text.splitlines()[4])          # the state, top right

    def test_detail(self):
        rich(self)
        self.drives()
        self.to_slot(5)
        text = self.app.press("enter")
        self.assertSnapshot("drives_detail", text)
        self.assertSnapshot("drives_detail_tones", self.term.tones())
        lines = text.splitlines()
        self.assertIn("└─ Slot 5 ─", lines[2])
        self.assertRegex(lines[4], r"^ WDC WD4002FYYZ-01B7CB0   SERIAL5   4 TB +! warning signs$")
        self.assertIn("Last test   ✓ Tue 20 Oct  passed", text)
        self.assertIn("Next test   ○ Mon 23 Nov 00:00", text)
        self.assertIn("Why         warning signs", text)
        self.assertIn("Interval    30 days  warning signs", text)
        self.assertIn("Power-on            59,080 h  6.7y", text)
        # The count comes first, the attribute id dim after it; a rise is marked.
        self.assertRegex(text, r"Pending sectors +3 ↑3 +197 │")
        self.assertRegex(text, r"Interface CRC errors +2 +199 │")      # no baseline for 199
        self.assertNotIn("↑2", text)
        self.assertIn("quick     Sat 21 Nov 09:00  2m      ✓ passed", text)
        self.assertIn("extended  Tue 20 Oct 01:00  10h 00m ✓ passed", text)
        # Newest first: the quick test is above the extended one.
        self.assertLess(text.index("quick "), text.index("extended  Tue 20 Oct"))
        self.app.press("esc")
        self.assertEqual(type(self.app.screen).__name__, "DrivesScreen")

    def test_detail_snoozed_and_overridden(self):
        rich(self)
        self.write_config("INTERVAL_OVERRIDES=SERIAL6:14\n")
        self.drives()
        self.to_slot(6)
        text = self.app.press("enter")
        self.assertSnapshot("drives_detail_snoozed", text)
        self.assertIn("Interval    14 days  per drive", text)
        self.assertIn("Snoozed     until Wed 25 Nov", text)
        self.assertIn("            ↳ waiting for a spare", text)
        self.assertIn("Why         snoozed", text)
        self.assertIn("Excluded    no", text)
        self.assertIn("Next test   ○ Wed 25 Nov", text)
        self.assertIn("u unsnooze", text)

    def test_detail_failed_and_running(self):
        rich(self)
        self.drives()
        text = self.app.press("enter")
        self.assertIn("Next test   ▶ running now", text)
        self.assertIn("▶ test running", text.splitlines()[4])
        self.assertIn("extended  Sun 22 Nov 00:10          ▶ running", text)
        self.app.press("esc")
        self.to_slot(4)
        text = self.app.press("enter")
        self.assertIn("✗ last test failed", text.splitlines()[4])
        self.assertIn("Last test   ✗ Thu 12 Nov  failed", text)
        self.assertIn("Why         retry", text)
        self.assertIn("Interval    60 days  old drive", text)
        self.assertIn("2h 10m  ✗ failed", text)
        self.assertNotIn("failed/failed", text)               # the plain failure says it once

    def test_detail_without_smartctl(self):
        ss.serial_to_device = lambda: {}
        self.drives("enter")
        text = self.term.text()
        self.assertIn("↳ unknown: no smartctl reading", text)
        self.assertNotIn("Pending sectors", text)
        self.assertIn("Temperature                   41°C", text)   # from UniFi Drive, not smartctl

    def test_api_problem_is_shown_not_raised(self):
        ss.api = lambda *a, **k: (500, None)
        text = self.drives()
        self.assertIn("UniFi Drive API problem: GET /storage returned HTTP 500", text)
        self.assertIn("Press r to try again.", text)
        self.app.press("enter", "t", "e", "x", "z")             # nothing to act on; no crash
        self.assertEqual(self.actions.calls, [])

    def test_plugin_lists_the_drives_screen(self):
        self.make_app()
        tabs = [s["tab"] for s in self.plugin.screens(self.ctx)]
        self.assertEqual(tabs, ["Calendar", "Drives"])
        ss_, actions, _ = self.plugin._env()
        self.assertIs(actions.ss, self.plugin.ss)          # actions follow a swapped-in ss
        self.assertIs(ss_, ss)


# --------------------------------------------------------------------------- run now

class RunNowTests(DrivesFixture, unittest.TestCase):
    def test_blocking_check_shows_the_list_and_never_offers_to_start(self):
        self.drives()
        self.actions.checks = [RUNNING, BUSY]
        self.to_slot(5)
        text = self.app.press("t", "e")
        self.assertSnapshot("drives_run_blocked", text)
        self.assertIn("BLOCKS        a SMART test is already running on slot 1", text)
        self.assertIn("can override  drive busy", text)
        self.assertEqual(self.actions.calls, [("check", "5", "extended")])   # never run_now
        self.assertEqual(type(self.app.screen).__name__, "ChecksScreen")
        self.app.press("esc")
        self.assertEqual(type(self.app.screen).__name__, "DrivesScreen")

    def test_overridable_check_needs_a_second_confirm(self):
        self.drives()
        self.actions.checks = [BUSY, SCRUB]
        self.to_slot(2)
        shots = []
        read = self.app.read_key

        def spy():                          # the screen as each dialog waits for a key
            if self.app.overlay is not None:
                shots.append(self.term.text())
            return read()
        patch_obj(self, self.app, "read_key", spy)
        # Yes, then no at the second confirm: nothing starts.
        self.app.press("t", "e", "y", "n")
        self.assertEqual(self.actions.calls, [("check", "2", "extended")])
        self.assertEqual(self.toast, "Not started.")
        first, second = shots
        self.assertSnapshot("drives_run_override_first", first)
        self.assertSnapshot("drives_run_override_second", second)
        self.assertIn("Start an extended test on slot 2 (SERIAL2) anyway?", first)
        self.assertIn("can override  drive busy (160128 KB/s)", first)
        self.assertIn("can override  would overlap pool 2's data scrub", first)
        self.assertIn("Second confirm", second)
        self.assertIn("Override these checks and start the test?", second)
        # Yes twice: started with override, without taking the lock a second time.
        self.app.press("t", "e", "y", "y")
        self.assertEqual(self.actions.calls[-1], ("run", "2", "extended", True, False, False))
        self.assertEqual(self.toast, "Started an extended test on slot 2.")

    def test_no_failed_checks_needs_one_confirm(self):
        self.drives()
        self.to_slot(3)
        self.app.press("t", "k", "y")
        self.assertEqual(self.actions.calls, [("check", "3", "quick"), ("run", "3", "quick", False, False, False)])
        self.assertIn("Started a quick test on slot 3.", self.toast)

    def test_declining_starts_nothing(self):
        self.drives()
        self.app.press("t", "e", "n")
        self.assertEqual([c[0] for c in self.actions.calls], ["check"])
        self.app.press("t", "esc")                          # cancel at the kind picker
        self.assertEqual([c[0] for c in self.actions.calls], ["check"])
        self.assertEqual(type(self.app.screen).__name__, "DrivesScreen")

    def test_from_the_details(self):
        self.drives()
        self.to_slot(4)
        self.app.press("enter", "t", "e", "y")
        self.assertEqual(self.actions.calls[-1], ("run", "4", "extended", False, False, False))
        self.assertEqual(type(self.app.screen).__name__, "DriveScreen")

    def test_dry_run_says_what_would_happen(self):
        self.drives(dry_run=True)
        self.app.press("t", "e", "y")
        self.assertEqual(self.actions.calls[-1], ("run", "1", "extended", False, True, False))
        self.assertIn("(dry-run) would start", self.toast)

    def test_real_actions_block_on_a_running_test(self):
        # The real check_run_now, with the fixture's running test on slot 1.
        with_history(self.fake)
        self.drives(fake_actions=False)
        self.to_slot(3)
        text = self.app.press("t", "e")
        self.assertIn("BLOCKS        a SMART test is already running on slot 1", text)
        self.assertFalse([c for c in self.fake.calls if c[0] == "POST"])


# --------------------------------------------------------------------------- snooze, exclude, interval

class SnoozeTests(DrivesFixture, unittest.TestCase):
    def test_snooze_with_a_date_and_note(self):
        self.drives()
        self.to_slot(5)
        text = self.app.press("z")
        self.assertIn("Snooze until (YYYY-MM-DD or days, then a note): 2026-11-29", text)
        self.type("2026-12-01 waiting for a spare")
        (state, dry), = self.saved
        self.assertFalse(dry)
        self.assertEqual(state["snoozes"]["SERIAL5"]["until"], "2026-12-01")
        self.assertEqual(state["snoozes"]["SERIAL5"]["note"], "waiting for a spare")
        self.assertIn("Snoozed slot 5 (SERIAL5): no test before Tue 01 Dec 2026", self.toast)

    def test_snooze_in_days(self):
        self.drives()
        self.app.press("z")
        self.type("3")
        self.assertEqual(self.saved[0][0]["snoozes"]["SERIAL1"]["until"], "2026-11-25")

    def test_digits_and_q_go_into_the_prompt(self):
        self.drives()
        self.app.press("z")
        self.type("10 quiet please")
        self.assertTrue(self.app.running)                                   # "q" didn't quit
        z = self.saved[0][0]["snoozes"]["SERIAL1"]
        self.assertEqual((z["until"], z["note"]), ("2026-12-02", "quiet please"))
        self.assertEqual(type(self.app.screen).__name__, "DrivesScreen")   # "3"/"1" didn't switch tabs

    def test_prompt_and_picker_never_outlive_the_screen(self):
        self.drives()
        self.app.press("z", "tab", "2")     # an open prompt keeps every key, tab and digits too
        self.assertEqual(type(self.app.screen).__name__, "DrivesScreen")
        self.assertIsNotNone(self.app.screen.actions.prompt)
        self.app.press("esc")
        self.assertIsNone(self.app.screen.actions.prompt)
        self.app.press("t", "2", "3")       # the kind picker is dropped by a tab switch
        self.assertIsNone(self.app.screen.actions.picking)
        self.app.press("e")
        self.assertEqual(self.actions.calls, [])
        self.assertEqual(self.saved, [])

    def test_bad_snooze_changes_nothing(self):
        self.drives()
        self.app.press("z")
        self.type("2026-11-01")
        self.assertEqual(self.saved, [])
        self.assertIn("Not snoozed: 2026-11-01 isn't after today", self.toast)
        self.app.press("z")
        self.type("soon")
        self.assertIn("Not snoozed: not a date", self.toast)
        self.app.press("z", "esc")
        self.assertEqual(self.saved, [])

    def test_unsnooze(self):
        self.state["snoozes"] = {"SERIAL6": {"until": "2026-11-25", "set": "2026-11-21T10:00:00-07:00"}}
        self.drives()
        self.to_slot(6)
        self.app.press("u", "y")
        (state, _), = self.saved
        self.assertNotIn("snoozes", state)
        self.assertEqual(self.toast, "Removed the snooze on slot 6 (SERIAL6).")
        self.app.press("up", "u")
        self.assertEqual(self.toast, "Slot 5 (SERIAL5) isn't snoozed.")

    def test_dry_run_writes_nothing(self):
        self.state["snoozes"] = {"SERIAL6": {"until": "2026-11-25", "set": "2026-11-21T10:00:00-07:00"}}
        self.drives(dry_run=True)
        self.app.press("z")
        self.type("2026-12-01")
        self.assertEqual(self.saved, [])
        self.assertIn("(dry-run) would snooze slot 1 (SERIAL1): no test before Tue 01 Dec 2026", self.toast)
        self.to_slot(6)
        self.app.press("u", "y")
        self.assertEqual(self.saved, [])
        self.assertEqual(self.toast, "(dry-run) would remove the snooze on slot 6 (SERIAL6).")


class ConfigEditTests(DrivesFixture, unittest.TestCase):
    def test_exclude_and_include(self):
        self.drives()
        self.to_slot(5)
        self.app.press("x", "y")
        self.assertEqual(self.actions.calls, [("exclude", "SERIAL5", True, "config", False, False)])
        self.assertEqual(self.toast, "Excluded slot 5 (SERIAL5).")
        self.write_config("EXCLUDE_SERIALS=SERIAL5\n")
        self.app.press("r", "x", "y")
        self.assertEqual(self.actions.calls[-1], ("exclude", "SERIAL5", False, "config", False, False))

    def test_exclude_declined(self):
        self.drives("x", "n")
        self.assertEqual(self.actions.calls, [])

    def test_set_interval(self):
        self.drives()
        self.to_slot(2)
        text = self.app.press("i")
        self.assertIn("Interval in days (empty: the normal rules):", text)
        self.type("14")
        self.assertEqual(self.actions.calls, [("interval", "SERIAL2", 14, "config", False, False)])
        self.assertEqual(self.toast, "Slot 2 (SERIAL2) now uses every 14 days.")
        self.app.press("i")
        self.type("")
        self.assertEqual(self.actions.calls[-1], ("interval", "SERIAL2", None, "config", False, False))
        self.app.press("i")
        self.type("two")
        self.assertEqual(len(self.actions.calls), 2)
        self.assertIn("isn't a whole number of days", self.toast)

    def test_validation_error_is_shown(self):
        self.drives()
        self.actions.error = ss.settings.ValidationError({"INTERVAL_OVERRIDES": "SERIAL1: at most 3650"})
        self.app.press("i")
        self.type("9999")
        self.assertEqual(self.toast, "Not changed: INTERVAL_OVERRIDES: SERIAL1: at most 3650.")

    def test_dry_run(self):
        self.drives(dry_run=True)
        self.app.press("x", "y")
        self.assertEqual(self.actions.calls[-1][-2:], (True, False))
        self.assertEqual(self.toast, "(dry-run) would exclude slot 1 (SERIAL1) (EXCLUDE_SERIALS).")
        self.app.press("i")
        self.type("30")
        self.assertEqual(self.toast, "(dry-run) would set slot 1 (SERIAL1) to every 30 days (INTERVAL_OVERRIDES).")

    def test_real_actions_edit_the_tool_config(self):
        # The real actions.py, writing the fixture's private copy of the config.
        self.drives(fake_actions=False)
        self.to_slot(5)
        self.app.press("x", "y")
        self.app.press("down", "i")
        self.type("21")
        with open(os.path.join(self.tool_dir, "config")) as f:
            text = f.read()
        self.assertIn("EXCLUDE_SERIALS=SERIAL5", text)
        self.assertIn("INTERVAL_OVERRIDES=SERIAL6:21", text)
        screen = self.app.screen
        self.assertIn("excluded", list_rows(self.term.text())["5"])
        self.assertEqual(next(d for d in screen.data["drives"] if d["serial"] == "SERIAL6")["interval"], 21)
        self.app.press("i")
        self.type("99999")
        self.assertIn("Not changed: INTERVAL_OVERRIDES", self.toast)

    def test_real_dry_run_leaves_the_config_alone(self):
        self.drives(dry_run=True, fake_actions=False)
        self.app.press("x", "y")
        self.assertFalse(os.path.exists(os.path.join(self.tool_dir, "config")))
        self.assertIn("(dry-run) would exclude", self.toast)


if __name__ == "__main__":
    unittest.main()
