"""UniFi Drive backup and replication tasks as windows the planner avoids.

UniFi Drive keeps its backup tasks (UniFi Cloud, UNAS-to-UNAS replication,
local copies) in the `remote_backup_tasks` table of its `unifi-drive`
database and each run in `remote_backup_task_runs`. smart_scheduler.py reads
both read-only through psql and hands the rows to `parse_tasks()` and
`parse_runs()`; `windows()` turns them into planner.Window objects.

**Credentials are never read.** The tables also hold access keys, secrets,
usernames and remote addresses. `task_query()` and `run_query()` select only
the named schedule and timing columns in TASK_COLUMNS / RUN_COLUMNS, and
`shape_problems()` refuses to build a query from anything else.

How long a run takes is estimated in this order:
  1. history: the longest of the task's recent successful or partial runs;
  2. size / speed: the source size over the task's bandwidth limit, else the
     configured upload speed (only when a source size is known);
  3. BACKUP_DEFAULT_HOURS.
Each window then reaches from BACKUP_MARGIN_HOURS before the scheduled start
to BACKUP_MARGIN_HOURS after the estimated end.

Everything here is pure. Standard library only.
"""

import datetime as dt

from planner import Window

TASKS_TABLE = "remote_backup_tasks"
RUNS_TABLE = "remote_backup_task_runs"

# Columns we rely on. Anything else in these tables is never selected.
TASK_COLUMNS = ("id", "remote_type", "schedule_enable", "schedule_first_run_time", "schedule_weekdays",
                "schedule_monthdays", "schedule_interval", "deleted_at")
# Read when present; their absence only loses a nicer label or the bandwidth estimate.
TASK_OPTIONAL = ("name", "bw_limit")
RUN_COLUMNS = ("task_id", "started_at", "finished_at", "status")

# Columns whose names suggest a credential or an address. Never selected, even
# if a later Drive version renames something into the lists above.
FORBIDDEN = ("key", "secret", "password", "passwd", "token", "username", "ip", "host", "credential", "auth")

# How many recent finished runs per task the history estimate looks at.
RECENT_RUNS = 10
# How far back the run query looks.
HISTORY_DAYS = 180
# An unfinished run older than this is treated as a leftover record.
STALE_RUN = dt.timedelta(days=2)

# Verified on hardware (Drive 4.4.x): started_at/finished_at/deleted_at are
# timestamptz; id/task_id uuid; schedule_enable boolean; schedule_interval and
# bw_limit integer; status, remote_type, name and schedule_* text. The other
# accepted types only keep a later change from breaking the read.
TIME_TYPES = ("timestamp with time zone", "timestamp without time zone", "timestamp", "date")
NUMBER_TYPES = ("bigint", "integer", "smallint", "numeric", "double precision", "real")

SHAPE_QUERY = (f"SELECT table_name, column_name, data_type FROM information_schema.columns "
               f"WHERE table_schema = current_schema() AND table_name IN ('{TASKS_TABLE}', '{RUNS_TABLE}')")

# Drive's remote_type values seen on hardware: ucs (UniFi Cloud Storage), unas
# (replication to another UNAS) and local. The rest are fallbacks.
REMOTE_LABELS = {"ucs": "UniFi Cloud backup", "cloud": "UniFi Cloud backup", "unifi_cloud": "UniFi Cloud backup", "unas": "replication",
                 "replication": "replication", "local": "local backup", "smb": "network backup",
                 "nfs": "network backup", "s3": "S3 backup"}


def _forbidden(column):
    parts = column.lower().replace("-", "_").split("_")
    return any(p in FORBIDDEN for p in parts) or any(f in column.lower() for f in ("secret", "password", "key"))


# --------------------------------------------------------------------------- shape and queries

def columns_by_table(rows):
    """{table: {column: data_type}} from SHAPE_QUERY rows."""
    out = {}
    for row in rows:
        if len(row) >= 3:
            out.setdefault(row[0], {})[row[1]] = row[2].lower()
    return out


def shape_problems(cols):
    """What's missing or changed in the tables we read, as a list of strings."""
    problems = []
    for table, wanted in ((TASKS_TABLE, TASK_COLUMNS), (RUNS_TABLE, RUN_COLUMNS)):
        have = cols.get(table)
        if not have:
            problems.append(f"table {table} not found")
            continue
        missing = [c for c in wanted if c not in have]
        if missing:
            problems.append(f"{table} has no column " + ", ".join(missing))
    for table, col in ((TASKS_TABLE, "deleted_at"), (RUNS_TABLE, "started_at"), (RUNS_TABLE, "finished_at")):
        kind = cols.get(table, {}).get(col)
        if kind and kind not in TIME_TYPES + NUMBER_TYPES:
            problems.append(f"{table}.{col} is {kind}, expected a timestamp")
    return problems


