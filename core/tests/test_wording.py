"""The wording lint (docs/tui-style.md §8 and §11). Run from the repository root:

    python3 -m unittest discover -s core/tests

It reads every settings schema's labels and help, and the text of every
screen the TUI draws from smart-scheduler's test fixtures (Home, Calendar and
a day's details, Drives and each drive, Alerts, and each Settings page), and
flags:

- words from the "Not" column of the glossary (§8), such as "disk" for a
  drive or "the next run" for the hourly check. A word the glossary rules
  out only in one sense is matched in that sense: "reinstall" is fine for a
  tool (`i reinstall`), not for updating better-unas,
- `-` standing alone as a value (unknown, none and zero are different words),
- help text longer than the three lines the settings form shows, and group
  help longer than its one line.

A flag names the text it found and where, so the fix is to reword the
string, not to loosen the lint. Nothing here touches a NAS.
"""

import glob
import json
import os
import re
import sys
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "smart-scheduler", "tests"))

from test_calendar import CalendarFixture, with_history   # noqa: E402

# The glossary's "Not" column, as patterns on lowercase prose. Upper-case
# setting keys (MAX_STARTS_PER_NIGHT) are removed before matching.
NOT_WORDS = [
    (r"\bscan(s|ned|ning)?\b", "test"),
    (r"\b(a|the|each|every|next|last|healthy|hourly) runs?\b", "the hourly check"),
    (r"\bruns (stop|end)\b", "the hourly check"),
    (r"\bthe job\b", "the hourly check"),
    (r"\bdisks?\b", "drive"),
    (r"\bdevices?\b", "drive"),
    (r"\bsd[a-z]\b", "slot or drive"),
    (r"\bupgrad(e|es|ed|ing)\b", "update"),
    (r"\bpull\b(?! request)", "update"),
    (r"\breinstall(ing)? (better-unas|the suite)\b", "update"),
    # The verbs, for tools ("Enabled" names smart-scheduler's on/off setting).
    (r"\b(enable|disable) (it|the tool|a tool|smart-scheduler)\b", "install / uninstall"),
    (r"\bpaus(e|es|ed|ing)\b", "snooze, or stop"),
    (r"\bmut(e|es|ed|ing)\b", "snooze"),
]

HELP_WIDTH = 76          # the settings form's help area at 80 columns
HELP_LINES = 3
GROUP_HELP_WIDTH = 75


def prose(text):
    """Text as the lint reads it: setting keys and URLs taken out, lowercase."""
    text = re.sub(r"\b[A-Z][A-Z0-9_]{2,}\b", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    return text.lower()


def not_words(text):
    """[(found, use instead)] for every glossary word in text."""
    found = []
    low = prose(text)
    for pattern, use in NOT_WORDS:
        for m in re.finditer(pattern, low):
            found.append((m.group(0), use))
    return found


def lone_dashes(line):
    """True if a `-` stands alone as a value in a screen line."""
    cells = re.split(r"\s{2,}|[│┌┐└┘─]", line)
    return any(c.strip() == "-" for c in cells)


def schemas():
    paths = [os.path.join(ROOT, "core", "settings.json")]
    paths += sorted(glob.glob(os.path.join(ROOT, "*", "settings.json")))
    for p in dict.fromkeys(paths):
        with open(p) as f:
            yield os.path.relpath(p, ROOT), json.load(f)


class SchemaWordingTests(unittest.TestCase):
    def test_glossary_words(self):
        problems = []
        for where, schema in schemas():
            for g in schema["groups"]:
                for text in (g.get("title", ""), g.get("help", "")):
                    problems += [f"{where} group {g.get('id')}: {w!r} (use {u})" for w, u in not_words(text)]
                for f in g["fields"]:
                    for text in (f.get("label", ""), f.get("help", "")):
                        problems += [f"{where} {f['key']}: {w!r} (use {u})" for w, u in not_words(text)]
        self.assertEqual(problems, [])

    def test_help_fits_the_form(self):
        problems = []
        for where, schema in schemas():
            for g in schema["groups"]:
                if len(g.get("help", "")) > GROUP_HELP_WIDTH:
                    problems.append(f"{where} group {g.get('id')}: help is longer than one line")
                for f in g["fields"]:
                    lines = textwrap.wrap(f.get("help", ""), HELP_WIDTH)
                    if len(lines) > HELP_LINES:
                        problems.append(f"{where} {f['key']}: help is {len(lines)} lines, at most {HELP_LINES}")
        self.assertEqual(problems, [])

    def test_the_lint_finds_what_it_should(self):
        self.assertEqual([u for _, u in not_words("Pauses the next run on that disk.")],
                         ["the hourly check", "drive", "snooze, or stop"])
        self.assertEqual(not_words("Run now. The hourly check starts tests. MAX_STARTS_PER_NIGHT"), [])
        self.assertEqual(not_words("a pull request's branch"), [])
        self.assertEqual(not_words("Enabled  [x] yes   i reinstall"), [])
        self.assertEqual([u for _, u in not_words("Reinstall better-unas to upgrade")], ["update", "update"])
        self.assertTrue(lone_dashes("   Interval   -   "))
        self.assertFalse(lone_dashes("planned 01:00-15:16, 22:00-04:00"))


class ScreenWordingTests(CalendarFixture, unittest.TestCase):
    """Every screen the fixtures reach, read as the user sees it."""

    def screens(self):
        with_history(self.fake)
        self.state["snoozes"] = {"SERIAL6": {"until": "2026-11-25", "note": "waiting for a spare",
                                             "set": "2026-11-21T10:00:00-07:00"}}
        self.write_config("EXCLUDE_SERIALS=SERIAL7\n")
        app = self.app = self.make_app()
        out = [("Home", app.press("1")), ("Calendar", app.press("2"))]
        out.append(("Calendar day", app.press("enter")))
        app.press("esc")
        out.append(("Calendar next day", app.press("right")))
        out.append(("Drives", app.press("3")))
        for slot in range(1, 8):
            out.append((f"Drive slot {slot}", app.press("enter")))
            app.press("esc", "down")
        out.append(("Alerts", app.press("4")))
        out.append(("Settings", app.press("5")))
        settings = app.screen
        for i in range(len(getattr(settings, "entries", [])) or 6):
            out.append((f"Settings page {i + 1}", app.press("enter")))
            app.press("esc", "n", "down")      # n: leave without saving, if asked
        return out

    def test_screens(self):
        problems = []
        for where, text in self.screens():
            for line in text.splitlines()[1:-1]:        # not the tab bar or the nav bar
                problems += [f"{where}: {w!r} (use {u}) in {line.strip()!r}" for w, u in not_words(line)]
                if lone_dashes(line):
                    problems.append(f"{where}: a lone '-' in {line.strip()!r}")
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
