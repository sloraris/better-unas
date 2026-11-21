"""Terminals: where frames go and keys come from.

CursesTerminal drives a real terminal. HeadlessTerminal renders into a
TextSurface and replays a script of keys, so the whole app runs in tests and
with `--headless`. Both offer: size(), begin_frame() -> Canvas, end_frame(),
read_key(timeout=None) -> key name or None.
"""

import curses
import locale
import os

from . import keys
from .canvas import Canvas, TextSurface
from .theme import Theme


class ScriptDone(BaseException):
    """The headless key script ran out. BaseException so a screen's own
    error handling (and the app's) never swallows it."""


class HeadlessTerminal:
    """Keys are names ("down", "q") or ("resize", width, height)."""

    def __init__(self, width=80, height=24, keys=(), ascii=False):
        self.width, self.height, self.ascii = width, height, ascii
        self.script = list(keys)
        self.surface = TextSurface(width, height, ascii)
        self.frames = 0

    def feed(self, *ks):
        self.script += ks

    def pending(self):
        return bool(self.script)

    def size(self):
        return self.height, self.width

    def begin_frame(self):
        self.surface = TextSurface(self.width, self.height, self.ascii)
        return Canvas(self.surface)

    def end_frame(self):
        self.frames += 1

    def read_key(self, timeout=None):
        if timeout is not None:
            return None        # spinners and lock waits never eat scripted keys
        if not self.script:
            raise ScriptDone()
        k = self.script.pop(0)
        if isinstance(k, tuple) and k[0] == "resize":
            self.width, self.height = k[1], k[2]
            return keys.RESIZE
        return k

    # what tests look at
    def text(self):
        return self.surface.dump()

    def tones(self):
        return self.surface.dump_tones()


class CursesSurface:
    def __init__(self, win, attrs, width, height, ascii):
        self.win, self.attrs = win, attrs
        self.width, self.height, self.ascii = width, height, ascii

    def put(self, y, x, s, tone, bold):
        attr = self.attrs.get(tone, 0) | (curses.A_BOLD if bold else 0)
        try:
            self.win.addstr(y, x, s, attr)
        except curses.error:
            pass   # writing the bottom-right cell moves the cursor off screen; harmless


def wants_ascii():
    enc = (locale.getpreferredencoding(False) or "").lower().replace("-", "")
    return enc not in ("utf8", "utf_8")


class CursesTerminal:
    def __init__(self, stdscr, theme=None):
        self.win = stdscr
        self.theme = theme or Theme.from_env()
        self.attrs = self.theme.curses_attrs(curses)
        self.table = keys.curses_table(curses)
        self.ascii = wants_ascii()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)

    def size(self):
        return self.win.getmaxyx()

    def begin_frame(self):
        h, w = self.size()
        self.win.erase()
        return Canvas(CursesSurface(self.win, self.attrs, w, h, self.ascii))

    def end_frame(self):
        self.win.noutrefresh()
        curses.doupdate()

    def read_key(self, timeout=None):
        self.win.timeout(-1 if timeout is None else int(timeout * 1000))
        try:
            ch = self.win.get_wch()
        except curses.error:
            return None
        k = keys.from_char(ch, self.table)
        if k == keys.RESIZE:
            curses.update_lines_cols()
        return k


def run_curses(build, theme=None):
    """Start curses, build the app with build(terminal), run it, restore the terminal."""
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")   # Esc shouldn't wait a second

    def inner(stdscr):
        app = build(CursesTerminal(stdscr, theme))
        app.run()
        return app

    return curses.wrapper(inner)
