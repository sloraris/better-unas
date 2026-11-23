"""core/lib: the library tools import. Run from the repository root:

    python3 -m unittest discover -s core/tests

A throwaway tool folder stands in for a real one; systemctl is faked and
trigger files go under a temporary /etc. Nothing touches a NAS.
"""

import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.lib import busy               # noqa: E402
from core.lib import config as C        # noqa: E402
from core.lib import settings as S      # noqa: E402
from core.lib.alerts import RESOLVED_KEEP, Alerts  # noqa: E402
from core.lib.tool import Tool          # noqa: E402

SCHEMA = {"contract": 1, "groups": [{"id": "general", "title": "General", "fields": [
    {"key": "ENABLED", "type": "bool", "label": "Enabled", "default": "yes"},
    {"key": "OWN_HEARTBEAT_URL", "type": "url", "label": "Own heartbeat URL", "default": ""},
    {"key": "OWN_HEARTBEAT_FAIL_URL", "type": "url", "label": "Own heartbeat URL on failure", "default": ""}]}]}
T0 = dt.datetime(2026, 11, 23, 3, 0, tzinfo=dt.timezone.utc)


class FakeSystemd:
    """systemctl as far as the triggers care: enable --now makes the timer enabled and active."""

    def __init__(self):
        self.calls, self.enabled = [], False

    def __call__(self, *cmd, timeout=60, env=None):
        self.calls.append(cmd)
        if cmd[:2] == ("systemctl", "is-enabled"):
            return 0, "enabled" if self.enabled else "disabled"
        if cmd[:2] == ("systemctl", "is-active"):
            return 0, "active" if self.enabled else "inactive"
        if cmd[:3] == ("systemctl", "enable", "--now"):
            self.enabled = True
        if cmd[:3] == ("systemctl", "disable", "--now"):
            self.enabled = False
        return 0, ""


