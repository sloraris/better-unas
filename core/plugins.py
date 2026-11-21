"""Load and check better-unas tools against the plugin contract (docs/plugin-contract.md).

Standard library only. Nothing here imports a tool's code except load_plugin().

As a command, it checks a tool folder and prints every problem it finds:

    python3 core/plugins.py check path/to/tool [path/to/other-tool ...]

Exit code 0 when every tool passes, 1 otherwise.
"""

import datetime as dt
import importlib.util
import json
import os
import re
import sys

CONTRACT = 1

NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
GROUP_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
SERIAL_RE = r"[A-Za-z0-9._-]+"
TIME_RE = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])\Z")

# Manifest keys (tool.conf). Values: required?
MANIFEST_KEYS = {
    "NAME": True,
    "VERSION": True,
    "SUMMARY": True,
    "CHANGES": True,
    "SETUP": False,
    "CLI": True,
    "CONTRACT": False,   # required once SCHEMA or PLUGIN is set
    "SCHEMA": False,
    "CONFIG": False,     # required when SCHEMA is set
    "CONFIG_EXAMPLE": False,
    "PLUGIN": False,
    "LOCK": False,
}
MANIFEST_DEFAULTS = {"SETUP": "", "SCHEMA": "", "CONFIG": "", "CONFIG_EXAMPLE": "",
                     "PLUGIN": "", "LOCK": ".lock", "CONTRACT": ""}
SUMMARY_MAX = 60
LABEL_MAX = 28

TRUE_WORDS = ("yes", "true", "on", "1")
FALSE_WORDS = ("no", "false", "off", "0")

# Per type: allowed extra attributes beyond the common ones.
COMMON_ATTRS = {"key", "type", "label", "help", "default", "essential"}
TYPE_ATTRS = {
    "int": {"min", "max", "unit"},
    "float": {"min", "max", "unit"},
    "bool": set(),
    "choice": {"choices"},
    "time_window": set(),
    "text": {"pattern", "max_length", "required", "secret"},
    "url": {"schemes", "required", "secret"},
    "serial_list": {"required"},
    "int_list": {"min", "max", "required"},
    "serial_map": {"min", "max", "unit", "required"},
}
# Types where an empty value is a valid value (unless "required": true).
EMPTY_OK = {"text", "url", "serial_list", "int_list", "serial_map"}

PLUGIN_REQUIRED = ("summary", "alerts", "status")
PLUGIN_OPTIONAL = ("screens", "validate")
STATES = ("ok", "warn", "error", "off", "unknown")
TONES = ("normal", "good", "warn", "bad", "dim")
LEVELS = ("error", "warn", "info")


class ContractError(Exception):
    """A tool breaks the contract. .problems lists every problem found, not just the first."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class FieldError(ValueError):
    """A value doesn't fit its field. The message is shown next to the field as is."""


# --------------------------------------------------------------------- KEY=VALUE files