def _time_expr(col, kind):
    # Epoch seconds whatever the column type, so DateStyle and time zones don't matter.
    if kind in NUMBER_TYPES:
        return f"{col}::text"
    return f"extract(epoch from {col})::text"


def task_query(cols):
    have = cols[TASKS_TABLE]
    names = [c for c in TASK_COLUMNS if c != "deleted_at"] + [c for c in TASK_OPTIONAL if c in have]
    assert not any(_forbidden(c) for c in names), names
    select = ", ".join(f"{c}::text" for c in names)
    return f"SELECT {select} FROM {TASKS_TABLE} WHERE deleted_at IS NULL ORDER BY id", names


def run_query(cols):
    have = cols[RUNS_TABLE]
    started = _time_expr("started_at", have["started_at"])
    finished = _time_expr("finished_at", have["finished_at"])
    assert not any(_forbidden(c) for c in RUN_COLUMNS)
    if have["started_at"] in NUMBER_TYPES:
        recent = f"started_at > 0"   # can't tell seconds from milliseconds in SQL; filtered in Python
    else:
        recent = f"started_at > now() - interval '{HISTORY_DAYS} days'"
    return (f"SELECT task_id::text, {started}, {finished}, status::text FROM {RUNS_TABLE} "
            f"WHERE {recent} ORDER BY started_at DESC LIMIT 2000")


# --------------------------------------------------------------------------- parsing rows

def _epoch(value, tz):
    if value in (None, ""):
        return None
    try:
        x = float(value)
    except ValueError:
        return None
    if x > 1e11:   # milliseconds
        x /= 1000
    return dt.datetime.fromtimestamp(x, tz)


def _bool(value):
    return str(value).strip().lower() in ("t", "true", "1", "yes", "on")


def parse_tasks(rows, names):
    """Task dicts from task_query rows (columns in `names` order). Disabled tasks are kept
    with enabled=False, so `status` can mention them."""
    tasks = []
    for row in rows:
        r = dict(zip(names, row + [""] * (len(names) - len(row))))
        tasks.append({"id": r["id"], "name": r.get("name") or "", "remote_type": r.get("remote_type") or "",
                      "enabled": _bool(r.get("schedule_enable")), "time": r.get("schedule_first_run_time") or "",
                      "weekdays": r.get("schedule_weekdays") or "", "monthdays": r.get("schedule_monthdays") or "",
                      "interval": r.get("schedule_interval") or "", "bw_limit": r.get("bw_limit") or ""})
    return tasks


def parse_runs(rows, tz):
    """Run dicts {task_id, started, finished, status} from run_query rows."""
    runs = []
    for row in rows:
        if len(row) < 4:
            continue
        runs.append({"task_id": row[0], "started": _epoch(row[1], tz), "finished": _epoch(row[2], tz),
                     "status": row[3].strip().lower()})
    return [r for r in runs if r["started"]]


# --------------------------------------------------------------------------- schedules

def label(task):
    kind = REMOTE_LABELS.get(task["remote_type"].lower(), f"{task['remote_type'] or 'backup'} task")
    return f"{kind} “{task['name']}”" if task["name"] else kind


def _parse_hhmm(text):
    try:
        h, _, m = str(text).strip().partition(":")
        return dt.time(int(h), int(m or 0))
    except ValueError:
        return None


def _day_set(text, lo, hi, wrap=None):
    """Set of ints from "1,3,5", "1-5" or None for "*"/"" (any). `wrap` maps e.g. 7 -> 0."""
    text = str(text).strip()
    if text in ("", "*"):
        return None
    out = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        try:
            a, b = int(a), int(b or a)
        except ValueError:
            raise ValueError(f"can't read {text!r}")
        for x in range(a, b + 1):
            if wrap and x in wrap:
                x = wrap[x]
            if not lo <= x <= hi:
                raise ValueError(f"{x} is out of range in {text!r}")
            out.add(x)
    return out


def schedule_problem(task):
    """Why this enabled task's schedule can't be read, or None."""
    if _parse_hhmm(task["time"]) is None:
        return f"first run time {task['time']!r}"
    try:
        _day_set(task["weekdays"], 0, 6, {7: 0})
        _day_set(task["monthdays"], 1, 31)
    except ValueError as e:
        return str(e)
    if task["interval"] and not str(task["interval"]).strip().isdigit():
        return f"interval {task['interval']!r}"
    return None


def _runs_on(day, weekdays, monthdays):
    wd = (day.weekday() + 1) % 7   # 0 = Sunday, as in cron
    if weekdays is None and monthdays is None:
        return True
    if weekdays is None:
        return day.day in monthdays
    if monthdays is None:
        return wd in weekdays
    return wd in weekdays or day.day in monthdays   # both restricted: cron's "either"


