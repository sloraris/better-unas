#!/usr/bin/env python3
"""health-monitor: early warning when a drive or filesystem error counter rises.

Every 15 minutes (a systemd timer, with a cron line as backup) it reads:

  - the watched SMART counters (WATCH_ATTRIBUTES) of every drive in a bay,
    with `smartctl -n standby`, so a drive that's asleep is skipped, not woken
  - the btrfs device error counters of each btrfs volume
  - each md array: whether a parity check that just finished found
    mismatches, and (MD_STATE_ALERTS=yes) whether a data array is degraded

and alerts through the shared webhook when one rises. The rules are in
monitor.py. It keeps no history: state.json holds each counter's last value
and the open alerts.

Usage:
  health_monitor.py run      one check: read, alert, self-heal the timer
  health_monitor.py status   the last check and open alerts
  health_monitor.py check    self-check only; non-zero exit on problems
  health_monitor.py heal     restore the timer and cron line from the copies here

Options:
  --dry-run   change nothing: no healing, no alerts, no state writes
  --source    who started this run (timer, cron, manual); set by the triggers

Standard library only; nothing is installed on the NAS.
"""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import syslog
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BASE)
import monitor  # noqa: E402  (lives next to this file)
import settings  # noqa: E402

NAME = "health-monitor"
CONFIG_FILE = os.path.join(BASE, "config")
CORE_CONFIG_FILE = os.path.join(os.path.dirname(BASE), "better-unas.conf")
STATE_FILE = os.path.join(BASE, "state.json")
MANIFEST_FILE = os.path.join(BASE, "manifest.sha256")
LOCK_FILE = os.path.join(BASE, ".lock")
TEMPLATE_DIR = os.path.join(BASE, "triggers")
API = "http://127.0.0.1:16080/api/v2"

TRIGGERS = {
    "health-monitor.service": "/etc/systemd/system/health-monitor.service",
    "health-monitor.timer": "/etc/systemd/system/health-monitor.timer",
    "health-monitor.cron": "/etc/cron.d/health-monitor",
}
TIMER_UNIT = "health-monitor.timer"
REQUIRED_TOOLS = ("smartctl", "lsblk", "systemctl")

SCHEMA = settings.load_schema(os.path.join(BASE, "settings.json"))
# The suite's shared settings as core/settings.json has them; tools don't import the core.
SHARED_SCHEMA = {"contract": 1, "groups": [{"id": "notifications", "title": "Notifications", "fields": [
    {"key": "WEBHOOK_URL", "type": "url", "label": "Webhook URL", "default": "", "secret": True},
    {"key": "WEBHOOK_FORMAT", "type": "choice", "label": "Webhook format",
     "choices": ["text", "json", "slack", "discord"], "default": "text"},
    {"key": "NOTIFY_SUCCESS", "type": "bool", "label": "Notify on success", "default": "no"},
    {"key": "ALERT_REPEAT_HOURS", "type": "int", "label": "Repeat alerts every", "min": 1, "default": "72"},
    {"key": "HEARTBEAT_URL", "type": "url", "label": "Heartbeat URL", "default": "", "secret": True},
    {"key": "HEARTBEAT_FAIL_URL", "type": "url", "label": "Heartbeat URL on failure", "default": "",
     "secret": True},
]}]}
RESOLVED_KEEP = 20


# --------------------------------------------------------------------------- helpers

def now():
    return dt.datetime.now().astimezone()


def parse_time(s):
    try:
        t = dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


