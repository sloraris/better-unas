"""How much a read costs: one smartctl per drive, one history call per drive,
read in parallel, and one read shared by the TUI's pages. Run from the
smart-scheduler folder:

    python3 -m unittest discover -s tests
"""

import importlib.util
import json
import os
import threading
import unittest

from test_scheduling import NIGHT, Fake, ss

# A private copy of the module for ReadSmart: the shared one's read_smart is
# replaced by the fixture (Fake.install) in other tests.
_spec = importlib.util.spec_from_file_location(
    "ss_reads_copy", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "smart_scheduler.py"))
fresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fresh)

SMARTCTL = {"ata_smart_data": {"self_test": {"polling_minutes": {"extended": 571},
                                             "status": {"value": 249, "remaining_percent": 90}}},
            "ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": 0}}, {"id": 197, "raw": {"value": 3}},
                                               {"id": 9, "raw": {"value": 12000}}]}}


def patch(test, name, value, mod=ss):
    test.addCleanup(setattr, mod, name, getattr(mod, name))
    setattr(mod, name, value)


class ReadSmart(unittest.TestCase):
    def test_one_smartctl_gives_minutes_attributes_and_progress(self):
        calls = []
        patch(self, "sh", lambda *c, **k: (calls.append(c), (0, json.dumps(SMARTCTL)))[1], fresh)
        minutes, attrs, left = fresh.read_smart("/dev/sda", {5, 197})
        self.assertEqual((minutes, attrs, left), (571, {"5": 0, "197": 3}, 90))
        self.assertEqual(calls, [("smartctl", "-j", "-A", "-c", "/dev/sda")])

    def test_not_running_and_garbage(self):
        idle = json.loads(json.dumps(SMARTCTL))
        idle["ata_smart_data"]["self_test"]["status"] = {"value": 0}
        patch(self, "sh", lambda *c, **k: (0, json.dumps(idle)), fresh)
        self.assertIsNone(fresh.read_smart("/dev/sda", set())[2])
        patch(self, "sh", lambda *c, **k: (1, "not json"), fresh)
        self.assertEqual(fresh.read_smart("/dev/sda", {5}), (None, {}, None))


class EvaluateReads(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.fake.install()
        patch(self, "now", lambda: NIGHT)

    def test_each_drive_read_once_in_parallel(self):
        threads, smart = set(), []

        def read_smart(dev, ids):
            threads.add(threading.get_ident())
            smart.append(dev)
            return 571, {}, None
        patch(self, "read_smart", read_smart)
        cfg = ss.Config("/nonexistent")
        state = {}
        drives = ss.evaluate_drives(cfg, self.fake.storage, "uid", state, ss.Alerts(cfg, state, True), report=False)
        history_calls = [p for _, p, _ in self.fake.calls if "smart-tests" in p]
        self.assertEqual(len(history_calls), len(drives))
        self.assertEqual(sorted(smart), sorted(set(smart)))            # one smartctl per drive
        self.assertEqual(len(smart), len(drives))
        self.assertGreater(len(threads), 1)
        self.assertTrue(all("history" in d for d in drives if not d.get("excluded")))


if __name__ == "__main__":
    unittest.main()
