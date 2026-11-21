"""The core's config-file writer: change the edited keys of a tool's KEY=VALUE
config and keep everything else (comments, blank lines, order, line endings,
quotes, lines it doesn't understand). The Settings screen (core/tui/settings.py)
saves through it, under the tool's run lock.

Standard library only. It is the same writer as smart-scheduler's
`settings.py` (#7), which keeps its own copy so the hourly run never imports
the core; core/tests/test_settings_ui.py checks that the two produce the
same file for the same edits. The value rules are core/plugins.py's.
"""

import os
import re
import tempfile

from . import plugins as P

# An active setting line: indent, key, "=" with its spacing, value, trailing space.
LINE_RE = re.compile(r"(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*?)(\s*)\Z")
HEADING_RE = re.compile(r"\s*#\s*-{2,}\s*(.*?)\s*-*\s*\Z")


class ValidationError(ValueError):
    """Edits refused by save(). .errors maps each bad key to its message."""

    def __init__(self, errors):
        self.errors = dict(errors)
        super().__init__("; ".join(f"{k}: {m}" for k, m in self.errors.items()))


def _unquote(v):
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1], v[0]
    return v, ""


# --------------------------------------------------------------------- writing

def _check_edits(schema, edits):
    """Validate and canonicalize edits: {KEY: text or typed value or None}."""
    known = P.field_map(schema)
    out, errors = {}, {}
    for key, value in edits.items():
        f = known.get(key)
        if f is None:
            errors[key] = "unknown setting"
            continue
        if value is None:
            out[key] = None
            continue
        try:
            text = value if isinstance(value, str) else P.format_value(f, value)
            if "\n" in text or "\r" in text:
                raise P.FieldError("must be on one line")
            out[key] = P.format_value(f, P.parse_value(f, text))
        except P.FieldError as e:
            errors[key] = str(e)
    if errors:
        raise ValidationError(errors)
    return out


def _render(match, value):
    indent, key, eq, old, trail = match.groups()
    quote = _unquote(old)[1]
    if quote and quote not in value:
        value = quote + value + quote
    return f"{indent}{key}{eq}{value}{trail}"


def apply_edits(text, edits, schema):
    """Return text with edits applied, or raise ValidationError (nothing applied).

    edits maps KEY to its new value: config text ("30"), a typed value (30),
    or None to remove the line so the default applies. Values are written in
    their canonical form ("true" becomes "yes", " 5, 187" becomes "5,187").

    Only the first active line of an edited key changes (the one the loader
    reads), and only its value: indentation, spacing around "=" and quotes
    are kept, and a line whose value already means the same is left alone.
    A key not in the file is added after the nearest setting of its schema
    group, else under the group's "# --- Title" heading, else at the end
    under a new heading.
    """
    edits = _check_edits(schema, edits)
    known = P.field_map(schema)
    lines = text.splitlines(keepends=True)
    eol = next((ln[len(ln.rstrip("\r\n")):] for ln in lines if ln.rstrip("\r\n") != ln), "\n")

    def body(ln):
        return ln.rstrip("\r\n")

    def where(key):
        """Index of the line the loader reads for key, or None."""
        for i, ln in enumerate(lines):
            s = body(ln).strip()
            if s.startswith("#") or "=" not in s:
                continue
            if s.split("=", 1)[0].strip() == key:
                return i
        return None

    def insert(i, new):
        if i > 0 and lines[i - 1] == body(lines[i - 1]):
            lines[i - 1] += eol       # the line before had no newline (end of file)
        lines.insert(i, new + eol)

    missing = []
    for key, value in edits.items():
        i = where(key)
        if i is None:
            if value is not None:
                missing.append(key)
            continue
        if value is None:
            del lines[i]
            continue
        m = LINE_RE.match(body(lines[i]))
        f = known[key]
        try:
            same = P.parse_value(f, _unquote(m.group(4))[0]) == P.parse_value(f, value)
        except P.FieldError:
            same = False
        if not same:
            lines[i] = _render(m, value) + lines[i][len(body(lines[i])):]

    order = [f["key"] for f in P.fields(schema)]
    for key in sorted(missing, key=order.index):
        group = next(g for g in schema["groups"] if any(f["key"] == key for f in g["fields"]))
        keys = [f["key"] for f in group["fields"]]
        pos = keys.index(key)
        new = f"{key}={edits[key]}"
        before = [where(k) for k in reversed(keys[:pos])]
        after = [where(k) for k in keys[pos + 1:]]
        before = next((i for i in before if i is not None), None)
        after = next((i for i in after if i is not None), None)
        if before is not None:
            insert(before + 1, new)
        elif after is not None:
            # Above the next setting's own comment, but not above a heading.
            i = after
            while i > 0 and body(lines[i - 1]).lstrip().startswith("#") \
                    and not HEADING_RE.match(body(lines[i - 1])):
                i -= 1
            insert(i, new)
        else:
            title = group["title"].lower()
            head = next((i for i, ln in enumerate(lines)
                         if HEADING_RE.match(body(ln))
                         and HEADING_RE.match(body(ln)).group(1).lower().startswith(title)), None)
            if head is not None:
                insert(head + 1, new)
            else:
                if lines and body(lines[-1]).strip():
                    insert(len(lines), "")
                insert(len(lines), f"# --- {group['title']} ".ljust(79, "-"))
                insert(len(lines), new)
    return "".join(lines)


def save(path, edits, schema):
    """Apply edits to the config file at path, atomically, mode 600.

    Raises ValidationError, before touching anything, if an edit is invalid.
    A missing file is created. The caller holds the tool's lock (ctx.lock()
    in the TUI) so the hourly run never reads a half-made change; the file
    itself is replaced in one rename, so it is never half-written either.
    Returns the new text.
    """
    try:
        with open(path, encoding="utf-8", newline="") as f:
            old = f.read()
    except FileNotFoundError:
        old = ""
    new = apply_edits(old, edits, schema)
    if new == old and os.path.exists(path):
        os.chmod(path, 0o600)
        return new
    write_atomic(path, new)
    return new


def write_atomic(path, text, mode=0o600):
    """Write text to path through a temporary file in the same folder and a rename."""
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=folder)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
