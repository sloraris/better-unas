"""Settings schema, validation and config writer tests. Run from the smart-scheduler folder:

    python3 -m unittest discover -s tests

Checks settings.json against the plugin contract (core/plugins.py), that
settings.py's value rules match the core's exactly, and that the writer keeps
everything it didn't edit. Nothing touches a NAS.
"""

import copy
import datetime as dt
import importlib.util
import json
import os
import shutil
import stat
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOL)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ss = load("ss", os.path.join(TOOL, "smart_scheduler.py"))
S = load("ss_settings", os.path.join(TOOL, "settings.py"))
P = load("core_plugins", os.path.join(ROOT, "core", "plugins.py"))   # tests only: the tool never imports it

SCHEMA = S.load_schema(os.path.join(TOOL, "settings.json"))
with open(os.path.join(TOOL, "config.example"), encoding="utf-8") as fh:
    EXAMPLE = fh.read()


def changed_lines(a, b):
    """Lines of a that aren't in b at the same position (files of equal length)."""
    a, b = a.splitlines(), b.splitlines()
    assert len(a) == len(b), (len(a), len(b))
    return [(x, y) for x, y in zip(a, b) if x != y]


class Schema(unittest.TestCase):
    def test_passes_the_contract_check(self):
        self.assertEqual(P.check_tool(TOOL), [])
        tool = P.load_tool(TOOL)
        self.assertEqual(tool.manifest["SCHEMA"], "settings.json")
        self.assertEqual(tool.manifest["CONFIG"], "config")

    def test_covers_config_example_in_order(self):
        raw, problems = S.parse_text(EXAMPLE)
        self.assertEqual(problems, [])
        self.assertEqual(list(raw), [f["key"] for f in S.fields(SCHEMA)])
        self.assertEqual(raw, S.defaults(SCHEMA))

    def test_groups_follow_config_example_headings(self):
        headings = [S.HEADING_RE.match(l).group(1) for l in EXAMPLE.splitlines() if S.HEADING_RE.match(l)]
        titles = [g["title"] for g in SCHEMA["groups"]]
        self.assertEqual(titles[0], "General")          # the unheaded top of the file
        self.assertEqual(titles[1:], headings)

    def test_essentials(self):
        self.assertEqual([f["key"] for f in P.essential_fields(SCHEMA)],
                         ["INTERVAL_DAYS", "WARN_INTERVAL_DAYS", "OLD_INTERVAL_DAYS",
                          "START_WINDOW", "WEBHOOK_URL", "WEBHOOK_FORMAT"])

    def test_defaults_come_from_the_schema(self):
        self.assertEqual(ss.DEFAULTS, S.defaults(SCHEMA))

    def test_broken_schema_is_reported(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        with self.assertRaisesRegex(S.SchemaError, "can't read"):
            S.load_schema(os.path.join(d, "settings.json"))
        bad = copy.deepcopy(SCHEMA)
        bad["groups"][1]["fields"][0]["default"] = "0"
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump(bad, f)
        with self.assertRaisesRegex(S.SchemaError, "INTERVAL_DAYS: value must be at least 1"):
            S.load_schema(os.path.join(d, "settings.json"))


class SameRulesAsCore(unittest.TestCase):
    """settings.py keeps its own copy of the value rules so the hourly run never
    imports the core. These tests keep the copy honest."""

    TEXTS = ["", " ", "0", "1", "-1", "7", "90", "3650", "3651", "1.5", "10.5", "abc", "nan", "inf",
             "yes", "No", "TRUE", "off", "maybe", "text", "json", "Slack",
             "00:00-05:00", "22:00-04:00", "5:00-6:00", "10:00-10:00", "24:00-01:00", "01:00",
             "admin", "a b", "x@y.z", "https://ntfy.sh/x", "ftp://x", "http://", "https://h/p?q=1",
             "ZL1,ZL2", "ZL1, ZL1", "bad serial", "5,187,197,198", "5,5", "0,1", "256",
             "ZL1:30,ZL2:120", "ZL1:0", "ZL1", "ZL1:30,ZL1:40", ":5", "ZL1:x"]

    EXTRA = [  # attributes smart-scheduler's schema doesn't use yet
        {"type": "text", "max_length": 3, "required": True},
        {"type": "url", "schemes": ["https"]},
        {"type": "int_list", "required": True},
        {"type": "serial_list", "required": True},
        {"type": "float", "min": 0.5, "max": 2.5},
    ]

    def outcome(self, mod, f, text):
        try:
            v = mod.parse_value(f, text)
        except mod.FieldError as e:
            return ("error", str(e))
        return ("ok", v, mod.format_value(f, v))

    def test_every_field_and_type(self):
        fs = S.fields(SCHEMA) + [{"key": "X", "label": "X", "default": "", **e} for e in self.EXTRA]
        for f in fs:
            for text in self.TEXTS:
                with self.subTest(key=f["key"], type=f["type"], text=text):
                    self.assertEqual(self.outcome(S, f, text), self.outcome(P, f, text))

    def test_validate_and_parse_text_match(self):
        text = EXAMPLE + "INTERVAL_DAYS=5\nNOPE=1\nbroken line\nlower=1\nMAX_CONCURRENT=0\n"
        self.assertEqual(S.parse_text(text), P.parse_keyvalue(text))
        raw = S.parse_text(text)[0]
        self.assertEqual(S.validate(SCHEMA, raw), P.validate_config(SCHEMA, raw))


class ConfigLoader(unittest.TestCase):
    def config(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        return ss.Config(f.name)

    def test_field_level_errors_fall_back_to_defaults(self):
        c = self.config("INTERVAL_DAYS=0\nMAX_CONCURRENT=x\nSTART_WINDOW=1-2\nWEBHOOK_FORMAT=teams\n"
                        "DRIVE_USER=bob;drop\nDURATION_FACTOR=20\n")
        self.assertEqual(c.field_errors, {
            "INTERVAL_DAYS": "value must be at least 1",
            "MAX_CONCURRENT": "want a whole number, got 'x'",
            "START_WINDOW": "want HH:MM-HH:MM, e.g. 22:00-04:00",
            "WEBHOOK_FORMAT": "want one of text, json, slack, discord",
            "DRIVE_USER": "not in the expected form",
            "DURATION_FACTOR": "value must be at most 10",
        })
        self.assertIn("INTERVAL_DAYS: value must be at least 1", c.errors)
        self.assertEqual((c.int("INTERVAL_DAYS"), c.int("MAX_CONCURRENT"), c["DRIVE_USER"]), (90, 1, ""))
        self.assertEqual(c.window, (dt.time(0), dt.time(5)))
        self.assertEqual(c.float("DURATION_FACTOR"), 1.5)

    def test_values_are_canonical(self):
        c = self.config('NOTIFY_SUCCESS=TRUE\nWARN_ATTRIBUTES= 5, 187\nEXCLUDE_SERIALS="A1, B2"\n')
        self.assertTrue(c.yes("NOTIFY_SUCCESS"))
        self.assertEqual(c["WARN_ATTRIBUTES"], "5,187")
        self.assertEqual(c.set_of("EXCLUDE_SERIALS"), {"A1", "B2"})
        self.assertEqual(c.typed["WARN_ATTRIBUTES"], [5, 187])
        self.assertEqual(c.errors, [])

    def test_line_problems_and_repeats(self):
        c = self.config("INTERVAL_DAYS=10\nnot a setting\nINTERVAL_DAYS=20\n")
        self.assertEqual(c.int("INTERVAL_DAYS"), 10)   # first wins, as the contract says
        self.assertEqual(c.errors, ["config line 2: expected KEY=VALUE",
                                    "config line 3: INTERVAL_DAYS repeated; the first value is used"])

    def test_missing_file_is_all_defaults(self):
        c = ss.Config("/nonexistent")
        self.assertEqual(c.values, ss.DEFAULTS)
        self.assertEqual(c.errors, [])


class Writer(unittest.TestCase):
    def test_round_trip_changes_only_edited_lines(self):
        text = "# my own note at the top\n\n" + EXAMPLE + "\n# trailing note\nSOMETHING_ELSE=keep me\nnot a setting\n"
        new = S.apply_edits(text, {"INTERVAL_DAYS": "45", "WEBHOOK_URL": "https://ntfy.sh/nas",
                                   "START_WINDOW": "22:00-04:00", "NOTIFY_SUCCESS": True}, SCHEMA)
        self.assertEqual(sorted(changed_lines(text, new)), sorted([
            ("INTERVAL_DAYS=90", "INTERVAL_DAYS=45"),
            ("WEBHOOK_URL=", "WEBHOOK_URL=https://ntfy.sh/nas"),
            ("START_WINDOW=00:00-05:00", "START_WINDOW=22:00-04:00"),
            ("NOTIFY_SUCCESS=no", "NOTIFY_SUCCESS=yes"),
        ]))
        raw = S.parse_text(new)[0]
        self.assertEqual((raw["INTERVAL_DAYS"], raw["SOMETHING_ELSE"]), ("45", "keep me"))

    def test_no_edits_is_byte_identical(self):
        text = "  # odd\r\nINTERVAL_DAYS = '90'  \r\nx"
        self.assertEqual(S.apply_edits(text, {}, SCHEMA), text)
        self.assertEqual(S.apply_edits(EXAMPLE, {k: v for k, v in S.defaults(SCHEMA).items()}, SCHEMA), EXAMPLE)

    def test_keeps_spacing_quotes_and_meaning(self):
        text = '  INTERVAL_DAYS = "90"  \nNOTIFY_SUCCESS=true\nDRIVE_USER=\'admin\'\n'
        new = S.apply_edits(text, {"INTERVAL_DAYS": "45", "NOTIFY_SUCCESS": "yes", "DRIVE_USER": "ops"}, SCHEMA)
        self.assertEqual(new, '  INTERVAL_DAYS = "45"  \nNOTIFY_SUCCESS=true\nDRIVE_USER=\'ops\'\n')

    def test_canonical_form(self):
        new = S.apply_edits("WARN_ATTRIBUTES=5\n", {"WARN_ATTRIBUTES": " 5, 187 ,197",
                                                    "INTERVAL_OVERRIDES": {"ZL1": 30, "ZL2": 120}}, SCHEMA)
        self.assertEqual(new, "WARN_ATTRIBUTES=5,187,197\n" + "INTERVAL_OVERRIDES=ZL1:30,ZL2:120\n")

    def test_edits_the_line_the_loader_reads(self):
        new = S.apply_edits("INTERVAL_DAYS=10\nINTERVAL_DAYS=20\n", {"INTERVAL_DAYS": 30}, SCHEMA)
        self.assertEqual(new, "INTERVAL_DAYS=30\nINTERVAL_DAYS=20\n")

    def test_commented_out_key_is_not_edited(self):
        new = S.apply_edits("# INTERVAL_DAYS=10\n", {"INTERVAL_DAYS": "30"}, SCHEMA)
        self.assertEqual(new, "# INTERVAL_DAYS=10\n\n# --- How often each drive gets an extended test "
                              + "-" * 30 + "\nINTERVAL_DAYS=30\n")

    def test_none_removes_the_line(self):
        text = "# Healthy drive.\nINTERVAL_DAYS=10\nMAX_CONCURRENT=2\n"
        self.assertEqual(S.apply_edits(text, {"INTERVAL_DAYS": None, "BUSY_KBPS": None}, SCHEMA),
                         "# Healthy drive.\nMAX_CONCURRENT=2\n")

    def test_new_key_goes_after_its_group_neighbour(self):
        text = EXAMPLE.replace("OLD_DRIVE_HOURS=43800\n", "")
        new = S.apply_edits(text, {"OLD_DRIVE_HOURS": "50000"}, SCHEMA)
        self.assertEqual(new, EXAMPLE.replace("OLD_DRIVE_HOURS=43800", "OLD_DRIVE_HOURS=50000"))

    def test_new_first_key_goes_above_the_next_ones_comment(self):
        text = EXAMPLE.replace("INTERVAL_DAYS=90\n", "")
        new = S.apply_edits(text, {"INTERVAL_DAYS": "45"}, SCHEMA)
        heading = "# --- How often each drive gets an extended test ------------------------------\n"
        # "# Healthy drive." now runs straight into the next setting's comment, so the
        # block can't be split; the key goes above it, just under the heading.
        self.assertIn(heading + "INTERVAL_DAYS=45\n# Healthy drive.\n# Drive showing", new)

    def test_new_key_under_its_heading(self):
        text = "ENABLED=yes\n\n# --- Notifications ---\n# (nothing set yet)\n\n# --- Other ---\nX=1\n"
        new = S.apply_edits(text, {"WEBHOOK_URL": "https://h/x", "HEARTBEAT_URL": "https://h/y"}, SCHEMA)
        self.assertEqual(new, "ENABLED=yes\n\n# --- Notifications ---\nWEBHOOK_URL=https://h/x\n"
                              "HEARTBEAT_URL=https://h/y\n# (nothing set yet)\n\n# --- Other ---\nX=1\n")

    def test_new_key_at_the_end_with_a_heading(self):
        new = S.apply_edits("ENABLED=no", {"START_WINDOW": "01:00-02:00", "ENABLED": "yes"}, SCHEMA)
        self.assertEqual(new, "ENABLED=yes\n\n# --- When tests may start " + "-" * 52
                              + "\nSTART_WINDOW=01:00-02:00\n")
        self.assertEqual(S.apply_edits("", {"DRIVE_USER": "ops"}, SCHEMA),
                         "# --- General " + "-" * 65 + "\nDRIVE_USER=ops\n")

    def test_crlf_files_stay_crlf(self):
        text = EXAMPLE.replace("\n", "\r\n").replace("RETRY_FAILED_DAYS=7\r\n", "")
        new = S.apply_edits(text, {"RETRY_FAILED_DAYS": "3", "INTERVAL_DAYS": "45"}, SCHEMA)
        self.assertNotIn("\n", new.replace("\r\n", ""))
        self.assertIn("OLD_DRIVE_HOURS=43800\r\nRETRY_FAILED_DAYS=3\r\n", new)

    def test_invalid_values_are_refused_per_field(self):
        with self.assertRaises(S.ValidationError) as cm:
            S.apply_edits(EXAMPLE, {"INTERVAL_DAYS": "0", "START_WINDOW": "late", "NOPE": "1",
                                    "WEBHOOK_URL": "ftp://x", "DRIVE_USER": "a\nENABLED=no",
                                    "EXCLUDE_SERIALS": "ZL1,ZL1", "MAX_CONCURRENT": "4"}, SCHEMA)
        self.assertEqual(cm.exception.errors, {
            "INTERVAL_DAYS": "value must be at least 1",
            "START_WINDOW": "want HH:MM-HH:MM, e.g. 22:00-04:00",
            "NOPE": "unknown setting",
            "WEBHOOK_URL": "URL must start with http:// or https://",
            "DRIVE_USER": "must be on one line",
            "EXCLUDE_SERIALS": "ZL1 listed twice",
        })

    def test_a_new_setting_needs_only_the_schema(self):
        schema = copy.deepcopy(SCHEMA)
        timing = next(g for g in schema["groups"] if g["id"] == "timing")
        timing["fields"].append({"key": "BACKUP_MARGIN_HOURS", "type": "int", "label": "Backup margin",
                                 "unit": "hours", "min": 0, "max": 48, "default": "1"})
        P.check_schema(schema)
        new = S.apply_edits(EXAMPLE, {"BACKUP_MARGIN_HOURS": "3"}, schema)
        self.assertIn("BUSY_KBPS=20000\nBACKUP_MARGIN_HOURS=3\n", new)
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(new)
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(ss.Config(f.name, schema).int("BACKUP_MARGIN_HOURS"), 3)
        self.assertEqual(ss.Config("/nonexistent", schema).int("BACKUP_MARGIN_HOURS"), 1)
        self.assertEqual(S.defaults(schema)["BACKUP_MARGIN_HOURS"], "1")


class Save(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "config")

    def mode(self):
        return stat.S_IMODE(os.stat(self.path).st_mode)

    def test_load_edit_save_round_trip(self):
        with open(self.path, "w") as f:
            f.write(EXAMPLE + "# mine\nLEGACY=1\n")
        os.chmod(self.path, 0o644)
        S.save(self.path, {"INTERVAL_DAYS": "45", "WEBHOOK_FORMAT": "slack"}, SCHEMA)
        self.assertEqual(self.mode(), 0o600)
        with open(self.path) as f:
            text = f.read()
        self.assertEqual(text, (EXAMPLE + "# mine\nLEGACY=1\n")
                         .replace("INTERVAL_DAYS=90", "INTERVAL_DAYS=45")
                         .replace("WEBHOOK_FORMAT=text", "WEBHOOK_FORMAT=slack"))
        c = ss.Config(self.path)
        self.assertEqual((c.int("INTERVAL_DAYS"), c["WEBHOOK_FORMAT"]), (45, "slack"))
        self.assertEqual(c.errors, ["LEGACY: unknown setting"])
        self.assertEqual(os.listdir(self.dir), ["config"])     # no temporary file left behind

    def test_creates_a_missing_file(self):
        S.save(self.path, {"WEBHOOK_URL": "https://ntfy.sh/x"}, SCHEMA)
        self.assertEqual(self.mode(), 0o600)
        self.assertEqual(ss.Config(self.path)["WEBHOOK_URL"], "https://ntfy.sh/x")

    def test_refused_edits_leave_the_file_alone(self):
        with open(self.path, "w") as f:
            f.write("INTERVAL_DAYS=10\n")
        before = os.stat(self.path)
        with self.assertRaises(S.ValidationError):
            S.save(self.path, {"INTERVAL_DAYS": "45", "MAX_CONCURRENT": "0"}, SCHEMA)
        with open(self.path) as f:
            self.assertEqual(f.read(), "INTERVAL_DAYS=10\n")
        self.assertEqual(os.stat(self.path).st_ino, before.st_ino)

    def test_a_failed_write_leaves_the_old_file(self):
        with open(self.path, "w") as f:
            f.write("INTERVAL_DAYS=10\n")
        real = os.replace

        def boom(a, b):
            raise OSError("disk full")
        os.replace = boom
        try:
            with self.assertRaises(OSError):
                S.save(self.path, {"INTERVAL_DAYS": "45"}, SCHEMA)
        finally:
            os.replace = real
        with open(self.path) as f:
            self.assertEqual(f.read(), "INTERVAL_DAYS=10\n")
        self.assertEqual(os.listdir(self.dir), ["config"])


if __name__ == "__main__":
    unittest.main()
