"""The app shell: tab bar, header, body, message line and footer; the screen
stacks; key routing; and the per-tool context from docs/plugin-contract.md.

Layout at any size from 80x24 up:

    row 0        tab bar          1 Home  2 Calendar  3 Drives ...
    row 1        header           screen title (and breadcrumbs), tool name
    rows 2..h-3  body             the screen's canvas (20 rows at 80x24)
    row h-2      message line     toast, spinner or "waiting for the lock"
    row h-1      footer           the keys that work right now

Smaller than 80x24 shows only a "please widen" screen.
"""

import contextlib
import datetime as dt
import threading
import traceback

from .. import plugins as P
from . import screens as core
from . import tools as tools_screen
from . import widgets
from .keys import RESIZE
from .lock import LockBusy, run_lock
from .theme import Theme

MIN_W, MIN_H = 80, 24
CHROME = 4   # rows outside the body


class Context:
    """One per tool (and one for the core's own screens, with tool None)."""

    LockBusy = LockBusy

    def __init__(self, app, tool=None):
        self.app = app          # core business, not part of the contract
        self.tool = tool
        self._preview = None

    # --- contract members
    widgets = widgets

    @property
    def dry_run(self):
        return self.app.dry_run

    @property
    def theme(self):
        return self.app.theme

    def config(self):
        if self._preview is not None:
            return dict(self._preview)
        return self.tool.read_config()[0] if self.tool else {}

    def config_errors(self):
        return self.tool.read_config()[1] if self.tool else {}

    def now(self):
        return self.app.clock()

    def run_slow(self, fn, label):
        return self.app.run_slow(fn, label)

    def lock(self, timeout=10):
        if self.tool is None:
            raise RuntimeError("the core's own screens have no tool lock")
        return self.app.lock(self.tool.file("LOCK"), timeout)

    def confirm(self, title, lines=(), second=None):
        return self.app.confirm(title, lines, second)

    def message(self, text, tone="normal"):
        self.app.message(text, tone)

    def push(self, screen):
        self.app.push(screen)

    def pop(self):
        return self.app.pop()

    def log(self, text):
        self.app.log(f"[{self.tool.name if self.tool else 'core'}] {text}")

    # --- core business
    @contextlib.contextmanager
    def preview(self, values):
        """While active, config() returns these values (the Settings preview)."""
        old, self._preview = self._preview, dict(values)
        try:
            yield self
        finally:
            self._preview = old


class Tab:
    def __init__(self, title, entries):
        self.title = title
        self.entries = entries      # [(label, ctx, make)]
        self.stack = []
        self.made = {}              # label -> screen, so make() runs once per session


