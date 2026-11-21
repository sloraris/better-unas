"""First-run setup (#15): preflight, finding an old install, and the plan
the setup wizard shows and carries out.

The bootstrap one-liner (bootstrap.sh) downloads a release into a temporary
folder and starts the TUI from there with --source pointing at it. The TUI
launcher asks needs_setup(home); on a first run it opens the wizard
(core/tui/setup.py), which drives a Setup:

    1. pick tools            Setup.offered, Setup.selected
    2. essential settings    Setup.essentials(name), Setup.set_edits(name, {...})
    3. review                Setup.review(): every change, in words
    4. install               Setup.apply(): core/install.py does the work

Nothing is written before apply(). In a dry run the wizard shows the review
and stops there.

An install made by the old root install.sh (smart-scheduler 0.1.x in
/persistent/better-unas/smart-scheduler/ with config, state.json and .lock,
the old installer kept as /persistent/better-unas/better-unas and linked from
/usr/local/bin) is found by find_legacy(): its tools start ticked, their
current config fills the essential settings, and install_tool keeps config,
state.json and .lock exactly as they are.

Nothing here prints or touches curses; the launcher and the wizard screens do.
"""

import os
import re
import shutil
import tempfile

from . import install as I
from . import plugins as P

MARKER = ".setup-done"          # written by apply(); a home with it never re-runs setup
REQUIRED = ("python3", "curl", "tar")
VERSION_FILE = "/usr/lib/version"
LEGACY_SIGNATURE = "better-unas installer"   # the old install.sh's first comment


# --------------------------------------------------------------------- preflight

def preflight(home, uid=None, version_file=VERSION_FILE, which=shutil.which, force=False):
    """Problems that stop setup, as sentences ([] when all is well):
    not root, not a UNAS (unless force), a required command missing, or
    nowhere to put the home. Nothing is changed."""
    problems = []
    uid = os.geteuid() if uid is None else uid
    if uid != 0:
        problems.append("Run this as root (log in as root@<your UNAS>).")
    if not force:
        try:
            with open(version_file, encoding="utf-8", errors="replace") as f:
                ident = f.read()
        except OSError:
            ident = ""
        if "unas" not in ident.lower():
            problems.append(f"This doesn't look like a UniFi UNAS ({version_file} doesn't say so). "
                            "Use --force to continue anyway.")
    missing = [t for t in REQUIRED if not which(t)]
    if missing:
        problems.append(f"Missing {', '.join(missing)}; this firmware isn't supported.")
    parent = os.path.dirname(os.path.abspath(home))
    if not os.path.isdir(parent):
        problems.append(f"{parent} not found; this firmware isn't supported.")
    return problems


# --------------------------------------------------------------------- old installs

def _read(path, limit=4096):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def is_legacy_entry(path):
    """True if path is the old install.sh kept as the better-unas command."""
    return os.path.isfile(path) and not os.path.islink(path) and LEGACY_SIGNATURE in _read(path)


def find_legacy(home, link=I.LINK):
    """An install made by the old root install.sh, or None.

    It is one when the home has no core/ yet and either the old installer
    script sits at home/better-unas or tool folders are there. Returns
    {"entry": path or None, "link": path or None,
     "tools": {name: {"version", "keep": [files kept], "config": {raw}}}}.
    """
    if not os.path.isdir(home) or os.path.isdir(os.path.join(home, "core")):
        return None
    entry = os.path.join(home, I.ENTRY)
    entry = entry if is_legacy_entry(entry) else None
    tools = {}
    for name in I.installed_tools(home) + I.uninstalled_tools(home):
        d = os.path.join(home, name)
        conf = os.path.join(d, "tool.conf")
        if not os.path.isfile(conf):
            conf = os.path.join(d, I.UNINSTALLED)
        m = I._manifest(conf)
        cfg_name = m.get("CONFIG") or "config"
        keep = [f for f in (cfg_name, "state.json", m.get("LOCK") or ".lock")
                if os.path.exists(os.path.join(d, f))]
        tools[name] = {"version": m.get("VERSION", "?"), "keep": keep,
                       "installed": os.path.isfile(os.path.join(d, "tool.conf")),
                       "config": _raw_config(os.path.join(d, cfg_name))}
    if entry is None and not tools:
        return None
    linked = None
    if link and os.path.islink(link) and entry and os.readlink(link) == entry:
        linked = link
    return {"entry": entry, "link": linked, "tools": tools}


