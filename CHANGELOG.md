# Changelog

## v0.2.0 (2026-11-23)

The first release with one version for the whole suite. Tested on a UNAS
Pro.

### better-unas

- `better-unas` terminal app (curses, standard library only): Home, Settings,
  Alerts and Tools tabs, plus tabs a tool brings. Minimum 80x24 (now 80x28), `NO_COLOR`
  respected, every screen also renders headless for tests (#4).
- Plugin contract: `tool.conf` manifest, settings schema, optional plugin
  module for the Home card, alerts and screens; `python3 core/plugins.py check
  <tool>` checks a tool (#2).
- Home dashboard: update status, active alerts, a card per tool (#9).
- Settings: forms built from each tool's schema, validated as you type,
  explicit save that keeps the config file's comments, calendar preview of
  unsaved values (#12).
- Alerts: active and recent alerts from every tool, webhook and heartbeat
  setup with a test send (#13).
- Tools: install, uninstall, reinstall, update and remove better-unas; update
  check against GitHub's latest release, cached for 6 hours; one suite
  `VERSION` (#14).
- `bootstrap.sh` curl one-liner and first-run wizard (pick tools, essential
  settings, review, install), with `--dry-run`, `--ref` and `--force`. Finds
  an install made by the old `install.sh` and moves it over, keeping config
  and state. The root `install.sh` is gone (#15).
- Where things live (docs/tui-style.md, step 2): the Tools tab is gone. Tabs
  are Home, the tools' tabs, Alerts, Settings. Settings › better-unas has the
  version, update (`u`), check (`c`), developer mode and remove (`R`); each
  tool, installed or not, has a page in Settings with install (`i`) and
  uninstall (`x`). Home shows an update notice only when there is one, and it
  opens Settings › better-unas. Saving the developer-mode branch checks it at
  once.
- Notifications in the core (docs/tui-style.md, step 3): the webhook,
  heartbeat and alert repeat are shared by every tool, set once in Settings ›
  Notifications and saved in `better-unas.conf`. `t` sends a test through the
  selected URL and shows the answer. smart-scheduler reads them from there,
  falling back to its own `config`; the first start or update copies
  smart-scheduler's existing values across once, and the setup wizard asks
  for the webhook as a better-unas setting. The Alerts tab shows where
  alerts are sent instead of its `n` setup form. A tool's own URL settings
  get `t` too, through its plugin's `send_test`.
- Inputs (docs/tui-style.md, step 4): an edited text, URL or list field opens
  as a full-width box under its label, so a pasted URL is visible end to end.
  A schema field can say `"on_change": "install"`: saving a change to it asks
  first, then runs the tool's `install.sh`. smart-scheduler's Enabled uses
  it, so turning it off removes the hourly timer when you save and turning it
  on puts the timer back, with no reinstall by hand; `install.sh` now handles
  `ENABLED=no` by removing the timer instead of failing.
- Home cards (docs/tui-style.md, step 5): each tool card is a bordered panel
  with the tool's name and state in the top border, and one stop instead of a
  stop per line. Only items that lead somewhere other than the tool's first
  tab are stops, with a dim `›` hint; smart-scheduler's running test and last
  result open that drive's page. Alerts are one stop. The action bar says
  where Enter goes. Summary items gain `target` (`"Tab:arg"`), `target_label`
  and `status`, and a screen may take `open(arg)`.
- Drives and Drive (docs/tui-style.md, step 6): the Drives list drops its
  Status column for status glyphs in Last test and Next test, shows the
  model, and explains the selected drive in a panel under the list. A drive's
  page has its model, serial, size and state on top, Schedule and Health
  panels side by side, and the test history with how long each test took.
  Values use smart-scheduler's short words (Why `baseline`, `retry`, …;
  interval `old drive`, `per drive`, …); SMART attribute ids come after the
  count. Table cells can be status cells.
- Calendar (docs/tui-style.md, step 7): day cells show drive chips as a
  status glyph and the slot (`○6`, `✓3`, `✗4`, `▶1`, up to three then `+N`)
  and scrub/backup glyphs; today's number is reversed and the selected day
  bracketed. The legend moved into the rule above the day panel, which shows
  the date as a heading, status rows and `! No test tonight: …` reasons in
  short words (`one is already running`, `scrub on pool 2 at 02:30`). Day
  details are headed sections with notes, and long rows wrap.
- Webhooks and heartbeat pings name themselves (`User-Agent: better-unas`).
  Discord and other hosts behind Cloudflare refused urllib's default with
  `HTTP 403`, so the test send and real alerts failed against a working
  webhook. A refused request now shows the host's short reason, e.g.
  `HTTP 403 Forbidden (error code: 1010)`.
- smart-scheduler: only a rise in a watched SMART count shortens the
  interval. A count above zero that stays put (14 offline-uncorrectable
  sectors on a drive that passes its tests) no longer keeps the drive on the
  30-day warning interval. A rise still makes the drive due at once and puts
  it on the warning interval until a test passes with no further rise; the
  plan assumes planned tests pass clean. UniFi risk reasons and a non-optimal
  state still count as warning signs.
- Wording (docs/tui-style.md, step 8): `core/tests/test_wording.py` lints
  every settings schema and every screen drawn from the fixtures for the
  glossary's ruled-out words, a lone `-` as a value, and help longer than
  the form shows. The settings form now shows three lines of help, not two.
  Help reworded where it said "run" for the hourly check, "pauses", or
  didn't say what a setting sets (heartbeat URLs, Starts per night, Old
  drive, Drive with warning signs, Old-drive age, Retry after a failure,
  Busy drive above).
- Screen style (docs/tui-style.md, step 1): keys on the action bar are
  reversed chips and the nav bar is dim, so the two no longer blend; the tab
  bar shows the installed version, or `▲ v…` when an update is out; screens
  get 1-column margins; text roles and status glyphs (with ASCII fallbacks)
  and `heading`, `panel` and `KeyValue` blocks for the screens to come.
- After an update, install or uninstall, the app offers to restart
  itself so the change loads without quitting and reopening.
- Developer mode (Settings → better-unas): follow the newest commit on a
  GitHub branch instead of releases, pick the branch from GitHub's list in
  Settings, and
  `better-unas update` installs that commit. `update --ref` takes a branch,
  tag or commit for a one-off.
- Plain commands: `better-unas status`, `list`, `update`, `install`,
  `uninstall`, `<tool> <command>`, with exit codes for scripts (#16).
- Boxed chrome: the tabs sit in a nav box spanning the width, with the
  version in its top edge and the breadcrumb (accent, without the tab it's
  already under) in its bottom edge, then a blank line before the page. The
  screen's keys sit in an action box above the nav bar, and on Settings and
  setup forms the selected field's help sits in a full-width box above that.
  The minimum size is now 80x28; at start the app asks a smaller terminal to
  grow where the terminal allows it, and puts it back on quit.
- Enter's label in the action bar is just `open` on every screen; the
  selected row, panel or `›` hint already says where it goes.

- Wording: drives are "Drive N" everywhere (screens, alerts, the CLI's
  `<drive|serial>`), not "slot N", and SMART attributes are written "S197",
  e.g. `S197 0->3`. The glossary and the wording lint cover both.
- smart-scheduler: a test planned because its interval came round names
  that interval as its reason instead of `due`: `default interval`,
  `old-drive interval`, `watch interval` (30 days after warning signs) or
  `drive interval` (set per drive). A test moved sooner by warning signs or
  a retry stands out on the Calendar. The Drive page's interval source uses
  the same words beside the days (`30 days  watch`, `14 days  drive`).
- Design for health-monitor (#18): `docs/health-monitor-design.md`.

- Settings: a `checklist` field type, for named options to tick plus an
  "Other IDs" line for anything not listed. smart-scheduler's Warning
  attributes use it: S5, S187, S197 and S198 ticked, S199 offered but off.
  The config text is unchanged (`5,187,197,198`).

- health-monitor (#18), a new tool: every 15 minutes it reads the watched
  SMART counters of every drive in a bay (asleep drives skipped, never
  woken), btrfs device error counters and md parity checks, and alerts when
  one rises. A SMART rise is a warning, an error when it repeats before the
  alert clears or jumps by 10 or more; alerts clear after 7 quiet days. The
  first check warns only about drives already close to failing. Degraded
  array alerts are a setting, off by default. Home card, no screens.
- Heartbeat per tool: smart-scheduler and health-monitor each have an
  optional Own heartbeat URL (and one on failure). When set, the tool pings
  its own pair instead of the shared one, so one tool stopping isn't hidden
  by the other's pings.

- health-monitor: the close-to-failing check at install counts a normalized
  value only once it has fallen below 100, and leaves temperatures out.
  Seagate IronWolf drives set S10's threshold at 97 and S184's at 99, so
  healthy drives at 100 were flagged. Each check now clears an open
  close-to-failing warning once the drive no longer meets the rule.
- Core library (`core/lib/`, docs/plugin-contract.md section 11; step 1 of
  docs/scrub-repair-design.md): settings, config, alerts, webhooks and
  heartbeats, the run lock, the install manifest, triggers and self-healing
  now live once in the core, and smart-scheduler, health-monitor and the
  space-watch example import them. The per-tool copies of `settings.py` and
  the tests that kept them identical are gone, and so are the core's own
  copies in `core/configfile.py` and `core/plugins.py`. A tool's `install.sh`
  and `uninstall.sh` now hand over to its script (`install`, `uninstall`),
  and the manifest covers every file in the tool's folder. Tools install
  with the suite only. No change in what the tools do; the next update
  rewrites each tool's manifest when it re-runs `install.sh`.
- Calendar in the core and the busy registry (step 2 of
  docs/scrub-repair-design.md). The Calendar tab is now the core's: it draws
  one month from every tool with a `calendar(ctx)` plugin hook, as a full box
  grid with a line between every day and every week, so a busy day's chips
  no longer run into the next. Weekday names sit in the top border and the
  legend in the bottom one; the selected day gets heavy side bars. A
  six-week month fills the body and the day's rows open with Enter; a
  shorter month keeps a day panel under the grid. A tool with the hook gets
  `p` (the Calendar planned with unsaved settings) on its Settings page
  without writing its own preview. The busy registry (`core/lib/busy.py`)
  lets tools keep their disk-heavy jobs apart: each tool publishes its
  windows to its own `busy.json` and its planner stays clear of the
  others'. Every window has a tier, highest first: `fixed` (UniFi's own
  jobs), `manual`, `urgent`, `scheduled`, `routine`. A job avoids its own
  tier and above and ignores lower ones; a lower job already running gives
  way to a higher one (pauses, or stops and is planned again), and the
  higher tool nudges it so it happens at once.

- scrub-repair (#19, step 3 of docs/scrub-repair-design.md), a new tool:
  every 15 minutes it reads the btrfs checksum lines in the kernel log
  (`/dev/kmsg`, from a saved cursor) and resolves each to a file. It looks
  for a good copy in UniFi Cloud backup versions, through UniFi Drive's local
  API, and in folders you list. A copy passes when it is the same size,
  reads cleanly and matches every block of the damaged file that still
  reads. It is staged on the same subvolume, then renamed over the damaged
  file: after you confirm by default (`MODE=stage`), at once with
  `MODE=auto`, or only alerted with `MODE=alert`. Read-only btrfs scrubs:
  urgent after new checksum errors or a parity mismatch, manual from the app
  or CLI, and scheduled (off by default; quarterly, 01:00-07:00, carrying on
  night after night). All of them go in the busy registry and give way to
  higher tiers. Damaged files tab, Home card, Calendar scrub nights, one
  alert per damaged file. Damage to btrfs metadata is an error alert only.
- Core library: `core/lib/drive.py`, UniFi Drive's local API (including
  downloads), read-only psql and the console-owner lookup, for tools that
  read what Drive knows.
- README: the tools table listed health-monitor twice.
- Faster pages: a tool can keep a slow read for a minute with `ctx.cached`
  (docs/plugin-contract.md section 6), so switching between Home, the
  Calendar and a tool's tab reuses it. `r` on Home or the Calendar reads
  everything live again.

### smart-scheduler

- Shared planner (`planner.py`): the hourly run and the plan in `status` and
  the Calendar use the same night-by-night simulation (#3).
- Avoids UniFi Drive's scheduled backup and replication tasks, estimating
  each run from its history (`BACKUP_*` settings) (#5).
- `snooze` / `unsnooze` a drive until a date, kept in `state.json` (#6).
- Settings schema (`settings.json`) and a config writer that changes only the
  edited lines; the loader takes its defaults and rules from the schema (#7).
- Drive actions: `run-now` (quick or extended, with checks and `--override`),
  `exclude` / `include`, `interval` (#8).
- Calendar tab: month grid of past and planned tests, scrub and backup days,
  day details (#10). Now drawn by the core from smart-scheduler's
  `calendar(ctx)` hook.
- Publishes its running and planned tests, and UniFi's scrub and backup
  windows, to the busy registry, and plans around other tools' windows by
  tier: the hourly run, `status`, the Calendar and `run-now`'s checks all see
  them. A running test that another tool's higher-tier job needs the disks
  for is stopped (`smartctl -X`) and planned again, without counting as a
  failure.
- Urgent tests: a rise in a watched SMART count or a failed test's retry
  starts on the next hourly run, outside `START_WINDOW`, without using up the
  night's start or waiting for `MAX_CONCURRENT`.
- Drives tab: per-drive schedule, SMART values and UniFi test history, with
  run now, snooze, exclude and interval (#11).
- Pages open faster. One read of the drives is shared by Home, the Calendar
  and Drives for a minute (`r`, or any change from the Drives tab, reads
  live). Each drive is read with one `smartctl` instead of two, its UniFi
  Drive history is fetched once instead of twice, and the drives are read in
  parallel. The hourly run gets the same fewer, parallel reads.

## smart-scheduler 0.1.x (untagged)

Installed by the old root `install.sh` menu installer.

- Schedules UniFi Drive's own extended SMART tests through its local API,
  with per-drive intervals that shorten on warning signs, and avoids scrubs,
  rebuilds and busy drives.
- Self-healing systemd timer and cron line, webhook alerts and heartbeat.
- Runs as the console owner by default (`DRIVE_USER` for another admin).
- Output fits narrow terminals; fixed a lock clash at install time; added
  `better-unas status`.
