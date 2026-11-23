"""A tool's folder and the plumbing every tool shares: its paths, state file,
run lock, install manifest, timer and cron triggers, self-healing, and the
install and uninstall steps the suite runs.

    TOOL = Tool("health-monitor", __file__, required=("smartctl", "lsblk", "systemctl"))

Triggers come from the tool's triggers/ folder, one template per file, with
@BASE@ replaced by the tool's folder:

    <name>.service, <name>.timer   -> /etc/systemd/system/
    <name>.cron                    -> /etc/cron.d/<name>

The timer is <name>.timer. Self-healing rewrites a trigger only while the
tool's own files match the manifest install wrote, so a changed or half-copied
folder is reported, never trusted.

Standard library only.
"""

import fcntl
import json
import os
import shutil
import syslog
import time

from core.lib import busy, notify, util
from core.lib import settings as S
from core.lib.alerts import Alerts
from core.lib.config import Config

# Files in a tool's folder that are its own data, not code: never in the manifest.
DATA = ("config", "state.json", ".lock", "manifest.sha256", "tool.conf.uninstalled", busy.FILE)


class Tool:
    """sh and which are attributes so tests can swap them; etc is the root
    trigger files are written under."""

    def __init__(self, name, script, required=(), etc="/etc"):
        self.name = name
        self.base = os.path.dirname(os.path.realpath(script))
        self.script = os.path.basename(script)
        self.required = tuple(required)
        self.config_file = os.path.join(self.base, "config")
        self.example_file = os.path.join(self.base, "config.example")
        self.home = os.path.dirname(self.base)       # the suite folder, beside the other tools
        self.core_config_file = os.path.join(self.home, "better-unas.conf")
        self.state_file = os.path.join(self.base, "state.json")
        self.manifest_file = os.path.join(self.base, "manifest.sha256")
        self.lock_file = os.path.join(self.base, ".lock")
        self.template_dir = os.path.join(self.base, "triggers")
        self.schema = S.load_schema(os.path.join(self.base, "settings.json"))
        self.timer_unit = f"{name}.timer"
        self.etc = etc
        self.sh = util.sh
        self.which = shutil.which

    # ------------------------------------------------------------ basics

    def log(self, msg, level=syslog.LOG_INFO):
        util.log(self.name, msg, level)

    def config(self, path=None, core_path=None):
        return Config(self.schema, path or self.config_file, core_path or self.core_config_file)

    def alerts(self, cfg, state, dry, now=None):
        return Alerts(cfg, state, dry, self.name, now=now, log=self.log)

    def load_state(self):
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_state(self, state, dry):
        if not dry:
            util.write_atomic(self.state_file, json.dumps(state, indent=1, sort_keys=True), 0o600, self.name)

    def take_lock(self, wait):
        """The run lock, held until the returned file is closed, or None if it
        stayed busy for `wait` seconds. Anything that writes state takes it."""
        lock = open(self.lock_file, "w")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    lock.close()
                    return None
                time.sleep(1)

    def missing_tools(self):
        return [x for x in self.required if not self.which(x)]

    def ping(self, url):
        """GET a heartbeat URL. Returns None, or why it failed (also logged)."""
        if url:
            err = notify.open_url(url, source=self.name)
            if err:
                self.log(f"heartbeat ping failed: {err}", syslog.LOG_WARNING)
            return err

    def heartbeat(self, cfg, alerts, dry):
        """After a run: ping the failure URL if an error is open, else the ok URL."""
        if not dry:
            ok_url, fail_url = notify.heartbeat_urls(cfg)
            self.ping(fail_url if alerts.has_errors() else ok_url)

    # ------------------------------------------------------------ manifest

    def own_files(self):
        """The tool's code and templates, relative to its folder, sorted."""
        out = []
        for root, dirs, files in os.walk(self.base):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), self.base)
                if rel in DATA or f.endswith(".pyc") or ".tmp-" in f:
                    continue
                out.append(rel)
        return sorted(out)

    def write_manifest(self):
        lines = [f"{util.sha256(os.path.join(self.base, rel))}  {rel}\n" for rel in self.own_files()]
        util.write_atomic(self.manifest_file, "".join(lines), 0o644, self.name)

    def verify_manifest(self):
        """A list of problems with this folder's own files."""
        try:
            with open(self.manifest_file) as f:
                lines = [ln.split(None, 1) for ln in f if ln.strip()]
        except OSError:
            return ["manifest.sha256 is missing (re-run install.sh)"]
        problems = []
        for digest, rel in lines:
            rel = rel.strip()
            path = os.path.join(self.base, rel)
            if not os.path.exists(path):
                problems.append(f"{rel} is missing")
            elif util.sha256(path) != digest:
                problems.append(f"{rel} changed since install")
        return problems

    # ------------------------------------------------------------ triggers

    @property
    def triggers(self):
        """{template file name: installed path}."""
        out = {}
        for t in sorted(os.listdir(self.template_dir)) if os.path.isdir(self.template_dir) else []:
            if t.endswith((".service", ".timer")):
                out[t] = os.path.join(self.etc, "systemd", "system", t)
            elif t.endswith(".cron"):
                out[t] = os.path.join(self.etc, "cron.d", t[:-len(".cron")])
        return out

    def render(self, template):
        with open(os.path.join(self.template_dir, template)) as f:
            return f.read().replace("@BASE@", self.base)

    def trigger_problems(self):
        problems = []
        for template, path in self.triggers.items():
            try:
                with open(path) as f:
                    if f.read() != self.render(template):
                        problems.append(f"{path} differs from the copy in {self.template_dir}")
            except FileNotFoundError:
                problems.append(f"{path} is missing")
        for what, want in (("is-enabled", "enabled"), ("is-active", "active")):
            _, out = self.sh("systemctl", what, self.timer_unit)
            if out != want:
                problems.append(f"{self.timer_unit} is not {want} ({out or 'unknown'})")
        return problems

    def heal(self, dry):
        """Rewrite missing or changed trigger files and re-enable the timer. Returns what was fixed."""
        fixed, units_changed = [], False
        for template, path in self.triggers.items():
            want = self.render(template)
            try:
                with open(path) as f:
                    have = f.read()
            except FileNotFoundError:
                have = None
            if have != want:
                fixed.append(f"{'restored' if have is None else 'rewrote'} {path}")
                if not dry:
                    util.write_atomic(path, want, 0o644, self.name)
                units_changed |= "/systemd/" in path
        _, en = self.sh("systemctl", "is-enabled", self.timer_unit)
        _, ac = self.sh("systemctl", "is-active", self.timer_unit)
        if units_changed or en != "enabled" or ac != "active":
            if not units_changed:
                fixed.append(f"re-enabled {self.timer_unit}")
            if not dry:
                self.sh("systemctl", "daemon-reload")
                self.sh("systemctl", "enable", "--now", self.timer_unit)
        return fixed

    def remove_triggers(self, dry):
        removed = []
        if not dry:
            self.sh("systemctl", "disable", "--now", self.timer_unit)
        for path in self.triggers.values():
            if os.path.exists(path):
                removed.append(path)
                if not dry:
                    os.remove(path)
        if not dry:
            self.sh("systemctl", "daemon-reload")
        return removed

    # ------------------------------------------------------------ the run

    def self_check(self, cfg, alerts, dry):
        """The checks every tool's run starts with: config problems, own files,
        triggers (healed when the files can be trusted) and required tools."""
        for e in cfg.errors:
            alerts.raise_(f"config:{e}", f"Config problem: {e}", "warning")
        own = self.verify_manifest()
        heal_ok = not own
        if own:
            alerts.raise_("manifest", f"Files in {self.base} don't match the install manifest: "
                          f"{'; '.join(own)}. Not self-healing until install.sh is re-run.")
        else:
            alerts.clear("manifest")
            fixed = self.heal(dry)
            if fixed:
                alerts.info("Self-heal: " + "; ".join(fixed) + ".")
        problems = self.trigger_problems() if not dry else []
        if problems:
            alerts.raise_("triggers", ("Triggers still broken after self-heal: " if heal_ok
                                       else "Triggers broken (not healed): ") + "; ".join(problems))
        else:
            alerts.clear("triggers")
        missing = self.missing_tools()
        if missing:
            alerts.raise_("tools", "Missing tools on the NAS: " + ", ".join(missing)
                          + ". Not installing anything; a firmware update may have removed them.")
        else:
            alerts.clear("tools")
        return heal_ok

    def turned_off(self, cfg, state, alerts, dry, what="the timer and cron line"):
        """ENABLED=no: remove the triggers once and say so. True when off; the
        caller then flushes, saves and stops."""
        if cfg["ENABLED"]:
            state.pop("disabled", None)
            return False
        if not state.get("disabled"):
            removed = self.remove_triggers(dry)
            busy.withdraw(self.base, self.name, util.now(), dry)
            alerts.info(f"Turned off (ENABLED=no): removed {what} (" + (", ".join(removed) or "none")
                        + "). Turn Enabled back on in Settings to restart it.")
            state["disabled"] = True
        return True

    def publish_busy(self, windows, t, dry=False):
        """Replace this tool's windows in the busy registry (core/lib/busy.py)."""
        return busy.publish(self.base, self.name, windows, t, dry)

    def others_busy(self, t):
        """The other tools' busy windows that haven't ended by t."""
        return busy.read(self.home, t, exclude=self.name)

    def cron_can_skip(self, state, t, within):
        """The cron line is only a backup: True when the timer ran within
        `within` (a timedelta) and the triggers are healthy."""
        last = util.parse_time(state.get("last_tick"))
        return bool(last and t - last < within and not self.trigger_problems())

    def lock_for(self, command, source, locked, manual_wait):
        """Take the run lock for a command in `locked`: scheduled runs skip if
        one is already going, manual ones wait up to manual_wait seconds.
        Returns (lock or None, busy)."""
        if command not in locked:
            return None, False
        lock = self.take_lock(0 if source in ("timer", "cron") else manual_wait)
        return lock, lock is None

    # ------------------------------------------------------------ commands

    def cmd_heal(self, dry, force=False):
        own = self.verify_manifest()
        if own and not force:
            print("Not healing; own files don't match the manifest: " + "; ".join(own))
            return 1
        print("\n".join(self.heal(dry)) or "Triggers already OK.")
        return 0

    def install(self, check, after=(), done=(), failed=(), off_note=""):
        """What install.sh runs, as root: create the config from the example,
        write the manifest, heal the triggers and self-check. check() returns 0
        when the self-check passes; then after() runs and done lines print,
        else the failed lines. With ENABLED=no it only removes the triggers."""
        if os.geteuid() != 0:
            print("Run as root.")
            return 1
        if not self.base.startswith("/persistent/"):
            print(f"Warning: {self.base} is not under /persistent and may not survive firmware updates.")
        missing = [x for x in ("python3",) + self.required if not self.which(x)]
        if missing:
            print(f"Missing required tool: {missing[0]}")
            return 1
        if not os.path.exists(self.config_file):
            shutil.copyfile(self.example_file, self.config_file)
            print("Created config from config.example (all defaults).")
        os.chmod(self.config_file, 0o600)
        if not self.config()["ENABLED"]:
            self.remove_triggers(False)
            print("Turned off (ENABLED=no): removed the timer and cron line." + (f" {off_note}" if off_note else ""))
            return 0
        for f in (self.script, "install.sh", "uninstall.sh"):
            os.chmod(os.path.join(self.base, f), 0o755)
        self.write_manifest()
        print("\n".join(self.heal(False)) or "Triggers already OK.")
        print()
        if check() == 0:
            print()
            for step in after:
                step()
            print()
            print("\n".join(done))
            return 0
        print()
        print("\n".join(("Installed, but the self-check found problems (above).",) + tuple(failed)))
        return 1

    def uninstall(self, purge=False, note=""):
        """What uninstall.sh runs: remove the triggers so nothing runs or
        self-heals. Keeps the folder (config, state) unless purge."""
        if os.geteuid() != 0:
            print("Run as root.")
            return 1
        self.remove_triggers(False)
        busy.withdraw(self.base, self.name, util.now())
        print("Removed the timer, service and cron line." + (f" {note}" if note else ""))
        if purge:
            if os.path.basename(self.base) == self.name:
                shutil.rmtree(self.base)
                print(f"Deleted {self.base}.")
            else:
                print(f"Not deleting {self.base} (unexpected folder name).")
        else:
            print(f"Kept {self.base}. Run install.sh to turn it back on, or uninstall.sh --purge to delete it.")
        return 0

