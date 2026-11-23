# better-unas tool plugin contract

Status: contract version 1, built (issue #2; the core implements it as of v0.2.0). The design it serves is in
[tui-design.md](tui-design.md).

This is what every tool gives the `better-unas` TUI so the core can install
it, show its settings as forms, put it on the Home screen and load its own
screens, without the core knowing anything about the tool.

A tool is a folder with up to four contract pieces:

| Piece | File | Needed for |
|---|---|---|
| Manifest | `tool.conf` | Always. Name, summary, CLI, and where the other pieces are. |
| Settings schema | `settings.json` (any name, set by `SCHEMA`) | A Settings section and first-run setup. |
| Plugin module | `plugin.py` (any name, set by `PLUGIN`) | Home card, Alerts, its state in Settings, and the tool's own screens. |
| CLI | the tool's script (set by `CLI`) | `better-unas <tool> <command>` and the scheduled run. |

A minimal working example lives in [examples/space-watch/](examples/space-watch/),
and [smart-scheduler/settings.json](../smart-scheduler/settings.json)
maps every smart-scheduler setting onto the schema. `core/plugins.py` loads
and checks all of this; run it on a tool folder while you write one:

```bash
python3 core/plugins.py check docs/examples/space-watch
```

## Ground rules

- **Python 3 standard library only**, as everywhere in better-unas. The core
  runs on the firmware's Python (3.13 today); tools should not need anything
  newer than 3.8 for their tests to run on any machine.
- **The tool keeps working without the TUI.** The scheduled run and the CLI
  never import the core. The plugin module and schema are an extra layer the
  TUI reads; removing them must not break the tool.
- **The config file stays the source of truth** and stays hand-editable. The
  schema describes it; it never replaces it.
- **Reads are live and writes are guarded.** Anything that changes the tool's
  files takes the tool's lock (see `ctx.lock()`), the same one the scheduled
  run takes.

## 1. Manifest: `tool.conf`

Plain `KEY=VALUE`, so a tool's own POSIX sh scripts can read it with `sed`
as well.

Format: one `KEY=VALUE` per line, `#` starts a comment line, blank lines are
ignored, the value is everything after the first `=` with surrounding spaces
and one pair of matching quotes removed. Nothing is expanded or executed. If a
key repeats, the first one wins and the checker reports it.

| Key | Required | Meaning |
|---|---|---|
| `NAME` | yes | Tool name: lowercase letters, digits and `-`. Must equal the folder name. Used on the command line (`better-unas <NAME> ...`). |
| `VERSION` | yes | Informational only (#14): the suite version in the root `VERSION` file covers every tool, and updates compare that, never this. Still required by the checker; it may go in a later contract version. |
| `SUMMARY` | yes | One line, at most 60 characters, for the Tools screen and the setup picker. |
| `CHANGES` | yes | One line shown on the review screen before installing: what changes on the NAS. |
| `SETUP` | no | A step the user must do in the UniFi UI before the first install, or empty. |
| `CLI` | yes | The tool's command-line script, relative to the tool folder. |
| `CONTRACT` | with `SCHEMA` or `PLUGIN` | Contract version the tool was written for. `1` for this document. |
| `SCHEMA` | no | Settings schema file. Without it the tool has no Settings section. |
| `CONFIG` | with `SCHEMA` | The config file the schema describes, e.g. `config`. |
| `CONFIG_EXAMPLE` | no | The shipped example config. The checker requires it to list every schema key with the schema's default. |
| `PLUGIN` | no | Plugin module (data API and screens). Without it the tool gets a plain Home card (name, version, "no status available") and no screens. |
| `LOCK` | no | Lock file, relative to the tool folder. Default `.lock`. |

All paths are relative and must stay inside the tool folder. Unknown keys are
an error, so a typo doesn't silently do nothing; new keys come with a new
contract version.

smart-scheduler's `tool.conf` has a schema since #7:

```
CONTRACT=1
SCHEMA=settings.json
CONFIG=config
CONFIG_EXAMPLE=config.example
```

and `PLUGIN=plugin.py` since #10 (Home card, alerts and the Calendar screen).

## 2. Config file

The file `CONFIG` names, in the format smart-scheduler already uses: plain
`KEY=VALUE`, the same parsing rules as `tool.conf`, keys `A-Z0-9_` starting
with a letter. Every key is optional; a missing key means its default.

A tool's own loader reads it at run time. The TUI reads it through the schema
(`Tool.read_config()` in `core/plugins.py`), and writes it with a writer that
changes only the edited keys and keeps comments, order and unknown lines
(built in #7; the core's copy is `core/configfile.py`, the tool's own is
`smart-scheduler/settings.py`). An edited key that isn't in the file yet is
appended under its group's comment heading if the writer can find it,
otherwise at the end.

### Shared settings: notifications

Some settings belong to the whole suite, not to one tool. They live in
`better-unas.conf` in the home (the core's schema, `core/settings.json`), and
the user sets them once in Settings › Notifications:

| Key | Type | Meaning |
|---|---|---|
| `WEBHOOK_URL` | url | where alerts go; empty: the system log only |
| `WEBHOOK_FORMAT` | choice | `text`, `json`, `slack` or `discord` |
| `NOTIFY_SUCCESS` | bool | also send good news, e.g. a passed test |
| `ALERT_REPEAT_HOURS` | int | send a still-active alert again after this long |
| `HEARTBEAT_URL` | url | pinged each time a tool's scheduled check ends without errors |
| `HEARTBEAT_FAIL_URL` | url | pinged instead when a tool's scheduled check ends with errors |

A tool may offer its own heartbeat pair in its schema (smart-scheduler and
health-monitor use `OWN_HEARTBEAT_URL` and `OWN_HEARTBEAT_FAIL_URL`). When
its own URL is set the tool pings its own pair, whole, instead of the shared
one, so each tool can have a check with its own period.

A tool doesn't import the core, so it reads these from
`<home>/better-unas.conf` itself (a plain `KEY=VALUE` file, the folder above
the tool's): a key set there wins, a key that isn't falls back to the same
key in the tool's own config, then to the default above. A tool's schema
doesn't list them. The core never calls one of them an unknown setting in a
tool's config (`SHARED_KEYS` in `core/plugins.py`), so an old config that
still has them keeps loading. The core copies them from the tools' configs
into `better-unas.conf` once, on the first start or update that knows about
`better-unas.conf`, and in first-run setup (`core/notify.py`, `migrate`); a
key already in `better-unas.conf` is never copied again. smart-scheduler's
`Config` and space-watch's `read_config` show the pattern.

## 3. Settings schema

A JSON file. JSON rather than Python so the core can check it and build the
setup forms from a downloaded release without running any tool code.

```json
{
  "contract": 1,
  "groups": [
    {
      "id": "timing",
      "title": "When tests may start",
      "help": "Optional paragraph shown above the group.",
      "fields": [
        {
          "key": "START_WINDOW",
          "type": "time_window",
          "label": "Start window",
          "default": "00:00-05:00",
          "essential": true,
          "help": "Local time (the NAS's timezone). May cross midnight, e.g. 22:00-04:00."
        }
      ]
    }
  ]
}
```

### Groups

Groups are the sections of the Settings screen, in file order, and should
follow the `# ---` sections of `config.example`.

| Key | Required | Meaning |
|---|---|---|
| `id` | yes | Lowercase letters, digits and `-`; unique in the schema. |
| `title` | yes | Section heading. |
| `help` | no | Text shown under the heading, on one line (at most 75 characters). |
| `fields` | yes | Non-empty list of fields, in display order. |

### Fields: common keys

| Key | Required | Meaning |
|---|---|---|
| `key` | yes | The config key, e.g. `INTERVAL_DAYS`. Unique across the whole schema. |
| `type` | yes | One of the types below. |
| `label` | yes | Short name shown in the form, at most 28 characters (the label column at 80 columns). |
| `default` | yes | The default **as the text written in the config file**, e.g. `"90"`, `"yes"`, `"5,187,197,198"`, `""`. It must pass the field's own validation. |
| `help` | no | Shown under the form when the field is selected, and in the setup wizard. At most three lines at 76 columns; it says what the setting controls, what changing it does and when, and how to undo it if that isn't obvious (docs/tui-style.md §8). `core/tests/test_wording.py` checks the length and the glossary. |
| `essential` | no | `true` puts the field in first-run setup (step 2). Keep it to the few settings someone must look at before the first run. Default `false`. |
| `on_change` | no | `"install"`: a save that changes this field also runs the tool's `install.sh`, after the config is written, so the change takes effect without another step. The save asks first and says so. `install.sh` must then do the right thing for the config it finds (smart-scheduler's removes its timer when `ENABLED=no` and puts it back when yes). Help text never tells the user to reinstall by hand. |

Keys a type doesn't list are an error, again to catch typos.

### Field types

The value column is what `parse_value()` returns to Python code; the TUI's form
widget (#4) edits the text form and validates as you type.

| Type | Config text | Value | Extra keys | Widget |
|---|---|---|---|---|
| `int` | `90` | `int` | `min`, `max`, `unit` | number field, `unit` shown after it |
| `float` | `1.5` | `float` | `min`, `max`, `unit` | number field |
| `bool` | `yes` / `no` | `bool` | | toggle. Reads `yes/true/on/1` and `no/false/off/0` (any case); writes `yes`/`no`. |
| `choice` | `slack` | `str` | `choices` (list of strings) | option list |
| `time_window` | `22:00-04:00` | `(datetime.time, datetime.time)` | | two `HH:MM` fields. 24-hour, zero-padded; may cross midnight; start and end must differ. |
| `text` | `admin` | `str` | `pattern`, `max_length`, `required`, `secret` | text field. `pattern` is a Python regex matched against the whole value. |
| `url` | `https://ntfy.sh/x` | `str` | `schemes` (default `["http","https"]`), `required`, `secret` | text field, plus "send test" where the tool offers it (Alerts screen, #13) |
| `serial_list` | `ZL1,ZL2` | `list[str]` | `required` | list editor; picks from the NAS's drives where a screen provides them |
| `int_list` | `5,187,197` | `list[int]` | `min`, `max` (per item), `required` | text field |
| `serial_map` | `ZL1:30,ZL2:120` | `dict[str, int]` | `min`, `max`, `unit` (per value), `required` | two-column list editor (serial, number) |
| `checklist` | `5,187,197` | `list[str]`, or `list[int]` with `min`/`max` | `options` (list of `{"value", "label"}`), `other`, `prefix`, `min`, `max` (per item), `required` | list of options to tick, plus an `other` row for values the options don't name. Ticked options come first in schema order, then other values. Without `other`, only the options are allowed. `prefix` goes before every value shown (`S` shows `197` as `S197`). |

Rules that apply to several types:

- **Empty values.** For `int`, `float`, `bool`, `choice` and `time_window` an
  empty value is invalid (remove the line to get the default). For `text`,
  `url`, `serial_list`, `int_list`, `serial_map` and `checklist` empty is valid and means
  "none", unless the field says `"required": true`.
- **Lists** are comma-separated; spaces around items are ignored and are not
  written back. Duplicates are an error. Serials match `[A-Za-z0-9._-]+`.
- **`secret: true`** (text, url) masks the value in the form except while
  editing, and the core never logs it or shows it on the review screen.
  Webhook and heartbeat URLs usually carry a token, so mark them.
- **Messages.** Validation errors are short, lower-case phrases meant to sit
  next to the field: `value must be at most 99`, `want HH:MM-HH:MM, e.g.
  22:00-04:00`, `ZL1 listed twice`.

### Validation and bad values

`validate_config(schema, raw)` returns `(typed, errors)`. `typed` has every
schema key; a bad value falls back to its default so a tool keeps running on a
broken line, which is what smart-scheduler's `Config` does today. `errors` maps
the key to its message; keys the schema doesn't know get `unknown setting`.

The Settings screen refuses to save while any field has an error. Rules that
involve more than one field go in the plugin's optional `validate(values)`
(section 4).

Saving (#12, `core/tui/settings.py`): `s` checks every field against the
schema and `validate(values)`, then writes only the changed keys through
`core/configfile.py` while holding the tool's lock (`ctx.lock()`). If the
scheduled run keeps the lock past the timeout, nothing is written and the
edits stay on screen. In a dry run (`--dry-run`) the edits are checked and
nothing is written. Leaving with unsaved edits asks whether to discard them.
First-run setup reuses the same form (`SettingsForm`, with
`schema_subset(schema, essential=True)`).

## 4. Plugin module

A Python file the core imports by path under a private module name
(`better_unas_tool_<name>`). The tool folder is **not** put on `sys.path`, so
two tools' helpers can't shadow each other; the module loads its siblings by
path (see `examples/space-watch/plugin.py`). An import error or a missing
function marks the tool as broken on the Tools screen and leaves the rest of
the TUI working.

```python
CONTRACT = 1

def status(ctx): ...      # required
def summary(ctx): ...     # required
def alerts(ctx): ...      # required
def validate(values): ... # optional
def screens(ctx): ...     # optional
def send_test(ctx, key, values): ...  # optional
def preview(ctx, values): ...  # optional
```

Every function except `validate` gets the context described in section 6. They must not write
anything or take the lock; changes happen only from screens, in response to a
key, after a confirm. If one raises, the core catches it and shows the tool's
card in the error state with the exception text, so a tool never needs a
blanket `try`.

### `status(ctx)`: is the tool installed and healthy

Cheap: local files only (config, state, trigger files), no network, no
`smartctl`, under a second. Used for the Tools screen, the Home card's badge
and `better-unas status`.

```python
{
  "enabled": True,            # the tool's own off switch (e.g. ENABLED=yes)
  "state": "ok",              # ok | warn | error | off | unknown
  "problems": ["Timer is missing; the next run restores it."],
  "detail": ["Last run 01:00, took 3 s"],   # optional extra lines
}
```

### `summary(ctx)`: the Home card

May do live reads (Drive API, smartctl); the core calls it with a spinner and
caches the result until the Home screen is opened again or refreshed.

```python
{
  "state": "warn",                                 # as in status
  "headline": "7 drives scheduled, 1 overdue",     # at most 60 characters
  "items": [                                       # at most 6 rows
    {"label": "Next test", "value": "Sat 23 Nov, Drive 3", "tone": "warn",
     "status": "planned"},
    {"label": "Last result", "value": "passed, Drive 5, Tue 19 Nov", "tone": "good",
     "status": "ok", "target": "Drives:<serial>", "target_label": "Drive 5"},
    {"label": "Next scrub", "value": "Sun 30 Nov 02:30", "status": "scrub"},
  ],
}
```

`tone` is one of the theme tones in section 7. Labels should be at most 16
characters so the card lines up.

Optional item keys:

- `status`: one of `ok failed warning running planned scrub backup update`.
  The Home card draws that status glyph before the value.
- `target`: where Enter on this row goes, `"Tab"` or `"Tab:arg"` (`tab` is
  the older name and still works). With an argument, the core switches to
  the tab, goes back to its root screen and calls that screen's `open(arg)`
  (section 5); smart-scheduler uses `"Drives:<serial>"` to open one drive.
- `target_label`: the short hint shown dim at the row's right (`› Drive 5`);
  the tab name by default.

**One stop per destination** (docs/tui-style.md §5). The card itself is one
stop, leading to the tool's first own tab (or its page in Settings, for a tool
without screens). An item is a stop of its own only when its target differs
from the card's and from every item above it; set `target` only for that.
The dashboard shows the card under the suite's update line and the active
alerts, so items are where a tool puts its next planned test, last results,
next scrub and next backup window.

### `alerts(ctx)`: active and recent alerts

```python
[
  {
    "id": "test:ZL2ABC",       # stable; the same problem keeps the same id
    "level": "error",          # error | warn | info
    "message": "Extended test on Drive 3 (ZL2ABC) failed: read failure.",
    "active": True,            # False for recently resolved ones
    "since": "2026-11-20T01:04:00-07:00",   # ISO 8601 with offset
    "last": "2026-11-21T01:00:00-07:00",    # optional: last time it was raised
    "resolved": None,          # optional: when it cleared, for active False ones
  },
]
```

The Alerts screen merges every tool's list, active first, newest first, and
shows `resolved` for the resolved ones. Home shows the count of active ones.
smart-scheduler's `state["alerts"]` entries map straight across: key to `id`,
`msg` to `message`, `sent` to `last`, and its level `warning` to `warn`. Its
`state["resolved"]` list (the last 20 cleared alerts, oldest first, each
`{key, msg, level, since, sent, resolved}`) gives the recent ones with
`active: False`; skip an entry whose key is active again.

### `validate(values)`: cross-field rules (optional)

Gets the typed values (as `validate_config` returns them, with the user's
unsaved edits applied) and returns `{KEY: message}` for fields that break a
rule spanning several settings, e.g. `{"WARN_INTERVAL_DAYS": "should be
shorter than the healthy interval"}`. Empty dict when fine. No context: it must
be pure.

### `send_test(ctx, key, values)`: "send test" for a URL setting (optional)

A tool's Settings page offers `t send test` on each of its own `url` fields
when the plugin has this function. The core then calls
`send_test(ctx, key, values)` through `ctx.run_slow`, after a confirm:

- `key` is the `url` field's key.
- `values` are the typed settings with the user's unsaved edits applied, so
  the test uses the URL and format on screen, not the saved ones.
- Return a short success text (`"test alert sent (slack format)"`), shown as
  a message in green. Raise an exception whose message says what went wrong
  (`"HTTP 404 Not Found"`, `"timed out"`); the core shows it in red without
  the exception type. Return `None` for a key that has no test; the core says so.

It must send exactly what the tool's own sender sends, so the user tests the
real format. It writes nothing locally and takes no lock. Without the
function there's no `t`. The shared webhook and heartbeat aren't a tool's
(see "Shared settings"): Settings › Notifications tests those with
`core/notify.py`, whose payloads match smart-scheduler's.

### `preview(ctx, values)`: show the effect of unsaved settings (optional)

`p` on the Settings screen calls it with the typed settings, the user's
unsaved edits applied (every field must be valid first), and pushes the screen
it returns; Esc comes back to the form with the edits intact. Return `None`
to show nothing. The screen must read settings only from `values` (or a
context whose `config()` returns them) and write nothing. Without the
function there is no `p`. smart-scheduler returns its Calendar planned with
the values (`preview_calendar`).

### `screens(ctx)`: the tool's own screens (optional)

```python
def screens(ctx):
    return [
        {"id": "calendar", "tab": "Calendar", "make": CalendarScreen},
        {"id": "drives",   "tab": "Drives",   "make": DrivesScreen},
    ]
```

- `id`: lowercase, unique within the tool.
- `tab`: the tab title. The core's own tabs are Home, Alerts and Settings; a
  tool can't use those. Tool tabs go between Home and Alerts, in the order
  tools are listed, then the order returned here. So with smart-scheduler
  installed the bar reads Home, Calendar, Drives, Alerts, Settings, as in
  [tui-style.md](tui-style.md) §6. If two tools return the same tab title, that
  tab opens a short chooser listing both.
- `make(ctx)`: called once per TUI session to build the screen object.

## 5. Screen objects

```python
class DrivesScreen:
    title = "Drives"                       # shown in the screen's header line

    def __init__(self, ctx): ...

    def keys(self):                        # footer hints, besides the core's own
        return [("Enter", "open"), ("t", "run test"), ("z", "snooze")]

    def show(self): ...                    # each time the screen becomes visible

    def render(self, canvas): ...          # draw everything; called after each key and resize

    def key(self, k):                      # return True if handled
        ...

    def can_leave(self):                   # optional: False keeps the user here
        return True

    def open(self, arg):                   # optional: a Home item's "Tab:arg" target
        ...
```

- **`open(arg)`** (optional) is called on a tab's root screen, after `show()`,
  when a Home summary item with target `"Tab:arg"` is chosen. It should go
  straight to what `arg` names (push a detail screen, move the cursor) and say
  so with `ctx.message` if it's gone.

- **`show()`** is where reads happen (via `ctx.run_slow`), so the calendar is
  recalculated every time it opens. `render()` must only draw from what
  `show()` or `key()` loaded; it is called often.
- **`render(canvas)`** gets a canvas the size of the area between the nav box
  and the action box, already cleared. It draws inside it; anything outside is
  clipped. It must cope with any size from 80x20 up (the core shows "please
  widen" below 80x28 and never calls `render`).
- **`key(k)`** gets one key name: a printable character (`"t"`, `"Z"`, `"?"`)
  or one of `up down left right enter esc tab backtab pgup pgdn home end
  backspace delete f1`...`f12`. Return `True` if the screen used it.
- **Keys the core keeps.** `1`-`9` and `tab`/`backtab` switch tabs, `q` quits
  and `?` shows help. A screen sees these keys first only while a core field
  it opened is being edited (the core knows, because the field registers
  itself while editing). Everything else, `esc` included, goes to the screen
  first; if it returns `False`, the core may use it. For `esc` that means going
  back: it pops a pushed screen, and on a tab's root screen does nothing. (A
  screen gets `esc` first so the Settings screen, a tab's root, can offer to
  discard, and so a screen can close something of its own before going back.)
- **`can_leave()`** (optional) is called before the core takes the user away
  from the screen: switching tabs, `esc` popping it, or `q`. Return `False` to
  stay. It may ask first (`return self.ctx.confirm("Discard your changes?")`).
  Without it, leaving is always allowed.
- A screen opens a sub-screen (a drive's details, a day's details) with
  `ctx.push(screen)`; the pushed object follows the same interface.

## 6. Context

One context object per tool, built by the core and passed to every plugin
function and screen. In tests, a small fake stands in for it (see
`core/tests/test_plugins.py`).

| Member | What it does |
|---|---|
| `ctx.tool` | The loaded tool: `.name`, `.path` (absolute folder), `.manifest` (dict), `.schema`, `.file(key)` (absolute path of a manifest path key, e.g. `ctx.tool.file("CONFIG")`). |
| `ctx.config()` | Typed settings: `{KEY: value}` for every schema key. In a Settings preview (the calendar showing the effect before you save) this returns the unsaved values, so screens and functions must read settings only through it, never from the file. |
| `ctx.config_errors()` | `{KEY: message}` for the current config file. |
| `ctx.now()` | Current time, timezone-aware, in the NAS's local zone. Use it instead of `datetime.now()` so tests and previews can fix the clock. |
| `ctx.dry_run` | `True` when the TUI runs with `--dry-run`: screens must not change anything and should say what they would do through `ctx.message`. |
| `ctx.run_slow(fn, label)` | Calls `fn()` with a spinner and `label` showing, returns its result or re-raises its exception. For any read that can take more than a moment. |
| `ctx.lock(timeout=10)` | Context manager taking the tool's lock (`LOCK`, an exclusive `flock`) for a save or action. Waits up to `timeout` seconds showing "waiting for the scheduled run", then raises `LockBusy`, which the core turns into a message if the screen doesn't catch it. |
| `ctx.confirm(title, lines=(), second=None)` | Modal yes/no dialog; returns `True` for yes. Default answer is no. With `second="Start anyway?"`, a yes is followed by a second confirm with that question, for overrides (a busy drive, an upcoming scrub). |
| `ctx.message(text, tone="normal")` | A toast in the footer until the next key. |
| `ctx.push(screen)` / `ctx.pop()` | Screen stack within the current tab. |
| `ctx.theme` | The theme (section 7). Screens usually just pass tone names to the canvas. |
| `ctx.log(text)` | Appends to the TUI's own log (not the journal) for debugging. Never pass secrets. |
| `ctx.LockBusy` | The exception `ctx.lock()` raises, so a screen can catch it without importing the core. |
| `ctx.widgets` | The core's widget module (`core/tui/widgets.py`): `ListView`, `Table`, `ScrollText`, `Form`, `panel`, `KeyValue` and the rest, so a plugin uses them without relying on `sys.path`. A `Table` cell is a string, a `(text, tone)` pair, or `("status", kind, text[, aside])` for a status glyph and text in the kind's tone with a dim aside after it (kind `None`: plain text, no glyph). |

The core implements this context (#4). Functions beyond this list are the
core's business and may change; a tool that needs more should ask for it in a
contract change.

## 7. Canvas and theme

The canvas is a rectangle of character cells. The curses one and the headless
one used in tests (#4) have the same methods:

| Method | What it does |
|---|---|
| `canvas.width`, `canvas.height` | Size in cells. |
| `canvas.text(y, x, s, tone="normal", bold=False)` | Draws `s` at row `y`, column `x`, clipped at the edges. No wrapping. |
| `canvas.fill(y, x, h, w, ch=" ", tone="normal")` | Fills a rectangle. |
| `canvas.box(y, x, h, w, title="")` | Draws a line box with an optional title. |
| `canvas.sub(y, x, h, w)` | A canvas for a region of this one, with its own origin and clipping. |
| `canvas.styled(y, x, s, role="value")` | Draws `s` in a text role: `heading`, `label`, `value`, `strong`, `note` (with `↳`), `reason` (with `!`), `tag`, `id`, `chip`. Returns the column after it. Prefer this to picking tones. |
| `canvas.status(y, x, kind, s="")` | A status glyph plus text in its tone: `ok` ✓, `failed` ✗, `warning` !, `running` ▶, `planned` ○, `scrub` ↻, `backup` ⇡, `update` ▲. ASCII terminals get `+ x ! > o s b ^`. |
| `canvas.symbols` | Box, arrow and status characters, Unicode or ASCII; also `deg` (`°`, nothing in ASCII) and `mid` (`·`, `-` in ASCII) for text. |

The body canvas a screen gets already has a 1-column margin on each side, so
draw from column 0. The roles, glyphs and layout rules are in
[tui-style.md](tui-style.md); `ctx.widgets` has `heading`, `panel` and
`KeyValue` for blocks laid out the way it describes.

Tones, from the design's color rules:

| Tone | Meaning | Color (when colors are on) |
|---|---|---|
| `normal` | ordinary text | terminal default |
| `good` | good, done, passed | green |
| `warn` | planned, warning | yellow |
| `bad` | failed, alert | red |
| `dim` | past, disabled, secondary | dim |

The core also has `selected` and `accent` for its own widgets. The terminal's
own background is always used, and with `NO_COLOR` set every tone draws as
plain text (with `bad` and `warn` bold), so a screen must never carry meaning
in color alone: say "failed", not just a red mark.

Screens draw with `text` and the core's widgets (list, table, form, from #4,
reached through `ctx.widgets`),
which take a canvas the same way.

## 8. CLI

`better-unas <tool> <args...>` runs `python3 <tool folder>/<CLI> <args...>`
with the user's terminal, and its exit code is passed through.

Every tool's CLI supports:

| Command | Meaning |
|---|---|
| `status` | What the tool is doing, in plain text. Read-only, no lock. |
| `check` | Self-check: config, own files, triggers, required firmware tools. Read-only. |

and, where they make sense, `run` (the scheduled job; `--dry-run` shows what it
would do) and `heal` (restore its own triggers). Exit codes: `0` fine, `1` a
problem was found or the action failed, `2` bad usage or not root.

`better-unas status` (#16) prints each installed tool's `status()` and
`summary()` without starting the TUI, falling back to the tool's own `status`
command for a tool without a plugin.

## 9. Checking a tool

`core/plugins.py` is the reference implementation of everything above that
doesn't need a terminal:

| Function | Use |
|---|---|
| `load_tool(path)` | Manifest + schema + `config.example` check; raises `ContractError` with `.problems` listing everything wrong. |
| `discover(home)` | Every tool under `/persistent/better-unas`: `(tools, {folder: problems})`. |
| `load_plugin(path, manifest)` | Imports the plugin module and checks its functions. |
| `parse_value(field, text)` / `format_value(field, value)` | One field, text to value and back. Raises `FieldError`. |
| `validate_config(schema, raw)` | A whole config: `(typed, errors)`. |
| `check_summary`, `check_alerts`, `check_status`, `check_screens` | Check a plugin's return values; for tools' own tests. |
| `fields`, `essential_fields`, `field_map` | Walk a schema. |

`python3 core/plugins.py check <tool folder>...` prints every problem and
exits 1 if any tool fails. Each tool's tests should include the same check
(see `core/tests/test_plugins.py` for how the example does it).

## 10. Worked example: smart-scheduler's settings

[smart-scheduler/settings.json](../smart-scheduler/settings.json)
covers every key in `smart-scheduler/config.example`, grouped like it, and the
tests check it against that file (keys and defaults). It started here as a
worked example and moved into the tool in #7, where `smart-scheduler/settings.py`
also drives the tool's own config loader and writer from it. Summary:

| Group | Key | Type | Constraints | Essential |
|---|---|---|---|---|
| General | `ENABLED` | bool | | |
| | `DRIVE_USER` | text | pattern `[A-Za-z0-9._@-]+`, may be empty | |
| How often | `INTERVAL_DAYS` | int | 1-3650 days | yes |
| | `WARN_INTERVAL_DAYS` | int | 1-3650 days | yes |
| | `OLD_INTERVAL_DAYS` | int | 1-3650 days | yes |
| | `OLD_DRIVE_HOURS` | int | min 0, power-on hours | |
| | `RETRY_FAILED_DAYS` | int | 1-365 days | |
| | `WARN_ATTRIBUTES` | checklist | S5, S187, S197, S198, S199 named; other IDs 1-255 | |
| | `INTERVAL_OVERRIDES` | serial_map | values 1-3650 days | |
| | `EXCLUDE_SERIALS` | serial_list | | |
| When tests may start | `START_WINDOW` | time_window | | yes |
| | `MAX_CONCURRENT` | int | 1-64 | |
| | `MAX_STARTS_PER_NIGHT` | int | 0-64 (0 pauses) | |
| | `SCRUB_MARGIN_HOURS` | int | 0-168 hours | |
| | `DURATION_FACTOR` | float | 1-10 | |
| | `BUSY_KBPS` | int | min 0 (0 = off) | |
| Backups | `BACKUP_AVOID` | bool | | |
| | `BACKUP_MARGIN_HOURS` | int | 0-168 hours | |
| | `BACKUP_DEFAULT_HOURS` | float | 0.1-168 hours | |
| | `BACKUP_UPLOAD_MBPS` | int | min 0 Mbit/s (0 = unknown) | |

Its notifications (webhook, heartbeat, alert repeat) are shared settings in
`better-unas.conf` since step 3 of docs/tui-style.md; the core's
`settings.json` holds them, with `WEBHOOK_URL` and `WEBHOOK_FORMAT` as its
essential fields. Between them, the two schemas use every field type.

Notes for #7:

- The ranges are proposals and are **stricter than today's loader**, which
  accepts any non-negative whole number. With the schema, `INTERVAL_DAYS=0` or
  `MAX_CONCURRENT=0` becomes an error that falls back to the default. #7
  should keep the loader and the schema agreeing.
- A cross-field rule for `validate()`: `WARN_INTERVAL_DAYS` and
  `OLD_INTERVAL_DAYS` no longer than `INTERVAL_DAYS`.
- `ENABLED=no` has side effects on the next run (triggers removed). The
  Settings screen should say so when it is turned off, through the field's help.

## Where each part was built

- **#4** (`core/tui/`) implements the context, canvas (curses and headless), widgets and
  the tab placement rules above, and loads tools with `discover()`.
- **#7** adds smart-scheduler's `settings.json` and the comment-keeping
  config writer (`smart-scheduler/settings.py`: `save(path, edits, schema)`),
  and shares validation between the tool's `Config` loader and the writer
  (see open question 2). smart-scheduler's `plugin.py` came in #10 and #11.
- **#12** renders schemas as forms, calls `validate()` and `preview()`, and
  saves through `core/configfile.py`; **#15** uses
  `essential` fields and `CHANGES` for first-run setup.

## Open questions

1. **`VERSION` with one suite version.** Decided in #14: one suite version
   in the root `VERSION` file (e.g. `v0.2.0`) covers better-unas and every
   tool, and updates go by it (`core/updates.py`). A `tool.conf` `VERSION` is
   informational; now that the old root `install.sh` is gone, it can be
   dropped in a later contract version.
2. **Sharing the validator with the scheduled run.** Decided in #7 for
   smart-scheduler: the tool keeps a copy of the value rules
   (`smart-scheduler/settings.py`) so the hourly run never imports the core,
   and its tests check that copy against `core/plugins.py` field by field and
   value by value, so the two can't drift. Inside the tool, the loader and the
   writer share it and take every rule from `settings.json`. The schema is
   therefore part of smart-scheduler itself, not an optional layer: it is in
   the install manifest and the tool won't start without it.
3. **Conditional fields.** Some settings only matter when another is set
   (`WEBHOOK_FORMAT` without `WEBHOOK_URL`). Version 1 has no `show_if`; the
   form shows every field. Add it if the Settings screen feels cluttered.
4. **Two tools on one tab.** The chooser rule (section 4) is a guess; nothing
   needs it until health-monitor exists, which may want Drives too. Merging
   sections into one Drives screen might be nicer.