def log(msg, level=syslog.LOG_INFO):
    print(msg, flush=True)
    try:
        syslog.openlog(NAME)
        syslog.syslog(level, msg)
    except Exception:
        pass


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def write_atomic(path, data, mode=0o644):
    tmp = path + ".tmp-" + NAME
    with open(tmp, "w") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def sh(*cmd, timeout=60):
    """Run a command; return (exit code, stdout). Never raises for a missing tool."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# --------------------------------------------------------------------------- config and state

class Config:
    """The config file checked against settings.json, plus the suite's shared
    settings, each read from better-unas.conf when it sets the key, else from
    this config. typed holds the parsed values; a bad line is reported in
    errors and its default used."""

    def __init__(self, path, core_path=None):
        raw, problems = settings.read(path)
        self.typed, field_errors = settings.validate(SCHEMA, raw)
        self.errors = [f"config {p}" for p in problems]
        core, core_problems = settings.read(core_path or CORE_CONFIG_FILE)
        self.errors += [f"better-unas.conf {p}" for p in core_problems]
        shared_raw = {k: core[k] if k in core else raw[k]
                      for k in settings.SHARED_KEYS if k in core or k in raw}
        shared, shared_errors = settings.validate(SHARED_SCHEMA, shared_raw)
        self.typed.update(shared)
        field_errors.update(shared_errors)
        self.errors += [f"{k}: {m}" for k, m in field_errors.items()]

    def __getitem__(self, k):
        return self.typed[k]


def heartbeat_urls(cfg):
    """(ok URL, failure URL): the tool's own pair when OWN_HEARTBEAT_URL is
    set, else the shared pair from Notifications."""
    if cfg["OWN_HEARTBEAT_URL"]:
        return cfg["OWN_HEARTBEAT_URL"], cfg["OWN_HEARTBEAT_FAIL_URL"]
    return cfg["HEARTBEAT_URL"], cfg["HEARTBEAT_FAIL_URL"]


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state, dry):
    if not dry:
        write_atomic(STATE_FILE, json.dumps(state, indent=1, sort_keys=True), 0o600)


# --------------------------------------------------------------------------- alerts

class Alerts:
    """Deduplicated alerts with an outbox, so nothing is lost while the
    webhook is down. The same model as smart-scheduler's."""

    def __init__(self, cfg, state, dry):
        self.cfg, self.state, self.dry = cfg, state, dry
        self.active = state.setdefault("alerts", {})
        self.outbox = state.setdefault("outbox", [])
        self.host = socket.gethostname()

    def raise_(self, key, msg, level="error"):
        t = now()
        a = self.active.get(key)
        repeat = dt.timedelta(hours=self.cfg["ALERT_REPEAT_HOURS"])
        if a and a["msg"] == msg and a["level"] == level and t - parse_time(a["sent"]) < repeat:
            return
        log(f"[{level}] {msg}", syslog.LOG_ERR if level == "error" else syslog.LOG_WARNING)
        self.active[key] = {"msg": msg, "level": level, "sent": t.isoformat(),
                            "since": (a or {}).get("since", t.isoformat())}
        self._queue(level, msg)

    def clear(self, key, msg=None):
        a = self.active.pop(key, None)
        if a:
            recent = self.state.setdefault("resolved", [])
            recent.append({"key": key, "msg": a["msg"], "level": a["level"],
                           "since": a.get("since", a["sent"]), "sent": a["sent"],
                           "resolved": now().isoformat()})
            del recent[:-RESOLVED_KEEP]
            text = msg or f"Resolved: {a['msg']}"
            log(f"[resolved] {text}")
            self._queue("resolved", text)

    def info(self, msg):
        log(f"[info] {msg}")
        self._queue("info", msg)

    def has_errors(self):
        return any(a["level"] == "error" for a in self.active.values())

    def _queue(self, level, msg):
        self.outbox.append({"level": level, "msg": msg, "at": now().isoformat()})
        del self.outbox[:-50]

    def flush(self):
        url = self.cfg["WEBHOOK_URL"]
        if self.dry:
            for m in self.outbox:
                print(f"(dry-run) would notify: [{m['level']}] {m['msg']}")
            return
        if not url:
            self.outbox.clear()
            return
        while self.outbox:
            if send_webhook(url, self.cfg["WEBHOOK_FORMAT"], self.outbox[0], self.host) is not None:
                log(f"webhook unreachable; {len(self.outbox)} message(s) kept for the next run",
                    syslog.LOG_WARNING)
                return
            self.outbox.pop(0)


