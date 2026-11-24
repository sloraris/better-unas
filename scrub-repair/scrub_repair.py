#!/usr/bin/env python3
"""scrub-repair: find damaged files on the btrfs volume and swap in a
verified good copy from a backup.

Every 15 minutes (a systemd timer, with a cron line as backup) it:

  1. reads the new btrfs checksum lines in the kernel log (/dev/kmsg, from a
     saved cursor) and resolves each to a file: anything that read a damaged
     block (Jellyfin, a backup run, a copy) reveals it;
  2. runs a read-only btrfs scrub when one is due: urgent at once after new
     checksum errors or a parity mismatch, or scheduled inside its night
     window when SCRUB_SCHEDULE=yes. Scrubs pause for higher-tier jobs in
     the busy registry (core/lib/busy.py) and resume after;
  3. looks for a good copy of each damaged file (restore.py), stages it
     beside the file, and swaps it in after you confirm (MODE=stage), at
     once (MODE=auto), or leaves both steps to you (MODE=alert).

Damage outside a file (btrfs metadata) is an error alert with no action.

Usage:
  scrub_repair.py run                one check (what the timer runs)
  scrub_repair.py status             damaged files, the scrub, open alerts
  scrub_repair.py check              self-check only; non-zero exit on problems
  scrub_repair.py files              the damaged files, one per line
  scrub_repair.py find PATH|--all    look for a good copy now and stage it
  scrub_repair.py restore PATH|--all swap in the staged copy
  scrub_repair.py forget PATH        drop a file from the list (you fixed it yourself)
  scrub_repair.py scrub start|pause|resume|cancel
                                     a manual scrub (manual tier), or control the running one
  scrub_repair.py heal               restore the timer and cron line from the copies here
  scrub_repair.py install            what install.sh runs
  scrub_repair.py uninstall [--purge]

Options:
  --dry-run   change nothing: no scrub, no copy, no swap, no alerts, no state writes
  --source    who started this run (timer, cron, manual); set by the triggers

Standard library only, plus better-unas's core library (core/lib).
"""

import argparse
import datetime as dt
import hashlib
import itertools
import math
import os
import re
import sys
import syslog

BASE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(1, os.path.dirname(BASE))   # the suite home, for core.lib
import detect  # noqa: E402  (lives next to this file)
import restore  # noqa: E402
from core.lib import busy, drive, util  # noqa: E402
from core.lib.tool import Tool  # noqa: E402

NAME = "scrub-repair"
TOOL = Tool(NAME, __file__, required=("btrfs", "systemctl"))
CONFIG_FILE = TOOL.config_file

KMSG = "/dev/kmsg"
BOOT_ID = "/proc/sys/kernel/random/boot_id"
MOUNTINFO = "/proc/self/mountinfo"
SYS_BLOCK = "/sys/block"

RETRY = dt.timedelta(days=1)          # a file with no good copy is searched again after this
BUSY_AHEAD = dt.timedelta(days=31)    # how far ahead the scheduled nights are published
GUESS = dt.timedelta(hours=12)        # a scrub's length before the first one has been timed
SOON = dt.timedelta(minutes=15)       # a job that starts this soon counts as now
KEEP = 20                             # restored files and scrubs remembered for the TUI

LABEL = f"{NAME} scrub"
OPEN = ("found", "searching", "staged", "no copy", "changed", "snapshot")

now = util.now
parse_time = util.parse_time
read_file = util.read_file
load_state = TOOL.load_state
trigger_problems = TOOL.trigger_problems


def log(msg, level=syslog.LOG_INFO):
    TOOL.log(msg, level)


def sh(*cmd, timeout=60, env=None):
    return TOOL.sh(*cmd, timeout=timeout, env=env)


def Config(path, core_path=None):
    return TOOL.config(path, core_path)


def folder_sources(cfg):
    return [p.strip().rstrip("/") or "/" for p in (cfg["FOLDER_SOURCES"] or "").split(",") if p.strip()]


# --------------------------------------------------------------------------- reading the NAS

def read_kmsg(path=KMSG):
    """Every record still in the kernel ring buffer, as [(seq, message)]."""
    out = []
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        raise OSError(f"couldn't open {path}: {e.strerror or e}")
    try:
        while True:
            try:
                raw = os.read(fd, 8192)
            except BlockingIOError:
                break
            except BrokenPipeError:      # the record was overwritten while we read: skip it
                continue
            if not raw:
                break
            r = detect.parse_record(raw)
            if r:
                out.append(r)
    finally:
        os.close(fd)
    return out


def btrfs_mounts(text=None):
    """[{"mount", "fsroot", "dev"}] for each btrfs filesystem, its first
    mount. dev is the kernel's name for it (dm-0), as the log says."""
    text = read_file(MOUNTINFO) if text is None else text
    out, seen = [], set()
    for line in (text or "").splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        a, b = left.split(), right.split()
        if len(a) < 5 or len(b) < 2 or b[0] != "btrfs":
            continue
        source = b[1]
        if source in seen:
            continue
        seen.add(source)
        out.append({"mount": a[4].replace("\\040", " "), "fsroot": a[3].replace("\\040", " "),
                    "dev": os.path.basename(os.path.realpath(source))})
    return out


