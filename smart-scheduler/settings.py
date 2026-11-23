"""smart-scheduler's settings: read, validate and write the config file.

Standard library only, and no import from better-unas's core, so the hourly
run keeps working without the TUI. The rules come from settings.json, the
settings schema of the plugin contract (docs/plugin-contract.md section 3), so
adding a setting means adding it there and nothing else: its default, type and
range drive smart_scheduler.py's Config, this module's writer and the TUI's
Settings form alike.

parse_value / format_value / validate behave exactly like core/plugins.py's
functions of the same names (tests/test_settings.py checks that field by
field), so the TUI and the hourly run never disagree about a value. Only the
types the contract defines are handled; check the schema itself with
`python3 core/plugins.py check smart-scheduler`.

The writer (save / apply_edits) changes only the edited keys and keeps
comments, blank lines, order, line endings and lines it doesn't understand.
"""

import datetime as dt
import json
import os
import re
import tempfile

KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
SERIAL_RE = r"[A-Za-z0-9._-]+"
TIME_RE = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])\Z")
TRUE_WORDS = ("yes", "true", "on", "1")
FALSE_WORDS = ("no", "false", "off", "0")
EMPTY_OK = {"text", "url", "serial_list", "int_list", "serial_map", "checklist"}
TYPES = {"int", "float", "bool", "choice", "time_window", "text", "url",
         "serial_list", "int_list", "serial_map", "checklist"}

# An active setting line: indent, key, "=" with its spacing, value, trailing space.
LINE_RE = re.compile(r"(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*?)(\s*)\Z")
HEADING_RE = re.compile(r"\s*#\s*-{2,}\s*(.*?)\s*-*\s*\Z")


class SchemaError(Exception):
    """settings.json is missing or not shaped like a schema."""


class FieldError(ValueError):
    """A value doesn't fit its field. The message is meant to sit next to the field."""


class ValidationError(ValueError):
    """Edits refused by save(). .errors maps each bad key to its message."""

    def __init__(self, errors):
        self.errors = dict(errors)
        super().__init__("; ".join(f"{k}: {m}" for k, m in self.errors.items()))


# --------------------------------------------------------------------- schema

def load_schema(path):
    """Read settings.json. Checks only what this module relies on; the full
    contract check is core/plugins.py's (and runs in the tests)."""
    name = os.path.basename(path)
    try:
        with open(path, encoding="utf-8") as f:
            schema = json.load(f)
    except OSError as e:
        raise SchemaError(f"{name}: can't read ({e.strerror}); re-run install.sh") from None
    except ValueError as e:
        raise SchemaError(f"{name}: isn't valid JSON ({e})") from None
    try:
        for f in fields(schema):
            if not (KEY_RE.match(f["key"]) and f["type"] in TYPES and isinstance(f["default"], str)):
                raise SchemaError(f"{name}: bad field {f.get('key')!r}")
            parse_value(f, f["default"])
    except (KeyError, TypeError, AttributeError):
        raise SchemaError(f"{name}: not a settings schema") from None
    except FieldError as e:
        raise SchemaError(f"{name}: bad default for {f['key']}: {e}") from None
    return schema


def fields(schema):
    """Every field, in schema order."""
    return [f for g in schema["groups"] for f in g["fields"]]


def field_map(schema):
    return {f["key"]: f for f in fields(schema)}


def defaults(schema):
    """{KEY: default text} for every setting, in schema order."""
    return {f["key"]: f["default"] for f in fields(schema)}


# --------------------------------------------------------------------- values

def _num(x):
    return str(int(x)) if float(x).is_integer() else str(x)


def _range(f, n, what="value"):
    if "min" in f and n < f["min"]:
        raise FieldError(f"{what} must be at least {_num(f['min'])}")
    if "max" in f and n > f["max"]:
        raise FieldError(f"{what} must be at most {_num(f['max'])}")


def _split(text):
    return [x.strip() for x in text.split(",") if x.strip()]


