"""health-monitor's rules (monitor.py). Run from the health-monitor folder:

    python3 -m unittest discover -s tests

Nothing touches a NAS: readings are built by hand.
"""

import copy
import datetime as dt
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(HERE)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load("hm_monitor", os.path.join(TOOL, "monitor.py"))
T0 = dt.datetime(2026, 11, 22, 1, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7)))
CFG = {"WATCH_ATTRIBUTES": [5, 187, 197, 198], "RISE_ERROR_JUMP": 10, "QUIET_DAYS": 7,
       "EXCLUDE_SERIALS": [], "PREFAIL_MARGIN": 10, "PREFAIL_S5": 100, "PREFAIL_S187": 10,
       "PREFAIL_S197": 10, "PREFAIL_S198": 100, "MD_STATE_ALERTS": False}


def attrs(**raw):
    """{id: attr} from S5=0, S197=3 style keywords; normalized 100, threshold 10 for S5."""
    out = {}
    for k, v in raw.items():
        i = int(k[1:])
        out[i] = {"raw": v, "value": 100, "thresh": 10 if i == 5 else 0}
    return out


def drive(n, **raw):
    return {"drive": n, "asleep": False, "error": None, "attrs": attrs(**raw)}


def md(**kw):
    r = {"state": "clean", "degraded": 0, "sync_action": "idle", "mismatch_cnt": 0, "level": "raid5", "data": True}
    r.update(kw)
    return r


