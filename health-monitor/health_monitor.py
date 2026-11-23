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
  health_monitor.py install  what install.sh runs: config, manifest, triggers, first check
  health_monitor.py uninstall [--purge]
                             what uninstall.sh runs: remove the triggers (--purge: the folder too)

Options:
  --dry-run   change nothing: no healing, no alerts, no state writes
  --source    who started this run (timer, cron, manual); set by the triggers

Standard library only, plus better-unas's core library (core/lib); nothing is
installed on the NAS.
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import syslog
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(1, os.path.dirname(BASE))   # the suite home, for core.lib
import monitor  # noqa: E402  (lives next to this file)
from core.lib import notify, util  # noqa: E402
from core.lib.tool import Tool  # noqa: E402

NAME = "health-monitor"
API = "http://127.0.0.1:16080/api/v2"
TOOL = Tool(NAME, __file__, required=("smartctl", "lsblk", "systemctl"))
CONFIG_FILE = TOOL.config_file

now = util.now
parse_time = util.parse_time
read_file = util.read_file
heartbeat_urls = notify.heartbeat_urls
load_state = TOOL.load_state
trigger_problems = TOOL.trigger_problems


def log(msg, level=syslog.LOG_INFO):
    TOOL.log(msg, level)


def sh(*cmd, timeout=60):
    return TOOL.sh(*cmd, timeout=timeout)


def Config(path, core_path=None):
    return TOOL.config(path, core_path)


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


# --------------------------------------------------------------------------- commands

def cmd_run(cfg, args):
    dry = args.dry_run
    state = load_state()
    alerts = TOOL.alerts(cfg, state, dry)
    t = now()
    if TOOL.turned_off(cfg, state, alerts, dry):
        alerts.flush()
        TOOL.save_state(state, dry)
        return 0
    if args.source == "cron":
        if TOOL.cron_can_skip(state, t, dt.timedelta(minutes=30)):
            return 0
        log("cron backup running a full check (timer missing or late)")

    TOOL.self_check(cfg, alerts, dry)
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
    TOOL.heartbeat(cfg, alerts, dry)
    TOOL.save_state(state, dry)
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
    own = TOOL.verify_manifest()
    line(not own, "own files", "; ".join(own))
    trig = trigger_problems()
    line(not trig, "timer and cron triggers", "; ".join(trig))
    missing = TOOL.missing_tools()
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
    return TOOL.cmd_heal(args.dry_run)


def cmd_install(args):
    def first_run():
        # The first check records every counter as it stands and warns only
        # about drives already close to failing.
        TOOL.sh(sys.executable, os.path.join(BASE, TOOL.script), "run", "--source", "manual", timeout=600)
    return TOOL.install(
        lambda: cmd_check(Config(CONFIG_FILE), args),
        after=(first_run, lambda: cmd_status(Config(CONFIG_FILE), args)),
        done=("Installed. It checks every 15 minutes.",
              "See the last check any time:  better-unas health-monitor status",
              "Logs:                         journalctl -t health-monitor"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("run", "status", "check", "heal", "install", "uninstall"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--purge", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--source", default="manual", choices=("timer", "cron", "manual"))
    args = ap.parse_args()
    if args.command == "install":
        return cmd_install(args)
    if args.command == "uninstall":
        return TOOL.uninstall(args.purge)
    if os.geteuid() != 0:
        print("Run as root.", file=sys.stderr)
        return 2
    cfg = Config(CONFIG_FILE)
    lock, busy = TOOL.lock_for(args.command, args.source, ("run", "heal"), 60)
    if busy:
        print("Another run is in progress; skipping this one.")
        return 0
    try:
        return {"run": cmd_run, "status": cmd_status, "check": cmd_check, "heal": cmd_heal}[args.command](cfg, args)
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
