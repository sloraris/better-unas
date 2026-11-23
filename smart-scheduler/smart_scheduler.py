#!/usr/bin/python3
"""smart-scheduler: schedule UniFi Drive's own extended SMART tests on a UNAS.

Tests are started through UniFi Drive's local API, so they appear in
Storage > Health Monitor and trigger UniFi's own notifications.

Commands:
  run      the hourly tick: self-check, self-heal, report results, maybe start a test
  status   one row per drive: last extended test, next due, and why
  check    self-check only; prints every check and exits non-zero on problems
  heal     restore the timer and cron triggers from the copies in this folder
  snooze <drive|serial> <YYYY-MM-DD> [--note TEXT]
           don't start a test on that drive before that date; expires on its own
  unsnooze <drive|serial>
           remove a drive's snooze
  run-now <drive|serial> [quick|extended] [--override]
           start a test now (extended by default) if nothing stands in the way;
           --override starts it despite a busy drive, a scrub or backup coming up,
           or being outside the start window. A running test or bad pool always blocks.
  exclude <drive|serial>     never test this drive (EXCLUDE_SERIALS)
  include <drive|serial>     undo exclude
  interval <drive|serial> <days|default>
           this drive's interval (INTERVAL_OVERRIDES); default removes it
  install  what install.sh runs: config, manifest, triggers, self-check
  uninstall [--purge]
           what uninstall.sh runs: remove the triggers (--purge: the folder too)

Options:
  --dry-run   change nothing: no tests, no healing, no alerts, no state writes
  --source    who started this run (timer, cron, manual); set by the triggers
  --note      with snooze: why, shown by status
  --override  with run-now: start despite checks that can be overridden

Standard library only, plus better-unas's core library (core/lib). Python 3
ships in the UniFi OS firmware image and UniFi's base packages depend on it.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import syslog
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(1, os.path.dirname(BASE))   # the suite home, for core.lib
import planner  # noqa: E402  (lives next to this file)
import backups  # noqa: E402
from core.lib import alerts as lib_alerts  # noqa: E402
from core.lib import config as lib_config  # noqa: E402
from core.lib import busy, notify, settings, util  # noqa: E402
from core.lib.tool import Tool  # noqa: E402
if __name__ == "__main__":
    # actions.py imports this module by name; give it this copy, not a second one.
    sys.modules.setdefault("smart_scheduler", sys.modules[__name__])

API = "http://127.0.0.1:16080/api/v2"
NAME = "smart-scheduler"
TOOL = Tool(NAME, __file__, required=("smartctl", "lsblk", "runuser", "psql", "systemctl"))
CONFIG_FILE = TOOL.config_file

# Every setting, its default, type and range live in settings.json (the
# plugin contract's settings schema). Adding a setting means adding it there.
SCHEMA = TOOL.schema
DEFAULTS = settings.defaults(SCHEMA)
SHARED_SCHEMA = lib_config.SHARED_SCHEMA

RUNNING_STATUSES = planner.RUNNING_STATUSES


# --------------------------------------------------------------------------- helpers

def now():
    return util.now()


parse_time = planner.parse_time
sh = util.sh
heartbeat_urls = notify.heartbeat_urls
RESOLVED_KEEP = lib_alerts.RESOLVED_KEEP


def log(msg, level=syslog.LOG_INFO):
    TOOL.log(msg, level)


class ApiError(Exception):
    def __init__(self, kind, detail, status=None):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail, self.status = kind, detail, status


def api(method, path, user_id=None, body=None, timeout=15):
    """Call UniFi Drive's local API. Returns (HTTP status, parsed JSON or None)."""
    req = urllib.request.Request(API + path, method=method)
    if user_id:
        req.add_header("X-UserId", user_id)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except (urllib.error.URLError, OSError) as e:
        raise ApiError("unreachable", f"{method} {path}: {e}")
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    return status, parsed


# --------------------------------------------------------------------------- config and state

class Config(lib_config.Config):
    """The config file plus the suite's shared settings (core.lib.config).
    cfg[KEY] is the parsed value; overrides and window are shortcuts the
    planner uses."""

    def __init__(self, path, schema=None, core_path=None):
        super().__init__(schema or SCHEMA, path, core_path or TOOL.core_config_file)
        self.overrides = dict(self.typed["INTERVAL_OVERRIDES"])
        self.window = self.typed["START_WINDOW"]


def load_state():
    return TOOL.load_state()


def save_state(state, dry):
    TOOL.save_state(state, dry)


# --------------------------------------------------------------------------- snoozes
#
# state["snoozes"] = {serial: {"until": "YYYY-MM-DD", "note": str, "set": iso time}}.
# A snoozed drive gets no test before 00:00 local time on "until". Snoozes are
# runtime state, not config: the run drops them once that day has come.

def active_snoozes(state, today):
    """{serial: snooze} for snoozes that still hold on `today` (a date)."""
    out = {}
    for serial, z in (state.get("snoozes") or {}).items():
        try:
            if dt.date.fromisoformat(z["until"]) > today:
                out[serial] = z
        except (KeyError, TypeError, ValueError):
            continue
    return out


def expire_snoozes(state, today):
    """Drop snoozes that no longer hold (and unreadable ones). Returns the dropped serials."""
    keep = active_snoozes(state, today)
    dropped = sorted(set(state.get("snoozes") or {}) - set(keep))
    if keep:
        state["snoozes"] = keep
    else:
        state.pop("snoozes", None)
    return dropped


