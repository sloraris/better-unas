#!/usr/bin/python3
"""smart-scheduler: schedule UniFi Drive's own extended SMART tests on a UNAS.

Tests are started through UniFi Drive's local API, so they appear in
Storage > Health Monitor and trigger UniFi's own notifications.

Commands:
  run      the hourly tick: self-check, self-heal, report results, maybe start a test
  status   one row per drive: last extended test, next due, and why
  check    self-check only; prints every check and exits non-zero on problems
  heal     restore the timer and cron triggers from the copies in this folder

Options:
  --dry-run   change nothing: no tests, no healing, no alerts, no state writes
  --source    who started this run (timer, cron, manual); set by the triggers

Standard library only. Python 3 ships in the UniFi OS firmware image and
UniFi's base packages depend on it.
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
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.realpath(__file__))
CONFIG_FILE = os.path.join(BASE, "config")
STATE_FILE = os.path.join(BASE, "state.json")
MANIFEST_FILE = os.path.join(BASE, "manifest.sha256")
LOCK_FILE = os.path.join(BASE, ".lock")
TEMPLATE_DIR = os.path.join(BASE, "triggers")

API = "http://127.0.0.1:16080/api/v2"
NAME = "smart-scheduler"

# Installed trigger files: template name -> path on the NAS.
TRIGGERS = {
    "smart-scheduler.service": "/etc/systemd/system/smart-scheduler.service",
    "smart-scheduler.timer": "/etc/systemd/system/smart-scheduler.timer",
    "smart-scheduler.cron": "/etc/cron.d/smart-scheduler",
}
TIMER_UNIT = "smart-scheduler.timer"

DEFAULTS = {
    "ENABLED": "yes",
    "DRIVE_USER": "",
    "INTERVAL_DAYS": "90",
    "WARN_INTERVAL_DAYS": "30",
    "OLD_INTERVAL_DAYS": "60",
    "OLD_DRIVE_HOURS": "43800",
    "RETRY_FAILED_DAYS": "7",
    "WARN_ATTRIBUTES": "5,187,197,198",
    "START_WINDOW": "00:00-05:00",
    "MAX_CONCURRENT": "1",
    "MAX_STARTS_PER_NIGHT": "1",
    "SCRUB_MARGIN_HOURS": "2",
    "DURATION_FACTOR": "1.5",
    "BUSY_KBPS": "20000",
    "EXCLUDE_SERIALS": "",
    "INTERVAL_OVERRIDES": "",
    "WEBHOOK_URL": "",
    "WEBHOOK_FORMAT": "text",
    "NOTIFY_SUCCESS": "no",
    "ALERT_REPEAT_HOURS": "72",
    "HEARTBEAT_URL": "",
    "HEARTBEAT_FAIL_URL": "",
}

RUNNING_STATUSES = {"running", "inprogress", "in_progress", "pending", "queued", "testing"}


# --------------------------------------------------------------------------- helpers

def now():
    return dt.datetime.now().astimezone()


def parse_time(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


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


def sh(*cmd, timeout=60, env=None):
    """Run a command; return (exit code, stdout). Never raises for a missing tool."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


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

