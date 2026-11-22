"""smart-scheduler's scheduling rules, shared by the hourly run and the TUI.

Everything here is pure: no API calls, no smartctl, no state writes. The
hourly run gathers facts (smart_scheduler.evaluate_drives) and asks
`decide()` whether to start a test right now. The calendar asks `plan()`
for the nights ahead. Both go through the same `_tick()`, so what the
calendar shows is what the run will do, as long as nothing on the NAS changes.

`cfg` is anything with the smart_scheduler.Config interface: `.window`
(start, end) times, `.int(key)` and `.float(key)`.

Extension points, so later rules plug in without restructuring:
  - `windows`: extra `Window`s (start, end, label, kind) during which no test
    may be running, e.g. backup or replication jobs (#5). Scrubs are added
    automatically from the storage response.
  - `not_before`: {serial: (datetime, reason)} or {serial: datetime}; that
    drive is not started before the given time, e.g. a snooze (#6).

Standard library only.
"""

import calendar
import datetime as dt
from collections import namedtuple

# How far plan() looks ahead at most, whatever the horizon rule says.
MAX_NIGHTS = 400
# The extended test length assumed when smartctl doesn't report one.
DEFAULT_MINUTES = 720
# The hourly timer's period.
TICK = dt.timedelta(hours=1)


# A period during which no SMART test may be running.
Window = namedtuple("Window", "start end label kind")
# The run's answer for one moment. `start` is the drive to start (or None),
# `gate` why nothing may start at all (or None), `due` every due drive in
# priority order, `skipped` [(drive, reason)] for due drives passed over.
Decision = namedtuple("Decision", "start gate due skipped")


class Plan(list):
    """A list of planned test entries (dicts), oldest first, plus `.skipped`:
    [{"night", "reasons"}] for nights where a drive was due but none started."""

    def __init__(self, entries=(), skipped=()):
        super().__init__(entries)
        self.skipped = list(skipped)


# --------------------------------------------------------------------------- time helpers