def subvolume_map(m):
    """{subvolume id: folder} for a mount, or (None, why not)."""
    rc, text = sh("btrfs", "subvolume", "list", m["mount"], timeout=60)
    if rc != 0:
        return None, f"btrfs subvolume list failed ({text[:120] or rc})"
    rel = detect.subvolumes(text, m["fsroot"])
    out = {sid: os.path.join(m["mount"], p) if p else m["mount"] for sid, p in rel.items()}
    rc, root = sh("btrfs", "inspect-internal", "rootid", m["mount"], timeout=30)
    if rc == 0 and root.strip().isdigit():
        out[int(root)] = m["mount"]
    if m["fsroot"] in ("/", ""):
        out.setdefault(5, m["mount"])
    return out, None


def resolve(subvols, root, ino):
    """([paths], None) for an inode in a subvolume, or ([], why not)."""
    folder = (subvols or {}).get(root)
    if folder is None:
        return [], f"subvolume {root} isn't on a mounted path"
    rc, out = sh("btrfs", "inspect-internal", "inode-resolve", str(ino), folder, timeout=60)
    if rc != 0:
        return [], "it no longer resolves to a file (deleted since?)"
    return [p for p in out.splitlines() if p.startswith("/")], None


def mount_of(path, mounts):
    best = None
    for m in mounts:
        mp = m["mount"].rstrip("/") + "/"
        if (path + "/").startswith(mp) and (best is None or len(mp) > len(best["mount"])):
            best = m
    return best


def subvol_of(path, subvols):
    """The folder of the subvolume path is in (the longest match)."""
    best = None
    for folder in (subvols or {}).values():
        f = folder.rstrip("/") + "/"
        if path.startswith(f) and (best is None or len(f) > len(best)):
            best = folder.rstrip("/")
    return best


def read_md(sys_block=SYS_BLOCK):
    """{name: {"sync_action", "last_sync_action", "mismatch_cnt", "data"}} for
    each md array (a data array is one LVM or a filesystem sits on)."""
    out = {}
    try:
        names = sorted(n for n in os.listdir(sys_block) if re.fullmatch(r"md\d+", n))
    except OSError:
        return out
    for name in names:
        md = os.path.join(sys_block, name, "md")
        if not os.path.isdir(md):
            continue
        try:
            holders = os.listdir(os.path.join(sys_block, name, "holders"))
        except OSError:
            holders = []
        cnt = read_file(os.path.join(md, "mismatch_cnt"))
        out[name] = {"sync_action": read_file(os.path.join(md, "sync_action")),
                     "last_sync_action": read_file(os.path.join(md, "last_sync_action")),
                     "mismatch_cnt": int(cnt) if cnt and cnt.isdigit() else 0, "data": bool(holders)}
    return out


def scrub_status(mount):
    rc, text = sh("btrfs", "scrub", "status", mount, timeout=30)
    return detect.parse_scrub_status(text if rc == 0 or text else "")


def readable(path, block=restore.BLOCK):
    """True when every block of path reads (it's no longer damaged)."""
    try:
        with open(path, "rb", buffering=0) as f:
            while f.read(block):
                pass
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- detection

def detect_damage(cfg, state, alerts, t, dry, records=None, boot=None, mounts=None):
    """Read the new kernel log records and add each damaged file to
    state["files"]. Returns {"new": [paths], "unresolved": n, "passive": bool}:
    passive is True when a read (not a scrub) found damage not seen before."""
    out = {"new": [], "unresolved": 0, "passive": False}
    try:
        records = read_kmsg() if records is None else records
    except OSError as e:
        alerts.raise_("kmsg", f"Can't read the kernel log: {e}. Damaged files won't be noticed as they're read.",
                      "warning")
        return out
    alerts.clear("kmsg")
    boot = (read_file(BOOT_ID) or "") if boot is None else boot
    new, cursor = detect.new_records(records, state.get("kmsg"), boot)
    state["kmsg"] = cursor
    reports = detect.damage(new)
    if not reports:
        return out
    mounts = btrfs_mounts() if mounts is None else mounts
    maps = {}
    files = state.setdefault("files", {})
    for _, r in reports:
        m = next((x for x in mounts if x["dev"] == r["dev"]), mounts[0] if len(mounts) == 1 else None)
        if r["how"] == "metadata":
            alerts.raise_(f"metadata:{r['dev']}",
                          f"btrfs metadata damage on {r['dev']}: {r['text']}. scrub-repair can't restore metadata; "
                          "check the volume in UniFi Drive and your backups.", "error")
            continue
        if m is None:
            out["unresolved"] += 1
            alerts.raise_(f"inode:{r['root']}:{r['ino']}",
                          f"A damaged block on {r['dev']} (subvolume {r['root']}, inode {r['ino']}) is on no mounted "
                          "btrfs volume.", "warning")
            continue
        if m["mount"] not in maps:
            maps[m["mount"]] = subvolume_map(m)
        subvols, err = maps[m["mount"]]
        paths, why = resolve(subvols, r["root"], r["ino"]) if subvols is not None else ([], err)
        if not paths:
            out["unresolved"] += 1
            key = f"{r['root']}:{r['ino']}"
            alerts.raise_(f"inode:{key}",
                          f"A damaged block (subvolume {r['root']}, inode {r['ino']}, found by a {r['how']}) "
                          f"couldn't be matched to a file: {why}.", "warning")
            seen = state.setdefault("seen_inodes", [])
            if key not in seen:
                seen.append(key)
                del seen[:-200]
                if r["how"] == "read":
                    out["passive"] = True
            continue
        for path in paths:
            if path in files and files[path].get("status") in OPEN:
                continue
            files[path] = record(path, m, subvols, r, t)
            out["new"].append(path)
            if r["how"] == "read":
                out["passive"] = True
            log(f"damaged file found by a {r['how']}: {path}", syslog.LOG_WARNING)
            file_alert(alerts, cfg, files[path])
    return out


