# Changelog

## v0.2.0 (unreleased)

The first release with one version for the whole suite. Not yet run on a
real NAS.

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
  day details (#10).
- Drives tab: per-drive schedule, SMART values and UniFi test history, with
  run now, snooze, exclude and interval (#11).

## smart-scheduler 0.1.x (untagged)

Installed by the old root `install.sh` menu installer.

- Schedules UniFi Drive's own extended SMART tests through its local API,
  with per-drive intervals that shorten on warning signs, and avoids scrubs,
  rebuilds and busy drives.
- Self-healing systemd timer and cron line, webhook alerts and heartbeat.
- Runs as the console owner by default (`DRIVE_USER` for another admin).
- Output fits narrow terminals; fixed a lock clash at install time; added
  `better-unas status`.