def _whole(text, what="a whole number"):
    if not re.fullmatch(r"-?[0-9]+", text):
        raise FieldError(f"want {what}, got {text!r}")
    return int(text)


def _no_dupes(items):
    seen = set()
    for x in items:
        if x in seen:
            raise FieldError(f"{x} listed twice")
        seen.add(x)


def parse_value(f, text):
    """Config-file text -> typed value, or FieldError with a short message.

    int -> int, float -> float, bool -> bool, choice/text/url -> str,
    time_window -> (time, time), serial_list -> [str], int_list -> [int],
    serial_map -> {serial: int}, checklist -> [str] or [int].
    """
    t = f["type"]
    text = text.strip()
    if not text:
        if t not in EMPTY_OK or f.get("required"):
            raise FieldError("a value is required")
        return {"serial_list": [], "int_list": [], "serial_map": {}, "checklist": []}.get(t, "")
    if t == "int":
        n = _whole(text)
        _range(f, n)
        return n
    if t == "float":
        try:
            n = float(text)
        except ValueError:
            raise FieldError(f"want a number, got {text!r}") from None
        if n != n or n in (float("inf"), float("-inf")):
            raise FieldError(f"want a number, got {text!r}")
        _range(f, n)
        return n
    if t == "bool":
        if text.lower() in TRUE_WORDS:
            return True
        if text.lower() in FALSE_WORDS:
            return False
        raise FieldError(f"want yes or no, got {text!r}")
    if t == "choice":
        if text not in f["choices"]:
            raise FieldError(f"want one of {', '.join(f['choices'])}")
        return text
    if t == "time_window":
        parts = text.split("-")
        if len(parts) != 2 or not all(TIME_RE.match(x.strip()) for x in parts):
            raise FieldError("want HH:MM-HH:MM, e.g. 22:00-04:00")
        a, b = (dt.time.fromisoformat(x.strip()) for x in parts)
        if a == b:
            raise FieldError("start and end must differ")
        return a, b
    if t == "text":
        if "max_length" in f and len(text) > f["max_length"]:
            raise FieldError(f"at most {f['max_length']} characters")
        if "pattern" in f and not re.fullmatch(f["pattern"], text):
            raise FieldError("not in the expected form")
        return text
    if t == "url":
        schemes = f.get("schemes", ["http", "https"])
        m = re.fullmatch(r"([a-zA-Z][a-zA-Z0-9+.-]*)://([^/\s?#]+)(\S*)", text)
        if not m:
            raise FieldError("want a full URL, e.g. https://example.com/path")
        if m.group(1).lower() not in schemes:
            raise FieldError(f"URL must start with {' or '.join(s + '://' for s in schemes)}")
        return text
    if t == "serial_list":
        items = _split(text)
        for s in items:
            if not re.fullmatch(SERIAL_RE, s):
                raise FieldError(f"{s!r} doesn't look like a serial number")
        _no_dupes(items)
        return items
    if t == "int_list":
        items = [_whole(s) for s in _split(text)]
        for n in items:
            _range(f, n, f"each number ({n})")
        _no_dupes(items)
        return items
    if t == "checklist":
        return _checklist(f, text)
    if t == "serial_map":
        out = {}
        for item in _split(text):
            serial, _, n = item.rpartition(":")
            if not re.fullmatch(SERIAL_RE, serial) or not re.fullmatch(r"[0-9]+", n.strip()):
                raise FieldError(f"bad entry {item!r} (want SERIAL:{f.get('unit', 'NUMBER').upper()})")
            if serial in out:
                raise FieldError(f"{serial} listed twice")
            out[serial] = int(n)
            _range(f, out[serial], f"{serial}'s value")
        return out
    raise FieldError(f"unknown type {t}")


