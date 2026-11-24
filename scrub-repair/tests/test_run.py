"""scrub-repair's run, scrubs and commands (scrub_repair.py), end to end on a
fake volume in a temporary folder. Run from the scrub-repair folder:

    python3 -m unittest discover -s tests

btrfs, the kernel log and md are faked: `sh` answers the btrfs commands the
tool runs (subvolume list, rootid, inode-resolve, scrub start/resume/cancel/
status) and records them. Nothing runs as root and nothing leaves the
temporary folder.
"""

import argparse
import datetime as dt
import importlib.util
import os
import shutil
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOL)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sr = load("sr_tool", os.path.join(TOOL, "scrub_repair.py"))
P = load("core_plugins_for_sr", os.path.join(ROOT, "core", "plugins.py"))   # tests only
busy = sr.busy
from core.lib.alerts import Alerts  # noqa: E402  (scrub_repair.py put the suite home on sys.path)

TZ = dt.timezone(dt.timedelta(hours=-7))
NOON = dt.datetime(2026, 11, 23, 12, 0, tzinfo=TZ)
DATA = bytes(range(256)) * 64
READ = "BTRFS warning (device dm-0): csum failed root 258 ino {ino} off 4096 csum 0x1 expected csum 0x2 mirror 1"
RUNNING = ("Status:           running\nTime left:        2:00:00\nBytes scrubbed:   1.00TiB  (40.00%)\n"
           "Error summary:    no errors found\n")


