"""The Settings tab (#12, docs/tui-style.md §6): better-unas itself and every
tool the home knows about.

    SettingsScreen   the tab: better-unas, then every tool (installed or not)
    CoreScreen       Settings › better-unas: version and updates (u, c),
                     remove (R), and the developer-mode settings
    NotifyScreen     Settings › Notifications: the shared webhook and
                     heartbeat (core/notify.py), `t` sends a test
    ToolFormScreen   an installed tool's settings, plus uninstall / reinstall
    ToolInfoScreen   a tool without a settings form (not installed, broken,
                     no schema): what it is, and install / uninstall
    FormScreen       one settings form: edit, `s` saves, `p` previews, `t`
                     tests a URL setting, Esc offers to discard
    SettingsForm     the reusable part: a schema (or a subset of one), the
                     current values and an on-save callback. First-run setup
                     (#15) builds one per tool from its essential settings.
    schema_subset    a schema cut down to some keys (or the essential ones)
    save_config      the save FormScreen does: validate, take the run lock,
                     write through the comment-keeping writer (core/configfile.py),
                     or only check in a dry run

The config file stays the source of truth: the form reads it when it opens
and a save changes only the edited keys, keeping comments and hand edits.
"""

import urllib.parse

from .. import configfile
from .. import notify as N
from .. import plugins as P
from .. import updates as U
from . import widgets as W


def schema_subset(schema, keys=None, essential=False):
    """A copy of schema with only the given keys (or the essential fields),
    in schema order; groups left empty are dropped."""
    want = set(keys or ())
    groups = []
    for g in schema["groups"]:
        fs = [f for f in g["fields"] if f["key"] in want or (essential and f.get("essential"))]
        if fs:
            groups.append(dict(g, fields=fs))
    return dict(schema, groups=groups)


def save_config(ctx, edits):
    """Write edits ({KEY: text}) to ctx.tool's config under the run lock.

    Raises configfile.ValidationError (with .errors per key) before taking the
    lock if an edit is invalid, and ctx.LockBusy if the scheduled run holds the
    lock too long; either way nothing is written. In a dry run the edits are
    checked and nothing is written. Returns the message to show.
    """
    tool = ctx.tool
    path = tool.file("CONFIG")
    shown = f"{tool.name}/{tool.manifest['CONFIG']}"
    try:
        with open(path, encoding="utf-8", newline="") as f:
            old = f.read()
    except FileNotFoundError:
        old = ""
    configfile.apply_edits(old, edits, tool.schema)      # validates; raises before the lock
    if ctx.dry_run:
        ctx.log(f"dry run: would save {sorted(edits)} to {path}")
        return f"Dry run: {len(edits)} setting(s) are valid; nothing was written to {shown}."
    with ctx.lock():
        configfile.save(path, edits, tool.schema)
    ctx.log(f"saved {sorted(edits)} to {path}")
    return f"Saved {len(edits)} setting(s) to {shown}."