def snooze_text(z):
    text = f"snoozed until {dt.date.fromisoformat(z['until']):%a %d %b %Y}"
    return text + (f" ({z['note']})" if z.get("note") else "")


def not_before(state, t):
    """The planner's not_before: {serial: (00:00 on the snooze date, reason)}."""
    return {serial: (dt.datetime.combine(dt.date.fromisoformat(z["until"]), dt.time(0), tzinfo=t.tzinfo),
                     snooze_text(z))
            for serial, z in active_snoozes(state, t.date()).items()}


def set_snooze(state, serial, until, note="", t=None):
    """Snooze `serial` until `until` (a date after today). Replaces an existing snooze."""
    t = t or now()
    if until <= t.date():
        raise ValueError(f"{until:%Y-%m-%d} isn't after today ({t:%Y-%m-%d})")
    z = {"until": until.isoformat(), "set": t.isoformat()}
    if note:
        z["note"] = " ".join(str(note).split())[:200]
    state.setdefault("snoozes", {})[serial] = z
    return z


def clear_snooze(state, serial):
    """Remove `serial`'s snooze. Returns it, or None if there was none."""
    z = (state.get("snoozes") or {}).pop(serial, None)
    if not state.get("snoozes"):
        state.pop("snoozes", None)
    return z


# --------------------------------------------------------------------------- alerts

def Alerts(cfg, state, dry):
    """core.lib.alerts.Alerts for this tool; it reads now() and log() through
    this module so tests can swap them."""
    return lib_alerts.Alerts(cfg, state, dry, NAME, now=lambda: now(),
                             log=lambda msg, level=syslog.LOG_INFO: log(msg, level))


# --------------------------------------------------------------------------- NAS facts

def versions():
    v = {}
    try:
        with open("/usr/lib/version") as f:
            v["firmware"] = f.read().strip()
    except OSError:
        v["firmware"] = "unknown"
    rc, out = sh("dpkg-query", "-W", "-f=${Version}", "unifi-drive")
    v["drive"] = out if rc == 0 else "unknown"
    return v


def psql(db, query):
    """Run a read-only query as the postgres user. Returns (rows as lists, error or None)."""
    env = dict(os.environ, PGOPTIONS="-c default_transaction_read_only=on")
    rc, out = sh("runuser", "-u", "postgres", "--", "psql", "-p", "5432", "-d", db,
                 "-At", "-F", "\t", "-c", query, env=env, timeout=20)
    if rc != 0:
        return None, f"psql on {db} failed ({out[:200] or 'exit ' + str(rc)})"
    return [line.split("\t") for line in out.splitlines() if line.strip()], None


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# UniFi OS keeps accounts and roles in its ulp-go database. The owner always
# exists and always has Drive management rights, so it is the default identity.
OWNER_QUERY = """SELECT u.unique_id FROM "user" u
 JOIN role_user ru ON ru.user_id = u.unique_id
 JOIN role r ON r.unique_id = ru.role_id
 WHERE r.system_key = 'owner' AND u.deleted_at IS NULL AND u.status = 'ACTIVE'
 ORDER BY u.created_at LIMIT 1"""


def resolve_identity(cfg):
    """Return (user id or None, label, error or None).

    UniFi Drive only accepts SMART calls from a UniFi OS admin with Drive
    management rights (others get 403). By default that's the console owner;
    DRIVE_USER names a different admin. None/None means a configured user
    no longer exists.
    """
    name = cfg["DRIVE_USER"]
    if not name:
        rows, err = psql("ulp-go", OWNER_QUERY)
        if err:
            return None, "the console owner", err
        if not rows or not UUID_RE.fullmatch(rows[0][0]):
            return None, "the console owner", "couldn't find the console owner in UniFi OS's user database"
        return rows[0][0], "the console owner", None
    rows, err = psql("unifi-drive", f"SELECT id FROM users WHERE username = '{name}'")  # validated in Config
    label = f"UniFi user {name!r}"
    if err:
        return None, label, err
    if not rows:
        return None, label, None
    if not UUID_RE.fullmatch(rows[0][0]):
        return None, label, f"unexpected lookup result {rows[0][0][:80]!r}"
    return rows[0][0], label, None


def check_access(storage, user_id):
    """Confirm Drive accepts this identity for SMART calls. Returns an error or None."""
    disk = next((d for d in storage["disks"] if d.get("smartTestSupported")), None)
    if disk is None:
        return None
    try:
        status, _ = api("GET", f"/storage/disks/{disk['slotId']}/smart-tests?limit=1", user_id)
    except ApiError as e:
        return e.detail
    if status == 403:
        return "UniFi Drive refused it (403): it needs to be a UniFi OS admin with Drive management rights"
    if status != 200:
        return f"SMART history returned HTTP {status}"
    return None


def serial_to_device():
    rc, out = sh("lsblk", "-dno", "NAME,SERIAL")
    devices = {}
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                devices[parts[1]] = "/dev/" + parts[0]
    return devices


def smart_facts(device, attr_ids):
    """Return (extended test minutes or None, {attr id: raw value})."""
    rc, out = sh("smartctl", "-j", "-A", "-c", device, timeout=30)
    try:
        d = json.loads(out)
    except ValueError:
        return None, {}
    minutes = d.get("ata_smart_data", {}).get("self_test", {}).get("polling_minutes", {}).get("extended")
    attrs = {}
    for a in d.get("ata_smart_attributes", {}).get("table", []):
        if a.get("id") in attr_ids:
            attrs[str(a["id"])] = a.get("raw", {}).get("value", 0)
    return minutes, attrs


