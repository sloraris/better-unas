"""The Tools tab (#14): suite version and updates, every tool the home knows
about, and install / uninstall / remove.

    better-unas v0.2.0        Update available: v0.3.0. Press u to update.
    checked Sat 22 Nov 01:00

      Tool               State          Summary
    › smart-scheduler    ok             Scheduled extended SMART tests, ...
      space-watch        not installed  Warns when a volume is nearly full

Reads the update status from the cache (core.updates.cached_status); the
network is used only when the user presses c (check), u (update), or i for a
tool that isn't in the kept release. Every change asks first, and in
--dry-run only says what it would do. Tool tabs come and go with a restart,
so after a change the screen says to quit and start better-unas again.

In developer mode (Settings → better-unas) the headline offers the newest
commit on the followed branch instead of a release, and b picks the branch
from GitHub's list.
"""

import os

from .. import install as I
from .. import plugins as P
from .. import updates as U
from . import widgets as W
from .screens import DetailScreen, state_tone, when

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


class ToolsScreen:
    title = "Tools"

    def __init__(self, ctx):
        self.ctx = ctx
        self.rows = []          # dicts: name, where, manifest, tool, status, problems
        self.status = None      # cached update status
        self.table = W.Table([("Tool", 18), ("State", 14), ("Summary", None)], empty="No tools found.")

    # ------------------------------------------------------------ data

    @property
    def app(self):
        return self.ctx.app

    @property
    def updater(self):
        return updater_of(self.app)

    def show(self):
        self.status = self.updater.cached_status()
        app = self.app
        loaded = {t.name: t for t in app.tools}
        rows = {}
        for name, manifest, where in I.catalog(self.updater.home):
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
        self.rows = sorted(rows.values(), key=lambda r: (order[r["where"]], r["name"]))
        self.table.set_rows([[r["name"], self.state(r), r["manifest"].get("SUMMARY", "") or
                              (r["problems"][0] if r["problems"] else "")] for r in self.rows])

    def state(self, r):
        if r.get("broken"):
            return ("broken", "bad")
        if r["where"] == "uninstalled":
            return ("uninstalled", "dim")
        if r["where"] == "available":
            return ("not installed", "dim")
        if r["tool"] is None:
            return ("installed", "warn")     # installed during this session: restart
        if r["problems"]:
            return ("error", "bad")
        if r["status"]:
            return (r["status"]["state"], state_tone(r["status"]["state"]))
        return ("installed", "normal")

    @property
    def current(self):
        return self.rows[self.table.index] if self.rows else None

    # ------------------------------------------------------------ drawing

    def keys(self):
        ks = []
        if self.status and self.status["available"]:
            ks.append(("u", "update"))
        if self.updater.dev_branch():
            ks.append(("b", "branch"))
        r = self.current
        if r and r["where"] == "installed":
            ks.append(("x", "uninstall"))
        elif r:
            ks.append(("i", "install"))
        # Most used first: the footer cuts the rest, and ? lists them all.
        ks += [("c", "check"), ("R", "remove"), ("Enter", "info")]
        if r and r["where"] == "installed":
            ks.append(("i", "reinstall"))
        return ks + self.table.keys()

    def headline(self):
        """(text, tone) for the update line."""
        s = self.status
        if s is None:
            return "Updates not checked yet. Press c to check.", "dim"
        if s["error"]:
            return f"Update check: {s['error']}.", "dim"
        if s["available"]:
            what = "New commit" if s.get("branch") else "Update available"
            return f"{what}: {s['latest']}. Press u to update.", "warn"
        return "Up to date.", "good"

    def render(self, canvas):
        current = self.updater.installed_label() or "(no VERSION)"
        name = f"better-unas {current}"
        canvas.text(0, 1, name, "normal", bold=True)
        text, tone = self.headline()
        canvas.text(0, len(name) + 4, W.fit(text, max(0, canvas.width - len(name) - 5)), tone,
                    bold=tone == "warn")
        sub = []
        branch = self.updater.dev_branch()
        if branch:
            sub.append(f"developer mode: following {branch}")
        if self.status:
            sub.insert(0, f"checked {when(self.status['checked_at'])}")
        canvas.text(1, 1, W.fit("  ".join(sub), canvas.width - 2), "warn" if branch else "dim")
        self.table.render(canvas.sub(3, 0, canvas.height - 3, canvas.width))

    # ------------------------------------------------------------ keys

    def key(self, k):
        r = self.current
        if k == "enter" and r:
            self.ctx.push(DetailScreen(r["name"], self.details(r)))
            return True
        if k == "c":
            self.check()
            return True
        if k == "u":
            self.update()
            return True
        if k == "i" and r:
            self.install(r)
            return True
        if k == "x" and r:
            self.uninstall(r)
            return True
        if k == "R":
            self.remove_all()
            return True
        if k == "b" and self.updater.dev_branch():
            self.pick_branch()
            return True
        return self.table.key(k)

    def details(self, r):
        m = r["manifest"]
        tool = r["tool"]
        lines = [(m.get("SUMMARY", ""), "normal"), ""]
        lines.append(f"State:    {self.state(r)[0]}")
        if m.get("VERSION"):
            lines.append(f"Version:  {m['VERSION']} (the tool's own; better-unas {self.updater.current()} covers it)")
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

    def dry(self, what):
        if self.ctx.dry_run:
            self.ctx.message(f"Dry run: would {what}.", "warn")
            return True
        return False

    def check(self):
        self.status = self.ctx.run_slow(lambda: self.updater.check(force=True), "Checking for updates")
        text, tone = self.headline()
        self.ctx.message(text, "bad" if self.status["error"] else tone)

    def update(self):
        s = self.status
        if not s or not s["latest"]:
            self.ctx.message("No newer release known. Press c to check.", "dim")
            return
        if not s["available"]:
            self.ctx.message(f"Already up to date ({s['current']}).", "good")
            return
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
            return
        report = self.ctx.run_slow(lambda: self.updater.update_to(s), f"Updating to {s['latest']}")
        self.status = self.updater.cached_status()
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
            return
        self.ctx.push(DetailScreen("Update", out))
        self.ctx.message(f"Updated to {report['to']}" + (f"; {', '.join(failed)} failed." if failed else ". ")
                         + ("" if failed else RESTART), "bad" if failed else "good")

    def offer_restart(self, done):
        """Ask to restart better-unas now to load a change. True if it will."""
        if not self.ctx.confirm("Restart better-unas now?",
                                [done, "The app that's open still runs the old code;",
                                 "restarting loads the new version and its tabs."]):
            return False
        self.app.request_restart()
        return True

    def pick_branch(self):
        """Developer mode: list the branches on GitHub and follow the one picked."""
        try:
            names = self.ctx.run_slow(self.updater.fetch_branches, "Listing branches on GitHub")
        except (OSError, ValueError) as e:
            self.ctx.message(f"Couldn't list branches: {getattr(e, 'reason', e)}.", "bad")
            return
        if not names:
            self.ctx.message("GitHub listed no branches.", "bad")
            return
        self.ctx.push(BranchScreen(self, names, self.updater.dev_branch()))

    def follow(self, branch):
        """Save DEV_BRANCH and check that branch right away."""
        from .. import configfile
        home = self.updater.home
        if self.dry(f"follow {branch}"):
            return
        try:
            with self.app.lock(os.path.join(home, ".lock"), 10):
                configfile.save(os.path.join(home, U.CORE_CONFIG), {"DEV_BRANCH": branch}, U.core_schema())
        except Exception as e:
            self.ctx.message(f"Couldn't save the branch: {e}", "bad")
            return
        self.check()

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
            return
        if not self.ctx.confirm(title, lines, second="Have you done that step?" if m.get("SETUP") and not again else None):
            return
        ok, text = self.ctx.run_slow(lambda: self.updater.install_tool(name), f"Installing {name}")
        self.after(name, ok, f"{name} installed." if ok else f"{name}'s installer failed.", text)

    def uninstall(self, r):
        name = r["name"]
        if r["where"] != "installed":
            self.ctx.message(f"{name} isn't installed.", "dim")
            return
        lines = ["Runs its uninstaller: its timers and triggers go.",
                 "Its config and state stay, and updates leave it off.",
                 "Press i later to turn it back on."]
        if self.dry(f"uninstall {name}") or not self.ctx.confirm(f"Uninstall {name}?", lines):
            return
        ok, text = self.ctx.run_slow(lambda: self.updater.uninstall_tool(name), f"Uninstalling {name}")
        self.after(name, ok, f"{name} uninstalled." if ok else f"{name}'s uninstaller failed.", text)

    def after(self, name, ok, headline, text):
        self.show()
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
            return
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
        if not delete:
            self.show()
        self.ctx.push(DetailScreen("Removed", out))
        self.ctx.message("better-unas removed." if not failed else f"Failed: {', '.join(failed)}.",
                         "bad" if failed else "good")


class BranchScreen:
    """Pick the branch developer mode follows."""

    title = "Branch"

    def __init__(self, tools, names, current):
        self.tools, self.names = tools, names
        self.list = W.ListView([f"{n}  (following)" if n == current else n for n in names])
        if current in names:
            self.list.index = names.index(current)

    def keys(self):
        return [("Enter", "follow")] + self.list.keys()

    def render(self, canvas):
        canvas.text(0, 0, "Branches on GitHub. Developer mode installs the newest commit of the one you pick.", "dim")
        self.list.render(canvas.sub(2, 0, canvas.height - 2, canvas.width))

    def key(self, k):
        if k == "enter":
            branch = self.names[self.list.index]
            self.tools.ctx.pop()
            self.tools.follow(branch)
            return True
        return self.list.key(k)
