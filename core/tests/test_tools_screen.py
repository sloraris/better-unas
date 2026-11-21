"""The Tools tab (#14), rendered headless at 80x24.

    python3 -m unittest discover -s core/tests
    UPDATE_SNAPSHOTS=1 python3 -m unittest discover -s core/tests   (after an intended change)

The home is installed from a fake release (fakerelease.py) with real sh tool
scripts; nothing touches the network or the real NAS paths.
"""

import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from core import install as I                    # noqa: E402
from core import plugins as P                    # noqa: E402
from core import updates as U                    # noqa: E402
from core.tui import widgets as W                # noqa: E402
from core.tui.app import App                     # noqa: E402
from core.tui.terminal import HeadlessTerminal   # noqa: E402
from core.tui.theme import Theme                 # noqa: E402
from fakerelease import FakeGitHub               # noqa: E402
from test_tui import SnapshotMixin               # noqa: E402

CLOCK = dt.datetime(2026, 11, 22, 1, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7)))
TOOLS_TAB = "4"     # Home, Settings, Alerts, Tools: the fake tools have no screens


class ToolsScreenTests(SnapshotMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.addCleanup(W.reset_editing)
        self.home = os.path.join(self.tmp, "better-unas")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.link = os.path.join(self.bin, "better-unas")
        self.gh = FakeGitHub(os.path.join(self.tmp, "github"))
        self.up = U.Updater(self.home, api_url=self.gh.api_url, tarball_url=self.gh.tarball_url,
                            link=self.link)
        # Installed at v0.2.0 with alpha and beta, then beta uninstalled; gamma never.
        self.gh.publish("v0.2.0")
        work = self.up.fetch_release("v0.2.0")
        self.up.update_from(work, "v0.2.0")
        shutil.rmtree(work)
        for t in ("alpha", "beta"):
            self.assertTrue(self.up.install_tool(t)[0])
        self.assertTrue(self.up.uninstall_tool("beta")[0])
        # v0.3.0 is out and the launch check saw it.
        self.gh.publish("v0.3.0")
        self.up.check(now=CLOCK)

    def make_app(self, **kw):
        tools, broken = P.discover(self.home)
        self.term = HeadlessTerminal(80, 24)
        self.frames = []
        real_end = self.term.end_frame

        def end_frame():
            self.frames.append(self.term.text())
            real_end()

        self.term.end_frame = end_frame
        app = App(self.term, tools, broken, clock=lambda: CLOCK, theme=Theme(True),
                  log_path=os.path.join(self.tmp, "tui.log"), **kw)
        app.home, app.updater = self.home, self.up
        app.start()
        app.draw()
        return app

    def frame_with(self, text):
        """The last frame drawn that shows text (a dialog waiting for its key)."""
        found = [f for f in self.frames if text in f]
        self.assertTrue(found, f"no frame showed {text!r}")
        return found[-1]

    def test_update_available(self):
        app = self.make_app()
        text = app.press(TOOLS_TAB)
        self.assertSnapshot("tools_update_available", text)
        self.assertIn("u update", text.splitlines()[-1])

    def test_not_checked_and_up_to_date(self):
        os.remove(os.path.join(self.home, U.CACHE))
        app = self.make_app()
        text = app.press(TOOLS_TAB)
        self.assertSnapshot("tools_not_checked", text)
        self.assertNotIn("u update", text)
        text = app.press("u")
        self.assertIn("No newer release known. Press c to check.", text)
        # c checks now (the fake GitHub), even though nothing is cached.
        text = app.press("c")
        self.assertIn("Update available: v0.3.0", text)
        self.gh.publish("v0.2.0")      # pretend latest is what's installed
        text = app.press("c")
        self.assertIn("Up to date.", text)
        self.assertIn("better-unas v0.2.0", text)

    def test_update_with_confirm(self):
        app = self.make_app()
        app.press(TOOLS_TAB)
        app.press("u", "n")
        self.assertSnapshot("tools_update_confirm", self.frame_with("Update better-unas v0.2.0 to v0.3.0?"))
        self.assertEqual(U.suite_version(self.home), "v0.2.0")
        text = app.press("u", "y")
        self.assertSnapshot("tools_updated", text)
        self.assertEqual(U.suite_version(self.home), "v0.3.0")
        self.assertEqual(os.readlink(self.link), os.path.join(self.home, "better-unas"))
        self.assertTrue(os.path.exists(os.path.join(self.home, "beta", I.UNINSTALLED)))
        text = app.press("esc")
        self.assertIn("better-unas v0.3.0", text)
        self.assertIn("Up to date.", text)

    def test_install_and_uninstall_a_tool(self):
        app = self.make_app()
        app.press(TOOLS_TAB)
        text = app.press("down")             # beta, uninstalled
        self.assertIn("i install", text.splitlines()[-1])
        text = app.press("i", "y")
        self.assertIn("Turns it back on with the config it had.", self.frame_with("Install beta?"))
        self.assertIn("beta installed", text)
        self.assertEqual(I.installed_tools(self.home), ["alpha", "beta"])
        text = app.press("esc")
        self.assertIn("beta               installed", text)

        app.press("up", "up")                # alpha
        text = app.press("x", "y")
        self.assertSnapshot("tools_uninstall_confirm", self.frame_with("Uninstall alpha?"))
        self.assertIn("alpha uninstalled", text)
        self.assertTrue(os.path.exists(os.path.join(self.home, "alpha", I.UNINSTALLED)))
        self.assertTrue(os.path.exists(os.path.join(self.home, "alpha", "config")))

    def test_details(self):
        app = self.make_app()
        app.press(TOOLS_TAB)
        text = app.press("down", "down", "enter")    # gamma
        self.assertIn("Not installed. Press i to install it.", text)
        self.assertIn("Changes:  Adds a gamma timer.", text)

    def test_remove_all(self):
        app = self.make_app()
        app.press(TOOLS_TAB)
        text = app.press("R", "y", "n")
        self.assertSnapshot("tools_remove_confirm", self.frame_with("Remove better-unas?"))
        self.frame_with("Also delete")
        self.assertIn("better-unas is removed.", text)
        self.assertTrue(os.path.isdir(self.home))
        self.assertFalse(os.path.lexists(self.link))
        self.assertEqual(I.installed_tools(self.home), [])

    def test_remove_all_and_delete(self):
        app = self.make_app()
        app.press(TOOLS_TAB)
        text = app.press("R", "y", "y")
        self.assertIn("Deleted", text)
        self.assertFalse(os.path.exists(self.home))

    def test_dry_run_changes_nothing(self):
        app = self.make_app(dry_run=True)
        app.press(TOOLS_TAB)
        text = app.press("u")
        self.assertIn("Dry run: would update to v0.3.0.", text)
        text = app.press("x")
        self.assertIn("Dry run: would uninstall alpha.", text)
        text = app.press("R")
        self.assertIn("Dry run: would remove better-unas.", text)
        self.assertEqual(U.suite_version(self.home), "v0.2.0")
        self.assertEqual(I.installed_tools(self.home), ["alpha"])


if __name__ == "__main__":
    unittest.main()