def webhook_request(url, fmt, m, host):
    """The POST for one outbox message m ({level, msg, at}); the same formats
    as smart-scheduler and core/notify.py."""
    title = f"{host} {NAME}: {m['level']}"
    fmt = (fmt or "text").lower()
    if fmt == "json":
        body, ctype = json.dumps({"host": host, "source": NAME, "level": m["level"],
                                  "message": m["msg"], "at": m["at"]}), "application/json"
    elif fmt == "slack":
        body, ctype = json.dumps({"text": f"*{title}*\n{m['msg']}"}), "application/json"
    elif fmt == "discord":
        body, ctype = json.dumps({"content": f"**{title}**\n{m['msg']}"}), "application/json"
    else:
        body, ctype = f"{title}\n{m['msg']}", "text/plain; charset=utf-8"
    req = urllib.request.Request(url, data=body.encode(), method="POST")
    req.add_header("Content-Type", ctype)
    req.add_header("Title", title)
    return req


USER_AGENT = f"{NAME} (better-unas)"


def open_url(req, timeout=10):
    """Fetch req (a URL or Request). None on a 2xx answer, else a short error."""
    if isinstance(req, str):
        req = urllib.request.Request(req)
    if not req.has_header("User-agent"):
        req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return None if 200 <= r.status < 300 else f"HTTP {r.status} {r.reason}".strip()
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} {e.reason}".strip()
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        return "timed out" if "timed out" in str(reason) else (str(reason) or type(e).__name__)


def send_webhook(url, fmt, m, host=None, timeout=10):
    try:
        req = webhook_request(url, fmt, m, host or socket.gethostname())
    except ValueError as e:
        return str(e)
    return open_url(req, timeout)


def ping(url):
    if url:
        err = open_url(url)
        if err:
            log(f"heartbeat ping failed: {err}", syslog.LOG_WARNING)
        return err


# --------------------------------------------------------------------------- reading the NAS

def bay_drives():
    """{serial: {"device": "/dev/sdX", "drive": bay number or None}} for the
    drives in bays. The bay numbers come from UniFi Drive's local API (read
    only, no account needed); without it, every SATA disk lsblk lists counts
    and has no number."""
    rc, out = sh("lsblk", "-dno", "NAME,SERIAL,TRAN,TYPE")
    devices = {}
    for line in out.splitlines() if rc == 0 else []:
        parts = line.split()
        if len(parts) == 4 and parts[3] == "disk":
            devices[parts[1]] = {"device": "/dev/" + parts[0], "tran": parts[2]}
    slots = storage_slots()
    if slots is not None:
        return {s: {"device": devices[s]["device"], "drive": slot}
                for s, slot in slots.items() if s in devices}
    return {s: {"device": d["device"], "drive": None} for s, d in devices.items() if d["tran"] == "sata"}


def storage_slots():
    """{serial: bay number} from GET /storage, or None if it can't be read."""
    try:
        with urllib.request.urlopen(API + "/storage", timeout=15) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("disks"), list):
        return None
    return {d["serial"]: d.get("slotId") for d in data["disks"] if isinstance(d, dict) and d.get("serial")}


def parse_smartctl(text):
    """smartctl -j -n standby -A output -> {"asleep", "error", "attrs"}."""
    try:
        d = json.loads(text)
    except ValueError:
        return {"asleep": False, "error": "smartctl gave no JSON", "attrs": {}}
    msgs = " ".join(m.get("string", "") for m in (d.get("smartctl") or {}).get("messages") or [])
    if re.search(r"STANDBY|SLEEP", msgs):
        return {"asleep": True, "error": None, "attrs": {}}
    table = (d.get("ata_smart_attributes") or {}).get("table")
    if not isinstance(table, list):
        return {"asleep": False, "error": msgs.strip() or "no SMART attributes", "attrs": {}}
    attrs = {}
    for a in table:
        if isinstance(a.get("id"), int):
            attrs[a["id"]] = {"raw": (a.get("raw") or {}).get("value", 0) or 0,
                              "value": a.get("value"), "thresh": a.get("thresh") or 0}
    return {"asleep": False, "error": None, "attrs": attrs}


def read_smart(drives):
    out = {}
    for serial, d in drives.items():
        rc, text = sh("smartctl", "-j", "-n", "standby", "-A", d["device"], timeout=30)
        out[serial] = dict(parse_smartctl(text), drive=d["drive"])
    return out


