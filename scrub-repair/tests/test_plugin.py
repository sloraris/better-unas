"""scrub-repair's plugin: the Home card, alerts, calendar hook and the Damaged
files screen, run headless at 80x28 in the real TUI. Run from the
scrub-repair folder:

    python3 -m unittest discover -s tests

The TUI loads a private copy of the tool in a temporary suite folder, so its
state.json and lock are ours. The damaged file and its staged copy live
there too; a restore from the screen really renames the copy over it.
"""

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOL)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import plugins as P                    # noqa: E402
from core.tui import widgets as W                # noqa: E402
from core.tui.app import App                     # noqa: E402
from core.tui.terminal import HeadlessTerminal   # noqa: E402
from core.tui.theme import Theme                 # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=-7))
NOW = dt.datetime(2026, 11, 23, 12, 0, tzinfo=TZ)
DATA = b"good bytes " * 100


class Fixture(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        self.addCleanup(W.reset_editing)
        self.tool_dir = os.path.join(self.home, "scrub-repair")
        shutil.copytree(TOOL, self.tool_dir, ignore=shutil.ignore_patterns(
            "tests", "__pycache__", "config", "state.json", ".lock", "manifest.sha256", "busy.json"))
        self.share = os.path.join(self.home, "vol", ".srv", ".unifi-drive", "Photos", ".data")
        self.target = os.path.join(self.share, "2019", "a.jpg")
        os.makedirs(os.path.dirname(self.target))
        with open(self.target, "wb") as f:
            f.write(DATA)
        self.staged = os.path.join(self.share, ".better-unas-staging", "tok")
        os.makedirs(os.path.dirname(self.staged))
        with open(self.staged, "wb") as f:
            f.write(DATA)

    def fp(self):
        st = os.lstat(self.target)
        return {"ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}

    def rec(self, **kw):
        r = {"path": self.target, "mount": os.path.join(self.home, "vol"), "subvol": self.share,
             "share": "Photos", "rel": "2019/a.jpg", "how": "read", "found": "2026-11-22T22:10:00-07:00",
             "root": 258, "ino": 1234, "fp": self.fp(), "status": "staged", "looked": ["UniFi Cloud backup: a different size"],
             "copy": {"source": "folder", "label": "folder /mnt/usb", "size": len(DATA), "mtime": 1_700_000_000.0,
                      "version_time": None},
             "check": {"ok": True, "compared": len(DATA) - 4096, "unreadable": 4096, "size": len(DATA)},
             "last_try": "2026-11-22T22:15:00-07:00", "staged": self.staged}
        r.update(kw)
        return r

    def write_state(self, state):
        with open(os.path.join(self.tool_dir, "state.json"), "w") as f:
            json.dump(state, f)

    def read_state(self):
        with open(os.path.join(self.tool_dir, "state.json")) as f:
            return json.load(f)

    def state(self, files=True):
        alert = {"msg": f"Damaged file: {self.target}. A verified copy from folder /mnt/usb is staged; ...",
                 "level": "error", "sent": "2026-11-22T22:15:00-07:00", "since": "2026-11-22T22:15:00-07:00"}
        return {"last_tick": "2026-11-23T11:45:00-07:00",
                "files": {self.target: self.rec()} if files else {},
                "alerts": {f"file:{self.target}": alert} if files else {},
                "scrub": {"job": None, "last_done": "2026-10-02T05:00:00-07:00",
                          "history": [{"began": "2026-10-01T01:00:00-07:00", "done": "2026-10-02T05:00:00-07:00",
                                       "tier": "scheduled", "reason": "the scheduled scrub", "errors": 0,
                                       "seconds": 100800}]}}

    def make_app(self, config=""):
        with open(os.path.join(self.tool_dir, "config"), "w") as f:
            f.write(config)
        tool = P.load_tool(self.tool_dir)
        self.term = HeadlessTerminal(80, 28)
        app = App(self.term, [tool], {}, clock=lambda: NOW, theme=Theme(True),
                  log_path=os.path.join(self.home, "tui.log"))
        self.plugin = app.plugins["scrub-repair"]
        self.plugin.sr.trigger_problems = lambda: []
        self.plugin.sr.TOOL.sh = lambda *a, **k: (0, "")
        self.ctx = app.contexts["scrub-repair"]
        app.start()
        app.draw()
        return app


class Card(Fixture):
    def test_contract(self):
        self.write_state(self.state())
        app = self.make_app("SCRUB_SCHEDULE=yes\n")
        ctx = self.ctx
        self.assertEqual(P.check_summary(self.plugin.summary(ctx)), [])
        self.assertEqual(P.check_alerts(self.plugin.alerts(ctx)), [])
        self.assertEqual(P.check_status(self.plugin.status(ctx)), [])
        self.assertEqual(P.check_calendar(self.plugin.calendar(ctx)), [])
        del app

    def test_card_with_a_staged_file(self):
        self.write_state(self.state())
        self.make_app("SCRUB_SCHEDULE=yes\n")
        s = self.plugin.summary(self.ctx)
        self.assertEqual((s["state"], s["headline"]), ("error", "1 damaged file"))
        self.assertEqual([(i["label"], i["value"]) for i in s["items"]], [
            ("Damaged files", "1 (1 staged)"),
            ("Next scrub", "Fri 01 Jan 01:00"),
            ("Last scrub", "Fri 02 Oct, no errors"),
            ("Last check", "Mon 23 Nov 11:45")])

    def test_quiet_card(self):
        self.write_state(self.state(files=False))
        self.make_app()
        s = self.plugin.summary(self.ctx)
        self.assertEqual((s["state"], s["headline"]), ("ok", "No damaged files found"))
        self.assertEqual(s["items"][1], {"label": "Next scrub", "value": "none scheduled", "tone": "dim"})

    def test_calendar_marks_scrub_nights(self):
        self.write_state(self.state())
        self.make_app("SCRUB_SCHEDULE=yes\n")
        cal = self.plugin.calendar(self.ctx)
        marks = sorted(d for d, es in cal["days"].items() for e in es if e["place"] == "mark" and not e.get("past"))
        # October's scrub is done; January's takes 5 nights (the last took 28 hours, the window is 6).
        self.assertEqual(marks, [dt.date(2027, 1, d) for d in range(1, 6)])
        self.assertIn(dt.date(2026, 10, 1), cal["days"])                             # the last scrub, past
        found = cal["days"][dt.date(2026, 11, 22)][0]
        self.assertEqual((found["status"], found["place"]), ("failed", "list"))

    def test_validate(self):
        self.make_app()
        self.assertEqual(self.plugin.validate({"FOLDER_SOURCES": "/a, /a"}), {"FOLDER_SOURCES": "a folder is listed twice"})
        self.assertEqual(self.plugin.validate({"FOLDER_SOURCES": "/a, /b"}), {})


class Screen(Fixture):
    def test_list_and_details(self):
        self.write_state(self.state())
        app = self.make_app()
        text = app.press("3")
        self.assertIn("Photos/2019/a.jpg", text)
        self.assertIn("verified copy staged", text)
        self.assertIn("folder /mnt/usb", text)
        self.assertIn("matched; 4,096 unreadable bytes skipped", text)
        self.assertIn("Enter restore", text)

    def test_restore_from_the_screen(self):
        self.write_state(self.state())
        app = self.make_app()
        text = app.press("3", "enter", "y")
        self.assertIn("restored", text)
        self.assertIn("No damaged files.", text)
        st = self.read_state()
        self.assertEqual(st["files"], {})
        self.assertEqual(st["restored"][-1]["status"], "restored")
        self.assertFalse(os.path.exists(self.staged))

    def test_no_saying_no(self):
        self.write_state(self.state())
        app = self.make_app()
        app.press("3", "enter", "n")
        self.assertEqual(self.read_state()["files"][self.target]["status"], "staged")
        self.assertTrue(os.path.exists(self.staged))

    def test_empty(self):
        self.write_state(self.state(files=False))
        app = self.make_app()
        self.assertIn("No damaged files.", app.press("3"))


if __name__ == "__main__":
    unittest.main()