def record(path, m, subvols, r, t):
    rel = os.path.relpath(path, m["mount"])
    share, inner = detect.share_of(rel)
    try:
        fp = restore.fingerprint(path)
    except OSError:
        fp = None
    return {"path": path, "mount": m["mount"], "subvol": subvol_of(path, subvols) or m["mount"],
            "share": share, "rel": inner, "how": r["how"], "found": t.isoformat(), "root": r["root"],
            "ino": r["ino"], "fp": fp, "status": "snapshot" if detect.is_snapshot(rel) else "found",
            "looked": [], "copy": None, "check": None, "last_try": None, "staged": None}


# --------------------------------------------------------------------------- alerts

def file_alert(alerts, cfg, rec):
    """Open, refresh or clear the alert for one file from its status."""
    key, path = f"file:{rec['path']}", rec["path"]
    s = rec["status"]
    if s == "restored":
        alerts.clear(key, f"Restored {path} from {rec['copy']['label']}.")
    elif s == "gone":
        alerts.clear(key, f"Damaged file {path} is gone (deleted or moved); nothing to restore.")
    elif s == "readable":
        alerts.clear(key, f"{path} reads without errors now; it was replaced or fixed outside scrub-repair.")
    elif s == "forgotten":
        alerts.clear(key, f"Stopped tracking damaged file {path} (forgotten by hand).")
    elif s == "snapshot":
        alerts.raise_(key, f"Damaged file in a snapshot: {path}. Snapshots are read-only and share blocks with the "
                           "live file, so it can't be restored there; the live file is checked when it's read.",
                      "warning")
    elif s == "staged":
        weak = "" if (rec.get("check") or {}).get("compared") else " (no block of the damaged file could be read to compare)"
        alerts.raise_(key, f"Damaged file: {path}. A verified copy from {rec['copy']['label']} is staged{weak}; "
                           "restore it in the Damaged files screen or with `better-unas scrub-repair restore`.")
    elif s == "no copy":
        where = "; ".join(rec.get("looked") or []) or "no copy anywhere it looked"
        alerts.raise_(key, f"Damaged file: {path}. No good copy found ({where}). Retrying daily, and after the next "
                           "backup run or a change to the folder sources.")
    elif s == "error":
        alerts.raise_(key, f"Damaged file: {path}. {rec.get('error') or 'Restoring it failed'}.")
    else:
        hint = ("Find a copy in the Damaged files screen or with `better-unas scrub-repair find`."
                if cfg["MODE"] == "alert" else "Looking for a good copy.")
        alerts.raise_(key, f"Damaged file: {path} (found by a {rec['how']}). {hint}")


# --------------------------------------------------------------------------- the scrub

def scrub_state(state):
    return state.setdefault("scrub", {})


def blocked(others, tier, t, until, md):
    """Why a `tier` scrub can't run from t to until, or None: another tool's
    window at its tier or above, or an md parity check or rebuild (UniFi's
    own, so fixed)."""
    for name, a in sorted((md or {}).items()):
        if a.get("data") and a.get("sync_action") not in (None, "idle", "frozen"):
            return f"UniFi's {a['sync_action']} of {name} is running"
    hits = busy.overlapping(busy.blocking(others, tier), t, min(until, t + SOON))
    return f"{hits[0].label} ({hits[0].tier}) needs the disks" if hits else None


def estimate(job, status, t, cfg):
    """When the running scrub should end."""
    if status.get("left"):
        end = t + status["left"]
    elif job.get("duration"):
        end = t + dt.timedelta(seconds=job["duration"])
    else:
        end = t + GUESS
    if job["tier"] == "scheduled":
        w = detect.current_window(t, cfg["SCRUB_WINDOW"])
        if w:
            end = min(end, w[1])
    return end


def btrfs_scrub(action, mount, dry):
    """start / resume (read-only) / cancel. Returns None or why it failed."""
    cmd = {"start": ("btrfs", "scrub", "start", "-r", mount), "resume": ("btrfs", "scrub", "resume", "-r", mount),
           "cancel": ("btrfs", "scrub", "cancel", mount)}[action]
    if dry:
        print("(dry-run) would run: " + " ".join(cmd))
        return None
    rc, out = sh(*cmd, timeout=60)
    if rc != 0 and not (action == "cancel" and "not running" in out):
        return f"`{' '.join(cmd)}` failed: {out[:160] or 'exit ' + str(rc)}"
    return None


def want_scrub(state, tier, reason):
    """Ask for a scrub; a higher tier replaces a lower one already asked for or running."""
    s = scrub_state(state)
    job = s.get("job")
    if job:
        if busy.rank(tier) > busy.rank(job["tier"]):
            job["tier"], job["reason"] = tier, reason
        return
    w = s.get("want")
    if not w or busy.rank(tier) > busy.rank(w["tier"]):
        s["want"] = {"tier": tier, "reason": reason}


