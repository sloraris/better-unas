"""Suite actions shared by the Settings pages (docs/tui-style.md §6): update
status and updating, the restart offer, installing and uninstalling a tool,
and removing better-unas. Settings › better-unas and each tool's Settings
page call these; nothing here draws a screen of its own except the result
pages it pushes.

Reads the update status from the cache (core.updates.cached_status); the
network is used only for a check, an update, or installing a tool that isn't
in the kept release. Every change asks first, and in --dry-run only says what
it would do.
"""

import os

from .. import install as I
from .. import plugins as P
from .. import updates as U
from .screens import DetailScreen, state_tone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESTART = "Quit (q) and start better-unas again to load the change."


def home_of(app):
    """The folder the suite runs from: set by the entry point, else the
    tools' folder, else next to core/."""
    home = getattr(app, "home", None)
    if home:
        return home
    if app.tools:
        return os.path.dirname(app.tools[0].path)
    return ROOT


def updater_of(app):
    up = getattr(app, "updater", None)
    if up is None:
        up = app.updater = U.Updater(home_of(app))
    return up


def output_lines(text, tone="normal"):
    return [(f"  {line}", tone) for line in (text or "").rstrip().splitlines()] or [("  (no output)", "dim")]


# ------------------------------------------------------------------ tools

def tool_rows(app):
    """Every tool the home knows about: dicts with name, where (installed,
    uninstalled, available), manifest, tool (loaded this session), status,
    problems and broken. Installed first, then uninstalled, then available."""
    home = updater_of(app).home
    loaded = {t.name: t for t in app.tools}
    rows = {}
    for name, manifest, where in I.catalog(home):
        rows[name] = {"name": name, "where": where, "manifest": manifest,
                      "tool": loaded.get(name) if where == "installed" else None,
                      "status": None, "problems": []}
    for name, problems in app.broken.items():
        r = rows.setdefault(name, {"name": name, "where": "installed", "manifest": {},
                                   "tool": None, "status": None, "problems": []})
        if r["where"] == "installed":
            r["problems"] = list(problems)
            r["broken"] = True
    for r in rows.values():
        mod = app.plugins.get(r["name"]) if r["tool"] else None
        if mod is not None and not r.get("broken"):
            try:
                r["status"] = mod.status(app.contexts[r["name"]])
                r["problems"] += P.check_status(r["status"])
            except Exception as e:
                r["problems"].append(f"status: {type(e).__name__}: {e}")
    order = {"installed": 0, "uninstalled": 1, "available": 2}
    return sorted(rows.values(), key=lambda r: (order[r["where"]], r["name"]))


def tool_state(r):
    """(status kind or None, text) for a tool row: kind is a canvas.status
    kind, so the state always carries its glyph."""
    if r.get("broken"):
        return "failed", "broken"
    if r["where"] == "uninstalled":
        return None, "uninstalled"
    if r["where"] == "available":
        return None, "not installed"
    if r["tool"] is None:
        return "warning", "installed, restart to load"
    if r["problems"]:
        return "failed", "error"
    state = (r["status"] or {}).get("state")
    if state == "off":
        return None, "installed, off"
    if state in ("warn", "error"):      # the glyph says it; the tool's page says why
        return {"warn": "warning", "error": "failed"}[state], "installed"
    return "ok", "installed"