def needs_setup(home):
    """Should the launcher open the setup wizard? Yes for an old install's
    home, and for a home with no tools that hasn't finished setup before."""
    if not home:
        return False
    if find_legacy(home) is not None:
        return True
    if os.path.exists(os.path.join(home, MARKER)):
        return False
    return not (I.installed_tools(home) or I.uninstalled_tools(home))


def default_source(home, root):
    """Where a first run installs from when no --source was given: the
    release kept in the home, else the folder this code came from if it
    holds tools. None if neither."""
    kept = I.release_source(home)
    if kept and I.release_tools(kept):
        return kept
    if root and I.release_tools(root):
        return root
    return None


# --------------------------------------------------------------------- config files

def _raw_config(path):
    try:
        values, _ = P.read_keyvalue(path)
    except OSError:
        return {}
    return values


_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*?)(\s*)$")


def apply_edits(text, edits):
    """text with KEY=VALUE edits applied: the first active line of a key
    gets the new value (indent and spacing kept), a missing key is added
    at the end. Comments and every other line stay as they are.

    A deliberately small writer for the few essential settings; the
    Settings screen's full writer can replace it later."""
    lines = text.splitlines(keepends=True)
    eol = "\r\n" if any(ln.endswith("\r\n") for ln in lines) else "\n"
    todo = dict(edits)
    for i, ln in enumerate(lines):
        body = ln.rstrip("\r\n")
        if body.lstrip().startswith("#"):
            continue
        m = _LINE.match(body)
        if m and m.group(2) in todo:
            lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{todo.pop(m.group(2))}{m.group(5)}" \
                       + ln[len(body):]
    if todo:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += eol
        lines += [f"{k}={v}{eol}" for k, v in todo.items()]
    return "".join(lines)


def write_private(path, text):
    """Write a config file atomically with mode 600."""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# --------------------------------------------------------------------- the plan

class Offer:
    """A tool the release offers, with what setup needs to know about it."""

    def __init__(self, src, name, manifest):
        self.name, self.manifest = name, manifest
        self.path = os.path.join(src, name)
        self.version = manifest.get("VERSION", "?")
        self.summary = manifest.get("SUMMARY", "")
        self.changes = manifest.get("CHANGES", "")
        self.setup_note = manifest.get("SETUP", "")
        self.schema, self.problems = None, []
        try:
            tool = P.load_tool(self.path)
            self.schema = tool.schema
        except P.ContractError as e:
            self.problems = e.problems

    @property
    def config_name(self):
        c = self.manifest.get("CONFIG", "")
        return c if c and "/" not in c else ("config" if self.schema else "")

    @property
    def example_name(self):
        c = self.manifest.get("CONFIG_EXAMPLE", "")
        return c if c and "/" not in c else ""

    def essentials(self):
        return P.essential_fields(self.schema) if self.schema else []

    def triggers(self):
        d = os.path.join(self.path, "triggers")
        if not os.path.isdir(d):
            return []
        return sorted(f for f in os.listdir(d) if not f.startswith("."))


