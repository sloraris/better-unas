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

Options:
  --dry-run   change nothing: no tests, no healing, no alerts, no state writes
  --source    who started this run (timer, cron, manual); set by the triggers
  --note      with snooze: why, shown by status
  --override  with run-now: start despite checks that can be overridden

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
sys.path.insert(0, BASE)
import planner  # noqa: E402  (lives next to this file)
import backups  # noqa: E402
import settings  # noqa: E402
if __name__ == "__main__":
    # actions.py imports this module by name; give it this copy, not a second one.
    sys.modules.setdefault("smart_scheduler", sys.modules[__name__])
CONFIG_FILE = os.path.join(BASE, "config")
# The suite's shared settings (webhook, heartbeat, alert repeat) are in the
# home's better-unas.conf; this tool's config is the fallback (Config below).
CORE_CONFIG_FILE = os.path.join(os.path.dirname(BASE), "better-unas.conf")
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

# Every setting, its default, type and range live in settings.json (the
# plugin contract's settings schema). Adding a setting means adding it there.
SCHEMA_FILE = os.path.join(BASE, "settings.json")
SCHEMA = settings.load_schema(SCHEMA_FILE)
DEFAULTS = settings.defaults(SCHEMA)
# The shared settings as the core's schema has them (core/settings.json),
# minus the help text. Tools don't import the core, so they're repeated here.
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

RUNNING_STATUSES = planner.RUNNING_STATUSES


# --------------------------------------------------------------------------- helpers

def now():
    return dt.datetime.now().astimezone()


parse_time = planner.parse_time


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
    """The config file, checked against settings.json (see settings.py), plus
    the suite's shared settings (SHARED_SCHEMA): each read from
    better-unas.conf when it sets the key, else from this config.

    values holds each setting's text in canonical form ("yes", "5,187");
    typed holds the parsed values. A bad or unknown line is reported in errors
    ("KEY: message") and the default is used instead.
    """

    def __init__(self, path, schema=None, core_path=None):
        self.schema = schema or SCHEMA
        raw, problems = settings.read(path)
        self.typed, self.field_errors = settings.validate(self.schema, raw)
        self.errors = [f"config {p}" for p in problems]
        core, core_problems = settings.read(core_path or CORE_CONFIG_FILE)
        self.errors += [f"better-unas.conf {p}" for p in core_problems]
        shared_raw = {k: core[k] if k in core else raw[k]
                      for k in settings.SHARED_KEYS if k in core or k in raw}
        shared, shared_errors = settings.validate(SHARED_SCHEMA, shared_raw)
        self.typed.update(shared)
        self.field_errors.update(shared_errors)
        self.errors += [f"{k}: {m}" for k, m in self.field_errors.items()]
        self.values = {f["key"]: settings.format_value(f, self.typed[f["key"]])
                       for f in settings.fields(self.schema) + settings.fields(SHARED_SCHEMA)}
        self.overrides = dict(self.typed["INTERVAL_OVERRIDES"])
        self.window = self.typed["START_WINDOW"]

    def __getitem__(self, k):
        return self.values[k]

    def int(self, k):
        return int(self.values[k])

    def float(self, k):
        return float(self.values[k])

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
            # Kept so the TUI's Alerts screen can list recent alerts with resolve times.
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
            m = self.outbox[0]
            if not self._send(url, m):
                log(f"webhook unreachable; {len(self.outbox)} message(s) kept for the next run",
                    syslog.LOG_WARNING)
                return
            self.outbox.pop(0)

    def _send(self, url, m):
        return send_webhook(url, self.cfg["WEBHOOK_FORMAT"], m, self.host) is None


RESOLVED_KEEP = 20       # resolved alerts kept in state for the TUI's Alerts screen


