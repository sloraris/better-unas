"""scrub-repair's detection rules (detect.py): kernel log records, checksum
lines, subvolume paths, scrub status and the scrub calendar. Run from the
scrub-repair folder:

    python3 -m unittest discover -s tests

The kernel lines follow the formats of the UNAS kernel (5.10); the volume
and share names are made up.
"""

import datetime as dt
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import detect  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=-7))
READ = ("BTRFS warning (device dm-0): csum failed root 258 ino 1234 off 8192 csum 0x8941f998 "
        "expected csum 0x1c3a2b4f mirror 1")
SCRUB = ("BTRFS warning (device dm-0): checksum error at logical 1048576 on dev /dev/dm-0, physical 2097152, "
         "root 258, inode 1234, offset 8192, length 4096, links 1 (path: Photos/2019/a.jpg)")
META = ("BTRFS warning (device dm-0): checksum error at logical 30408704 on dev /dev/dm-0, physical 30408704: "
        "metadata leaf (level 0) in tree 2")


class Kmsg(unittest.TestCase):
    def test_record(self):
        self.assertEqual(detect.parse_record(b"4,812,1234567,-,caller=T0;" + READ.encode() + b"\n SUBSYSTEM=x\n"),
                         (812, READ))
        self.assertIsNone(detect.parse_record("garbage"))
        self.assertIsNone(detect.parse_record("4,x,1,-;m"))

    def test_cursor_skips_what_was_read(self):
        recs = [(1, "a"), (2, "b"), (3, "c")]
        new, cur = detect.new_records(recs, None, "boot1")
        self.assertEqual((new, cur), (recs, {"boot": "boot1", "seq": 3}))
        new, cur = detect.new_records(recs + [(4, "d")], cur, "boot1")
        self.assertEqual((new, cur), ([(4, "d")], {"boot": "boot1", "seq": 4}))
        new, cur = detect.new_records([], cur, "boot1")
        self.assertEqual((new, cur), ([], {"boot": "boot1", "seq": 4}))

    def test_a_reboot_starts_over(self):
        new, cur = detect.new_records([(1, "x")], {"boot": "boot1", "seq": 900}, "boot2")
        self.assertEqual((new, cur), ([(1, "x")], {"boot": "boot2", "seq": 1}))


class Lines(unittest.TestCase):
    def test_read_scrub_and_metadata(self):
        self.assertEqual(detect.parse_line(READ), {"how": "read", "dev": "dm-0", "root": 258, "ino": 1234,
                                                   "off": 8192, "path": None})
        self.assertEqual(detect.parse_line(SCRUB)["path"], "Photos/2019/a.jpg")
        self.assertEqual(detect.parse_line(SCRUB)["how"], "scrub")
        m = detect.parse_line(META)
        self.assertEqual((m["how"], m["dev"]), ("metadata", "dm-0"))
        self.assertIn("metadata leaf", m["text"])

    def test_other_lines(self):
        for line in ("BTRFS info (device dm-0): bdev /dev/dm-0 errs: wr 0, rd 0, flush 0, corrupt 3, gen 0",
                     "EXT4-fs (sda1): mounted filesystem", "", "BTRFS info (device dm-0): scrub: started on devid 1"):
            self.assertIsNone(detect.parse_line(line), line)

    def test_one_report_per_inode_with_the_scrub_path(self):
        out = detect.damage([(1, READ), (2, READ.replace("off 8192", "off 12288")), (3, SCRUB), (4, META),
                             (5, META), (6, READ.replace("ino 1234", "ino 99"))])
        self.assertEqual([(seq, r["how"], r.get("ino")) for seq, r in out],
                         [(1, "read", 1234), (4, "metadata", None), (6, "read", 99)])
        self.assertEqual(out[0][1]["path"], "Photos/2019/a.jpg")


class Subvolumes(unittest.TestCase):
    LIST = ("ID 257 gen 900 top level 5 path @unifi\n"
            "ID 258 gen 901 top level 257 path @unifi/.srv/.unifi-drive/Photos/.data\n"
            "ID 260 gen 902 top level 257 path @unifi/.srv/.unifi-drive/.snapshot/Photos/abc\n"
            "ID 300 gen 3 top level 5 path @other\n")

    def test_paths_under_the_mounted_subvolume(self):
        self.assertEqual(detect.subvolumes(self.LIST, "/@unifi"),
                         {257: "", 258: ".srv/.unifi-drive/Photos/.data",
                          260: ".srv/.unifi-drive/.snapshot/Photos/abc"})

    def test_paths_already_relative_to_the_mount(self):
        text = "ID 258 gen 901 top level 257 path .srv/.unifi-drive/Photos\n"
        self.assertEqual(detect.subvolumes(text, "/@unifi"), {258: ".srv/.unifi-drive/Photos"})
        self.assertEqual(detect.subvolumes("ID 258 gen 1 top level 5 path <FS_TREE>/data\n"), {258: "data"})

    def test_shares(self):
        self.assertEqual(detect.share_of(".srv/.unifi-drive/Photos/.data/2019/a.jpg"), ("Photos", "2019/a.jpg"))
        self.assertEqual(detect.share_of(".srv/.unifi-drive/Photos/2019/a.jpg"), ("Photos", "2019/a.jpg"))
        self.assertEqual(detect.share_of(".srv/.unifi-drive/.snapshot/Photos/x/a.jpg"), (None, None))
        self.assertEqual(detect.share_of("elsewhere/a.jpg"), (None, None))
        self.assertTrue(detect.is_snapshot(".srv/.unifi-drive/.snapshot/Photos/x/a.jpg"))
        self.assertFalse(detect.is_snapshot(".srv/.unifi-drive/Photos/a.jpg"))