def manage_scrub(cfg, state, alerts, t, dry, mounts=None, others=None, md=None):
    """Start, pause, resume or finish the scrub. Returns the busy windows to
    publish for it."""
    s = scrub_state(state)
    mounts = btrfs_mounts() if mounts is None else mounts
    others = TOOL.others_busy(t) if others is None else others
    md = read_md() if md is None else md
    if not mounts:
        return []
    job = s.get("job")
    if not job:
        want = s.pop("want", None)
        if not want and cfg["SCRUB_SCHEDULE"] and detect.scheduled_due(
                t, cfg["SCRUB_FREQUENCY"], cfg["SCRUB_START_DAY"], parse_time(s.get("last_done"))) \
                and detect.current_window(t, cfg["SCRUB_WINDOW"]):
            want = {"tier": "scheduled", "reason": "the scheduled scrub"}
        if want:
            job = s["job"] = {"tier": want["tier"], "reason": want["reason"], "todo": [m["mount"] for m in mounts],
                              "started": None, "paused": None, "errors": 0, "began": t.isoformat(),
                              "duration": (s.get("history") or [{}])[-1].get("seconds")}
    if not job:
        out = planned_windows(cfg, s, t)
        for m in mounts:                         # a scrub someone started by hand
            st = scrub_status(m["mount"])
            if st["state"] == "running":
                out.append((t, t + (st.get("left") or GUESS), f"btrfs scrub of {m['mount']} (started by hand)",
                            "running", "manual"))
        return out

    mount = job["todo"][0]
    status = scrub_status(mount)
    if status["state"] == "running" and not job.get("started"):
        job["started"] = t.isoformat()          # already running (started by hand): carry on with it
    if job.get("hold"):                          # paused by hand: waits for `scrub resume`
        return windows_for(cfg, s, job, t, estimate(job, status, t, cfg), paused=True)
    end = estimate(job, status, t, cfg)
    in_window = job["tier"] != "scheduled" or detect.current_window(t, cfg["SCRUB_WINDOW"]) is not None
    why_not = blocked(others, job["tier"], t, end, md)
    if status["state"] == "running" and job.get("started"):
        mine = [busy.Busy(t, end, LABEL, "running", NAME, job["tier"])]
        yield_to = busy.must_yield(mine, others, t)
        stop = (f"make way for {yield_to[0][1].label} ({yield_to[0][1].tier})" if yield_to
                else None if in_window else "its night window ended")
        if stop:
            err = btrfs_scrub("cancel", mount, dry)
            if err:
                alerts.raise_("scrub:control", f"Couldn't pause the scrub of {mount}: {err}.", "warning")
            else:
                alerts.clear("scrub:control")
                job["paused"] = stop
                job["progress"] = status.get("percent")
                if yield_to:
                    alerts.info(f"Paused the {job['tier']} scrub of {mount} at {pct(status)} to {stop}; it resumes "
                                "after.")
                log(f"paused the scrub of {mount}: {stop}")
            return windows_for(cfg, s, job, t, end, paused=True)
        job["progress"] = status.get("percent")
        return windows_for(cfg, s, job, t, end)

    if status["state"] == "finished" and job.get("started"):
        job["errors"] += status.get("errors") or 0
        job["todo"].pop(0)
        job["started"], job["paused"] = None, None
        if job["todo"]:
            return manage_scrub(cfg, state, alerts, t, dry, mounts, others, md)
        finish(s, job, t, alerts)
        return planned_windows(cfg, s, t)

    # Not running: never started, paused by us, cancelled or stopped by a reboot.
    if not in_window:
        return windows_for(cfg, s, job, t, end, paused=True)
    if why_not:
        if job.get("paused") != why_not:
            log(f"scrub of {mount} waiting: {why_not}")
        job["paused"] = why_not
        return windows_for(cfg, s, job, t, end, paused=True)
    action = "resume" if job.get("started") and status["state"] in ("aborted", "interrupted") else "start"
    err = btrfs_scrub(action, mount, dry)
    if err and action == "resume":      # nothing to resume (stats gone): start over
        action, err = "start", btrfs_scrub("start", mount, dry)
    if err:
        alerts.raise_("scrub:control", f"Couldn't {action} the scrub of {mount}: {err}.", "warning")
        return windows_for(cfg, s, job, t, end, paused=True)
    alerts.clear("scrub:control")
    if busy.rank(job["tier"]) > busy.rank("scheduled"):
        TOOL.nudge(busy.in_the_way(others, t, end, job["tier"]), dry)
    if not job.get("started"):
        job["started"] = t.isoformat()
        log(f"started a {job['tier']} read-only scrub of {mount} ({job['reason']})")
        if job["tier"] != "scheduled":
            alerts.info(f"Started a read-only scrub of {mount} ({job['reason']}). Any damaged file it finds is "
                        "listed in Damaged files.")
    else:
        log(f"resumed the scrub of {mount}")
    job["paused"] = None
    return windows_for(cfg, s, job, t, end)


def finish(s, job, t, alerts):
    began = parse_time(job.get("began")) or t
    s["last_done"] = t.isoformat()
    s["job"] = None
    s.setdefault("history", []).append({"began": began.isoformat(), "done": t.isoformat(), "tier": job["tier"],
                                        "reason": job["reason"], "errors": job["errors"],
                                        "seconds": int((t - began).total_seconds())})
    del s["history"][:-KEEP]
    found = f"{job['errors']} checksum error(s); the damaged files are in Damaged files" if job["errors"] \
        else "no errors"
    alerts.info(f"Scrub finished ({job['reason']}): {found}.")
    log(f"scrub finished: {found}")


