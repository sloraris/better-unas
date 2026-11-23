# better-unas TUI: style and layout

Status: all 8 steps of the build order (§12) are built. The checks in §11 keep it that way: snapshot tests for the layouts below, and `core/tests/test_wording.py` for the wording in §8. It applies to every screen in the core and in every tool. The building blocks it describes live in `core/tui/widgets.py`, so a screen that follows this spec has little to decide for itself. What the app does is in [tui-design.md](tui-design.md); this file covers how it looks and reads.

Where this came from: the first hardware run showed screens that worked but read as a wall of same-looking text. The problems are covered in the rules below. Each rule says which problem it fixes, so it doesn't get "simplified" away later.

## 1. Principles

1. **Readable at a glance.** Someone glancing at a screen should be able to tell what's wrong, what's next and what they can do, before reading any sentence in full.
2. **Each kind of text has one look.** A label always looks like a label and a value like a value. A note, a warning and an identifier each have their own look too. Screens never style text ad hoc.
3. **Answer the question the label asks**, in as few words as that takes. Explanations go in a note or in help, never in the value.
4. **One place for each job.** Each thing has one home screen, and other screens link to it instead of repeating it.
5. **Only things that do something are selectable.** Each stop goes somewhere different.
6. **Never rely on color alone.** Every colored meaning also has a glyph or a word, so `NO_COLOR` and ASCII terminals lose nothing.

## 2. Screen anatomy (80x28 and up)

```
 0  nav box     ┌──────────────────────────────────────────────── v0.2.1 ─┐
 1              │  1 Home  │ 2 Calendar │  3 Drives  │  4 Alerts │ 5 Set… │
 2              └─ Drive 4 ────────────────────────────── smart-scheduler ─┘
 3  (blank)
 4  body        (rows 4-23: 20 rows; see section 4)
19  info box    ┌─────────────────────────────────────────────────────────┐
                │ help for the selected setting, up to three lines        │
23              └─────────────────────────────────────────────────────────┘
24  action box  ┌─────────────────────────────────────────────────────────┐
25              │  [t] test  [z] snooze  [x] exclude          ↑↓ history  │
26              └─────────────────────────────────────────────────────────┘
27  nav bar     Esc back   1-5 tabs                         ? help   q quit
```

- **Nav box:** the tabs share the full width equally, and the current one is reversed across its whole share. The installed version sits in the top edge at the right, dim. When an update is available it becomes `▲ v0.2.2` in the warn tone, on every screen; DRY RUN goes to its left. Enter isn't involved: the update itself lives in Settings › better-unas (section 6).
- **Breadcrumb:** in the nav box's bottom edge at the left, accent and bold. It lists the screens below the tab (`Drive 4`, `space-watch › Preview`). The tab itself isn't repeated, because the highlighted tab right above already says it. Top-level screens show no breadcrumb. The tool that owns the screen sits at the right of that edge, dim.
- **Info box:** shown only on screens with a form (Settings and the setup wizard). It spans the full width just above the action box and holds the selected field's help. The body loses its 5 rows.
- **Action bar** (inside the action box): only this screen's own keys. Each key is drawn as a **chip**, the key letter in reverse video with a space on each side, followed by its label in normal text. Movement hints (`↑↓ history`, `←→ change`) go at the right end in dim, without chips. If the keys don't fit, the bar ends with `[?] more`. There's no `───` rule on this row any more, so it can't be mistaken for a title.
- **Nav bar:** dim, no chips. Back and tabs on the left; help and quit on the right. It looks the same on every screen, which teaches people to stop reading it.
- **Messages** (the toast after an action) replace the action bar for 4 seconds or until the next key. They start with their glyph (section 3) and carry its tone.

This fixes: page-specific keys blending into the static nav. Chips against dim text is a difference you see at once, without reading.

## 3. Text roles

Every piece of text on a screen has exactly one role. Widgets take the role, not a tone, so screens can't drift apart.

| Role | Look | Used for |
|---|---|---|
| heading | accent, bold | Section and panel titles, the day title on the calendar |
| label | dim | The left side of a key-value row, column headers |
| value | normal | The answer to a label |
| value, strong | bold | The one value per block that matters most (e.g. a failed result) |
| status | tone + glyph | ok / planned / warning / failed / running, always with its glyph |
| note | dim, starts with `↳` | One-line explanation under the thing it explains |
| reason | warn, starts with `!` | Why something won't happen (no test tonight, blocked action) |
| tag | reverse, padded | Short markers next to a heading: `today`, `developer mode`, `changed` |
| id | dim, after the name | SMART attribute numbers, serials in a title, pool ids |
| key chip | reverse, padded | Keys in the action bar and in help |
| selection | reverse, full row width | The selected row in any list, plus the `›` marker |