def run_times(task, start, until):
    """Scheduled start times of an enabled task between `start` and `until` (aware datetimes).

    `schedule_first_run_time` is the first start of each scheduled day (local
    time); with an `schedule_interval` in minutes the task repeats through the
    rest of that day. weekdays use 0 = Sunday ("*" or empty = every day);
    monthdays are days of the month (empty = any)."""
    first = _parse_hhmm(task["time"])
    weekdays = _day_set(task["weekdays"], 0, 6, {7: 0})
    monthdays = _day_set(task["monthdays"], 1, 31)
    every = int(task["interval"]) if str(task["interval"]).strip().isdigit() else 0
    tz = start.tzinfo
    out = []
    day = start.date() - dt.timedelta(days=1)   # a run from yesterday may still be going
    while day <= until.date():
        if _runs_on(day, weekdays, monthdays):
            t = dt.datetime.combine(day, first, tzinfo=tz)
            midnight = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tzinfo=tz)
            while t < midnight and t <= until:
                if t >= start:
                    out.append(t)
                if every <= 0:
                    break
                t += dt.timedelta(minutes=every)
        day += dt.timedelta(days=1)
    return out


# --------------------------------------------------------------------------- duration

def _good(status):
    # Drive's run statuses on hardware: "succeeded" and "partially-successful".
    return any(w in status for w in ("succeed", "success", "partial", "complete", "done", "finish"))


def estimate(cfg, task, runs, size_bytes=None):
    """(estimated run length, how it was estimated) for one task."""
    mine = [r for r in runs if r["task_id"] == task["id"] and r["finished"] and _good(r["status"])]
    mine.sort(key=lambda r: r["started"], reverse=True)
    lengths = [r["finished"] - r["started"] for r in mine[:RECENT_RUNS] if r["finished"] >= r["started"]]
    if lengths:
        longest = max(lengths)
        return longest, f"longest of the last {len(lengths)} run(s): {_fmt(longest)}"
    speed, source = bandwidth(cfg, task)
    if size_bytes and speed:
        length = dt.timedelta(seconds=size_bytes / speed)
        return length, f"{size_bytes / 1e9:.1f} GB at {source}: {_fmt(length)}"
    hours = cfg["BACKUP_DEFAULT_HOURS"]
    return dt.timedelta(hours=hours), f"no run history; default {hours:g} h"


def bandwidth(cfg, task):
    """(bytes per second, description) the task can upload at, or (None, None).

    The task's own `bw_limit` wins. Its unit isn't confirmed on hardware; it is
    read as Mbit/s, the unit Drive's UI shows. Then BACKUP_UPLOAD_MBPS."""
    for value, what in ((task.get("bw_limit"), "the task's limit"), (cfg["BACKUP_UPLOAD_MBPS"], "BACKUP_UPLOAD_MBPS")):
        try:
            mbps = float(value)
        except (TypeError, ValueError):
            continue
        if mbps > 0:
            return mbps * 1e6 / 8, f"{mbps:g} Mbit/s ({what})"
    return None, None


def _fmt(td):
    minutes = round(td.total_seconds() / 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes / 60:.1f} h"


# --------------------------------------------------------------------------- windows

def windows(cfg, tasks, runs, start, until, sizes=None):
    """planner.Windows for every enabled task's runs that could touch [start, until].

    Also covers a run that is going on right now (started, not finished), from
    its real start. Overlapping windows of one task are merged, so a task that
    repeats every hour makes one window per day, not twenty-four."""
    margin = dt.timedelta(hours=cfg["BACKUP_MARGIN_HOURS"])
    out = []
    for task in tasks:
        if not task["enabled"] or schedule_problem(task):
            continue
        length, _ = estimate(cfg, task, runs, (sizes or {}).get(task["id"]))
        name = label(task)
        spans = [(t - margin, t + length + margin, f"{name} at {t:%a %d %b %H:%M}")
                 for t in run_times(task, start - length - margin, until + margin)]
        for r in runs:
            # A run with no finish time that started more than STALE_RUN ago is a
            # leftover record (Drive restarted mid-run, say), not a live run.
            if (r["task_id"] == task["id"] and r["finished"] is None
                    and start - STALE_RUN < r["started"] <= start):
                end = max(r["started"] + length, start) + margin
                spans.append((r["started"] - margin, end, f"{name} running since {r['started']:%a %d %b %H:%M}"))
        spans = [s for s in spans if s[1] > start and s[0] <= until]
        spans.sort()
        merged = []
        for s in spans:
            if merged and s[0] <= merged[-1][1]:
                a, b, lab = merged[-1]
                merged[-1] = (a, max(b, s[1]), lab)
            else:
                merged.append(s)
        out += [Window(a, b, lab, "backup") for a, b, lab in merged]
    return sorted(out, key=lambda w: w.start)