def pct(status):
    return f"{status['percent']:.0f}%" if status.get("percent") is not None else "an unknown point"


def nights_needed(cfg, s):
    """How many night windows a scheduled scrub takes, from the last one timed."""
    a, b = detect.window_on(dt.date(2000, 1, 3), cfg["SCRUB_WINDOW"], dt.timezone.utc)
    hours = (b - a).total_seconds()
    timed = [h["seconds"] for h in s.get("history") or [] if h.get("seconds")]
    return max(1, min(14, math.ceil(timed[-1] / hours))) if timed else max(1, math.ceil(GUESS.total_seconds() / hours))


def planned_windows(cfg, s, t):
    if not cfg["SCRUB_SCHEDULE"]:
        return []
    return [(a, b, f"{NAME} scheduled scrub", "planned", "scheduled")
            for a, b in detect.scheduled_nights(t, t + BUSY_AHEAD, cfg["SCRUB_FREQUENCY"], cfg["SCRUB_START_DAY"],
                                                cfg["SCRUB_WINDOW"], parse_time(s.get("last_done")),
                                                nights_needed(cfg, s))]


def windows_for(cfg, s, job, t, end, paused=False):
    """The running (or waiting) scrub, then the rest of a scheduled one's nights."""
    out = []
    if job["tier"] == "scheduled":
        nights = detect.scheduled_nights(t, t + BUSY_AHEAD, cfg["SCRUB_FREQUENCY"], cfg["SCRUB_START_DAY"],
                                         cfg["SCRUB_WINDOW"], None, nights_needed(cfg, s))
        if not paused:
            out.append((t, end, LABEL, "running", "scheduled"))
            nights = [w for w in nights if w[0] >= end]
        out += [(a, b, f"{NAME} scheduled scrub", "planned", "scheduled") for a, b in nights[:nights_needed(cfg, s)]]
    else:
        out.append((t, end, LABEL, "planned" if paused else "running", job["tier"]))
    return out


# --------------------------------------------------------------------------- finding and swapping copies

def sources_signature(cfg):
    return f"{cfg['USE_UNIFI_BACKUPS']}|{','.join(folder_sources(cfg))}"


def unifi_source(cfg):
    """A restore.UnifiCloud for the UniFi Cloud backup tasks, or (None, why not)."""
    if not cfg["USE_UNIFI_BACKUPS"]:
        return None, None
    rows, err = drive.psql(sh, "unifi-drive", "SELECT id::text FROM remote_backup_tasks "
                                              "WHERE deleted_at IS NULL AND remote_type = 'ucs' ORDER BY id")
    if err:
        return None, f"couldn't read UniFi Drive's backup tasks: {err}"
    tasks = [(r[0], "UniFi Cloud backup") for r in rows if r and drive.UUID_RE.fullmatch(r[0])]
    if not tasks:
        return None, None
    user, err = drive.owner_id(sh)
    if not user:
        return None, err

    def api(method, path):
        return drive.api(method, path, user)

    def download(path, dest):
        return drive.download(path, dest, user, timeout=600)
    return restore.UnifiCloud(api, download, tasks, parse_time), None


def backups_finished():
    """When UniFi Drive last finished any backup run (epoch seconds), or None.
    Only the run table's task id, finish time and status are read."""
    rows, err = drive.psql(sh, "unifi-drive", "SELECT extract(epoch from max(finished_at))::text "
                                              "FROM remote_backup_task_runs")
    if err or not rows or not rows[0] or not rows[0][0]:
        return None
    try:
        return float(rows[0][0])
    except ValueError:
        return None


def token(path):
    return hashlib.sha1(path.encode()).hexdigest()[:16]


def search(cfg, rec, t, dry, unifi=None, tried=None):
    """Look for a good copy of one file and stage it. Updates rec."""
    path = rec["path"]
    if rec["status"] == "snapshot":
        return rec
    if not os.path.exists(path):
        rec["status"] = "gone"
        return rec
    sources = []
    if unifi is not None and rec.get("share") and rec.get("rel"):
        sources.append(unifi.candidates(rec["share"], rec["rel"]))
    if rec.get("rel"):
        sources.append(restore.folder_candidates(folder_sources(cfg), rec.get("share"), rec["rel"]))
    elif not rec.get("share"):
        sources.append(restore.folder_candidates(folder_sources(cfg), None,
                                                 os.path.relpath(path, rec["mount"])))
    if dry:
        print(f"(dry-run) would look for a copy of {path}")
        return rec
    dest = restore.stage_path(rec["subvol"], path, token(path))
    result = restore.find_copy(path, itertools.chain(*sources), dest, tried=tried)
    rec["last_try"] = t.isoformat()
    rec["sources"] = sources_signature(cfg)
    rec["looked"] = result["looked"] + ([] if sources else ["no sources are set"])
    if unifi is not None and unifi.problems:
        rec["looked"] += sorted(set(unifi.problems))
    rec["status"] = result["status"]
    if result["status"] == "staged":
        rec["copy"], rec["check"], rec["staged"] = result["copy"], result["check"], dest
        log(f"staged a verified copy of {path} from {result['copy']['label']}")
    else:
        rec["copy"], rec["check"], rec["staged"] = None, None, None
    return rec