### Glyphs

| Meaning | Unicode | ASCII | Tone |
|---|---|---|---|
| passed / ok | `✓` | `+` | good |
| failed / alert | `✗` | `x` | bad |
| warning / reason | `!` | `!` | warn |
| running | `▶` | `>` | accent |
| planned | `○` | `o` | warn |
| scrub | `↻` | `s` | accent |
| backup window | `⇡` | `b` | accent |
| note | `↳` | `-` | dim |
| update available | `▲` | `^` | warn |

A glyph always goes before the thing it describes (`✗ failed`, `▶ Drive 1`), and a status is never shown by color alone.

This fixes:
- `No test: would overlap UniFi Cloud Backup` blending into the date above it. It's now a `!` reason in warn, indented under a bold heading.
- The date blending into `today`. `today` is now a tag.
- SMART attribute numbers reading as counts. They're now dim ids after the name.

## 4. Layout rules

1. **Margins.** Every screen draws its body from column 1 to width-2. The settings form currently starts at column 0, and that goes.
2. **Blocks.** A block is a heading plus rows. Leave one blank line between blocks. Rows inside a block are indented 2 from its heading.
3. **Key-value rows.** All labels in a block share one width: the longest label plus 3, at least 12. A value can have a second part after it, shown dim: the reason, a unit, a source (`7 days  retry`, `90 days  default`). Numbers are right-aligned within their column.
4. **Panels.** When two blocks sit side by side, each gets a box (`canvas.box`) with its heading in the top border. Below 100 columns at most two panels share a row. A panel is never used just to decorate a single block that fills the width.
5. **Use the height.** If a screen has a growing region (a list, a history, a log), that region takes all the height left after the fixed blocks. Fixed blocks don't get squeezed to make room for empty space at the bottom. Detail screens put their summary blocks at the top and the growing region below them, filling down to the action bar.
6. **Tables.** Column headers are labels (dim). One column is flexible and takes the spare width; the others are sized to their content. A status column holds glyph + word, never a word alone.
7. **Selection detail.** When a list screen shows more about the selected row, it goes in a panel at the bottom of the body. The panel's title names the row, so it can't be mistaken for part of the list.
8. **Widths.** Everything has to work at 80x28. Wider terminals give the flexible column and the growing region more room, and nothing else changes. Layouts don't switch at breakpoints, except that at 100+ columns a third panel may sit beside the first two.

This fixes: SMART and schedule details bunched at the top with half the screen empty below.

## 5. Navigation and selection

1. **One stop per destination.** If several rows would all open the same place, they form one stop, and the whole group is highlighted as a unit. A row becomes its own stop only when Enter on it goes somewhere different from its group.
2. **Every stop shows where it goes.** The stop itself carries the destination (a dim `› Drives` hint, a selected row, a panel naming the drive), so the action bar just says `[Enter] open`.
3. **Non-stops aren't drawn as selectable.** They get no `›` and no hover. Arrow keys skip them.
4. **Esc goes back one level, always.** It never discards anything without asking.

On Home, each tool card is one stop that opens the tool's first tab. A card item becomes a stop only when it names a target different from the card's own (contract change, section 9). For example, smart-scheduler's "Last result: ✗ failed, Drive 4" opens that drive's page.

This fixes: Home's smart-scheduler lines each being selectable, yet all going to the calendar.

## 6. Where things live

The whole suite updates as one piece, so a separate Tools tab isn't needed. It goes. Tabs become: **Home, the tools' tabs, Alerts, Settings.**

| Job | Lives in | Shown elsewhere as |
|---|---|---|
| Version, update check, update, developer mode, branch | Settings › better-unas | `▲ v0.2.2` in the tab bar; a notice on Home when one is available, whose Enter opens Settings › better-unas |
| Remove better-unas | Settings › better-unas (`R`) | — |
| Install / uninstall / reinstall a tool | Settings › *tool* (`i` / `x`) | The Settings list shows every known tool, not-installed ones included |
| Webhook, heartbeat, test send | Settings › Notifications (`t` test) | Alerts shows `Sent to: ntfy` in its header |
| A tool's own settings | Settings › *tool* | — |

### Settings list