def self_test_progress(device):
    """Percent remaining of a self-test the drive itself says is running, else None.

    UniFi Drive's history only lists tests started through Drive. A test
    started another way (smartctl -t long, another tool) shows up only here."""
    rc, out = sh("smartctl", "-j", "-c", device, timeout=30)
    try:
        status = json.loads(out).get("ata_smart_data", {}).get("self_test", {}).get("status", {})
    except (ValueError, AttributeError):
        return None
    value = status.get("value") if isinstance(status, dict) else None
    if not isinstance(value, int) or value >> 4 != 0xF:   # 0xF_: in progress, low nibble = tenths left
        return None
    pct = status.get("remaining_percent")
    return pct if isinstance(pct, int) else (value & 0xF) * 10


def get_storage():
    status, data = api("GET", "/storage")
    if status == 404:
        raise ApiError("changed", "GET /storage returned 404 (route moved?)", status)
    if status != 200 or not isinstance(data, dict):
        raise ApiError("error", f"GET /storage returned HTTP {status}", status)
    missing = [k for k in ("pools", "disks") if not isinstance(data.get(k), list)]
    disk_keys = ("slotId", "serial", "smartTestSupported", "powerOnHours")
    pool_keys = ("id", "status", "dataScrubbing")
    for d in data.get("disks") or []:
        missing += [f"disks[].{k}" for k in disk_keys if k not in d]
    for p in data.get("pools") or []:
        missing += [f"pools[].{k}" for k in pool_keys if k not in p]
    if missing:
        raise ApiError("shape", "storage response is missing " + ", ".join(sorted(set(missing))))
    return data


def get_history(slot, serial, user_id, current=None):
    """Drive's test history for a slot, newest first.

    The history endpoint lists finished tests only. A test in progress is in
    the disk's `smartTest` field of GET /storage; pass it as `current` and it
    is added (once) at the front."""
    status, data = api("GET", f"/storage/disks/{slot}/smart-tests?serial={urllib.parse.quote(serial)}&limit=20", user_id)
    if status != 200 or not isinstance(data, list):
        raise ApiError("history", f"history for Drive {slot} returned HTTP {status}", status)
    return with_current(data, current)


def with_current(history, current):
    if not isinstance(current, dict) or not current.get("type"):
        return history
    if current.get("id") is not None and any(h.get("id") == current["id"] for h in history):
        return history
    return [current] + list(history)


def drive_progress_left(current):
    """Percent left of Drive's own test in progress (its `progress` is percent done), else None."""
    if isinstance(current, dict) and planner.is_running(current) and isinstance(current.get("progress"), (int, float)):
        return max(0, min(100, 100 - int(current["progress"])))
    return None


is_running = planner.is_running
is_good = planner.is_good


# --------------------------------------------------------------------------- self-check and healing

def trigger_problems():
    return TOOL.trigger_problems()


# --------------------------------------------------------------------------- scheduling

# The rules themselves live in planner.py, shared with the TUI's calendar.
in_window = planner.in_window
night_key = planner.night_key
start_gate = planner.start_gate


def evaluate_drives(cfg, storage, user_id, state, alerts, report=True):
    """Gather per-drive facts and work out when each one is due."""
    attr_ids = set(cfg["WARN_ATTRIBUTES"])
    excluded = set(cfg["EXCLUDE_SERIALS"])
    devices = serial_to_device()
    drives_state = state.setdefault("drives", {})
    t = now()
    result = []
    for d in storage["disks"]:
        serial, slot = d.get("serial"), d.get("slotId")
        if not serial or not d.get("smartTestSupported"):
            continue
        info = {"slot": slot, "serial": serial, "model": d.get("model", "?"),
                "excluded": serial in excluded, "running": False, "current_test": d.get("smartTest"),
                "kbps": (d.get("readKBPS") or 0) + (d.get("writeKBPS") or 0)}
        result.append(info)
        left = self_test_progress(devices[serial]) if serial in devices else None
        if info["excluded"]:
            info["reason"] = "excluded in config"
            _mark_outside_test(info, left)
            continue
        try:
            yielded = yielded_ids(state, serial)
            history = [h for h in get_history(slot, serial, user_id, d.get("smartTest"))
                       if str(h.get("id")) not in yielded]
            alerts.clear(f"history:{serial}")
        except ApiError as e:
            if not report:
                info["reason"], info["error"] = f"history unavailable: {e.detail}", True
                _mark_outside_test(info, left)
                continue
            alerts.raise_(f"history:{serial}", f"Couldn't read SMART history for Drive {slot} ({serial}): {e.detail}")
            info["reason"] = "history unavailable"
            info["error"] = True
            _mark_outside_test(info, left)
            continue
        extended = [h for h in history if str(h.get("type")).lower() == "extended"]
        finished = sorted((h for h in extended if not is_running(h)),
                          key=lambda h: h.get("finishedAt") or "", reverse=True)

        minutes, attrs = (None, {})
        if serial in devices:
            minutes, attrs = smart_facts(devices[serial], attr_ids)
        info["minutes"] = minutes or 720
        info["attrs"] = attrs
        ds = drives_state.setdefault(serial, {})
        baseline = ds.get("attrs_at_last_test")

        # Report finished tests we have not reported yet.
        for h in finished if report else ():
            key = f"{serial}:{h.get('id')}"
            if key in state.setdefault("reported_tests", []):
                continue
            state["reported_tests"].append(key)
            del state["reported_tests"][:-500]
            if not is_good(h):
                alerts.raise_(f"test:{key}", f"Extended SMART test on Drive {slot} ({serial}, {info['model']}) "
                                             f"ended {h.get('status')}/{h.get('result')} at {h.get('finishedAt')}.")
            elif cfg["NOTIFY_SUCCESS"]:
                alerts.info(f"Extended SMART test on Drive {slot} ({serial}) passed.")

        rose = planner.rise_state(history, attrs, baseline, ds.get("rose"), t)
        if report:
            if rose:
                ds["rose"] = rose
            else:
                ds.pop("rose", None)
        info.update(planner.assess(cfg, d, history, attrs, baseline, t, rose=rose))
        drive_left = drive_progress_left(d.get("smartTest"))
        if info.get("running") and drive_left is not None:
            info["remaining_percent"] = drive_left
        _mark_outside_test(info, left)
    return result