class SettingsForm:
    """A settings form that validates and saves.

    schema: a contract schema (schema_subset() for part of one).
    raw: {KEY: text} as in the config file; a missing key shows its default.
    on_save(edits) gets {KEY: text} for the changed fields once every field is
    valid (schema rules plus validate(values), the plugin's cross-field rules),
    and returns the message to show (or None). If it raises an exception with
    an `errors` dict, those messages go onto the fields; ctx.LockBusy and any
    other exception become a message. Either way the edits stay on screen.
    After a save, the saved values count as unchanged.
    """

    def __init__(self, ctx, schema, raw, on_save, validate=None, suggestions=None, pickers=None):
        self.ctx, self.on_save, self.validator = ctx, on_save, validate
        self.form = W.Form(schema, raw, suggestions, pickers)
        self.save_errors = {}
        self.revalidate()

    # --- reading
    @property
    def editing(self):
        return self.form.editing

    def changed(self):
        return self.form.changed()

    def errors(self):
        return self.form.errors()

    def values(self):
        return self.form.values()

    # --- validation
    def revalidate(self):
        extra = {}
        if self.validator is not None:
            try:
                extra = dict(self.validator(self.form.values()) or {})
            except Exception as e:
                self.ctx.message(f"validate: {type(e).__name__}: {e}", "bad")
        for k, msg in self.save_errors.items():
            extra.setdefault(k, msg)
        self.form.set_extra_errors(extra)

    def problems(self):
        """[(label, message)] for every field with an error, in form order."""
        return [(f.label, f.error) for f in self.form.fields if f.error]

    def select_first_error(self):
        for i, f in enumerate(self.form.fields):
            if f.error:
                self.form.scroll.index = i
                return

    # --- saving
    def save(self):
        """Validate and call on_save. True if the edits were saved."""
        self.save_errors = {}
        self.revalidate()
        problems = self.problems()
        if problems:
            label, msg = problems[0]
            more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
            self.ctx.message(f"Not saved: {label}: {msg}{more}.", "bad")
            self.select_first_error()
            return False
        edits = self.changed()
        if not edits:
            self.ctx.message("Nothing to save: no setting was changed.")
            return False
        try:
            result = self.on_save(edits)
        except self.ctx.LockBusy as e:
            self.ctx.message(f"Not saved: {e}. Your edits are still here.", "warn")
            return False
        except Exception as e:
            errors = getattr(e, "errors", None)
            if isinstance(errors, dict) and errors:
                self.save_errors = {k: str(v) for k, v in errors.items()}
                self.revalidate()
                self.select_first_error()
                self.ctx.message(f"Not saved: {e}.", "bad")
            else:
                self.ctx.message(f"Not saved: {type(e).__name__}: {e}", "bad")
            return False
        if self.ctx.dry_run:
            # Nothing was written, so the edits are still unsaved.
            self.ctx.message(result or "Dry run: nothing was written.", "warn")
            return False
        for f in self.form.fields:
            f.initial = f.text
        self.ctx.message(result or f"Saved {len(edits)} setting(s).", "good")
        return True

    # --- screen protocol
    def keys(self):
        ks = self.form.keys()
        return ks if self.editing else [("s", "save")] + ks

    def key(self, k):
        if not self.editing and k == "s":
            self.save()
            return True
        used = self.form.key(k)
        if used:
            self.save_errors = {}       # the writer's verdict was for the old values
            self.revalidate()
        return used

    def render(self, canvas):
        self.form.render(canvas)


CORE_NAME = "better-unas"
NOTIFY_NAME = "Notifications"


def where(url):
    """scheme://host of a URL, for a confirm: never the path, which often holds a token."""
    try:
        u = urllib.parse.urlsplit(url)
        host = u.hostname
    except ValueError:
        host = None
    return f"{u.scheme}://{host}" if host else "the URL on screen"


def core_entry(app):
    """(Tool, Context) for better-unas's own settings (core/settings.json,
    saved to better-unas.conf in the home), or None if the schema can't load."""
    from .app import Context
    from .suite import home_of
    home = home_of(app)
    try:
        schema = U.core_schema()
    except Exception:
        return None
    manifest = {"NAME": CORE_NAME, "CONFIG": U.CORE_CONFIG, "LOCK": ".lock",
                "SUMMARY": "better-unas itself: updates and developer mode"}
    tool = P.Tool(home, manifest, schema)
    return tool, Context(app, tool)


def branch_picker(ctx):
    """load() for the Branch field: the branches on GitHub, or None after
    saying why there are none."""
    from .suite import updater_of

    def load():
        try:
            names = ctx.run_slow(updater_of(ctx.app).fetch_branches, "Listing branches on GitHub")
        except (OSError, ValueError) as e:
            ctx.message(f"Couldn't list branches: {getattr(e, 'reason', e)}.", "bad")
            return None
        if not names:
            ctx.message("GitHub listed no branches.", "bad")
        return names
    return load


