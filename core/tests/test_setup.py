"""First-run setup (#15): preflight, the old-install migration, the plan and
the wizard screens, all headless against temporary folders.

    python3 -m unittest discover -s core/tests

The release used is a copy of this checkout's VERSION, core/ and
smart-scheduler/ (a real schema with essential fields); tool scripts never
run: a fake `run` records what would have run. Nothing touches the NAS paths.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from core import install as I                    # noqa: E402
from core import setup as S                      # noqa: E402
from core.tui import __main__ as entry           # noqa: E402
from core.tui import setup as wizard             # noqa: E402
from core.tui import widgets as W                # noqa: E402
from core.tui.terminal import HeadlessTerminal   # noqa: E402
from core.tui.theme import Theme                 # noqa: E402
from test_tui import SnapshotMixin               # noqa: E402

TOOL = "smart-scheduler"

# The first lines of the old root install.sh, as kept at home/better-unas.
OLD_ENTRY = """#!/bin/sh
# better-unas installer. Run on the UNAS as root:
#
#   sh -c "$(curl -fsSL https://raw.githubusercontent.com/sloraris/better-unas/main/install.sh)"
set -eu
echo old
"""
OLD_TOOL_CONF = """NAME=smart-scheduler
VERSION=0.1.0
SUMMARY=Scheduled extended SMART tests, run by UniFi Drive
SETUP=
CHANGES=Adds an hourly timer that runs SMART tests at night.
CLI=smart_scheduler.py
"""
OLD_CONFIG = """# my settings
ENABLED=yes
INTERVAL_DAYS=45
START_WINDOW=01:00-04:00
"""
OLD_STATE = '{"drives": {"SERIAL-A": {"last_extended": "2026-10-01"}}}\n'


class FakeRun:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, folder, script, *args):
        name = os.path.basename(folder)
        self.calls.append((name, script) + args)
        if name in self.fail:
            return False, f"{name}: missing smartctl\n"
        return True, f"{name} installed\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.addCleanup(W.reset_editing)
        self.src = os.path.join(self.tmp, "release")
        os.makedirs(self.src)
        shutil.copy(os.path.join(ROOT, "VERSION"), self.src)
        ignore = shutil.ignore_patterns("__pycache__", "tests", "config", "state.json", ".lock")
        for d in ("core", TOOL):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(self.src, d), ignore=ignore)
        self.home = os.path.join(self.tmp, "persistent", "better-unas")
        os.makedirs(os.path.dirname(self.home))
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.link = os.path.join(self.bin, "better-unas")
        self.run = FakeRun()

    def setup(self, **kw):
        return S.Setup(self.home, self.src, link=self.link, run=self.run, **kw)

    def make_old_install(self):
        """The layout the old root install.sh leaves behind."""
        d = os.path.join(self.home, TOOL)
        shutil.copytree(os.path.join(self.src, TOOL), d)
        files = {"tool.conf": OLD_TOOL_CONF, "config": OLD_CONFIG, "state.json": OLD_STATE, ".lock": ""}
        for name, text in files.items():
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
        with open(os.path.join(self.home, "better-unas"), "w") as f:
            f.write(OLD_ENTRY)
        with open(os.path.join(self.home, "VERSION"), "w") as f:
            f.write("v0.1.0\n")
        os.symlink(os.path.join(self.home, "better-unas"), self.link)

    def read(self, *parts):
        with open(os.path.join(self.home, *parts)) as f:
            return f.read()


# --------------------------------------------------------------------- preflight

class PreflightTests(Base):
    def version_file(self, text):
        p = os.path.join(self.tmp, "version")
        with open(p, "w") as f:
            f.write(text)
        return p

    def check(self, uid=0, ident="UNAS Pro v4.1.0\n", have=("python3", "curl", "tar"), **kw):
        return S.preflight(self.home, uid=uid, version_file=self.version_file(ident),
                           which=lambda t: f"/usr/bin/{t}" if t in have else None, **kw)

    def test_all_good(self):
        self.assertEqual(self.check(), [])

    def test_not_root(self):
        problems = self.check(uid=501)
        self.assertEqual(len(problems), 1)
        self.assertIn("as root", problems[0])

    def test_not_a_unas(self):
        problems = self.check(ident="Debian GNU/Linux\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("doesn't look like a UniFi UNAS", problems[0])
        self.assertIn("--force", problems[0])
        self.assertEqual(self.check(ident="Debian\n", force=True), [])

    def test_missing_version_file(self):
        problems = S.preflight(self.home, uid=0, version_file=os.path.join(self.tmp, "nope"),
                               which=lambda t: "/x")
        self.assertIn("UNAS", problems[0])

    def test_missing_commands(self):
        problems = self.check(have=("python3",))
        self.assertEqual(problems, ["Missing curl, tar; this firmware isn't supported."])

    def test_no_persistent(self):
        problems = S.preflight(os.path.join(self.tmp, "nope", "better-unas"), uid=0,
                               version_file=self.version_file("unas"), which=lambda t: "/x")
        self.assertIn("not found", problems[0])

    def test_everything_wrong_lists_everything(self):
        self.assertEqual(len(self.check(uid=1, ident="", have=())), 3)


# --------------------------------------------------------------------- first run, old installs

class DetectTests(Base):
    def test_needs_setup(self):
        self.assertTrue(S.needs_setup(self.home))           # doesn't exist yet
        os.makedirs(self.home)
        self.assertTrue(S.needs_setup(self.home))           # empty
        with open(os.path.join(self.home, S.MARKER), "w") as f:
            f.write("v0.2.0\n")
        self.assertFalse(S.needs_setup(self.home))          # set up, no tools picked later
        self.assertFalse(S.needs_setup(ROOT))               # a checkout with core/ and tools

    def test_old_install_is_found(self):
        self.make_old_install()
        legacy = S.find_legacy(self.home, link=self.link)
        self.assertEqual(legacy["entry"], os.path.join(self.home, "better-unas"))
        self.assertEqual(legacy["link"], self.link)
        t = legacy["tools"][TOOL]
        self.assertEqual(t["version"], "0.1.0")
        self.assertEqual(sorted(t["keep"]), [".lock", "config", "state.json"])
        self.assertEqual(t["config"]["INTERVAL_DAYS"], "45")
        self.assertTrue(S.needs_setup(self.home))

    def test_new_layout_is_not_legacy(self):
        self.make_old_install()
        os.makedirs(os.path.join(self.home, "core"))
        self.assertIsNone(S.find_legacy(self.home))
        self.assertFalse(S.needs_setup(self.home))

    def test_default_source(self):
        os.makedirs(self.home)
        self.assertIsNone(S.default_source(self.home, os.path.join(self.tmp, "nothing")))
        self.assertEqual(S.default_source(self.home, self.src), self.src)
        I.keep_release(self.src, self.home)
        self.assertEqual(S.default_source(self.home, None), os.path.join(self.home, I.RELEASE_DIR))


class ApplyEditsTests(unittest.TestCase):
    def test_replace_keep_and_append(self):
        text = "# head\nA=1\n  B = 2  \n# C=commented\nA=dup\n"
        out = S.apply_edits(text, {"A": "9", "B": "8", "C": "7"})
        self.assertEqual(out, "# head\nA=9\n  B = 8  \n# C=commented\nA=dup\nC=7\n")

    def test_no_trailing_newline_and_crlf(self):
        self.assertEqual(S.apply_edits("A=1", {"B": "2"}), "A=1\nB=2\n")
        self.assertEqual(S.apply_edits("A=1\r\n", {"A": "2", "B": "3"}), "A=2\r\nB=3\r\n")


# --------------------------------------------------------------------- the plan

class PlanTests(Base):
    def test_fresh_install(self):
        s = self.setup()
        self.assertEqual([o.name for o in s.offered], [TOOL])
        self.assertEqual(s.selected, [])                     # nothing ticked on a fresh box
        s.toggle(TOOL)
        self.assertEqual(s.needs_settings(), [TOOL])
        keys = [f["key"] for f in S.P.fields(s.essential_schema(TOOL))]
        self.assertIn("INTERVAL_DAYS", keys)
        self.assertNotIn("DRIVE_USER", keys)
        self.assertEqual(s.starting_text(TOOL)["INTERVAL_DAYS"], "90")
        self.assertEqual(s.set_edits(TOOL, {"INTERVAL_DAYS": "60", "WARN_INTERVAL_DAYS": "30"}),
                         {"INTERVAL_DAYS": "60"})           # unchanged values aren't written

        text = s.review_text()
        self.assertFalse(os.path.exists(self.home))          # reviewing changes nothing
        for want in (f"better-unas {s.version}", f"{TOOL} 0.1.1 (new)",
                     "Adds an hourly timer that runs SMART tests at night.",
                     f"Copy its files to {self.home}/{TOOL}/",
                     "Create config from config.example with:", "INTERVAL_DAYS=60   (was 90)",
                     "smart-scheduler.timer", "Run its install.sh", f"{self.home}/.release/"):
            self.assertIn(want, text)

        report = s.apply()
        self.assertEqual(report["tools"], [(TOOL, True, f"{TOOL} installed\n")])
        self.assertEqual(self.run.calls, [(TOOL, "install.sh")])
        self.assertTrue(os.path.isdir(os.path.join(self.home, "core")))
        self.assertEqual(self.read("VERSION"), self.read(".release", "VERSION"))
        cfg = self.read(TOOL, "config")
        self.assertIn("\nINTERVAL_DAYS=60\n", cfg)
        self.assertIn("\nWARN_INTERVAL_DAYS=30\n", cfg)
        self.assertEqual(os.stat(os.path.join(self.home, TOOL, "config")).st_mode & 0o777, 0o600)
        self.assertFalse(S.needs_setup(self.home))
        with self.assertRaises(I.InstallError):
            s.apply()

    def test_defaults_kept_writes_no_config(self):
        s = self.setup()
        s.toggle(TOOL)
        s.set_edits(TOOL, s.starting_text(TOOL))
        self.assertIn("Create config from config.example (all defaults)", s.review_text())
        s.apply()
        # The tool's own install.sh creates it from config.example (fake run here).
        self.assertFalse(os.path.exists(os.path.join(self.home, TOOL, "config")))

    def test_bad_value_is_refused(self):
        s = self.setup()
        s.toggle(TOOL)
        with self.assertRaises(S.P.FieldError):
            s.set_edits(TOOL, {"INTERVAL_DAYS": "0"})

    def test_failed_installer_is_reported(self):
        self.run.fail.add(TOOL)
        s = self.setup()
        s.toggle(TOOL)
        report = s.apply()
        self.assertEqual(report["tools"], [(TOOL, False, f"{TOOL}: missing smartctl\n")])
        self.assertTrue(s.done)

    def test_migration_keeps_config_and_state(self):
        self.make_old_install()
        lock = os.path.join(self.home, TOOL, ".lock")
        s = self.setup()
        self.assertEqual(s.selected, [TOOL])                 # the old tool starts ticked
        self.assertEqual(s.starting_text(TOOL)["INTERVAL_DAYS"], "45")   # its value, not the default
        s.set_edits(TOOL, s.starting_text(TOOL))
        text = s.review_text()
        self.assertIn(f"{TOOL} 0.1.1 (update from 0.1.0)", text)
        self.assertIn("Keep config unchanged", text)
        self.assertIn("Keep config, state.json, .lock as they are", text)
        self.assertIn("old installer script", text)

        report = s.apply()
        self.assertTrue(report["tools"][0][1])
        self.assertEqual(self.read(TOOL, "config"), OLD_CONFIG)          # byte for byte
        self.assertEqual(self.read(TOOL, "state.json"), OLD_STATE)
        self.assertTrue(os.path.exists(lock))
        self.assertIn("VERSION=0.1.1", self.read(TOOL, "tool.conf"))
        self.assertTrue(os.path.isdir(os.path.join(self.home, "core")))
        self.assertIsNone(S.find_legacy(self.home))
        self.assertFalse(S.needs_setup(self.home))
        entry = os.path.join(self.home, "better-unas")
        if os.path.isfile(os.path.join(self.src, "better-unas")):
            # The release's own command replaced the old script; the link follows.
            self.assertFalse(S.is_legacy_entry(entry))
            self.assertTrue(report["link"])
            self.assertEqual(os.readlink(self.link), entry)
        else:
            self.assertEqual(report["legacy"], entry + ".old")
            self.assertFalse(os.path.exists(entry))
            self.assertIn("better-unas installer", self.read("better-unas.old"))

    def test_migration_with_an_edit(self):
        self.make_old_install()
        s = self.setup()
        s.set_edits(TOOL, {"INTERVAL_DAYS": "30"})
        self.assertIn("Keep config; change:", s.review_text())
        self.assertIn("INTERVAL_DAYS=30   (was 45)", s.review_text())
        s.apply()
        cfg = self.read(TOOL, "config")
        self.assertEqual(cfg, OLD_CONFIG.replace("INTERVAL_DAYS=45", "INTERVAL_DAYS=30"))
        self.assertEqual(self.read(TOOL, "state.json"), OLD_STATE)

    def test_untick_old_tool_leaves_it(self):
        self.make_old_install()
        s = self.setup()
        s.toggle(TOOL)
        self.assertIn("Left as they are", s.review_text())
        s.apply()
        self.assertEqual(self.run.calls, [])
        self.assertIn("VERSION=0.1.0", self.read(TOOL, "tool.conf"))
        self.assertEqual(self.read(TOOL, "config"), OLD_CONFIG)


# --------------------------------------------------------------------- the wizard

class WizardTests(SnapshotMixin, Base):
    def make_app(self, dry_run=False, setup=None):
        self.s = setup or self.setup()
        self.term = HeadlessTerminal(80, 24)
        self.frames = []
        real_end = self.term.end_frame

        def end_frame():
            self.frames.append(self.term.text())
            real_end()

        self.term.end_frame = end_frame
        app = wizard.SetupApp(self.term, self.s, dry_run=dry_run, theme=Theme(True))
        app.start()
        app.draw()
        return app

    def frame_with(self, text):
        found = [f for f in self.frames if text in f]
        self.assertTrue(found, f"no frame showed {text!r}")
        return found[-1]

    def test_pick_screen(self):
        app = self.make_app()
        text = app.press()
        self.assertSnapshot("setup_pick", text.replace(self.s.version, "vX.Y.Z"))
        text = app.press("enter")
        self.assertIn("Tick at least one tool with Space.", text)
        text = app.press(" ")
        self.assertIn(f"[x] {TOOL}", text)
        app.press(" ")
        self.assertEqual(self.s.selected, [])

    def test_full_install(self):
        app = self.make_app()
        text = app.press(" ", "enter")
        self.assertSnapshot("setup_essentials", text)
        # Healthy drive: 90 -> 60
        app.press("enter", "backspace", "backspace", "6", "0", "enter")
        text = app.press("n")
        self.assertIn("Review", text.splitlines()[1])
        self.assertIn("INTERVAL_DAYS=60   (was 90)", text)
        self.assertIn("Enter install", text.split("\n")[22])
        self.assertFalse(os.path.exists(self.home))
        app.press("enter", "n")                              # said no at the confirm
        self.assertIn("Install better-unas", self.frame_with("[ Yes ]"))
        self.assertFalse(os.path.exists(self.home))
        text = app.press("enter", "y")
        self.assertIn("is installed.", text)
        self.assertIn(f"{TOOL}: installed", text)
        self.assertIn("\nINTERVAL_DAYS=60\n", self.read(TOOL, "config"))
        self.assertTrue(self.s.done)
        app.press("esc")                                     # nothing to go back to
        self.assertTrue(app.running)
        app.press("enter")
        self.assertFalse(app.running)

    def test_back_keeps_choices(self):
        app = self.make_app()
        app.press(" ", "enter", "enter", "backspace", "backspace", "7", "7", "enter", "n")
        text = app.press("esc")
        self.assertIn("77 days", text)                       # back on the settings step
        text = app.press("esc")
        self.assertIn(f"[x] {TOOL}", text)                   # back on the pick step
        text = app.press("enter")
        self.assertIn("77 days", text)
        text = app.press("n")
        self.assertIn("INTERVAL_DAYS=77", text)

    def test_bad_value_blocks_next(self):
        app = self.make_app()
        app.press(" ", "enter", "enter", "backspace", "backspace", "0", "enter")
        text = app.press("n")
        self.assertIn("Healthy drive:", text)
        self.assertIn("settings", text.splitlines()[1])

    def test_cancel_changes_nothing(self):
        app = self.make_app()
        app.press(" ", "enter", "n")
        app.press("q", "n")                                  # asked; stayed
        self.assertTrue(app.running)
        self.assertIn("Nothing has been changed yet.", self.frame_with("Leave setup?"))
        app.press("q", "y")
        self.assertFalse(app.running)
        self.assertFalse(self.s.done)
        self.assertFalse(os.path.exists(self.home))
        self.assertEqual(self.run.calls, [])

    def test_dry_run_shows_review_only(self):
        app = self.make_app(dry_run=True)
        text = app.press(" ", "enter", "n")
        self.assertIn("Dry run: this is what setup would do.", text)
        self.assertIn("DRY RUN", text.splitlines()[0])
        self.assertNotIn("Enter install", text.split("\n")[22])
        text = app.press("enter")
        self.assertIn("Dry run: nothing was changed.", text)
        self.assertFalse(os.path.exists(self.home))
        self.assertEqual(self.run.calls, [])

    def test_migration_screen(self):
        self.make_old_install()
        app = self.make_app()
        text = app.press()
        self.assertIn("Found an install from the old installer: smart-scheduler 0.1.0.", text)
        self.assertIn(f"[x] {TOOL}", text)
        text = app.press("enter")
        self.assertIn("45 days", text)                       # the old config's value
        app.press("n", "enter", "y")
        self.assertEqual(self.read(TOOL, "config"), OLD_CONFIG)
        self.assertEqual(self.read(TOOL, "state.json"), OLD_STATE)


# --------------------------------------------------------------------- the launcher

class LauncherTests(Base):
    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stderr", err):
            code = entry.main(["--home", self.home, *args], out=out)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_headless(self):
        code, out, _ = self.main("--source", self.src, "--dry-run", "--headless", "80x24",
                                 "--keys", "space enter n")
        self.assertEqual(code, 0)
        self.assertIn(f"{TOOL} 0.1.1 (new)", out)
        self.assertFalse(os.path.exists(self.home))

    def test_preflight_failure_stops(self):
        with mock.patch.object(S, "preflight", return_value=["Run this as root (log in as root@<your UNAS>)."]):
            with mock.patch.object(entry, "run_curses") as curses:
                code, _, err = self.main("--source", self.src)
        self.assertEqual(code, 1)
        self.assertIn("better-unas: Run this as root", err)
        curses.assert_not_called()
        self.assertFalse(os.path.exists(self.home))

    def test_dry_run_shows_preflight_problems_as_warnings(self):
        with mock.patch.object(S, "preflight", return_value=["Missing tar; this firmware isn't supported."]):
            code, out, _ = self.main("--source", self.src, "--dry-run", "--headless", "80x24")
        self.assertEqual(code, 0)
        self.assertIn("Not checked in a dry run: Missing tar", out)

    def test_no_source(self):
        os.makedirs(self.home)
        with mock.patch.object(entry, "ROOT", os.path.join(self.tmp, "nothing")):
            code, _, err = self.main()
        self.assertEqual(code, 1)
        self.assertIn("no release to set up from", err)

    def test_set_up_home_skips_the_wizard(self):
        os.makedirs(self.home)
        with open(os.path.join(self.home, S.MARKER), "w") as f:
            f.write("v0.2.0\n")
        with mock.patch.object(entry, "first_run") as fr:
            code, out, _ = self.main("--headless", "80x24")
        fr.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("Home", out.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
