"""space-watch's plugin module: the data API and one screen for the better-unas TUI.

See docs/plugin-contract.md. The core imports this file by path; it loads its
own sibling module by path too, so it never relies on sys.path.
"""

import importlib.util
import os

CONTRACT = 1

_spec = importlib.util.spec_from_file_location(
    "better_unas_space_watch_core", os.path.join(os.path.dirname(os.path.abspath(__file__)), "space_watch.py"))
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)


def _reading(ctx):
    cfg = ctx.config()
    pct = sw.measure(cfg["WATCH_PATH"])
    return cfg, pct, pct > cfg["WARN_PERCENT"]


# ------------------------------------------------------------------ data API

def status(ctx):
    """Cheap: local files only."""
    cfg = ctx.config()
    problems = [f"{k}: {msg}" for k, msg in ctx.config_errors().items()]
    return {
        "enabled": cfg["ENABLED"],
        "state": "off" if not cfg["ENABLED"] else ("warn" if problems else "ok"),
        "problems": problems,
        "detail": [f"Watching {cfg['WATCH_PATH']}"],
    }


def summary(ctx):
    cfg, pct, over = _reading(ctx)
    if not cfg["ENABLED"]:
        return {"state": "off", "headline": "Turned off", "items": []}
    state = sw.load_state(os.path.join(ctx.tool.path, "state.json"))
    items = [{"label": "Used", "value": f"{pct}%", "tone": "bad" if over else "good"}]
    if "peak" in state:
        items.append({"label": "Peak", "value": f"{state['peak']}%", "tone": "normal"})
    return {
        "state": "warn" if over else "ok",
        "headline": f"{cfg['WATCH_PATH']} is {pct}% full",
        "items": items,
    }


def alerts(ctx):
    cfg, pct, over = _reading(ctx)
    if not (cfg["ENABLED"] and over):
        return []
    at = ctx.now().isoformat()
    return [{"id": "full", "level": "warn", "active": True, "since": at, "last": at,
             "message": f"{cfg['WATCH_PATH']} is {pct}% full (warn above {cfg['WARN_PERCENT']}%)"}]


def validate(values):
    """Cross-field rules the schema can't express. Nothing to check here, but a
    tool like smart-scheduler would compare intervals."""
    return {}


# ------------------------------------------------------------------ screens

class SpaceScreen:
    title = "Space"

    def __init__(self, ctx):
        self.ctx = ctx
        self.pct = None

    def keys(self):
        return [("r", "refresh"), ("x", "forget peak")]

    def show(self):
        _, self.pct, self.over = self.ctx.run_slow(lambda: _reading(self.ctx), "Measuring")

    def render(self, canvas):
        cfg = self.ctx.config()
        canvas.text(0, 0, f"Volume holding {cfg['WATCH_PATH']}", bold=True)
        if self.pct is None:
            canvas.text(2, 0, "Not measured yet.", tone="dim")
            return
        width = max(10, canvas.width - 10)
        filled = int(width * self.pct / 100)
        canvas.text(2, 0, "#" * filled, tone="bad" if self.over else "good")
        canvas.text(2, filled, "." * (width - filled), tone="dim")
        canvas.text(2, width + 1, f"{self.pct:5.1f}%")
        canvas.text(4, 0, f"Warns above {cfg['WARN_PERCENT']}%.", tone="dim")

    def key(self, k):
        if k == "r":
            self.show()
            return True
        if k == "x":
            if self.ctx.confirm("Forget the peak reading?", ["The next run starts a new peak."]):
                path = os.path.join(self.ctx.tool.path, "state.json")
                with self.ctx.lock():
                    state = sw.load_state(path)
                    state.pop("peak", None)
                    state.pop("peak_at", None)
                    sw.save_state(state, path)
                self.ctx.message("Peak forgotten.", tone="good")
            return True
        return False


def screens(ctx):
    return [{"id": "space", "tab": "Space", "make": SpaceScreen}]
