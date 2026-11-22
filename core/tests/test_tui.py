"""TUI framework tests (issue #4). Run from the repository root:

    python3 -m unittest discover -s core/tests

Everything renders to the headless canvas; nothing needs a terminal or a NAS.
Snapshots live in core/tests/snapshots/. After an intended change to how
something looks, regenerate them and review the diff:

    UPDATE_SNAPSHOTS=1 python3 -m unittest discover -s core/tests
"""

import contextlib
import datetime as dt
import fcntl
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import plugins as P                              # noqa: E402
from core.tui import keys, widgets as W                    # noqa: E402
from core.tui import __main__ as entry                     # noqa: E402
from core.tui.app import App, Context                      # noqa: E402
from core.tui.canvas import headless, TextSurface, Canvas  # noqa: E402
from core.tui.lock import LockBusy, run_lock               # noqa: E402
from core.tui.terminal import HeadlessTerminal, ScriptDone  # noqa: E402
from core.tui.theme import Theme, color_wanted              # noqa: E402

SNAPSHOTS = os.path.join(HERE, "snapshots")
EXAMPLE = os.path.join(ROOT, "docs", "examples", "space-watch")
SS_SCHEMA = os.path.join(ROOT, "smart-scheduler", "settings.json")
CLOCK = dt.datetime(2026, 11, 22, 1, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7)))


class SnapshotMixin:
    def assertSnapshot(self, name, text):
        path = os.path.join(SNAPSHOTS, name + ".txt")
        if os.environ.get("UPDATE_SNAPSHOTS"):
            os.makedirs(SNAPSHOTS, exist_ok=True)
            with open(path, "w") as f:
                f.write(text + "\n")
        if not os.path.exists(path):
            self.fail(f"no snapshot {name}; run with UPDATE_SNAPSHOTS=1 to create it")
        with open(path) as f:
            want = f.read()[:-1]
        self.assertEqual(text, want, f"snapshot {name} differs; UPDATE_SNAPSHOTS=1 regenerates it")


# ------------------------------------------------------------------ a fixture tool

FIXTURE_PLUGIN = '''
CONTRACT = 1
SEEN = []

def status(ctx):
    return {"enabled": True, "state": "warn", "problems": ["Timer is missing."], "detail": ["Last run 01:00"]}

def summary(ctx):
    return {"state": "warn", "headline": "3 drives scheduled, 1 overdue", "items": [
        {"label": "Next test", "value": "Sat 22 Nov, slot 3", "tone": "warn"},
        {"label": "Last result", "value": "Passed, slot 5, Tue 18 Nov", "tone": "good"},
        {"label": "Failed", "value": "slot 2, Thu 20 Nov", "tone": "bad"}]}

def alerts(ctx):
    return [
        {"id": "test:ZL2", "level": "error", "active": True, "since": "2026-11-20T01:04:00-07:00",
         "message": "Extended test on slot 2 (ZL2) failed: read failure."},
        {"id": "hb", "level": "warn", "active": False, "since": "2026-11-18T03:00:00-07:00",
         "message": "Heartbeat URL did not answer."},
    ]

class Board:
    title = "Board"
    def __init__(self, ctx):
        self.ctx = ctx
        self.list = ctx.widgets.ListView(["alpha", ("beta (failed)", "bad"), ("gamma", "dim")])
    def keys(self):
        return [("Enter", "open"), ("b", "boom")]
    def show(self):
        SEEN.append("show")
    def render(self, canvas):
        self.list.render(canvas)
    def key(self, k):
        if k == "enter":
            self.ctx.push(Detail(self.ctx, self.list.current))
            return True
        if k == "b":
            raise RuntimeError("boom")
        return self.list.key(k)

class Detail:
    def __init__(self, ctx, item):
        self.ctx, self.item = ctx, item if isinstance(item, str) else item[0]
        self.title = "Item"
    def keys(self):
        return []
    def render(self, canvas):
        canvas.text(0, 0, f"Details of {self.item}")
    def key(self, k):
        return False

def screens(ctx):
    return [{"id": "board", "tab": "Board", "make": Board}]
'''


def write_tool(home, name, plugin=FIXTURE_PLUGIN, summary="Fixture tool for the TUI tests"):
    d = os.path.join(home, name)
    os.makedirs(d)
    with open(os.path.join(d, "tool.conf"), "w") as f:
        f.write(f"NAME={name}\nVERSION=1.2.3\nSUMMARY={summary}\nCHANGES=Nothing.\n"
                f"CLI=cli.py\nCONTRACT=1\nPLUGIN=plugin.py\n")
    with open(os.path.join(d, "cli.py"), "w") as f:
        f.write("")
    with open(os.path.join(d, "plugin.py"), "w") as f:
        f.write(plugin)
    return d