def webhook_request(url, fmt, m, host):
    """The POST for one outbox message m ({level, msg, at}) in WEBHOOK_FORMAT fmt.
    core/notify.py builds the TUI's test the same way."""
    title = f"{host} {NAME}: {m['level']}"
    fmt = (fmt or "text").lower()
    if fmt == "json":
        body, ctype = json.dumps({"host": host, "source": NAME, "level": m["level"],
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
    return req


def http_error(e):
    """A short description of a failed request, for the journal and the TUI."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code} {e.reason}".strip()
    if isinstance(e, urllib.error.URLError):
        e = e.reason
    if isinstance(e, socket.timeout) or "timed out" in str(e):
        return "timed out"
    return str(e) or type(e).__name__


# Some hosts (Discord, others behind Cloudflare) refuse urllib's default
# "Python-urllib/3.x" with HTTP 403, so webhooks and pings name themselves.
USER_AGENT = f"{NAME} (better-unas)"


def open_url(req, timeout=10):
    """Fetch req (a URL or Request). Returns None on a 2xx answer, else a short error."""
    if isinstance(req, str):
        req = urllib.request.Request(req)
    if not req.has_header("User-agent"):
        req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            if 200 <= r.status < 300:
                return None
            return f"HTTP {r.status} {r.reason}".strip()
    except urllib.error.HTTPError as e:
        # The answer's first line, when it's short text, says why a host refused
        # (Cloudflare's "error code: 1010").
        try:
            first = (e.read(200).decode("utf-8", "replace").strip().splitlines() or [""])[0].strip()
        except Exception:
            first = ""
        why = http_error(e)
        return why + (f" ({first})" if first and len(first) <= 60 and not first.startswith(("<", "{", "[")) else "")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return http_error(e)


def send_webhook(url, fmt, m, host=None, timeout=10):
    """POST one message to the webhook. Returns None if it was accepted, else why not."""
    try:
        req = webhook_request(url, fmt, m, host or socket.gethostname())
    except ValueError as e:      # not a URL urllib can use
        return str(e)
    return open_url(req, timeout)


def ping(url):
    if not url:
        return
    err = open_url(url)
    if err:
        log(f"heartbeat ping failed: {err}", syslog.LOG_WARNING)


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

# The rules themselves live in planner.py, shared with the TUI's calendar.
in_window = planner.in_window
night_key = planner.night_key
start_gate = planner.start_gate


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
                "excluded": serial in excluded, "running": False, "current_test": d.get("smartTest"),
                "kbps": (d.get("readKBPS") or 0) + (d.get("writeKBPS") or 0)}
        result.append(info)
        left = self_test_progress(devices[serial]) if serial in devices else None
        if info["excluded"]:
            info["reason"] = "excluded in config"
            _mark_outside_test(info, left)
            continue
        try:
            history = get_history(slot, serial, user_id, d.get("smartTest"))
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
            elif cfg.yes("NOTIFY_SUCCESS"):
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
    if not cfg.yes("BACKUP_AVOID"):
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


def scrub_conflict(cfg, storage, minutes, t):
    """Return a reason if a test of this length would run into a scheduled scrub."""
    until = t + planner.test_length(cfg, minutes)
    return planner.conflict(cfg, planner.scrub_windows(cfg, storage, until), minutes, t)


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
            alerts.info("Turned off (ENABLED=no): removed the hourly timer and cron line ("
                        + (", ".join(removed) or "none") + "). Turn Enabled back on in Settings to restart it.")
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

    for serial in expire_snoozes(state, t.date()):
        log(f"snooze on {serial} has ended")

    storage, user_id = self_check(cfg, state, alerts, dry)
    if storage is not None and user_id:
        drives = evaluate_drives(cfg, storage, user_id, state, alerts)
        longest = max((planner.test_length(cfg, d.get("minutes")) for d in drives), default=dt.timedelta(0))
        windows, _ = backup_windows(cfg, t, t + longest, alerts)
        decision = planner.decide(cfg, storage, drives, state, t, windows=windows,
                                  not_before=not_before(state, t))
        if decision.gate:
            if decision.due and decision.gate != "outside start window":
                log(f"{len(decision.due)} drive(s) due; not starting: {decision.gate}")
        else:
            for d, why in decision.skipped:
                log(f"Drive {d['slot']} ({d['serial']}) due; not starting: {why}")
            if decision.start:
                start_test(cfg, decision.start, user_id, state, alerts, dry, t)
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
    label = f"Drive {d['slot']} ({d['serial']}, {d['model']})"
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
    horizon = t + dt.timedelta(days=planner.MAX_NIGHTS + 2)
    windows, backup_info = backup_windows(cfg, t, horizon)
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


def print_backups(cfg, info, t):
    if not cfg.yes("BACKUP_AVOID") or info is None:
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
    """The run lock, held until the returned file is closed, or None if it
    stayed busy for `wait` seconds. Anything that writes state takes it."""
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
    ap.add_argument("command", choices=("run", "status", "check", "heal", "snooze", "unsnooze",
                                        "run-now", "exclude", "include", "interval"))
    ap.add_argument("target", nargs="?", help=argparse.SUPPRESS)
    ap.add_argument("date", nargs="?", help=argparse.SUPPRESS)
    ap.add_argument("--note", default="", help=argparse.SUPPRESS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--source", default="manual", choices=("timer", "cron", "manual"))
    ap.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--override", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
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