def _checklist(f, text):
    """checklist text -> [str]: the ticked options in schema order, then any
    other values ("other" fields only) in the order given. With "min" or
    "max", every value is a whole number in range and the list holds ints."""
    known = [o["value"] for o in f["options"]]
    numeric = "min" in f or "max" in f
    picked = []
    for s in _split(text):
        if numeric:
            n = _whole(s)
            _range(f, n, f"each number ({n})")
            s = str(n)
        if s not in known and not f.get("other"):
            raise FieldError(f"want some of {', '.join(known)}")
        picked.append(s)
    _no_dupes(picked)
    out = [v for v in known if v in picked] + [v for v in picked if v not in known]
    return [int(v) for v in out] if numeric else out


def format_value(f, value):
    """The inverse of parse_value: the text to write to the config file."""
    t = f["type"]
    if t == "bool":
        return "yes" if value else "no"
    if t == "float":
        return _num(value)
    if t == "time_window":
        return f"{value[0]:%H:%M}-{value[1]:%H:%M}"
    if t in ("serial_list", "int_list", "checklist"):
        return ",".join(str(x) for x in value)
    if t == "serial_map":
        return ",".join(f"{k}:{v}" for k, v in value.items())
    return str(value)


# Settings every better-unas tool shares (core/notify.py). They live in
# <home>/better-unas.conf; one left in this tool's config is a fallback, not
# an "unknown setting". Same list as core/plugins.py's SHARED_KEYS.
SHARED_KEYS = ("WEBHOOK_URL", "WEBHOOK_FORMAT", "NOTIFY_SUCCESS", "ALERT_REPEAT_HOURS",
               "HEARTBEAT_URL", "HEARTBEAT_FAIL_URL")


def validate(schema, raw):
    """(typed, errors) for {KEY: text} as read from a config file.

    typed has every schema key; a missing key takes its default and a bad
    value falls back to it, so the hourly run keeps going on a broken line.
    errors maps KEY -> message, including "unknown setting" for keys the
    schema doesn't know (SHARED_KEYS aside). Same contract as
    core/plugins.py's validate_config.
    """
    typed, errors = {}, {}
    known = field_map(schema)
    for key, f in known.items():
        try:
            typed[key] = parse_value(f, raw.get(key, f["default"]))
        except FieldError as e:
            errors[key] = str(e)
            typed[key] = parse_value(f, f["default"])
    for key in raw:
        if key not in known and key not in SHARED_KEYS:
            errors[key] = "unknown setting"
    return typed, errors


# --------------------------------------------------------------------- reading

def _unquote(v):
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1], v[0]
    return v, ""


def parse_text(text):
    """Parse KEY=VALUE text. Returns ({KEY: text}, problems).

    The plugin contract's rules: blank lines and # comments are skipped, the
    value is everything after the first "=" stripped of spaces and one pair of
    matching quotes, nothing is expanded. A repeated key: the first wins and
    the repeat is reported.
    """
    values, problems = {}, []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            problems.append(f"line {n}: expected KEY=VALUE")
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if not KEY_RE.match(k):
            problems.append(f"line {n}: {k!r} isn't a valid key (A-Z, 0-9, _)")
            continue
        if k in values:
            problems.append(f"line {n}: {k} repeated; the first value is used")
            continue
        values[k] = _unquote(v.strip())[0]
    return values, problems


def read(path):
    """parse_text() of a file; a missing file is an empty config."""
    try:
        with open(path, encoding="utf-8") as f:
            return parse_text(f.read())
    except FileNotFoundError:
        return {}, []


# --------------------------------------------------------------------- writing

def _check_edits(schema, edits):
    """Validate and canonicalize edits: {KEY: text or typed value or None}."""
    known = field_map(schema)
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
            text = value if isinstance(value, str) else format_value(f, value)
            if "\n" in text or "\r" in text:
                raise FieldError("must be on one line")
            out[key] = format_value(f, parse_value(f, text))
        except FieldError as e:
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
    known = field_map(schema)
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
            same = parse_value(f, _unquote(m.group(4))[0]) == parse_value(f, value)
        except FieldError:
            same = False
        if not same:
            lines[i] = _render(m, value) + lines[i][len(body(lines[i])):]

    order = [f["key"] for f in fields(schema)]
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
