"""health-monitor's plugin module for the better-unas TUI (docs/plugin-contract.md).

No screens of its own: a Home card and the alerts, both from state.json, so
nothing here reads a drive. The 15-minute run never imports this file.
"""

import importlib.util
import os

CONTRACT = 1
HERE = os.path.dirname(os.path.abspath(__file__))
LEVELS = {"error": "error", "warning": "warn", "warn": "warn"}


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hm = _load("better_unas_health_monitor", "health_monitor.py")


def _scheduler_installed():
    """True when smart-scheduler sits next to this tool, so its Drives tab exists."""
    return os.path.isfile(os.path.join(os.path.dirname(HERE), "smart-scheduler", "tool.conf"))


def _level(active):
    if any(a.get("level") == "error" for a in active):
        return "error"
    return "warn" if active else "ok"


def status(ctx):
    """Cheap: config, state and trigger files only."""
    cfg = ctx.config()
    problems = [f"{k}: {msg}" for k, msg in ctx.config_errors().items()]
    if not cfg["ENABLED"]:
        return {"enabled": False, "state": "off", "problems": problems,
                "detail": ["Turned off (ENABLED=no): no checks run."]}
    problems += hm.trigger_problems()
    state = hm.load_state()
    detail = []
    last = hm.parse_time((state.get("last") or {}).get("at"))
    if last:
        detail.append(f"Last check {last:%a %d %b %H:%M}")
    level = _level(list((state.get("alerts") or {}).values()))
    return {"enabled": True, "state": "warn" if level == "ok" and problems else level,
            "problems": problems, "detail": detail}


def _worst(active, test):
    """(key, alert) of the worst open alert whose key passes test, errors first."""
    hits = sorted(((k, a) for k, a in active.items() if test(k)),
                  key=lambda p: (p[1].get("level") != "error", p[0]))
    return (hits[0], len(hits)) if hits else ((None, None), 0)


def _row(label, active, test, quiet):
    """A card line: the worst matching alert, or `quiet` when there's none."""
    (key, a), n = _worst(active, test)
    if a is None:
        return {"label": label, "value": quiet, "status": "ok", "tone": "normal"}, None
    bad = a.get("level") == "error"
    value = a["msg"] + (f" (+{n - 1} more)" if n > 1 else "")
    return {"label": label, "value": value, "status": "failed" if bad else "warning",
            "tone": "bad" if bad else "warn"}, key


def summary(ctx):
    """The Home card, from state.json: one line per signal and the last check."""
    cfg = ctx.config()
    if not cfg["ENABLED"]:
        return {"state": "off", "headline": "Turned off (ENABLED=no)", "items": []}
    state = hm.load_state()
    last = state.get("last")
    if not last:
        return {"state": "unknown", "headline": "No check yet; one runs every 15 minutes", "items": []}
    active = state.get("alerts") or {}

    asleep = f", {last['asleep']} asleep" if last.get("asleep") else ""
    smart, key = _row("SMART", active, lambda k: k.startswith(("smart:", "prefail:")),
                      f"{last['drives']} drives, no rises{asleep}")
    if key and _scheduler_installed():
        smart.update(target=f"Drives:{key.split(':')[1]}", target_label="Drive page")

    vols = last.get("volumes")
    btrfs, _ = _row("btrfs", active, lambda k: k.startswith("btrfs:"),
                    "couldn't read" if vols is None else f"no errors, {vols} volume(s)" if vols else "no volumes")
    parity, _ = _row("Parity", active, lambda k: k.startswith("md:") and k.endswith(":mismatch"),
                     "no mismatches found")

    arrays = last.get("arrays") or {}
    data = sorted(n for n, a in arrays.items() if a.get("data"))
    if cfg["MD_STATE_ALERTS"]:
        md, _ = _row("Arrays", active, lambda k: k.startswith("md:") and k.endswith(":state"),
                     ", ".join(f"{n} {arrays[n]['state']}" for n in data) or "no data arrays")
    else:
        md = {"label": "Arrays", "value": "alerts off (UNAS alerts on these)", "tone": "dim"}

    t = hm.parse_time(last["at"])
    items = [smart, btrfs, parity, md, {"label": "Last check", "value": f"{t:%a %d %b %H:%M}", "tone": "dim"}]
    n = len(active)
    headline = f"Watching {last['drives']} drives" if not n else f"{n} open alert{'s' if n != 1 else ''}"
    return {"state": _level(list(active.values())), "headline": headline, "items": items}


def alerts(ctx):
    """state["alerts"] and state["resolved"] mapped onto the contract."""
    state = hm.load_state()
    active = state.get("alerts") or {}
    out = []
    for key, a in sorted(active.items()):
        since = a.get("since") or a.get("sent")
        if since:
            out.append({"id": key, "level": LEVELS.get(a.get("level"), "info"), "message": a.get("msg", ""),
                        "active": True, "since": since, "last": a.get("sent")})
    for r in state.get("resolved") or []:
        if r.get("key") in active or not r.get("since"):
            continue
        out.append({"id": r["key"], "level": LEVELS.get(r.get("level"), "info"), "message": r.get("msg", ""),
                    "active": False, "since": r["since"], "last": r.get("sent"), "resolved": r.get("resolved")})
    return out