class Config:
    def __init__(self, path):
        self.values = dict(DEFAULTS)
        self.errors = []
        if os.path.exists(path):
            with open(path) as f:
                for n, line in enumerate(f, 1):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        self.errors.append(f"config line {n}: expected KEY=VALUE")
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k not in DEFAULTS:
                        self.errors.append(f"config line {n}: unknown setting {k}")
                        continue
                    self.values[k] = v
        self.overrides = {}
        for item in filter(None, (x.strip() for x in self.values["INTERVAL_OVERRIDES"].split(","))):
            serial, _, days = item.rpartition(":")
            if serial and days.isdigit():
                self.overrides[serial] = int(days)
            else:
                self.errors.append(f"INTERVAL_OVERRIDES: bad entry {item!r} (want SERIAL:DAYS)")
        try:
            self.window = self._window(self.values["START_WINDOW"])
        except ValueError:
            self.errors.append("START_WINDOW: want HH:MM-HH:MM")
            self.window = self._window(DEFAULTS["START_WINDOW"])
        for key in ("INTERVAL_DAYS", "WARN_INTERVAL_DAYS", "OLD_INTERVAL_DAYS", "OLD_DRIVE_HOURS",
                    "RETRY_FAILED_DAYS", "MAX_CONCURRENT", "MAX_STARTS_PER_NIGHT",
                    "SCRUB_MARGIN_HOURS", "ALERT_REPEAT_HOURS", "BUSY_KBPS"):
            if not self.values[key].isdigit():
                self.errors.append(f"{key}: want a whole number, got {self.values[key]!r}")
                self.values[key] = DEFAULTS[key]
        if self.values["DRIVE_USER"] and not re.fullmatch(r"[A-Za-z0-9._@-]+", self.values["DRIVE_USER"]):
            self.errors.append("DRIVE_USER: letters, digits and . _ @ - only")
            self.values["DRIVE_USER"] = DEFAULTS["DRIVE_USER"]

    @staticmethod
    def _window(text):
        a, b = text.split("-")
        return dt.time.fromisoformat(a.strip()), dt.time.fromisoformat(b.strip())

    def __getitem__(self, k):
        return self.values[k]

    def int(self, k):
        return int(self.values[k])

    def float(self, k):
        try:
            return float(self.values[k])
        except ValueError:
            return float(DEFAULTS[k])

    def yes(self, k):
        return self.values[k].lower() in ("yes", "true", "1", "on")

    def set_of(self, k):
        return {x.strip() for x in self.values[k].split(",") if x.strip()}


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
    """Deduplicated alerts with an outbox, so nothing is lost while the webhook is down."""

    def __init__(self, cfg, state, dry):
        self.cfg, self.state, self.dry = cfg, state, dry
        self.active = state.setdefault("alerts", {})
        self.outbox = state.setdefault("outbox", [])
        self.host = socket.gethostname()

    def raise_(self, key, msg, level="error"):
        t = now()
        a = self.active.get(key)
        repeat = dt.timedelta(hours=self.cfg.int("ALERT_REPEAT_HOURS"))
        if a and a["msg"] == msg and t - parse_time(a["sent"]) < repeat:
            return
        log(f"[{level}] {msg}", syslog.LOG_ERR if level == "error" else syslog.LOG_WARNING)
        self.active[key] = {"msg": msg, "level": level, "sent": t.isoformat(),
                            "since": (a or {}).get("since", t.isoformat())}
        self._queue(level, msg)

    def clear(self, key, msg=None):
        a = self.active.pop(key, None)
        if a:
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
            m = self.outbox[0]
            if not self._send(url, m):
                log(f"webhook unreachable; {len(self.outbox)} message(s) kept for the next run",
                    syslog.LOG_WARNING)
                return
            self.outbox.pop(0)

    def _send(self, url, m):
        title = f"{self.host} {NAME}: {m['level']}"
        fmt = self.cfg["WEBHOOK_FORMAT"].lower()
        if fmt == "json":
            body, ctype = json.dumps({"host": self.host, "source": NAME, "level": m["level"],
                                      "message": m["msg"], "at": m["at"]}), "application/json"
        elif fmt == "slack":
            body, ctype = json.dumps({"text": f"*{title}*\n{m['msg']}"}), "application/json"
        elif fmt == "discord":
            body, ctype = json.dumps({"content": f"**{title}**\n{m['msg']}"}), "application/json"
        else:  # plain text; works with ntfy, Gotify-style relays and most generic receivers
            body, ctype = f"{title}\n{m['msg']}", "text/plain; charset=utf-8"
        req = urllib.request.Request(url, data=body.encode(), method="POST")
        req.add_header("Content-Type", ctype)
        req.add_header("Title", title)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return 200 <= r.status < 300
        except (urllib.error.URLError, OSError):
            return False


def ping(url):
    if not url:
        return
    try:
        urllib.request.urlopen(url, timeout=10).read()
    except (urllib.error.URLError, OSError) as e:
        log(f"heartbeat ping failed: {e}", syslog.LOG_WARNING)


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


def get_history(slot, serial, user_id):
    status, data = api("GET", f"/storage/disks/{slot}/smart-tests?serial={urllib.parse.quote(serial)}&limit=20", user_id)
    if status != 200 or not isinstance(data, list):
        raise ApiError("history", f"history for slot {slot} returned HTTP {status}", status)
    return data


def is_running(test):
    return not test.get("finishedAt") or str(test.get("status", "")).lower() in RUNNING_STATUSES


def is_good(test):
    return str(test.get("status")).lower() == "successful" and str(test.get("result")).lower() == "optimal"


# --------------------------------------------------------------------------- self-check and healing

def verify_manifest():
    """Return a list of problems with this folder's own files."""
    try:
        with open(MANIFEST_FILE) as f:
            lines = [l.split() for l in f if l.strip()]
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
    rc, out = sh("systemctl", "is-enabled", TIMER_UNIT)
    if out != "enabled":
        problems.append(f"{TIMER_UNIT} is not enabled ({out or 'unknown'})")
    rc, out = sh("systemctl", "is-active", TIMER_UNIT)
    if out != "active":
        problems.append(f"{TIMER_UNIT} is not active ({out or 'unknown'})")
    return problems


