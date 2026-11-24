"""scrub-repair's copies (restore.py): the block check, folder sources,
UniFi Cloud backup versions, staging and the swap. Run from the
scrub-repair folder:

    python3 -m unittest discover -s tests

Files live in a temporary folder. A damaged block is faked by making
os.pread fail for reads that touch it, as the kernel does (EIO).
"""

import datetime as dt
import errno
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                    # the tool's own modules
sys.path.insert(1, os.path.dirname(os.path.dirname(HERE)))   # the suite home, for core.lib
import restore  # noqa: E402
from core.lib import util  # noqa: E402

DATA = bytes(range(256)) * 1024          # 256 KiB


def bad_blocks(path, ranges):
    """os.pread that fails with EIO on path for reads touching [a, b)."""
    real = os.pread
    ino = os.stat(path).st_ino

    def pread(fd, n, off):
        if os.fstat(fd).st_ino == ino and any(off < b and off + n > a for a, b in ranges):
            raise OSError(errno.EIO, "Input/output error")
        return real(fd, n, off)
    return mock.patch.object(restore.os, "pread", pread)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def file(self, name, data=DATA, mtime=None):
        p = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        if mtime:
            os.utime(p, (mtime, mtime))
        return p


class Compare(Base):
    def test_identical(self):
        r = restore.compare(self.file("a"), self.file("b"))
        self.assertEqual((r["ok"], r["compared"], r["unreadable"]), (True, len(DATA), 0))

    def test_unreadable_blocks_are_skipped(self):
        damaged = self.file("a")
        with bad_blocks(damaged, [(70000, 70001)]):
            r = restore.compare(damaged, self.file("b"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["unreadable"], 4096)             # only the 4 KiB piece holding the bad byte
        self.assertEqual(r["compared"], len(DATA) - 4096)

    def test_a_different_readable_block_fails(self):
        other = bytearray(DATA)
        other[100000] ^= 0xFF
        r = restore.compare(self.file("a"), self.file("b", bytes(other)))
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "it differs from the damaged file at byte 100000")

    def test_size_and_copy_errors(self):
        self.assertIn("a different size", restore.compare(self.file("a"), self.file("b", DATA[:-1]))["reason"])
        copy = self.file("b")
        with bad_blocks(copy, [(0, 1)]):
            r = restore.compare(self.file("a"), copy)
        self.assertEqual(r["reason"], "the copy has a read error at byte 0 (Input/output error)")


class Folders(Base):
    def test_both_layouts_newest_first(self):
        old = self.file("usb/Photos/2019/a.jpg", mtime=1_700_000_000)
        new = self.file("mirror/2019/a.jpg", mtime=1_750_000_000)
        cands = restore.folder_candidates([os.path.join(self.dir, "usb"), os.path.join(self.dir, "mirror"),
                                           os.path.join(self.dir, "missing")], "Photos", "2019/a.jpg")
        self.assertEqual([c.label for c in cands], [f"folder {self.dir}/mirror", f"folder {self.dir}/usb"])
        dest = os.path.join(self.dir, "dest")
        self.assertIsNone(cands[1].fetch(dest))
        with open(dest, "rb") as f, open(old, "rb") as g:
            self.assertEqual(f.read(), g.read())
        del new


class FakeDrive:
    """UniFi Drive's backup routes, as probed on hardware (Drive 4.4)."""

    def __init__(self, files):
        self.files = files            # {(version, drive id, item id): (meta, bytes)}
        self.calls = []

    def api(self, method, path):
        self.calls.append(path)
        base = "/remote-backup/tasks/T1/ucs/versions"
        if path == base:
            return 200, {"data": [
                {"taskId": "T1", "version": "v1", "createdAt": "2026-10-19T13:48:11Z", "status": "successful"},
                {"taskId": "T1", "version": "v3", "createdAt": "2026-11-19T13:48:11Z", "status": "successful"},
                {"taskId": "T1", "version": "v2", "createdAt": "2026-11-12T13:48:11Z", "status": "failed"}]}
        if path.endswith("/drives"):
            return 200, {"data": [{"driveId": "D-" + path.split("/")[-2], "driveName": "Photos", "restorable": True},
                                  {"driveId": "X", "driveName": "Media", "restorable": True}]}
        parts = path.split("/")
        key = (parts[6], parts[8], parts[10])
        if key in self.files:
            return 200, {"data": self.files[key][0]}
        return 404, None

    def download(self, path, dest):
        parts = path.split("/")
        meta, data = self.files[(parts[6], parts[8], parts[10])]
        with open(dest, "wb") as f:
            f.write(data)
        return 200