class SettingsScreen:
    """The Settings tab: better-unas first, then every tool the home knows
    about, installed or not. Enter opens the page for the selected one."""

    title = "Settings"

    def __init__(self, ctx):
        self.ctx = ctx
        self.core = core_entry(ctx.app)
        self.entries = []        # ("heading", text), ("core" | "notify", None) or ("tool", row)
        self.index = 0           # into self.entries, always a selectable entry
        self.refresh()

    @property
    def app(self):
        return self.ctx.app

    def refresh(self):
        from . import suite
        entries = [("heading", "better-unas")]
        if self.core:
            entries += [("core", None), ("notify", None)]
        rows = suite.tool_rows(self.app)
        entries.append(("heading", "Tools"))
        entries += [("tool", r) for r in rows] or [("empty", "No tools found.")]
        keep = self.current_name()
        self.entries = entries
        stops = self.stops()
        names = [self.name_of(self.entries[i]) for i in stops]
        self.index = stops[names.index(keep)] if keep in names else (stops[0] if stops else 0)

    def stops(self):
        return [i for i, (kind, _) in enumerate(self.entries) if kind in ("core", "notify", "tool")]

    @staticmethod
    def name_of(entry):
        kind, item = entry
        return {"core": CORE_NAME, "notify": NOTIFY_NAME}.get(kind) or (item["name"] if kind == "tool" else None)

    def current_name(self):
        if 0 <= self.index < len(self.entries):
            return self.name_of(self.entries[self.index])
        return None

    def show(self):
        self.refresh()

    def keys(self):
        name = self.current_name()
        return ([("Enter", f"open {name}")] if name else []) + [("↑↓", "move")]

    def line(self, entry):
        """(lead text, status kind or None, detail text, tone) for a list
        row: better-unas shows its version then the update status, a tool
        its state (with its glyph) then how many settings it has."""
        from . import suite
        kind, item = entry
        if kind == "core":
            up = suite.updater_of(self.app)
            status = up.cached_status()
            text, tone = suite.Actions(self.ctx).headline(status)
            available = bool(status and status["available"])
            return f"{up.installed_label()}  ", "update" if available else None, text, \
                "warn" if available else "dim"
        if kind == "notify":
            return "", None, N.describe(self.core[1].config()), "normal"
        r = item
        glyph, state = suite.tool_state(r)
        tool = r["tool"]
        extra = ""
        if tool is not None and tool.schema:
            n = len(P.fields(tool.schema))
            try:
                problems = tool.read_config()[1]
            except OSError:
                problems = {"": "can't read the config file"}
            extra = f"  {n} settings" + (f", {len(problems)} config problem" + ("s" if len(problems) > 1 else "")
                                         if problems else "")
        return "", glyph, state + extra, "dim" if glyph is None else "normal"

    def render(self, canvas):
        y = 1
        stops = self.stops()
        for i, entry in enumerate(self.entries):
            if y >= canvas.height:
                break
            kind, item = entry
            if kind == "heading":
                if y > 1:
                    y += 1
                W.heading(canvas, y, 0, item)
                y += 1
                continue
            if kind == "empty":
                canvas.styled(y, 2, item, "label")
                y += 1
                continue
            name = self.name_of(entry)
            lead, glyph, text, tone = self.line(entry)
            chosen = i == self.index and i in stops
            if chosen:
                canvas.fill(y, 0, 1, canvas.width, tone="selected")
                canvas.text(y, 0, canvas.symbols["marker"], "selected", bold=True)
                canvas.text(y, 2, W.pad(name, 20), "selected", bold=True)
            else:
                canvas.text(y, 2, W.pad(name, 20), "normal", bold=True)
            x = 23
            if lead:
                canvas.text(y, x, lead, "selected" if chosen else "normal")
                x += len(lead)
            if glyph:
                x = canvas.status(y, x, glyph) + 1
            canvas.text(y, x, W.fit(text, max(0, canvas.width - x - 1)), "selected" if chosen else tone)
            y += 1

    def move(self, step):
        stops = self.stops()
        if not stops:
            return
        pos = stops.index(self.index) if self.index in stops else 0
        self.index = stops[max(0, min(len(stops) - 1, pos + step))]

    def open(self, name):
        """Open the page for better-unas or a tool by name. True if found."""
        for i, entry in enumerate(self.entries):
            if self.name_of(entry) == name:
                self.index = i
                self.ctx.push(self.page(entry))
                return True
        return False

    def page(self, entry):
        kind, r = entry
        app = self.app
        if kind == "core":
            core_ctx = self.core[1]
            return CoreScreen(core_ctx, pickers={"DEV_BRANCH": branch_picker(core_ctx)})
        if kind == "notify":
            return NotifyScreen(self.core[1])
        tool = r["tool"]
        if tool is not None and tool.schema and tool.name in app.contexts and not r.get("broken"):
            return ToolFormScreen(app.contexts[tool.name], app.plugins.get(tool.name), r)
        return ToolInfoScreen(self.ctx, r)

    def key(self, k):
        if k in ("up", "down"):
            self.move(-1 if k == "up" else 1)
            return True
        if k in ("home", "end"):
            self.move(-len(self.entries) if k == "home" else len(self.entries))
            return True
        if k == "enter" and self.index in self.stops():
            self.ctx.push(self.page(self.entries[self.index]))
            return True
        return False


