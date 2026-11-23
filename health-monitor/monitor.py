"""health-monitor's rules: one sample in, alerts out. Pure, no I/O, so the
tests drive it with fixtures (docs/health-monitor-design.md).

    actions = sample(cfg, state, readings, t)

cfg is {KEY: typed value} (Config.typed). state is the tool's state dict and
is updated in place. readings is what health_monitor.py read this time:

    {"smart": {serial: {"drive": 3 or None, "asleep": bool, "error": str or None,
                        "attrs": {id: {"raw": int, "value": int, "thresh": int}}}},
     "btrfs": {device: {"mount": path, "counters": {"corruption_errs": int, ...}}} or None,
     "md":    {name: {"state": str, "degraded": int, "sync_action": str,
                      "mismatch_cnt": int, "level": str, "data": bool}} or None}

None for btrfs or md means it couldn't be read this time: nothing about it
changes. The result is a list of ("raise", key, message, level) and
("clear", key, message) for the caller's Alerts.

State it keeps:

    state["baselined"]  when the first sample was taken (pre-fail checks run then only)
    state["smart"]      {serial: {id: last raw value}}
    state["btrfs"]      {device: {counter: last value}}
    state["md"]         {name: last sync_action}
    state["rises"]      {alert key: {"msg", "level", "opened", "last"}}: open
                        SMART, btrfs and pre-fail alerts, cleared after
                        QUIET_DAYS with no further rise
"""

import datetime as dt

# Raw-count pre-fail limits at install, by attribute (settings).
PREFAIL_KEYS = {5: "PREFAIL_S5", 187: "PREFAIL_S187", 197: "PREFAIL_S197", 198: "PREFAIL_S198"}
BTRFS_ERROR = ("corruption_errs",)          # a rise in these is an error, the rest a warning
BTRFS_WORDS = {"write_io_errs": "write", "read_io_errs": "read", "flush_io_errs": "flush",
               "corruption_errs": "corruption", "generation_errs": "generation"}
MD_BAD_STATES = ("inactive", "broken", "suspended")


def drive_name(serial, r):
    return f"Drive {r['drive']}" if r.get("drive") is not None else serial


def _iso(t):
    return t.isoformat()


def _parse(s):
    try:
        return dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def sample(cfg, state, readings, t):
    actions = []
    first = "baselined" not in state
    actions += _smart(cfg, state, readings.get("smart") or {}, t, first)
    if readings.get("btrfs") is not None:
        actions += _btrfs(cfg, state, readings["btrfs"], t)
    if readings.get("md") is not None:
        actions += _md(cfg, state, readings["md"])
    actions += _expire(cfg, state, t)
    state.setdefault("baselined", _iso(t))
    return actions


# ------------------------------------------------------------------ rises

def _rise(cfg, state, key, msg, level, t):
    """Open or refresh a rise alert. Returns the level it ends up at."""
    rises = state.setdefault("rises", {})
    o = rises.get(key)
    if o and o.get("level") == "error":
        level = "error"
    rises[key] = {"msg": msg, "level": level, "opened": (o or {}).get("opened", _iso(t)), "last": _iso(t)}
    return level


def _expire(cfg, state, t):
    quiet = dt.timedelta(days=cfg["QUIET_DAYS"])
    out = []
    for key, o in sorted((state.get("rises") or {}).items()):
        last = _parse(o.get("last"))
        if last is None or t - last >= quiet:
            out.append(("clear", key, f"No further rise in {cfg['QUIET_DAYS']} days: {o.get('msg', key)}"))
            del state["rises"][key]
    return out


# ------------------------------------------------------------------ SMART