def do_swap(rec, t, dry):
    """Swap the staged copy in. Updates rec; returns None or why not."""
    path = rec["path"]
    if dry:
        print(f"(dry-run) would rename {rec['staged']} over {path}")
        return None
    err = restore.swap(path, rec["staged"], rec["fp"], (rec.get("copy") or {}).get("mtime"))
    if err is None:
        restore.tidy(rec["staged"])                 # the copy was renamed away: only the empty folder is left
        rec["status"], rec["restored"] = "restored", t.isoformat()
        log(f"restored {path} from {rec['copy']['label']}")
        return None
    log(f"didn't restore {path}: {err}", syslog.LOG_WARNING)
    if err == "the damaged file is gone":
        rec["status"] = "gone"
    elif err == "the file changed since it was found":
        rec["status"] = "changed"
    else:
        rec["status"], rec["error"] = "error", f"Restoring it failed: {err}"
    return err


def recheck_changed(rec):
    """A file that changed since it was found: readable now, or still damaged
    (then it's searched again with its new fingerprint)."""
    path = rec["path"]
    if not os.path.exists(path):
        rec["status"] = "gone"
    elif readable(path):
        rec["status"] = "readable"
    else:
        rec["fp"] = restore.fingerprint(path)
        rec["status"] = "found"
    _drop_staged(rec)


def _drop_staged(rec):
    restore.tidy(rec.get("staged"))
    rec["staged"] = None


def retry_due(cfg, rec, t, finished):
    last = parse_time(rec.get("last_try"))
    return (last is None or t - last >= RETRY or rec.get("sources") != sources_signature(cfg)
            or (finished is not None and finished > last.timestamp()))


def repair(cfg, state, alerts, t, dry):
    """Search for copies and swap them in as MODE says."""
    files = state.get("files") or {}
    todo = [r for r in files.values() if r["status"] in ("found", "no copy", "staged", "changed")]
    if not todo:
        return
    finished = backups_finished() if any(r["status"] == "no copy" for r in todo) else None
    unifi, tried = None, set()
    unifi_ready = False
    for rec in sorted(todo, key=lambda r: r["found"]):
        before = (rec["status"], (rec.get("copy") or {}).get("label"))
        if rec["status"] == "changed":
            recheck_changed(rec)
        if rec["status"] == "staged" and not os.path.isfile(rec.get("staged") or ""):
            rec["status"] = "found"
        wants = (rec["status"] == "found" and cfg["MODE"] != "alert") or \
                (rec["status"] == "no copy" and retry_due(cfg, rec, t, finished))
        if wants:
            if not unifi_ready:
                unifi, why = unifi_source(cfg)
                unifi_ready = True
                if why:
                    alerts.raise_("sources:unifi", f"Can't look in UniFi's backups: {why}.", "warning")
                else:
                    alerts.clear("sources:unifi")
            search(cfg, rec, t, dry, unifi, tried)
        if rec["status"] == "staged" and cfg["MODE"] == "auto" and (rec.get("check") or {}).get("compared"):
            do_swap(rec, t, dry)
            if rec["status"] == "changed":
                recheck_changed(rec)
        if (rec["status"], (rec.get("copy") or {}).get("label")) != before or rec["status"] in ("no copy",):
            file_alert(alerts, cfg, rec)
        if rec["status"] in ("restored", "gone", "readable"):
            settle(state, rec)


def settle(state, rec):
    """Move a file that's done (restored, gone, readable again, forgotten) from
    the open list to the recent list."""
    state.get("files", {}).pop(rec["path"], None)
    _drop_staged(rec)
    recent = state.setdefault("restored", [])
    recent.append({"path": rec["path"], "status": rec["status"], "at": rec.get("restored") or util.now().isoformat(),
                   "found": rec["found"], "how": rec["how"], "label": (rec.get("copy") or {}).get("label")})
    del recent[:-KEEP]


def check_open_files(cfg, state, alerts):
    """Files deleted or moved since they were found stop being tracked."""
    for rec in list((state.get("files") or {}).values()):
        if not os.path.lexists(rec["path"]):
            rec["status"] = "gone"
            file_alert(alerts, cfg, rec)
            settle(state, rec)


# --------------------------------------------------------------------------- the run

def run_once(cfg, state, alerts, t, dry):
    """One check, after the self-check. Returns the busy windows to publish."""
    found = detect_damage(cfg, state, alerts, t, dry)
    md = read_md()
    seen = state.setdefault("md_seen", {})
    if cfg["URGENT_SCRUB"]:
        if found["passive"]:
            want_scrub(state, "urgent", "checksum errors in the kernel log")
        for name, n in detect.parity_mismatch(md, seen):
            want_scrub(state, "urgent", f"UniFi's parity check of {name} found {n} mismatches")
    for name, a in md.items():
        if a.get("sync_action") in ("idle", None) and a.get("last_sync_action") == "check":
            seen[name] = a.get("mismatch_cnt") or 0
    windows = manage_scrub(cfg, state, alerts, t, dry, md=md)
    check_open_files(cfg, state, alerts)
    repair(cfg, state, alerts, t, dry)
    return windows


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
    windows = run_once(cfg, state, alerts, t, dry)
    TOOL.publish_busy(windows, t, dry)
    state["last_tick"] = t.isoformat()
    alerts.flush()
    TOOL.heartbeat(cfg, alerts, dry)
    TOOL.save_state(state, dry)
    return 1 if alerts.has_errors() else 0


# --------------------------------------------------------------------------- commands by hand