class ScrubStatus(unittest.TestCase):
    def test_never_run(self):
        self.assertEqual(detect.parse_scrub_status("UUID: x\n\tno stats available\n")["state"], "none")

    def test_running(self):
        s = detect.parse_scrub_status(
            "UUID:             x\nScrub started:    Sat Nov 22 01:00:01 2026\nStatus:           running\n"
            "Duration:         1:00:00\nTime left:        5:30:00\nETA:              Sat Nov 22 07:30:00 2026\n"
            "Total to scrub:   7.34TiB\nBytes scrubbed:   1.00TiB  (13.62%)\nRate:             291.27MiB/s\n"
            "Error summary:    csum=3\n  Corrected:      0\n  Uncorrectable:  3\n  Unverified:     0\n")
        self.assertEqual((s["state"], s["percent"], s["left"], s["errors"], s["uncorrectable"]),
                         ("running", 13.62, dt.timedelta(hours=5, minutes=30), 3, 3))

    def test_finished_and_aborted(self):
        s = detect.parse_scrub_status("Status:           finished\nDuration:         6:00:00\n"
                                      "Error summary:    no errors found\n")
        self.assertEqual((s["state"], s["percent"], s["errors"], s["duration"]),
                         ("finished", 100.0, 0, dt.timedelta(hours=6)))
        self.assertEqual(detect.parse_scrub_status("Status:           aborted\n")["state"], "aborted")


class Calendar(unittest.TestCase):
    def test_periods(self):
        self.assertEqual(detect.period_start(dt.date(2026, 11, 23), "quarterly", 1), dt.date(2026, 10, 1))
        self.assertEqual(detect.period_start(dt.date(2026, 1, 3), "quarterly", 5), dt.date(2025, 10, 5))
        self.assertEqual(detect.period_start(dt.date(2026, 2, 28), "monthly", 28), dt.date(2026, 2, 28))
        self.assertEqual(detect.next_period(dt.date(2026, 10, 1), "quarterly", 1), dt.date(2027, 1, 1))
        self.assertEqual(detect.next_period(dt.date(2026, 7, 1), "half-yearly", 1), dt.date(2027, 1, 1))
        self.assertEqual(detect.next_period(dt.date(2026, 1, 1), "yearly", 1), dt.date(2027, 1, 1))

    def test_due_and_nights(self):
        t = dt.datetime(2026, 11, 23, 14, 0, tzinfo=TZ)
        w = (dt.time(1), dt.time(7))
        self.assertTrue(detect.scheduled_due(t, "quarterly", 1, None))
        self.assertFalse(detect.scheduled_due(t, "quarterly", 1, dt.datetime(2026, 10, 2, tzinfo=TZ)))
        nights = detect.scheduled_nights(t, t + dt.timedelta(days=60), "quarterly", 1, w,
                                         dt.datetime(2026, 10, 2, tzinfo=TZ), 2)
        self.assertEqual([a.date() for a, _ in nights], [dt.date(2027, 1, 1), dt.date(2027, 1, 2)])
        # Due now: the next nights from tonight.
        nights = detect.scheduled_nights(t, t + dt.timedelta(days=3), "quarterly", 1, w, None, 2)
        self.assertEqual([(a.isoformat(), b.isoformat()) for a, b in nights],
                         [("2026-11-24T01:00:00-07:00", "2026-11-24T07:00:00-07:00"),
                          ("2026-11-25T01:00:00-07:00", "2026-11-25T07:00:00-07:00")])

    def test_windows_across_midnight(self):
        w = (dt.time(22), dt.time(4))
        t = dt.datetime(2026, 11, 24, 2, 0, tzinfo=TZ)
        self.assertEqual(detect.current_window(t, w)[0], dt.datetime(2026, 11, 23, 22, 0, tzinfo=TZ))
        self.assertIsNone(detect.current_window(t.replace(hour=12), w))


class Parity(unittest.TestCase):
    def test_new_mismatches_only(self):
        md = {"md3": {"sync_action": "idle", "last_sync_action": "check", "mismatch_cnt": 8, "data": True},
              "md0": {"sync_action": "idle", "last_sync_action": "check", "mismatch_cnt": 128, "data": False},
              "md4": {"sync_action": "check", "last_sync_action": "check", "mismatch_cnt": 8, "data": True}}
        self.assertEqual(detect.parity_mismatch(md, {}), [("md3", 8)])
        self.assertEqual(detect.parity_mismatch(md, {"md3": 8}), [])


if __name__ == "__main__":
    unittest.main()
