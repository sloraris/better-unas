"""smart-scheduler's plugin module for the better-unas TUI (docs/plugin-contract.md).

The core imports this file by path. It loads smart_scheduler.py and its own
screen modules by path too, so it never relies on the tool folder being on
sys.path. The scheduled run never imports this file; removing it leaves the
tool working.

Settings are read only through ctx.config() (never the config file), so a
Settings preview with unsaved values flows through everything here.
"""

import importlib.util
import os

CONTRACT = 1
HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ss = _load("better_unas_smart_scheduler", "smart_scheduler.py")
calendar_screen = _load("better_unas_smart_scheduler_calendar", "screens_calendar.py")

LEVELS = {"error": "error", "warning": "warn", "warn": "warn"}


# ------------------------------------------------------------------ data API

def status(ctx):
    """Cheap: config, state and trigger files only."""
    cfg = ctx.config()
    problems = [f"{k}: {msg}" for k, msg in ctx.config_errors().items()]
    if not cfg["ENABLED"]:
        return {"enabled": False, "state": "off", "problems": problems,
                "detail": ["Turned off (ENABLED=no); the next run removes its timer."]}
    problems += ss.trigger_problems()
    state = ss.load_state()
    detail = []
    last = ss.parse_time(state.get("last_tick"))
    if last:
        detail.append(f"Last run {last:%a %d %b %H:%M}")
    active = (state.get("alerts") or {}).values()
    if any(a.get("level") == "error" for a in active):
        level = "error"
    elif problems or active:
        level = "warn"
    else:
        level = "ok"
    return {"enabled": True, "state": level, "problems": problems, "detail": detail}


def summary(ctx):
    """The Home card. Live: reads UniFi Drive and plans like the calendar."""
    if not ctx.config()["ENABLED"]:
        return {"state": "off", "headline": "Turned off (ENABLED=no)", "items": []}
    try:
        data = calendar_screen.gather(ss, ctx)
    except calendar_screen.CalendarError as e:
        return {"state": "error", "headline": str(e)[:60], "items": []}
    t = data["now"]
    drives = [d for d in data["drives"] if not d.get("excluded") and not d.get("error")]
    due = [d for d in drives if not d.get("running") and d.get("due") and d["due"] <= t]
    headline = f"{len(drives)} drive{'s' if len(drives) != 1 else ''} scheduled"
    if due:
        headline += f", {len(due)} due now"
    items = []
    running = [x for v in data["days"].values() for x in v["tests"] if x["outcome"] == "running"]
    if running:
        items.append({"label": "Running", "value": f"slot {running[0]['slot']} ({running[0]['serial']})",
                      "tone": "warn"})
    planned = sorted((e for v in data["days"].values() for e in v["planned"]), key=lambda e: e["start"])
    if planned:
        e = planned[0]
        items.append({"label": "Next test", "value": f"{e['start']:%a %d %b}, slot {e['slot']} ({e['serial']})",
                      "tone": "warn"})
    done = sorted((x for v in data["days"].values() for x in v["tests"] if x["outcome"] != "running"),
                  key=lambda x: x["end"] or x["start"])
    if done:
        x = done[-1]
        items.append({"label": "Last result",
                      "value": f"{x['outcome'].capitalize()}, slot {x['slot']}, {(x['end'] or x['start']):%a %d %b}",
                      "tone": "good" if x["outcome"] == "passed" else "bad"})
    scrubs = sorted((d, b) for d, v in data["days"].items() for b in v["blocks"]
                    if b["kind"] == "scrub" and not b["past"] and d >= t.date())
    if scrubs:
        d, b = scrubs[0]
        items.append({"label": "Next scrub", "value": f"{d:%a %d %b}, {b['short']}", "tone": "normal"})
    if data["snoozes"]:
        z = data["snoozes"][0]
        more = f" (+{len(data['snoozes']) - 1})" if len(data["snoozes"]) > 1 else ""
        items.append({"label": "Snoozed", "value": f"slot {z['slot']} until {z['until']:%a %d %b}{more}",
                      "tone": "normal"})
    level = "warn" if data["problems"] or any(x["outcome"] == "failed" for x in done[-1:]) else "ok"
    return {"state": level, "headline": headline, "items": items[:6]}


def alerts(ctx):
    """state["alerts"] mapped onto the contract: key -> id, msg -> message, sent -> last."""
    out = []
    for key, a in sorted((ss.load_state().get("alerts") or {}).items()):
        since = a.get("since") or a.get("sent")
        if not since:
            continue
        out.append({"id": key, "level": LEVELS.get(a.get("level"), "info"), "message": a.get("msg", ""),
                    "active": True, "since": since, "last": a.get("sent")})
    return out


def validate(values):
    """Cross-field rules: the shorter intervals shouldn't be longer than the healthy one."""
    out = {}
    healthy = values.get("INTERVAL_DAYS")
    for key in ("WARN_INTERVAL_DAYS", "OLD_INTERVAL_DAYS"):
        v = values.get(key)
        if isinstance(v, int) and isinstance(healthy, int) and v > healthy:
            out[key] = f"should be at most the healthy interval ({healthy} days)"
    return out


# ------------------------------------------------------------------ screens

def _calendar(ctx):
    # `ss` is looked up each time the screen reads, so tests can swap in a faked module.
    return calendar_screen.CalendarScreen(ctx, lambda: calendar_screen.gather(ss, ctx))


def screens(ctx):
    return [{"id": "calendar", "tab": "Calendar", "make": _calendar}]


class _PreviewContext:
    """The tool's context with config() answering from unsaved values."""

    def __init__(self, ctx, values):
        self._ctx, self._values = ctx, dict(values)

    def config(self):
        return dict(self._values)

    def __getattr__(self, name):
        return getattr(self._ctx, name)


def preview_calendar(ctx, values):
    """A Calendar screen that plans with unsaved settings, for the Settings screen:

        ctx.push(plugin.preview_calendar(ctx, values))

    `values` is {KEY: typed value} with the user's edits applied (what
    ctx.config() would return after saving). Every read, including `r`
    (recalculate), uses them; nothing is written.
    """
    screen = _calendar(_PreviewContext(ctx, values))
    screen.title = "Calendar preview (unsaved settings)"
    return screen