def parse_keyvalue(text):
    """Parse a KEY=VALUE file (tool.conf, config). Returns (values, problems).

    Blank lines and lines starting with # are skipped. Values are taken
    literally after the first "=", stripped, with one pair of matching quotes
    removed. Nothing is expanded or executed. A repeated key: the first wins
    (as install.sh's conf_get does) and the repeat is reported.
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
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        if not KEY_RE.match(k):
            problems.append(f"line {n}: {k!r} isn't a valid key (A-Z, 0-9, _)")
            continue
        if k in values:
            problems.append(f"line {n}: {k} repeated; the first value is used")
            continue
        values[k] = v
    return values, problems


def read_keyvalue(path):
    with open(path, encoding="utf-8") as f:
        return parse_keyvalue(f.read())


# --------------------------------------------------------------------- manifest

def check_manifest(values, tool_dir=None):
    """Return the manifest with defaults filled in, or raise ContractError."""
    problems = []
    m = dict(MANIFEST_DEFAULTS)
    m.update(values)
    for k, required in MANIFEST_KEYS.items():
        if required and not values.get(k):
            problems.append(f"tool.conf: {k} is required")
    for k in values:
        if k not in MANIFEST_KEYS:
            problems.append(f"tool.conf: unknown key {k}")
    name = m.get("NAME", "")
    if name and not NAME_RE.match(name):
        problems.append("tool.conf: NAME must be lowercase letters, digits and -")
    if name and tool_dir and os.path.basename(os.path.normpath(tool_dir)) != name:
        problems.append(f"tool.conf: NAME {name!r} must match the folder name")
    if len(m.get("SUMMARY", "")) > SUMMARY_MAX:
        problems.append(f"tool.conf: SUMMARY is longer than {SUMMARY_MAX} characters")
    if (m["SCHEMA"] or m["PLUGIN"]) and m["CONTRACT"] != str(CONTRACT):
        problems.append(f"tool.conf: CONTRACT={CONTRACT} is required with SCHEMA or PLUGIN"
                        + (f" (got {m['CONTRACT']!r})" if m["CONTRACT"] else ""))
    if m["SCHEMA"] and not m["CONFIG"]:
        problems.append("tool.conf: CONFIG is required when SCHEMA is set")
    for k in ("CLI", "SCHEMA", "CONFIG", "CONFIG_EXAMPLE", "PLUGIN", "LOCK"):
        p = m.get(k, "")
        if p and (os.path.isabs(p) or ".." in p.split("/")):
            problems.append(f"tool.conf: {k} must be a path inside the tool folder")
        elif p and tool_dir and k in ("CLI", "SCHEMA", "CONFIG_EXAMPLE", "PLUGIN") \
                and not os.path.isfile(os.path.join(tool_dir, p)):
            problems.append(f"tool.conf: {k} file {p} not found")
    if problems:
        raise ContractError(problems)
    return m


def load_manifest(tool_dir):
    path = os.path.join(tool_dir, "tool.conf")
    try:
        values, problems = read_keyvalue(path)
    except OSError as e:
        raise ContractError([f"tool.conf: can't read ({e.strerror})"])
    try:
        m = check_manifest(values, tool_dir)
    except ContractError as e:
        raise ContractError([f"tool.conf: {p}" for p in problems] + e.problems)
    if problems:
        raise ContractError([f"tool.conf: {p}" for p in problems])
    return m


# --------------------------------------------------------------------- schema

def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def check_schema(schema):
    """Check a parsed settings schema. Returns it unchanged, or raises ContractError."""
    problems = []
    if not isinstance(schema, dict):
        raise ContractError(["schema: must be a JSON object"])
    for k in schema:
        if k not in ("contract", "groups"):
            problems.append(f"schema: unknown key {k!r}")
    if schema.get("contract") != CONTRACT:
        problems.append(f"schema: \"contract\" must be {CONTRACT}")
    groups = schema.get("groups")
    if not isinstance(groups, list) or not groups:
        problems.append("schema: \"groups\" must be a non-empty list")
        raise ContractError(problems)
    seen_keys, seen_groups = set(), set()
    for gi, g in enumerate(groups):
        where = f"schema: group {gi + 1}"
        if not isinstance(g, dict):
            problems.append(f"{where}: must be an object")
            continue
        for k in g:
            if k not in ("id", "title", "help", "fields"):
                problems.append(f"{where}: unknown key {k!r}")
        gid = g.get("id")
        if not isinstance(gid, str) or not GROUP_ID_RE.match(gid):
            problems.append(f"{where}: \"id\" must be lowercase letters, digits and -")
        elif gid in seen_groups:
            problems.append(f"{where}: id {gid!r} used twice")
        else:
            seen_groups.add(gid)
            where = f"schema: group {gid}"
        if not isinstance(g.get("title"), str) or not g.get("title"):
            problems.append(f"{where}: \"title\" is required")
        if "help" in g and not isinstance(g["help"], str):
            problems.append(f"{where}: \"help\" must be a string")
        fields = g.get("fields")
        if not isinstance(fields, list) or not fields:
            problems.append(f"{where}: \"fields\" must be a non-empty list")
            continue
        for fi, f in enumerate(fields):
            problems.extend(_check_field(f, f"{where}, field {fi + 1}", seen_keys))
    if problems:
        raise ContractError(problems)
    return schema


def _check_field(f, where, seen_keys):
    p = []
    if not isinstance(f, dict):
        return [f"{where}: must be an object"]
    key = f.get("key")
    if not isinstance(key, str) or not KEY_RE.match(key):
        p.append(f"{where}: \"key\" must be A-Z, 0-9 and _, starting with a letter")
    elif key in seen_keys:
        p.append(f"{where}: key {key} used twice")
    else:
        seen_keys.add(key)
        where = f"schema: {key}"
    t = f.get("type")
    if t not in TYPE_ATTRS:
        return p + [f"{where}: unknown type {t!r} (one of {', '.join(TYPE_ATTRS)})"]
    for k in f:
        if k not in COMMON_ATTRS and k not in TYPE_ATTRS[t]:
            p.append(f"{where}: {k!r} isn't allowed on {t} fields")
    label = f.get("label")
    if not isinstance(label, str) or not label:
        p.append(f"{where}: \"label\" is required")
    elif len(label) > LABEL_MAX:
        p.append(f"{where}: \"label\" is longer than {LABEL_MAX} characters")
    if "help" in f and not isinstance(f["help"], str):
        p.append(f"{where}: \"help\" must be a string")
    for k in ("essential", "required", "secret"):
        if k in f and not isinstance(f[k], bool):
            p.append(f"{where}: {k!r} must be true or false")
    if "unit" in f and not isinstance(f["unit"], str):
        p.append(f"{where}: \"unit\" must be a string")
    num = _is_num if t == "float" else _is_int
    for k in ("min", "max"):
        if k in f and not num(f[k]):
            p.append(f"{where}: {k!r} must be a {'number' if t == 'float' else 'whole number'}")
    if num(f.get("min")) and num(f.get("max")) and f["min"] > f["max"]:
        p.append(f"{where}: \"min\" is above \"max\"")
    if t == "choice":
        ch = f.get("choices")
        if not isinstance(ch, list) or not ch or not all(
                isinstance(c, str) and c and "," not in c for c in ch):
            p.append(f"{where}: \"choices\" must be a non-empty list of strings")
        elif len(set(ch)) != len(ch):
            p.append(f"{where}: \"choices\" has duplicates")
    if "pattern" in f:
        try:
            re.compile(f["pattern"])
        except (re.error, TypeError):
            p.append(f"{where}: \"pattern\" isn't a valid regular expression")
    if "max_length" in f and not (_is_int(f["max_length"]) and f["max_length"] > 0):
        p.append(f"{where}: \"max_length\" must be a positive whole number")
    if "schemes" in f and not (isinstance(f["schemes"], list) and f["schemes"]
                               and all(isinstance(s, str) and s for s in f["schemes"])):
        p.append(f"{where}: \"schemes\" must be a non-empty list of strings")
    if "default" not in f or not isinstance(f["default"], str):
        p.append(f"{where}: \"default\" is required, as the text written in the config file")
    elif not p:
        try:
            parse_value(f, f["default"])
        except FieldError as e:
            p.append(f"{where}: default {f['default']!r} is invalid: {e}")
    return p


def load_schema(path):
    try:
        with open(path, encoding="utf-8") as fh:
            schema = json.load(fh)
    except OSError as e:
        raise ContractError([f"schema: can't read {os.path.basename(path)} ({e.strerror})"])
    except ValueError as e:
        raise ContractError([f"schema: {os.path.basename(path)} isn't valid JSON ({e})"])
    return check_schema(schema)


def fields(schema):
    """Every field, in schema order."""
    return [f for g in schema["groups"] for f in g["fields"]]


def essential_fields(schema):
    return [f for f in fields(schema) if f.get("essential")]


def field_map(schema):
    return {f["key"]: f for f in fields(schema)}


# --------------------------------------------------------------------- values

def _range(f, n, what="value"):
    if "min" in f and n < f["min"]:
        raise FieldError(f"{what} must be at least {_num(f['min'])}")
    if "max" in f and n > f["max"]:
        raise FieldError(f"{what} must be at most {_num(f['max'])}")


def _num(x):
    return str(int(x)) if float(x).is_integer() else str(x)


def _split(text):
    return [x.strip() for x in text.split(",") if x.strip()]


def _whole(text, what="a whole number"):
    if not re.fullmatch(r"-?[0-9]+", text):
        raise FieldError(f"want {what}, got {text!r}")
    return int(text)


def parse_value(f, text):
    """Turn config-file text into a typed value, or raise FieldError with a short message.

    Types: int -> int, float -> float, bool -> bool, choice/text/url -> str,
    time_window -> (datetime.time, datetime.time), serial_list -> [str],
    int_list -> [int], serial_map -> {serial: int}.
    """
    t = f["type"]
    text = text.strip()
    if not text:
        if t not in EMPTY_OK:
            raise FieldError("a value is required")
        if f.get("required"):
            raise FieldError("a value is required")
        return {"serial_list": [], "int_list": [], "serial_map": {}}.get(t, "")
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
    raise FieldError(f"unknown type {t}")  # check_schema stops this earlier


def _no_dupes(items):
    seen = set()
    for x in items:
        if x in seen:
            raise FieldError(f"{x} listed twice")
        seen.add(x)


def format_value(f, value):
    """The inverse of parse_value: the text to write to the config file."""
    t = f["type"]
    if t == "bool":
        return "yes" if value else "no"
    if t == "float":
        return _num(value)
    if t == "time_window":
        return f"{value[0]:%H:%M}-{value[1]:%H:%M}"
    if t in ("serial_list", "int_list"):
        return ",".join(str(x) for x in value)
    if t == "serial_map":
        return ",".join(f"{k}:{v}" for k, v in value.items())
    return str(value)


def validate_config(schema, raw):
    """Check config-file values against the schema.

    raw: {KEY: text} as read from the config file; missing keys take their default.
    Returns (typed, errors): typed has every schema key (a bad value falls back
    to its default, so a tool keeps running on a broken line), errors maps
    KEY -> message. Keys the schema doesn't know are reported under that key.
    """
    typed, errors = {}, {}
    known = field_map(schema)
    for key, f in known.items():
        text = raw.get(key, f["default"])
        try:
            typed[key] = parse_value(f, text)
        except FieldError as e:
            errors[key] = str(e)
            typed[key] = parse_value(f, f["default"])
    for key in raw:
        if key not in known:
            errors[key] = "unknown setting"
    return typed, errors


def check_example(schema, path):
    """Problems where a config.example disagrees with the schema: keys missing on
    either side, or a default that differs (compared as typed values)."""
    raw, problems = read_keyvalue(path)
    problems = [f"{os.path.basename(path)}: {p}" for p in problems]
    known = field_map(schema)
    for key, f in known.items():
        if key not in raw:
            problems.append(f"{os.path.basename(path)}: {key} missing")
            continue
        try:
            if parse_value(f, raw[key]) != parse_value(f, f["default"]):
                problems.append(f"{os.path.basename(path)}: {key}={raw[key]} but the schema "
                                f"default is {f['default']!r}")
        except FieldError as e:
            problems.append(f"{os.path.basename(path)}: {key}: {e}")
    for key in raw:
        if key not in known:
            problems.append(f"{os.path.basename(path)}: {key} isn't in the schema")
    return problems


# --------------------------------------------------------------------- plugin module

def load_plugin(tool_dir, manifest):
    """Import the tool's PLUGIN module, or return None if it has none.

    The module is loaded by path under a private name (better_unas_tool_<name>)
    and the tool folder is not put on sys.path, so two tools' helper modules
    can't shadow each other. Raises ContractError if it can't be imported or
    lacks a required function.
    """
    if not manifest.get("PLUGIN"):
        return None
    path = os.path.join(tool_dir, manifest["PLUGIN"])
    modname = "better_unas_tool_" + manifest["NAME"].replace("-", "_")
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise ContractError([f"plugin: can't load {manifest['PLUGIN']}"])
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # a broken tool must not take the TUI down
        raise ContractError([f"plugin: importing {manifest['PLUGIN']} failed: "
                             f"{type(e).__name__}: {e}"])
    problems = []
    if getattr(mod, "CONTRACT", None) != CONTRACT:
        problems.append(f"plugin: module must set CONTRACT = {CONTRACT}")
    for name in PLUGIN_REQUIRED:
        if not callable(getattr(mod, name, None)):
            problems.append(f"plugin: {name}(ctx) is required")
    for name in PLUGIN_OPTIONAL:
        if hasattr(mod, name) and not callable(getattr(mod, name)):
            problems.append(f"plugin: {name} must be a function")
    if problems:
        raise ContractError(problems)
    return mod


def check_summary(s):
    """Problems with a summary(ctx) return value (empty list = fine)."""
    p = []
    if not isinstance(s, dict):
        return ["summary: must return a dict"]
    if s.get("state") not in STATES:
        p.append(f"summary: state must be one of {', '.join(STATES)}")
    if not isinstance(s.get("headline"), str):
        p.append("summary: headline must be a string")
    items = s.get("items", [])
    if not isinstance(items, list):
        return p + ["summary: items must be a list"]
    for i, it in enumerate(items):
        if not (isinstance(it, dict) and isinstance(it.get("label"), str)
                and isinstance(it.get("value"), str) and it.get("tone", "normal") in TONES):
            p.append(f"summary: item {i + 1} needs label and value strings and a valid tone")
    return p


def check_alerts(alerts):
    p = []
    if not isinstance(alerts, list):
        return ["alerts: must return a list"]
    for i, a in enumerate(alerts):
        ok = (isinstance(a, dict) and isinstance(a.get("id"), str) and a.get("level") in LEVELS
              and isinstance(a.get("message"), str) and isinstance(a.get("active"), bool)
              and _iso(a.get("since")) and (a.get("last") is None or _iso(a.get("last"))))
        if not ok:
            p.append(f"alerts: alert {i + 1} needs id, level ({'/'.join(LEVELS)}), message, "
                     "active, and since/last as ISO 8601 times")
    return p


def check_status(s):
    p = []
    if not isinstance(s, dict):
        return ["status: must return a dict"]
    if not isinstance(s.get("enabled"), bool):
        p.append("status: enabled must be true or false")
    if s.get("state") not in STATES:
        p.append(f"status: state must be one of {', '.join(STATES)}")
    for k in ("problems", "detail"):
        v = s.get(k, [])
        if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            p.append(f"status: {k} must be a list of strings")
    return p


def check_screens(specs):
    p = []
    if not isinstance(specs, list):
        return ["screens: must return a list"]
    ids = set()
    for i, s in enumerate(specs):
        if not (isinstance(s, dict) and isinstance(s.get("id"), str) and GROUP_ID_RE.match(s["id"])
                and isinstance(s.get("tab"), str) and s["tab"] and callable(s.get("make"))):
            p.append(f"screens: screen {i + 1} needs id (lowercase), tab (title) and make(ctx)")
            continue
        if s["id"] in ids:
            p.append(f"screens: id {s['id']!r} used twice")
        ids.add(s["id"])
        if s["tab"] in CORE_TABS:
            p.append(f"screens: tab {s['tab']!r} belongs to the core")
    return p


CORE_TABS = ("Home", "Settings", "Alerts", "Tools")


def _iso(v):
    if not isinstance(v, str):
        return False
    try:
        dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------- tools

class Tool:
    """A tool folder that passed the static checks: manifest and (if any) schema."""

    def __init__(self, path, manifest, schema):
        self.path, self.manifest, self.schema = path, manifest, schema
        self.name = manifest["NAME"]

    def file(self, key):
        """Absolute path of a manifest path key (CLI, CONFIG, LOCK...), or None if unset."""
        rel = self.manifest.get(key)
        return os.path.join(self.path, rel) if rel else None

    def read_config(self):
        """(typed, errors) for the tool's current config file (defaults if it's missing)."""
        if self.schema is None:
            return {}, {}
        try:
            raw, problems = read_keyvalue(self.file("CONFIG"))
        except FileNotFoundError:
            raw, problems = {}, []
        typed, errors = validate_config(self.schema, raw)
        for p in problems:
            errors.setdefault("", p)
        return typed, errors


def load_tool(path):
    """Load a tool's manifest and schema. Raises ContractError listing every problem."""
    path = os.path.abspath(path)
    m = load_manifest(path)
    schema, problems = None, []
    if m["SCHEMA"]:
        try:
            schema = load_schema(os.path.join(path, m["SCHEMA"]))
        except ContractError as e:
            problems += e.problems
    if schema and m["CONFIG_EXAMPLE"]:
        problems += check_example(schema, os.path.join(path, m["CONFIG_EXAMPLE"]))
    if problems:
        raise ContractError(problems)
    return Tool(path, m, schema)


def discover(home):
    """Every subfolder of home with a tool.conf: returns (tools, {folder: problems})."""
    tools, broken = [], {}
    for entry in sorted(os.listdir(home)):
        d = os.path.join(home, entry)
        if os.path.isfile(os.path.join(d, "tool.conf")):
            try:
                tools.append(load_tool(d))
            except ContractError as e:
                broken[entry] = e.problems
    return tools, broken


def check_tool(path):
    """Every problem with a tool, including importing its plugin. Empty list = passes."""
    try:
        tool = load_tool(path)
    except ContractError as e:
        return e.problems
    try:
        load_plugin(tool.path, tool.manifest)
    except ContractError as e:
        return e.problems
    return []


def main(argv):
    if len(argv) < 2 or argv[0] != "check":
        print(__doc__.strip(), file=sys.stderr)
        return 2
    bad = 0
    for path in argv[1:]:
        problems = check_tool(path)
        if problems:
            bad += 1
            print(f"FAIL  {path}")
            for p in problems:
                print(f"      {p}")
        else:
            print(f"OK    {path}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