def tool_details(r, updater):
    """Lines for a tool's info page: what it is, where it stands, problems."""
    m = r["manifest"]
    tool = r["tool"]
    lines = [(m.get("SUMMARY", ""), "normal"), ""]
    lines.append(f"State:    {tool_state(r)[1]}")
    if m.get("VERSION"):
        suite_version = updater.current()
        covered = f"better-unas {suite_version} covers it" if suite_version else "the suite version covers it"
        lines.append(f"Version:  {m['VERSION']} (the tool's own; {covered})")
    if tool:
        lines.append(f"Folder:   {tool.path}")
    if m.get("CHANGES"):
        lines.append(f"Changes:  {m['CHANGES']}")
    if m.get("SETUP"):
        lines.append(f"Setup:    {m['SETUP']}")
    if r["where"] == "uninstalled":
        lines += ["", ("Uninstalled; its config and state are kept. Updates leave it off.", "dim"),
                  ("Press i to install it again.", "dim")]
    elif r["where"] == "available":
        lines += ["", ("Not installed. Press i to install it.", "dim")]
    elif tool is None and not r.get("broken"):
        lines += ["", (f"Installed after better-unas started. {RESTART}", "warn")]
    status = r["status"]
    if status:
        lines += ["", (f"State: {status['state']}", state_tone(status["state"]))]
        lines += [(f"  {p}", "warn") for p in status.get("problems", [])]
        lines += [f"  {d}" for d in status.get("detail", [])]
    if r["problems"]:
        lines += ["", ("Problems", "bad")] + [(f"  {p}", "bad") for p in r["problems"]]
    return lines


# ------------------------------------------------------------------ actions

