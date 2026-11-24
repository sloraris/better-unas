"""ctx.cached / ctx.forget: one slow read shared by the pages that open
after it, and `r` on Home reading everything live again. Run from the repo
root:

    python3 -m unittest discover -s core/tests
"""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.tui import app as app_module          # noqa: E402
from core.tui.app import App, Context           # noqa: E402
from core.tui.terminal import HeadlessTerminal  # noqa: E402
from core.tui.theme import Theme                # noqa: E402


class Tool:
    def __init__(self, name):
        self.name = name


class ReadCache(unittest.TestCase):
    def setUp(self):
        self.app = App(HeadlessTerminal(80, 28), theme=Theme(True))
        self.clock = [100.0]
        p = mock.patch.object(app_module.time, "monotonic", lambda: self.clock[0])
        p.start()
        self.addCleanup(p.stop)
        self.reads = []

    def read(self, value):
        def fn():
            self.reads.append(value)
            return value
        return fn

    def test_shared_until_it_expires(self):
        ctx = Context(self.app, Tool("a"))
        self.assertEqual(ctx.cached("k", self.read(1)), 1)
        self.assertEqual(ctx.cached("k", self.read(2)), 1)
        self.clock[0] += 59
        self.assertEqual(ctx.cached("k", self.read(3)), 1)
        self.clock[0] += 1
        self.assertEqual(ctx.cached("k", self.read(4)), 4)
        self.assertEqual(ctx.cached("k", self.read(5), ttl=0), 5)
        self.assertEqual(self.reads, [1, 4, 5])

    def test_forget_is_per_tool(self):
        a, b = Context(self.app, Tool("a")), Context(self.app, Tool("b"))
        a.cached("k", self.read("a1"))
        b.cached("k", self.read("b1"))
        a.forget()
        self.assertEqual(a.cached("k", self.read("a2")), "a2")
        self.assertEqual(b.cached("k", self.read("b2")), "b1")
        self.app.forget_cache()
        self.assertEqual(b.cached("k", self.read("b3")), "b3")

    def test_r_on_home_forgets_everything(self):
        self.app.start()
        Context(self.app, Tool("a")).cached("k", self.read(1))
        self.app.press("r")
        self.assertEqual(self.app.cache, {})


if __name__ == "__main__":
    unittest.main()