def _mark_outside_test(info, left):
    """Running state for paths that skip the history (excluded, unreadable)
    and for a test the drive says is running that Drive doesn't know about
    (started with smartctl or another tool). Either counts as running everywhere."""
    current = info.get("current_test")
    if not info.get("running") and isinstance(current, dict) and planner.is_running(current):
        info["running"] = True
        drive_left = drive_progress_left(current)
        if drive_left is not None:
            info["remaining_percent"] = drive_left
        return
    if left is None:
        return
    info["remaining_percent"] = left
    if not info.get("running"):
        info["running"] = True
        info["outside_drive"] = True
        info["reason"] = f"test running outside UniFi Drive ({left}% left)"


def read_backups(cfg, tz):
    """UniFi Drive's backup tasks and recent runs, read-only.

    Returns {"tasks", "runs", "problems": [(alert key, message)]}. Only the
    schedule and timing columns named in backups.py are selected, never the
    credential columns next to them. Any problem means no backup windows from
    the affected part; the caller alerts and tests carry on without them."""
    out = {"tasks": [], "runs": [], "problems": []}
    rows, err = psql("unifi-drive", backups.SHAPE_QUERY)
    if err:
        out["problems"].append(("backups:read", f"Couldn't read UniFi Drive's backup tasks: {err}"))
        return out
    cols = backups.columns_by_table(rows)
    shape = backups.shape_problems(cols)
    if shape:
        out["problems"].append(("backups:shape", "UniFi Drive's backup tables changed: " + "; ".join(shape)))
        return out
    query, names = backups.task_query(cols)
    rows, err = psql("unifi-drive", query)
    if err:
        out["problems"].append(("backups:read", f"Couldn't read UniFi Drive's backup tasks: {err}"))
        return out
    out["tasks"] = backups.parse_tasks(rows, names)
    rows, err = psql("unifi-drive", backups.run_query(cols))
    if err:
        out["problems"].append(("backups:read", f"Couldn't read UniFi Drive's backup history: {err}"))
    else:
        out["runs"] = backups.parse_runs(rows, tz)
    for task in out["tasks"]:
        bad = task["enabled"] and backups.schedule_problem(task)
        if bad:
            out["problems"].append((f"backups:schedule:{task['id']}",
                                    f"Can't read the schedule of {backups.label(task)} (task {task['id']}): {bad}"))
    return out


def backup_windows(cfg, t, until, alerts=None):
    """(planner.Windows for UniFi backup tasks between t and until, read_backups result).

    With `alerts`, problems are raised as warnings and cleared once they go away."""
    if not cfg["BACKUP_AVOID"]:
        if alerts:
            for key in [k for k in alerts.active if k.startswith("backups:")]:
                alerts.clear(key)
        return [], None
    info = read_backups(cfg, t.tzinfo)
    if alerts:
        now_keys = {k for k, _ in info["problems"]}
        for key, msg in info["problems"]:
            alerts.raise_(key, msg + ". Tests won't avoid the backups it affects until this is fixed.", "warning")
        for key in [k for k in alerts.active if k.startswith("backups:") and k not in now_keys]:
            alerts.clear(key)
    return backups.windows(cfg, info["tasks"], info["runs"], t, until), info


def busy_windows(t):
    """The other tools' busy windows (core/lib/busy.py) as planner.Windows, so
    tests stay clear of them like they do of backups."""
    return [planner.Window(w.start, w.end, w.label, w.kind, w.tier) for w in TOOL.others_busy(t)]


def horizon_of(t):
    """How far ahead the plan, the calendar and `status` look for backups."""
    return t + dt.timedelta(days=planner.MAX_NIGHTS + 2)


# How far ahead the hourly run reads backups and publishes its windows.
BUSY_AHEAD = dt.timedelta(days=31)


def running_tier(state, d):
    """The tier of the test running on drive d: what this tool recorded when
    it started it, else manual (started in UniFi Drive or with smartctl)."""
    return (state.get("drives", {}).get(d["serial"]) or {}).get("running_tier") or "manual"


def running_windows(cfg, state, drives, t):
    """This tool's running tests as busy windows."""
    return [busy.Busy(t, planner.running_until(cfg, d, t), f"{NAME} test on Drive {d['slot']}", "running",
                      NAME, running_tier(state, d)) for d in drives if d.get("running")]