class Actions:
    """The suite's actions for a screen's ctx. Each asks before changing
    anything and reports with a message or a result page. Methods return
    True when they changed something, so the caller can redraw."""

    def __init__(self, ctx):
        self.ctx = ctx

    @property
    def app(self):
        return self.ctx.app

    @property
    def updater(self):
        return updater_of(self.app)

    def dry(self, what):
        if self.ctx.dry_run:
            self.ctx.message(f"Dry run: would {what}.", "warn")
            return True
        return False

    # --- updates

    def headline(self, status):
        """(text, tone) for the update line."""
        s = status
        if s is None:
            return "Not checked yet. Press c to check.", "dim"
        if s["error"]:
            return f"Check failed: {s['error']}.", "warn"
        if s["available"]:
            what = f"new commit {s['latest_ref'][:7]}" if s.get("branch") else f"{s['latest']} available"
            return what, "warn"
        return "Up to date.", "good"

    def check(self):
        status = self.ctx.run_slow(lambda: self.updater.check(force=True), "Checking for updates")
        self.app.forget_badge()
        text, tone = self.headline(status)
        if status["available"]:
            text = f"Update: {text}. Press u to update."
        self.ctx.message(text, "bad" if status["error"] else tone)
        return status

    def update(self, status):
        s = status
        if not s or not s["latest"]:
            self.ctx.message("No newer version known. Press c to check.", "dim")
            return False
        if not s["available"]:
            self.ctx.message(f"Already up to date ({s['current']}).", "good")
            return False
        have = I.installed_tools(self.updater.home)
        off = I.uninstalled_tools(self.updater.home)
        if s.get("branch"):
            lines = [f"Downloads commit {s['latest_ref'][:7]} of {s['branch']} from GitHub. This is",
                     "unreleased code (developer mode). Config and state stay as they are."]
        else:
            lines = ["Downloads the release from GitHub and replaces better-unas",
                     "and the tools' files. Config and state stay as they are."]
        if have:
            lines.append(f"Then runs the installer of: {', '.join(have)}.")
        if off:
            lines.append(f"Stays off: {', '.join(off)}.")
        title = f"Update better-unas {s['current'] or '?'} to {s['latest']}?"
        if self.dry(f"update to {s['latest']}") or not self.ctx.confirm(title, lines):
            return False
        report = self.ctx.run_slow(lambda: self.updater.update_to(s), f"Updating to {s['latest']}")
        self.app.forget_badge()
        failed = [n for n, ok, _ in report["tools"] if not ok]
        out = [(f"Updated from {report['from'] or '?'} to {report['to']}.", "good"), ""]
        for name, ok, text in report["tools"]:
            out.append((f"{name}: {'installed' if ok else 'installer FAILED'}", "good" if ok else "bad"))
            out += output_lines(text, "normal" if ok else "bad")
            out.append("")
        for name in report["skipped"]:
            out += [(f"{name}: not in this release; left as it is.", "dim"), ""]
        out.append((RESTART, "warn"))
        if not failed and self.offer_restart(f"Updated to {report['to']}."):
            return True
        self.ctx.push(DetailScreen("Update", out))
        self.ctx.message(f"Updated to {report['to']}" + (f"; {', '.join(failed)} failed." if failed else ". ")
                         + ("" if failed else RESTART), "bad" if failed else "good")
        return True

    def offer_restart(self, done):
        """Ask to restart better-unas now to load a change. True if it will."""
        if not self.ctx.confirm("Restart better-unas now?",
                                [done, "The app that's open still runs the old code;",
                                 "restarting loads the new version and its tabs."]):
            return False
        self.app.request_restart()
        return True

    # --- tools

    def install(self, r):
        name, m = r["name"], r["manifest"]
        again = r["where"] == "installed"
        title = f"{'Reinstall' if again else 'Install'} {name}?"
        lines = []
        if m.get("CHANGES"):
            lines.append(f"Changes: {m['CHANGES']}")
        if again:
            lines.append("Replaces its files and runs its installer; config and state stay.")
        elif r["where"] == "uninstalled":
            lines.append("Turns it back on with the config it had.")
        if m.get("SETUP") and not again:
            lines.append(f"First, in the UniFi UI: {m['SETUP']}")
        if self.dry(f"{'reinstall' if again else 'install'} {name}"):
            return False
        if not self.ctx.confirm(title, lines, second="Have you done that step?" if m.get("SETUP") and not again else None):
            return False
        ok, text = self.ctx.run_slow(lambda: self.updater.install_tool(name), f"Installing {name}")
        self.after(name, ok, f"{name} installed." if ok else f"{name}'s installer failed.", text)
        return True

    def uninstall(self, r):
        name = r["name"]
        if r["where"] != "installed":
            self.ctx.message(f"{name} isn't installed.", "dim")
            return False
        lines = ["Runs its uninstaller: its timers and triggers go.",
                 "Its config and state stay, and updates leave it off.",
                 "Press i later to turn it back on."]
        if self.dry(f"uninstall {name}") or not self.ctx.confirm(f"Uninstall {name}?", lines):
            return False
        ok, text = self.ctx.run_slow(lambda: self.updater.uninstall_tool(name), f"Uninstalling {name}")
        self.after(name, ok, f"{name} uninstalled." if ok else f"{name}'s uninstaller failed.", text)
        return True

    def after(self, name, ok, headline, text):
        if ok and self.offer_restart(headline):
            return
        self.ctx.push(DetailScreen(name, [(headline, "good" if ok else "bad"), ""] + output_lines(text)
                                   + ["", (RESTART, "warn")]))
        self.ctx.message(headline + (" " + RESTART if ok else ""), "good" if ok else "bad")

    def remove_all(self):
        home = self.updater.home
        have = I.installed_tools(home)
        lines = [f"Uninstalls every tool ({', '.join(have) or 'none installed'})",
                 "and removes the better-unas command."]
        if self.dry("remove better-unas") or not self.ctx.confirm("Remove better-unas?", lines):
            return False
        delete = self.ctx.confirm(f"Also delete {home}?",
                                  ["Deletes every tool's config and state too. This can't be undone.",
                                   "No keeps the folder, so a reinstall picks up where you left off."])
        results = self.ctx.run_slow(lambda: self.updater.remove_all(delete_home=delete), "Removing better-unas")
        failed = [n for n, ok, _ in results if not ok]
        out = []
        for name, ok, text in results:
            out.append((f"{name}: {'uninstalled' if ok else 'uninstaller FAILED'}", "good" if ok else "bad"))
            out += output_lines(text, "normal" if ok else "bad") + [""]
        out.append((f"Deleted {home}." if delete else f"Kept {home} (config and state).", "normal"))
        out.append(("better-unas is removed. Quit with q.", "warn"))
        self.ctx.push(DetailScreen("Removed", out))
        self.ctx.message("better-unas removed." if not failed else f"Failed: {', '.join(failed)}.",
                         "bad" if failed else "good")
        return True