def btrfs_mounts(mounts_text):
    """{source device: first mount point} for btrfs in /proc/mounts."""
    out = {}
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "btrfs":
            out.setdefault(parts[0], parts[1].replace("\\040", " "))
    return out


STAT_RE = re.compile(r"^\[(.+)\]\.(\w+)\s+(\d+)\s*$")


def parse_btrfs_stats(text):
    """`btrfs device stats` output -> {device: {counter: n}}."""
    out = {}
    for line in text.splitlines():
        m = STAT_RE.match(line.strip())
        if m:
            out.setdefault(m.group(1), {})[m.group(2)] = int(m.group(3))
    return out


def read_btrfs(mounts_path="/proc/mounts"):
    """{device: {"mount", "counters"}}, {} with no btrfs, or None if a volume
    couldn't be read."""
    mounts = btrfs_mounts(read_file(mounts_path) or "")
    if not mounts:
        return {}
    if not shutil.which("btrfs"):
        return None
    out = {}
    for mount in dict.fromkeys(mounts.values()):
        rc, text = sh("btrfs", "device", "stats", mount, timeout=30)
        stats = parse_btrfs_stats(text)
        if not stats:
            return None
        for dev, counters in stats.items():
            out[dev] = {"mount": mount, "counters": counters}
    return out


def read_md(sys_block="/sys/block", swaps_path="/proc/swaps", mounts_path="/proc/mounts"):
    """{name: {...}} for each md array. A data array holds something (LVM, a
    filesystem) and isn't swap; UNAS keeps swap on its own md mirror."""
    try:
        names = sorted(n for n in os.listdir(sys_block) if re.fullmatch(r"md\d+", n))
    except OSError:
        return None
    swaps = read_file(swaps_path) or ""
    mounts = read_file(mounts_path) or ""
    out = {}
    for name in names:
        md = os.path.join(sys_block, name, "md")
        if not os.path.isdir(md):
            continue

        def num(f):
            v = read_file(os.path.join(md, f))
            return int(v) if v and v.isdigit() else 0
        try:
            holders = os.listdir(os.path.join(sys_block, name, "holders"))
        except OSError:
            holders = []
        dev = f"/dev/{name}"
        used = bool(holders) or re.search(rf"^{dev}\s", mounts, re.M) is not None
        out[name] = {"state": read_file(os.path.join(md, "array_state")) or "unknown",
                     "degraded": num("degraded"), "sync_action": read_file(os.path.join(md, "sync_action")),
                     "mismatch_cnt": num("mismatch_cnt"), "level": read_file(os.path.join(md, "level")) or "md",
                     "data": used and re.search(rf"^{dev}\s", swaps, re.M) is None}
    return out


def read_all():
    drives = bay_drives()
    return {"smart": read_smart(drives) if shutil.which("smartctl") else {},
            "btrfs": read_btrfs(), "md": read_md()}


def last_summary(readings, t):
    """What the Home card and `status` show about this check."""
    smart = readings["smart"]
    return {"at": t.isoformat(),
            "drives": len(smart),
            "asleep": sum(1 for r in smart.values() if r.get("asleep")),
            "unread": sorted(s for s, r in smart.items() if r.get("error")),
            "volumes": None if readings["btrfs"] is None else len({r["mount"] for r in readings["btrfs"].values()}),
            "arrays": None if readings["md"] is None else {
                n: {k: r[k] for k in ("state", "degraded", "sync_action", "level", "data")}
                for n, r in readings["md"].items()}}


# --------------------------------------------------------------------------- triggers

def verify_manifest():
    try:
        with open(MANIFEST_FILE) as f:
            lines = [ln.split() for ln in f if ln.strip()]
    except OSError:
        return ["manifest.sha256 is missing (re-run install.sh)"]
    problems = []
    for digest, name in lines:
        path = os.path.join(BASE, name)
        if not os.path.exists(path):
            problems.append(f"{name} is missing")
        elif sha256(path) != digest:
            problems.append(f"{name} changed since install")
    return problems


