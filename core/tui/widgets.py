"""The core's widgets. Each one draws on any canvas it is given and takes key
names (keys.py); none of them touches curses, so they render the same in the
headless canvas.

    ListView        selectable list
    Table           columns and selectable rows
    ScrollText      wrapped text that scrolls
    Spinner         "working" indicator for slow reads
    Toast           one-line message
    ConfirmDialog   modal yes/no, optionally followed by a second confirm
    LineEdit        one-line text editing (used by the fields)
    Form            a settings schema as a form, one field widget per type:
                    TextField (text, url, int, float, int_list), ToggleField
                    (bool), ChoiceField, TimeWindowField, SerialListField,
                    SerialMapField

While a field is being edited it registers itself as the active editor; the
app then sends every key to the screen first (so typing "q" or "2" into a
field doesn't quit or switch tabs) and shows the editor's keys in the footer.
"""

import textwrap

from .. import plugins as P
from .keys import is_printable

NAV = ("up", "down", "pgup", "pgdn", "home", "end")

# ------------------------------------------------------------------ editing focus

_editing = []


def active_editor():
    """The widget currently being edited, or None."""
    return _editing[-1] if _editing else None


def _begin(w):
    if w not in _editing:
        _editing.append(w)


def _end(w):
    if w in _editing:
        _editing.remove(w)


def reset_editing():
    _editing.clear()


# ------------------------------------------------------------------ helpers

def cell(c):
    """A cell is a string or a (string, tone) pair."""
    if isinstance(c, tuple):
        return str(c[0]), c[1]
    return str(c), "normal"


def fit(s, width, more="…"):
    """s cut to width, with an ellipsis if it was cut."""
    s = str(s)
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    return s[: width - 1] + more if width > 1 else s[:width]


def pad(s, width, align="left", more="…"):
    s = fit(s, width, more)
    return s.rjust(width) if align == "right" else s.ljust(width)


def wrap(text, width):
    """Wrap text to width, keeping blank lines and leading indentation."""
    out = []
    width = max(1, width)
    for line in str(text).split("\n"):
        if not line.strip():
            out.append("")
            continue
        indent = len(line) - len(line.lstrip(" "))
        out += textwrap.wrap(line.strip(), width, initial_indent=" " * indent,
                             subsequent_indent=" " * indent, break_long_words=True,
                             break_on_hyphens=False) or [""]
    return out


class Scroller:
    """Selection index plus scroll offset for a list of `count` rows."""

    def __init__(self, count=0, index=0):
        self.count, self.index, self.top, self.page = count, index, 0, 10

    def move(self, k):
        if k not in NAV:
            return False
        if self.count == 0:
            return True
        step = {"up": -1, "down": 1, "pgup": -self.page, "pgdn": self.page}
        if k in step:
            self.index += step[k]
        elif k == "home":
            self.index = 0
        else:
            self.index = self.count - 1
        self.clamp()
        return True

    def clamp(self):
        self.index = max(0, min(self.index, self.count - 1)) if self.count else 0

    def visible(self, rows):
        """The row numbers to draw in `rows` lines, keeping the selection in view."""
        self.page = max(1, rows)
        self.clamp()
        if self.index < self.top:
            self.top = self.index
        if self.index >= self.top + rows:
            self.top = self.index - rows + 1
        self.top = max(0, min(self.top, max(0, self.count - rows)))
        return range(self.top, min(self.count, self.top + rows))


def _indicators(canvas, top, rows, count):
    s = canvas.symbols
    if top > 0:
        canvas.text(0, canvas.width - 1, s["up"], "dim")
    if top + rows < count:
        canvas.text(rows - 1, canvas.width - 1, s["down"], "dim")


# ------------------------------------------------------------------ list and table

class ListView:
    """A selectable list. Items are strings or (string, tone) pairs."""

    def __init__(self, items=(), index=0, empty="Nothing here."):
        self.items = list(items)
        self.scroll = Scroller(len(self.items), index)
        self.empty = empty

    def set_items(self, items):
        self.items = list(items)
        self.scroll.count = len(self.items)
        self.scroll.clamp()

    @property
    def index(self):
        return self.scroll.index if self.items else None

    @index.setter
    def index(self, i):
        self.scroll.index = i
        self.scroll.clamp()

    @property
    def current(self):
        return self.items[self.scroll.index] if self.items else None

    def key(self, k):
        return self.scroll.move(k)

    def keys(self):
        return [("↑↓", "move")]

    def render(self, canvas, focused=True):
        if not self.items:
            canvas.text(0, 2, self.empty, "dim")
            return
        rows = canvas.height
        marker = canvas.symbols["marker"]
        for y, i in enumerate(self.scroll.visible(rows)):
            text, tone = cell(self.items[i])
            if i == self.scroll.index:
                if focused:
                    canvas.fill(y, 0, 1, canvas.width - 1, tone="selected")
                    canvas.text(y, 0, f"{marker} {fit(text, canvas.width - 3)}", "selected")
                else:
                    canvas.text(y, 0, f"{marker} {fit(text, canvas.width - 3)}", tone)
            else:
                canvas.text(y, 2, fit(text, canvas.width - 3), tone)
        _indicators(canvas, self.scroll.top, rows, len(self.items))