class Volume(unittest.TestCase):
    """A volume at <tmp>/vol, mounted from subvolume /@unifi, with the share
    Photos (subvolume 258) and a USB folder source holding good copies."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.vol = os.path.join(self.dir, "vol")
        self.share = os.path.join(self.vol, ".srv", ".unifi-drive", "Photos", ".data")
        self.usb = os.path.join(self.dir, "usb")
        self.inodes = {}
        self.target = self.put("2019/a.jpg", ino=1234)
        self.copy("2019/a.jpg")
        self.t = NOON
        self.records = []
        self.cmds = []
        self.scrub = "UUID: x\n\tno stats available\n"
        self.md = {}
        self.others = []
        self.published = []
        self.nudged = []
        self.pings = []
        boot = os.path.join(self.dir, "boot_id")
        with open(boot, "w") as f:
            f.write("boot-1\n")
        self.config("")
        for name, value in (("state_file", os.path.join(self.dir, "state.json")),
                            ("verify_manifest", lambda: []), ("heal", lambda dry: []),
                            ("trigger_problems", lambda: []), ("ping", self.pings.append),
                            ("which", lambda x: "/usr/bin/" + x), ("sh", self.sh),
                            ("others_busy", lambda t: list(self.others)),
                            ("publish_busy", lambda w, t, dry=False: self.published.append(list(w))),
                            ("nudge", lambda tools, dry=False: self.nudged.extend(tools))):
            self.patch(sr.TOOL, name, value)
        self.patch(sr, "BOOT_ID", boot)
        self.patch(sr, "now", lambda: self.t)
        self.patch(sr, "read_kmsg", lambda: list(self.records))
        self.patch(sr, "btrfs_mounts", lambda text=None: [{"mount": self.vol, "fsroot": "/@unifi", "dev": "dm-0"}])
        self.patch(sr, "read_md", lambda sys_block=None: self.md)
        self.patch(sr, "unifi_source", lambda cfg: (None, None))
        self.patch(sr, "backups_finished", lambda: None)
        self.patch(Alerts, "flush", lambda alerts: None)     # keep the outbox to read

    def patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def config(self, extra, sources=None):
        path = os.path.join(self.dir, "config")
        with open(path, "w") as f:
            f.write(f"FOLDER_SOURCES={sources or self.usb}\n" + extra)
        self.cfg = sr.Config(path, core_path=os.path.join(self.dir, "better-unas.conf"))
        self.assertEqual(self.cfg.errors, [])

    def put(self, rel, data=DATA, ino=None):
        p = os.path.join(self.share, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        if ino:
            self.inodes[ino] = p
        return p

    def copy(self, rel, data=DATA):
        p = os.path.join(self.usb, "Photos", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def sh(self, *cmd, timeout=60, env=None):
        self.cmds.append(cmd)
        if cmd[:3] == ("btrfs", "subvolume", "list"):
            return 0, ("ID 257 gen 1 top level 5 path @unifi\n"
                       "ID 258 gen 2 top level 257 path @unifi/.srv/.unifi-drive/Photos/.data\n"
                       "ID 260 gen 3 top level 257 path @unifi/.srv/.unifi-drive/.snapshot/Photos/s1\n")
        if cmd[:3] == ("btrfs", "inspect-internal", "rootid"):
            return 0, "257"
        if cmd[:3] == ("btrfs", "inspect-internal", "inode-resolve"):
            p = self.inodes.get(int(cmd[3]))
            return (0, p) if p and p.startswith(cmd[4]) else (1, "ERROR: ino paths ioctl: No such file or directory")
        if cmd[:3] == ("btrfs", "scrub", "status"):
            return 0, self.scrub
        if cmd[:3] in (("btrfs", "scrub", "start"), ("btrfs", "scrub", "resume")):
            self.scrub = RUNNING
            return 0, ""
        if cmd[:3] == ("btrfs", "scrub", "cancel"):
            self.scrub = "Status:           aborted\nBytes scrubbed:   1.00TiB  (40.00%)\n"
            return 0, ""
        return 1, f"unexpected command {cmd}"

    def run_once(self, dry=False):
        with mock.patch("builtins.print"):
            return sr.cmd_run(self.cfg, argparse.Namespace(dry_run=dry, source="timer"))

    def state(self):
        return sr.load_state()

    def scrub_cmds(self):
        return [c[2] for c in self.cmds if c[:2] == ("btrfs", "scrub") and c[2] != "status"]

    def damage(self, ino=1234, seq=None):
        self.records.append((seq or len(self.records) + 1, READ.format(ino=ino)))

    def command(self, fn, *args, dry=False):
        with mock.patch("builtins.print") as out:
            rc = fn(self.cfg, argparse.Namespace(args=list(args), dry_run=dry))
        return rc, " ".join(str(c.args[0]) for c in out.call_args_list if c.args)


class Detection(Volume):
    def test_a_read_finds_the_file_and_stages_a_copy(self):
        self.damage()
        self.assertEqual(self.run_once(), 1)                  # an open damaged file is an error
        rec = self.state()["files"][self.target]
        self.assertEqual((rec["status"], rec["how"], rec["share"], rec["rel"]),
                         ("staged", "read", "Photos", "2019/a.jpg"))
        self.assertEqual(rec["copy"]["label"], f"folder {self.usb}")
        self.assertEqual(rec["staged"], os.path.join(self.share, ".better-unas-staging", sr.token(self.target)))
        alert = self.state()["alerts"][f"file:{self.target}"]
        self.assertIn("A verified copy from folder", alert["msg"])
        self.assertEqual([p for p in self.pings if p], [])    # no heartbeat URL set

    def test_the_cursor_reads_each_line_once(self):
        self.damage()
        self.run_once()
        self.assertEqual(self.state()["kmsg"], {"boot": "boot-1", "seq": 1})
        self.cmds.clear()
        self.run_once()
        self.assertFalse([c for c in self.cmds if c[:3] == ("btrfs", "inspect-internal", "inode-resolve")])

    def test_metadata_damage_is_an_error_alert_only(self):
        self.records.append((1, "BTRFS warning (device dm-0): checksum error at logical 1 on dev /dev/dm-0, "
                                "physical 1: metadata leaf (level 0) in tree 2"))
        self.run_once()
        st = self.state()
        self.assertEqual(st.get("files"), {})
        self.assertIn("btrfs metadata damage on dm-0", st["alerts"]["metadata:dm-0"]["msg"])

    def test_an_inode_that_wont_resolve(self):
        self.damage(ino=999)
        self.run_once()
        st = self.state()
        self.assertIn("couldn't be matched to a file", st["alerts"]["inode:258:999"]["msg"])
        self.assertEqual(st["alerts"]["inode:258:999"]["level"], "warning")
        self.assertEqual(self.scrub_cmds(), ["start"])         # an unknown damaged block: scrub to find the rest
        self.damage(ino=999)
        self.scrub = "Status:           finished\nError summary:    no errors found\n"
        self.run_once()
        self.assertEqual(self.scrub_cmds(), ["start"])         # the same inode again doesn't start another

    def test_a_snapshot_copy_isnt_restored(self):
        snap = os.path.join(self.vol, ".srv", ".unifi-drive", ".snapshot", "Photos", "s1", "a.jpg")
        os.makedirs(os.path.dirname(snap))
        with open(snap, "wb") as f:
            f.write(DATA)
        self.inodes[77] = snap
        self.records.append((1, READ.format(ino=77).replace("root 258", "root 260")))
        self.run_once()
        rec = self.state()["files"][snap]
        self.assertEqual(rec["status"], "snapshot")
        self.assertEqual(self.state()["alerts"][f"file:{snap}"]["level"], "warning")

    def test_a_file_deleted_since_is_dropped(self):
        self.damage()
        self.run_once()
        os.remove(self.target)
        self.run_once()
        st = self.state()
        self.assertEqual(st["files"], {})
        self.assertNotIn(f"file:{self.target}", st["alerts"])
        self.assertEqual(st["restored"][-1]["status"], "gone")
        self.assertFalse(os.path.exists(os.path.join(self.share, ".better-unas-staging")))   # tidied away

    def test_dry_run_changes_nothing(self):
        self.damage()
        self.assertEqual(self.run_once(dry=True), 1)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "state.json")))
        self.assertEqual(self.scrub_cmds(), [])
        self.assertFalse(os.path.exists(os.path.join(self.share, ".better-unas-staging")))


class Modes(Volume):
    def test_stage_then_restore_by_hand(self):
        self.damage()
        self.run_once()
        os.chmod(self.target, 0o640)
        st = self.state()
        st["files"][self.target]["fp"] = sr.restore.fingerprint(self.target)
        sr.TOOL.save_state(st, False)
        rc, out = self.command(sr.cmd_restore, self.target)
        self.assertEqual(rc, 0, out)
        self.assertIn("restored", out)
        st = self.state()
        self.assertEqual(st["files"], {})
        self.assertEqual(st["restored"][-1]["status"], "restored")
        self.assertNotIn(f"file:{self.target}", st["alerts"])
        self.assertEqual(os.stat(self.target).st_mode & 0o777, 0o640)
        self.assertTrue(any(m["msg"].startswith("Restored ") for m in st["outbox"]) or not st["outbox"])

    def test_auto_swaps_in_the_run(self):
        self.config("MODE=auto\n")
        self.damage()
        ino_before = os.stat(self.target).st_ino
        self.run_once()
        st = self.state()
        self.assertEqual(st["files"], {})
        self.assertEqual(st["restored"][-1]["label"], f"folder {self.usb}")
        self.assertNotEqual(os.stat(self.target).st_ino, ino_before)    # a new file renamed in
        self.assertEqual(st["resolved"][-1]["key"], f"file:{self.target}")

    def test_auto_doesnt_swap_a_file_that_changed(self):
        self.config("MODE=stage\n")
        self.damage()
        self.run_once()
        with open(self.target, "ab") as f:
            f.write(b"edited")
        self.config("MODE=auto\n")
        self.run_once()
        rec = self.state()["files"].get(self.target)
        # It changed, still reads fine (no fake EIO), so it's taken as fixed.
        self.assertIsNone(rec)
        self.assertEqual(self.state()["restored"][-1]["status"], "readable")

    def test_alert_only_waits_for_find(self):
        self.config("MODE=alert\n")
        self.damage()
        self.run_once()
        rec = self.state()["files"][self.target]
        self.assertEqual(rec["status"], "found")
        self.assertIn("Find a copy", self.state()["alerts"][f"file:{self.target}"]["msg"])
        rc, out = self.command(sr.cmd_find, self.target)
        self.assertEqual((rc, self.state()["files"][self.target]["status"]), (0, "staged"))

    def test_no_copy_retries_daily_and_on_new_sources(self):
        shutil.rmtree(self.usb)
        self.damage()
        self.run_once()
        rec = self.state()["files"][self.target]
        self.assertEqual(rec["status"], "no copy")
        self.assertIn("No good copy found", self.state()["alerts"][f"file:{self.target}"]["msg"])
        tried = rec["last_try"]
        self.t = NOON + dt.timedelta(hours=1)
        self.run_once()
        self.assertEqual(self.state()["files"][self.target]["last_try"], tried)      # not yet
        other = os.path.join(self.dir, "nas2")
        os.makedirs(os.path.join(other, "2019"))
        shutil.copyfile(self.target, os.path.join(other, "2019", "a.jpg"))
        self.config("", sources=f"{self.usb},{other}")
        self.run_once()
        rec = self.state()["files"][self.target]
        self.assertEqual((rec["status"], rec["copy"]["label"]), ("staged", f"folder {other}"))

    def test_forget(self):
        self.damage()
        self.run_once()
        rc, out = self.command(sr.cmd_forget, self.target)
        self.assertEqual(rc, 0)
        st = self.state()
        self.assertEqual(st["files"], {})
        self.assertEqual(st["restored"][-1]["status"], "forgotten")
        self.assertTrue(os.path.exists(self.target))


class Scrubs(Volume):
    def windows(self):
        return self.published[-1]

    def test_urgent_scrub_after_read_errors_nudges_lower_jobs(self):
        self.others = [busy.Busy(NOON - dt.timedelta(hours=1), NOON + dt.timedelta(hours=7),
                                 "smart-scheduler test on Drive 3", "running", "smart-scheduler", "routine")]
        self.damage()
        self.run_once()
        self.assertEqual([c for c in self.cmds if c[:3] == ("btrfs", "scrub", "start")],
                         [("btrfs", "scrub", "start", "-r", self.vol)])
        self.assertEqual(self.nudged, ["smart-scheduler"])
        job = self.state()["scrub"]["job"]
        self.assertEqual((job["tier"], job["reason"]), ("urgent", "checksum errors in the kernel log"))
        self.assertIn((NOON, NOON + dt.timedelta(hours=12), sr.LABEL, "running", "urgent"), self.windows())

    def test_urgent_waits_for_its_tier_and_above(self):
        self.others = [busy.Busy(NOON - dt.timedelta(hours=1), NOON + dt.timedelta(hours=7),
                                 "smart-scheduler test on Drive 3", "running", "smart-scheduler", "manual")]
        self.damage()
        self.run_once()
        self.assertEqual(self.scrub_cmds(), [])
        self.assertEqual(self.state()["scrub"]["job"]["paused"],
                         "smart-scheduler test on Drive 3 (manual) needs the disks")
        self.others = []
        self.t = NOON + dt.timedelta(minutes=15)
        self.run_once()
        self.assertEqual(self.scrub_cmds(), ["start"])

    def test_unifi_parity_check_holds_a_scrub(self):
        self.md = {"md3": {"sync_action": "check", "last_sync_action": "check", "mismatch_cnt": 0, "data": True}}
        self.damage()
        self.run_once()
        self.assertEqual(self.scrub_cmds(), [])
        self.assertEqual(self.state()["scrub"]["job"]["paused"], "UniFi's check of md3 is running")

    def test_a_parity_mismatch_starts_one_urgent_scrub(self):
        self.md = {"md3": {"sync_action": "idle", "last_sync_action": "check", "mismatch_cnt": 16, "data": True}}
        self.run_once()
        self.assertEqual(self.state()["scrub"]["job"]["reason"], "UniFi's parity check of md3 found 16 mismatches")
        self.scrub = "Status:           finished\nError summary:    csum=2\n"
        self.t = NOON + dt.timedelta(minutes=15)
        self.run_once()
        st = self.state()["scrub"]
        self.assertIsNone(st["job"])
        self.assertEqual((st["history"][-1]["errors"], st["last_done"]), (2, self.t.isoformat()))
        self.t += dt.timedelta(minutes=15)
        self.run_once()
        self.assertEqual(self.scrub_cmds(), ["start"])          # the same count doesn't start another

    def test_scheduled_scrub_runs_in_its_window_and_resumes(self):
        self.config("SCRUB_SCHEDULE=yes\n")
        self.run_once()
        self.assertEqual(self.scrub_cmds(), [])                 # noon: outside 01:00-07:00
        nights = [w for w in self.windows() if w[3] == "planned"]
        self.assertEqual(nights[0][:2], (dt.datetime(2026, 11, 24, 1, 0, tzinfo=TZ),
                                         dt.datetime(2026, 11, 24, 7, 0, tzinfo=TZ)))
        self.assertTrue(all(w[4] == "scheduled" for w in nights))
        self.t = dt.datetime(2026, 11, 24, 1, 5, tzinfo=TZ)
        self.run_once()
        self.assertEqual(self.scrub_cmds(), ["start"])
        self.assertEqual(self.windows()[0][3:], ("running", "scheduled"))
        self.t = dt.datetime(2026, 11, 24, 7, 5, tzinfo=TZ)
        self.run_once()
        self.assertEqual(self.scrub_cmds(), ["start", "cancel"])
        self.assertEqual(self.state()["scrub"]["job"]["paused"], "its night window ended")
        self.t = dt.datetime(2026, 11, 25, 1, 5, tzinfo=TZ)
        self.run_once()
        self.assertEqual(self.scrub_cmds(), ["start", "cancel", "resume"])
        self.scrub = "Status:           finished\nError summary:    no errors found\n"
        self.t = dt.datetime(2026, 11, 25, 3, 0, tzinfo=TZ)
        self.run_once()
        st = self.state()["scrub"]
        self.assertEqual((st["job"], st["history"][-1]["tier"]), (None, "scheduled"))
        self.assertFalse(sr.detect.scheduled_due(self.t, "quarterly", 1, sr.parse_time(st["last_done"])))

    def test_a_running_scrub_gives_way_to_a_higher_tier(self):
        self.config("SCRUB_SCHEDULE=yes\n")
        self.t = dt.datetime(2026, 11, 24, 1, 5, tzinfo=TZ)
        self.run_once()
        self.others = [busy.Busy(self.t + dt.timedelta(minutes=10), self.t + dt.timedelta(hours=9),
                                 "smart-scheduler test on Drive 6", "planned", "smart-scheduler", "urgent")]
        self.t += dt.timedelta(minutes=15)
        self.run_once()
        self.assertEqual(self.scrub_cmds(), ["start", "cancel"])
        info = [m["msg"] for m in self.state()["outbox"] if m["level"] == "info"]
        self.assertTrue(any("make way for smart-scheduler test on Drive 6 (urgent)" in m for m in info), info)

    def test_manual_scrub_pause_and_resume(self):
        rc, out = self.command(sr.cmd_scrub, "start")
        self.assertEqual(rc, 0)
        self.assertEqual(self.scrub_cmds(), ["start"])
        self.assertEqual(self.state()["scrub"]["job"]["tier"], "manual")
        self.command(sr.cmd_scrub, "pause")
        self.run_once()
        self.assertEqual(self.scrub_cmds(), ["start", "cancel"])      # held: the run doesn't resume it
        self.command(sr.cmd_scrub, "resume")
        self.assertEqual(self.scrub_cmds(), ["start", "cancel", "resume"])
        rc, out = self.command(sr.cmd_scrub, "status")
        self.assertIn("manual scrub running", out)

    def test_turned_off(self):
        removed = []
        self.cfg.typed["ENABLED"] = False
        with mock.patch.object(sr.TOOL, "remove_triggers", lambda dry: removed.append(1) or []):
            self.damage()
            self.assertEqual(self.run_once(), 0)
        self.assertEqual(removed, [1])
        self.assertEqual(self.scrub_cmds(), [])


class Readers(unittest.TestCase):
    def test_mounts(self):
        text = ("29 1 0:26 /@unifi /volume/VOLUME-ID rw,noatime shared:1 - btrfs /dev/mapper/vg-lv rw,space_cache\n"
                "30 1 0:26 /@unifi/.srv/.unifi-drive/Photos/.data /var/x rw - btrfs /dev/mapper/vg-lv rw\n"
                "31 1 8:1 / /boot rw - ext4 /dev/sda1 rw\n")
        with mock.patch.object(sr.os.path, "realpath", lambda p: "/dev/dm-0"):
            self.assertEqual(sr.btrfs_mounts(text), [{"mount": "/volume/VOLUME-ID", "fsroot": "/@unifi",
                                                     "dev": "dm-0"}])


class Settings(unittest.TestCase):
    def test_schema_and_example(self):
        schema = P.load_schema(os.path.join(TOOL, "settings.json"))
        self.assertEqual(P.check_example(schema, os.path.join(TOOL, "config.example")), [])
        self.assertFalse(set(P.field_map(schema)) & set(P.SHARED_KEYS))

    def test_defaults(self):
        cfg = sr.Config(os.path.join(tempfile.gettempdir(), "no-such-sr-config"),
                        core_path=os.path.join(tempfile.gettempdir(), "no-such-core-config"))
        self.assertEqual((cfg["MODE"], cfg["USE_UNIFI_BACKUPS"], cfg["SCRUB_SCHEDULE"], cfg["URGENT_SCRUB"]),
                         ("stage", True, False, True))
        self.assertEqual(cfg["SCRUB_WINDOW"], (dt.time(1), dt.time(7)))
        self.assertEqual(sr.folder_sources(cfg), [])
        self.assertEqual(cfg.errors, [])

    def test_folder_sources(self):
        f = P.field_map(P.load_schema(os.path.join(TOOL, "settings.json")))["FOLDER_SOURCES"]
        self.assertEqual(P.parse_value(f, "/mnt/usb, /volume/x/backup"), "/mnt/usb, /volume/x/backup")
        with self.assertRaises(P.FieldError):
            P.parse_value(f, "relative/path")


class UnifiSource(unittest.TestCase):
    """unifi_source reads only task ids and the owner id, read-only."""

    def test_reads_ids_only(self):
        queries = []

        def psql(sh, db, query):
            queries.append((db, query))
            if db == "unifi-drive":
                return [["aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"]], None
            return [["0b2c8f4e-1111-2222-3333-444455556666"]], None
        cfg = {"USE_UNIFI_BACKUPS": True}
        with mock.patch.object(sr.drive, "psql", psql):
            u, why = sr.unifi_source(cfg)
        self.assertIsNone(why)
        self.assertEqual(u.tasks, [("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "UniFi Cloud backup")])
        self.assertIn("SELECT id::text FROM remote_backup_tasks", queries[0][1])
        for _, q in queries:
            for word in ("access_key", "secret", "password", "token", "credential"):
                self.assertNotIn(word, q.lower())


if __name__ == "__main__":
    unittest.main()