class ToolFolder(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        self.base = os.path.join(self.home, "demo-tool")
        os.makedirs(os.path.join(self.base, "triggers"))
        os.makedirs(os.path.join(self.base, "__pycache__"))
        files = {"demo_tool.py": "# the script\n", "install.sh": "#!/bin/sh\n", "uninstall.sh": "#!/bin/sh\n",
                 "settings.json": json.dumps(SCHEMA), "config.example": "ENABLED=yes\n",
                 "triggers/demo-tool.service": "[Service]\nExecStart=@BASE@/demo_tool.py run\n",
                 "triggers/demo-tool.timer": "[Timer]\nOnCalendar=hourly\n",
                 "triggers/demo-tool.cron": "7 * * * * root @BASE@/demo_tool.py run --source cron\n",
                 "__pycache__/x.pyc": "", "state.json": "{}", "config": "ENABLED=yes\n"}
        for rel, text in files.items():
            with open(os.path.join(self.base, rel), "w") as f:
                f.write(text)
        self.etc = os.path.join(self.home, "etc")
        os.makedirs(os.path.join(self.etc, "systemd", "system"))
        os.makedirs(os.path.join(self.etc, "cron.d"))
        self.tool = Tool("demo-tool", os.path.join(self.base, "demo_tool.py"), required=("smartctl",), etc=self.etc)
        self.systemd = FakeSystemd()
        self.tool.sh = self.systemd
        self.tool.which = lambda x: "/usr/bin/" + x
        self.tool.log = lambda msg, level=None: None

    def cfg(self, **kw):
        with open(self.tool.config_file, "w") as f:
            f.write("".join(f"{k}={v}\n" for k, v in kw.items()))
        return self.tool.config()

    def alerts(self, cfg, state):
        return Alerts(cfg, state, False, "demo-tool", now=lambda: T0, log=lambda m, level=None: None,
                      send=lambda *a: None)


class Triggers(ToolFolder):
    def test_paths_follow_the_template_names(self):
        self.assertEqual(self.tool.triggers, {
            "demo-tool.cron": os.path.join(self.etc, "cron.d", "demo-tool"),
            "demo-tool.service": os.path.join(self.etc, "systemd", "system", "demo-tool.service"),
            "demo-tool.timer": os.path.join(self.etc, "systemd", "system", "demo-tool.timer")})
        self.assertEqual(self.tool.timer_unit, "demo-tool.timer")

    def test_heal_then_nothing_left_to_fix(self):
        self.assertEqual(len(self.tool.trigger_problems()), 5)      # three files, not enabled, not active
        fixed = self.tool.heal(False)
        self.assertEqual(len(fixed), 3)
        with open(os.path.join(self.etc, "cron.d", "demo-tool")) as f:
            self.assertIn(self.base + "/demo_tool.py", f.read())    # @BASE@ filled in
        self.assertIn(("systemctl", "enable", "--now", "demo-tool.timer"), self.systemd.calls)
        self.assertEqual(self.tool.trigger_problems(), [])
        self.assertEqual(self.tool.heal(False), [])

    def test_dry_heal_writes_nothing(self):
        self.assertEqual(len(self.tool.heal(True)), 3)
        self.assertEqual(os.listdir(os.path.join(self.etc, "cron.d")), [])
        self.assertNotIn(("systemctl", "daemon-reload"), self.systemd.calls)

    def test_remove(self):
        self.tool.heal(False)
        self.assertEqual(len(self.tool.remove_triggers(False)), 3)
        self.assertFalse(self.systemd.enabled)
        self.assertEqual(os.listdir(os.path.join(self.etc, "cron.d")), [])


class Manifest(ToolFolder):
    def test_covers_code_and_templates_not_data(self):
        self.assertEqual(self.tool.own_files(), [
            "config.example", "demo_tool.py", "install.sh", "settings.json", "triggers/demo-tool.cron",
            "triggers/demo-tool.service", "triggers/demo-tool.timer", "uninstall.sh"])

    def test_verify(self):
        self.assertEqual(self.tool.verify_manifest(), ["manifest.sha256 is missing (re-run install.sh)"])
        self.tool.write_manifest()
        self.assertEqual(self.tool.verify_manifest(), [])
        with open(os.path.join(self.base, "triggers", "demo-tool.timer"), "a") as f:
            f.write("# edited\n")
        os.remove(os.path.join(self.base, "install.sh"))
        self.assertEqual(self.tool.verify_manifest(), ["install.sh is missing",
                                                       "triggers/demo-tool.timer changed since install"])

    def test_state_and_config_changes_are_not_problems(self):
        self.tool.write_manifest()
        self.tool.save_state({"x": 1}, False)
        self.cfg(ENABLED="no")
        self.assertEqual(self.tool.verify_manifest(), [])


class Run(ToolFolder):
    def test_self_check_heals_only_trusted_folders(self):
        cfg, state = self.cfg(), {}
        alerts = self.alerts(cfg, state)
        self.assertFalse(self.tool.self_check(cfg, alerts, False))      # no manifest yet
        self.assertIn("manifest", alerts.active)
        self.assertIn("Triggers broken (not healed)", alerts.active["triggers"]["msg"])
        self.tool.write_manifest()
        self.assertTrue(self.tool.self_check(cfg, alerts, False))
        self.assertEqual(set(alerts.active), set())
        self.tool.which = lambda x: None
        self.tool.self_check(cfg, alerts, False)
        self.assertIn("smartctl", alerts.active["tools"]["msg"])

    def test_turned_off_removes_the_triggers_once(self):
        self.tool.heal(False)
        cfg, state = self.cfg(ENABLED="no"), {}
        alerts = self.alerts(cfg, state)
        self.assertTrue(self.tool.turned_off(cfg, state, alerts, False))
        self.assertTrue(self.tool.turned_off(cfg, state, alerts, False))
        self.assertEqual([m["level"] for m in alerts.outbox], ["info"])
        self.assertTrue(state["disabled"])
        cfg = self.cfg(ENABLED="yes")
        self.assertFalse(self.tool.turned_off(cfg, state, alerts, False))
        self.assertNotIn("disabled", state)

    def test_cron_skips_only_while_the_timer_is_healthy_and_recent(self):
        state = {"last_tick": (T0 - dt.timedelta(minutes=10)).isoformat()}
        self.assertFalse(self.tool.cron_can_skip(state, T0, dt.timedelta(minutes=30)))   # triggers missing
        self.tool.heal(False)
        self.assertTrue(self.tool.cron_can_skip(state, T0, dt.timedelta(minutes=30)))
        self.assertFalse(self.tool.cron_can_skip(state, T0, dt.timedelta(minutes=5)))
        self.assertFalse(self.tool.cron_can_skip({}, T0, dt.timedelta(hours=2)))

    def test_lock(self):
        lock = self.tool.take_lock(0)
        self.assertIsNotNone(lock)
        self.assertIsNone(self.tool.take_lock(0))
        lock.close()
        self.assertEqual(self.tool.lock_for("status", "timer", ("run",), 60), (None, False))


class BusyRegistry(ToolFolder):
    def other(self, name, windows, updated=T0):
        d = os.path.join(self.home, name)
        os.makedirs(d, exist_ok=True)
        busy.publish(d, name, windows, updated)
        return d

    def test_tools_read_each_others_windows_not_their_own(self):
        h = dt.timedelta(hours=1)
        self.tool.publish_busy([(T0 + h, T0 + 2 * h, "demo-tool job", "planned")], T0)
        self.other("scrub-repair", [busy.Busy(T0 + 3 * h, T0 + 9 * h, "scrub-repair scrub", "scrub", "x"),
                                    (T0 - 2 * h, T0 - h, "ended", "running")])
        got = self.tool.others_busy(T0)
        self.assertEqual([(w.label, w.kind, w.tool) for w in got], [("scrub-repair scrub", "scrub", "scrub-repair")])
        mine = busy.read(self.home, T0, exclude="scrub-repair")
        self.assertEqual([(w.label, w.start) for w in mine], [("demo-tool job", T0 + h)])
        self.assertEqual(busy.overlapping(got, T0, T0 + 4 * h), got)
        self.assertEqual(busy.overlapping(got, T0, T0 + 3 * h), [])

    def test_stale_and_broken_files_are_ignored(self):
        h = dt.timedelta(hours=1)
        self.other("old-tool", [(T0, T0 + 99 * h, "old", "planned")], updated=T0 - busy.STALE - h)
        d = os.path.join(self.home, "broken-tool")
        os.makedirs(d)
        with open(os.path.join(d, busy.FILE), "w") as f:
            f.write("{not json")
        self.assertEqual(self.tool.others_busy(T0), [])
        with self.assertRaises(ValueError):
            busy.publish(d, "broken-tool", [(T0, T0 + h, "x", "lunch")], T0)

    def test_withdrawn_when_turned_off_or_uninstalled(self):
        h = dt.timedelta(hours=1)
        self.tool.publish_busy([(T0 + h, T0 + 2 * h, "job", "planned")], T0)
        reader = self.other("reader", [])
        name = lambda: [w.label for w in busy.read(self.home, T0, exclude="reader")]   # noqa: E731
        self.assertEqual(name(), ["job"])
        cfg, state = self.cfg(ENABLED="no"), {}
        with mock.patch("core.lib.util.now", lambda: T0):
            self.tool.turned_off(cfg, state, self.alerts(cfg, state), False)
        self.assertEqual(name(), [])
        self.assertTrue(os.path.isdir(reader))
        self.assertIn(busy.FILE, __import__("core.lib.tool", fromlist=["DATA"]).DATA)

    def test_not_in_the_manifest(self):
        self.tool.write_manifest()
        self.tool.publish_busy([(T0, T0 + dt.timedelta(hours=1), "job", "running")], T0)
        self.assertEqual(self.tool.verify_manifest(), [])


class Install(ToolFolder):
    def install(self, check=0):
        out = io.StringIO()
        with mock.patch("os.geteuid", lambda: 0), redirect_stdout(out):
            rc = self.tool.install(lambda: check, done=("Installed.",), failed=("Look above.",))
        return rc, out.getvalue()

    def test_fresh_install(self):
        os.remove(self.tool.config_file)
        rc, out = self.install()
        self.assertEqual(rc, 0)
        self.assertIn("Created config from config.example", out)
        self.assertIn("is not under /persistent", out)
        self.assertTrue(out.rstrip().endswith("Installed."))
        self.assertEqual(oct(os.stat(self.tool.config_file).st_mode & 0o777), "0o600")
        self.assertEqual(self.tool.verify_manifest(), [])
        self.assertEqual(self.tool.trigger_problems(), [])

    def test_failed_self_check(self):
        rc, out = self.install(check=1)
        self.assertEqual(rc, 1)
        self.assertIn("Installed, but the self-check found problems (above).\nLook above.", out)

    def test_turned_off_only_removes_triggers(self):
        self.tool.heal(False)
        self.cfg(ENABLED="no")
        rc, out = self.install()
        self.assertEqual(rc, 0)
        self.assertIn("Turned off (ENABLED=no)", out)
        self.assertFalse(os.path.exists(self.tool.manifest_file))
        self.assertEqual(os.listdir(os.path.join(self.etc, "cron.d")), [])

    def test_missing_tool_stops(self):
        self.tool.which = lambda x: None if x == "smartctl" else "/usr/bin/" + x
        rc, out = self.install()
        self.assertEqual((rc, out.strip().splitlines()[-1]), (1, "Missing required tool: smartctl"))

    def test_uninstall_purge(self):
        with mock.patch("os.geteuid", lambda: 0), redirect_stdout(io.StringIO()):
            self.assertEqual(self.tool.uninstall(purge=True), 0)
        self.assertFalse(os.path.exists(self.base))


class SharedConfig(ToolFolder):
    def test_shared_schema_is_core_settings_json(self):
        core = S.load_schema(os.path.join(ROOT, "core", "settings.json"))
        want = [f for f in S.fields(core) if f["key"] in S.SHARED_KEYS]
        self.assertEqual(S.fields(C.SHARED_SCHEMA), want)
        self.assertEqual([f["key"] for f in want], list(S.SHARED_KEYS))

    def test_better_unas_conf_wins_tool_config_is_fallback(self):
        with open(self.tool.core_config_file, "w") as f:
            f.write("WEBHOOK_URL=https://hook.example/a\n")
        cfg = self.cfg(WEBHOOK_URL="https://hook.example/old", ALERT_REPEAT_HOURS="5", NOPE="1")
        self.assertEqual(cfg["WEBHOOK_URL"], "https://hook.example/a")
        self.assertEqual(cfg["ALERT_REPEAT_HOURS"], 5)
        self.assertTrue(cfg["ENABLED"])
        self.assertEqual(cfg.values["ENABLED"], "yes")
        self.assertEqual(cfg.errors, ["NOPE: unknown setting"])


class AlertModel(ToolFolder):
    def test_repeat_level_and_resolved(self):
        cfg, state = self.cfg(), {}
        a = self.alerts(cfg, state)
        a.raise_("k", "disk hot", "warning")
        a.raise_("k", "disk hot", "warning")                     # same: not sent again
        a.raise_("k", "disk hot", "error")                       # escalated: sent
        self.assertEqual([m["level"] for m in a.outbox], ["warning", "error"])
        self.assertTrue(a.has_errors())
        for i in range(RESOLVED_KEEP + 3):
            a.raise_(f"r{i}", "x")
            a.clear(f"r{i}")
        self.assertEqual(len(state["resolved"]), RESOLVED_KEEP)

    def test_outbox_kept_while_the_webhook_is_down(self):
        cfg, state = self.cfg(WEBHOOK_URL="https://hook.example/a"), {}
        sent = []
        a = Alerts(cfg, state, False, "demo-tool", now=lambda: T0, log=lambda m, level=None: None,
                   send=lambda url, fmt, m, host, source: sent.append(source) or "timed out")
        a.info("one")
        a.flush()
        self.assertEqual((sent, len(state["outbox"])), (["demo-tool"], 1))


class NoCopies(unittest.TestCase):
    def test_tools_carry_no_copy_of_the_library(self):
        for name in sorted(os.listdir(ROOT)):
            if os.path.isfile(os.path.join(ROOT, name, "tool.conf")):
                with self.subTest(tool=name):
                    self.assertFalse(os.path.exists(os.path.join(ROOT, name, "settings.py")))


if __name__ == "__main__":
    unittest.main()