class Table:
    """Columns and rows. columns: (title, width) or (title, width, align);
    width None shares what's left. Cells are strings or (string, tone)."""

    def __init__(self, columns, rows=(), selectable=True, empty="Nothing to show."):
        self.columns = [c if len(c) == 3 else (c[0], c[1], "left") for c in columns]
        self.rows = [list(r) for r in rows]
        self.selectable = selectable
        self.scroll = Scroller(len(self.rows))
        self.empty = empty

    def set_rows(self, rows):
        self.rows = [list(r) for r in rows]
        self.scroll.count = len(self.rows)
        self.scroll.clamp()

    @property
    def index(self):
        return self.scroll.index if self.rows else None

    @property
    def current(self):
        return self.rows[self.scroll.index] if self.rows else None

    def key(self, k):
        if not self.scroll.move(k):
            return False
        if not self.selectable:
            # No selection: the index is the first row shown.
            self.scroll.index = min(self.scroll.index, max(0, self.scroll.count - self.scroll.page))
        return True

    def keys(self):
        return [("↑↓", "move")] if self.selectable else [("↑↓", "scroll")]

    def widths(self, total):
        gaps = len(self.columns) - 1
        fixed = sum(c[1] for c in self.columns if c[1])
        flex = [i for i, c in enumerate(self.columns) if not c[1]]
        left = max(0, total - fixed - gaps)
        out = []
        for i, c in enumerate(self.columns):
            if c[1]:
                out.append(c[1])
            else:
                share = left // len(flex) + (1 if flex.index(i) < left % len(flex) else 0)
                out.append(max(3, share))
        return out

    def render(self, canvas):
        more = canvas.symbols["more"]
        widths = self.widths(canvas.width - 3)
        x = 2
        for (title, _, align), w in zip(self.columns, widths):
            canvas.text(0, x, pad(title, w, align, more), "dim", bold=True)
            x += w + 1
        body = canvas.sub(1, 0, canvas.height - 1, canvas.width)
        if not self.rows:
            body.text(0, 2, self.empty, "dim")
            return
        marker = canvas.symbols["marker"]
        if not self.selectable:
            self.scroll.top = self.scroll.index
        for y, i in enumerate(self.scroll.visible(body.height)):
            sel = self.selectable and i == self.scroll.index
            if sel:
                body.fill(y, 0, 1, body.width - 1, tone="selected")
                body.text(y, 0, marker, "selected")
            x = 2
            for (title, _, align), w, c in zip(self.columns, widths, self.rows[i] + [""] * len(widths)):
                text, tone = cell(c)
                body.text(y, x, pad(text, w, align, more), "selected" if sel else tone)
                x += w + 1
        _indicators(body, self.scroll.top, body.height, len(self.rows))


# ------------------------------------------------------------------ text, spinner, toast

class ScrollText:
    """Text wrapped to the canvas width that scrolls with the arrow keys.
    Takes a string, or a list of strings / (string, tone) pairs (one per paragraph)."""

    def __init__(self, text=""):
        self.set_text(text)

    def set_text(self, text):
        self.paras = [cell(p) for p in (text.split("\n") if isinstance(text, str) else text)]
        self.top = 0
        self.page = 10
        self.total = 0

    def key(self, k):
        if k not in NAV:
            return False
        step = {"up": -1, "down": 1, "pgup": -self.page, "pgdn": self.page}
        if k in step:
            self.top += step[k]
        elif k == "home":
            self.top = 0
        else:
            self.top = self.total
        self.top = max(0, min(self.top, max(0, self.total - self.page)))
        return True

    def keys(self):
        return [("↑↓", "scroll")]

    def render(self, canvas):
        lines = []
        for text, tone in self.paras:
            lines += [(line, tone) for line in wrap(text, canvas.width - 2)]
        self.total, self.page = len(lines), max(1, canvas.height)
        self.top = max(0, min(self.top, max(0, self.total - self.page)))
        for y, (line, tone) in enumerate(lines[self.top: self.top + canvas.height]):
            canvas.text(y, 0, line, tone)
        _indicators(canvas, self.top, canvas.height, self.total)


# ------------------------------------------------------------------ layout blocks
# docs/tui-style.md §3-4: a block is a heading plus rows indented under it;
# key-value rows share one label width; side-by-side blocks sit in panels.

def heading(canvas, y, x, text, tag=None):
    """A heading (accent, bold), optionally followed by a tag such as `today`.
    Returns the row below it."""
    end = canvas.styled(y, x, text, "heading")
    if tag:
        canvas.styled(y, end + 2, tag, "tag")
    return y + 1


def panel(canvas, y, x, h, w, title=""):
    """A box with its heading in the top border; returns the canvas inside it
    (one column of padding each side)."""
    canvas.box(y, x, h, w)
    if title and w > 6:
        canvas.styled(y, x + 2, f" {fit(title, w - 6)} "[: w - 4], "heading")
    return canvas.sub(y + 1, x + 2, max(0, h - 2), max(0, w - 4))


