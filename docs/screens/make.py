"""Regenerate the 80x28 screen captures in docs/screens/ from the repo root:

    python3 docs/screens/make.py

The app runs headless (the same text canvas the tests use) with
smart-scheduler's real plugin, planner and screens. UniFi Drive's API,
smartctl, psql and systemctl are faked by smart-scheduler/tests, with the
anonymized fixtures (SERIAL1-7) and the clock at Sun 22 Nov 2026 01:00.
Nothing here touches a NAS.
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TESTS = os.path.join(ROOT, "smart-scheduler", "tests")
sys.path.insert(0, TESTS)
sys.path.insert(0, ROOT)

from test_calendar import CalendarFixture, with_history   # noqa: E402

SHOTS = {
    "home": [],
    "calendar": ["2"],
    "calendar-day": ["2", "enter"],
    "drives": ["3"],
    "drive": ["3", "down", "down", "down", "enter"],
    "alerts": ["4"],
    "settings": ["5", "down", "down", "enter"],
    "settings-notifications": ["5", "down", "enter"],
    "settings-list": ["5"],
    "settings-better-unas": ["5", "enter"],
}


class Shots(CalendarFixture, unittest.TestCase):
    def test_shots(self):
        with_history(self.fake)
        self.state = {"alerts": {"test_failed:SERIAL4": {
            "msg": "Extended SMART test failed on Drive 4 (SERIAL4)", "level": "warning",
            "since": "2026-11-12T03:15:00-07:00", "sent": "2026-11-12T03:15:00-07:00"}}}
        with open(os.path.join(self.home, "VERSION"), "w") as f:
            f.write("v0.2.0\n")
        with open(os.path.join(self.home, ".update-check.json"), "w") as f:
            json.dump({"checked_at": "2026-11-22T00:30:00-07:00", "latest": "v0.2.0"}, f)
        for name, keys in SHOTS.items():
            app = self.make_app()
            app.home = self.home
            text = app.press(*keys) if keys else self.term.text()
            with open(os.path.join(HERE, name + ".txt"), "w") as f:
                f.write("\n".join(line.rstrip() for line in text.split("\n")) + "\n")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