def render(template):
    with open(os.path.join(TEMPLATE_DIR, template)) as f:
        return f.read().replace("@BASE@", BASE)


def trigger_problems():
    problems = []
    for template, path in TRIGGERS.items():
        try:
            with open(path) as f:
                if f.read() != render(template):
                    problems.append(f"{path} differs from the copy in {TEMPLATE_DIR}")
        except FileNotFoundError:
            problems.append(f"{path} is missing")
    for what in ("is-enabled", "is-active"):
        rc, out = sh("systemctl", what, TIMER_UNIT)
        want = "enabled" if what == "is-enabled" else "active"
        if out != want:
            problems.append(f"{TIMER_UNIT} is not {want} ({out or 'unknown'})")
    return problems


def heal(dry):
    """Rewrite missing or changed trigger files and re-enable the timer. Returns what was fixed."""
    fixed, units_changed = [], False
    for template, path in TRIGGERS.items():
        want = render(template)
        try:
            with open(path) as f:
                have = f.read()
        except FileNotFoundError:
            have = None
        if have != want:
            fixed.append(f"{'restored' if have is None else 'rewrote'} {path}")
            if not dry:
                write_atomic(path, want, 0o644)
            units_changed |= path.startswith("/etc/systemd/")
    _, en = sh("systemctl", "is-enabled", TIMER_UNIT)
    _, ac = sh("systemctl", "is-active", TIMER_UNIT)
    if units_changed or en != "enabled" or ac != "active":
        if not units_changed:
            fixed.append(f"re-enabled {TIMER_UNIT}")
        if not dry:
            sh("systemctl", "daemon-reload")
            sh("systemctl", "enable", "--now", TIMER_UNIT)
    return fixed


def remove_triggers(dry):
    removed = []
    if not dry:
        sh("systemctl", "disable", "--now", TIMER_UNIT)
    for path in TRIGGERS.values():
        if os.path.exists(path):
            removed.append(path)
            if not dry:
                os.remove(path)
    if not dry:
        sh("systemctl", "daemon-reload")
    return removed


def self_check(cfg, alerts, dry):
    for e in cfg.errors:
        alerts.raise_(f"config:{e}", f"Config problem: {e}", "warning")
    own = verify_manifest()
    heal_ok = not own
    if own:
        alerts.raise_("manifest", f"Files in {BASE} don't match the install manifest: {'; '.join(own)}. "
                      "Not self-healing until install.sh is re-run.")
    else:
        alerts.clear("manifest")
        fixed = heal(dry)
        if fixed:
            alerts.info("Self-heal: " + "; ".join(fixed) + ".")
    problems = trigger_problems() if not dry else []
    if problems:
        alerts.raise_("triggers", ("Triggers still broken after self-heal: " if heal_ok
                                   else "Triggers broken (not healed): ") + "; ".join(problems))
    else:
        alerts.clear("triggers")
    missing = [x for x in REQUIRED_TOOLS if not shutil.which(x)]
    if missing:
        alerts.raise_("tools", "Missing tools on the NAS: " + ", ".join(missing)
                      + ". Not installing anything; a firmware update may have removed them.")
    else:
        alerts.clear("tools")


# --------------------------------------------------------------------------- commands

def cmd_run(cfg, args):
    dry = args.dry_run
    state = load_state()
    alerts = Alerts(cfg, state, dry)
    t = now()
    if not cfg["ENABLED"]:
        if not state.get("disabled"):
            removed = remove_triggers(dry)
            alerts.info("Turned off (ENABLED=no): removed the timer and cron line ("
                        + (", ".join(removed) or "none") + "). Turn Enabled back on in Settings to restart it.")
            state["disabled"] = True
        alerts.flush()
        save_state(state, dry)
        return 0
    state.pop("disabled", None)
    if args.source == "cron":
        last = parse_time(state.get("last_tick"))
        if last and t - last < dt.timedelta(minutes=30) and not trigger_problems():
            return 0
        log("cron backup running a full check (timer missing or late)")

    self_check(cfg, alerts, dry)
    readings = read_all()
    for r in readings["smart"].values():
        if r.get("error"):
            log(f"couldn't read SMART on {monitor.drive_name('?', r)}: {r['error']}", syslog.LOG_WARNING)
    if readings["btrfs"] is None:
        log("couldn't read btrfs device stats this time", syslog.LOG_WARNING)
    for action in monitor.sample(cfg.typed, state, readings, t):
        if action[0] == "raise":
            alerts.raise_(action[1], action[2], action[3])
        else:
            alerts.clear(action[1], action[2])
    state["last"] = last_summary(readings, t)
    state["last_tick"] = t.isoformat()
    alerts.flush()
    if not dry:
        ok_url, fail_url = heartbeat_urls(cfg)
        ping(fail_url if alerts.has_errors() else ok_url)
    save_state(state, dry)
    return 1 if alerts.has_errors() else 0