def pick(state, target):
    """The open file records a PATH or --all names."""
    files = state.get("files") or {}
    if target == "--all":
        return list(files.values())
    path = os.path.abspath(target)
    return [files[path]] if path in files else []


def cmd_find(cfg, args, state=None, t=None):
    """Look for a good copy now, whatever the mode."""
    target = (args.args or [None])[0]
    if not target:
        print("Name a damaged file's path, or --all.")
        return 2
    state = load_state() if state is None else state
    alerts = TOOL.alerts(cfg, state, args.dry_run)
    t = now() if t is None else t
    recs = [r for r in pick(state, target) if r["status"] in ("found", "no copy", "staged", "changed")]
    if not recs:
        print("No such damaged file waiting for a copy." if target != "--all" else "No damaged files waiting.")
        return 1
    unifi, why = unifi_source(cfg)
    if why:
        print(f"Not looking in UniFi's backups: {why}")
    tried, lines = set(), []
    for rec in recs:
        if rec["status"] == "changed":
            recheck_changed(rec)
        if rec["status"] in ("found", "no copy", "staged"):
            _drop_staged(rec)
            search(cfg, rec, t, args.dry_run, unifi, tried)
        lines.append(f"{rec['path']}: {describe(rec)}")
        file_alert(alerts, cfg, rec)
        if rec["status"] in ("gone", "readable"):
            settle(state, rec)
    alerts.flush()
    TOOL.save_state(state, args.dry_run)
    print("\n".join(lines))
    return 0 if all(r["status"] in ("staged", "gone", "readable") for r in recs) else 1


def cmd_restore(cfg, args, state=None, t=None):
    """Swap in staged copies."""
    target = (args.args or [None])[0]
    if not target:
        print("Name a damaged file's path, or --all.")
        return 2
    state = load_state() if state is None else state
    alerts = TOOL.alerts(cfg, state, args.dry_run)
    t = now() if t is None else t
    recs = [r for r in pick(state, target) if r["status"] == "staged"]
    if not recs:
        print("No staged copy for that file; run `find` first." if target != "--all" else "No staged copies.")
        return 1
    ok, lines = True, []
    for rec in recs:
        err = do_swap(rec, t, args.dry_run)
        if rec["status"] == "changed":
            recheck_changed(rec)
        lines.append(f"{rec['path']}: " + ("restored" if err is None else f"not restored: {err}"))
        ok = ok and err is None
        if not args.dry_run:
            file_alert(alerts, cfg, rec)
            if rec["status"] in ("restored", "gone", "readable"):
                settle(state, rec)
    alerts.flush()
    TOOL.save_state(state, args.dry_run)
    if len(lines) > 1:
        lines.append(f"Restored {sum(1 for ln in lines if ln.endswith(': restored'))} of {len(lines)} files.")
    print("\n".join(lines))
    return 0 if ok else 1


def cmd_forget(cfg, args, state=None):
    target = (args.args or [None])[0]
    state = load_state() if state is None else state
    recs = pick(state, target) if target and target != "--all" else []
    if not recs:
        print("Name one damaged file's path.")
        return 2
    alerts = TOOL.alerts(cfg, state, args.dry_run)
    rec = recs[0]
    rec["status"] = "forgotten"
    file_alert(alerts, cfg, rec)
    settle(state, rec)
    alerts.flush()
    TOOL.save_state(state, args.dry_run)
    print(f"Stopped tracking {rec['path']}.")
    return 0


def cmd_scrub(cfg, args, state=None, t=None):
    """start: a manual scrub now; pause/resume/cancel: the running one."""
    what = (args.args or ["status"])[0]
    state = load_state() if state is None else state
    alerts = TOOL.alerts(cfg, state, args.dry_run)
    t = now() if t is None else t
    s = scrub_state(state)
    job = s.get("job")
    if what == "start":
        want_scrub(state, "manual", "started by hand")
        if job:
            job["tier"], job["reason"] = "manual", "started by hand"
            job["paused"] = None
    elif what in ("pause", "cancel"):
        if not job:
            print("No scrub is running or waiting.")
            return 1
        for mount in job["todo"][:1]:
            err = btrfs_scrub("cancel", mount, args.dry_run)
            if err:
                print(err)
                return 1
        if what == "cancel":
            s["job"] = None
            print("Cancelled the scrub.")
        else:
            job["tier"], job["paused"] = "manual", "paused by hand"
            job["hold"] = True
            print("Paused the scrub. `scrub resume` carries on from where it stopped.")
        alerts.flush()
        TOOL.save_state(state, args.dry_run)
        return 0
    elif what == "resume":
        if not job:
            print("No scrub to resume.")
            return 1
        job.pop("hold", None)
        job["tier"], job["paused"] = "manual", None
    elif what == "status":
        print(scrub_line(s))
        return 0
    else:
        print("Use: scrub start | pause | resume | cancel | status")
        return 2
    windows = manage_scrub(cfg, state, alerts, t, args.dry_run)
    TOOL.publish_busy(windows, t, args.dry_run)
    alerts.flush()
    TOOL.save_state(state, args.dry_run)
    print(scrub_line(s) if s.get("job") else "Nothing to scrub.")
    return 0