```
 1 Home │ 2 Calendar │ 3 Drives │ 4 Alerts │ 5 Settings              ▲ v0.2.2
 Settings ─────────────────────────────────────────────────────────────────────

 better-unas
 › better-unas         v0.2.1 · ▲ v0.2.2 available
   Notifications       webhook: ntfy · heartbeat: on

 Tools
   smart-scheduler     ✓ installed · 26 settings
   space-watch         not installed







 [Enter] open better-unas                                          ↑↓ move
 1-5 tabs                                                     ? help   q quit
```

### Settings › better-unas

```
 Settings › better-unas ───────────────────────────────────────────────────────

 Version       v0.2.1   release
 Update        ▲ v0.2.2 available   checked 00:30
               ↳ Adds a drive temperature graph. Press u to update.

 Updates
   Developer mode              [ ] off
   Branch                      main

 [u] update  [c] check  [s] save  [R] remove better-unas            ↑↓ move
```

- In developer mode, Version reads `main @ 1a2b3c4   developer mode` (the last part as a tag), and Update reads `▲ new commit 5d6e7f8`.
- After an update the restart question appears, as now.

### Settings › a tool

The header shows the tool's state and version. The action bar has `[x] uninstall` (or `[i] install` / `[i] reinstall`). A tool that isn't installed shows its summary, `CHANGES` and `SETUP` instead of a form, plus `[i] install`.

### Settings › Notifications

These are core settings, saved in `better-unas.conf`: webhook URL, format, notify on success, alert repeat hours, heartbeat URL, heartbeat fail URL. `[t] send test` sends a test through the webhook and shows the HTTP result as a message. A test is available on every setting that reaches something outside the NAS.

## 7. Inputs

1. **An editing field never hides its own text.** While a text field is being edited, it opens as a full-width box under its label, from column 3 to width-3. A long value wraps inside the box, up to 4 lines, so a pasted URL is visible end to end.

   ```
     Webhook URL                 https://ntfy.sh/nas-alerts-7f2…   (not editing)

     Webhook URL                                                   (editing)
     ┌─────────────────────────────────────────────────────────────────────────┐
     │ https://ntfy.sh/nas-alerts-7f2c9d1e4b?auth=tk_8sd7f6g5h4j3k2l1_         │
     └─────────────────────────────────────────────────────────────────────────┘
   ```

2. **Pick, don't type, when the options are knowable.** This covers branches, drives, UniFi accounts and tools (the Branch picker already works this way). A setting with a known set of values plus room for more, like the watched SMART attributes, is a checklist: one `[x]`/`[ ]` row per named option and an "Other IDs" row last. Space ticks, Enter on the last row edits the others, `u` undoes, Esc closes. The closed field lists what's ticked (`S5, S187, S197, S198`).
3. **A setting that reaches outside has a test**, next to it in the action bar.
4. **Saving does the whole job.** If a change needs a follow-up step to take effect, saving does that step, and the save confirm says so. Help text never asks the user to "reinstall" by hand. Example: turning smart-scheduler's Enabled back on reinstalls its timer when you save (`"on_change": "install"` in the schema, section 9).

## 8. Wording

### Rules

1. **Answer the label's question.** "Last test" asks *when*: `never`, `Thu 12 Nov`. "Why" asks *for a reason*: one word or a short phrase from the tool's vocabulary (below).
2. **Say a fact once per screen.** If Last test says `never`, no other field explains that the drive was never tested.
3. **Short values, longer notes.** A value fits in its column. When an explanation helps, it goes on a `↳` note under the row, or in `?` help.
4. **One name per thing.** Use the names in the glossary everywhere: screens, help, messages and the CLI.
5. **Say who does what, and when.** Don't use vague subjects ("the next run") or unnamed objects ("reinstall"). Name the actor and the moment: "the hourly check", "when you save".
6. **Unknown, none and zero are different.** `unknown` (dim) when it couldn't be read, `none` when there's nothing, `0` for a counted zero. Never a bare `-`.
7. **Dates and times.** `Thu 12 Nov`, with the year added outside the current year. 24-hour `02:30`. Durations `2h 10m`. Relative words (`today`, `tomorrow`) only as tags.

### Settings help text

A setting's help answers three things, in this order, in at most three short lines:

1. **What it controls**, naming the thing ("smart-scheduler's hourly check, which starts tests that are due").
2. **What changing it does**, and when ("Off: no new tests start, and saving removes the hourly timer. Tests already running finish.").
3. **How to undo it**, if that isn't obvious ("Turn it back on and save to restore the timer.").