def publish_busy(cfg, storage, drives, state, t, windows, dry, started=None):
    """Put this tool's windows in the busy registry: running and planned tests,
    and UniFi's scrubs and backups as this tool reads them (with its margins,
    tier "fixed"), so other tools needn't read UniFi themselves. `windows` is
    what the run planned with (backups and the other tools' windows);
    `started` is a drive this run just started, which doesn't report running yet."""
    out = running_windows(cfg, state, drives, t)
    if started is not None:
        out.append((t, t + planner.test_length(cfg, started.get("minutes")),
                    f"{NAME} test on Drive {started['slot']}", "running", planner.tier_of(started)))
        drives = [dict(d, running=True) if d is started else d for d in drives]
    plan = planner.plan(storage, drives, state, cfg, t, windows=windows, not_before=not_before(state, t))
    until = t + BUSY_AHEAD
    out += [(e["start"], e["end"], f"{NAME} test on Drive {e['slot']}", "planned", e["tier"])
            for e in plan if e["start"] < until]
    out += [(w.start, w.end, f"UniFi {w.label}", w.kind, "fixed")
            for w in planner.scrub_windows(cfg, storage, until) + list(windows)
            if w.kind in ("scrub", "backup") and w.tier == "fixed" and w.start < until]
    return TOOL.publish_busy(out, t, dry)


def abort_test(d, dry):
    """Stop the extended test running on drive d (smartctl -X). None if it
    stopped, else why not. A SMART test can't pause, so it's planned again."""
    dev = serial_to_device().get(d["serial"])
    if not dev:
        return "its device wasn't found"
    if dry:
        print(f"(dry-run) would stop the test on Drive {d['slot']}: smartctl -X {dev}")
        return None
    rc, out = sh("smartctl", "-X", dev, timeout=30)
    return None if rc == 0 else f"smartctl -X returned {rc}: {out[:200]}"


def give_way(cfg, state, drives, t, others, alerts, dry):
    """Stop each running test another tool's higher-tier job needs the disks
    for now (core/lib/busy.py must_yield). Its UniFi record is then left out
    of the history, so the drive stays due and is planned again."""
    stopped = []
    for d in drives:
        if not d.get("running"):
            continue
        clash = busy.must_yield(running_windows(cfg, state, [d], t), others, t)
        if not clash:
            continue
        w, o = clash[0]
        why = abort_test(d, dry)
        if why:
            alerts.raise_(f"yield:{d['serial']}", f"Couldn't stop the {w.tier} test on Drive {d['slot']} for "
                                                  f"{o.label} ({o.tier}): {why}", "warning")
            continue
        alerts.clear(f"yield:{d['serial']}")
        alerts.info(f"Stopped the {w.tier} test on Drive {d['slot']} ({d['serial']}) to make way for "
                    f"{o.label} ({o.tier}); it will be planned again.")
        if not dry:
            ds = state.setdefault("drives", {}).setdefault(d["serial"], {})
            test = d.get("current_test") if isinstance(d.get("current_test"), dict) else {}
            if test.get("id") is not None:
                ds["yielded"] = (ds.get("yielded", []) + [str(test["id"])])[-20:]
            ds.pop("running_tier", None)
            d["running"] = False
        stopped.append(d)
    return stopped


def yielded_ids(state, serial):
    """UniFi history ids of tests this tool stopped to make way: not failures."""
    return set((state.get("drives", {}).get(serial) or {}).get("yielded", []))


def scrub_conflict(cfg, storage, minutes, t):
    """Return a reason if a test of this length would run into a scheduled scrub."""
    until = t + planner.test_length(cfg, minutes)
    return planner.conflict(cfg, planner.scrub_windows(cfg, storage, until), minutes, t)


# --------------------------------------------------------------------------- commands

def self_check(cfg, state, alerts, dry):
    """Check our own files, triggers, tools, API and user. Returns (storage, user_id) or (None, None)."""
    TOOL.self_check(cfg, alerts, dry)

    # Firmware / Drive version change.
    v = versions()
    old = state.get("versions")
    if old and old != v:
        alerts.info(f"Firmware or Drive changed: {old.get('firmware')} / Drive {old.get('drive')} -> "
                    f"{v['firmware']} / Drive {v['drive']}. Running a full self-check.")
        state["version_changed"] = True
    state["versions"] = v

    # API.
    try:
        storage = get_storage()
        for k in ("api:unreachable", "api:changed", "api:error", "api:shape"):
            alerts.clear(k)
    except ApiError as e:
        alerts.raise_(f"api:{e.kind}", f"UniFi Drive API problem: {e.detail}. No tests will start "
                      f"until this clears (Drive {v['drive']}).")
        return None, None

    # Identity Drive accepts for SMART calls.
    user_id, label, err = resolve_identity(cfg)
    if err:
        cached = state.get("user_id")
        alerts.raise_("user:lookup", f"Couldn't look up {label}: {err}."
                      + (" Using the last known ID." if cached else " No tests will start."))
        user_id = cached
    else:
        alerts.clear("user:lookup")
        if user_id is None:
            alerts.raise_("user:missing", f"{label} (DRIVE_USER in config) no longer exists. Treating this as "
                          "turned off on purpose: no tests will start. Recreate the user, or clear DRIVE_USER "
                          "to use the console owner.", "warning")
            return storage, None
        alerts.clear("user:missing")
    if user_id:
        denied = check_access(storage, user_id)
        if denied:
            alerts.raise_("user:access", f"Can't use {label} for SMART tests: {denied}.")
            return storage, None
        alerts.clear("user:access")
        state["user_id"] = user_id
    return storage, user_id