def describe(rec):
    s = rec["status"]
    if s == "staged":
        return f"verified copy staged, from {rec['copy']['label']}"
    if s == "no copy":
        return "no good copy found" + (f" ({'; '.join(rec['looked'])})" if rec.get("looked") else "")
    if s == "found":
        return "no copy looked for yet"
    return {"snapshot": "in a snapshot (can't be restored)", "gone": "gone (deleted or moved)",
            "readable": "reads fine now", "restored": "restored", "changed": "changed since it was found",
            "error": rec.get("error") or "error"}.get(s, s)


def scrub_line(s):
    job = s.get("job")
    if not job:
        done = parse_time(s.get("last_done"))
        return f"last finished {done:%a %d %b %Y}" if done else "never run here"
    p = job.get("progress")
    at = f", {p:.0f}% done" if p is not None else ""
    if job.get("paused"):
        return f"{job['tier']} scrub paused{at}: {job['paused']}"
    if job.get("started"):
        return f"{job['tier']} scrub running{at} ({job['reason']})"
    return f"{job['tier']} scrub waiting to start ({job['reason']})"


def next_scheduled(cfg, s, t):
    """The start of the next scheduled scrub night, or None."""
    if not cfg["SCRUB_SCHEDULE"]:
        return None
    nights = detect.scheduled_nights(t, t + dt.timedelta(days=400), cfg["SCRUB_FREQUENCY"], cfg["SCRUB_START_DAY"],
                                     cfg["SCRUB_WINDOW"], parse_time(s.get("last_done")))
    return nights[0][0] if nights else None


def cmd_status(cfg, args):
    state = load_state()
    t = now()
    print("scrub-repair\n")
    if not cfg["ENABLED"]:
        print("Turned off (ENABLED=no).\n")
    last = parse_time(state.get("last_tick"))
    print(f"Last check   {last:%a %d %b %H:%M}" if last else "No check yet. The timer runs one every 15 minutes.")
    print(f"Mode         {cfg['MODE']}")
    s = state.get("scrub") or {}
    print(f"Scrub        {scrub_line(s)}")
    nxt = next_scheduled(cfg, s, t)
    print(f"Next         {nxt:%a %d %b %H:%M} (scheduled)" if nxt else "Next         none scheduled (SCRUB_SCHEDULE=no)")
    files = state.get("files") or {}
    print(f"\nDamaged files: {len(files) or 'none'}")
    for path, rec in sorted(files.items()):
        print(f"  {path}\n      {describe(rec)}")
    active = state.get("alerts") or {}
    others = {k: a for k, a in active.items() if not k.startswith("file:")}
    print(f"\nOther open alerts: {len(others) or 'none'}")
    for key, a in sorted(others.items()):
        print(f"  [{a['level']}] {a['msg']}")
    return 0


def cmd_files(cfg, args):
    for path, rec in sorted((load_state().get("files") or {}).items()):
        print(f"{rec['status']}\t{path}")
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
    try:
        n = len(read_kmsg())
        line(True, "kernel log", f"{n} records readable")
    except OSError as e:
        line(False, "kernel log", str(e))
    mounts = btrfs_mounts()
    line(bool(mounts), "btrfs volumes", ", ".join(m["mount"] for m in mounts) or "none found")
    for m in mounts:
        subvols, err = subvolume_map(m)
        line(subvols is not None, f"subvolumes of {m['mount']}", err or f"{len(subvols)} found")
    for folder in folder_sources(cfg):
        line(os.path.isdir(folder), f"folder source {folder}", "" if os.path.isdir(folder) else "not a folder")
    if cfg["USE_UNIFI_BACKUPS"]:
        unifi, why = unifi_source(cfg)
        line(why is None, "UniFi Cloud backups", why or (f"{len(unifi.tasks)} task(s)" if unifi else "no tasks"))
    return 0 if ok else 1


def cmd_heal(cfg, args):
    return TOOL.cmd_heal(args.dry_run)


def cmd_install(args):
    return TOOL.install(
        lambda: cmd_check(Config(CONFIG_FILE), args),
        after=(lambda: cmd_status(Config(CONFIG_FILE), args),),
        done=("Installed. It reads the kernel log every 15 minutes.",
              "See damaged files any time:  better-unas scrub-repair status",
              "Logs:                        journalctl -t scrub-repair"))


COMMANDS = {"run": cmd_run, "status": cmd_status, "check": cmd_check, "heal": cmd_heal, "files": cmd_files,
            "find": cmd_find, "restore": cmd_restore, "forget": cmd_forget, "scrub": cmd_scrub}
LOCKED = ("run", "heal", "find", "restore", "forget", "scrub")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=tuple(COMMANDS) + ("install", "uninstall"))
    ap.add_argument("args", nargs="*")
    ap.add_argument("--all", action="store_true", help="find or restore every damaged file")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--purge", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--source", default="manual", choices=("timer", "cron", "manual"))
    args = ap.parse_args()
    if args.all:
        args.args = ["--all"]
    if args.command == "install":
        return cmd_install(args)
    if args.command == "uninstall":
        return TOOL.uninstall(args.purge)
    if os.geteuid() != 0:
        print("Run as root.", file=sys.stderr)
        return 2
    cfg = Config(CONFIG_FILE)
    lock, held = TOOL.lock_for(args.command, args.source, LOCKED, 60)
    if held:
        print("Another run is in progress; skipping this one." if args.source != "manual"
              else "Another run is in progress (it may be copying a file); try again in a minute.")
        return 0 if args.source != "manual" else 1
    try:
        return COMMANDS[args.command](cfg, args)
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
