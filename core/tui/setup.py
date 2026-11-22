"""The first-run setup wizard (#15): the screens over core/setup.py's Setup.

    Pick tools  ->  <tool> settings (essential fields only)  ->  Review  ->  Done

Each step is pushed on one stack, so Esc goes back a step with every choice
kept. q leaves (asking first) and nothing has changed until Enter on Review
is confirmed. In a dry run Review is the last step.

The wizard runs in a SetupApp: the core App with a single "Setup" tab, so
dialogs, the spinner, the footer and help work as everywhere else. When it
ends, wizard.setup.done says whether anything was installed and the launcher
opens the normal app.
"""

from .app import App, Tab
from . import widgets as W
from .screens import DetailScreen

LEAVE = "Nothing has been changed yet."


def _can_leave(ctx, setup):
    if setup.done or not ctx.app.running:
        return True
    return ctx.confirm("Leave setup?", [LEAVE, "Run better-unas again to start over."])


class PickScreen:
    """Step 1: tick the tools to install."""

    title = "Setup"

    def __init__(self, ctx, setup, warnings=()):
        self.ctx, self.setup = ctx, setup
        self.warnings = list(warnings)
        self.list = W.ListView(empty="This release has no tools.")
        self.refresh()

    def refresh(self):
        items = []
        for o in self.setup.offered:
            box = "[x]" if o.name in self.setup.selected else "[ ]"
            items.append((f"{box} {o.name:<20} {o.version:<8} {o.summary}",
                          "bad" if o.problems else "normal"))
        self.list.set_items(items)

    def keys(self):
        return [("Space", "tick"), ("Enter", "next")] + self.list.keys()

    def intro(self):
        s = self.setup
        lines = [(f"Welcome to better-unas {s.version}.", "accent"),
                 "Pick the tools to install. Nothing changes until you confirm the review.", ""]
        if s.legacy:
            names = [f"{n} {t['version']}" for n, t in s.legacy["tools"].items()]
            lines.append((f"Found an install from the old installer: {', '.join(names) or 'the command only'}.",
                          "warn"))
            lines.append(("Its config and state are kept; installing moves it to the new layout.", "dim"))
            lines.append("")
        for w in self.warnings:
            lines.append((w, "warn"))
        if self.warnings:
            lines.append("")
        return lines

    def render(self, canvas):
        lines = self.intro()
        for y, item in enumerate(lines):
            text, tone = W.cell(item)
            canvas.text(y, 0, W.fit(text, canvas.width - 1), tone, bold=tone == "accent")
        top = len(lines)
        self.list.render(canvas.sub(top, 0, max(1, canvas.height - top - 2), canvas.width))
        o = self.current()
        if o is not None:
            note = o.changes or o.summary
            if o.problems:
                note = f"Can't be installed: {o.problems[0]}"
            canvas.text(canvas.height - 1, 0, W.fit(note, canvas.width - 1), "bad" if o.problems else "dim")

    def current(self):
        i = self.list.index
        return None if i is None else self.setup.offered[i]

    def key(self, k):
        o = self.current()
        if k == " " and o is not None:
            if o.problems and o.name not in self.setup.selected:
                self.ctx.message(f"{o.name} can't be installed: {o.problems[0]}", "bad")
            else:
                self.setup.toggle(o.name)
                self.refresh()
            return True
        if k == "enter":
            if not self.setup.selected:
                self.ctx.message("Tick at least one tool with Space.", "warn")
                return True
            next_step(self.ctx, self.setup, None)
            return True
        return self.list.key(k)

    def can_leave(self):
        return _can_leave(self.ctx, self.setup)


class EssentialsScreen:
    """Step 2: one tool's essential settings, prefilled with its defaults
    (or an old install's current values)."""

    def __init__(self, ctx, setup, name):
        self.ctx, self.setup, self.name = ctx, setup, name
        self.title = f"{name} settings"
        start = setup.starting_text(name)
        self.form = W.Form(setup.essential_schema(name), start)
        # Coming back to this step shows what was picked before.
        for k, v in setup.edits.get(name, {}).items():
            self.form.field(k).text = v

    def keys(self):
        ks = self.form.keys()
        return ks if self.form.editing else ks + [("n", "next")]

    def render(self, canvas):
        canvas.text(0, 0, W.fit("Settings to check before the first run; the rest are on the "
                                "Settings tab.", canvas.width - 1), "dim")
        self.form.render(canvas.sub(2, 0, canvas.height - 2, canvas.width))

    def key(self, k):
        if not self.form.editing and k == "n":
            errors = self.form.errors()
            if errors:
                key = next(iter(errors))
                self.ctx.message(f"{self.form.field(key).label}: {errors[key]}", "bad")
                return True
            self.setup.set_edits(self.name, self.form.texts())
            next_step(self.ctx, self.setup, self.name)
            return True
        return self.form.key(k)

    def can_leave(self):
        # Esc keeps what was typed (stored), so going back loses nothing.
        if self.form.editing:
            return False
        if not self.ctx.app.running or self.setup.done:
            return True
        if not self.form.errors():
            self.setup.set_edits(self.name, self.form.texts())
        return True if self.ctx.app.going_back else _can_leave(self.ctx, self.setup)