### Before and after

| Where | Before | After |
|---|---|---|
| Drive › Why | `never tested; first test sets a baseline` | `baseline` |
| Drive › Last test (never tested) | `none passed yet` | `never` |
| Drive › Last test (failed) | `none passed yet` | `Thu 12 Nov  ✗ failed` |
| Drive › Interval | `60 days: 59,052 power-on hours` | `60 days  old drive` |
| Drive › Why (retry) | `retrying: last test ended failed/failed` | `retry` |
| Calendar day | `No test: a SMART test is already running` | `! No test tonight: one is already running` |
| Enabled help | `Set to no to stop: the next run removes the timer and cron line. To turn it back on, set yes and reinstall.` | `The hourly check that starts tests when they're due. Off: saving removes its timer; running tests finish. On: saving puts the timer back.` |

### smart-scheduler vocabulary

- **Why** (why the next test is when it is): `baseline` (never tested), `retry` (last test failed), `warning signs` (a rise made it due at once), `snoozed`, `excluded`, or the interval that came round: `default interval`, `old-drive interval`, `watch interval`, `drive interval`.
- **Interval source** (shown after the days, e.g. `30 days  watch`): `default` (INTERVAL_DAYS), `old drive` (OLD_INTERVAL_DAYS), `watch` (WARN_INTERVAL_DAYS, until a test passes with no further rise), `drive` (INTERVAL_OVERRIDES).
- **No test tonight** reasons start with what blocks it: `one is already running`, `scrub on pool 2 at 02:30`, `overlaps UniFi Cloud Backup (02:00–04:00)`, `outside the start window`, `all drives are up to date`.

### Glossary

