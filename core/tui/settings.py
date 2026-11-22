"""The Settings tab (#12): every installed tool's settings schema as a form.

    SettingsScreen   the tab: pick a tool
    FormScreen       one tool's settings: edit, `s` saves, `p` previews, Esc
                     offers to discard
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

from .. import configfile
from .. import plugins as P
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


def core_entry(app):
    """(Tool, Context) for better-unas's own settings (core/settings.json,
    saved to better-unas.conf in the home), or None if the schema can't load."""
    from .. import updates as U
    from .app import Context
    from .tools import home_of
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
    from .tools import updater_of

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
    """The Settings tab: pick a tool to see its settings form."""

    title = "Settings"

    def __init__(self, ctx):
        self.ctx = ctx
        self.core = core_entry(ctx.app)
        self.tools = [t for t in ctx.app.tools if t.schema] + ([self.core[0]] if self.core else [])
        self.list = W.ListView([], empty="No installed tool has settings.")
        self.refresh()

    def refresh(self):
        items = []
        for t in self.tools:
            text = f"{t.name:<20} {len(P.fields(t.schema))} settings"
            try:
                errors = t.read_config()[1]
            except OSError:
                errors = {"": "can't read the config file"}
            if errors:
                items.append((f"{text}, {len(errors)} problem(s) in the config file", "warn"))
            else:
                items.append(text)
        self.list.set_items(items)

    def show(self):
        self.refresh()

    def keys(self):
        return [("Enter", "open")] + self.list.keys()

    def render(self, canvas):
        canvas.text(0, 0, "Settings by tool. The config files stay the source of truth.", "dim")
        self.list.render(canvas.sub(2, 0, canvas.height - 2, canvas.width))

    def key(self, k):
        if k == "enter" and self.tools:
            tool = self.tools[self.list.index]
            app = self.ctx.app
            if self.core and tool is self.core[0]:
                core_ctx = self.core[1]
                self.ctx.push(FormScreen(core_ctx, pickers={"DEV_BRANCH": branch_picker(core_ctx)}))
            else:
                self.ctx.push(FormScreen(app.contexts[tool.name], app.plugins.get(tool.name)))
            return True
        return self.list.key(k)


class FormScreen:
    """One tool's settings: a SettingsForm saving with save_config(), a status
    line, `p` to preview the effect (the plugin's optional preview(ctx, values))
    and a discard question when leaving with unsaved edits."""

    def __init__(self, ctx, plugin=None, pickers=None):
        self.ctx, self.plugin = ctx, plugin
        tool = ctx.tool
        self.title = tool.name
        try:
            raw, self.file_problems = P.read_keyvalue(tool.file("CONFIG"))
        except FileNotFoundError:
            raw, self.file_problems = {}, []
        known = P.field_map(tool.schema)
        self.unknown = sorted(k for k in raw if k not in known)
        validate = getattr(plugin, "validate", None) if plugin is not None else None
        self.sform = SettingsForm(ctx, tool.schema, raw, lambda edits: save_config(ctx, edits),
                                  validate=validate if callable(validate) else None,
                                  pickers=pickers)

    @property
    def form(self):
        return self.sform.form

    @property
    def can_preview(self):
        return self.plugin is not None and callable(getattr(self.plugin, "preview", None))

    def keys(self):
        ks = self.sform.keys()
        if not self.sform.editing and self.can_preview:
            ks = ks[:1] + [("p", "preview")] + ks[1:]
        return ks

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

    def render(self, canvas):
        tool = self.ctx.tool
        more = canvas.symbols["more"]
        path = f"{tool.name}/{tool.manifest['CONFIG']}"
        canvas.text(0, 0, W.fit(path, canvas.width // 2, more), "dim")
        text, tone = self.status_line()
        room = canvas.width - len(path) - 3
        text = W.fit(text, max(0, room), more)
        canvas.text(0, canvas.width - len(text), text, tone, bold=tone == "warn")
        self.sform.render(canvas.sub(1, 0, canvas.height - 1, canvas.width))

    def can_leave(self):
        n = len(self.sform.changed())
        if not n:
            return True
        return self.ctx.confirm("Discard your changes?",
                                [f"{n} setting(s) were edited and not saved. The config file stays as it was."])