class Setup:
    """Everything the wizard decides, and apply() to carry it out.

    home    the install folder (/persistent/better-unas on the NAS)
    source  an extracted release (or a checkout) holding VERSION, core/ and tools
    link    the short command, or None to leave it alone (tests, other homes)
    """

    def __init__(self, home, source, link="default", run=None):
        self.home = os.path.abspath(home)
        self.source = os.path.abspath(source)
        if link == "default":
            link = I.LINK if self.home == I.DEFAULT_HOME else None
        self.link = link
        self.run = run or I.run_script
        self.version = _read(os.path.join(self.source, "VERSION")).strip().splitlines()[0:1]
        self.version = self.version[0] if self.version else "?"
        self.offered = [Offer(self.source, n, m) for n, m in I.release_tools(self.source).items()]
        self.legacy = find_legacy(self.home, link=self.link)
        self.installed = {n: I._manifest(os.path.join(self.home, n, "tool.conf")).get("VERSION", "?")
                          for n in I.installed_tools(self.home)}
        # An old install's tools start ticked; so does anything already installed.
        self.selected = [o.name for o in self.offered if o.name in self.installed
                         or (self.legacy and self.legacy["tools"].get(o.name, {}).get("installed"))]
        self.edits = {}           # name -> {KEY: text} the user chose
        self.done = False

    # --- step 1
    def offer(self, name):
        return next(o for o in self.offered if o.name == name)

    def toggle(self, name):
        if name in self.selected:
            self.selected.remove(name)
        else:
            self.selected.append(name)
        self.selected.sort(key=[o.name for o in self.offered].index)

    def chosen(self):
        return [self.offer(n) for n in self.selected]

    # --- step 2
    def current_config(self, name):
        """{KEY: text} the tool's config has now (an old install's, or none)."""
        o = self.offer(name)
        if not o.config_name:
            return {}
        return _raw_config(os.path.join(self.home, name, o.config_name))

    def essential_schema(self, name):
        """The tool's schema cut down to its essential fields (None if none)."""
        o = self.offer(name)
        if not o.essentials():
            return None
        groups = []
        for g in o.schema["groups"]:
            fs = [f for f in g["fields"] if f.get("essential")]
            if fs:
                groups.append(dict(g, fields=fs))
        return dict(o.schema, groups=groups)

    def needs_settings(self):
        """Selected tools with essential settings, in order."""
        return [o.name for o in self.chosen() if o.essentials()]

    def starting_text(self, name):
        """{KEY: text} the form starts from: the current config, else defaults."""
        cur = self.current_config(name)
        return {f["key"]: cur.get(f["key"], f["default"]) for f in self.offer(name).essentials()}

    def set_edits(self, name, texts):
        """Keep only the values that differ from what the tool would use now."""
        o = self.offer(name)
        start = self.starting_text(name)
        out = {}
        for f in o.essentials():
            k = f["key"]
            if k not in texts:
                continue
            new = P.format_value(f, P.parse_value(f, texts[k]))   # raises FieldError if bad
            try:
                same = P.parse_value(f, start[k]) == P.parse_value(f, new)
            except P.FieldError:
                same = False
            if not same:
                out[k] = new
        self.edits[name] = out
        return out

    # --- step 3
    def review(self):
        """What apply() will change: [(heading, [(line, tone)])]."""
        home = self.home
        sections = []
        old_entry = self.legacy["entry"] if self.legacy else None
        has_entry = os.path.isfile(os.path.join(self.source, I.ENTRY))
        what = "core/, VERSION and the better-unas command" if has_entry else "core/ and VERSION"
        core = [(f"Copy {what} to {home}/", "normal"),
                (f"Keep this release in {home}/{I.RELEASE_DIR}/ for installing tools later", "normal")]
        if old_entry and has_entry:
            core.append((f"Replace the old installer script at {old_entry}", "warn"))
        elif old_entry:
            core.append((f"Move the old installer script aside to {old_entry}.old", "warn"))
        elif not has_entry:
            core.append(("This release has no better-unas command; run python3 -m core.tui "
                         f"in {home}", "warn"))
        if self.link and has_entry:
            core.append((f"Link {self.link} -> {home}/{I.ENTRY}", "normal"))
        sections.append((f"better-unas {self.version}", core))

        for o in self.chosen():
            dest = os.path.join(home, o.name)
            had = self.installed.get(o.name) or (self.legacy and self.legacy["tools"].get(o.name, {})
                                                 .get("version"))
            if not had:
                what = "new"
            elif had == o.version:
                what = "reinstall"
            else:
                what = f"update from {had}"
            lines = []
            if o.setup_note:
                lines.append((f"Before it runs: {o.setup_note}", "warn"))
            if o.changes:
                lines.append((o.changes, "accent"))
            lines.append((f"Copy its files to {dest}/", "normal"))
            if o.config_name:
                lines += self._config_lines(o, dest)
            keep = self._kept(o, dest)
            if keep:
                lines.append((f"Keep {', '.join(keep)} as they are", "good"))
            trig = o.triggers()
            if trig:
                lines.append((f"Triggers its install.sh sets up: {', '.join(trig)}", "normal"))
            lines.append(("Run its install.sh", "normal"))
            if o.problems:
                lines += [(f"Problem: {p}", "bad") for p in o.problems]
            sections.append((f"{o.name} {o.version} ({what})", lines))

        left = sorted(set(self.installed) | set((self.legacy or {}).get("tools", {})))
        left = [n for n in left if n not in self.selected]
        if left:
            sections.append(("Left as they are", [(n, "dim") for n in left]))
        return sections

    def _config_lines(self, o, dest):
        cfg = os.path.join(dest, o.config_name)
        edits = self.edits.get(o.name, {})
        lines = []
        if os.path.exists(cfg):
            lines.append((f"Keep {o.config_name}" + ("; change:" if edits else " unchanged"), "normal"))
        elif o.example_name:
            lines.append((f"Create {o.config_name} from {o.example_name}" + (" with:" if edits else
                                                                              " (all defaults)"), "normal"))
        else:
            lines.append((f"Create {o.config_name}" + (" with:" if edits else " (all defaults)"), "normal"))
        start = self.starting_text(o.name)
        for k, v in edits.items():
            lines.append((f"  {k}={v}   (was {start.get(k) or 'empty'})", "warn"))
        return lines

    def _kept(self, o, dest):
        names = [o.config_name, "state.json", o.manifest.get("LOCK") or ".lock"]
        return [n for n in dict.fromkeys(names) if n and os.path.exists(os.path.join(dest, n))]

    def review_text(self):
        out = []
        for heading, lines in self.review():
            out.append(heading)
            out += [f"  {line}" for line, _ in lines]
            out.append("")
        return "\n".join(out).rstrip() + "\n"

    # --- step 4
    def apply(self):
        """Carry out the plan. Returns {"tools": [(name, ok, output)],
        "link": bool, "legacy": path the old installer was moved to or None}."""
        if self.done:
            raise I.InstallError("setup already ran")
        home = self.home
        os.makedirs(home, exist_ok=True)
        old_entry = self.legacy["entry"] if self.legacy else None
        moved = None
        I.install_core(self.source, home)
        src = I.release_source(home) or self.source
        entry = os.path.join(home, I.ENTRY)
        if old_entry and is_legacy_entry(entry):
            # The release brought no new command: keep the old script beside
            # the new layout rather than leaving an installer that no longer fits.
            moved = entry + ".old"
            os.replace(entry, moved)
        results = []
        for o in self.chosen():
            try:
                dest = I.copy_tool(src, home, o.name)
                self._write_config(o, dest)
                mark = os.path.join(dest, I.UNINSTALLED)
                if os.path.exists(mark):
                    os.remove(mark)
                ok, out = self.run(dest, "install.sh")
            except (OSError, I.InstallError, P.FieldError) as e:
                ok, out = False, f"{type(e).__name__}: {e}\n"
            results.append((o.name, ok, out))
        linked = False
        if self.link and os.path.isfile(entry):
            try:
                linked = I.restore_link(home, self.link)
            except OSError:
                linked = False
        with open(os.path.join(home, MARKER), "w", encoding="utf-8") as f:
            f.write(self.version + "\n")
        self.done = True
        return {"tools": results, "link": linked, "legacy": moved}

    def _write_config(self, o, dest):
        if not o.config_name:
            return
        cfg = os.path.join(dest, o.config_name)
        edits = self.edits.get(o.name, {})
        if not edits:
            return      # an old install's config stays untouched; a new one comes from install.sh
        if os.path.exists(cfg):
            text = _read(cfg, limit=None)
        elif o.example_name and os.path.isfile(os.path.join(dest, o.example_name)):
            text = _read(os.path.join(dest, o.example_name), limit=None)
        else:
            text = ""
        write_private(cfg, apply_edits(text, edits))