| Use | Not |
|---|---|
| test (an extended or quick SMART test) | scan, check, run |
| the hourly check (smart-scheduler's timer) | the next run, the run, the job |
| Drive N (a drive by its bay number) / drive | Slot N, slot, bay, disk, device, sdX (except in logs) |
| S197 (a SMART attribute: S plus its id) | attribute 197, ID 197, a bare 197 |
| update (better-unas to a new version) | upgrade, pull, reinstall |
| install / uninstall (a tool) | enable / disable |
| snooze (skip a drive until a date) | pause, mute |

## 9. Contract changes this needs

These go into [plugin-contract.md](plugin-contract.md) when they're built:

1. **Summary items (built, plugin-contract.md `summary`):** an item's `tab` (or a new `target`, which can name a tab plus a screen argument such as a drive) is set only when it differs from the card's default. The core makes only those items stops.
2. **Schema `on_change` (built):** `"install"` makes the core run the tool's installer after a save that changed this field. The confirm says so.
3. **Notifications move to the core (built, plugin-contract.md "Shared settings"):** `WEBHOOK_URL`, `WEBHOOK_FORMAT`, `NOTIFY_SUCCESS`, `ALERT_REPEAT_HOURS`, `HEARTBEAT_URL` and `HEARTBEAT_FAIL_URL` go in `better-unas.conf`. Tools read those keys from `<home>/better-unas.conf`, a plain key=value file, with their own config as a fallback (now through `core/lib/config.py`). The update moves smart-scheduler's existing values across, once.
4. **Roles, not tones:** widgets take a role (section 3) where they now take a tone. Plugins keep passing tones for status, and the core maps those to status roles.

## 10. Screens, redrawn

The mockups below show layout only. Tones and chips are described under each one. `[k]` stands for a key chip.

### Home

```
 1 Home │ 2 Calendar │ 3 Drives │ 4 Alerts │ 5 Settings              ▲ v0.2.2
 Home ─────────────────────────────────────────────────────────────────────────

 › ▲ Update available: v0.2.2                             Settings › better-unas

   ! 1 active alert
     ✗ Extended SMART test failed on Drive 4                        12 Nov 03:15

 ┌ smart-scheduler ───────────────────────────────────────────────── ! warning ┐
 │ Running       ▶ Drive 1   extended, 80%                                     │
 │ Next test     Mon 23 Nov   Drive 6                                          │
 │ Last result   ✗ failed   Drive 4, Thu 12 Nov                      › Drive 4 │
 │ Next scrub    ↻ Mon 30 Nov 02:30   pool 2                                   │
 └─────────────────────────────────────────────────────────────────────────────┘

 [Enter] open Settings › better-unas  [r] refresh                    ↑↓ move
```

- Stops: the update notice, the alerts block (opens Alerts) and the card (opens Calendar). Inside the card, only "Last result" is a stop, because it opens Drive 4's page. Its target is shown as a dim `› Drive 4` at the right.
- Selecting the card draws its border in accent and reverses its title.

### Drives

```
 Drives ─────────────────────────────────────────────────────── smart-scheduler
  Drive Model             Temp   Last test        Next test       Every
 ›  1   ST4000VN006       41°C   ▶ running 80%    Sun 21 Feb        90d
    2   ST4000VN006       42°C   never            ! due now         90d
    3   ST4000VN006       38°C   ✓ Tue 03 Nov     Tue 02 Feb        90d
    4   WD4002FYYZ        48°C   ✗ Thu 12 Nov     ○ Tue 24 Nov      60d
    5   WD4002FYYZ        48°C   ✓ Tue 20 Oct     Sun 20 Dec        60d
    6   ST4000VN006       48°C   never            ○ Mon 23 Nov      30d
    7   ST4000VN006       45°C   never            ○ Wed 25 Nov      60d





 ┌ Drive 1 · ST4000VN006-3CW104 · SERIAL1 ─────────────────────────────────────┐
 │ ▶ Extended test running, 80%, started Sun 00:10. Next after it: Sun 21 Feb. │
 └─────────────────────────────────────────────────────────────────────────────┘
 [Enter] open  [t] test  [z] snooze  [x] exclude  [i] interval        ↑↓ move
```

- The Status column is gone: its meaning now sits as glyphs in Last test and Next test. The serial moves to the selection panel.
- Interval is always the drive's interval and where it comes from. A retry after a failed test is a reason for the next date (Why `retry`), not an interval.

### Drive

```
 Drives › Drive 4 ───────────────────────────────────────────── smart-scheduler
 WDC WD4002FYYZ-01B7CB0   SERIAL4   4 TB                    ✗ last test failed

 ┌ Schedule ──────────────────────────┐  ┌ Health ─────────────────────────────┐
 │ Last test   Thu 12 Nov  ✗ failed   │  │ Temperature                   48°C  │
 │ Next test   Tue 24 Nov  ○ planned  │  │ Power-on             59,052 h  6.7y │
 │ Why         retry                  │  │                                     │
 │ Interval    60 days  old drive      │  │ Reallocated sectors         0   S5  │
 │ Snoozed     no                     │  │ Uncorrectable errors        0 S187  │
 │ Excluded    no                     │  │ Pending sectors             0 S197  │
 │                                    │  │ Offline uncorrectable       0 S198  │
 │                                    │  │ Interface CRC errors        0 S199  │
 └────────────────────────────────────┘  └─────────────────────────────────────┘

 Test history (UniFi Drive)
   Type       Started            Took      Result
   extended   Thu 12 Nov 01:00   2h 10m    ✗ failed  read failure
   extended   Mon 10 Aug 00:40   9h 02m    ✓ passed



 [t] test  [z] snooze  [x] exclude  [i] interval                  ↑↓ history
```

- Attribute ids are dim and come after the value, so the first number your eye lands on is the count. A count that rose since the last test is shown strong and bad, with a dim `↑2` after it.
- The history fills the space down to the action bar.

### Calendar

The Calendar is the core's (docs/plugin-contract.md §4, `calendar(ctx)`): one month drawn from every tool that has the hook.

```
 ‹ November 2026 ›                              planned through Sun 21 Feb 2027
 ┌─ Mon ────┬─ Tue ────┬─ Wed ────┬─ Thu ────┬─ Fri ────┬─ Sat ────┬─ Sun ────┐
 │          │          │          │          │          │          │  1       │
 │          │          │          │          │          │          │          │
 ├──────────┼──────────┼──────────┼──────────┼──────────┼──────────┼──────────┤
 │  2       │  3       │  4 ↻     │  5       │  6       │  7       │  8       │
 │          │ ✓3       │          │          │          │          │          │
 ├──────────┼──────────┼──────────┼──────────┼──────────┼──────────┼──────────┤
 │  9       │ 10       │ 11       │ 12       │ 13       │ 14       │ 15       │
 │          │          │          │ ✗4       │          │          │          │
 ├──────────┼──────────┼──────────┼──────────┼──────────┼──────────┼──────────┤
 │ 16       │ 17       │ 18       │ 19       │ 20       │ 21       ┃ 22 today ┃
 │          │          │          │          │          │          ┃ ▶1       ┃
 ├──────────┼──────────┼──────────┼──────────┼──────────┼──────────┼──────────┤
 │ 23       │ 24       │ 25       │ 26       │ 27       │ 28       │ 29       │
 │ ○6       │ ○4       │ ○7       │ ○2       │          │          │          │
 ├──────────┼──────────┼──────────┼──────────┼──────────┼──────────┼──────────┤
 │ 30 ↻     │          │          │          │          │          │          │
 │          │          │          │          │          │          │          │
 └─ ○ planned ─ ✓ passed ─ ✗ failed ─ ▶ running ─ ↻ scrub ─ ⇡ backup ─────────┘
```

- A full box grid: a line between every day and every week, all dim, so a busy day's chips never run into the next day's. The weekday names sit in the top border and the legend in the bottom one.
- Each cell's first line has the day number, then its marks (scrub and backup glyphs) in their status tone. The second line holds chips: a glyph plus a short label (a drive's slot), in the status tone, as many as fit, then `+2`. Past chips are dim, except a failure.
- Today: the day number is reversed, and `today` follows it when the cell has room. The selected day gets heavy side bars (`┃`, `#` in ASCII).
- A six-week month fills the 20-row body. A shorter month leaves rows under the grid for the selected day: the date as a heading, then status rows and `!` reasons, as many as fit, then `N more: Enter for day details`.
- Day details (Enter): headed sections from each tool (smart-scheduler's are Planned, Test history, No test this night, Scrubs and backups, Snoozed drives) with status rows and `↳` notes, then any Problems; long rows wrap.
- A future night with no chips and a reason gets `!` on its chip line. The header's "planned through" date carries the year when it isn't this one, and `! N problems` sits before it when a tool reported any.
- Wider terminals widen the cells; taller ones give the day panel more rows.

### Settings › smart-scheduler

```
 Settings › smart-scheduler ──────────────────────────────────── ✓ installed
 smart-scheduler/config                                       no unsaved changes

 General
   Enabled                     [x] on
   UniFi OS account            console owner

 How often each drive gets an extended test
   Healthy drive               90 days
   Drive with warning signs    30 days
   Old drive                   60 days
   Old-drive age               43,800 power-on hours  ≈ 5 years
 ──────────────────────────────────────────────────────────────────────────────
 The hourly check that starts tests when they're due. Off: saving removes its
 timer; running tests finish. On: saving puts the timer back.
 [s] save  [p] preview  [x] uninstall                    Space toggle  ↑↓ move
```

### Alerts

```
 Alerts ───────────────────────────────────────────────────────────────────────
 1 active · 0 resolved in the last 30 days                    Sent to: ntfy

   Level        Since          Tool             Message
 › ✗ failed     12 Nov 03:15   smart-scheduler  Extended SMART test failed on…

 [Enter] open  [r] refresh                                           ↑↓ move
```

## 11. Checks that keep it this way

- **Widget tests** cover each role, the chips, glyph fallbacks under ASCII, and the full-width editor.
- **Snapshot tests** keep the headless screens matching the layouts above.
- **A wording lint** (`core/tests/test_wording.py`) reads every schema's labels and help, and every screen the TUI draws from smart-scheduler's fixtures (Home, Calendar and a day, Drives and each drive, Alerts, each Settings page). It flags words from the "Not" column of the glossary, in the sense the glossary rules them out (`reinstall` for updating, not `[i] reinstall` for a tool), `-` standing alone as a value, field help over three lines at 76 columns and group help over one. The settings form shows all three lines of a field's help.
- **The screen captures** in `docs/screens/` are regenerated with each change, so the README shows what's real.

## 12. Build order

Each step is one PR, and each leaves the app working:

1. **Primitives:** roles, glyphs, chips, key-value blocks, panels, notes, and the new action and nav bars. Every screen picks up the footer, margins and roles with no layout change.
2. **Where things live:** drop the Tools tab; the new Settings list; Settings › better-unas with updates and remove; install and uninstall on tool pages; the version in the tab bar.
3. **Notifications in the core:** move the settings, add `[t] send test`, migrate smart-scheduler's keys, and the contract change.
4. **Inputs:** the full-width editor and `on_change: install` (fixes Enabled).
5. **Home:** cards as single stops, item targets, and the contract change.
6. **Drives and Drive:** the layouts and smart-scheduler's vocabulary.
7. **Calendar:** cells, legend rule and day panel. Later moved into the core as a full box grid (the busy-registry PR).
8. **Wording pass and lint:** all help text against section 8, plus `test_wording.py`.