def heal(dry):
    """Rewrite missing or changed trigger files and re-enable the timer. Returns what was fixed."""
    fixed = []
    units_changed = False
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
    rc_en, en = sh("systemctl", "is-enabled", TIMER_UNIT)
    rc_ac, ac = sh("systemctl", "is-active", TIMER_UNIT)
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


# --------------------------------------------------------------------------- scheduling

def in_window(cfg, t):
    start, end = cfg.window
    cur = t.time()
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end


def night_key(cfg, t):
    start, end = cfg.window
    if start > end and t.time() < end:
        return (t - dt.timedelta(days=1)).date().isoformat()
    return t.date().isoformat()


def evaluate_drives(cfg, storage, user_id, state, alerts, report=True):
    """Gather per-drive facts and work out when each one is due."""
    attr_ids = {int(x) for x in cfg.set_of("WARN_ATTRIBUTES") if x.isdigit()}
    excluded = cfg.set_of("EXCLUDE_SERIALS")
    devices = serial_to_device()
    drives_state = state.setdefault("drives", {})
    t = now()
    result = []
    for d in storage["disks"]:
        serial, slot = d.get("serial"), d.get("slotId")
        if not serial or not d.get("smartTestSupported"):
            continue
        info = {"slot": slot, "serial": serial, "model": d.get("model", "?"),
                "excluded": serial in excluded, "running": False,
                "kbps": (d.get("readKBPS") or 0) + (d.get("writeKBPS") or 0)}
        result.append(info)
        if info["excluded"]:
            info["reason"] = "excluded in config"
            continue
        try:
            history = get_history(slot, serial, user_id)
            alerts.clear(f"history:{serial}")
        except ApiError as e:
            if not report:
                info["reason"], info["error"] = f"history unavailable: {e.detail}", True
                continue
            alerts.raise_(f"history:{serial}", f"Couldn't read SMART history for slot {slot} ({serial}): {e.detail}")
            info["reason"] = "history unavailable"
            info["error"] = True
            continue
        extended = [h for h in history if str(h.get("type")).lower() == "extended"]
        info["running"] = any(is_running(h) for h in history)
        finished = sorted((h for h in extended if not is_running(h)),
                          key=lambda h: h.get("finishedAt") or "", reverse=True)
        last_good = next((h for h in finished if is_good(h)), None)
        latest = finished[0] if finished else None

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
                alerts.raise_(f"test:{key}", f"Extended SMART test on slot {slot} ({serial}, {info['model']}) "
                                             f"ended {h.get('status')}/{h.get('result')} at {h.get('finishedAt')}.")
            elif cfg.yes("NOTIFY_SUCCESS"):
                alerts.info(f"Extended SMART test on slot {slot} ({serial}) passed.")

        # Interval selection.
        warn = [f"SMART attribute {k} is {v}" for k, v in sorted(attrs.items()) if v]
        if d.get("riskReasons"):
            warn.append("UniFi risk: " + ", ".join(map(str, d["riskReasons"])))
        if d.get("state") not in (None, "optimal"):
            warn.append(f"UniFi state {d.get('state')}")
        increased = [k for k, v in attrs.items() if baseline is not None and v > baseline.get(k, 0)]
        if serial in cfg.overrides:
            interval, why = cfg.overrides[serial], "set in config"
        elif warn:
            interval, why = cfg.int("WARN_INTERVAL_DAYS"), "; ".join(warn)
        elif (d.get("powerOnHours") or 0) >= cfg.int("OLD_DRIVE_HOURS"):
            interval, why = cfg.int("OLD_INTERVAL_DAYS"), f"{d['powerOnHours']:,} power-on hours"
        else:
            interval, why = cfg.int("INTERVAL_DAYS"), "healthy"
        info["interval"] = interval

        if latest is not None and not is_good(latest):
            info["due"] = parse_time(latest["finishedAt"]) + dt.timedelta(days=cfg.int("RETRY_FAILED_DAYS"))
            info["reason"] = f"retrying: last test ended {latest.get('status')}/{latest.get('result')}"
        elif last_good is None:
            info["due"] = t
            info["reason"] = "never tested; first test sets a baseline"
        elif increased:
            info["due"] = t
            info["reason"] = "SMART attribute rose since last test: " + ", ".join(
                f"{k} went {baseline.get(k, 0)} -> {attrs[k]}" for k in increased)
        else:
            info["due"] = parse_time(last_good["finishedAt"]) + dt.timedelta(days=interval)
            info["reason"] = f"every {interval} days ({why})"
        info["last"] = last_good.get("finishedAt") if last_good else None
    return result