class Smart(unittest.TestCase):
    def setUp(self):
        self.state, self.cfg = {}, dict(CFG)

    def run_at(self, t, smart, btrfs=None, mdr=None, cfg=None):
        return M.sample(cfg or self.cfg, self.state, {"smart": smart, "btrfs": btrfs, "md": mdr}, t)

    def test_first_sample_is_a_silent_baseline(self):
        self.assertEqual(self.run_at(T0, {"SER1": drive(1, S5=0, S197=3, S198=14)}), [])
        self.assertEqual(self.state["smart"], {"SER1": {"5": 0, "197": 3, "198": 14}})
        self.assertIn("baselined", self.state)

    def test_a_rise_is_a_warning_written_drive_and_attribute(self):
        self.run_at(T0, {"SER1": drive(3, S5=0, S197=0)})
        out = self.run_at(T0 + dt.timedelta(minutes=15), {"SER1": drive(3, S5=0, S197=3)})
        self.assertEqual(out, [("raise", "smart:SER1:197", "Drive 3 - S197 0->3 (SER1)", "warning")])

    def test_a_second_rise_while_open_is_an_error(self):
        self.run_at(T0, {"SER1": drive(3, S197=0)})
        self.run_at(T0 + dt.timedelta(hours=1), {"SER1": drive(3, S197=2)})
        out = self.run_at(T0 + dt.timedelta(days=2), {"SER1": drive(3, S197=4)})
        self.assertEqual(out, [("raise", "smart:SER1:197",
                                "Drive 3 - S197 2->4, rose again within 7 days (SER1)", "error")])
        self.assertEqual(self.state["rises"]["smart:SER1:197"]["level"], "error")

    def test_a_big_jump_is_an_error_at_once(self):
        self.run_at(T0, {"SER1": drive(2, S5=0)})
        out = self.run_at(T0 + dt.timedelta(hours=1), {"SER1": drive(2, S5=12)})
        self.assertEqual(out, [("raise", "smart:SER1:5", "Drive 2 - S5 0->12, up 12 at once (SER1)", "error")])

    def test_clears_after_quiet_days_and_a_later_rise_opens_it_again(self):
        self.run_at(T0, {"SER1": drive(1, S197=0)})
        self.run_at(T0 + dt.timedelta(hours=1), {"SER1": drive(1, S197=1)})
        out = self.run_at(T0 + dt.timedelta(days=7, hours=1), {"SER1": drive(1, S197=1)})
        self.assertEqual(out, [("clear", "smart:SER1:197",
                                "No further rise in 7 days: Drive 1 - S197 0->1 (SER1)")])
        out = self.run_at(T0 + dt.timedelta(days=8), {"SER1": drive(1, S197=2)})
        self.assertEqual(out[0][3], "warning")                    # a fresh alert, not an escalation

    def test_a_drop_is_quiet_and_moves_the_baseline(self):
        self.run_at(T0, {"SER1": drive(1, S197=3)})
        self.assertEqual(self.run_at(T0 + dt.timedelta(hours=1), {"SER1": drive(1, S197=0)}), [])
        out = self.run_at(T0 + dt.timedelta(hours=2), {"SER1": drive(1, S197=1)})
        self.assertEqual(out[0][2], "Drive 1 - S197 0->1 (SER1)")

    def test_asleep_unread_and_excluded_drives_keep_their_baseline(self):
        self.run_at(T0, {"SER1": drive(1, S197=0), "SER2": drive(2, S197=0)})
        asleep = {"drive": 1, "asleep": True, "error": None, "attrs": {}}
        self.cfg["EXCLUDE_SERIALS"] = ["SER2"]
        self.assertEqual(self.run_at(T0 + dt.timedelta(hours=1), {"SER1": asleep, "SER2": drive(2, S197=9)}), [])
        self.assertEqual(self.state["smart"]["SER1"], {"197": 0})
        out = self.run_at(T0 + dt.timedelta(hours=2), {"SER1": drive(1, S197=1)})
        self.assertEqual(out[0][1], "smart:SER1:197")

    def test_unwatched_attributes_and_newly_watched_ones(self):
        self.run_at(T0, {"SER1": drive(1, S197=0, S199=0)})
        self.assertEqual(self.run_at(T0 + dt.timedelta(hours=1), {"SER1": drive(1, S197=0, S199=5)}), [])
        self.cfg["WATCH_ATTRIBUTES"] = [197, 199]
        # The first reading of a newly watched attribute is its baseline.
        self.assertEqual(self.run_at(T0 + dt.timedelta(hours=2), {"SER1": drive(1, S197=0, S199=5)}), [])
        self.assertEqual(self.run_at(T0 + dt.timedelta(hours=3), {"SER1": drive(1, S197=0, S199=6)})[0][1],
                         "smart:SER1:199")

    def test_a_drive_without_a_bay_number_is_named_by_serial(self):
        self.run_at(T0, {"SER1": drive(None, S5=0)})
        out = self.run_at(T0 + dt.timedelta(hours=1), {"SER1": drive(None, S5=1)})
        self.assertEqual(out[0][2], "SER1 - S5 0->1 (SER1)")


class PreFail(unittest.TestCase):
    def test_warns_at_install_only(self):
        state = {}
        near = drive(4, S5=120, S197=0)
        out = M.sample(CFG, state, {"smart": {"SER4": near}}, T0)
        self.assertEqual(out, [("raise", "prefail:SER4", "Drive 4 looks close to failing: S5 at 120, limit 100 (SER4)",
                                "warning")])
        # A drive that shows up later gets a silent baseline, near its limits or not.
        out = M.sample(CFG, state, {"smart": {"SER4": near, "SER5": drive(5, S5=500)}}, T0 + dt.timedelta(hours=1))
        self.assertEqual(out, [])

    def test_normalized_value_near_the_threshold(self):
        a = attrs(S5=0)
        a[1] = {"raw": 999, "value": 15, "thresh": 6}         # 9 above its threshold: within 10
        a[3] = {"raw": 0, "value": 40, "thresh": 21}
        self.assertEqual(M.prefail(CFG, a), ["S1 at 15, threshold 6"])

    def test_prefail_alert_clears_after_quiet_days(self):
        state = {}
        M.sample(CFG, state, {"smart": {"SER4": drive(4, S197=10)}}, T0)
        out = M.sample(CFG, state, {"smart": {"SER4": drive(4, S197=10)}}, T0 + dt.timedelta(days=7))
        self.assertEqual([a[:2] for a in out], [("clear", "prefail:SER4")])


