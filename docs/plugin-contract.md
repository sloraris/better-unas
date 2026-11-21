# better-unas tool plugin contract

Status: contract version 1, for review (issue #2). The design it serves is in
[tui-design.md](tui-design.md).

This is what every tool gives the `better-unas` TUI so the core can install
it, show its settings as forms, put it on the Home screen and load its own
screens, without the core knowing anything about the tool.

A tool is a folder with up to four contract pieces:

| Piece | File | Needed for |
|---|---|---|
| Manifest | `tool.conf` | Always. Name, summary, CLI, and where the other pieces are. |
| Settings schema | `settings.json` (any name, set by `SCHEMA`) | A Settings section and first-run setup. |
| Plugin module | `plugin.py` (any name, set by `PLUGIN`) | Home card, Alerts, Tools status, and the tool's own screens. |
| CLI | the tool's script (set by `CLI`) | `better-unas <tool> <command>` and the scheduled run. |

A minimal working example lives in [examples/space-watch/](examples/space-watch/),
and [examples/smart-scheduler.settings.json](examples/smart-scheduler.settings.json)
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

The existing `tool.conf`, extended. It stays plain `KEY=VALUE` so the POSIX sh
installer can keep reading it with `sed` until the TUI replaces it.

Format: one `KEY=VALUE` per line, `#` starts a comment line, blank lines are
ignored, the value is everything after the first `=` with surrounding spaces
and one pair of matching quotes removed. Nothing is expanded or executed. If a
key repeats, the first one wins (as `conf_get` in `install.sh` does) and the
checker reports it.

| Key | Required | Meaning |
|---|---|---|
| `NAME` | yes | Tool name: lowercase letters, digits and `-`. Must equal the folder name. Used on the command line (`better-unas <NAME> ...`). |
| `VERSION` | yes | The tool's version. See open question 1 (one suite version). |
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

smart-scheduler's `tool.conf` today is a valid version-0 manifest (no schema,
no plugin). Issue #7 adds:

```
CONTRACT=1
SCHEMA=settings.json
CONFIG=config
CONFIG_EXAMPLE=config.example
PLUGIN=plugin.py
```

## 2. Config file

The file `CONFIG` names, in the format smart-scheduler already uses: plain
`KEY=VALUE`, the same parsing rules as `tool.conf`, keys `A-Z0-9_` starting
with a letter. Every key is optional; a missing key means its default.

