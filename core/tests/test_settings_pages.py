"""Settings › better-unas and the tools' pages (docs/tui-style.md §6):
updates, install / uninstall and remove, rendered headless at 80x24.

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
from fakerelease import FakeGitHub, tool_files   # noqa: E402
from test_tui import SnapshotMixin               # noqa: E402

CLOCK = dt.datetime(2026, 11, 22, 1, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7)))
SETTINGS = "3"      # Home, Alerts, Settings: the fake tools have no screens


class SettingsPagesTests(SnapshotMixin, unittest.TestCase):
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
                            link=self.link, commit_url=self.gh.commit_url,
                            branches_url=self.gh.branches_url)
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

    def core_page(self, app):
        return app.press(SETTINGS, "enter")

    def test_settings_list(self):
        app = self.make_app()
        text = app.press(SETTINGS)
        self.assertSnapshot("settings_pages_list", text)
        self.assertIn("› better-unas          v0.2.0  ▲ v0.3.0 available", text)
        self.assertIn("alpha                ✓ installed", text)
        self.assertIn("beta                 uninstalled", text)
        self.assertIn("gamma                not installed", text)
        self.assertIn("Enter open better-unas", text.split("\n")[22])
        self.assertNotIn("Tools │", text.split("\n")[0])

    def test_update_available(self):
        app = self.make_app()
        text = self.core_page(app)
        self.assertSnapshot("settings_core_update_available", text)
        self.assertIn("Version     v0.2.0  release", text)
        self.assertIn("Update      ▲ v0.3.0 available", text)
        self.assertIn("u update", text.split("\n")[22])

    def test_version_badge(self):
        app = self.make_app()
        self.assertTrue(self.term.text().split("\n")[0].endswith("▲ v0.3.0"))
        self.core_page(app)
        app.press("u", "y", "n")
        self.assertTrue(self.term.text().split("\n")[0].endswith("v0.3.0"))
        self.assertNotIn("▲", self.term.text().split("\n")[0])

    def test_not_checked_and_up_to_date(self):
        os.remove(os.path.join(self.home, U.CACHE))
        app = self.make_app()
        text = self.core_page(app)
        self.assertIn("Not checked yet. Press c to check.", text)
        self.assertNotIn("u update", text)
        text = app.press("u")
        self.assertIn("No newer version known. Press c to check.", text)
        # c checks now (the fake GitHub), even though nothing is cached.
        text = app.press("c")
        self.assertIn("Update: v0.3.0 available. Press u to update.", text)
        self.gh.publish("v0.2.0")      # pretend latest is what's installed
        text = app.press("c")
        self.assertIn("Up to date.", text)

    def test_update_with_confirm(self):
        app = self.make_app()
        self.core_page(app)
        app.press("u", "n")
        self.assertSnapshot("settings_update_confirm", self.frame_with("Update better-unas v0.2.0 to v0.3.0?"))
        self.assertEqual(U.suite_version(self.home), "v0.2.0")
        text = app.press("u", "y", "n")       # update, then not restarting now
        self.frame_with("Restart better-unas now?")
        self.assertFalse(app.restart)
        self.assertIn("Updated from v0.2.0 to v0.3.0.", text)
        self.assertEqual(U.suite_version(self.home), "v0.3.0")
        self.assertEqual(os.readlink(self.link), os.path.join(self.home, "better-unas"))
        self.assertTrue(os.path.exists(os.path.join(self.home, "beta", I.UNINSTALLED)))
        text = app.press("esc")
        self.assertIn("Version     v0.3.0", text)
        self.assertIn("Up to date.", text)

    def test_update_and_restart(self):
        app = self.make_app()
        self.core_page(app)
        app.press("u", "y", "y")
        self.assertIn("Updated to v0.3.0.", self.frame_with("Restart better-unas now?"))
        self.assertTrue(app.restart)
        self.assertFalse(app.running)
        self.assertEqual(U.suite_version(self.home), "v0.3.0")

    def test_no_restart_offer_when_an_installer_failed(self):
        self.gh.publish("v0.3.0", tools={"alpha": tool_files("alpha", "Alpha", failing=True)})
        app = self.make_app()
        self.core_page(app)
        text = app.press("u", "y")
        self.assertFalse([f for f in self.frames if "Restart better-unas now?" in f])
        self.assertIn("alpha failed", text)
        self.assertFalse(app.restart)

    def test_install_and_uninstall_a_tool(self):
        app = self.make_app()
        text = app.press(SETTINGS, "down", "down", "enter")     # beta, uninstalled
        self.assertIn("i install", text.split("\n")[22])
        text = app.press("i", "y", "n")
        self.assertIn("Turns it back on with the config it had.", self.frame_with("Install beta?"))
        self.assertIn("beta installed", text)
        self.assertEqual(I.installed_tools(self.home), ["alpha", "beta"])
        text = app.press("esc", "esc")
        self.assertIn("beta                 ! installed, restart to load", text)

        text = app.press("up", "enter")       # alpha
        self.assertIn("x uninstall", text.split("\n")[22])
        text = app.press("x", "y", "n")
        frame = self.frame_with("Uninstall alpha?")     # over the info page, which shows a temp path
        self.assertIn("Its config and state stay, and updates leave it off.", frame)
        self.assertIn("alpha uninstalled", text)
        self.assertTrue(os.path.exists(os.path.join(self.home, "alpha", I.UNINSTALLED)))
        self.assertTrue(os.path.exists(os.path.join(self.home, "alpha", "config")))

    def test_details(self):
        app = self.make_app()
        text = app.press(SETTINGS, "down", "down", "down", "enter")    # gamma
        self.assertIn("Not installed. Press i to install it.", text)
        self.assertIn("Changes:  Adds a gamma timer.", text)
        self.assertNotIn("x uninstall", text)

    def test_remove_all(self):
        app = self.make_app()
        self.core_page(app)
        text = app.press("R", "y", "n")
        self.assertSnapshot("settings_remove_confirm", self.frame_with("Remove better-unas?"))
        self.frame_with("Also delete")
        self.assertIn("better-unas is removed.", text)
        self.assertTrue(os.path.isdir(self.home))
        self.assertFalse(os.path.lexists(self.link))
        self.assertEqual(I.installed_tools(self.home), [])

    def test_remove_all_and_delete(self):
        app = self.make_app()
        self.core_page(app)
        text = app.press("R", "y", "y")
        self.assertIn("Deleted", text)
        self.assertFalse(os.path.exists(self.home))

    def test_dry_run_changes_nothing(self):
        app = self.make_app(dry_run=True)
        self.core_page(app)
        text = app.press("u")
        self.assertIn("Dry run: would update to v0.3.0.", text)
        text = app.press("R")
        self.assertIn("Dry run: would remove better-unas.", text)
        text = app.press("esc", "down", "enter", "x")
        self.assertIn("Dry run: would uninstall alpha.", text)
        self.assertEqual(U.suite_version(self.home), "v0.2.0")
        self.assertEqual(I.installed_tools(self.home), ["alpha"])

    def test_home_update_notice_opens_the_page(self):
        app = self.make_app()
        text = app.press("1")
        self.assertIn("▲ Update available: v0.3.0   Settings › better-unas", text)
        text = app.press("enter")
        self.assertIn("Settings › better-unas", text.split("\n")[1])
        self.assertIn("Update      ▲ v0.3.0 available", text)


class DevModeTests(SnapshotMixin, unittest.TestCase):
    """Settings › better-unas in developer mode, on the same installed home."""
    SHA1, SHA2 = "1" * 40, "a2b3c4d5e6" * 4
    make_app = SettingsPagesTests.make_app
    frame_with = SettingsPagesTests.frame_with

    def setUp(self):
        SettingsPagesTests.setUp(self)
        with open(os.path.join(self.home, U.CORE_CONFIG), "w") as f:
            f.write("DEV_MODE=yes\nDEV_BRANCH=main\n")
        self.gh.push("main", self.SHA1)
        self.gh.push("feature/x", self.SHA2)
        self.up.check(force=True, now=CLOCK)

    def test_shows_the_branch_and_offers_the_new_commit(self):
        app = self.make_app()
        text = app.press(SETTINGS, "enter")
        self.assertIn("Version     v0.2.0  developer mode", text)
        self.assertIn("Update      ▲ new commit 1111111", text)
        self.assertTrue(text.split("\n")[0].endswith("▲ new commit 1111111"))
        text = app.press("u", "y", "n")
        frame = self.frame_with("main @ 1111111?")
        self.assertIn("unreleased code (developer mode)", frame)
        self.assertIn("Updated from v0.2.0 to main @ 1111111.", text)
        self.assertEqual(U.installed_commit(self.home), self.SHA1)

    def test_pick_the_branch_and_check_it(self):
        app = self.make_app()
        app.press(SETTINGS, "enter", "down", "down")    # better-unas, Branch
        text = app.press("enter")
        self.assertIn("main  (current)", text)
        self.assertIn("feature/x", text)
        text = app.press("down", "enter")
        self.assertIn("* Branch                      feature/x", text)
        text = app.press("s")
        self.assertEqual(U.dev_branch(self.home), "feature/x")
        # saving the branch checks it straight away
        self.assertIn("Update      ▲ new commit a2b3c4d", text)

    def test_branch_picker_without_github_keeps_the_branch(self):
        self.up.branches_url = "file:///nonexistent/branches.json"
        app = self.make_app()
        app.press(SETTINGS, "enter", "down", "down")
        text = app.press("enter")
        self.assertIn("Couldn't list branches", text)
        self.assertNotIn("(current)", text)
        self.assertIn("Branch                      main", text)


if __name__ == "__main__":
    unittest.main()
