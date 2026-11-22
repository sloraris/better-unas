"""Tones and how they look.

Tones carry meaning (docs/tui-design.md, "Colors"): green good/done, yellow
planned/warning, red failed/alert, dim past/disabled. The terminal's own
background is always used. With NO_COLOR set (https://no-color.org: present
and not empty) nothing is colored; bad and warn turn bold instead, so meaning
never rides on color alone.
"""

import os

PLUGIN_TONES = ("normal", "good", "warn", "bad", "dim")
CORE_TONES = ("selected", "accent")
TONES = PLUGIN_TONES + CORE_TONES

# Text roles (docs/tui-style.md §3): what a piece of text is, mapped to how it
# looks. Widgets take a role so screens can't style text ad hoc.
ROLES = {
    "heading": ("accent", True),
    "label": ("dim", False),
    "value": ("normal", False),
    "strong": ("normal", True),
    "note": ("dim", False),       # drawn after the note glyph
    "reason": ("warn", False),    # drawn after the warning glyph
    "tag": ("selected", False),   # padded with a space each side
    "id": ("dim", False),
    "chip": ("selected", True),   # a key, padded with a space each side
}

# Status kinds: the glyph name in canvas symbols and the tone it's drawn in.
STATUS = {
    "ok": "good", "failed": "bad", "warning": "warn", "running": "accent",
    "planned": "warn", "scrub": "accent", "backup": "accent", "update": "warn",
}

COLOR_NAMES = {"good": "green", "warn": "yellow", "bad": "red", "accent": "cyan"}


def color_wanted(env=None):
    env = os.environ if env is None else env
    return not env.get("NO_COLOR")


class Theme:
    """What the TUI shows each tone as. `color` is False under NO_COLOR or on a
    terminal without colors."""

    def __init__(self, color=True):
        self.color = color
        self.tones = TONES

    @classmethod
    def from_env(cls, env=None):
        return cls(color_wanted(env))

    def describe(self, tone):
        """A plain description of a tone, e.g. for the help screen."""
        if tone == "selected":
            return "reverse"
        if tone == "dim":
            return "dim"
        if not self.color:
            return "bold" if tone in ("bad", "warn") else "plain"
        return COLOR_NAMES.get(tone, "default")

    def curses_attrs(self, curses, colors=None):
        """Map every tone to a curses attribute. Call after initscr()."""
        attrs = {t: curses.A_NORMAL for t in TONES}
        attrs["selected"] = curses.A_REVERSE
        attrs["accent"] = curses.A_BOLD
        attrs["dim"] = curses.A_DIM
        use_color = self.color and curses.has_colors()
        if not use_color:
            attrs["bad"] = attrs["warn"] = curses.A_BOLD
            return attrs
        curses.start_color()
        try:
            curses.use_default_colors()   # -1 = the terminal's own colors
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        pairs = {"good": curses.COLOR_GREEN, "warn": curses.COLOR_YELLOW,
                 "bad": curses.COLOR_RED, "accent": curses.COLOR_CYAN}
        n = 1
        for tone, fg in pairs.items():
            curses.init_pair(n, fg, bg)
            attrs[tone] = curses.color_pair(n) | (curses.A_BOLD if tone == "accent" else 0)
            n += 1
        if getattr(curses, "COLORS", 8) >= 256:
            # A mid grey reads as "dim" on light and dark backgrounds alike,
            # and some terminals ignore A_DIM.
            curses.init_pair(n, 244, bg)
            attrs["dim"] = curses.color_pair(n)
        return attrs