def cmd_run(cfg, args):
    dry = args.dry_run
    state = load_state()
    alerts = Alerts(cfg, state, dry)
    t = now()

    if TOOL.turned_off(cfg, state, alerts, dry, "the hourly timer and cron line"):
        alerts.flush()
        save_state(state, dry)
        return 0

    # The cron line is only a backup: if the timer is healthy and ran recently, just heal and leave.
    if args.source == "cron":
        if TOOL.cron_can_skip(state, t, dt.timedelta(hours=2)):
            return 0
        log("cron backup running a full tick (timer missing or late)")

    for serial in expire_snoozes(state, t.date()):
        log(f"snooze on {serial} has ended")

    storage, user_id = self_check(cfg, state, alerts, dry)
    if storage is not None and user_id:
        drives = evaluate_drives(cfg, storage, user_id, state, alerts)
        for d in drives:
            if not d.get("running"):
                state.get("drives", {}).get(d["serial"], {}).pop("running_tier", None)
        others = TOOL.others_busy(t)
        give_way(cfg, state, drives, t, others, alerts, dry)
        windows, _ = backup_windows(cfg, t, t + BUSY_AHEAD, alerts)
        windows += busy_windows(t)
        decision = planner.decide(cfg, storage, drives, state, t, windows=windows,
                                  not_before=not_before(state, t))
        started = None
        if decision.gate:
            if decision.due and decision.gate != "outside start window":
                log(f"{len(decision.due)} drive(s) due; not starting: {decision.gate}")
        else:
            for d, why in decision.skipped:
                if why != "outside start window":
                    log(f"Drive {d['slot']} ({d['serial']}) due; not starting: {why}")
            if decision.start and start_test(cfg, decision.start, user_id, state, alerts, dry, t):
                started = decision.start
                tier = planner.tier_of(started)
                end = t + planner.test_length(cfg, started.get("minutes"))
                TOOL.nudge(busy.in_the_way(others, t, end, tier), dry)
        publish_busy(cfg, storage, drives, state, t, windows, dry, started=started)
        if state.pop("version_changed", False):
            alerts.info("Self-check after the update passed: API, identity and triggers all OK."
                        if not alerts.has_errors() else "Self-check after the update found problems (see alerts).")
    state["last_tick"] = t.isoformat()
    alerts.flush()
    TOOL.heartbeat(cfg, alerts, dry)
    save_state(state, dry)
    return 1 if alerts.has_errors() else 0


def start_test(cfg, d, user_id, state, alerts, dry, t):
    """Start the extended test the run picked. True if it started (or would, dry)."""
    label = f"Drive {d['slot']} ({d['serial']}, {d['model']})"
    tier = planner.tier_of(d)
    if dry:
        print(f"(dry-run) would start an extended test on {label}: {d['reason']}"
              + (" (urgent)" if tier == "urgent" else ""))
        return True
    try:
        status, body = api("POST", f"/storage/disks/{d['slot']}/smart-tests", user_id, {"type": "extended"})
    except ApiError as e:
        alerts.raise_("api:start", f"Couldn't start a test on {label}: {e.detail}")
        return False
    if status in (200, 201, 202):
        alerts.clear("api:start")
        log(f"started extended test on {label}: {d['reason']}" + (" (urgent)" if tier == "urgent" else ""))
        nk = night_key(cfg, t)
        if tier != "urgent":           # urgent starts don't use up the night's count
            state["starts"] = {nk: state.get("starts", {}).get(nk, 0) + 1}
        ds = state.setdefault("drives", {}).setdefault(d["serial"], {})
        ds["attrs_at_last_test"] = d.get("attrs", {})
        ds["running_tier"] = tier
        return True
    if status == 409:
        log(f"{label}: a test is already running (409); skipping")
    else:
        alerts.raise_("api:start", f"Starting a test on {label} returned HTTP {status}: {json.dumps(body)[:200]}")
    return False


def cmd_status(cfg, args):
    state = load_state()
    alerts = Alerts(cfg, state, True)
    try:
        storage = get_storage()
    except ApiError as e:
        print(f"UniFi Drive API problem: {e.detail}")
        return 1
    user_id, label, err = resolve_identity(cfg)
    user_id = user_id or state.get("user_id")
    if not user_id:
        print(f"Can't use {label}: {err or 'it no longer exists'}.")
        return 1
    drives = evaluate_drives(cfg, storage, user_id, json.loads(json.dumps(state)), alerts, report=False)
    t = now()
    snoozes = active_snoozes(state, t.date())
    print("Extended SMART tests\n")
    for d in sorted(drives, key=lambda d: int(d["slot"]) if str(d["slot"]).isdigit() else 99):
        last = parse_time(d.get("last"))
        due = d.get("due")
        if d.get("running"):
            nxt = "running now"
        elif d["excluded"]:
            nxt = "never (excluded)"
        elif d["serial"] in snoozes and (not due or due.date() < dt.date.fromisoformat(snoozes[d["serial"]]["until"])):
            nxt = f"not before {dt.date.fromisoformat(snoozes[d['serial']]['until']):%a %d %b %Y} (snoozed)"
        elif due and due <= t:
            nxt = "due now"
        else:
            nxt = f"{due:%a %d %b %Y}" if due else "unknown"
        print(f"Drive {d['slot']}  {d['model']}  ({d['serial']})")
        print(f"  Last test: {f'{last:%a %d %b %Y}' if last else 'never'}")
        print(f"  Next test: {nxt}")
        print(f"  Why:       {d.get('reason', '')}")
        if d["serial"] in snoozes:
            print(f"  Snoozed:   {snooze_text(snoozes[d['serial']])[8:]}")
        print()
    windows, backup_info = backup_windows(cfg, t, horizon_of(t))
    windows += busy_windows(t)
    plan = planner.plan(storage, drives, state, cfg, t, windows=windows, not_before=not_before(state, t))
    if plan:
        print("Plan (assuming each test passes):")
        rows = [(e["night"], f"{e['start']:%a %d %b %H:%M}  Drive {e['slot']} ({e['serial']})") for e in plan]
        rows += [(s["night"], f"{dt.date.fromisoformat(s['night']):%a %d %b}        skipped: "
                  + "; ".join(s["reasons"])) for s in plan.skipped]
        for _, row in sorted(rows, key=lambda r: r[0]):
            print("  " + row)
        print()
    print_backups(cfg, backup_info, t)
    gate = start_gate(cfg, storage, drives, state, t)
    print(f"Tests start between {cfg.values['START_WINDOW'].replace('-', ' and ')}.")
    print(f"Right now: {gate or 'a test could start'}.")
    for p in storage["pools"]:
        sc = p.get("dataScrubbing") or {}
        nxt = parse_time(sc.get("nextRun"))
        if nxt:
            print(f"Next scrub (pool {p.get('number', '?')}): {nxt:%a %d %b %H:%M}, status {sc.get('status')}.")
    if state.get("alerts"):
        print("\nActive alerts:")
        for a in state["alerts"].values():
            print(f"  [{a['level']}] {a['msg']}")
    return 0