class App:
    def __init__(self, terminal, tools=(), broken=None, extra_tabs=(), dry_run=False,
                 clock=None, log_path=None, theme=None):
        self.term = terminal
        self.theme = theme or Theme.from_env()
        self.dry_run = dry_run
        self.clock = clock or (lambda: dt.datetime.now().astimezone())
        self.log_path = log_path
        self.running = True
        self.toast = self.spinner = self.overlay = None
        self.tools = list(tools)
        self.broken = dict(broken or {})       # tool folder name -> problems
        self.plugins, self.contexts = {}, {}
        self.core_ctx = Context(self)
        self.owners = {}                       # id(screen) -> tool name, for the header
        self.typed_ahead = []                  # keys pressed during a slow read or lock wait

        tool_tabs = [(title, title, self.core_ctx, make) for title, make in extra_tabs]
        for tool in self.tools:
            ctx = self.contexts[tool.name] = Context(self, tool)
            try:
                mod = P.load_plugin(tool.path, tool.manifest)
            except P.ContractError as e:
                self.broken[tool.name] = e.problems
                continue
            if mod is None:
                continue
            self.plugins[tool.name] = mod
            if not hasattr(mod, "screens"):
                continue
            try:
                specs = mod.screens(ctx)
            except Exception as e:
                self.broken[tool.name] = [f"screens: {type(e).__name__}: {e}"]
                continue
            problems = P.check_screens(specs)
            if problems:
                self.broken[tool.name] = problems
                continue
            for s in specs:
                tool_tabs.append((s["tab"], f"{tool.name}: {s['id']}", ctx, s["make"]))

        grouped = {}
        for title, label, ctx, make in tool_tabs:
            grouped.setdefault(title, []).append((label, ctx, make))
        self.tabs = [Tab("Home", [("Home", self.core_ctx, core.HomeScreen)])]
        self.tabs += [Tab(title, entries) for title, entries in grouped.items()]
        self.tabs += [Tab("Settings", [("Settings", self.core_ctx, core.SettingsScreen)]),
                      Tab("Alerts", [("Alerts", self.core_ctx, core.AlertsScreen)]),
                      Tab("Tools", [("Tools", self.core_ctx, tools_screen.ToolsScreen)])]
        self.current = 0

    # ------------------------------------------------------------ screens

    @property
    def tab(self):
        return self.tabs[self.current]

    def make(self, tab, label, ctx, make):
        if label not in tab.made:
            try:
                tab.made[label] = make(ctx)
                if ctx.tool is not None:
                    self.owners[id(tab.made[label])] = ctx.tool.name
            except Exception as e:
                self.log(traceback.format_exc())
                tab.made[label] = core.ErrorScreen(tab.title, e)
        return tab.made[label]

    def stack(self, tab=None):
        tab = tab or self.tab
        if not tab.stack:
            if len(tab.entries) == 1:
                tab.stack.append(self.make(tab, *tab.entries[0]))
            else:
                tab.stack.append(core.ChooserScreen(self.core_ctx, tab))
        return tab.stack

    @property
    def screen(self):
        return self.stack()[-1]

    def call(self, screen, name, *args, default=None):
        """Call a screen method; an exception becomes a message, never a crash."""
        fn = getattr(screen, name, None)
        if fn is None:
            return default
        try:
            return fn(*args)
        except LockBusy as e:
            self.message(f"Busy: {e}.", "warn")
        except Exception as e:
            self.log(traceback.format_exc())
            self.message(f"{type(e).__name__}: {e}", "bad")
        return default

    def start(self):
        self.call(self.screen, "show")

    def can_leave(self):
        return bool(self.call(self.screen, "can_leave", default=True))

    def switch(self, i):
        if i == self.current or not 0 <= i < len(self.tabs) or not self.can_leave():
            return
        widgets.reset_editing()
        self.current = i
        self.call(self.screen, "show")

    def push(self, screen):
        self.stack().append(screen)
        self.call(screen, "show")

    def pop(self):
        st = self.stack()
        if len(st) > 1 and self.can_leave():
            widgets.reset_editing()
            st.pop()
            self.call(st[-1], "show")
            return True
        return False

    def quit(self):
        if self.can_leave():
            self.running = False

    # ------------------------------------------------------------ context services

    def message(self, text, tone="normal"):
        self.toast = widgets.Toast(text, tone)

    def log(self, text):
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a") as f:
                f.write(f"{dt.datetime.now().astimezone():%Y-%m-%d %H:%M:%S} {text.rstrip()}\n")
        except OSError:
            pass

    def confirm(self, title, lines=(), second=None):
        return self.modal(widgets.ConfirmDialog(title, lines, second))

    def modal(self, w):
        """Run a modal widget until w.done; returns w.result."""
        prev, self.overlay = self.overlay, w
        try:
            while not w.done:
                self.draw()
                k = self.read_key()
                if k is None or k == RESIZE or self.too_small():
                    continue
                w.key(k)
            return w.result
        finally:
            self.overlay = prev

    def run_slow(self, fn, label):
        box = {}

        def work():
            try:
                box["value"] = fn()
            except BaseException as e:   # handed back to the caller below
                box["error"] = e

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(0.05)              # quick reads don't flash a spinner
        if t.is_alive():
            prev, self.spinner = self.spinner, widgets.Spinner(label)
            try:
                while t.is_alive():
                    self.draw()
                    self.poll_key(0.1)
                    self.spinner.tick()
            finally:
                self.spinner = prev
        if "error" in box:
            raise box["error"]
        return box.get("value")

    @contextlib.contextmanager
    def lock(self, path, timeout=10):
        prev = self.spinner
        waiting = widgets.Spinner("Waiting for the scheduled run to finish")

        def on_wait(elapsed):
            self.spinner = waiting
            waiting.tick()
            self.draw()
            self.poll_key(0)

        try:
            with run_lock(path, timeout, on_wait):
                self.spinner = prev
                yield
        finally:
            self.spinner = prev

    # ------------------------------------------------------------ keys

    def too_small(self):
        h, w = self.term.size()
        return w < MIN_W or h < MIN_H

    def handle(self, k):
        if k is None or k == RESIZE:
            return
        self.toast = None
        if self.too_small():
            if k == "q":
                self.running = False
            return
        screen = self.screen
        if widgets.active_editor() is not None and self.call(screen, "key", k, default=True):
            return
        if isinstance(k, str) and len(k) == 1 and k.isdigit() and k != "0":
            self.switch(int(k) - 1)
        elif k == "tab":
            self.switch((self.current + 1) % len(self.tabs))
        elif k == "backtab":
            self.switch((self.current - 1) % len(self.tabs))
        elif k == "q":
            self.quit()
        elif k == "?":
            if not isinstance(screen, core.HelpScreen):
                self.push(core.HelpScreen(self.core_ctx, self.screen_keys(screen), self.core_keys()))
        elif not self.call(screen, "key", k, default=True) and k == "esc":
            self.pop()

    def read_key(self):
        if self.typed_ahead:
            return self.typed_ahead.pop(0)
        return self.term.read_key()

    def poll_key(self, timeout):
        """While busy: wait up to timeout for a key and keep it for afterwards
        (a resize needs nothing kept: the next draw picks up the new size)."""
        k = self.term.read_key(timeout=timeout)
        if k is not None and k != RESIZE:
            self.typed_ahead.append(k)

    def press(self, *ks):
        """Feed keys to a headless terminal, handle them, draw. For tests."""
        self.term.feed(*ks)
        while self.running and (self.typed_ahead or self.term.pending()):
            self.handle(self.read_key())
        self.draw()
        return self.term.text()

    def run(self):
        self.start()
        while self.running:
            self.draw()
            self.handle(self.read_key())

    # ------------------------------------------------------------ drawing

    def screen_keys(self, screen):
        try:
            return list(screen.keys() or [])
        except Exception:
            return []

    def core_keys(self):
        n = len(self.tabs)
        ks = []
        if len(self.stack()) > 1:
            ks.append(("Esc", "back"))
        ks += [(f"1-{n}/Tab", "tabs"), ("?", "help"), ("q", "quit")]
        return ks

    def footer_keys(self):
        """(screen's keys, core keys) for the footer right now."""
        if self.overlay is not None:
            return self.overlay.keys(), []
        ed = widgets.active_editor()
        if ed is not None:
            return ed.keys(), []
        return self.screen_keys(self.screen), self.core_keys()

    def draw(self):
        canvas = self.term.begin_frame()
        try:
            if canvas.width < MIN_W or canvas.height < MIN_H:
                core.render_too_small(canvas, MIN_W, MIN_H)
                return
            h, w = canvas.height, canvas.width
            self.draw_tabs(canvas.sub(0, 0, 1, w))
            self.draw_header(canvas.sub(1, 0, 1, w))
            body = canvas.sub(2, 0, h - CHROME, w)
            try:
                self.screen.render(body)
            except Exception as e:
                self.log(traceback.format_exc())
                body.clear()
                body.text(0, 0, f"This screen failed to draw: {type(e).__name__}: {e}", "bad", bold=True)
            if self.overlay is not None:
                self.overlay.render(body)
            line = canvas.sub(h - 2, 0, 1, w)
            if self.spinner is not None:
                self.spinner.render(line, 0, 1)
            elif self.toast is not None:
                self.toast.render(line, 0, 1)
            else:
                line.text(0, 0, canvas.symbols["h"] * w, "dim")
            self.draw_footer(canvas.sub(h - 1, 0, 1, w))
        finally:
            self.term.end_frame()

    def draw_tabs(self, c):
        x = 0
        sep = c.symbols["sep"]
        full = sum(len(t.title) + 4 for t in self.tabs) <= c.width
        for i, t in enumerate(self.tabs):
            num = str(i + 1) if i < 9 else " "
            label = f" {num} {t.title} " if full or i == self.current else f" {num} "
            c.text(0, x, label, "selected" if i == self.current else "normal",
                   bold=i == self.current)
            x += len(label)
            if i < len(self.tabs) - 1:
                c.text(0, x, sep, "dim")
                x += 1
        if self.dry_run:
            c.text(0, c.width - 9, " DRY RUN ", "warn", bold=True)

    def draw_header(self, c):
        st = self.stack()
        titles = [getattr(s, "title", "") or self.tab.title for s in st]
        title = f" {c.symbols['right']} ".join(titles)
        owner = next((self.owners[id(s)] for s in reversed(st) if id(s) in self.owners), "")
        c.text(0, 1, widgets.fit(title, c.width - len(owner) - 6), "normal", bold=True)
        x = min(len(title), c.width - len(owner) - 6) + 2
        c.text(0, x, c.symbols["h"] * (c.width - x - len(owner) - 2), "dim")
        if owner:
            c.text(0, c.width - len(owner) - 1, owner, "dim")

    def draw_footer(self, c):
        own, core_keys = self.footer_keys()

        def width(ks):
            return sum(len(k) + len(lbl) + 3 for k, lbl in ks)

        right = width(core_keys)
        x = c.width - right
        for k, lbl in core_keys:
            c.text(0, x, k, "accent", bold=True)
            c.text(0, x + len(k) + 1, lbl)
            x += len(k) + len(lbl) + 3
        room = c.width - right - 1
        x = 1
        for i, (k, lbl) in enumerate(own):
            need = len(k) + len(lbl) + 3
            if x + need > room:
                c.text(0, x, c.symbols["more"] + " ?", "dim")
                break
            c.text(0, x, k, "accent", bold=True)
            c.text(0, x + len(k) + 1, lbl)
            x += need