class Unifi(Base):
    def test_item_ids(self):
        self.assertEqual(restore.item_id("11111111-2222-4333-8444-555555555555", "Documents"),
                         "MTExMTExMTEtMjIyMi00MzMzLTg0NDQtNTU1NTU1NTU1NTU1Oi9Eb2N1bWVudHM")

    def test_newest_working_version_first(self):
        meta = {"name": "a.jpg", "isDir": False, "size": len(DATA), "modifyTime": "2026-01-02T03:04:05Z"}
        drive = FakeDrive({("v3", "D-v3", restore.item_id("D-v3", "2019/a.jpg")): (meta, DATA),
                           ("v1", "D-v1", restore.item_id("D-v1", "2019/a.jpg")): (meta, DATA)})
        u = restore.UnifiCloud(drive.api, drive.download, [("T1", "UniFi Cloud backup")], util.parse_time)
        cands = list(u.candidates("Photos", "2019/a.jpg"))
        self.assertEqual([c.label for c in cands], ["UniFi Cloud backup, version of 19 Nov 2026",
                                                    "UniFi Cloud backup, version of 19 Oct 2026"])
        self.assertEqual(cands[0].key, cands[1].key)        # the same unchanged file: checked once
        self.assertEqual(cands[0].mtime, dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc).timestamp())
        dest = os.path.join(self.dir, "dest")
        self.assertIsNone(cands[0].fetch(dest))
        self.assertEqual(os.path.getsize(dest), len(DATA))

    def test_unknown_share_and_missing_file(self):
        drive = FakeDrive({})
        u = restore.UnifiCloud(drive.api, drive.download, [("T1", "UniFi Cloud backup")], util.parse_time)
        self.assertEqual(list(u.candidates("Nope", "a")), [])
        self.assertEqual(list(u.candidates("photos", "a")), [])       # matched case-insensitively, then 404


class FindAndSwap(Base):
    def cand(self, data, label, key=None):
        src = self.file(f"src-{label}", data)
        return restore.Candidate("folder", label, len(data), 1_700_000_000.0,
                                 lambda dest: shutil.copyfile(src, dest) and None, key=key)

    def test_first_passing_copy_is_staged(self):
        damaged = self.file("share/a.jpg")
        bad = bytearray(DATA)
        bad[5] ^= 1
        dest = os.path.join(self.dir, "stage")
        with bad_blocks(damaged, [(200000, 200001)]):
            r = restore.find_copy(damaged, [self.cand(DATA[:10], "short"), self.cand(bytes(bad), "edited"),
                                            self.cand(DATA, "good")], dest)
        self.assertEqual(r["status"], "staged")
        self.assertEqual(r["copy"]["label"], "good")
        self.assertEqual(r["looked"], ["short: a different size", "edited: it differs from the damaged file at byte 5"])
        self.assertTrue(os.path.isfile(dest))

    def test_not_enough_room_to_stage(self):
        damaged = self.file("a.jpg")
        r = restore.find_copy(damaged, [self.cand(DATA, "big")], os.path.join(self.dir, "s"),
                              free=lambda folder: restore.ROOM + 10)
        self.assertEqual(r["looked"], [f"big: not enough free space to stage it ({len(DATA):,} bytes)"])

    def test_no_copy(self):
        damaged = self.file("a.jpg")
        bad = bytearray(DATA)
        bad[5] ^= 1
        tried = set()
        r = restore.find_copy(damaged, [self.cand(bytes(bad), "edited", key="k")], os.path.join(self.dir, "s"),
                              tried=tried)
        self.assertEqual(r["status"], "no copy")
        self.assertEqual(tried, {"k"})
        self.assertFalse(os.path.exists(os.path.join(self.dir, "s")))
        r = restore.find_copy(damaged, [self.cand(bytes(bad), "again", key="k")], os.path.join(self.dir, "s"),
                              tried=tried)
        self.assertEqual(r["looked"], [])                   # the same bytes aren't fetched twice

    def test_swap_keeps_mode_and_takes_the_copy_time(self):
        target = self.file("share/a.jpg")
        os.chmod(target, 0o640)
        expected = restore.fingerprint(target)
        staged = self.file("stage", DATA)
        self.assertIsNone(restore.swap(target, staged, expected, 1_600_000_000))
        st = os.stat(target)
        self.assertEqual((st.st_mode & 0o777, int(st.st_mtime)), (0o640, 1_600_000_000))
        self.assertFalse(os.path.exists(staged))

    def test_swap_refuses_a_changed_file(self):
        target = self.file("a.jpg")
        expected = restore.fingerprint(target)
        with open(target, "ab") as f:
            f.write(b"more")
        staged = self.file("stage")
        self.assertEqual(restore.swap(target, staged, expected), "the file changed since it was found")
        self.assertTrue(os.path.exists(staged))
        os.link(target, os.path.join(self.dir, "second"))
        self.assertEqual(restore.swap(target, staged, restore.fingerprint(target)), "it has 2 hard links")

    def test_staging_on_the_subvolume(self):
        target = self.file("share/a.jpg")
        self.assertEqual(restore.stage_path(self.dir, target, "tok"),
                         os.path.join(self.dir, restore.STAGING, "tok"))
        with mock.patch.object(restore.os, "makedirs", side_effect=OSError(errno.EROFS, "read-only")):
            self.assertEqual(restore.stage_path(self.dir, target, "tok"),
                             os.path.join(self.dir, "share", f".{restore.STAGING}-tok"))


if __name__ == "__main__":
    unittest.main()