def _smart(cfg, state, readings, t, first):
    watched = [int(i) for i in cfg["WATCH_ATTRIBUTES"]]
    excluded = set(cfg["EXCLUDE_SERIALS"])
    last = state.setdefault("smart", {})
    rises = state.get("rises") or {}
    out = []
    for serial in sorted(readings):
        r = readings[serial]
        if serial in excluded or r.get("asleep") or r.get("error"):
            continue
        attrs = r.get("attrs") or {}
        name = drive_name(serial, r)
        now = {str(i): attrs[i]["raw"] for i in watched if i in attrs}
        prev = last.get(serial)
        if prev is None:
            last[serial] = now
            if first:
                why = prefail(cfg, attrs)
                if why:
                    msg = f"{name} looks close to failing: {'; '.join(why)} ({serial})"
                    level = _rise(cfg, state, f"prefail:{serial}", msg, "warning", t)
                    out.append(("raise", f"prefail:{serial}", msg, level))
            continue
        for k, new in now.items():
            old = prev.get(k)
            if old is not None and new > old:
                key = f"smart:{serial}:{k}"
                again = key in rises
                jump = new - old >= cfg["RISE_ERROR_JUMP"]
                level = "error" if again or jump else "warning"
                note = (f", rose again within {cfg['QUIET_DAYS']} days" if again
                        else f", up {new - old} at once" if jump else "")
                msg = f"{name} - S{k} {old}->{new}{note} ({serial})"
                level = _rise(cfg, state, key, msg, level, t)
                out.append(("raise", key, msg, level))
        prev.update(now)
    return out


def prefail(cfg, attrs):
    """Reasons a drive already looks close to failing, from all its attributes:
    a normalized value within PREFAIL_MARGIN of the manufacturer's threshold,
    or a raw count at or above its limit."""
    why = []
    margin = cfg["PREFAIL_MARGIN"]
    for i in sorted(attrs):
        a = attrs[i]
        thresh, value = a.get("thresh") or 0, a.get("value")
        if thresh > 0 and value is not None and value - thresh <= margin:
            why.append(f"S{i} at {value}, threshold {thresh}")
        elif i in PREFAIL_KEYS and a.get("raw", 0) >= cfg[PREFAIL_KEYS[i]]:
            why.append(f"S{i} at {a['raw']}, limit {cfg[PREFAIL_KEYS[i]]}")
    return why


# ------------------------------------------------------------------ btrfs

def _btrfs(cfg, state, readings, t):
    last = state.setdefault("btrfs", {})
    out = []
    for dev in sorted(readings):
        r = readings[dev]
        counters = r.get("counters") or {}
        prev = last.get(dev)
        if prev is not None:
            for c in sorted(counters):
                old, new = prev.get(c), counters[c]
                if old is not None and new > old:
                    key = f"btrfs:{dev}:{c}"
                    msg = f"btrfs {BTRFS_WORDS.get(c, c)} errors on {r.get('mount') or dev}: {old}->{new}"
                    level = _rise(cfg, state, key, msg, "error" if c in BTRFS_ERROR else "warning", t)
                    out.append(("raise", key, msg, level))
        last[dev] = dict(counters)
    return out


# ------------------------------------------------------------------ md

def _md(cfg, state, readings):
    last = state.setdefault("md", {})
    out = []
    for name in sorted(readings):
        r = readings[name]
        was = last.get(name)
        action = r.get("sync_action")
        # A parity check that finished since the last sample: say what it found, once.
        if was == "check" and action == "idle":
            n = r.get("mismatch_cnt") or 0
            if n > 0:
                out.append(("raise", f"md:{name}:mismatch",
                            f"Parity check on {name} found {n:,} mismatched sectors", "warning"))
            else:
                out.append(("clear", f"md:{name}:mismatch", f"Parity check on {name} found no mismatches"))
        last[name] = action
        key = f"md:{name}:state"
        bad = r.get("data") and (r.get("degraded", 0) > 0 or r.get("state") in MD_BAD_STATES)
        if cfg["MD_STATE_ALERTS"] and bad:
            what = (f"{r['degraded']} member(s) missing" if r.get("degraded", 0) > 0
                    else f"state {r.get('state')}")
            more = ", rebuilding" if action in ("recover", "resync") else ""
            out.append(("raise", key, f"Array {name} ({r.get('level', 'md')}) is degraded: {what}{more}", "error"))
        elif not cfg["MD_STATE_ALERTS"]:
            out.append(("clear", key, f"Array alerts turned off; {name} is no longer watched"))
        else:
            out.append(("clear", key, f"Array {name} has recovered"))
    return out