def print_backups(cfg, info, t):
    if not cfg["BACKUP_AVOID"] or info is None:
        print("UniFi backup tasks: not avoided (BACKUP_AVOID=no).\n")
        return
    if not info["tasks"] and not info["problems"]:
        print("UniFi backup tasks: none.\n")
        return
    print("UniFi backup tasks (tests avoid them):")
    for task in info["tasks"]:
        name = backups.label(task)
        if not task["enabled"]:
            print(f"  {name}: schedule off, ignored")
            continue
        bad = backups.schedule_problem(task)
        if bad:
            print(f"  {name}: can't read its schedule ({bad})")
            continue
        _, how = backups.estimate(cfg, task, info["runs"])
        mine = backups.windows(cfg, [task], info["runs"], t, t + dt.timedelta(days=62))
        if mine:
            w = mine[0]
            end = f"{w.end:%H:%M}" if w.end.date() == w.start.date() else f"{w.end:%a %d %b %H:%M}"
            when = f"next blocks {w.start:%a %d %b %H:%M}-{end}"
        else:
            when = "no run in the next two months"
        print(f"  {name}: {when}; estimate {how}")
    for _, msg in info["problems"]:
        if not msg.startswith("Can't read the schedule"):
            print(f"  problem: {msg}")
    print()


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
    v = versions()
    print(f"  INFO  firmware {v['firmware']}, Drive {v['drive']}")
    try:
        storage = get_storage()
        line(True, "UniFi Drive API", f"{len(storage['disks'])} disks, {len(storage['pools'])} pool(s)")
    except ApiError as e:
        line(False, "UniFi Drive API", e.detail)
        return 1
    uid, label, err = resolve_identity(cfg)
    if not uid:
        line(False, f"identity ({label})", err or "no longer exists")
    else:
        denied = check_access(storage, uid)
        line(not denied, f"identity ({label})", denied or "")
    return 0 if ok else 1


def cmd_heal(cfg, args):
    return TOOL.cmd_heal(args.dry_run, force=args.force)


def cmd_install(args):
    return TOOL.install(
        lambda: cmd_check(Config(CONFIG_FILE), args),
        after=(lambda: cmd_status(Config(CONFIG_FILE), args),),
        done=("Installed. It checks hourly and starts tests only inside START_WINDOW.",
              "See the schedule any time:  better-unas smart-scheduler status",
              "Logs:                       journalctl -t smart-scheduler"),
        failed=("If the identity check failed and DRIVE_USER is set in config, that account must",
                "be a UniFi OS admin with Drive management. Clear DRIVE_USER to use the console owner."),
        off_note="Running tests finish.")


def resolve_drive(target, storage):
    """(serial, label) for a drive number or serial, or raise ValueError."""
    disks = (storage or {}).get("disks") or []
    for d in disks:
        if str(d.get("slotId")) == target and d.get("serial"):
            return d["serial"], f"Drive {d['slotId']} ({d['serial']})"
    for d in disks:
        if d.get("serial") == target:
            return target, f"Drive {d.get('slotId')} ({target})"
    raise ValueError(f"no drive with number or serial {target!r}")


def cmd_snooze(cfg, args):
    if not args.target or not args.date:
        print("Usage: smart_scheduler.py snooze <drive|serial> <YYYY-MM-DD> [--note TEXT]", file=sys.stderr)
        return 2
    try:
        until = dt.date.fromisoformat(args.date)
    except ValueError:
        print(f"Not a date: {args.date!r} (want YYYY-MM-DD).", file=sys.stderr)
        return 2
    state = load_state()
    try:
        serial, label = drive_for(args.target, state)
        z = set_snooze(state, serial, until, args.note or "")
    except ValueError as e:
        print(f"{e}.", file=sys.stderr)
        return 1
    save_state(state, args.dry_run)
    print(f"{'(dry-run) would snooze' if args.dry_run else 'Snoozed'} {label}: no test before "
          f"{until:%a %d %b %Y}" + (f" ({z['note']})" if z.get("note") else "") + ".")
    return 0


