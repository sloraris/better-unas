"""health-monitor's readers, run, heartbeat, settings and Home card. Run from
the health-monitor folder:

    python3 -m unittest discover -s tests

Nothing touches a NAS: smartctl, btrfs and sysfs come from fixtures, and the
run's reads, triggers and pings are faked.
"""

import argparse
import datetime as dt
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOL)
FIX = os.path.join(HERE, "fixtures")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


hm = load("hm_tool", os.path.join(TOOL, "health_monitor.py"))
P = load("core_plugins_for_hm", os.path.join(ROOT, "core", "plugins.py"))   # tests only


class Readers(unittest.TestCase):
    def test_smartctl_awake(self):
        r = hm.parse_smartctl(fixture("smartctl_awake.json"))
        self.assertFalse(r["asleep"])
        self.assertIsNone(r["error"])
        self.assertEqual(r["attrs"][197], {"raw": 3, "value": 100, "thresh": 0})
        self.assertEqual(r["attrs"][1]["thresh"], 6)

    def test_smartctl_standby_is_skipped_not_an_error(self):
        self.assertEqual(hm.parse_smartctl(fixture("smartctl_standby.json")),
                         {"asleep": True, "error": None, "attrs": {}})

    def test_smartctl_garbage(self):
        self.assertEqual(hm.parse_smartctl("")["error"], "smartctl gave no JSON")
        self.assertEqual(hm.parse_smartctl('{"smartctl": {"exit_status": 1}}')["error"], "no SMART attributes")

    def test_btrfs_stats_and_mounts(self):
        self.assertEqual(hm.parse_btrfs_stats(fixture("btrfs_stats.txt")),
                         {"/dev/mapper/vgdata-lvdata": {"write_io_errs": 0, "read_io_errs": 0, "flush_io_errs": 0,
                                                        "corruption_errs": 0, "generation_errs": 0}})
        # Two subvolumes of one filesystem: read once, at the first mount.
        self.assertEqual(hm.btrfs_mounts(fixture("mounts.txt")),
                         {"/dev/mapper/vgdata-lvdata": "/volume/VOLUME-ID"})

    def test_read_btrfs(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        mounts = os.path.join(d, "mounts")
        shutil.copy(os.path.join(FIX, "mounts.txt"), mounts)
        calls = []

        def sh(*cmd, timeout=60):
            calls.append(cmd)
            return 0, fixture("btrfs_stats.txt")
        with mock.patch.object(hm, "sh", sh), mock.patch.object(hm.shutil, "which", lambda x: "/usr/bin/" + x):
            out = hm.read_btrfs(mounts)
        self.assertEqual(calls, [("btrfs", "device", "stats", "/volume/VOLUME-ID")])
        self.assertEqual(out["/dev/mapper/vgdata-lvdata"]["mount"], "/volume/VOLUME-ID")
        with mock.patch.object(hm, "sh", lambda *c, timeout=60: (1, "ERROR: not a btrfs")), \
                mock.patch.object(hm.shutil, "which", lambda x: "/usr/bin/" + x):
            self.assertIsNone(hm.read_btrfs(mounts))

    def make_md(self, root, name, holders=(), **files):
        os.makedirs(os.path.join(root, name, "md"))
        os.makedirs(os.path.join(root, name, "holders"))
        for h in holders:
            open(os.path.join(root, name, "holders", h), "w").close()
        for k, v in files.items():
            with open(os.path.join(root, name, "md", k), "w") as f:
                f.write(f"{v}\n")

    def test_read_md_tells_data_arrays_from_swap(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.make_md(d, "md0", array_state="clean", degraded=3, sync_action="idle", mismatch_cnt=0, level="raid1")
        self.make_md(d, "md3", holders=("dm-0",), array_state="clean", degraded=0, sync_action="check",
                     mismatch_cnt=0, level="raid5")
        os.makedirs(os.path.join(d, "sda"))
        out = hm.read_md(d, os.path.join(FIX, "swaps.txt"), os.path.join(FIX, "mounts.txt"))
        self.assertEqual(sorted(out), ["md0", "md3"])
        self.assertFalse(out["md0"]["data"])
        self.assertEqual(out["md0"]["degraded"], 3)
        self.assertTrue(out["md3"]["data"])
        self.assertEqual(out["md3"]["sync_action"], "check")

    def test_bay_drives_prefers_the_api_numbers(self):
        lsblk = "sda SER1 sata disk\nsdb SER2 sata disk\nsde SER5 sata disk\nmmcblk0 EMMC1 mmc disk\n"
        with mock.patch.object(hm, "sh", lambda *c, timeout=60: (0, lsblk)), \
                mock.patch.object(hm, "storage_slots", lambda: {"SER1": 1, "SER2": 2, "SER5": 5, "GONE": 7}):
            self.assertEqual(hm.bay_drives(), {"SER1": {"device": "/dev/sda", "drive": 1},
                                               "SER2": {"device": "/dev/sdb", "drive": 2},
                                               "SER5": {"device": "/dev/sde", "drive": 5}})
        with mock.patch.object(hm, "sh", lambda *c, timeout=60: (0, lsblk)), \
                mock.patch.object(hm, "storage_slots", lambda: None):
            self.assertEqual(sorted(hm.bay_drives()), ["SER1", "SER2", "SER5"])   # SATA only, no numbers


class Heartbeat(unittest.TestCase):
    def cfg(self, **kw):
        base = {"HEARTBEAT_URL": "https://hc/shared", "HEARTBEAT_FAIL_URL": "https://hc/shared/fail",
                "OWN_HEARTBEAT_URL": "", "OWN_HEARTBEAT_FAIL_URL": ""}
        base.update(kw)
        return base

    def test_shared_pair_by_default(self):
        self.assertEqual(hm.heartbeat_urls(self.cfg()), ("https://hc/shared", "https://hc/shared/fail"))

    def test_own_pair_wins_whole(self):
        self.assertEqual(hm.heartbeat_urls(self.cfg(OWN_HEARTBEAT_URL="https://hc/hm")), ("https://hc/hm", ""))
        self.assertEqual(hm.heartbeat_urls(self.cfg(OWN_HEARTBEAT_URL="https://hc/hm",
                                                    OWN_HEARTBEAT_FAIL_URL="https://hc/hm/fail")),
                         ("https://hc/hm", "https://hc/hm/fail"))


class Run(unittest.TestCase):
    """cmd_run with the NAS reads, triggers and pings faked."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        cfg_path = os.path.join(self.dir, "config")
        with open(cfg_path, "w") as f:
            f.write("OWN_HEARTBEAT_URL=https://hc/hm\nOWN_HEARTBEAT_FAIL_URL=https://hc/hm/fail\n")
        self.cfg = hm.Config(cfg_path, core_path=os.path.join(self.dir, "better-unas.conf"))
        self.pings = []
        self.readings = {"smart": {"SER1": {"drive": 1, "asleep": False, "error": None,
                                            "attrs": {197: {"raw": 0, "value": 100, "thresh": 0}}},
                                   "SER2": {"drive": 2, "asleep": True, "error": None, "attrs": {}}},
                         "btrfs": {"/dev/mapper/vg-lv": {"mount": "/volume/x", "counters": {"corruption_errs": 0}}},
                         "md": {"md3": {"state": "clean", "degraded": 0, "sync_action": "idle", "mismatch_cnt": 0,
                                        "level": "raid5", "data": True}}}
        for name, value in (("STATE_FILE", os.path.join(self.dir, "state.json")),
                            ("verify_manifest", lambda: []), ("heal", lambda dry: []),
                            ("trigger_problems", lambda: []), ("read_all", lambda: self.readings),
                            ("ping", self.pings.append)):
            p = mock.patch.object(hm, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(hm.shutil, "which", lambda x: "/usr/bin/" + x)
        p.start()
        self.addCleanup(p.stop)

    def run_once(self):
        with mock.patch("builtins.print"):
            return hm.cmd_run(self.cfg, argparse.Namespace(dry_run=False, source="timer"))

    def test_baseline_then_a_corruption_error(self):
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.pings, ["https://hc/hm"])
        state = hm.load_state()
        self.assertEqual(state["last"]["drives"], 2)
        self.assertEqual(state["last"]["asleep"], 1)
        self.assertEqual(state["last"]["volumes"], 1)
        self.readings["btrfs"]["/dev/mapper/vg-lv"]["counters"]["corruption_errs"] = 1
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(self.pings[-1], "https://hc/hm/fail")
        state = hm.load_state()
        self.assertEqual(state["alerts"]["btrfs:/dev/mapper/vg-lv:corruption_errs"]["msg"],
                         "btrfs corruption errors on /volume/x: 0->1")

    def test_turned_off_removes_the_triggers(self):
        removed = []
        self.cfg.typed["ENABLED"] = False
        with mock.patch.object(hm, "remove_triggers", lambda dry: removed.append(1) or ["/etc/cron.d/health-monitor"]):
            self.assertEqual(self.run_once(), 0)
            self.run_once()
        self.assertEqual(removed, [1])                      # once, not every 15 minutes
        self.assertEqual(self.pings, [])


class Settings(unittest.TestCase):
    def test_settings_module_matches_smart_scheduler(self):
        def body(path):
            with open(path) as f:
                s = f.read()
            return s[s.index('"""', 3) + 3:]
        self.assertEqual(body(os.path.join(TOOL, "settings.py")),
                         body(os.path.join(ROOT, "smart-scheduler", "settings.py")))

    def test_schema_and_example(self):
        schema = P.load_schema(os.path.join(TOOL, "settings.json"))
        self.assertEqual(P.check_example(schema, os.path.join(TOOL, "config.example")), [])
        self.assertFalse(set(P.field_map(schema)) & set(P.SHARED_KEYS))     # shared keys live in the core

    def test_defaults(self):
        cfg = hm.Config(os.path.join(tempfile.gettempdir(), "no-such-hm-config"),
                        core_path=os.path.join(tempfile.gettempdir(), "no-such-core-config"))
        self.assertEqual(cfg["WATCH_ATTRIBUTES"], [5, 187, 197, 198])
        self.assertFalse(cfg["MD_STATE_ALERTS"])
        self.assertEqual(cfg["QUIET_DAYS"], 7)
        self.assertEqual(cfg.errors, [])


class FakeCtx:
    def __init__(self, **cfg):
        self.values = {"ENABLED": True, "MD_STATE_ALERTS": False}
        self.values.update(cfg)

    def config(self):
        return dict(self.values)

    def config_errors(self):
        return {}


class Card(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.plugin = load("hm_plugin", os.path.join(TOOL, "plugin.py"))
        self.state_file = os.path.join(self.dir, "state.json")
        p = mock.patch.object(self.plugin.hm, "STATE_FILE", self.state_file)
        p.start()
        self.addCleanup(p.stop)

    def write(self, state):
        with open(self.state_file, "w") as f:
            json.dump(state, f)

    LAST = {"at": "2026-11-22T14:15:00-07:00", "drives": 7, "asleep": 3, "unread": [], "volumes": 1,
            "arrays": {"md0": {"state": "clean", "degraded": 3, "sync_action": "idle", "level": "raid1", "data": False},
                       "md3": {"state": "clean", "degraded": 0, "sync_action": "idle", "level": "raid5", "data": True}}}

    def test_no_check_yet(self):
        self.assertEqual(self.plugin.summary(FakeCtx())["state"], "unknown")

    def test_quiet_card(self):
        self.write({"last": self.LAST})
        s = self.plugin.summary(FakeCtx())
        self.assertEqual((s["state"], s["headline"]), ("ok", "Watching 7 drives"))
        self.assertEqual([(i["label"], i["value"]) for i in s["items"]], [
            ("SMART", "7 drives, no rises, 3 asleep"),
            ("btrfs", "no errors, 1 volume(s)"),
            ("Parity", "no mismatches found"),
            ("Arrays", "alerts off (UNAS alerts on these)"),
            ("Last check", "Sun 22 Nov 14:15")])
        on = self.plugin.summary(FakeCtx(MD_STATE_ALERTS=True))
        self.assertEqual(on["items"][3]["value"], "md3 clean")

    def test_alerts_show_on_their_line(self):
        self.write({"last": self.LAST, "alerts": {
            "smart:SER3:197": {"msg": "Drive 3 - S197 0->3 (SER3)", "level": "warning", "sent": self.LAST["at"],
                               "since": self.LAST["at"]},
            "md:md3:mismatch": {"msg": "Parity check on md3 found 8 mismatched sectors", "level": "warning",
                                "sent": self.LAST["at"], "since": self.LAST["at"]}}})
        with mock.patch.object(self.plugin, "_scheduler_installed", lambda: True):
            s = self.plugin.summary(FakeCtx())
        self.assertEqual((s["state"], s["headline"]), ("warn", "2 open alerts"))
        smart = s["items"][0]
        self.assertEqual((smart["value"], smart["status"], smart["target"]),
                         ("Drive 3 - S197 0->3 (SER3)", "warning", "Drives:SER3"))
        self.assertEqual(s["items"][2]["value"], "Parity check on md3 found 8 mismatched sectors")

    def test_alerts_list_active_and_resolved(self):
        t = self.LAST["at"]
        self.write({"alerts": {"btrfs:d:corruption_errs": {"msg": "m", "level": "error", "sent": t, "since": t}},
                    "resolved": [{"key": "smart:S:5", "msg": "old", "level": "warning", "since": t, "sent": t,
                                  "resolved": t},
                                 {"key": "btrfs:d:corruption_errs", "msg": "again", "level": "error", "since": t,
                                  "sent": t, "resolved": t}]})
        out = self.plugin.alerts(FakeCtx())
        self.assertEqual([(a["id"], a["level"], a["active"]) for a in out],
                         [("btrfs:d:corruption_errs", "error", True), ("smart:S:5", "warn", False)])


if __name__ == "__main__":
    unittest.main()
