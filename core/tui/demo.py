"""Placeholder Calendar and Drives tabs for the demo (`--demo`).

They stand in for smart-scheduler's real screens and exercise the
widgets: a table, a drill-down, a confirm with a second confirm, a slow read
with a spinner. Nothing here touches a drive.
"""

import time

from . import screens as core
from . import widgets as W

DRIVES = [
    ("1", "ZL2AAA01", "16 TB", "Passed, Tue 18 Nov", "good", "Sat 29 Nov", "34 C"),
    ("2", "ZL2AAA02", "16 TB", "Passed, Wed 19 Nov", "good", "Sun 30 Nov", "35 C"),
    ("3", "ZL2ABC03", "16 TB", "Failed, Thu 20 Nov", "bad", "Sat 22 Nov", "41 C"),
    ("4", "ZL2AAA04", "16 TB", "Never tested", "dim", "Mon 24 Nov", "33 C"),
    ("5", "WD9XYZ05", "12 TB", "Excluded", "dim", "-", "36 C"),
]


class CalendarPlaceholder:
    title = "Calendar"

    def __init__(self, ctx):
        self.ctx = ctx
        self.text = W.ScrollText([
            ("Demo: smart-scheduler's Calendar tab shows the month grid of drive tests.", "dim"), "",
            "The colors it uses:",
            ("  Passed or done                 green", "good"),
            ("  Planned, or a warning          yellow", "warn"),
            ("  Failed, or an alert            red", "bad"),
            ("  Past or disabled               dim", "dim"), "",
            ("Words always say the same thing as the color, so NO_COLOR loses nothing.", "dim"),
        ])

    def keys(self):
        return self.text.keys()

    def render(self, canvas):
        self.text.render(canvas)

    def key(self, k):
        return self.text.key(k)


class DrivesPlaceholder:
    title = "Drives"

    def __init__(self, ctx):
        self.ctx = ctx
        self.table = W.Table([("Slot", 4, "right"), ("Serial", 10), ("Size", 6), ("Last test", 20),
                              ("Next", 11), ("Temp", 5, "right")])
        self.loaded = False

    def keys(self):
        return [("Enter", "open"), ("t", "run test"), ("r", "refresh")] + self.table.keys()

    def show(self):
        self.ctx.run_slow(lambda: time.sleep(0.6), "Reading drives (demo)")
        self.table.set_rows([[s, ser, size, (last, tone), nxt, temp]
                             for s, ser, size, last, tone, nxt, temp in DRIVES])
        self.loaded = True

    def render(self, canvas):
        canvas.text(0, 0, "Demo data: smart-scheduler's Drives tab shows the real drives.", "dim")
        self.table.render(canvas.sub(2, 0, canvas.height - 2, canvas.width))

    def key(self, k):
        row = self.table.current
        if k == "enter" and row:
            d = next(d for d in DRIVES if d[1] == row[1])
            self.ctx.push(core.DetailScreen(f"Slot {d[0]}", [
                (f"{d[1]}, {d[2]}", "normal"), "",
                (f"Last test:    {d[3]}", d[4]),
                (f"Next planned: {d[5]}", "warn"),
                f"Temperature:  {d[6]}", "",
                ("Demo: the real Drives tab shows SMART values and test history.", "dim")]))
            return True
        if k == "t" and row:
            ok = self.ctx.confirm(f"Start an extended test on slot {row[0]} ({row[1]})?",
                                  ["It takes about 20 hours and slows the drive while it runs.",
                                   "Demo: the drive is busy right now."],
                                  second="The drive is busy. Start anyway?")
            self.ctx.message("Demo: nothing was started." if ok else "Not started.",
                             "warn" if ok else "dim")
            return True
        if k == "r":
            self.show()
            return True
        return self.table.key(k)


TABS = [("Calendar", CalendarPlaceholder), ("Drives", DrivesPlaceholder)]