def cmd_unsnooze(cfg, args):
    if not args.target or args.date:
        print("Usage: smart_scheduler.py unsnooze <drive|serial>", file=sys.stderr)
        return 2
    state = load_state()
    try:
        serial, label = drive_for(args.target, state)
    except ValueError as e:
        print(f"{e}.", file=sys.stderr)
        return 1
    if not clear_snooze(state, serial):
        print(f"{label} isn't snoozed.")
        return 0
    save_state(state, args.dry_run)
    print(f"{'(dry-run) would remove' if args.dry_run else 'Removed'} the snooze on {label}.")
    return 0


def drive_for(target, state):
    """Resolve a drive number or serial against the live drive list. A snoozed serial
    resolves even when the API is down or the drive is gone, so it can be removed."""
    try:
        return resolve_drive(target, get_storage())
    except ApiError as e:
        if target in (state.get("snoozes") or {}):
            return target, target
        raise ValueError(f"can't read the drive list ({e.detail})")
    except ValueError:
        if target in (state.get("snoozes") or {}):
            return target, target
        raise


def resolve_serial(target):
    """A drive number or serial -> (serial, label) for config edits. A target that
    isn't a current drive number is taken as a serial, so a drive that's gone (or an
    API that's down) can still be removed from the config."""
    try:
        return resolve_drive(target, get_storage())
    except (ApiError, ValueError):
        if target.isdigit() and len(target) < 3:
            raise ValueError(f"Drive {target} is empty")
        return target, target


def cmd_run_now(cfg, args):
    import actions
    kind = args.date or "extended"
    if not args.target or kind not in actions.KINDS:
        print("Usage: smart_scheduler.py run-now <drive|serial> [quick|extended] [--override]", file=sys.stderr)
        return 2
    try:
        storage = get_storage()
        slot = next((str(d["slotId"]) for d in storage["disks"] if d.get("serial") == args.target), args.target)
    except ApiError:
        slot = args.target   # check_run_now reports the API problem
    try:
        r = actions.run_now(slot, kind, override=args.override, cfg=cfg, dry_run=args.dry_run, wait=120)
    except actions.LockBusy as e:
        print(f"Not started: {e}.", file=sys.stderr)
        return 1
    for c in r.checks:
        print(f"  {'override' if c.overridable else 'BLOCKS  '}  {c.reason}")
    print(r.message)
    return 0 if r.started else 1


def cmd_config_edit(cfg, args):
    import actions
    usage = {"exclude": "exclude <drive|serial>", "include": "include <drive|serial>",
             "interval": "interval <drive|serial> <days|default>"}[args.command]
    if not args.target or (args.command == "interval") != bool(args.date):
        print(f"Usage: smart_scheduler.py {usage}", file=sys.stderr)
        return 2
    try:
        serial, label = resolve_serial(args.target)
        if args.command == "interval":
            days = None if args.date == "default" else int(args.date) if args.date.isdigit() else args.date
            changed = actions.set_interval(serial, days, dry_run=args.dry_run, wait=120)
            what = f"interval for {label}: " + ("the normal rules" if days is None else f"every {days} days")
        else:
            on = args.command == "exclude"
            changed = actions.exclude(serial, on, dry_run=args.dry_run, wait=120)
            what = f"{label} {'excluded from' if on else 'included in'} testing"
    except settings.ValidationError as e:
        print("; ".join(f"{k}: {m}" for k, m in e.errors.items()) + ".", file=sys.stderr)
        return 1
    except (ValueError, actions.LockBusy) as e:
        print(f"{e}.", file=sys.stderr)
        return 1
    if not changed:
        print(f"No change: {what} already.")
    else:
        print(f"{'(dry-run) would set' if args.dry_run else 'Set'} {what}.")
    return 0


def take_lock(wait):
    """The run lock (core.lib.tool), held until the returned file is closed,
    or None if it stayed busy for `wait` seconds. Anything that writes state takes it."""
    return TOOL.take_lock(wait)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("run", "status", "check", "heal", "snooze", "unsnooze",
                                        "run-now", "exclude", "include", "interval", "install", "uninstall"))
    ap.add_argument("target", nargs="?", help=argparse.SUPPRESS)
    ap.add_argument("date", nargs="?", help=argparse.SUPPRESS)
    ap.add_argument("--note", default="", help=argparse.SUPPRESS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--source", default="manual", choices=("timer", "cron", "manual"))
    ap.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--override", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--purge", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.command == "install":
        return cmd_install(args)
    if args.command == "uninstall":
        return TOOL.uninstall(args.purge, "Tests already running on a drive are left to finish.")
    if os.geteuid() != 0:
        print("Run as root.", file=sys.stderr)
        return 2
    cfg = Config(CONFIG_FILE)
    # status and check only read, so they never wait. run, heal and the snooze
    # commands change state and take the lock: scheduled runs skip if one is
    # already going, manual ones wait for it (a run normally takes a few seconds).
    # run-now, exclude, include and interval take it inside actions.py.
    lock = None
    if args.command in ("run", "heal", "snooze", "unsnooze"):
        lock = take_lock(0 if args.source in ("timer", "cron") else 120)
        if lock is None:
            print("Another run is in progress; skipping this one.")
            return 0 if args.command in ("run", "heal") else 1
    try:
        return {"run": cmd_run, "status": cmd_status, "check": cmd_check, "heal": cmd_heal,
                "snooze": cmd_snooze, "unsnooze": cmd_unsnooze, "run-now": cmd_run_now,
                "exclude": cmd_config_edit, "include": cmd_config_edit,
                "interval": cmd_config_edit}[args.command](cfg, args)
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