def start_gate(cfg, storage, drives, state, t):
    """Return None if a test may start now, otherwise the reason it may not."""
    if not in_window(cfg, t):
        return "outside start window"
    if cfg.int("MAX_STARTS_PER_NIGHT") == 0:
        return "paused (MAX_STARTS_PER_NIGHT=0 in config)"
    if state.get("starts", {}).get(night_key(cfg, t), 0) >= cfg.int("MAX_STARTS_PER_NIGHT"):
        return "already started tonight's test(s)"
    if sum(1 for d in drives if d.get("running")) >= cfg.int("MAX_CONCURRENT"):
        return "a SMART test is already running"
    for p in storage["pools"]:
        if p.get("status") != "fullyOperational":
            return f"pool {p.get('number', p['id'])} is {p.get('status')}"
        for rg in p.get("raidGroups") or []:
            if rg.get("currentProtection", 1) < rg.get("expectedProtection", 0) or rg.get("progress"):
                return f"pool {p.get('number', p['id'])} RAID group is degraded or resyncing"
        if (p.get("dataScrubbing") or {}).get("status", "idle") != "idle":
            return f"pool {p.get('number', p['id'])} is scrubbing"
    return None


def scrub_conflict(cfg, storage, minutes, t):
    """Return a reason if a test of this length would run into a scheduled scrub."""
    end = t + dt.timedelta(minutes=minutes * cfg.float("DURATION_FACTOR"))
    margin = dt.timedelta(hours=cfg.int("SCRUB_MARGIN_HOURS"))
    for p in storage["pools"]:
        sc = p.get("dataScrubbing") or {}
        nxt = parse_time(sc.get("nextRun"))
        if not (sc.get("schedule") or {}).get("enabled") or not nxt:
            continue
        # The +1h covers UniFi's nextRun being an hour early across a DST change.
        if t < nxt + dt.timedelta(hours=1) and end > nxt - margin:
            return f"would overlap pool {p.get('number', p['id'])} scrub at {nxt:%a %d %b %H:%M}"
    return None


# --------------------------------------------------------------------------- commands

def self_check(cfg, state, alerts, dry, heal_ok=True):
    """Check our own files, triggers, tools, API and user. Returns (storage, user_id) or (None, None)."""
    for e in cfg.errors:
        alerts.raise_(f"config:{e}", f"Config problem: {e}", "warning")

    # Our own files. Only heal triggers from templates we can trust.
    own = verify_manifest()
    if own:
        alerts.raise_("manifest", "Files in " + BASE + " don't match the install manifest: "
                      + "; ".join(own) + ". Not self-healing until install.sh is re-run.")
        heal_ok = False
    else:
        alerts.clear("manifest")

    if heal_ok:
        fixed = heal(dry)
        if fixed:
            alerts.info("Self-heal: " + "; ".join(fixed) + ".")
    problems = trigger_problems() if not dry else []
    if problems and heal_ok:
        alerts.raise_("triggers", "Triggers still broken after self-heal: " + "; ".join(problems))
    elif problems:
        alerts.raise_("triggers", "Triggers broken (not healed): " + "; ".join(problems))
    else:
        alerts.clear("triggers")

    # Tools we rely on but do not own: never install, only report.
    missing = [tool for tool in ("smartctl", "lsblk", "runuser", "psql", "systemctl")
               if not shutil.which(tool)]
    if missing:
        alerts.raise_("tools", "Missing tools on the NAS: " + ", ".join(missing)
                      + ". Not installing anything; a firmware update may have removed them.")
    else:
        alerts.clear("tools")

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

    if not cfg.yes("ENABLED"):
        if not state.get("disabled"):
            removed = remove_triggers(dry)
            alerts.info("ENABLED=no in config: removed triggers (" + (", ".join(removed) or "none")
                        + "). Set ENABLED=yes and run install.sh to turn it back on.")
            state["disabled"] = True
        alerts.flush()
        save_state(state, dry)
        return 0
    state.pop("disabled", None)

    # The cron line is only a backup: if the timer is healthy and ran recently, just heal and leave.
    if args.source == "cron":
        last = parse_time(state.get("last_tick"))
        if last and t - last < dt.timedelta(hours=2) and not trigger_problems():
            return 0
        log("cron backup running a full tick (timer missing or late)")

    storage, user_id = self_check(cfg, state, alerts, dry)
    if storage is not None and user_id:
        drives = evaluate_drives(cfg, storage, user_id, state, alerts)
        gate = start_gate(cfg, storage, drives, state, t)
        due = sorted((d for d in drives if not d["excluded"] and d.get("due") and d["due"] <= t
                      and not d.get("error")), key=lambda d: (d["interval"], d["due"]))
        if gate:
            if due and gate not in ("outside start window",):
                log(f"{len(due)} drive(s) due; not starting: {gate}")
        else:
            for d in due:
                busy = cfg.int("BUSY_KBPS")
                if busy and d["kbps"] > busy:
                    log(f"slot {d['slot']} ({d['serial']}) due; not starting: drive busy ({d['kbps']} KB/s)")
                    continue
                conflict = scrub_conflict(cfg, storage, d["minutes"], t)
                if conflict:
                    log(f"slot {d['slot']} ({d['serial']}) due; not starting: {conflict}")
                    continue
                start_test(cfg, d, user_id, state, alerts, dry, t)
                break
        if state.pop("version_changed", False):
            alerts.info("Self-check after the update passed: API, identity and triggers all OK."
                        if not alerts.has_errors() else "Self-check after the update found problems (see alerts).")
    state["last_tick"] = t.isoformat()
    alerts.flush()
    if not dry:
        if alerts.has_errors():
            ping(cfg["HEARTBEAT_FAIL_URL"])
        else:
            ping(cfg["HEARTBEAT_URL"])
    save_state(state, dry)
    return 1 if alerts.has_errors() else 0