class KeyValue:
    """Key-value rows with one label width (the longest label plus 3, at
    least 12). Each row is (label, value) or (label, value, aside):

    - value: a string, a (text, role) pair, or ("status", kind, text) for a
      status with its glyph (canvas.status kinds: ok, failed, running, ...).
      None or "" shows `none`.
    - aside: shown dim after the value: a reason, a unit, a source.

    ("note", text) is a `↳` note under the row before it, at the value column.
    """

    MIN_LABEL = 12

    def __init__(self, rows):
        self.rows = list(rows)

    def label_width(self):
        labels = [r[0] for r in self.rows if r and r[0] != "note"]
        return max([self.MIN_LABEL] + [len(lbl) + 3 for lbl in labels])

    def height(self):
        return len(self.rows)

    def render(self, canvas, y=0, x=0):
        """Draw from (y, x); returns the row below the last one drawn."""
        lw = self.label_width()
        vx = x + lw
        for row in self.rows:
            if y >= canvas.height:
                break
            if row[0] == "note":
                canvas.styled(y, vx, fit(row[1], max(0, canvas.width - vx - 2)), "note")
                y += 1
                continue
            label, value = row[0], row[1]
            aside = row[2] if len(row) > 2 else None
            canvas.styled(y, x, fit(label, lw - 1), "label")
            room = max(0, canvas.width - vx)
            if isinstance(value, tuple) and value and value[0] == "status":
                end = canvas.status(y, vx, value[1], fit(value[2], max(0, room - 2)))
            elif isinstance(value, tuple):
                end = canvas.styled(y, vx, fit(value[0], room), value[1])
            else:
                end = canvas.styled(y, vx, fit(str(value) if value not in (None, "") else "none", room),
                                    "value" if value not in (None, "") else "label")
            if aside and end + 2 < canvas.width:
                canvas.styled(y, end + 2, fit(str(aside), canvas.width - end - 2), "label")
            y += 1
        return y


class Spinner:
    """Shown while a slow read runs (ctx.run_slow). ASCII frames on purpose."""

    FRAMES = "|/-\\"

    def __init__(self, label):
        self.label, self.frame = label, 0

    def tick(self):
        self.frame += 1

    def render(self, canvas, y=0, x=0):
        canvas.text(y, x, f"{self.FRAMES[self.frame % len(self.FRAMES)]} {self.label}...", "accent")


class Toast:
    """A one-line message (ctx.message). The app clears it on the next key."""

    def __init__(self, text, tone="normal"):
        self.text, self.tone = str(text), tone

    GLYPHS = {"good": "ok", "bad": "failed", "warn": "warning"}

    def render(self, canvas, y=0, x=0):
        text = self.text
        glyph = self.GLYPHS.get(self.tone)
        if glyph:
            text = f"{canvas.symbols[glyph]} {text}"
        canvas.text(y, x, fit(text, canvas.width - x, canvas.symbols["more"]), self.tone,
                    bold=self.tone in ("bad", "warn"))


# ------------------------------------------------------------------ confirm dialog