def parse_time(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def in_window(cfg, t):
    start, end = cfg.window
    cur = t.time()
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end


def night_key(cfg, t):
    """The date a night is filed under: the date its start window opens."""
    start, end = cfg.window
    if start > end and t.time() < end:
        return (t - dt.timedelta(days=1)).date().isoformat()
    return t.date().isoformat()


def night_bounds(cfg, day, tz):
    """(window opens, window closes) for the night filed under `day`."""
    start, end = cfg.window
    opens = dt.datetime.combine(day, start, tzinfo=tz)
    closes = dt.datetime.combine(day + dt.timedelta(days=1) if start > end else day, end, tzinfo=tz)
    return opens, closes


def test_length(cfg, minutes):
    return dt.timedelta(minutes=(minutes or DEFAULT_MINUTES) * cfg.float("DURATION_FACTOR"))


# --------------------------------------------------------------------------- per-drive rules

def is_running(test):
    return not test.get("finishedAt") or str(test.get("status", "")).lower() in RUNNING_STATUSES


def is_good(test):
    return str(test.get("status")).lower() == "successful" and str(test.get("result")).lower() == "optimal"


RUNNING_STATUSES = {"running", "inprogress", "in_progress", "pending", "queued", "testing"}


def assess(cfg, disk, history, attrs, baseline, t):
    """Work out one drive's interval and due date from its facts.

    disk: the drive's entry in GET /storage. history: its SMART test history.
    attrs: {attr id: raw} for the watched attributes. baseline: the attrs
    recorded when the last test started (or None). Returns a dict with
    running, interval, why, due, reason and last.
    """
    extended = [h for h in history if str(h.get("type")).lower() == "extended"]
    finished = sorted((h for h in extended if not is_running(h)),
                      key=lambda h: h.get("finishedAt") or "", reverse=True)
    last_good = next((h for h in finished if is_good(h)), None)
    latest = finished[0] if finished else None
    serial = disk.get("serial")

    warn = [f"SMART attribute {k} is {v}" for k, v in sorted(attrs.items()) if v]
    if disk.get("riskReasons"):
        warn.append("UniFi risk: " + ", ".join(map(str, disk["riskReasons"])))
    if disk.get("state") not in (None, "optimal"):
        warn.append(f"UniFi state {disk.get('state')}")
    increased = [k for k, v in attrs.items() if baseline is not None and v > baseline.get(k, 0)]
    # `source` and `cause` are the short words screens show (docs/tui-style.md
    # §8, smart-scheduler vocabulary); `why` and `reason` are the long forms
    # for the CLI and the log.
    if serial in cfg.overrides:
        interval, why, source = cfg.overrides[serial], "set in config", "per drive"
    elif warn:
        interval, why, source = cfg.int("WARN_INTERVAL_DAYS"), "; ".join(warn), "warning signs"
    elif (disk.get("powerOnHours") or 0) >= cfg.int("OLD_DRIVE_HOURS"):
        interval, why, source = cfg.int("OLD_INTERVAL_DAYS"), f"{disk['powerOnHours']:,} power-on hours", "old drive"
    else:
        interval, why, source = cfg.int("INTERVAL_DAYS"), "healthy", "default"

    if latest is not None and not is_good(latest):
        due = parse_time(latest["finishedAt"]) + dt.timedelta(days=cfg.int("RETRY_FAILED_DAYS"))
        reason, cause = f"retrying: last test ended {latest.get('status')}/{latest.get('result')}", "retry"
    elif last_good is None:
        due, reason, cause = t, "never tested; first test sets a baseline", "baseline"
    elif increased:
        due, cause = t, "warning signs"
        reason = "SMART attribute rose since last test: " + ", ".join(
            f"{k} went {baseline.get(k, 0)} -> {attrs[k]}" for k in increased)
    else:
        due = parse_time(last_good["finishedAt"]) + dt.timedelta(days=interval)
        reason = f"every {interval} days ({why})"
        cause = "per-drive interval" if source == "per drive" else "due"
    return {"running": any(is_running(h) for h in history), "interval": interval, "why": why,
            "source": source, "cause": cause, "rose": sorted(increased),
            "due": due, "reason": reason, "last": last_good.get("finishedAt") if last_good else None}


def priority(d):
    """Shorter interval first (drives with warning signs), then the longest overdue."""
    return d["interval"], d["due"]


# --------------------------------------------------------------------------- blocking windows

def _add_months(t, months, day_of_month):
    y, m = divmod(t.month - 1 + months, 12)
    y, m = t.year + y, m + 1
    last = calendar.monthrange(y, m)[1]
    if str(day_of_month).upper() == "L":
        day = last
    elif str(day_of_month).isdigit():
        day = min(int(day_of_month), last)
    else:
        day = min(t.day, last)
    return t.replace(year=y, month=m, day=day)


def scrub_runs(scrub, until):
    """Scheduled scrub start times from `nextRun` up to `until`.

    UniFi only reports the next run; later ones are projected from the
    schedule's frequency (day, week or month, with month's dayOfMonth,
    including "L" for the last day). An unknown unit projects nothing.
    """
    nxt = parse_time(scrub.get("nextRun"))
    schedule = scrub.get("schedule") or {}
    if not schedule.get("enabled") or not nxt:
        return []
    freq = schedule.get("frequency") or {}
    unit, every = str(freq.get("unit", "")).lower(), freq.get("interval") or 1
    if not isinstance(every, int) or every < 1:
        every = 1
    runs, k = [], 0
    while True:
        if unit == "day":
            t = nxt + dt.timedelta(days=every * k)
        elif unit == "week":
            t = nxt + dt.timedelta(weeks=every * k)
        elif unit == "month":
            t = _add_months(nxt, every * k, freq.get("dayOfMonth", nxt.day)) if k else nxt
        else:
            t = nxt if k == 0 else None
        if t is None or t > until or k > 1000:
            return runs
        runs.append(t)
        k += 1


def scrub_windows(cfg, storage, until, whole_night=False):
    """A Window per scheduled scrub whose window opens before `until`: from
    SCRUB_MARGIN_HOURS before it starts until an hour after (UniFi's nextRun
    can be an hour early across a DST change). After that the run's live
    "pool is scrubbing" check takes over. plan() can't see that for future
    nights, so with `whole_night` the window lasts until that night's start
    window closes."""
    margin = dt.timedelta(hours=cfg.int("SCRUB_MARGIN_HOURS"))
    out = []
    for p in storage["pools"]:
        for t in scrub_runs(p.get("dataScrubbing") or {}, until + margin):
            end = t + dt.timedelta(hours=1)
            if whole_night:
                end = max(end, night_bounds(cfg, dt.date.fromisoformat(night_key(cfg, t)), t.tzinfo)[1])
            out.append(Window(t - margin, end,
                              f"pool {p.get('number', p['id'])} scrub at {t:%a %d %b %H:%M}", "scrub"))
    return out


def conflict(cfg, windows, minutes, t):
    """The first window a test of this length started at `t` would run into, as a reason."""
    end = t + test_length(cfg, minutes)
    for w in sorted(windows, key=lambda w: w.start):
        if t < w.end and end > w.start:
            return f"would overlap {w.label}"
    return None


def blocking_windows(cfg, storage, until, extra=(), whole_night=False):
    return scrub_windows(cfg, storage, until, whole_night) + list(extra or ())


# --------------------------------------------------------------------------- the gate

def pool_problem(storage):
    for p in storage["pools"]:
        name = p.get("number", p["id"])
        if p.get("status") != "fullyOperational":
            return f"pool {name} is {p.get('status')}"
        for rg in p.get("raidGroups") or []:
            if rg.get("currentProtection", 1) < rg.get("expectedProtection", 0) or rg.get("progress"):
                return f"pool {name} RAID group is degraded or resyncing"
        if (p.get("dataScrubbing") or {}).get("status", "idle") != "idle":
            return f"pool {name} is scrubbing"
    return None


def gate(cfg, storage, running, starts_tonight, t):
    """None if a test may start at `t`, else why not. `storage` None skips the
    live pool checks (used for future nights, where we assume pools are fine)."""
    if not in_window(cfg, t):
        return "outside start window"
    if cfg.int("MAX_STARTS_PER_NIGHT") == 0:
        return "paused (MAX_STARTS_PER_NIGHT=0 in config)"
    if starts_tonight >= cfg.int("MAX_STARTS_PER_NIGHT"):
        return "already started tonight's test(s)"
    if running >= cfg.int("MAX_CONCURRENT"):
        return "a SMART test is already running"
    if storage is not None:
        return pool_problem(storage)
    return None


def start_gate(cfg, storage, drives, state, t):
    """The run's gate: None if a test may start now, otherwise the reason it may not."""
    return gate(cfg, storage, sum(1 for d in drives if d.get("running")),
                state.get("starts", {}).get(night_key(cfg, t), 0), t)


# --------------------------------------------------------------------------- one tick

def _not_before(not_before, serial):
    value = (not_before or {}).get(serial)
    if value is None:
        return None, None
    if isinstance(value, tuple):
        return value[0], value[1]
    return value, f"not before {value:%a %d %b %Y}"


def _due(drives, t):
    """Drives the run counts as due at `t`, most urgent first."""
    return sorted((d for d in drives if not d.get("excluded") and not d.get("error")
                   and d.get("due") and d["due"] <= t), key=priority)


def _tick(cfg, drives, t, *, storage, running, starts_tonight, windows, not_before, live):
    """One hourly decision. Returns a Decision. `live` applies facts that are
    only known for right now (drive busy); `storage` None skips pool checks."""
    due = _due(drives, t)
    g = gate(cfg, storage, running, starts_tonight, t)
    if g:
        return Decision(None, g, due, [])
    skipped = []
    busy = cfg.int("BUSY_KBPS")
    for d in due:
        if d.get("running"):
            skipped.append((d, "its test is still running"))
            continue
        until, why = _not_before(not_before, d["serial"])
        if until is not None and t < until:
            skipped.append((d, why))
            continue
        if live and busy and d.get("kbps", 0) > busy:
            skipped.append((d, f"drive busy ({d['kbps']} KB/s)"))
            continue
        c = conflict(cfg, windows, d.get("minutes"), t)
        if c:
            skipped.append((d, c))
            continue
        return Decision(d, None, due, skipped)
    return Decision(None, None, due, skipped)


def decide(cfg, storage, drives, state, t, windows=(), not_before=None):
    """Should the hourly run start a test at `t`, and on which drive?

    drives: smart_scheduler.evaluate_drives output. windows: extra blocking
    Windows (scrubs are added here). not_before: per-serial earliest start.
    """
    longest = max((test_length(cfg, d.get("minutes")) for d in drives), default=dt.timedelta(0))
    return _tick(cfg, drives, t, storage=storage,
                 running=sum(1 for d in drives if d.get("running")),
                 starts_tonight=state.get("starts", {}).get(night_key(cfg, t), 0),
                 windows=blocking_windows(cfg, storage, t + longest, windows),
                 not_before=not_before, live=True)


# --------------------------------------------------------------------------- the plan

def _ticks(cfg, day, tz, after):
    opens, closes = night_bounds(cfg, day, tz)
    t = opens
    if after > opens:
        t = after if after < closes else closes
    while t < closes:
        yield t
        t += TICK


def plan(storage, drives, state, cfg, now, horizon=None, windows=(), not_before=None):
    """Simulate the hourly run night by night from `now`.

    Assumes every planned test passes, so the drive's next due date moves to
    its estimated end plus its interval. Live facts (pool health, scrub in
    progress, busy drives, tests already started tonight) count for tonight
    only; later nights assume a healthy, idle NAS, and a night in which a
    scrub starts stays blocked until its start window closes (tonight, the
    run's live "pool is scrubbing" check does that job instead).

    horizon: None plans until every drive that can be planned (not excluded,
    history readable) has one test; a datetime plans every night up to it.
    Returns a Plan: entries {serial, slot, model, night, start, end, due,
    interval, reason, delayed_by: [reasons]} plus `.skipped` nights.
    """
    if cfg.int("MAX_STARTS_PER_NIGHT") == 0:
        return Plan(skipped=[{"night": night_key(cfg, now), "reasons": ["paused (MAX_STARTS_PER_NIGHT=0 in config)"]}])

    sim = []
    for d in drives:
        s = dict(d)
        if s.get("running"):
            # Assume the running test passes and ends after its estimated length
            # (scaled by what the drive says is left). It blocks other starts even
            # on an excluded drive or one whose history couldn't be read.
            length = test_length(cfg, s.get("minutes"))
            if isinstance(s.get("remaining_percent"), int):
                length = length * max(s["remaining_percent"], 10) / 100
            s["busy_until"] = now + length
            if not s.get("excluded") and not s.get("error") and s.get("interval"):
                s["due"] = s["busy_until"] + dt.timedelta(days=s["interval"])
                s["reason"] = f"every {s['interval']} days ({s.get('why', 'after the running test')})"
                s["cause"] = "per-drive interval" if s.get("source") == "per drive" else "due"
        s["running"] = False
        sim.append(s)
    wanted = {s["serial"] for s in sim if not s.get("excluded") and not s.get("error") and s.get("due")}

    last_night = dt.date.fromisoformat(night_key(cfg, now)) + dt.timedelta(days=MAX_NIGHTS)
    until = horizon if horizon is not None else night_bounds(cfg, last_night, now.tzinfo)[1]
    longest = max((test_length(cfg, s.get("minutes")) for s in sim), default=dt.timedelta(0))
    # Tonight uses the run's own windows (its live scrub check covers the rest).
    live_blocks = blocking_windows(cfg, storage, until + longest, windows)
    blocks = blocking_windows(cfg, storage, until + longest, windows, whole_night=True)

    entries, skipped, delays, planned = [], [], {}, set()
    starts = dict(state.get("starts", {}))
    tonight = night_key(cfg, now)
    max_starts = cfg.int("MAX_STARTS_PER_NIGHT")
    day = dt.date.fromisoformat(tonight)
    while day <= last_night:
        if horizon is None and wanted <= planned:
            break
        nk = day.isoformat()
        live = nk == tonight
        started, reasons = 0, []
        for t in _ticks(cfg, day, now.tzinfo, now):
            if t > until:
                break
            for s in sim:
                s["running"] = bool(s.get("busy_until") and s["busy_until"] > t)
            dec = _tick(cfg, sim, t, storage=storage if live else None,
                        running=sum(1 for s in sim if s["running"]),
                        starts_tonight=starts.get(nk, 0), windows=live_blocks if live else blocks,
                        not_before=not_before, live=live)
            notes = [(d, dec.gate) for d in dec.due if dec.gate] + dec.skipped
            if dec.start:
                notes += [(d, f"waiting its turn ({max_starts} start(s) per night, most urgent first)")
                          for d in dec.due if d is not dec.start and not any(d is x for x, _ in notes)]
            for d, why in notes:
                if why == "already started tonight's test(s)":
                    why = f"waiting its turn ({max_starts} start(s) per night, most urgent first)"
                if why not in delays.setdefault(d["serial"], []):
                    delays[d["serial"]].append(why)
                if why not in reasons and not why.startswith("waiting its turn"):
                    reasons.append(why)
            d = dec.start
            if d is None:
                continue
            end = t + test_length(cfg, d.get("minutes"))
            entries.append({"serial": d["serial"], "slot": d["slot"], "model": d.get("model", "?"),
                            "night": nk, "start": t, "end": end, "due": d["due"],
                            "interval": d["interval"], "reason": d["reason"], "cause": d.get("cause") or "due",
                            "delayed_by": delays.pop(d["serial"], [])})
            planned.add(d["serial"])
            starts[nk] = starts.get(nk, 0) + 1
            started += 1
            d["busy_until"] = end
            d["due"] = end + dt.timedelta(days=d["interval"])
            d["reason"] = f"every {d['interval']} days ({d.get('why', 'after the planned test')})"
            d["cause"] = "per-drive interval" if d.get("source") == "per drive" else "due"
        if not started and reasons:
            skipped.append({"night": nk, "reasons": reasons})
        if horizon is not None and night_bounds(cfg, day, now.tzinfo)[0] > horizon:
            break
        day += dt.timedelta(days=1)
    return Plan(entries, skipped)