def start_test(cfg, d, user_id, state, alerts, dry, t):
    label = f"slot {d['slot']} ({d['serial']}, {d['model']})"
    if dry:
        print(f"(dry-run) would start an extended test on {label}: {d['reason']}")
        return
    try:
        status, body = api("POST", f"/storage/disks/{d['slot']}/smart-tests", user_id, {"type": "extended"})
    except ApiError as e:
        alerts.raise_("api:start", f"Couldn't start a test on {label}: {e.detail}")
        return
    if status in (200, 201, 202):
        alerts.clear("api:start")
        log(f"started extended test on {label}: {d['reason']}")
        nk = night_key(cfg, t)
        state["starts"] = {nk: state.get("starts", {}).get(nk, 0) + 1}
        state.setdefault("drives", {}).setdefault(d["serial"], {})["attrs_at_last_test"] = d.get("attrs", {})
    elif status == 409:
        log(f"{label}: a test is already running (409); skipping")
    else:
        alerts.raise_("api:start", f"Starting a test on {label} returned HTTP {status}: {json.dumps(body)[:200]}")


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
    print("Extended SMART tests\n")
    for d in sorted(drives, key=lambda d: int(d["slot"]) if str(d["slot"]).isdigit() else 99):
        last = parse_time(d.get("last"))
        due = d.get("due")
        if d.get("running"):
            nxt = "running now"
        elif d["excluded"]:
            nxt = "never (excluded)"
        elif due and due <= t:
            nxt = "due now"
        else:
            nxt = f"{due:%a %d %b %Y}" if due else "unknown"
        print(f"Slot {d['slot']}  {d['model']}  ({d['serial']})")
        print(f"  Last test: {f'{last:%a %d %b %Y}' if last else 'never'}")
        print(f"  Next test: {nxt}")
        print(f"  Why:       {d.get('reason', '')}")
        print()
    gate = start_gate(cfg, storage, drives, state, t)
    print(f"Tests start between {cfg['START_WINDOW'].replace('-', ' and ')}.")
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
    missing = [t for t in ("smartctl", "lsblk", "runuser", "psql", "systemctl") if not shutil.which(t)]
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
    own = verify_manifest()
    if own and not args.force:
        print("Not healing; own files don't match the manifest: " + "; ".join(own))
        return 1
    fixed = heal(args.dry_run)
    print("\n".join(fixed) or "Triggers already OK.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("run", "status", "check", "heal"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--source", default="manual", choices=("timer", "cron", "manual"))
    ap.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if os.geteuid() != 0:
        print("Run as root.", file=sys.stderr)
        return 2
    cfg = Config(CONFIG_FILE)
    # status and check only read, so they never wait. run and heal change state
    # and take the lock: scheduled runs skip if one is already going, manual
    # ones wait for it (a run normally takes a few seconds).
    if args.command in ("run", "heal"):
        lock = open(LOCK_FILE, "w")
        deadline = time.monotonic() + (0 if args.source in ("timer", "cron") else 120)
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print("Another run is in progress; skipping this one.")
                    return 0
                time.sleep(1)
    return {"run": cmd_run, "status": cmd_status, "check": cmd_check, "heal": cmd_heal}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