class ConfirmDialog:
    """Modal yes/no. The default answer is No. With `second`, a Yes is followed
    by a second question (default No again) for overrides such as starting a
    test on a busy drive."""

    def __init__(self, title, lines=(), second=None):
        self.title, self.lines, self.second = title, list(lines), second
        self.stage = 1
        self.choice = False
        self.done = False
        self.result = False

    def keys(self):
        return [("y", "yes"), ("n", "no"), ("←→", "choose"), ("Enter", "pick"), ("Esc", "no")]

    def _answer(self, yes):
        if yes and self.second and self.stage == 1:
            self.stage, self.choice = 2, False
            return
        self.done, self.result = True, bool(yes)

    def key(self, k):
        if k in ("left", "right", "tab", "backtab", "h", "l"):
            self.choice = not self.choice
        elif k in ("y", "Y"):
            self._answer(True)
        elif k in ("n", "N", "esc"):
            self._answer(False)
        elif k == "enter":
            self._answer(self.choice)
        return True

    def render(self, canvas):
        second = self.stage == 2
        question = self.second if second else self.title
        body = [] if second else self.lines
        inner = min(canvas.width - 8, max(40, len(question) + 2, *(len(x) + 2 for x in body or [""])))
        q_lines = wrap(question, inner - 2)
        b_lines = [line for para in body for line in wrap(para, inner - 2)]
        h = 2 + 1 + len(q_lines) + (1 + len(b_lines) if b_lines else 0) + 2 + 1
        w = inner + 2
        y0 = max(0, (canvas.height - h) // 2)
        x0 = max(0, (canvas.width - w) // 2)
        canvas.fill(y0, x0, h, w)
        tone = "bad" if second else "normal"
        canvas.box(y0, x0, h, w, title="Second confirm" if second else "Confirm", tone=tone)
        y = y0 + 2
        for line in q_lines:
            canvas.text(y, x0 + 2, line, tone, bold=True)
            y += 1
        if b_lines:
            y += 1
            for line in b_lines:
                canvas.text(y, x0 + 2, line)
                y += 1
        y += 1
        yes, no = "[ Yes ]", "[ No ]"
        bx = x0 + (w - len(yes) - len(no) - 3) // 2
        canvas.text(y, bx, yes, "selected" if self.choice else "normal")
        canvas.text(y, bx + len(yes) + 3, no, "normal" if self.choice else "selected")


# ------------------------------------------------------------------ text editing

class LineEdit:
    """A one-line text buffer with a cursor. `allowed(ch)` filters typed characters."""

    def __init__(self, text="", allowed=None):
        self.text, self.cur, self.allowed = text, len(text), allowed

    def key(self, k):
        t, c = self.text, self.cur
        if k == "left":
            self.cur = max(0, c - 1)
        elif k == "right":
            self.cur = min(len(t), c + 1)
        elif k == "home":
            self.cur = 0
        elif k == "end":
            self.cur = len(t)
        elif k == "backspace":
            if c:
                self.text, self.cur = t[: c - 1] + t[c:], c - 1
        elif k == "delete":
            self.text = t[:c] + t[c + 1:]
        elif is_printable(k):
            if self.allowed is None or self.allowed(k):
                self.text, self.cur = t[:c] + k + t[c:], c + 1
        else:
            return False
        return True

    def wrapped(self, width, rows):
        """(lines, cursor row, cursor column, first line shown): the text cut
        into lines of width, with the window of rows that holds the cursor."""
        width = max(1, width)
        lines = [self.text[i:i + width] for i in range(0, len(self.text), width)] or [""]
        row, col = divmod(self.cur, width)
        if row == len(lines):
            lines.append("")
        first = max(0, min(row - rows + 1, len(lines) - rows)) if row >= rows else 0
        return lines, row, col, first

    def render_wrapped(self, canvas, y, x, width, rows, tone="normal"):
        """The whole text wrapped over up to rows lines, cursor shown."""
        lines, row, col, first = self.wrapped(width, rows)
        for i, line in enumerate(lines[first:first + rows]):
            canvas.text(y + i, x, line.ljust(width), tone)
        under = self.text[self.cur] if self.cur < len(self.text) else " "
        canvas.text(y + row - first, x + col, under, "selected")

    def render(self, canvas, y=0, x=0, width=None, tone="normal"):
        width = (canvas.width - x) if width is None else width
        if width <= 0:
            return
        start = max(0, self.cur - width + 1)
        shown = self.text[start: start + width]
        canvas.text(y, x, shown.ljust(width), tone)
        under = self.text[self.cur] if self.cur < len(self.text) else " "
        canvas.text(y, x + self.cur - start, under, "selected")


# ------------------------------------------------------------------ form fields

class Field:
    """One schema field (docs/plugin-contract.md, section 3), edited as its
    config-file text. `error` validates as you type with plugins.parse_value."""

    edit_hints = [("Enter", "edit")]

    def __init__(self, spec, text=None):
        self.spec = spec
        self.key_name = spec["key"]
        self.label = spec["label"]
        self.help = spec.get("help", "")
        self.text = spec["default"] if text is None else text
        self.initial = self.text
        self.editing = False
        self.extra_error = None    # cross-field message from the plugin's validate()

    @property
    def error(self):
        try:
            P.parse_value(self.spec, self.text)
        except P.FieldError as e:
            return str(e)
        return self.extra_error

    @property
    def value(self):
        try:
            return P.parse_value(self.spec, self.text)
        except P.FieldError:
            return None

    @property
    def changed(self):
        try:
            return P.parse_value(self.spec, self.text) != P.parse_value(self.spec, self.initial)
        except P.FieldError:
            return self.text.strip() != self.initial.strip()

    def reset(self):
        self.text = self.initial

    def begin(self):
        self.editing = True
        _begin(self)

    def finish(self):
        self.editing = False
        _end(self)

    def keys(self):
        """Footer hints while editing."""
        return [("Enter", "done"), ("Esc", "cancel")]

    def display(self, symbols):
        """(text, tone) shown when not editing."""
        if not self.text.strip():
            return "(none)", "dim"
        return self.text, "normal"

    def key(self, k):
        return False

    def render(self, canvas):
        text, tone = self.display(canvas.symbols)
        canvas.text(0, 0, fit(text, canvas.width, canvas.symbols["more"]), tone)

    def popup_size(self):
        return None

    def render_popup(self, canvas):
        pass


class TextField(Field):
    """text, url, int, float and int_list: a text field. Numbers take only
    digit-like characters; `unit` is shown after the value; `secret` masks the
    value except while editing.

    While editing, text, url and int_list values open in a full-width box
    under the label (docs/tui-style.md §7), wrapped over up to BOX_LINES
    lines, so a pasted URL is visible end to end. Numbers stay inline."""

    BOXED = ("text", "url", "int_list")
    BOX_LINES = 4
    in_form = False          # a Form sets it: only there is there room for the box

    ALLOWED = {
        "int": lambda ch: ch.isdigit() or ch == "-",
        "float": lambda ch: ch.isdigit() or ch in ".-",
        "int_list": lambda ch: ch.isdigit() or ch in ", ",
    }

    def __init__(self, spec, text=None):
        super().__init__(spec, text)
        self.edit = None
        self.before = None

    def display(self, symbols):
        if not self.text.strip():
            return "(none)", "dim"
        if self.spec.get("secret"):
            return symbols["dot"] * 8, "normal"
        unit = self.spec.get("unit")
        return (f"{self.text} {unit}" if unit else self.text), "normal"

    def key(self, k):
        if not self.editing:
            if k == "enter":
                self.before = self.text
                self.edit = LineEdit(self.text, self.ALLOWED.get(self.spec["type"]))
                self.begin()
                return True
            return False
        if k == "enter":
            self.text = self.edit.text.strip()
            self.finish()
        elif k == "esc":
            self.text = self.before
            self.finish()
        else:
            self.edit.key(k)
            self.text = self.edit.text
        return True   # an editing field swallows every key

    @property
    def boxed(self):
        return self.editing and self.in_form and self.spec["type"] in self.BOXED

    def box_height(self, width):
        """Rows the editing box takes at this width (borders included)."""
        inner = max(1, width - 4)
        return min(self.BOX_LINES, len(self.edit.text) // inner + 1) + 2

    def render_box_part(self, canvas, skip=0):
        """The editing box (a border, the text wrapped inside it), drawn from
        its row `skip` down, so a box scrolled half out of view still draws."""
        w = canvas.width
        h = self.box_height(w)
        canvas.box(-skip, 0, h, w, tone="accent")
        self.edit.render_wrapped(canvas, 1 - skip, 2, max(1, w - 4), h - 2)

    def render(self, canvas):
        if not self.editing:
            return super().render(canvas)
        unit = self.spec.get("unit")
        if self.boxed:          # the value is in the box under the label
            if unit:
                canvas.text(0, 0, unit, "dim")
            return
        width = canvas.width - (len(unit) + 1 if unit else 0)
        self.edit.render(canvas, 0, 0, max(1, min(width, 40)))
        if unit:
            canvas.text(0, min(width, 40) + 1, unit, "dim")


class ToggleField(Field):
    """bool: Enter, Space or the arrows flip it. Writes yes/no."""

    edit_hints = [("Space", "toggle")]

    def key(self, k):
        if k in ("enter", " ", "left", "right"):
            self.text = "no" if self.value else "yes"
            return True
        return False

    def display(self, symbols):
        v = self.value
        if v is None:
            return self.text, "bad"
        return ("[x] yes" if v else "[ ] no"), "normal"


class ChoiceField(Field):
    """choice: the arrows step through the options; Enter opens the option list."""

    edit_hints = [("Enter", "pick"), ("←→", "change")]

    def __init__(self, spec, text=None):
        super().__init__(spec, text)
        self.list = None

    @property
    def choices(self):
        return self.spec["choices"]

    def keys(self):
        return [("↑↓", "move"), ("Enter", "choose"), ("Esc", "cancel")]

    def key(self, k):
        choices = self.choices
        if not self.editing:
            if k in ("left", "right"):
                i = choices.index(self.text) if self.text in choices else -1
                self.text = choices[(i + (1 if k == "right" else -1)) % len(choices)]
                return True
            if k == "enter":
                i = choices.index(self.text) if self.text in choices else 0
                self.list = ListView(choices, i)
                self.begin()
                return True
            return False
        if k == "enter":
            self.text = self.list.current
            self.finish()
        elif k == "esc":
            self.finish()
        else:
            self.list.key(k)
        return True

    def display(self, symbols):
        return f"{symbols['left']} {self.text} {symbols['right']}", "normal"

    def popup_size(self):
        choices = self.choices
        return min(len(choices), 8) + 2, max(max(len(c) for c in choices) + 8, len(self.label) + 6)

    def render_popup(self, canvas):
        canvas.box(0, 0, canvas.height, canvas.width, self.label)
        self.list.render(canvas.sub(1, 1, canvas.height - 2, canvas.width - 2))


class PickField(ChoiceField):
    """A text field picked from a list fetched when Enter is pressed, such as
    the branches on GitHub. load() returns the options, or None/[] when it
    couldn't (it says why itself); the value then stays as it is. The current
    value is marked in the list."""

    edit_hints = [("Enter", "pick")]

    def __init__(self, spec, text=None, load=None):
        super().__init__(spec, text)
        self.load, self.options = load, []

    @property
    def choices(self):
        return self.options

    def key(self, k):
        if not self.editing:
            if k != "enter":
                return False
            options = list(self.load() or [])
            if not options:
                return True
            self.options = options
            i = options.index(self.text) if self.text in options else 0
            self.list = ListView([f"{o}  (current)" if o == self.text else o for o in options], i)
            self.begin()
            return True
        if k == "enter":
            self.text = self.options[self.list.index]
            self.finish()
            return True
        return super().key(k)

    def display(self, symbols):
        return self.text, "normal"

    def popup_size(self):
        h, w = super().popup_size()
        return h, w + len("  (current)")


class TimeWindowField(Field):
    """time_window: two HH:MM times. While editing, digits fill the template
    HH:MM-HH:MM and the cursor skips the separators."""

    SLOTS = (0, 1, 3, 4, 6, 7, 9, 10)

    def __init__(self, spec, text=None):
        super().__init__(spec, text)
        self.buf, self.pos, self.before = None, 0, None

    def keys(self):
        return [("0-9", "type"), ("←→", "move"), ("Enter", "done"), ("Esc", "cancel")]

    def display(self, symbols):
        v = self.value
        if v is None:
            return self.text or "(none)", "bad"
        return f"{v[0]:%H:%M} to {v[1]:%H:%M}", "normal"

    def key(self, k):
        if not self.editing:
            if k == "enter":
                self.before = self.text
                t = self.text.replace(" ", "")
                ok = len(t) == 11 and t[2] == ":" and t[5] == "-" and t[8] == ":"
                self.buf = list(t if ok else "00:00-00:00")
                self.pos = 0
                self.text = "".join(self.buf)
                self.begin()
                return True
            return False
        if k in ("enter", "esc"):
            if k == "esc":
                self.text = self.before
            self.finish()
            return True
        if isinstance(k, str) and len(k) == 1 and k.isdigit():
            self.buf[self.SLOTS[self.pos]] = k
            self.pos = min(self.pos + 1, len(self.SLOTS) - 1)
        elif k in ("left", "backspace"):
            self.pos = max(0, self.pos - 1)
        elif k == "right":
            self.pos = min(len(self.SLOTS) - 1, self.pos + 1)
        elif k == "home":
            self.pos = 0
        elif k == "end":
            self.pos = len(self.SLOTS) - 1
        self.text = "".join(self.buf)
        return True

    def render(self, canvas):
        if not self.editing:
            return super().render(canvas)
        canvas.text(0, 0, self.text)
        at = self.SLOTS[self.pos]
        canvas.text(0, at, self.text[at], "selected")
        canvas.text(0, 13, "start-end, 24-hour", "dim")


class _ItemsField(Field):
    """Shared by the list editors: a popup with one row per item plus an
    "add" row. Enter edits or adds, d deletes, u undoes, Esc closes."""

    def __init__(self, spec, text=None):
        super().__init__(spec, text)
        self.items, self.list, self.before = [], None, None
        self.row_edit = None      # editing state of one row, or None

    # subclasses: parse_items(text), join(items), row_text(item), edit_row(item), commit_row()

    def keys(self):
        if self.row_edit is not None:
            return self.row_keys()
        return [("Enter", "edit/add"), ("d", "delete"), ("u", "undo"), ("Esc", "done")]

    def row_keys(self):
        return [("Enter", "save item"), ("Esc", "cancel item")]

    def display(self, symbols):
        items = self.parse_items(self.text)
        if not items:
            return "(none)", "dim"
        return ", ".join(self.row_text(i).strip() for i in items), "normal"

    def _rows(self):
        return [self.row_text(i) for i in self.items] + ["+ add"]

    def _sync(self):
        self.text = self.join(self.items)
        self.list.set_items(self._rows())

    def key(self, k):
        if not self.editing:
            if k == "enter":
                self.before = self.text
                self.items = self.parse_items(self.text)
                self.list = ListView(self._rows())
                self.begin()
                return True
            return False
        if self.row_edit is not None:
            self.row_key(k)
            return True
        i = self.list.index
        adding = i == len(self.items)
        if k == "enter":
            self.start_row(None if adding else i)
        elif k in ("d", "delete") and not adding:
            del self.items[i]
            self._sync()
        elif k == "u":
            self.items = self.parse_items(self.before)
            self._sync()
        elif k == "esc":
            self.finish()
        else:
            self.list.key(k)
        return True

    def popup_size(self):
        return min(len(self.items) + 1, 10) + 2, 48

    def render_popup(self, canvas):
        canvas.box(0, 0, canvas.height, canvas.width, self.label)
        inner = canvas.sub(1, 1, canvas.height - 2, canvas.width - 2)
        self.list.render(inner)
        if self.row_edit is not None:
            row = self.list.index - self.list.scroll.top
            inner.fill(row, 0, 1, inner.width)
            self.render_row_edit(inner.sub(row, 2, 1, inner.width - 3))


class SerialListField(_ItemsField):
    """serial_list: one serial per row. With `suggestions` (the NAS's drives,
    when a screen provides them), adding offers those first."""

    def __init__(self, spec, text=None, suggestions=()):
        super().__init__(spec, text)
        self.suggestions = list(suggestions)
        self.picker = None

    @staticmethod
    def parse_items(text):
        return [s.strip() for s in text.split(",") if s.strip()]

    @staticmethod
    def join(items):
        return ",".join(items)

    def row_text(self, item):
        return item

    def row_keys(self):
        if self.picker is not None:
            return [("↑↓", "move"), ("Enter", "add"), ("Esc", "cancel")]
        return super().row_keys()

    def start_row(self, i):
        left = [s for s in self.suggestions if s not in self.items]
        if i is None and left:
            self.picker = ListView(left + ["Other..."])
            self.row_edit = ("pick", None)
        else:
            self.row_edit = (LineEdit("" if i is None else self.items[i]), i)

    def row_key(self, k):
        if self.picker is not None:
            if k == "esc":
                self.picker, self.row_edit = None, None
            elif k == "enter":
                choice = self.picker.current
                self.picker = None
                if choice == "Other...":
                    self.row_edit = (LineEdit(""), None)
                else:
                    self.items.append(choice)
                    self.row_edit = None
                    self._sync()
            else:
                self.picker.key(k)
            return
        edit, i = self.row_edit
        if k == "esc":
            self.row_edit = None
        elif k == "enter":
            v = edit.text.strip()
            if v:
                if i is None:
                    self.items.append(v)
                else:
                    self.items[i] = v
            elif i is not None:
                del self.items[i]
            self.row_edit = None
            self._sync()
        else:
            edit.key(k)

    def render_popup(self, canvas):
        if self.picker is not None:
            canvas.box(0, 0, canvas.height, canvas.width, "Add a drive")
            self.picker.render(canvas.sub(1, 1, canvas.height - 2, canvas.width - 2))
            return
        super().render_popup(canvas)

    def popup_size(self):
        if self.picker is not None:
            return min(len(self.picker.items), 10) + 2, 48
        return super().popup_size()

    def render_row_edit(self, canvas):
        self.row_edit[0].render(canvas, 0, 0, min(canvas.width, 30))


class SerialMapField(_ItemsField):
    """serial_map: rows of (serial, number). Editing a row has two parts; Tab
    moves between them."""

    @staticmethod
    def parse_items(text):
        out = []
        for item in (s.strip() for s in text.split(",")):
            if item:
                serial, _, n = item.rpartition(":")
                out.append([serial.strip(), n.strip()] if serial else [n.strip(), ""])
        return out

    @staticmethod
    def join(items):
        return ",".join(f"{s}:{n}" for s, n in items)

    def row_text(self, item):
        unit = self.spec.get("unit", "")
        return f"{item[0]:<20} {item[1]} {unit}".rstrip()

    def display(self, symbols):
        items = self.parse_items(self.text)
        if not items:
            return "(none)", "dim"
        unit = self.spec.get("unit", "")
        return ", ".join(f"{s}: {n} {unit}".rstrip() for s, n in items), "normal"

    def row_keys(self):
        return [("Tab", "serial/number"), ("Enter", "save item"), ("Esc", "cancel item")]

    def start_row(self, i):
        s, n = ("", "") if i is None else self.items[i]
        self.row_edit = [LineEdit(s), LineEdit(n, lambda ch: ch.isdigit()), 0, i]

    def row_key(self, k):
        serial, number, part, i = self.row_edit
        if k == "esc":
            self.row_edit = None
        elif k in ("tab", "backtab"):
            self.row_edit[2] = 1 - part
        elif k == "enter":
            s, n = serial.text.strip(), number.text.strip()
            if s or n:
                if i is None:
                    self.items.append([s, n])
                else:
                    self.items[i] = [s, n]
            elif i is not None:
                del self.items[i]
            self.row_edit = None
            self._sync()
        else:
            (serial, number)[part].key(k)

    def render_row_edit(self, canvas):
        serial, number, part, _ = self.row_edit
        if part == 0:
            serial.render(canvas, 0, 0, 20)
            canvas.text(0, 21, number.text or "_", "dim")
        else:
            canvas.text(0, 0, (serial.text or "_")[:20], "dim")
            number.render(canvas, 0, 21, 8)
        unit = self.spec.get("unit")
        if unit:
            canvas.text(0, 21 + min(8, len(number.text or "_") + (part == 1)) + 1, unit, "dim")


FIELD_TYPES = {
    "int": TextField, "float": TextField, "text": TextField, "url": TextField,
    "int_list": TextField, "bool": ToggleField, "choice": ChoiceField,
    "time_window": TimeWindowField, "serial_list": SerialListField,
    "serial_map": SerialMapField,
}


def make_field(spec, text=None, suggestions=None, pickers=None):
    load = (pickers or {}).get(spec["key"])
    if load is not None and spec["type"] == "text":
        return PickField(spec, text, load)
    if spec["type"] == "serial_list":
        return SerialListField(spec, text, (suggestions or {}).get(spec["key"], ()))
    return FIELD_TYPES[spec["type"]](spec, text)


# ------------------------------------------------------------------ form

class Form:
    """A settings schema as a form: groups as headings, one widget per field.

    raw: {KEY: text} from the config file; a missing key shows its default.
    suggestions: {KEY: [serial, ...]} for serial_list pickers.
    pickers: {KEY: load} turns a text field into a PickField.
    Changed fields are marked with *, errors show under the field, and the
    selected field's help sits at the bottom. Saving is the screen's job (#12):
    read changed(), errors() and values().
    """

    LABEL_W = 30

    def __init__(self, schema, raw=None, suggestions=None, pickers=None):
        raw = raw or {}
        self.rows, self.fields = [], []
        for g in schema["groups"]:
            self.rows.append(("group", g))
            for spec in g["fields"]:
                f = make_field(spec, raw.get(spec["key"]), suggestions, pickers)
                f.in_form = True
                self.rows.append(("field", f))
                self.fields.append(f)
        self.scroll = Scroller(len(self.fields))
        self.top = 0

    @property
    def current(self):
        return self.fields[self.scroll.index]

    @property
    def editing(self):
        return self.current.editing

    def field(self, key):
        return next(f for f in self.fields if f.key_name == key)

    def key(self, k):
        f = self.current
        if f.editing:
            return f.key(k)
        if self.scroll.move(k):
            return True
        return f.key(k)

    def keys(self):
        if self.editing:
            return self.current.keys()
        return [("↑↓", "move")] + self.current.edit_hints

    def texts(self):
        return {f.key_name: f.text for f in self.fields}

    def changed(self):
        return {f.key_name: f.text for f in self.fields if f.changed}

    def errors(self):
        return {f.key_name: f.error for f in self.fields if f.error}

    def values(self):
        """Typed values; a field with a bad value gives its default."""
        out = {}
        for f in self.fields:
            v = f.value
            out[f.key_name] = v if v is not None else P.parse_value(f.spec, f.spec["default"])
        return out

    def set_extra_errors(self, errors):
        for f in self.fields:
            f.extra_error = (errors or {}).get(f.key_name)

    def render(self, canvas):
        more = canvas.symbols["more"]
        help_h = 3 if canvas.height >= 10 else 0
        area = canvas.sub(0, 0, canvas.height - help_h, canvas.width)
        lines = []           # (kind, payload)
        sel_line = anchor = 0
        heading = 0
        for kind, item in self.rows:
            if kind == "group":
                if lines:
                    lines.append(("blank", None))
                heading = len(lines)
                lines.append(("group", item))
                if item.get("help"):
                    lines.append(("ghelp", item))
                continue
            if item is self.current:
                sel_line = len(lines)
                # the first field of a group brings its heading into view
                anchor = heading if lines[-1][0] in ("group", "ghelp") else sel_line
            lines.append(("field", item))
            if getattr(item, "boxed", False):
                box_w = area.width - 4
                lines += [("box", (item, i, box_w)) for i in range(item.box_height(box_w))]
            if item.error:
                lines.append(("error", item))
        rows = area.height
        last = sel_line + (1 if self.current.error else 0)
        if getattr(self.current, "boxed", False):
            last += self.current.box_height(area.width - 4)
        if anchor < self.top:
            self.top = anchor
        if last >= self.top + rows:
            self.top = last - rows + 1
        self.top = max(0, min(self.top, max(0, len(lines) - rows)))
        sel_y = None
        for y, (kind, item) in enumerate(lines[self.top: self.top + rows]):
            if kind == "group":
                area.text(y, 0, item["title"], "accent", bold=True)
            elif kind == "ghelp":
                area.text(y, 2, fit(item["help"], area.width - 3, more), "dim")
            elif kind == "box":
                field, i, box_w = item
                if i == 0 or y == 0:        # draw the whole box once, from its first visible row
                    h = field.box_height(box_w) - i
                    field.render_box_part(area.sub(y, 2, min(h, rows - y), box_w), i)
            elif kind == "error":
                area.text(y, self.LABEL_W, fit(f"! {item.error}", area.width - self.LABEL_W - 1, more), "bad")
            elif kind == "field":
                sel = item is self.current
                if item.changed:
                    area.text(y, 0, "*", "warn", bold=True)
                label = pad(item.label, self.LABEL_W - 3, more=more)
                area.text(y, 2, label + " ", "selected" if sel else "dim")
                item.render(area.sub(y, self.LABEL_W, 1, area.width - self.LABEL_W - 1))
                if sel:
                    sel_y = y
        _indicators(area, self.top, rows, len(lines))
        if help_h:
            canvas.text(canvas.height - 3, 0, canvas.symbols["h"] * canvas.width, "dim")
            for i, line in enumerate(wrap(self.current.help, canvas.width - 2)[:2]):
                canvas.text(canvas.height - 2 + i, 0, line, "dim")
        size = self.current.popup_size() if self.current.editing else None
        if size and sel_y is not None:
            h, w = min(size[0], area.height), min(size[1], area.width - self.LABEL_W)
            y = sel_y + 1 if sel_y + 1 + h <= area.height else max(0, sel_y - h)
            pop = area.sub(y, self.LABEL_W, h, w)
            pop.clear()
            self.current.render_popup(pop)
