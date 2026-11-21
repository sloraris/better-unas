"""Canvases: rectangles of character cells that screens and widgets draw on.

One Canvas class does the clipping and sub-regions; it writes to a surface.
TextSurface keeps cells in memory (the headless renderer used by tests and
`--headless`); the curses surface lives in terminal.py. Both take the same
calls, so a screen can't tell them apart (docs/plugin-contract.md, section 7).
"""

from .theme import TONES

UNICODE = {
    "h": "─", "v": "│", "tl": "┌", "tr": "┐", "bl": "└", "br": "┘",
    "up": "↑", "down": "↓", "more": "…", "marker": "›", "dot": "•",
    "left": "‹", "right": "›", "sep": "│",
}
ASCII = {
    "h": "-", "v": "|", "tl": "+", "tr": "+", "bl": "+", "br": "+",
    "up": "^", "down": "v", "more": "~", "marker": ">", "dot": "*",
    "left": "<", "right": ">", "sep": "|",
}


class Canvas:
    """A clipped region of a surface, with its own origin.

    Methods from the contract: width, height, text, fill, box, sub. Widgets
    also use `symbols` (box and arrow characters, Unicode or ASCII).
    """

    def __init__(self, surface, y0=0, x0=0, height=None, width=None):
        self.surface = surface
        self.y0, self.x0 = y0, x0
        self.height = surface.height if height is None else max(0, height)
        self.width = surface.width if width is None else max(0, width)
        self.symbols = ASCII if surface.ascii else UNICODE

    def text(self, y, x, s, tone="normal", bold=False):
        """Draw s at (y, x), clipped at the edges. No wrapping."""
        if tone not in TONES:
            raise ValueError(f"unknown tone {tone!r}")
        if not 0 <= y < self.height:
            return
        s = str(s).replace("\n", " ").replace("\t", " ")
        if x < 0:
            s, x = s[-x:], 0
        s = s[: max(0, self.width - x)]
        if s:
            self.surface.put(self.y0 + y, self.x0 + x, s, tone, bold)

    def fill(self, y, x, h, w, ch=" ", tone="normal"):
        for row in range(y, y + h):
            self.text(row, x, ch * w, tone)

    def clear(self):
        self.fill(0, 0, self.height, self.width)

    def box(self, y, x, h, w, title="", tone="normal"):
        """A line box. The title sits in the top edge."""
        if h < 2 or w < 2:
            return
        s = self.symbols
        self.text(y, x, s["tl"] + s["h"] * (w - 2) + s["tr"], tone)
        for row in range(y + 1, y + h - 1):
            self.text(row, x, s["v"], tone)
            self.text(row, x + w - 1, s["v"], tone)
        self.text(y + h - 1, x, s["bl"] + s["h"] * (w - 2) + s["br"], tone)
        if title and w > 4:
            self.text(y, x + 2, f" {title} "[: w - 4], tone, bold=True)

    def sub(self, y, x, h, w):
        """A canvas for part of this one; never reaches outside it."""
        if y < 0:
            h, y = h + y, 0
        if x < 0:
            w, x = w + x, 0
        h = max(0, min(h, self.height - y))
        w = max(0, min(w, self.width - x))
        return Canvas(self.surface, self.y0 + y, self.x0 + x, h, w)


class TextSurface:
    """Cells in memory: the headless renderer."""

    def __init__(self, width, height, ascii=False):
        self.width, self.height, self.ascii = width, height, ascii
        self.chars = [[" "] * width for _ in range(height)]
        self.tones = [["normal"] * width for _ in range(height)]
        self.bold = [[False] * width for _ in range(height)]

    def put(self, y, x, s, tone, bold):
        if not 0 <= y < self.height:
            return
        for i, ch in enumerate(s):
            if 0 <= x + i < self.width:
                self.chars[y][x + i] = ch
                self.tones[y][x + i] = tone
                self.bold[y][x + i] = bold

    def dump(self):
        """The screen as text, trailing spaces removed."""
        return "\n".join("".join(r).rstrip() for r in self.chars)

    TONE_CODES = {"normal": " ", "good": "g", "warn": "y", "bad": "r", "dim": "d",
                  "selected": "S", "accent": "a"}

    def dump_tones(self):
        """One letter per cell for its tone (space = normal), for color snapshots."""
        return "\n".join("".join(self.TONE_CODES[t] for t in r).rstrip() for r in self.tones)

    def find(self, needle):
        """(y, x) of the first occurrence of needle, or None."""
        for y, row in enumerate(self.chars):
            x = "".join(row).find(needle)
            if x >= 0:
                return y, x
        return None

    def tone_at(self, y, x):
        return self.tones[y][x]

    def row(self, y):
        return "".join(self.chars[y]).rstrip()


def headless(width=80, height=24, ascii=False):
    """A fresh headless canvas and its surface."""
    surface = TextSurface(width, height, ascii)
    return Canvas(surface), surface