class FormScreen:
    """One tool's settings: a SettingsForm saving with save_config(), a status
    line, `p` to preview the effect (the plugin's optional preview(ctx, values))
    and a discard question when leaving with unsaved edits."""

    def __init__(self, ctx, plugin=None, pickers=None, schema=None, tester=None):
        """schema: the part of the tool's schema to show (default all of it;
        the save still checks against the whole). tester(key, values): the
        `t` test for a URL setting, default the plugin's send_test."""
        self.ctx, self.plugin = ctx, plugin
        tool = ctx.tool
        self.title = tool.name
        try:
            raw, self.file_problems = P.read_keyvalue(tool.file("CONFIG"))
        except FileNotFoundError:
            raw, self.file_problems = {}, []
        known = P.field_map(tool.schema)
        self.unknown = sorted(k for k in raw if k not in known and k not in P.SHARED_KEYS)
        validate = getattr(plugin, "validate", None) if plugin is not None else None
        if tester is None and callable(getattr(plugin, "send_test", None)):
            tester = lambda key, values: plugin.send_test(ctx, key, values)  # noqa: E731
        self.tester = tester
        self.sform = SettingsForm(ctx, schema or tool.schema, raw, lambda edits: save_config(ctx, edits),
                                  validate=validate if callable(validate) else None,
                                  pickers=pickers)

    @property
    def form(self):
        return self.sform.form

    @property
    def can_preview(self):
        return self.plugin is not None and callable(getattr(self.plugin, "preview", None))

    @property
    def can_test(self):
        """A test for the selected field: a URL setting and a tester."""
        f = self.form.current if self.form.fields else None
        return self.tester is not None and f is not None and f.spec["type"] == "url"

    def keys(self):
        ks = self.sform.keys()
        if not self.sform.editing and self.can_preview:
            ks = ks[:1] + [("p", "preview")] + ks[1:]
        if not self.sform.editing and self.can_test:
            ks = ks[:1] + [("t", "send test")] + ks[1:]
        return ks

    def values(self):
        """Typed settings with the form's unsaved edits applied."""
        values = dict(self.ctx.config())
        values.update(self.sform.values())
        return values

    def send_test(self):
        """`t`: one test through the selected URL setting, using the values on
        screen (saved or not). The result is a message."""
        field = self.form.current
        label = field.label
        if field.error:
            return self.ctx.message(f"{label}: fix it first ({field.error}).", "bad")
        url = field.value
        if not url:
            return self.ctx.message(f"{label}: enter a URL first.", "warn")
        if not self.ctx.confirm(f"Send a test to {label}?",
                                [f"To {where(url)}, using the settings on screen (saved or not)."]):
            return
        values = self.values()
        try:
            ok = self.ctx.run_slow(lambda: self.tester(field.key_name, values), f"Sending a test to {label}")
        except Exception as e:
            return self.ctx.message(f"{label}: test failed: {str(e) or type(e).__name__}", "bad")
        if ok is None:
            return self.ctx.message(f"{label}: this setting has no test.", "dim")
        self.ctx.message(f"{label}: {ok}.", "good")

    def preview(self):
        problems = self.sform.problems()
        if problems:
            label, msg = problems[0]
            self.ctx.message(f"Fix {label} first: {msg}.", "bad")
            self.sform.select_first_error()
            return
        screen = self.plugin.preview(self.ctx, self.sform.values())
        if screen is not None:
            self.ctx.push(screen)

    def key(self, k):
        if not self.sform.editing and k == "p" and self.can_preview:
            self.preview()
            return True
        if not self.sform.editing and k == "t" and self.can_test:
            self.send_test()
            return True
        return self.sform.key(k)

    def status_line(self):
        n = len(self.sform.changed())
        if n:
            return f"{n} unsaved change(s); s saves, Esc discards", "warn"
        notes = []
        if self.unknown:
            notes.append(f"kept as is: {', '.join(self.unknown)}")
        if self.file_problems:
            notes.append(f"{len(self.file_problems)} line(s) the loader skips")
        if notes:
            return "; ".join(notes), "dim"
        return "no unsaved changes", "dim"

    def header(self, canvas):
        """Rows drawn above the file line; returns how many. Pages with
        more to say (Settings › better-unas) override it."""
        return 0

    def state(self):
        """(status kind, text) shown before the file line, or None."""
        return None

    def render(self, canvas):
        tool = self.ctx.tool
        more = canvas.symbols["more"]
        top = self.header(canvas)
        x = 0
        state = self.state()
        if state:
            x = canvas.status(top, 0, state[0], state[1]) + 3
        path = f"{tool.name}/{tool.manifest['CONFIG']}"
        canvas.text(top, x, W.fit(path, canvas.width // 2 - x, more), "dim")
        text, tone = self.status_line()
        room = canvas.width - x - len(path) - 3
        text = W.fit(text, max(0, room), more)
        canvas.text(top, canvas.width - len(text), text, tone, bold=tone == "warn")
        self.sform.render(canvas.sub(top + 1, 0, canvas.height - top - 1, canvas.width))

    def can_leave(self):
        n = len(self.sform.changed())
        if not n:
            return True
        return self.ctx.confirm("Discard your changes?",
                                [f"{n} setting(s) were edited and not saved. The config file stays as it was."])


class CoreScreen(FormScreen):
    """Settings › better-unas: the version and update status on top, the
    developer-mode settings below. u updates, c checks, R removes better-unas.
    Saving a change to developer mode or the branch checks again straight
    away, so the update line is about what you just picked."""

    DEV_KEYS = ("DEV_MODE", "DEV_BRANCH")

    def __init__(self, ctx, pickers=None):
        from . import suite
        super().__init__(ctx, pickers=pickers, schema=schema_subset(ctx.tool.schema, self.DEV_KEYS))
        self.actions = suite.Actions(ctx)
        self.status = None
        save = self.sform.on_save

        def on_save(edits):
            result = save(edits)
            if any(k in edits for k in self.DEV_KEYS) and not ctx.dry_run:
                self.status = self.actions.check()
            return result

        self.sform.on_save = on_save

    @property
    def updater(self):
        return self.actions.updater

    def show(self):
        self.status = self.updater.cached_status()

    def keys(self):
        if self.sform.editing:
            return self.sform.keys()
        ks = []
        if self.status and self.status["available"]:
            ks.append(("u", "update"))
        ks += [("c", "check")] + super().keys()[:1] + [("R", "remove better-unas")] + super().keys()[1:]
        return ks

    def header(self, canvas):
        s = self.status
        up = self.updater
        label = up.installed_label() or "(no VERSION)"
        if up.dev_branch():
            kind = "developer mode"
        else:
            kind = "not a release" if U.installed_ref(up.home) else "release"
        text, tone = self.actions.headline(s)
        if s and s["available"]:
            update = ("status", "update", text)
        else:
            update = (text, "value" if tone != "dim" else "label")
        rows = [("Version", label, kind), ("Update", update)]
        if s:
            from .screens import when
            note = f"checked {when(s['checked_at'])}"
            if s["available"]:
                note += ". Press u to update."
            rows.append(("note", note))
        end = W.KeyValue(rows).render(canvas)
        return end + 1

    def key(self, k):
        if not self.sform.editing:
            if k == "u":
                self.actions.update(self.status)
                self.show()
                return True
            if k == "c":
                self.status = self.actions.check()
                return True
            if k == "R":
                self.actions.remove_all()
                return True
        return super().key(k)


class NotifyScreen(FormScreen):
    """Settings › Notifications: the webhook, heartbeat and alert repeat every
    tool shares, saved in better-unas.conf. `t` sends a test through the
    selected URL setting (core/notify.py)."""

    def __init__(self, ctx):
        schema = schema_subset(ctx.tool.schema, N.KEYS)
        super().__init__(ctx, schema=schema, tester=lambda key, values: N.send_test(values, key))
        self.title = NOTIFY_NAME


class ToolFormScreen(FormScreen):
    """An installed tool's settings page: its form, its state before the file
    line, and x uninstall / i reinstall."""

    def __init__(self, ctx, plugin, row):
        from . import suite
        super().__init__(ctx, plugin)
        self.row = row
        self.actions = suite.Actions(ctx.app.core_ctx)

    def state(self):
        from . import suite
        glyph, text = suite.tool_state(self.row)
        return (glyph or "ok", text)

    def keys(self):
        ks = super().keys()
        if self.sform.editing:
            return ks
        moves = [k for k in ks if k[0] in ("↑↓",)]
        rest = [k for k in ks if k not in moves]
        return rest + [("x", "uninstall"), ("i", "reinstall")] + moves

    def key(self, k):
        if not self.sform.editing:
            if k == "x":
                self.actions.uninstall(self.row)
                return True
            if k == "i":
                self.actions.install(self.row)
                return True
        return super().key(k)


class ToolInfoScreen:
    """A tool's page when there's no settings form to show: not installed,
    uninstalled, broken, without a schema, or installed after the app started.
    What it is and where it stands, and i install / x uninstall."""

    def __init__(self, ctx, row):
        from . import suite
        self.ctx, self.row = ctx, row
        self.title = row["name"]
        self.actions = suite.Actions(ctx)
        self.text = W.ScrollText(suite.tool_details(row, self.actions.updater))

    def show(self):
        from . import suite
        rows = {r["name"]: r for r in suite.tool_rows(self.ctx.app)}
        self.row = rows.get(self.row["name"], self.row)
        self.text = W.ScrollText(suite.tool_details(self.row, self.actions.updater))

    def keys(self):
        installed = self.row["where"] == "installed"
        ks = [("i", "reinstall" if installed else "install")]
        if installed:
            ks.append(("x", "uninstall"))
        return ks + self.text.keys()

    def render(self, canvas):
        self.text.render(canvas)

    def key(self, k):
        if k == "i":
            self.actions.install(self.row)
            return True
        if k == "x" and self.row["where"] == "installed":
            self.actions.uninstall(self.row)
            return True
        return self.text.key(k)
