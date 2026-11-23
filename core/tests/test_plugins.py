"""Plugin contract tests. Run from the repository root:

    python3 -m unittest discover -s core/tests

Checks the loader/validator in core/plugins.py, the example tool in
docs/examples/space-watch, and the smart-scheduler schema worked example
against smart-scheduler/config.example. Nothing touches a NAS.
"""

import contextlib
import datetime as dt
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
spec = importlib.util.spec_from_file_location("plugins", os.path.join(ROOT, "core", "plugins.py"))
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

EXAMPLE = os.path.join(ROOT, "docs", "examples", "space-watch")
SS_SCHEMA = os.path.join(ROOT, "smart-scheduler", "settings.json")
SS_EXAMPLE = os.path.join(ROOT, "smart-scheduler", "config.example")


# ------------------------------------------------------------------ test doubles

class TextCanvas:
    """Just enough of the contract's canvas to render a screen to text."""

    def __init__(self, width, height):
        self.width, self.height = width, height
        self.rows = [[" "] * width for _ in range(height)]
        self.tones = {}

    def text(self, y, x, s, tone="normal", bold=False):
        assert tone in P.TONES, tone
        if not 0 <= y < self.height:
            return
        for i, ch in enumerate(s):
            if 0 <= x + i < self.width:
                self.rows[y][x + i] = ch
                self.tones[(y, x + i)] = tone

    def dump(self):
        return "\n".join("".join(r).rstrip() for r in self.rows)


class FakeContext:
    def __init__(self, tool, answers=()):
        self.tool = tool
        self.answers = list(answers)
        self.asked, self.messages, self.locks = [], [], 0
        self.dry_run = False

    def config(self):
        return self.tool.read_config()[0]

    def config_errors(self):
        return self.tool.read_config()[1]

    def now(self):
        return dt.datetime(2026, 11, 22, 1, 0, tzinfo=dt.timezone.utc)

    @contextlib.contextmanager
    def lock(self, timeout=10):
        self.locks += 1
        yield

    def confirm(self, title, lines=(), second=None):
        self.asked.append(title)
        return self.answers.pop(0)

    def message(self, text, tone="normal"):
        self.messages.append((text, tone))

    def run_slow(self, fn, label):
        return fn()


def copy_example(tmp):
    dest = os.path.join(tmp, "space-watch")
    shutil.copytree(EXAMPLE, dest, ignore=shutil.ignore_patterns("__pycache__", "state.json", "config"))
    return dest


# ------------------------------------------------------------------ example tool

class ExampleTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = copy_example(self.tmp)

    def test_checker_passes(self):
        self.assertEqual(P.check_tool(EXAMPLE), [])

    def test_manifest_and_schema(self):
        tool = P.load_tool(self.dir)
        self.assertEqual(tool.name, "space-watch")
        self.assertEqual(tool.manifest["LOCK"], ".lock")
        self.assertEqual([f["key"] for f in P.essential_fields(tool.schema)],
                         ["WATCH_PATH", "WARN_PERCENT"])
        typed, errors = tool.read_config()   # no config file: defaults
        self.assertEqual(errors, {})
        self.assertEqual(typed["WARN_PERCENT"], 90)
        self.assertEqual(typed["QUIET_HOURS"], (dt.time(22), dt.time(7)))

    def test_config_errors_are_per_field(self):
        with open(os.path.join(self.dir, "config"), "w") as f:
            f.write("WARN_PERCENT=101\nWATCH_PATH=relative\nNOPE=1\n")
        typed, errors = P.load_tool(self.dir).read_config()
        self.assertEqual(errors["WARN_PERCENT"], "value must be at most 99")
        self.assertEqual(errors["WATCH_PATH"], "not in the expected form")
        self.assertEqual(errors["NOPE"], "unknown setting")
        self.assertEqual(typed["WARN_PERCENT"], 90)  # falls back to the default

    def test_data_api(self):
        tool = P.load_tool(self.dir)
        mod = P.load_plugin(tool.path, tool.manifest)
        ctx = FakeContext(tool)
        usage = shutil._ntuple_diskusage(100, 95, 5)
        with mock.patch("shutil.disk_usage", return_value=usage):
            s, a, st = mod.summary(ctx), mod.alerts(ctx), mod.status(ctx)
        self.assertEqual(P.check_summary(s), [])
        self.assertEqual(s["state"], "warn")
        self.assertEqual(P.check_alerts(a), [])
        self.assertEqual(a[0]["id"], "full")
        self.assertEqual(P.check_status(st), [])
        self.assertEqual(P.check_screens(mod.screens(ctx)), [])
        self.assertEqual(mod.validate(ctx.config()), {})

    def test_screen_renders_and_uses_lock_after_confirm(self):
        tool = P.load_tool(self.dir)
        mod = P.load_plugin(tool.path, tool.manifest)
        with open(os.path.join(self.dir, "state.json"), "w") as f:
            json.dump({"peak": 97.0}, f)
        ctx = FakeContext(tool, answers=[False, True])
        screen = mod.screens(ctx)[0]["make"](ctx)
        with mock.patch("shutil.disk_usage", return_value=shutil._ntuple_diskusage(100, 40, 60)):
            screen.show()
        canvas = TextCanvas(80, 6)
        screen.render(canvas)
        out = canvas.dump()
        self.assertIn("Volume holding /", out)
        self.assertIn(" 40.0%", out)
        self.assertEqual(canvas.tones[(2, 0)], "good")
        self.assertTrue(all(len(k) == 2 for k in screen.keys()))

        self.assertTrue(screen.key("x"))          # declined: nothing written
        self.assertEqual(ctx.locks, 0)
        self.assertTrue(screen.key("x"))          # confirmed: written under the lock
        self.assertEqual(ctx.locks, 1)
        with open(os.path.join(self.dir, "state.json")) as f:
            self.assertNotIn("peak", json.load(f))
        self.assertFalse(screen.key("z"))         # not consumed: core handles it

    def test_discover(self):
        os.mkdir(os.path.join(self.tmp, "broken"))
        with open(os.path.join(self.tmp, "broken", "tool.conf"), "w") as f:
            f.write("NAME=broken\n")
        os.mkdir(os.path.join(self.tmp, "not-a-tool"))
        tools, broken = P.discover(self.tmp)
        self.assertEqual([t.name for t in tools], ["space-watch"])
        self.assertIn("tool.conf: VERSION is required", broken["broken"])


# ------------------------------------------------------------------ manifest

class Manifest(unittest.TestCase):
    BASE = {"NAME": "x", "VERSION": "1", "SUMMARY": "s", "CHANGES": "c", "CLI": "x.py"}

    def problems(self, **kw):
        try:
            P.check_manifest({**self.BASE, **kw})
            return []
        except P.ContractError as e:
            return e.problems

    def test_current_smart_scheduler_conf_still_valid(self):
        values, problems = P.read_keyvalue(os.path.join(ROOT, "smart-scheduler", "tool.conf"))
        self.assertEqual(problems, [])
        m = P.check_manifest(values, os.path.join(ROOT, "smart-scheduler"))
        self.assertEqual(m["SCHEMA"], "settings.json")
        self.assertEqual(P.check_tool(os.path.join(ROOT, "smart-scheduler")), [])

    def test_rules(self):
        self.assertEqual(self.problems(), [])
        self.assertIn("tool.conf: NAME must be lowercase letters, digits and -", self.problems(NAME="Bad_Name"))
        self.assertIn("tool.conf: unknown key COLOR", self.problems(COLOR="red"))
        self.assertTrue(any("CONTRACT=1" in p for p in self.problems(PLUGIN="plugin.py")))
        self.assertIn("tool.conf: CONFIG is required when SCHEMA is set",
                      self.problems(SCHEMA="s.json", CONTRACT="1"))
        self.assertIn("tool.conf: CLI must be a path inside the tool folder", self.problems(CLI="../x.py"))
        self.assertTrue(any("SUMMARY" in p for p in self.problems(SUMMARY="x" * 61)))

    def test_keyvalue_never_expands(self):
        values, problems = P.parse_keyvalue('# c\nA=$(rm -rf /)\nB="quoted"\nA=2\nbad line\n')
        self.assertEqual(values, {"A": "$(rm -rf /)", "B": "quoted"})
        self.assertEqual(problems, ["line 4: A repeated; the first value is used",
                                    "line 5: expected KEY=VALUE"])


