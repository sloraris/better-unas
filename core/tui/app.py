"""The app shell: nav box, body, info box, action box and nav bar; the screen
stacks; key routing; and the per-tool context from docs/plugin-contract.md.

Layout at any size from 80x28 up (docs/tui-style.md §2):

    rows 0..2      nav box      tabs across the width; version in the top edge,
                                breadcrumb and tool name in the bottom edge
    row 3          blank
    rows 4..h-5    body         the screen's canvas (20 rows at 80x28)
    rows h-9..h-5  info box     only for screens with a form: the field's help
    rows h-4..h-2  action box   the screen's keys, or a toast or spinner
    row h-1        nav bar      back, tabs, help, quit

Smaller than 80x28 shows only a "please widen" screen.
"""

import contextlib
import datetime as dt
import threading
import time
import traceback

from .. import plugins as P
from . import screens as core
from .alerts import AlertsScreen
from . import calendar as cal
from .home import HomeScreen
from .settings import SettingsScreen
from . import widgets
from .widgets import fit as W_fit
from .keys import RESIZE
from .lock import LockBusy, run_lock
from .theme import Theme

MIN_W, MIN_H = 80, 28
# Rows outside the body (docs/tui-style.md §2): the nav box (3) and a blank
# line above it; the action box (3) and the nav bar below it.
HEAD, FOOT = 4, 4
CHROME = HEAD + FOOT
INFO_H = 5   # the info box above the action box: three lines of help (§8)


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

    def cached(self, key, fn, ttl=60):
        """fn()'s result, kept for ttl seconds under (this tool, key), so a
        slow read is shared by the pages that open one after another. `r` on
        Home or the Calendar forgets everything; a tool's own screens call
        forget() on their `r` and after an action changes something."""
        return self.app.cached((self.tool.name if self.tool else None, key), fn, ttl)

    def forget(self):
        """Drop this tool's cached reads."""
        self.app.forget_cache(self.tool.name if self.tool else None)

    # --- core business
    def with_values(self, values):
        """A copy whose config() always returns these values: a screen built
        on it keeps planning with unsaved settings (the Settings preview)."""
        ctx = Context(self.app, self.tool)
        ctx._preview = dict(values)
        return ctx

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
        self.restart = False                   # quit, then start the (new) better-unas again
        self.toast = self.spinner = self.overlay = None
        self.tools = list(tools)
        self.broken = dict(broken or {})       # tool folder name -> problems
        self.plugins, self.contexts = {}, {}
        self.core_ctx = Context(self)
        self.owners = {}                       # id(screen) -> tool name, for the header
        self.typed_ahead = []                  # keys pressed during a slow read or lock wait
        self.cache = {}                        # (tool name, key) -> (monotonic time, value): ctx.cached

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

        # The Calendar is the core's, drawn from every tool with a calendar hook.
        sources = cal.sources_for(self.plugins, self.contexts)
        if sources:
            tool_tabs.insert(0, ("Calendar", "Calendar", self.core_ctx,
                                 lambda ctx: cal.CalendarScreen(ctx, sources)))
        grouped = {}
        for title, label, ctx, make in tool_tabs:
            grouped.setdefault(title, []).append((label, ctx, make))
        self.tabs = [Tab("Home", [("Home", self.core_ctx, HomeScreen)])]
        self.tabs += [Tab(title, entries) for title, entries in grouped.items()]
        self.tabs += [Tab("Alerts", [("Alerts", self.core_ctx, AlertsScreen)]),
                      Tab("Settings", [("Settings", self.core_ctx, SettingsScreen)])]
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

    def request_restart(self):
        """Stop the loop and have the launcher start better-unas again, so an
        update or a tool change is loaded (core/tui/__main__.py does the exec)."""
        self.log("restart requested")
        self.restart, self.running = True, False

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

    def cached(self, key, fn, ttl):
        hit = self.cache.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = fn()
        self.cache[key] = (time.monotonic(), value)
        return value

    def forget_cache(self, tool=False):
        """Drop one tool's cached reads (its name, or None for the core's),
        or everything with no argument."""
        if tool is False:
            self.cache.clear()
        else:
            for k in [k for k in self.cache if k[0] == tool]:
                del self.cache[k]

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
            self.draw_nav_box(canvas.sub(0, 0, 3, w))
            info = self.screen_info()
            info_h = INFO_H if info is not None else 0
            body = canvas.sub(HEAD, 1, h - CHROME - info_h, w - 2)   # 1-column margins (tui-style.md §4)
            try:
                self.screen.render(body)
            except Exception as e:
                self.log(traceback.format_exc())
                body.clear()
                body.text(0, 0, f"This screen failed to draw: {type(e).__name__}: {e}", "bad", bold=True)
            if self.overlay is not None:
                self.overlay.render(body)
            if info_h:
                top = h - FOOT - info_h
                canvas.box(top, 0, info_h, w, tone="dim")
                for i, text in enumerate(widgets.wrap(info, w - 4)[:info_h - 2]):
                    canvas.text(top + 1 + i, 2, text, "dim")
            canvas.box(h - 4, 0, 3, w, tone="dim")
            line = canvas.sub(h - 3, 2, 1, w - 4)
            if self.spinner is not None:
                self.spinner.render(line, 0, 0)
            elif self.toast is not None:
                self.toast.render(line, 0, 0)
            else:
                self.draw_screen_keys(line)
            self.draw_footer(canvas.sub(h - 1, 0, 1, w))
        finally:
            self.term.end_frame()

    def screen_info(self):
        """The info box's text for the current screen, or None for no box: the
        screen's info() if it has one, else the help of the field selected in
        its form (a widgets.Form at screen.form, which then leaves it out)."""
        screen = self.screen
        try:
            if hasattr(screen, "info"):
                return screen.info()
            form = getattr(screen, "form", None)
        except Exception as e:
            self.log(f"info: {type(e).__name__}: {e}")
            return None
        if isinstance(form, widgets.Form):
            form.help_outside = True
            cur = form.current
            return (cur.help or "") if cur is not None else ""
        return None

    def draw_nav_box(self, c):
        """The nav box (docs/tui-style.md §2): the tabs spread across the width,
        the version (or DRY RUN) in its top edge at the right, and in its
        bottom edge the path below the tab, in accent (the tab itself is the
        highlighted button, so it isn't repeated), with the tool at the right."""
        w = c.width
        c.box(0, 0, 3, w, tone="dim")
        self.draw_tabs(c.sub(1, 1, 1, w - 2))
        right = w - 2
        badge = self.version_badge()
        if badge:
            update, text = badge
            text = f" {c.symbols['update']} {text} " if update else f" {text} "
            c.text(0, right - len(text), text, "warn" if update else "dim", bold=update)
            right -= len(text)
        if self.dry_run:
            c.text(0, right - 10, " DRY RUN ", "warn", bold=True)
        st = self.stack()
        owner = next((self.owners[id(s)] for s in reversed(st) if id(s) in self.owners), "")
        end = w - 2
        if owner:
            end -= len(owner) + 2
            c.text(2, end, f" {owner} ", "dim")
        crumbs = [getattr(s, "title", "") for s in st[1:]]
        if any(crumbs):
            path = f" {c.symbols['right']} ".join(t for t in crumbs if t)
            c.text(2, 2, f" {widgets.fit(path, max(1, end - 6))} ", "accent", bold=True)

    def draw_tabs(self, c):
        """Each tab gets an equal share of the row, separated by a dim bar; the
        current one is reversed across its share."""
        n = len(self.tabs)
        if not n:
            return
        sep = c.symbols["sep"]
        share, extra = divmod(c.width - (n - 1), n)
        x = 0
        for i, t in enumerate(self.tabs):
            cw = share + (1 if i < extra else 0)
            num = str(i + 1) if i < 9 else " "
            label = f"{num} {t.title}"
            if len(label) > cw - 2:
                label = num
            cur = i == self.current
            pad_l = (cw - len(label)) // 2
            cell = " " * pad_l + label + " " * (cw - pad_l - len(label))
            c.text(0, x, cell, "selected" if cur else "normal", bold=cur)
            x += cw
            if i < n - 1:
                c.text(0, x, sep, "dim")
                x += 1

    def version_badge(self):
        """(update, text) for the right end of the tab bar: (False, installed
        version), or (True, newer version) when an update is known. Only for an app
        with a home (not the demo or bare tests); re-read every few seconds."""
        home = getattr(self, "home", None)
        if not home:
            return None
        now = time.monotonic()
        cached = getattr(self, "_badge", None)
        if cached and now - cached[0] < 3:
            return cached[1]
        badge = None
        try:
            from .suite import updater_of
            up = updater_of(self)
            status = up.cached_status()
            if status and status.get("available") and status.get("latest"):
                latest = status["latest"]
                if status.get("branch") and status.get("latest_ref"):
                    latest = f"new commit {status['latest_ref'][:7]}"
                badge = (True, latest)
            else:
                label = up.installed_label()
                badge = (False, label) if label else None
        except Exception as e:
            self.log(f"version badge: {type(e).__name__}: {e}")
        self._badge = (now, badge)
        return badge

    def forget_badge(self):
        self._badge = None

    MOVES = ("↑", "↓", "←", "→", "Arrows", "PgUp", "PgDn", "Home", "End")

    @classmethod
    def is_move(cls, key):
        return any(m in key for m in cls.MOVES)

    def draw_screen_keys(self, c):
        """The action bar (docs/tui-style.md §2): the screen's own keys (or an
        editor's or a dialog's) as chips from the left, movement hints dim at
        the right end. Keys that don't fit give way to a `? more` chip."""
        own, _ = self.footer_keys()
        acts = [(k, l) for k, l in own if not self.is_move(k)]
        moves = [(k, l) for k, l in own if self.is_move(k)]
        right = "  ".join(f"{k} {l}" for k, l in moves)
        end = c.width - 1 - (len(right) + 2 if right else 0)
        # A chip is the key reversed with a space each side; its label follows
        # straight after (the padding separates them), then a 1-column gap:
        # the reversed chips already set the keys apart, and seven keys fit.
        more_w = len(" ? ") + len("more")
        x = 0
        for i, (k, lbl) in enumerate(acts):
            last = i == len(acts) - 1
            if i == 0:   # the first key always shows, its label cut to leave room
                room = end - x - len(k) - 2 - (0 if last else 1 + more_w)
                lbl = W_fit(lbl, max(1, room))
            need = len(k) + 2 + len(lbl)
            if i and x + need + (0 if last else 1 + more_w) > end:
                x = c.styled(0, x, "?", "chip")
                c.text(0, x, "more")
                x += 4
                break
            x = c.styled(0, x, k, "chip")
            c.text(0, x, lbl)
            x += len(lbl) + 1
        if right and len(right) + 2 <= c.width - x:
            c.text(0, c.width - 1 - len(right), right, "dim")

    def draw_footer(self, c):
        """The nav bar, the same on every screen and all dim: back and tabs on
        the left, help and quit on the right."""
        _, core_keys = self.footer_keys()
        left = [(k, l) for k, l in core_keys if k not in ("?", "q")]
        right = [(k, l) for k, l in core_keys if k in ("?", "q")]
        c.text(0, 1, "  ".join(f"{k} {l}" for k, l in left), "dim")
        text = "  ".join(f"{k} {l}" for k, l in right)
        c.text(0, c.width - 1 - len(text), text, "dim")