class ReviewScreen:
    """Step 3: exactly what will change, then install."""

    title = "Review"

    def __init__(self, ctx, setup):
        self.ctx, self.setup = ctx, setup
        self.text = W.ScrollText()
        self.show()

    def show(self):
        lines = []
        if self.ctx.dry_run:
            lines += [("Dry run: this is what setup would do. Nothing will be changed.", "warn"), ""]
        for heading, items in self.setup.review():
            lines.append((heading, "accent"))
            lines += [(f"  {t}", tone) for t, tone in items]
            lines.append("")
        self.text.set_text(lines)

    def keys(self):
        if self.ctx.dry_run:
            return self.text.keys()
        return [("Enter", "install")] + self.text.keys()

    def render(self, canvas):
        self.text.render(canvas)

    def key(self, k):
        if k == "enter":
            if self.ctx.dry_run:
                self.ctx.message("Dry run: nothing was changed. Press q to leave.", "warn")
                return True
            names = ", ".join(self.setup.selected)
            if not self.ctx.confirm(f"Install better-unas {self.setup.version}?",
                                    [f"Tools: {names}.", "The changes listed on this screen are made now."]):
                return True
            report = self.ctx.run_slow(self.setup.apply, "Installing")
            self.ctx.push(DoneScreen(self.ctx, self.setup, report))
            return True
        return self.text.key(k)

    def can_leave(self):
        if self.ctx.app.going_back:
            return True
        return _can_leave(self.ctx, self.setup)


class DoneScreen(DetailScreen):
    """Step 4: what happened, with each tool's installer output."""

    def __init__(self, ctx, setup, report):
        self.ctx, self.setup, self.report = ctx, setup, report
        failed = [n for n, ok, _ in report["tools"] if not ok]
        lines = []
        if failed:
            lines.append((f"Installed, but {', '.join(failed)} failed. See the output below; "
                          "its page in Settings can retry (i).", "bad"))
        else:
            lines.append((f"better-unas {setup.version} is installed.", "good"))
        if report.get("link"):
            lines.append(f"Run better-unas any time to open it again ({setup.link}).")
        if report.get("legacy"):
            lines.append((f"The old installer script was kept as {report['legacy']}.", "dim"))
        lines.append("")
        for name, ok, out in report["tools"]:
            lines.append((f"{name}: {'installed' if ok else 'installer FAILED'}", "good" if ok else "bad"))
            lines += [(f"  {ln}", "normal" if ok else "bad") for ln in out.rstrip().splitlines()]
            lines.append("")
        super().__init__("Done", lines)

    def keys(self):
        return [("Enter", "open better-unas")] + super().keys()

    def key(self, k):
        if k == "enter":
            self.ctx.app.running = False
            return True
        if k == "esc":
            return True        # nothing to go back to: it's installed
        return super().key(k)


def next_step(ctx, setup, after):
    """Push the step after `after` (None: after picking tools)."""
    names = setup.needs_settings()
    i = 0 if after is None else names.index(after) + 1
    if i < len(names):
        ctx.push(EssentialsScreen(ctx, setup, names[i]))
    else:
        ctx.push(ReviewScreen(ctx, setup))


class SetupApp(App):
    """The core App with only the wizard's tab."""

    def __init__(self, terminal, setup, warnings=(), **kw):
        super().__init__(terminal, **kw)
        self.setup = setup
        self.going_back = False
        self.tabs = [Tab("Setup", [("Setup", self.core_ctx,
                                    lambda ctx: PickScreen(ctx, setup, warnings))])]
        self.current = 0

    def pop(self):
        self.going_back = True
        try:
            return super().pop()
        finally:
            self.going_back = False

    def core_keys(self):
        ks = []
        if len(self.stack()) > 1 and not self.setup.done:
            ks.append(("Esc", "back"))
        return ks + [("?", "help"), ("q", "quit")]


def run_wizard(term, setup, warnings=(), dry_run=False, **kw):
    """Build and run the wizard on a terminal. Returns the app."""
    app = SetupApp(term, setup, warnings, dry_run=dry_run, **kw)
    app.run()
    return app