def cmd_status(cfg, args):
    state = load_state()
    last = state.get("last")
    print("health-monitor\n")
    if not cfg["ENABLED"]:
        print("Turned off (ENABLED=no).\n")
    if not last:
        print("No check yet. The timer runs one every 15 minutes.")
    else:
        t = parse_time(last["at"])
        print(f"Last check   {t:%a %d %b %H:%M}")
        asleep = f", {last['asleep']} asleep" if last.get("asleep") else ""
        print(f"Drives       {last['drives']} in bays{asleep}")
        if last.get("unread"):
            print(f"             couldn't read: {', '.join(last['unread'])}")
        vols = last.get("volumes")
        print("btrfs        " + ("couldn't read" if vols is None else f"{vols} volume(s)"))
        arrays = last.get("arrays")
        if arrays is None:
            print("Arrays       couldn't read")
        for name, a in sorted((arrays or {}).items()):
            kind = "data" if a.get("data") else "system"
            print(f"Array {name:<6} {a.get('level')} {kind}, {a.get('state')}, "
                  f"degraded {a.get('degraded')}, {a.get('sync_action')}")
    active = state.get("alerts") or {}
    print(f"\nOpen alerts: {len(active) or 'none'}")
    for key, a in sorted(active.items()):
        print(f"  [{a['level']}] {a['msg']}")
    return 0


def cmd_check(cfg, args):
    ok = True

    def line(good, what, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"  {'OK  ' if good else 'FAIL'}  {what}" + (f": {detail}" if detail else ""))

    print("Self-check\n")
    for e in cfg.errors:
        line(False, "config", e)
    own = verify_manifest()
    line(not own, "own files", "; ".join(own))
    trig = trigger_problems()
    line(not trig, "timer and cron triggers", "; ".join(trig))
    missing = [x for x in REQUIRED_TOOLS if not shutil.which(x)]
    line(not missing, "required tools", "missing " + ", ".join(missing) if missing else "")
    drives = bay_drives()
    line(bool(drives), "drives in bays", f"{len(drives)} found" if drives else "none found")
    b = read_btrfs()
    line(b is not None, "btrfs device stats", "unreadable" if b is None else f"{len(b)} device(s)")
    md = read_md()
    line(md is not None, "md arrays", "unreadable" if md is None else
         ", ".join(f"{n} ({'data' if a['data'] else 'system'})" for n, a in md.items()) or "none")
    return 0 if ok else 1


def cmd_heal(cfg, args):
    own = verify_manifest()
    if own:
        print("Not healing; own files don't match the manifest: " + "; ".join(own))
        return 1
    print("\n".join(heal(args.dry_run)) or "Triggers already OK.")
    return 0


def take_lock(wait):
    lock = open(LOCK_FILE, "w")
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("run", "status", "check", "heal"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--source", default="manual", choices=("timer", "cron", "manual"))
    args = ap.parse_args()
    if os.geteuid() != 0:
        print("Run as root.", file=sys.stderr)
        return 2
    cfg = Config(CONFIG_FILE)
    lock = None
    if args.command in ("run", "heal"):
        lock = take_lock(0 if args.source in ("timer", "cron") else 60)
        if lock is None:
            print("Another run is in progress; skipping this one.")
            return 0
    try:
        return {"run": cmd_run, "status": cmd_status, "check": cmd_check, "heal": cmd_heal}[args.command](cfg, args)
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