# ------------------------------------------------------------------ schema and fields

def field(**kw):
    f = {"key": "K", "label": "L", "default": ""}
    f.update(kw)
    return f


def schema_with(*fs):
    return {"contract": 1, "groups": [{"id": "g", "title": "G", "fields": list(fs)}]}


class Schema(unittest.TestCase):
    def problems(self, schema):
        try:
            P.check_schema(schema)
            return []
        except P.ContractError as e:
            return e.problems

    def test_structure(self):
        self.assertTrue(self.problems({"contract": 2, "groups": []}))
        self.assertIn("schema: K: unknown type 'date' (one of int, float, bool, choice, time_window, "
                      "text, url, serial_list, int_list, serial_map, checklist)",
                      self.problems(schema_with(field(type="date"))))
        self.assertIn("schema: K: 'choices' isn't allowed on int fields",
                      self.problems(schema_with(field(type="int", default="1", choices=["a"]))))
        self.assertIn("schema: group g, field 2: key K used twice",
                      self.problems(schema_with(field(type="text"), field(type="text"))))
        self.assertIn("schema: K: default '0' is invalid: value must be at least 1",
                      self.problems(schema_with(field(type="int", min=1, default="0"))))
        self.assertIn("schema: K: \"min\" is above \"max\"",
                      self.problems(schema_with(field(type="int", min=5, max=1, default="3"))))
        self.assertIn("schema: K: \"default\" is required, as the text written in the config file",
                      self.problems(schema_with(field(type="int", default=3))))
        self.assertTrue(self.problems(schema_with(field(type="text", label="x" * 29))))
        self.assertIn("schema: K: \"options\" must be a non-empty list of {\"value\", \"label\"} objects",
                      self.problems(schema_with(field(type="checklist", options=[{"value": "a,b"}]))))
        self.assertIn("schema: K: \"options\" has duplicate values",
                      self.problems(schema_with(field(type="checklist", options=[{"value": "a"}, {"value": "a"}]))))
        self.assertIn("schema: K: 'other' must be a non-empty string",
                      self.problems(schema_with(field(type="checklist", options=[{"value": "a"}], other=True))))

    def test_smart_scheduler_worked_example(self):
        schema = P.load_schema(SS_SCHEMA)
        self.assertEqual(P.check_example(schema, SS_EXAMPLE), [])
        core = P.load_schema(os.path.join(ROOT, "core", "settings.json"))
        # With the core's Notifications group, every field type but int_list
        # (covered in Values) has an example.
        self.assertEqual({f["type"] for f in P.fields(schema) + P.fields(core)}, set(P.TYPE_ATTRS) - {"int_list"})
        self.assertEqual([f["key"] for f in P.essential_fields(schema)],
                         ["INTERVAL_DAYS", "WARN_INTERVAL_DAYS", "OLD_INTERVAL_DAYS", "START_WINDOW"])
        self.assertEqual([f["key"] for f in P.essential_fields(core)], ["WEBHOOK_URL", "WEBHOOK_FORMAT"])
        self.assertFalse(set(P.field_map(schema)) & set(P.SHARED_KEYS))     # shared keys live in the core

    def test_check_example_reports_drift(self):
        schema = P.check_schema(schema_with(field(key="A", type="int", default="1"),
                                            field(key="B", type="bool", default="yes")))
        path = os.path.join(tempfile.mkdtemp(), "config.example")
        self.addCleanup(shutil.rmtree, os.path.dirname(path))
        with open(path, "w") as f:
            f.write("A=2\nB=true\nC=1\n")
        self.assertEqual(P.check_example(schema, path), [
            "config.example: A=2 but the schema default is '1'",
            "config.example: C isn't in the schema"])


