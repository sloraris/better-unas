"""Key names, as screens receive them (docs/plugin-contract.md, section 5).

A printable character stands for itself; everything else has a name.
"""

NAMED = ("up", "down", "left", "right", "enter", "esc", "tab", "backtab",
         "pgup", "pgdn", "home", "end", "backspace", "delete",
         *(f"f{n}" for n in range(1, 13)))

RESIZE = "resize"   # core-only pseudo key; screens never see it


def curses_table(curses):
    t = {
        curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left",
        curses.KEY_RIGHT: "right", curses.KEY_ENTER: "enter", curses.KEY_BTAB: "backtab",
        curses.KEY_PPAGE: "pgup", curses.KEY_NPAGE: "pgdn", curses.KEY_HOME: "home",
        curses.KEY_END: "end", curses.KEY_BACKSPACE: "backspace", curses.KEY_DC: "delete",
        curses.KEY_RESIZE: RESIZE,
    }
    for name in ("KEY_A1", "KEY_C1"):        # keypad Home/End on some terminals
        if hasattr(curses, name):
            t[getattr(curses, name)] = "home" if name == "KEY_A1" else "end"
    for n in range(1, 13):
        t[curses.KEY_F0 + n] = f"f{n}"
    return t


def from_char(ch, table=None):
    """Turn what get_wch() returned (str or int) into a key name, or None."""
    if isinstance(ch, int):
        return (table or {}).get(ch)
    if ch in ("\n", "\r"):
        return "enter"
    if ch == "\t":
        return "tab"
    if ch == "\x1b":
        return "esc"
    if ch in ("\x7f", "\x08"):
        return "backspace"
    if len(ch) == 1 and ch.isprintable():
        return ch
    return None


def is_printable(k):
    return isinstance(k, str) and len(k) == 1 and k.isprintable()
