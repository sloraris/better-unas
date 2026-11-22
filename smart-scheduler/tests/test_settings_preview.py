"""smart-scheduler in the core's Settings screen (#12). Run from the
smart-scheduler folder:

    python3 -m unittest discover -s tests

The whole TUI runs headless at 80x24 against a private copy of the tool, with
the Drive API faked as in test_calendar. Saving writes only the copy's config.
"""

import datetime as dt
import os
import unittest

from test_calendar import CalendarFixture

TOOL = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
with open(os.path.join(TOOL, "config.example")) as _f:
    EXAMPLE = _f.read()


class SettingsScreenTests(CalendarFixture, unittest.TestCase):
    def open_settings(self):
        self.app = self.make_app()
        n = [t.title for t in self.app.tabs].index("Settings") + 1
        return self.app.press(str(n), "enter")

    def set(self, key, text):
        """Select the field for key and type text into it."""
        form = self.app.screen.form
        form.scroll.index = [f.key_name for f in form.fields].index(key)
        self.app.press("enter", *["backspace"] * 12, *text)
        return self.app.press("enter")

    def config(self):
        with open(os.path.join(self.tool_dir, "config")) as f:
            return f.read()

    def tonight(self):
        return [e["serial"] for e in self.app.screen.data["days"][dt.date(2026, 11, 22)]["planned"]]

    def test_form_at_80x24(self):
        self.write_config(EXAMPLE)
        text = self.open_settings()
        self.assertSnapshot("settings_form", text)
        self.assertIn("smart-scheduler/config", text)
        self.assertIn("p preview", text.split("\n")[22])

    def test_preview_then_save_keeps_comments(self):
        self.write_config(EXAMPLE)
        self.open_settings()
        self.set("MAX_CONCURRENT", "2")
        self.set("MAX_STARTS_PER_NIGHT", "2")
        text = self.app.press("p")
        self.assertSnapshot("settings_preview", text)
        self.assertIn("Calendar preview (unsaved settings)", text)
        self.assertEqual(self.tonight(), ["SERIAL6", "SERIAL4"])   # planned with the edits
        self.assertEqual(self.config(), EXAMPLE)                    # nothing written
        text = self.app.press("esc")
        self.assertIn("2 unsaved change(s)", text)
        text = self.app.press("s")
        self.assertIn("Saved 2 setting(s) to smart-scheduler/config.", text)
        self.assertEqual(self.config(), EXAMPLE.replace("MAX_CONCURRENT=1", "MAX_CONCURRENT=2")
                         .replace("MAX_STARTS_PER_NIGHT=1", "MAX_STARTS_PER_NIGHT=2"))
        self.assertEqual(self.ctx.config()["MAX_STARTS_PER_NIGHT"], 2)

    def test_cross_field_rule_blocks_save_and_preview(self):
        self.write_config(EXAMPLE)
        self.open_settings()
        text = self.set("WARN_INTERVAL_DAYS", "120")
        self.assertIn("! should be at most the healthy interval (90 day", text)
        text = self.app.press("p")
        self.assertIn("Fix Drive with warning signs first: should be at most the healthy", text)
        self.assertNotIn("Calendar preview", text)
        text = self.app.press("s")
        self.assertIn("Not saved:", text)
        self.assertEqual(self.config(), EXAMPLE)
        self.set("INTERVAL_DAYS", "120")          # fixing the other field clears it
        self.assertIn("Saved 2 setting(s)", self.app.press("s"))


if __name__ == "__main__":
    unittest.main()