class Values(unittest.TestCase):
    def ok(self, f, text, value):
        f = {"key": "K", "label": "L", "default": "", **f}
        self.assertEqual(P.parse_value(f, text), value)
        self.assertEqual(P.parse_value(f, P.format_value(f, value)), value)

    def bad(self, f, text, msg):
        f = {"key": "K", "label": "L", "default": "", **f}
        with self.assertRaises(P.FieldError) as cm:
            P.parse_value(f, text)
        self.assertEqual(str(cm.exception), msg)

    def test_int_float(self):
        self.ok({"type": "int", "min": 0}, " 42 ", 42)
        self.bad({"type": "int"}, "4.5", "want a whole number, got '4.5'")
        self.bad({"type": "int", "max": 10}, "11", "value must be at most 10")
        self.bad({"type": "int"}, "", "a value is required")
        self.ok({"type": "float", "min": 1}, "1.5", 1.5)
        self.assertEqual(P.format_value({"type": "float"}, 2.0), "2")
        self.bad({"type": "float"}, "nan", "want a number, got 'nan'")

    def test_bool_choice(self):
        for t in ("yes", "Yes", "true", "on", "1"):
            self.assertIs(P.parse_value({"type": "bool"}, t), True)
        self.assertEqual(P.format_value({"type": "bool"}, False), "no")
        self.bad({"type": "bool"}, "maybe", "want yes or no, got 'maybe'")
        self.ok({"type": "choice", "choices": ["a", "b"]}, "b", "b")
        self.bad({"type": "choice", "choices": ["a", "b"]}, "c", "want one of a, b")

    def test_checklist(self):
        opts = [{"value": "5", "label": "reallocated"}, {"value": "197", "label": "pending"}]
        attrs = {"type": "checklist", "options": opts, "other": "Other IDs", "min": 1, "max": 255}
        self.ok(attrs, "197, 5, 12", [5, 197, 12])            # options in schema order, then others
        self.ok(attrs, "", [])
        self.assertEqual(P.format_value(attrs, [5, 197, 12]), "5,197,12")
        self.bad(attrs, "5,300", "each number (300) must be at most 255")
        self.bad(attrs, "5,5", "5 listed twice")
        names = {"type": "checklist", "options": [{"value": "a"}, {"value": "b"}]}
        self.ok(names, "b,a", ["a", "b"])
        self.bad(names, "c", "want some of a, b")              # no "other": only the options

    def test_time_window(self):
        self.ok({"type": "time_window"}, "22:00-04:00", (dt.time(22), dt.time(4)))
        self.ok({"type": "time_window"}, "00:00 - 05:30", (dt.time(0), dt.time(5, 30)))
        self.bad({"type": "time_window"}, "25:00-04:00", "want HH:MM-HH:MM, e.g. 22:00-04:00")
        self.bad({"type": "time_window"}, "3:00-04:00", "want HH:MM-HH:MM, e.g. 22:00-04:00")
        self.bad({"type": "time_window"}, "04:00-04:00", "start and end must differ")

    def test_text_url(self):
        self.ok({"type": "text"}, "", "")
        self.bad({"type": "text", "required": True}, " ", "a value is required")
        self.bad({"type": "text", "pattern": "[a-z]+"}, "a b", "not in the expected form")
        self.bad({"type": "text", "max_length": 3}, "abcd", "at most 3 characters")
        self.ok({"type": "url"}, "https://ntfy.sh/topic?x=1", "https://ntfy.sh/topic?x=1")
        self.bad({"type": "url"}, "ftp://x", "URL must start with http:// or https://")
        self.bad({"type": "url"}, "ntfy.sh/topic", "want a full URL, e.g. https://example.com/path")

    def test_lists_and_map(self):
        self.ok({"type": "serial_list"}, "ZL1, WD-ABC_2", ["ZL1", "WD-ABC_2"])
        self.ok({"type": "serial_list"}, "", [])
        self.bad({"type": "serial_list"}, "A,A", "A listed twice")
        self.bad({"type": "serial_list"}, "A B", "'A B' doesn't look like a serial number")
        self.ok({"type": "int_list", "min": 1, "max": 255}, "5,187,197,198", [5, 187, 197, 198])
        self.bad({"type": "int_list", "max": 255}, "5,300", "each number (300) must be at most 255")
        self.ok({"type": "serial_map", "min": 1}, "ZL1:30, ZL2:120", {"ZL1": 30, "ZL2": 120})
        self.bad({"type": "serial_map", "unit": "days"}, "ZL1", "bad entry 'ZL1' (want SERIAL:DAYS)")
        self.bad({"type": "serial_map"}, "ZL1:1,ZL1:2", "ZL1 listed twice")
        self.bad({"type": "serial_map", "min": 1}, "ZL1:0", "ZL1's value must be at least 1")


class PluginLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, body):
        with open(os.path.join(self.tmp, "plugin.py"), "w") as f:
            f.write(body)
        return {"NAME": "t", "PLUGIN": "plugin.py"}

    def test_missing_functions(self):
        with self.assertRaises(P.ContractError) as cm:
            P.load_plugin(self.tmp, self.write("CONTRACT = 1\ndef summary(ctx): pass\n"))
        self.assertEqual(cm.exception.problems, ["plugin: alerts(ctx) is required",
                                                 "plugin: status(ctx) is required"])

    def test_import_error_is_contained(self):
        with self.assertRaises(P.ContractError) as cm:
            P.load_plugin(self.tmp, self.write("raise RuntimeError('boom')\n"))
        self.assertIn("RuntimeError: boom", cm.exception.problems[0])

    def test_return_value_checks(self):
        self.assertTrue(P.check_summary({"state": "great", "headline": "x"}))
        self.assertTrue(P.check_alerts([{"id": "a", "level": "error", "message": "m",
                                         "active": True, "since": "yesterday"}]))
        self.assertTrue(P.check_screens([{"id": "h", "tab": "Home", "make": object}]))
        self.assertTrue(P.check_screens([{"id": "cal", "tab": "Calendar", "make": object}]))
        self.assertEqual(P.check_screens([{"id": "drives", "tab": "Drives", "make": object}]), [])

    def test_calendar_checks(self):
        d = dt.date(2026, 11, 22)
        good = {"today": d, "until": None, "legend": [("planned", "planned")], "problems": ["p"],
                "days": {d: [{"status": "planned", "place": "chip", "chip": "3", "text": "Drive 3",
                              "section": "Planned", "notes": ["n"]},
                             {"status": None, "text": "a note"},
                             {"status": "scrub", "place": "mark", "text": "scrub", "past": True}]}}
        self.assertEqual(P.check_calendar(good), [])
        self.assertEqual(P.check_calendar({"error": "no API"}), [])
        self.assertTrue(P.check_calendar([]))
        self.assertTrue(P.check_calendar({"days": {"2026-11-22": []}}))
        for bad in ({"status": "great", "text": "x"}, {"status": "ok", "place": "chip", "text": "x"},
                    {"status": None, "place": "mark", "text": "x"}, {"status": "ok"},
                    {"status": "ok", "place": "corner", "text": "x"}, {"text": "x", "notes": "n"}):
            self.assertTrue(P.check_calendar({"days": {d: [bad]}}), bad)


if __name__ == "__main__":
    unittest.main()