class AppFixture(SnapshotMixin):
    """A home with the space-watch example (disk reading pinned) and a fixture tool."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        shutil.copytree(EXAMPLE, os.path.join(self.home, "space-watch"),
                        ignore=shutil.ignore_patterns("__pycache__", "state.json", "config", ".lock"))
        write_tool(self.home, "fixture")
        self.addCleanup(W.reset_editing)
        # No update info, whether or not core.updates exists (test_home.py covers updates).
        no_updates = mock.patch.dict(sys.modules, {"core.updates": None})
        no_updates.start()
        self.addCleanup(no_updates.stop)

    def make_app(self, width=80, height=24, extra_tabs=(), **kw):
        tools, broken = P.discover(self.home)
        self.term = HeadlessTerminal(width, height)
        self.frames = []
        real_end = self.term.end_frame

        def end_frame():
            self.frames.append(self.term.text())
            real_end()

        self.term.end_frame = end_frame
        app = App(self.term, tools, broken, extra_tabs=extra_tabs, clock=lambda: CLOCK,
                  log_path=os.path.join(self.home, "tui.log"), theme=Theme(True), **kw)
        if "space-watch" in app.plugins:
            app.plugins["space-watch"].sw.measure = lambda path: 42.0
        app.start()
        app.draw()
        return app


# ------------------------------------------------------------------ canvas, theme, keys

class CanvasTests(unittest.TestCase):
    def test_text_clips_at_every_edge(self):
        c, s = headless(10, 3)
        c.text(0, -2, "abcdef")
        c.text(1, 7, "xyz123")
        c.text(5, 0, "never")
        c.text(-1, 0, "never")
        self.assertEqual(s.dump(), "cdef\n       xyz\n")

    def test_sub_has_own_origin_and_clipping(self):
        c, s = headless(12, 4)
        inner = c.sub(1, 2, 2, 5)
        self.assertEqual((inner.height, inner.width), (2, 5))
        inner.text(0, 0, "hello world")
        inner.text(1, 3, "zz top")
        inner.text(2, 0, "outside")
        nested = inner.sub(1, 4, 5, 5)          # asks for more than there is
        self.assertEqual((nested.height, nested.width), (1, 1))
        self.assertEqual(s.dump(), "\n  hello\n     zz\n")

    def test_fill_box_and_tones(self):
        c, s = headless(8, 4)
        c.fill(0, 0, 4, 8, ".", "dim")
        c.box(0, 0, 4, 8, title="Hi")
        c.text(1, 1, "ok", "good")
        self.assertEqual(s.dump(), "┌─ Hi ─┐\n│ok....│\n│......│\n└──────┘")
        self.assertEqual(s.tone_at(1, 1), "good")
        self.assertEqual(s.tone_at(2, 2), "dim")

    def test_ascii_symbols(self):
        c, s = headless(6, 2, ascii=True)
        c.box(0, 0, 2, 6)
        self.assertEqual(s.dump(), "+----+\n+----+")

    def test_unknown_tone_is_an_error(self):
        c, _ = headless(5, 1)
        with self.assertRaises(ValueError):
            c.text(0, 0, "x", tone="green")


class ThemeTests(unittest.TestCase):
    def test_no_color(self):
        self.assertTrue(color_wanted({}))
        self.assertTrue(color_wanted({"NO_COLOR": ""}))   # present but empty doesn't count
        self.assertFalse(color_wanted({"NO_COLOR": "1"}))
        self.assertFalse(Theme.from_env({"NO_COLOR": "yes"}).color)

    def test_curses_attrs_without_color_make_bad_and_warn_bold(self):
        class FakeCurses:
            A_NORMAL, A_BOLD, A_DIM, A_REVERSE = 0, 1, 2, 4

            @staticmethod
            def has_colors():
                return True

            def start_color(self):
                raise AssertionError("no colors under NO_COLOR")

        attrs = Theme(color=False).curses_attrs(FakeCurses())
        self.assertEqual(attrs["bad"], FakeCurses.A_BOLD)
        self.assertEqual(attrs["warn"], FakeCurses.A_BOLD)
        self.assertEqual(attrs["good"], FakeCurses.A_NORMAL)
        self.assertEqual(attrs["selected"], FakeCurses.A_REVERSE)

    def test_curses_attrs_with_color_use_terminal_background(self):
        pairs = []

        class FakeCurses:
            A_NORMAL, A_BOLD, A_DIM, A_REVERSE = 0, 1, 2, 4
            COLOR_GREEN, COLOR_YELLOW, COLOR_RED, COLOR_CYAN, COLOR_BLACK = 2, 3, 1, 6, 0
            COLORS = 256
            error = Exception

            @staticmethod
            def has_colors():
                return True

            def start_color(self):
                pass

            def use_default_colors(self):
                pass

            def init_pair(self, n, fg, bg):
                pairs.append((n, fg, bg))

            def color_pair(self, n):
                return n << 8

        attrs = Theme(color=True).curses_attrs(FakeCurses())
        self.assertTrue(all(bg == -1 for _, _, bg in pairs))
        self.assertIn((1, 2, -1), pairs)                      # good = green
        self.assertEqual(attrs["good"], 1 << 8)
        self.assertEqual(attrs["dim"], 5 << 8)                # grey on 256 colors

    def test_describe(self):
        self.assertEqual(Theme(True).describe("bad"), "red")
        self.assertEqual(Theme(False).describe("bad"), "bold")


class KeyTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(keys.from_char("\n"), "enter")
        self.assertEqual(keys.from_char("\t"), "tab")
        self.assertEqual(keys.from_char("\x1b"), "esc")
        self.assertEqual(keys.from_char("\x7f"), "backspace")
        self.assertEqual(keys.from_char("q"), "q")
        self.assertEqual(keys.from_char("é"), "é")
        self.assertIsNone(keys.from_char("\x01"))
        self.assertEqual(keys.from_char(260, {260: "left"}), "left")
        self.assertIsNone(keys.from_char(999, {}))

    def test_curses_table(self):
        import curses
        t = keys.curses_table(curses)
        self.assertEqual(t[curses.KEY_RESIZE], keys.RESIZE)
        self.assertEqual(t[curses.KEY_F0 + 12], "f12")
        self.assertEqual(t[curses.KEY_BTAB], "backtab")


# ------------------------------------------------------------------ run lock

class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, ".lock")

    def held_elsewhere(self):
        """True if another open file description can't take the lock now."""
        with open(self.path, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
            return False

    def test_held_inside_released_after(self):
        with run_lock(self.path):
            self.assertTrue(self.held_elsewhere())
        self.assertFalse(self.held_elsewhere())

    def test_released_on_exception(self):
        with self.assertRaises(KeyError):
            with run_lock(self.path):
                raise KeyError("x")
        self.assertFalse(self.held_elsewhere())

    def test_busy_waits_then_raises(self):
        waits = []
        now = [0.0]
        with open(self.path, "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            with self.assertRaises(LockBusy) as cm:
                with run_lock(self.path, timeout=1, on_wait=waits.append, poll=0.25,
                              clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s)):
                    self.fail("must not get the lock")
        self.assertEqual(waits, [0.0, 0.25, 0.5, 0.75])
        self.assertIn("scheduled run", str(cm.exception))


# ------------------------------------------------------------------ widgets

class WidgetTests(SnapshotMixin, unittest.TestCase):
    def setUp(self):
        self.addCleanup(W.reset_editing)

    def render(self, widget, w=40, h=8, **kw):
        c, s = headless(w, h)
        widget.render(c, **kw)
        return s

    def test_list_view_scrolls_and_marks_selection(self):
        lv = W.ListView([f"item {i}" for i in range(10)] + [("failed one", "bad")])
        for _ in range(7):
            lv.key("down")
        s = self.render(lv, 30, 5)
        self.assertSnapshot("list_scrolled", s.dump())
        self.assertEqual(s.tone_at(4, 2), "selected")
        lv.key("end")
        self.assertEqual(lv.current, ("failed one", "bad"))
        s = self.render(lv, 30, 5)
        self.assertIn("failed one", s.row(4))
        lv.key("home")
        self.assertEqual(lv.index, 0)
        self.assertFalse(lv.key("x"))
        self.assertEqual(self.render(W.ListView([], empty="Nothing.")).row(0), "  Nothing.")

    def test_table(self):
        t = W.Table([("Slot", 4, "right"), ("Serial", 10), ("Result", None)],
                    [["1", "ZL1", ("passed", "good")], ["12", "ZL2", ("failed: read failure on LBA 123456", "bad")],
                     ["3", "ZL3", ("planned", "warn")]])
        t.key("down")
        s = self.render(t, 40, 5)
        self.assertSnapshot("table", s.dump())
        self.assertEqual(s.tone_at(1, 18), "good")
        self.assertEqual(s.tone_at(2, 18), "selected")
        self.assertEqual(s.tone_at(3, 18), "warn")
        self.assertEqual(t.current[1], "ZL2")

    def test_table_without_selection_scrolls(self):
        t = W.Table([("N", 3)], [[str(i)] for i in range(10)], selectable=False)
        s = self.render(t, 10, 4)
        t.key("pgdn")
        s = self.render(t, 10, 4)
        self.assertEqual([s.row(y)[:5].strip() for y in range(1, 4)], ["3", "4", "5"])
        self.assertNotIn("selected", {s.tone_at(y, x) for y in range(4) for x in range(10)})

    def test_scroll_text_wraps_and_scrolls(self):
        st = W.ScrollText(["A first paragraph that is long enough to wrap onto more lines.",
                           ("", "normal"), ("Red line", "bad"), "l1", "l2", "l3", "l4"])
        s = self.render(st, 24, 4)
        self.assertSnapshot("scroll_text_top", s.dump())
        st.key("pgdn")
        s = self.render(st, 24, 4)
        self.assertSnapshot("scroll_text_paged", s.dump())
        st.key("end")
        s = self.render(st, 24, 4)
        self.assertEqual(s.row(3), "l4")

    def test_spinner_and_toast(self):
        sp = W.Spinner("Measuring")
        frames = []
        for _ in range(5):
            frames.append(self.render(sp, 20, 1).row(0))
            sp.tick()
        self.assertEqual(frames, ["| Measuring...", "/ Measuring...", "- Measuring...",
                                  "\\ Measuring...", "| Measuring..."])
        s = self.render(W.Toast("Saved, a long message that will not fit", "good"), 20, 1)
        self.assertEqual(s.row(0), "Saved, a long messa…")
        self.assertEqual(s.tone_at(0, 0), "good")

    def test_confirm_defaults_to_no(self):
        d = W.ConfirmDialog("Forget the peak?", ["The next run starts a new peak."])
        s = self.render(d, 60, 12)
        self.assertSnapshot("confirm", s.dump())
        y, x = s.find("[ No ]")
        self.assertEqual(s.tone_at(y, x), "selected")
        d.key("enter")
        self.assertTrue(d.done)
        self.assertFalse(d.result)

    def test_confirm_answers(self):
        for ks, want in ((["y"], True), (["n"], False), (["esc"], False),
                         (["right", "enter"], True), (["left", "left", "enter"], False)):
            d = W.ConfirmDialog("Sure?")
            for k in ks:
                d.key(k)
            self.assertEqual((d.done, d.result), (True, want), ks)

    def test_second_confirm(self):
        d = W.ConfirmDialog("Start a test on slot 3?", ["The drive is busy."], second="Start anyway?")
        d.key("y")
        self.assertFalse(d.done)
        s = self.render(d, 60, 12)
        self.assertSnapshot("confirm_second", s.dump())
        y, x = s.find("Start anyway?")
        self.assertEqual(s.tone_at(y, x), "bad")
        d.key("enter")                      # default is No again
        self.assertEqual((d.done, d.result), (True, False))
        d = W.ConfirmDialog("Start?", second="Really?")
        d.key("y")
        d.key("y")
        self.assertEqual((d.done, d.result), (True, True))

    def test_line_edit(self):
        e = W.LineEdit("abc")
        for k in ("left", "X", "home", "Y", "end", "backspace", "delete"):
            e.key(k)
        self.assertEqual(e.text, "YabX")
        n = W.LineEdit("", allowed=str.isdigit)
        for k in "1a2":
            n.key(k)
        self.assertEqual(n.text, "12")
        s = self.render(e, 10, 1, y=0, x=0, width=3)
        self.assertEqual(s.row(0), "bX")     # scrolled to keep the cursor (after the end) in view
        self.assertEqual(s.tone_at(0, 2), "selected")


class FormTests(SnapshotMixin, unittest.TestCase):
    """The form widget against smart-scheduler's worked-example schema, which
    uses every field type in the contract."""

    def setUp(self):
        with open(SS_SCHEMA) as f:
            self.schema = json.load(f)
        self.addCleanup(W.reset_editing)
        types = {f["type"] for f in P.fields(self.schema)}
        self.assertEqual(types, set(W.FIELD_TYPES), "the example should cover every field type")

    def form(self, raw=None, **kw):
        return W.Form(self.schema, raw or {}, **kw)

    def goto(self, form, key):
        form.scroll.index = [f.key_name for f in form.fields].index(key)

    def draw(self, form, w=80, h=20):
        c, s = headless(w, h)
        form.render(c)
        return s

    def test_renders_groups_fields_and_help(self):
        form = self.form({"WEBHOOK_URL": "https://hooks.example/abc", "INTERVAL_DAYS": "0"})
        s = self.draw(form)
        self.assertSnapshot("form_top", s.dump())
        self.goto(form, "WEBHOOK_URL")
        s = self.draw(form)
        self.assertSnapshot("form_notifications", s.dump())
        self.assertNotIn("hooks.example", s.dump())        # secret stays masked
        self.assertEqual(form.errors(), {"INTERVAL_DAYS": "value must be at least 1"})

    def test_int_field_validates_as_you_type(self):
        form = self.form()
        self.goto(form, "INTERVAL_DAYS")
        form.key("enter")
        self.assertIs(W.active_editor(), form.current)
        for k in ("backspace", "backspace", "x", "0"):     # x is filtered out
            form.key(k)
        self.assertEqual(form.current.text, "0")
        self.assertEqual(form.current.error, "value must be at least 1")
        s = self.draw(form)
        self.assertIn("! value must be at least 1", s.dump())
        for k in ("backspace", "4", "5", "enter"):
            form.key(k)
        self.assertIsNone(W.active_editor())
        self.assertEqual(form.changed(), {"INTERVAL_DAYS": "45"})
        self.assertEqual(form.values()["INTERVAL_DAYS"], 45)
        y, _ = self.draw(form).find("Healthy drive")
        self.assertEqual(self.draw(form).row(y)[0], "*")    # changed marker

    def test_esc_cancels_an_edit(self):
        form = self.form()
        self.goto(form, "DRIVE_USER")
        for k in ("enter", "b", "o", "b", "esc"):
            form.key(k)
        self.assertEqual(form.changed(), {})
        self.assertIsNone(W.active_editor())

    def test_secret_shows_while_editing(self):
        form = self.form({"WEBHOOK_URL": "https://x.example/t"})
        self.goto(form, "WEBHOOK_URL")
        form.key("enter")
        self.assertIn("https://x.example/t", self.draw(form).dump())

    def test_bool_toggle(self):
        form = self.form({"ENABLED": "true"})
        f = form.field("ENABLED")
        self.assertFalse(f.changed)
        form.key(" ")
        self.assertEqual(f.text, "no")
        form.key("enter")
        self.assertEqual(f.text, "yes")
        self.assertFalse(f.changed)                          # same value as "true"
        self.assertIn("[x] yes", self.draw(form).dump())

    def test_choice(self):
        form = self.form()
        self.goto(form, "WEBHOOK_FORMAT")
        form.key("right")
        self.assertEqual(form.current.text, "json")
        form.key("enter")
        s = self.draw(form)
        self.assertSnapshot("form_choice_popup", s.dump())
        form.key("down")
        form.key("down")
        form.key("enter")
        self.assertEqual(form.current.text, "discord")
        form.key("enter")
        form.key("up")
        form.key("esc")
        self.assertEqual(form.current.text, "discord")

    def test_time_window(self):
        form = self.form()
        self.goto(form, "START_WINDOW")
        self.assertIn("00:00 to 05:00", self.draw(form).dump())
        form.key("enter")
        for k in "2200":
            form.key(k)
        self.assertEqual(form.current.text, "22:00-05:00")
        s = self.draw(form)
        y, x = s.find("22:00-05:00")
        self.assertEqual(s.tone_at(y, x + 6), "selected")    # cursor on the end hour
        for k in "0":
            form.key(k)
        form.key("9")                                         # 09:00 -> invalid? no: "09"
        self.assertEqual(form.current.text, "22:00-09:00")
        form.key("left")
        form.key("left")
        form.key("3")                                         # 39:00 is not a time
        self.assertEqual(form.current.error, "want HH:MM-HH:MM, e.g. 22:00-04:00")
        form.key("esc")
        self.assertEqual(form.current.text, "00:00-05:00")

    def test_serial_list_editor(self):
        form = self.form({"EXCLUDE_SERIALS": "ZL1"}, suggestions={"EXCLUDE_SERIALS": ["ZL1", "ZL2", "ZL3"]})
        self.goto(form, "EXCLUDE_SERIALS")
        form.key("enter")
        form.key("down")                 # to "+ add"
        form.key("enter")                # picker offers ZL2, ZL3, Other
        s = self.draw(form)
        self.assertSnapshot("form_serial_picker", s.dump())
        form.key("down")
        form.key("enter")                # ZL3
        form.key("down")
        form.key("enter")                # picker again
        form.key("end")
        form.key("enter")                # Other...
        for k in "BAD SERIAL":
            form.key(k)
        form.key("enter")
        self.assertEqual(form.current.text, "ZL1,ZL3,BAD SERIAL")
        self.assertIn("doesn't look like a serial", form.current.error)
        form.key("d")                    # delete the selected (last) row
        form.key("home")
        form.key("d")
        self.assertEqual(form.current.text, "ZL3")
        form.key("u")                    # undo everything since opening
        self.assertEqual(form.current.text, "ZL1")
        form.key("esc")
        self.assertIsNone(W.active_editor())

    def test_serial_map_editor(self):
        form = self.form({"INTERVAL_OVERRIDES": "ZL1:30"})
        self.goto(form, "INTERVAL_OVERRIDES")
        form.key("enter")
        form.key("end")
        form.key("enter")
        for k in "ZL9":
            form.key(k)
        form.key("tab")
        for k in "1a20":                # the number part takes digits only
            form.key(k)
        s = self.draw(form)
        self.assertSnapshot("form_serial_map_edit", s.dump())
        form.key("enter")
        self.assertEqual(form.current.text, "ZL1:30,ZL9:120")
        self.assertEqual(form.current.value, {"ZL1": 30, "ZL9": 120})
        form.key("home")
        form.key("enter")
        form.key("tab")
        form.key("backspace")
        form.key("backspace")
        form.key("enter")               # number now empty
        self.assertEqual(form.current.error, "bad entry 'ZL1:' (want SERIAL:DAYS)")
        form.key("esc")

    def test_int_list_and_float_filters(self):
        form = self.form()
        self.goto(form, "WARN_ATTRIBUTES")
        form.key("enter")
        for k in ",9x9":
            form.key(k)
        form.key("enter")
        self.assertEqual(form.current.text, "5,187,197,198,99")
        self.goto(form, "DURATION_FACTOR")
        form.key("enter")
        for k in ("backspace", "backspace", "backspace", "2", "e", ".", "5", "enter"):
            form.key(k)
        self.assertEqual(form.current.value, 2.5)

    def test_extra_errors_from_validate(self):
        form = self.form()
        form.set_extra_errors({"WARN_INTERVAL_DAYS": "should be shorter than the healthy interval"})
        self.assertEqual(form.errors(), {"WARN_INTERVAL_DAYS": "should be shorter than the healthy interval"})
        self.goto(form, "WARN_INTERVAL_DAYS")
        self.assertIn("! should be shorter", self.draw(form).dump())


# ------------------------------------------------------------------ the app

class AppTests(AppFixture, unittest.TestCase):
    def test_home_frame(self):
        app = self.make_app()
        self.assertSnapshot("app_home", self.term.text())
        self.assertSnapshot("app_home_tones", self.term.tones())
        self.assertEqual([t.title for t in app.tabs],
                         ["Home", "Board", "Space", "Settings", "Alerts", "Tools"])

    def test_placeholder_tabs_go_first_and_match_the_design(self):
        from core.tui import demo
        app = self.make_app(extra_tabs=demo.TABS)
        self.assertEqual([t.title for t in app.tabs],
                         ["Home", "Calendar", "Drives", "Board", "Space", "Settings", "Alerts", "Tools"])

    def test_number_keys_and_tab_switch_tabs(self):
        app = self.make_app()
        app.press("3")
        self.assertEqual(app.tab.title, "Space")
        app.press("tab")
        self.assertEqual(app.tab.title, "Settings")
        app.press("backtab", "backtab")
        self.assertEqual(app.tab.title, "Board")
        app.press("9")                        # no tab 9: nothing happens
        self.assertEqual(app.tab.title, "Board")
        app.press("6", "tab")                 # wraps around
        self.assertEqual(app.tab.title, "Home")
        app.press("backtab")
        self.assertEqual(app.tab.title, "Tools")

    def test_space_watch_is_a_working_screen(self):
        app = self.make_app()
        text = app.press("3")
        self.assertSnapshot("app_space_watch", text)
        self.assertIn("42.0%", text)
        sw = app.plugins["space-watch"].sw
        state = os.path.join(self.home, "space-watch", "state.json")
        sw.save_state({"peak": 97.0}, state)
        lock = os.path.join(self.home, "space-watch", ".lock")
        held = []
        real_save = sw.save_state

        def save_and_check(st, path):
            with open(lock, "a") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(False)
                    fcntl.flock(f, fcntl.LOCK_UN)
                except BlockingIOError:
                    held.append(True)
            real_save(st, path)

        sw.save_state = save_and_check
        app.press("x", "y")
        self.assertTrue(any("Forget the peak reading?" in f for f in self.frames))
        self.assertEqual(held, [True])        # saved while holding the tool's lock
        self.assertEqual(sw.load_state(state), {})
        self.assertIn("Peak forgotten.", self.term.text())
        y, x = self.term.surface.find("Peak forgotten.")
        self.assertEqual(self.term.surface.tone_at(y, x), "good")
        app.press("down")                     # the toast goes away on the next key
        self.assertNotIn("Peak forgotten.", self.term.text())

    def test_confirm_no_leaves_things_alone(self):
        app = self.make_app()
        app.press("3", "x", "enter")          # default answer is No
        self.assertNotIn("Peak forgotten.", self.term.text())
        self.assertIsNone(app.overlay)

    def test_lock_busy_becomes_a_message(self):
        app = self.make_app()
        app.press("3")
        app.plugins["space-watch"].sw.save_state({"peak": 97.0},
                                                 os.path.join(self.home, "space-watch", "state.json"))
        lock = os.path.join(self.home, "space-watch", ".lock")
        ctx = app.contexts["space-watch"]
        real = ctx.lock
        ctx.lock = lambda timeout=10: real(timeout=0.3)
        with open(lock, "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            text = app.press("x", "y")
        self.assertIn("Busy: the scheduled run is still going", text)
        self.assertIsNone(app.spinner)

    def test_screen_stack_enter_and_esc(self):
        app = self.make_app()
        self.assertEqual(app.plugins["fixture"].SEEN, [])
        app.press("2")
        self.assertEqual(app.plugins["fixture"].SEEN, ["show"])
        text = app.press("down", "enter")
        self.assertSnapshot("app_pushed", text)
        self.assertIn("Board › Item", text)
        self.assertIn("Esc back", text)
        app.press("esc")
        self.assertEqual(app.plugins["fixture"].SEEN, ["show", "show"])   # shown again
        self.assertNotIn("Esc back", self.term.text())
        app.press("esc")                      # on a root screen: nothing
        self.assertEqual(app.tab.title, "Board")
        self.assertEqual(len(app.stack()), 1)

    def test_each_tab_keeps_its_stack(self):
        app = self.make_app()
        app.press("2", "enter", "1", "2")
        self.assertIn("Details of alpha", self.term.text())

    def test_help(self):
        app = self.make_app()
        text = app.press("2", "?")
        self.assertSnapshot("app_help", text)
        app.press("?")                        # not stacked twice
        self.assertEqual(len(app.stack()), 2)
        app.press("esc")
        self.assertEqual(len(app.stack()), 1)

    def test_q_quits(self):
        app = self.make_app()
        app.press("q")
        self.assertFalse(app.running)

    def test_typing_into_a_field_never_quits_or_switches(self):
        app = self.make_app()
        app.press("4", "enter")               # Settings > space-watch
        self.assertIn("Settings › space-watch", self.term.text())
        app.press("down", "enter")            # edit "Folder to watch"
        self.assertIsNotNone(W.active_editor())
        text = app.press("q", "2", "tab")
        self.assertTrue(app.running)
        self.assertEqual(app.tab.title, "Settings")
        self.assertIn("Enter done", text)     # footer shows the editor's keys
        self.assertNotIn("q quit", text)
        app.press("esc")                      # cancels the edit, not the screen
        self.assertIn("Settings › space-watch", self.term.text())
        self.assertIn("q quit", self.term.text())

    def test_leaving_a_changed_form_asks(self):
        app = self.make_app()
        app.press("4", "enter", " ")          # toggle Enabled
        app.press("esc", "n")                 # discard? no
        self.assertIn("Settings › space-watch", self.term.text())
        app.press("2", "n")                   # switching tabs asks too
        self.assertEqual(app.tab.title, "Settings")
        app.press("esc", "y")
        self.assertNotIn("space-watch ─", self.term.text())
        self.assertEqual(len(app.stack()), 1)

    def test_footer_always_lists_keys(self):
        # Shared keys sit in the rule above the footer; the screen's own keys
        # get the footer row to themselves.
        app = self.make_app()
        for k in ("2", "enter", "esc", "3", "4", "enter", "esc", "5", "6", "enter", "esc", "1"):
            rule = app.press(k).split("\n")[22]
            self.assertIn("q quit", rule, k)
            self.assertIn("? help", rule, k)

    def test_screen_keys_get_the_whole_footer(self):
        class Seven:
            title = "Seven"

            def keys(self):   # 79 columns of keys, like smart-scheduler's Drives screen
                return [("Enter", "details"), ("t", "test"), ("z", "snooze"), ("x", "exclude"),
                        ("i", "interval"), ("u", "unsnooze"), ("r", "refresh")]

            def render(self, c):
                pass

            def key(self, k):
                return False

        app = self.make_app(80, 24, extra_tabs=[("Seven", lambda ctx: Seven())])
        lines = app.press("2").splitlines()
        self.assertEqual(lines[-1].strip(),
                         "Enter details  t test  z snooze  x exclude  i interval  u unsnooze  r refresh")
        self.assertNotIn("…", lines[-1])
        self.assertTrue(lines[-2].rstrip().endswith("q quit ──"), lines[-2])

    def test_footer_cuts_long_key_lists_but_keeps_core_keys(self):
        class Busy:
            title = "Busy"

            def keys(self):
                return [(f"F{i}", "does a thing") for i in range(12)]

            def render(self, c):
                pass

            def key(self, k):
                return False

        app = self.make_app(extra_tabs=[("Busy", lambda ctx: Busy())])
        lines = app.press("2").splitlines()
        self.assertIn("F0 does a thing", lines[-1])
        self.assertTrue(lines[-1].rstrip().endswith("… ?"), lines[-1])
        self.assertIn("q quit", lines[-2])

    def test_footer_overflow_marker_always_fits(self):
        class Crowded:
            title = "Crowded"
            fill = 0

            def keys(self):
                return [("a", "x" * self.fill), ("b", "second"), ("c", "third")]

            def render(self, c):
                pass

            def key(self, k):
                return False

        screen = Crowded()
        app = self.make_app(80, 24, extra_tabs=[("Crowded", lambda ctx: screen)])
        app.press("2")
        for fill in range(40, 70):
            screen.fill = fill
            app.draw()
            last = self.term.text().splitlines()[-1].rstrip()
            self.assertLessEqual(len(last), 79, (fill, last))
            self.assertTrue(last.endswith("c third") or last.endswith("… ?"), (fill, last))
            self.assertIn("a " + "x" * fill, last, fill)

    def test_alerts_and_tools(self):
        app = self.make_app()
        text = app.press("5")
        self.assertSnapshot("app_alerts", text)
        text = app.press("6")
        self.assertSnapshot("app_tools", text)
        text = app.press("enter")
        self.assertIn("Timer is missing.", text)

    def test_dry_run_marker(self):
        app = self.make_app(dry_run=True)
        self.assertIn("DRY RUN", self.term.text().splitlines()[0])
        self.assertTrue(app.contexts["space-watch"].dry_run)

    def test_screen_errors_are_contained(self):
        app = self.make_app()
        text = app.press("2", "b")
        self.assertIn("RuntimeError: boom", text)
        self.assertTrue(app.running)

        class BadRender:
            title = "Bad"

            def keys(self):
                return []

            def render(self, c):
                raise ValueError("no")

            def key(self, k):
                return False

        app.push(BadRender())
        app.draw()
        self.assertIn("This screen failed to draw: ValueError: no", self.term.text())
        with open(os.path.join(self.home, "tui.log")) as f:
            self.assertIn("ValueError: no", f.read())


class SizeTests(AppFixture, unittest.TestCase):
    def test_too_small_shows_only_please_widen(self):
        for w, h in ((79, 24), (80, 23), (40, 10)):
            app = self.make_app(w, h)
            text = self.term.text()
            self.assertIn("Please widen the terminal.", text)
            self.assertNotIn("Home", text)
            if (w, h) == (79, 24):
                self.assertSnapshot("too_small", text)
            app.press("2")                     # keys are ignored...
            self.assertEqual(app.tab.title, "Home")
            app.press("q")                     # ...except q
            self.assertFalse(app.running)

    def test_tiny_terminal_does_not_crash(self):
        for w, h in ((1, 1), (5, 2), (0, 0)):
            self.make_app(w, h)

    def test_resize_down_and_back(self):
        app = self.make_app()
        app.press("2", "enter")
        text = app.press(("resize", 60, 20))
        self.assertIn("this one is 60x20", text)
        text = app.press(("resize", 80, 24))
        self.assertIn("Board › Item", text)    # state survived
        text = app.press(("resize", 120, 40))
        self.assertSnapshot("app_120x40", text)
        self.assertEqual(len(text.split("\n")), 40)

    def test_resize_during_a_dialog(self):
        app = self.make_app()
        app.press("3")
        app.plugins["space-watch"].sw.save_state({"peak": 97.0},
                                                 os.path.join(self.home, "space-watch", "state.json"))
        self.frames.clear()
        app.press("x", ("resize", 70, 24), "y", ("resize", 80, 24), "n")
        texts = [f for f in self.frames]
        small = [i for i, f in enumerate(texts) if "this one is 70x24" in f]
        self.assertTrue(small)
        self.assertIn("Forget the peak reading?", texts[small[-1] + 1])   # back after widening
        self.assertIsNone(app.overlay)
        state = app.plugins["space-watch"].sw.load_state(os.path.join(self.home, "space-watch", "state.json"))
        self.assertEqual(state, {"peak": 97.0})   # "y" was ignored while too small

    def test_dialog_survives_resize_then_answers(self):
        app = self.make_app()
        app.press("3", "x", ("resize", 100, 30), "n")
        self.assertIsNone(app.overlay)
        self.assertIn("Forget the peak reading?", self.frames[-2])
        self.assertEqual(len(self.frames[-2].splitlines()), 30)

    def test_body_is_twenty_rows_at_minimum_size(self):
        seen = []

        class Measure:
            title = "M"

            def keys(self):
                return []

            def render(self, c):
                seen.append((c.height, c.width))

            def key(self, k):
                return False

        app = self.make_app(extra_tabs=[("M", lambda ctx: Measure())])
        app.press("2")
        self.assertEqual(seen[-1], (20, 80))


class PluginLoadingTests(AppFixture, unittest.TestCase):
    def test_broken_plugin_keeps_the_rest_working(self):
        write_tool(self.home, "broken-one", plugin="CONTRACT = 1\nraise ImportError('missing helper')\n")
        write_tool(self.home, "bad-screens", plugin=FIXTURE_PLUGIN.replace('"tab": "Board"', '"tab": "Home"'))
        app = self.make_app()
        self.assertIn("broken-one", app.broken)
        self.assertIn("screens: tab 'Home' belongs to the core", app.broken["bad-screens"])
        text = app.press(str(len(app.tabs)))
        self.assertIn("broken-one", text)
        self.assertIn("broken", text)
        self.assertIn("Board", self.term.text().splitlines()[0])   # the fixture still loads

    def test_two_tools_on_one_tab_get_a_chooser(self):
        write_tool(self.home, "second")
        app = self.make_app()
        self.assertEqual([t.title for t in app.tabs].count("Board"), 1)
        text = app.press("2")
        self.assertIn("More than one tool has a Board screen.", text)
        self.assertIn("fixture: board", text)
        self.assertIn("second: board", text)
        text = app.press("down", "enter")
        self.assertIn("Board › Board", text)
        self.assertIn("second", text.splitlines()[1])   # header names the tool
        first = app.stack()[-1]
        app.press("esc", "down", "enter")
        self.assertIs(app.stack()[-1], first)            # make() ran once

    def test_make_failure_shows_an_error_screen(self):
        write_tool(self.home, "fails", plugin=FIXTURE_PLUGIN.replace(
            "self.ctx = ctx\n", "raise OSError('no drive api')\n", 1).replace('"Board"', '"Fails"'))
        app = self.make_app()
        idx = [t.title for t in app.tabs].index("Fails") + 1
        text = app.press(str(idx))
        self.assertIn("This screen could not be opened.", text)
        self.assertIn("OSError: no drive api", text)

    def test_tool_without_plugin_gets_a_plain_card(self):
        shutil.copytree(os.path.join(ROOT, "smart-scheduler"), os.path.join(self.home, "smart-scheduler"),
                        ignore=shutil.ignore_patterns("__pycache__", "tests", "config", "state.json"))
        # smart-scheduler has a plugin since #10; drop it from the copy's manifest.
        conf = os.path.join(self.home, "smart-scheduler", "tool.conf")
        with open(conf) as f:
            lines = [line for line in f if not line.startswith("PLUGIN=")]
        with open(conf, "w") as f:
            f.writelines(lines)
        app = self.make_app()
        self.assertIn("No status available.", self.term.text())
        self.assertNotIn("smart-scheduler", app.broken)


class ContextTests(AppFixture, unittest.TestCase):
    def test_members(self):
        app = self.make_app()
        ctx = app.contexts["space-watch"]
        self.assertEqual(ctx.now(), CLOCK)
        self.assertEqual(ctx.config()["WARN_PERCENT"], 90)
        self.assertEqual(ctx.config_errors(), {})
        self.assertIs(ctx.LockBusy, LockBusy)
        self.assertIs(ctx.widgets, W)
        self.assertTrue(ctx.theme.color)
        with ctx.preview({"WARN_PERCENT": 60}):
            self.assertEqual(ctx.config(), {"WARN_PERCENT": 60})
        self.assertEqual(ctx.config()["WARN_PERCENT"], 90)
        ctx.log("hello")
        with open(os.path.join(self.home, "tui.log")) as f:
            self.assertIn("[space-watch] hello", f.read())
        with self.assertRaises(RuntimeError):
            app.core_ctx.lock()

    def test_run_slow_shows_a_spinner_and_passes_results(self):
        app = self.make_app()
        frames = []
        real_end = self.term.end_frame

        def end_frame():
            frames.append(self.term.text().splitlines()[-2])
            real_end()

        self.term.end_frame = end_frame
        ctx = app.contexts["space-watch"]
        self.assertEqual(ctx.run_slow(lambda: (time.sleep(0.3), 7)[1], "Measuring"), 7)
        self.assertTrue(any("Measuring..." in f for f in frames), frames)
        self.assertIsNone(app.spinner)
        with self.assertRaises(ZeroDivisionError):
            ctx.run_slow(lambda: 1 / 0, "Dividing")

    def test_keys_typed_during_a_slow_read_are_kept(self):
        app = self.make_app()
        typed = ["down", None, ("resize", 100, 30)]
        real = self.term.read_key

        def read_key(timeout=None):
            if timeout is not None and typed:
                k = typed.pop(0)
                if isinstance(k, tuple):
                    self.term.width, self.term.height = k[1], k[2]
                    return "resize"
                return k
            return real(timeout)

        self.term.read_key = read_key
        app.contexts["space-watch"].run_slow(lambda: time.sleep(0.4), "Slow")
        self.assertEqual(app.typed_ahead, ["down"])          # resize isn't queued
        self.assertEqual(len(self.frames[-1].splitlines()), 30)
        app.press()
        self.assertEqual(app.typed_ahead, [])

    def test_quick_reads_do_not_flash_a_spinner(self):
        app = self.make_app()
        before = self.term.frames
        app.contexts["space-watch"].run_slow(lambda: 1, "Quick")
        self.assertEqual(self.term.frames, before)

    def test_confirm_runs_modally_with_its_own_footer(self):
        app = self.make_app()
        frames = []
        real_end = self.term.end_frame

        def end_frame():
            frames.append(self.term.text())
            real_end()

        self.term.end_frame = end_frame
        self.term.feed("right", "enter")
        self.assertTrue(app.confirm("Go?", ["Line"], second=None))
        self.assertIn("y yes", frames[0].splitlines()[-1])
        self.assertNotIn("q quit", frames[0].splitlines()[-1])
        with self.assertRaises(ScriptDone):    # the dialog waits for a key
            app.confirm("Again?")
        self.assertIsNone(app.overlay)


class EntryPointTests(unittest.TestCase):
    def test_demo_runs_headless(self):
        out = io.StringIO()
        self.assertEqual(entry.main(["--demo", "--headless", "80x24", "--keys", "3 down enter"], out=out), 0)
        text = out.getvalue()
        self.assertIn("1 Home │ 2 Calendar │ 3 Drives │ 4 Space │ 5 Settings │ 6 Alerts │ 7 Tools", text)
        self.assertIn("Drives › Slot 2", text)

    def test_demo_second_confirm_headless(self):
        out = io.StringIO()
        entry.main(["--demo", "--headless", "80x24", "--keys", "3 t y y"], out=out)
        self.assertIn("Demo: nothing was started.", out.getvalue())
        out = io.StringIO()
        entry.main(["--demo", "--headless", "80x24", "--keys", "3 t y enter"], out=out)
        self.assertIn("Not started.", out.getvalue())

    def test_key_parsing(self):
        self.assertEqual(entry.parse_keys("2 space resize:100x30 q"), ["2", " ", ("resize", 100, 30), "q"])


if __name__ == "__main__":
    unittest.main()