A tool's own loader reads it at run time. The TUI reads it through the schema
(`Tool.read_config()` in `core/plugins.py`), and writes it with a writer that
changes only the edited keys and keeps comments, order and unknown lines
(built in #7). An edited key that isn't in the file yet is appended under its
group's comment heading if the writer can find it, otherwise at the end.

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
| `help` | no | Text shown under the heading. |
| `fields` | yes | Non-empty list of fields, in display order. |

### Fields: common keys

| Key | Required | Meaning |
|---|---|---|
| `key` | yes | The config key, e.g. `INTERVAL_DAYS`. Unique across the whole schema. |
| `type` | yes | One of the types below. |
| `label` | yes | Short name shown in the form, at most 28 characters (the label column at 80 columns). |
| `default` | yes | The default **as the text written in the config file**, e.g. `"90"`, `"yes"`, `"5,187,197,198"`, `""`. It must pass the field's own validation. |
| `help` | no | One or two sentences shown under the field when it is selected and in the setup wizard. Usually the `config.example` comment, tightened. |
| `essential` | no | `true` puts the field in first-run setup (step 2). Keep it to the few settings someone must look at before the first run. Default `false`. |

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

Rules that apply to several types:

- **Empty values.** For `int`, `float`, `bool`, `choice` and `time_window` an
  empty value is invalid (remove the line to get the default). For `text`,
  `url`, `serial_list`, `int_list` and `serial_map` empty is valid and means
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
    {"label": "Next test", "value": "Sat 23 Nov, slot 3 (ZL2ABC)", "tone": "warn"},
    {"label": "Last result", "value": "Passed, slot 5, Tue 19 Nov", "tone": "good"},
    {"label": "Next scrub", "value": "Sun 30 Nov 02:30", "tone": "normal"},
  ],
}
```

`tone` is one of the theme tones in section 7. Labels should be at most 16
characters so the card lines up.

### `alerts(ctx)`: active and recent alerts

```python
[
  {
    "id": "test:ZL2ABC",       # stable; the same problem keeps the same id
    "level": "error",          # error | warn | info
    "message": "Extended test on slot 3 (ZL2ABC) failed: read failure.",
    "active": True,            # False for recently resolved ones
    "since": "2026-11-20T01:04:00-07:00",   # ISO 8601 with offset
    "last": "2026-11-21T01:00:00-07:00",    # optional: last time it was raised
  },
]
```

The Alerts screen merges every tool's list, active first, newest first. Home
shows the count of active ones. smart-scheduler's `state["alerts"]` entries map
straight across: key to `id`, `msg` to `message`, `sent` to `last`.

### `validate(values)`: cross-field rules (optional)

Gets the typed values (as `validate_config` returns them, with the user's
unsaved edits applied) and returns `{KEY: message}` for fields that break a
rule spanning several settings, e.g. `{"WARN_INTERVAL_DAYS": "should be
shorter than the healthy interval"}`. Empty dict when fine. No context: it must
be pure.

### `screens(ctx)`: the tool's own screens (optional)

```python
def screens(ctx):
    return [
        {"id": "calendar", "tab": "Calendar", "make": CalendarScreen},
        {"id": "drives",   "tab": "Drives",   "make": DrivesScreen},
    ]
```

- `id`: lowercase, unique within the tool.
- `tab`: the tab title. The core's own tabs are Home, Settings, Alerts and
  Tools; a tool can't use those. Tool tabs go between Home and Settings, in
  the order tools are listed, then the order returned here. So with
  smart-scheduler installed the bar reads Home, Calendar, Drives, Settings,
  Alerts, Tools, as in the design. If two tools return the same tab title, that
  tab opens a short chooser listing both.
- `make(ctx)`: called once per TUI session to build the screen object.

## 5. Screen objects

```python
class DrivesScreen:
    title = "Drives"                       # shown in the screen's header line

    def __init__(self, ctx): ...

    def keys(self):                        # footer hints, besides the core's own
        return [("Enter", "details"), ("t", "run test"), ("z", "snooze")]

    def show(self): ...                    # each time the screen becomes visible

    def render(self, canvas): ...          # draw everything; called after each key and resize

    def key(self, k):                      # return True if handled
        ...
```

- **`show()`** is where reads happen (via `ctx.run_slow`), so the calendar is
  recalculated every time it opens. `render()` must only draw from what
  `show()` or `key()` loaded; it is called often.
- **`render(canvas)`** gets a canvas the size of the area between the tab bar
  and the footer, already cleared. It draws inside it; anything outside is
  clipped. It must cope with any size from 80x20 up (the core shows "please
  widen" below 80x24 and never calls `render`).
- **`key(k)`** gets one key name: a printable character (`"t"`, `"Z"`, `"?"`)
  or one of `up down left right enter esc tab backtab pgup pgdn home end
  backspace delete f1`...`f12`. Return `True` if the screen used it.
- **Keys the core keeps.** `1`-`9` and `tab`/`backtab` switch tabs, `q` quits,
  `?` shows help, and `esc` goes back (pops a pushed screen; on a tab's root
  screen it does nothing). A screen sees these keys first only while a core
  text field it opened is being edited. Everything else goes to the screen
  first; if it returns `False`, the core may use it.
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

Screens draw with `text` and the core's widgets (list, table, form, from #4),
which take a canvas the same way.

## 8. CLI

`better-unas <tool> <args...>` runs `python3 <tool folder>/<CLI> <args...>`
with the user's terminal, and its exit code is passed through. This is how the
installer behaves today and it stays the same.

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

[examples/smart-scheduler.settings.json](examples/smart-scheduler.settings.json)
covers every key in `smart-scheduler/config.example`, grouped like it, and the
tests check it against that file (keys and defaults). #7 moves it into the
tool. Summary:

| Group | Key | Type | Constraints | Essential |
|---|---|---|---|---|
| General | `ENABLED` | bool | | |
| | `DRIVE_USER` | text | pattern `[A-Za-z0-9._@-]+`, may be empty | |
| How often | `INTERVAL_DAYS` | int | 1-3650 days | yes |
| | `WARN_INTERVAL_DAYS` | int | 1-3650 days | yes |
| | `OLD_INTERVAL_DAYS` | int | 1-3650 days | yes |
| | `OLD_DRIVE_HOURS` | int | min 0, power-on hours | |
| | `RETRY_FAILED_DAYS` | int | 1-365 days | |
| | `WARN_ATTRIBUTES` | int_list | items 1-255 | |
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
| Notifications | `WEBHOOK_URL` | url | secret, may be empty | yes |
| | `WEBHOOK_FORMAT` | choice | text, json, slack, discord | yes |
| | `NOTIFY_SUCCESS` | bool | | |
| | `ALERT_REPEAT_HOURS` | int | min 1 hour | |
| | `HEARTBEAT_URL` | url | secret, may be empty | |
| | `HEARTBEAT_FAIL_URL` | url | secret, may be empty | |

Notes for #7:

- The ranges are proposals and are **stricter than today's loader**, which
  accepts any non-negative whole number. With the schema, `INTERVAL_DAYS=0` or
  `MAX_CONCURRENT=0` becomes an error that falls back to the default. #7
  should keep the loader and the schema agreeing.
- A cross-field rule for `validate()`: `WARN_INTERVAL_DAYS` and
  `OLD_INTERVAL_DAYS` no longer than `INTERVAL_DAYS`.
- `ENABLED=no` has side effects on the next run (triggers removed). The
  Settings screen should say so when it is turned off, through the field's help.

## What this leaves to other issues

- **#4** implements the context, canvas (curses and headless), widgets and
  the tab placement rules above, and loads tools with `discover()`.
- **#7** adds smart-scheduler's `settings.json`, `plugin.py` and the
  comment-keeping config writer, and decides whether the tool's own `Config`
  loader shares validation with `core/plugins.py` (see open question 2).
- **#12** renders schemas as forms and calls `validate()`; **#15** uses
  `essential` fields and `CHANGES` for first-run setup.

## Open questions

1. **`VERSION` with one suite version.** The design says one suite version
   covers every tool. Should `tool.conf` keep its own `VERSION` (informational,
   or for a tool's own migrations), or drop it once #14 lands? This contract
   keeps it required so today's installer keeps working.
2. **Sharing the validator with the scheduled run.** A tool must run without
   the core, but #7 wants one validation implementation. Options: the tool
   imports `core/plugins.py` when present and falls back to its own minimal
   checks; or the core is always installed with the tools (true for anything
   installed by the TUI) and tools may import it; or each tool vendors a copy.
   Leaning to the second, since the TUI is now the only installer.
3. **Conditional fields.** Some settings only matter when another is set
   (`WEBHOOK_FORMAT` without `WEBHOOK_URL`). Version 1 has no `show_if`; the
   form shows every field. Add it if the Settings screen feels cluttered.
4. **Two tools on one tab.** The chooser rule (section 4) is a guess; nothing
   needs it until health-monitor exists, which may want Drives too. Merging
   sections into one Drives screen might be nicer.