class Btrfs(unittest.TestCase):
    def counters(self, **kw):
        c = {"write_io_errs": 0, "read_io_errs": 0, "flush_io_errs": 0, "corruption_errs": 0, "generation_errs": 0}
        c.update(kw)
        return {"/dev/mapper/vg-lv": {"mount": "/volume/x", "counters": c}}

    def test_corruption_is_an_error_and_the_rest_warnings(self):
        state = {}
        self.assertEqual(M.sample(CFG, state, {"btrfs": self.counters(read_io_errs=4)}, T0), [])
        out = M.sample(CFG, state, {"btrfs": self.counters(read_io_errs=5, corruption_errs=2)},
                       T0 + dt.timedelta(minutes=15))
        self.assertEqual(out, [
            ("raise", "btrfs:/dev/mapper/vg-lv:corruption_errs", "btrfs corruption errors on /volume/x: 0->2", "error"),
            ("raise", "btrfs:/dev/mapper/vg-lv:read_io_errs", "btrfs read errors on /volume/x: 4->5", "warning")])

    def test_unreadable_keeps_the_last_values(self):
        state = {}
        M.sample(CFG, state, {"btrfs": self.counters()}, T0)
        M.sample(CFG, state, {"btrfs": None}, T0 + dt.timedelta(minutes=15))
        self.assertEqual(state["btrfs"]["/dev/mapper/vg-lv"]["corruption_errs"], 0)


class Md(unittest.TestCase):
    def test_mismatches_after_a_check_warn_once(self):
        state = {}
        M.sample(CFG, state, {"md": {"md3": md(sync_action="check")}}, T0)
        out = M.sample(CFG, state, {"md": {"md3": md(mismatch_cnt=128)}}, T0 + dt.timedelta(minutes=15))
        self.assertIn(("raise", "md:md3:mismatch", "Parity check on md3 found 128 mismatched sectors", "warning"), out)
        out = M.sample(CFG, state, {"md": {"md3": md(mismatch_cnt=128)}}, T0 + dt.timedelta(minutes=30))
        self.assertFalse([a for a in out if a[0] == "raise"])
        M.sample(CFG, state, {"md": {"md3": md(sync_action="check")}}, T0 + dt.timedelta(days=30))
        out = M.sample(CFG, state, {"md": {"md3": md()}}, T0 + dt.timedelta(days=30, hours=8))
        self.assertIn(("clear", "md:md3:mismatch", "Parity check on md3 found no mismatches"), out)

    def test_state_alerts_off_by_default(self):
        state = {}
        out = M.sample(CFG, state, {"md": {"md3": md(degraded=1)}}, T0)
        self.assertFalse([a for a in out if a[0] == "raise"])

    def test_state_alerts_when_on_cover_data_arrays_only(self):
        cfg = dict(CFG, MD_STATE_ALERTS=True)
        state = {}
        out = M.sample(cfg, state, {"md": {"md0": md(level="raid1", degraded=3, data=False),
                                           "md3": md(degraded=1, sync_action="recover")}}, T0)
        raised = [a for a in out if a[0] == "raise"]
        self.assertEqual(raised, [("raise", "md:md3:state",
                                   "Array md3 (raid5) is degraded: 1 member(s) missing, rebuilding", "error")])
        out = M.sample(cfg, state, {"md": {"md3": md()}}, T0 + dt.timedelta(hours=9))
        self.assertIn(("clear", "md:md3:state", "Array md3 has recovered"), out)


class State(unittest.TestCase):
    def test_sample_leaves_readings_alone(self):
        readings = {"smart": {"SER1": drive(1, S5=0)}, "btrfs": None, "md": None}
        before = copy.deepcopy(readings)
        M.sample(CFG, {}, readings, T0)
        self.assertEqual(readings, before)


if __name__ == "__main__":
    unittest.main()
