"""Settings screen tests (issue #12). Run from the repository root:

    python3 -m unittest discover -s core/tests

The space-watch example stands in for any tool; smart-scheduler's own tests
cover its calendar preview end to end. UPDATE_SNAPSHOTS=1 regenerates the
snapshots in core/tests/snapshots/.
"""

import fcntl
import importlib.util
import os
import stat
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core import configfile                                # noqa: E402
from core import plugins as P                              # noqa: E402
from core.tui import settings as S                         # noqa: E402
from core.tui import widgets as W                          # noqa: E402
from test_tui import AppFixture, SS_SCHEMA                 # noqa: E402

SS_EXAMPLE = os.path.join(ROOT, "smart-scheduler", "config.example")

CONFIG = """\
# space-watch settings, edited by hand.

# Set to "no" to stop checking.
ENABLED=yes

# --- What to watch -------------------------------------------------------------
WATCH_PATH = "/volume1"
# my own note: 90 was too late last time
WARN_PERCENT=90
LEGACY_THING=keep me

# --- Notifications ---------------------------------------------------------------
QUIET_HOURS=22:00-07:00
"""


def load_ss_settings():
    spec = importlib.util.spec_from_file_location(
        "ss_settings_for_core_tests", os.path.join(ROOT, "smart-scheduler", "settings.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Fixture(AppFixture):
    def setUp(self):
        super().setUp()
        self.tool_dir = os.path.join(self.home, "space-watch")
        self.config = os.path.join(self.tool_dir, "config")
        self.lockfile = os.path.join(self.tool_dir, ".lock")

    def write_config(self, text=CONFIG):
        with open(self.config, "w") as f:
            f.write(text)

    def read_config(self):
        with open(self.config) as f:
            return f.read()

    def open_form(self, **kw):
        """Settings tab (4), Enter on space-watch."""
        self.app = self.make_app(**kw)
        self.app.press("4", "enter")
        return self.app.screen

    def set_warn(self, text):
        """Select "Warn above" (third field) and type a new value."""
        self.app.press("home", "down", "down", "enter", *["backspace"] * 20)
        self.app.press(*list(text))
        return self.app.press("enter")

    def held_elsewhere(self):
        with open(self.lockfile, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
            return False


class SettingsScreenTests(Fixture, unittest.TestCase):
    def test_list_and_form_frames(self):
        self.write_config()
        app = self.make_app()
        self.assertSnapshot("settings_list", app.press("4"))
        text = app.press("enter")
        self.assertSnapshot("settings_form", text)
        self.assertIn("space-watch/config", text)
        self.assertIn("kept as is: LEGACY_THING", text)
        self.assertIn("/volume1", text)
        self.assertIn("s save", text.split("\n")[22])
        self.assertNotIn("p preview", text)      # space-watch has no preview hook

    def test_list_flags_a_broken_config(self):
        self.write_config("WARN_PERCENT=7\n")
        text = self.make_app().press("4")
        self.assertIn("1 problem(s) in the config file", text)

    def test_edit_validate_save_keeps_comments(self):
        self.write_config()
        self.open_form()
        text = self.set_warn("80")
        self.assertIn("* Warn above", text)
        self.assertIn("1 unsaved change(s)", text)
        real_save = configfile.save
        held = []

        def save(*a, **kw):
            held.append(self.held_elsewhere())
            return real_save(*a, **kw)

        configfile.save = save
        self.addCleanup(setattr, configfile, "save", real_save)
        text = self.app.press("s")
        self.assertSnapshot("settings_saved", text)
        self.assertEqual(held, [True])            # written under the run lock
        self.assertFalse(self.held_elsewhere())   # and released afterwards
        self.assertIn("Saved 1 setting(s) to space-watch/config.", text)
        self.assertNotIn("* Warn above", text)
        self.assertEqual(self.read_config(), CONFIG.replace("WARN_PERCENT=90", "WARN_PERCENT=80"))
        self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), 0o600)
        self.assertEqual(self.app.contexts["space-watch"].config()["WARN_PERCENT"], 80)
        self.app.press("esc")                     # saved: leaving asks nothing
        self.assertIsNone(self.app.overlay)
        self.assertEqual(len(self.app.stack()), 1)

    def test_save_creates_a_missing_config(self):
        self.open_form()
        self.set_warn("75")
        self.app.press("s")
        self.assertEqual(P.read_keyvalue(self.config)[0], {"WARN_PERCENT": "75"})

    def test_new_key_lands_under_its_group(self):
        self.write_config()
        self.open_form()
        self.app.press("end", "up", "enter", *"https://hooks.example.com/x", "enter")
        self.app.press("s")
        text = self.read_config()
        self.assertIn("# --- Notifications -------------------------------------------------"
                      "--------------\nWEBHOOK_URL=https://hooks.example.com/x\nQUIET_HOURS=", text)
        self.assertIn("# my own note: 90 was too late last time\n", text)
        self.assertIn("LEGACY_THING=keep me\n", text)

    def test_invalid_input_is_explained_and_not_saved(self):
        self.write_config()
        self.open_form()
        self.set_warn("40")
        self.app.press("home")                    # move away; s jumps back to the problem
        text = self.app.press("s")
        self.assertSnapshot("settings_invalid", text)
        self.assertIn("! value must be at least 50", text)
        self.assertIn("Not saved: Warn above: value must be at least 50.", text)
        self.assertEqual(self.app.screen.form.current.key_name, "WARN_PERCENT")
        self.assertEqual(self.read_config(), CONFIG)
        self.assertFalse(os.path.exists(self.lockfile) and self.held_elsewhere())

    def test_a_bad_value_already_in_the_file_blocks_saving(self):
        self.write_config(CONFIG.replace("WARN_PERCENT=90", "WARN_PERCENT=lots"))
        self.open_form()
        self.app.press("home", " ")               # toggle Enabled
        text = self.app.press("s")
        self.assertIn("Not saved: Warn above: want a whole number, got 'lots'.", text)
        self.assertIn("ENABLED=yes", self.read_config())

    def test_plugin_validate_blocks_saving(self):
        self.write_config()
        app = self.make_app()
        app.plugins["space-watch"].validate = lambda values: (
            {"WARN_PERCENT": "must leave room for quiet hours"} if values["WARN_PERCENT"] > 95 else {})
        app.press("4", "enter")
        self.app = app
        text = self.set_warn("97")
        self.assertIn("! must leave room for quiet hours", text)
        text = app.press("s")
        self.assertIn("Not saved: Warn above: must leave room for quiet hours.", text)
        self.assertEqual(self.read_config(), CONFIG)
        self.set_warn("95")
        self.assertIn("Saved 1 setting(s)", app.press("s"))

    def test_nothing_to_save(self):
        self.write_config()
        self.open_form()
        self.assertIn("Nothing to save", self.app.press("s"))
        self.set_warn("90")                       # same value again: not a change
        self.assertIn("Nothing to save", self.app.press("s"))

    def test_discard(self):
        self.write_config()
        self.open_form()
        self.set_warn("80")
        self.app.press("esc", "n")                # keep editing
        asked = [f for f in self.frames if "Discard your changes?" in f]
        self.assertSnapshot("settings_discard", asked[-1])
        self.assertIn("* Warn above", self.term.text())
        self.assertIn("80 %", self.term.text())
        self.app.press("esc", "y")
        self.assertEqual(len(self.app.stack()), 1)
        self.assertEqual(self.read_config(), CONFIG)
        text = self.app.press("enter")            # opened again: the file's values
        self.assertIn("90 %", text)
        self.assertNotIn("* Warn above", text)

    def test_quitting_with_edits_asks_too(self):
        self.write_config()
        self.open_form()
        self.set_warn("80")
        self.app.press("q", "n")
        self.assertTrue(self.app.running)
        self.app.press("q", "y")
        self.assertFalse(self.app.running)
        self.assertEqual(self.read_config(), CONFIG)

    def test_lock_busy(self):
        self.write_config()
        self.open_form()
        ctx = self.app.contexts["space-watch"]
        real = ctx.lock
        ctx.lock = lambda timeout=10: real(timeout=0.3)
        self.set_warn("80")
        with open(self.lockfile, "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            text = self.app.press("s")
        self.assertIn("Not saved: the scheduled run is still going", text)
        self.assertIn("* Warn above", text)       # edits kept
        self.assertEqual(self.read_config(), CONFIG)
        self.assertIsNone(self.app.spinner)
        self.assertIn("Saved 1 setting(s)", self.app.press("s"))   # run finished: works now

    def test_dry_run_writes_nothing(self):
        self.write_config()
        self.open_form(dry_run=True)
        self.set_warn("80")
        text = self.app.press("s")
        self.assertIn("Dry run: 1 setting(s) are valid; nothing was written", text)
        self.assertEqual(self.read_config(), CONFIG)
        self.assertFalse(os.path.exists(self.lockfile))   # not even the lock was taken
        self.assertIn("* Warn above", text)       # still unsaved
        self.assertIn("Not saved: Warn above", (self.set_warn("10"), self.app.press("s"))[1])

    def test_preview_hand_off(self):
        self.write_config()
        app = self.make_app()
        seen = []

        class Preview:
            title = "Preview"

            def keys(self):
                return []

            def render(self, c):
                c.text(0, 0, "previewing")

            def key(self, k):
                return False

        def preview(ctx, values):
            seen.append((ctx.tool.name, values))
            return Preview()

        app.plugins["space-watch"].preview = preview
        self.app = app
        app.press("4", "enter")
        self.assertIn("p preview", self.term.text().split("\n")[22])
        self.set_warn("80")
        text = app.press("p")
        self.assertIn("Settings › space-watch › Preview", text)
        self.assertIn("previewing", text)
        name, values = seen[0]
        self.assertEqual(name, "space-watch")
        self.assertEqual(values["WARN_PERCENT"], 80)          # the unsaved edit
        self.assertEqual(values["WATCH_PATH"], "/volume1")     # the rest from the file
        self.assertEqual(self.read_config(), CONFIG)           # previewing saves nothing
        text = app.press("esc")                                # back to the form, edits intact
        self.assertIn("* Warn above", text)
        self.assertIsNone(app.overlay)
        self.set_warn("10")
        text = app.press("p")
        self.assertIn("Fix Warn above first: value must be at least 50.", text)
        self.assertEqual(len(seen), 1)

    def test_typing_p_or_s_into_a_field_is_text(self):
        self.write_config()
        self.open_form()
        self.app.press("home", "down", "enter", *["backspace"] * 20, *"/sps", "enter")
        self.assertIn("/sps", self.term.text())
        self.assertEqual(self.read_config(), CONFIG)


class SettingsFormTests(unittest.TestCase):
    """The reusable form, as first-run setup (#15) will use it."""

    class Ctx:
        LockBusy = P.ContractError      # any exception type works for the test
        dry_run = False

        def __init__(self):
            self.messages = []

        def message(self, text, tone="normal"):
            self.messages.append((text, tone))

    def setUp(self):
        self.addCleanup(W.reset_editing)
        self.schema = P.load_schema(SS_SCHEMA)

    def test_schema_subset(self):
        sub = S.schema_subset(self.schema, essential=True)
        self.assertEqual([f["key"] for f in P.fields(sub)], [f["key"] for f in P.essential_fields(self.schema)])
        self.assertTrue(all(g["fields"] for g in sub["groups"]))
        P.check_schema(sub)
        sub = S.schema_subset(self.schema, keys=["MAX_CONCURRENT", "ENABLED"])
        self.assertEqual([f["key"] for f in P.fields(sub)], ["ENABLED", "MAX_CONCURRENT"])
        self.assertEqual(len(P.fields(self.schema)), 26)     # the original is untouched

    def test_on_save_gets_changed_texts_and_errors_map_to_fields(self):
        ctx = self.Ctx()
        calls = []

        def on_save(edits):
            calls.append(edits)
            if len(calls) == 1:
                raise configfile.ValidationError({"INTERVAL_DAYS": "rejected by the writer"})
            return "Stored."

        form = S.SettingsForm(ctx, S.schema_subset(self.schema, essential=True),
                              {"INTERVAL_DAYS": "28"}, on_save)
        form.key("enter")
        for _ in range(2):
            form.key("backspace")
        for ch in "35":
            form.key(ch)
        form.key("enter")
        self.assertEqual(form.changed(), {"INTERVAL_DAYS": "35"})
        self.assertFalse(form.save())
        self.assertEqual(calls, [{"INTERVAL_DAYS": "35"}])
        self.assertEqual(form.errors(), {"INTERVAL_DAYS": "rejected by the writer"})
        self.assertEqual(ctx.messages[-1][1], "bad")
        self.assertTrue(form.save())
        self.assertEqual(ctx.messages[-1], ("Stored.", "good"))
        self.assertEqual(form.changed(), {})
        self.assertEqual(form.values()["INTERVAL_DAYS"], 35)

    def test_validate_runs_on_every_key(self):
        ctx = self.Ctx()
        form = S.SettingsForm(ctx, self.schema, {}, lambda e: None,
                              validate=lambda v: {"WARN_INTERVAL_DAYS": "too long"}
                              if v["WARN_INTERVAL_DAYS"] > v["INTERVAL_DAYS"] else {})
        self.assertEqual(form.errors(), {})
        form.form.field("WARN_INTERVAL_DAYS").text = "400"
        form.key("down")
        self.assertEqual(form.errors(), {"WARN_INTERVAL_DAYS": "too long"})
        self.assertFalse(form.save())

    def test_validate_that_raises_is_a_message(self):
        ctx = self.Ctx()

        def boom(values):
            raise KeyError("X")

        S.SettingsForm(ctx, self.schema, {}, lambda e: None, validate=boom)
        self.assertIn("validate: KeyError", ctx.messages[0][0])


class WriterTests(unittest.TestCase):
    """core/configfile.py is a copy of smart-scheduler's writer: same output."""

    EDITS = [
        {"INTERVAL_DAYS": "35"},
        {"INTERVAL_DAYS": 35, "ENABLED": False, "START_WINDOW": "23:00 - 05:00"},
        {"WEBHOOK_URL": "https://hooks.example.com/abc", "WEBHOOK_FORMAT": "slack"},
        {"EXCLUDE_SERIALS": " AAA1, BBB2 ", "INTERVAL_OVERRIDES": "AAA1:7"},
        {"DURATION_FACTOR": "1.50", "MAX_CONCURRENT": None},
        {"NOTIFY_SUCCESS": "true"},
    ]

    def setUp(self):
        self.ss = load_ss_settings()
        self.schema = P.load_schema(SS_SCHEMA)
        with open(SS_EXAMPLE) as f:
            self.example = f.read()

    def test_same_output_as_smart_schedulers_writer(self):
        texts = [self.example, "", "# only a comment\r\nINTERVAL_DAYS = '28'\r\n",
                 "\n".join(ln for ln in self.example.splitlines() if not ln.startswith("WEBHOOK"))]
        for text in texts:
            for edits in self.EDITS:
                with self.subTest(text=text[:30], edits=edits):
                    self.assertEqual(configfile.apply_edits(text, edits, self.schema),
                                     self.ss.apply_edits(text, edits, self.schema))

    def test_same_refusals(self):
        bad = {"INTERVAL_DAYS": "0", "NOPE": "1", "START_WINDOW": "x"}
        with self.assertRaises(configfile.ValidationError) as core_err:
            configfile.apply_edits(self.example, bad, self.schema)
        with self.assertRaises(self.ss.ValidationError) as ss_err:
            self.ss.apply_edits(self.example, bad, self.schema)
        self.assertEqual(core_err.exception.errors, ss_err.exception.errors)

    def test_only_the_edited_line_changes(self):
        out = configfile.apply_edits(self.example, {"INTERVAL_DAYS": "35"}, self.schema)
        diff = [(a, b) for a, b in zip(self.example.splitlines(), out.splitlines()) if a != b]
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0][1], "INTERVAL_DAYS=35")
        self.assertEqual(len(out.splitlines()), len(self.example.splitlines()))


if __name__ == "__main__":
    unittest.main()
