# better-unas TUI: design

Status: built for v0.2.0 (issues #2-#16), tested against fixtures, not yet
run on a NAS. It replaced the old root `install.sh` menu installer. The
contract between the core and a tool is in
[plugin-contract.md](plugin-contract.md). How screens look and read (text
roles, layout, selection, wording) is in [tui-style.md](tui-style.md); rows
below marked **Changing** are being reworked to match it.

## Goal

One command, `better-unas`, opens a full-screen terminal app on the UNAS. The
first time, it sets up the tools you pick. After that it's where you see what
the tools are doing, change their settings, update them, and look at a
calendar of drive tests.

## Decisions

| Area | As built |
|---|---|
| Runtime | Python standard library `curses`. It ships in the firmware (`libpython3.13-stdlib`), 256 colors work, and it needs no `dialog`/`whiptail`. Every screen also renders to a headless text canvas, which the tests and `python3 -m core.tui --headless 80x24 --keys "..."` use. |
| First install | `bootstrap.sh`, a POSIX sh curl one-liner: checks root, UNAS (`/usr/lib/version`), `python3` with curses, `curl` and `tar`; downloads the latest release (or `--ref`) to a temporary folder; runs `python3 -m core.tui --home /persistent/better-unas --source <tmp>`. Nothing is written until the wizard's review is confirmed. `--dry-run` and `--force` pass through. |
| First-run hook | `core/tui/__main__.py` calls `core.setup.needs_setup(home)`: true when the home has no installed or uninstalled tools and no `.setup-done` marker, or holds an install made by the old `install.sh`. Then the wizard runs first (also with `--setup`, or `--source` on a home not set up yet), and the normal app opens once it has installed something. On a home that's set up, the one-liner just opens the app. |
| First-run setup | 1) pick tools, 2) each tool's `essential` settings, prefilled with defaults (or the old install's config), 3) review listing every change, 4) install: core files, each tool copied and its `install.sh` run, essential edits written, the link, `.setup-done`. |
| Old install | `core.setup.find_legacy()` finds smart-scheduler 0.1.x installed by the old `install.sh`. Its tools start ticked, and `config`, `state.json` and `.lock` are kept as they are. |
| Home screen | **Changing** (tui-style.md §5, §10): each tool card becomes one stop, and only items with their own target are selectable. As built: suite update line, up to 3 active alerts, then a card per tool from the plugin's `status()` and `summary()`. A summary item can name a tab; Enter jumps there. |
| Navigation | **Changing** (tui-style.md §2, §6): tabs become Home, the tools' tabs, Alerts, Settings; screen keys become chips, nav goes dim. As built: tab bar at the top: Home, then the tools' tabs (Calendar, Drives from smart-scheduler), Settings, Alerts, Tools. Number keys or Tab switch; arrows and Enter within a screen; Esc goes back; `?` shows the screen's keys; `q` quits. The footer is navigation (back, tabs, help, quit); the screen's own keys sit on the line above it, ending in `… ?` only if they don't fit. |
| Minimum size | 80x24. Smaller terminals get a "please widen" screen and nothing else. |
| Calendar | Month grid. Each night shows the drives tested or planned, scrub (`sc`) and backup (`bk`) days are marked, past results are colored. Arrows move days, PgUp/PgDn months, `t` today, `r` recalculates, Enter shows the day's details. |
| Planning horizon | Forward until every drive has one planned test. Planned days look different from past results and are recalculated every time the screen opens. |
| Drives screen | A list (slot, serial, temperature, last and next test, interval, status), and Enter for one drive: schedule (last/next, interval and why, snooze, exclusion), temperature, power-on hours, 5/187/197/198/199 with a rise since the last test marked, and UniFi's test history. |
| Drive actions | `t` run a test now (`e` extended, `k` quick), `z` snooze until a date or for a number of days, `u` unsnooze, `x` exclude/include, `i` set the interval. |
| Run now | Shows which checks fail and why. A busy drive, the start window, an upcoming scrub or a UniFi backup can be overridden with a second confirm. A running test, unhealthy pool, a drive that can't run SMART tests, or Drive refusing the account always blocks. |
| Settings | Every setting from the tool's schema, grouped like the config file, validated as you type and by the plugin's `validate()`. Changed fields are marked; `s` saves, Esc offers to discard, `p` previews the calendar with the unsaved values. Saves go through `core/configfile.py`, which changes only the edited lines, under the tool's lock. The config file stays the source of truth and is still hand-editable. |
| Alerts | **Changing** (tui-style.md §6): webhook and heartbeat move to Settings › Notifications, saved in `better-unas.conf`. As built: active alerts and recently resolved ones from every tool. `n` opens the webhook/heartbeat form: format (text/ntfy, Slack, Discord, JSON), URL, and "send test" through the plugin's `send_test()`. |
| Updates | **Changing** (tui-style.md §6): update, check and developer mode move to Settings › better-unas; the tab bar shows `▲ v…` when one is available. As built: checked in the background when the app opens against GitHub's latest release, cached in `.update-check.json` for 6 hours (30 minutes after an error), shown on Home and Tools. `u` on Tools plus a confirm updates, then offers to restart (the app execs the home's launcher once curses has restored the terminal; same after installing or uninstalling a tool). Never automatic. Developer mode (Settings → better-unas, saved in `better-unas.conf`) follows the newest commit on a branch instead; the Branch setting and `b` on Tools pick the branch from GitHub's list, and the installed ref and commit are kept in `.ref` and `.commit`. |
| Versions | One suite version in the root `VERSION` file (v0.2.0) covers better-unas and every tool. `tool.conf` `VERSION` is informational. |
| Tools screen | **Going** (tui-style.md §6): install/uninstall move to each tool's Settings page, remove to Settings › better-unas. As built: installed, uninstalled and available tools with state; `u` update, `i` install/reinstall, `x` uninstall (config kept, `tool.conf` renamed to `tool.conf.uninstalled` so updates leave it off), `c` check for updates, Enter for details, `R` remove better-unas. |
| CLI | `better-unas <command>` prints plain text and never opens a screen: `status`, `list`, `update [--check] [--yes]`, `install`, `uninstall [--purge]`, `<tool> <command...>`. With no arguments it opens the app. Exit codes: 0 ok, 1 problem, 2 usage error, 3 update available, 130 interrupted; a tool's own command passes its code through. |
| Plugins | Each tool ships a settings schema the core turns into forms, and optionally a plugin module (`plugin.py`) for its Home card, alerts, tests and its own screens. A new tool adds screens without touching the core. |
| Removal | `R` on Tools uninstalls every tool, removes the command link, and optionally deletes `/persistent/better-unas`. A tool whose `uninstall.sh` fails stops the folder from being deleted. |

## Avoiding backups and replication

The planner (and the hourly run's start gate) treats scheduled UniFi Drive
backup and replication tasks as busy windows, the same way it treats scrubs.

- **Schedules** come from Drive's `remote_backup_tasks` table: enabled tasks only,
  with weekdays/monthdays, first run time and interval. That covers UniFi Cloud
  backups, UNAS-to-UNAS replication and local tasks. Credential columns are never read.
- **How long each run takes** is estimated in this order:
  1. **History.** `remote_backup_task_runs` start and finish times for that
     task: the longest of the last 10 successful or partial runs. On the test
     UNAS the weekly cloud backup's last runs took 7 and 50 minutes.
  2. **Size ÷ speed**, when there's no history: the task's source size divided by
     its bandwidth limit if set, otherwise `BACKUP_UPLOAD_MBPS`. Not wired up
     yet, since no reliable source-size column is known.
  3. **`BACKUP_DEFAULT_HOURS`** (4 h) when neither is available.
- **Overlap rule:** a test may not start if it could still be running
  `BACKUP_MARGIN_HOURS` before a task starts, or before that margin has passed
  after its estimated end. Windows are shown on the calendar.
- Snapshot schedules are ignored, since they're short and metadata-only.
- `BACKUP_AVOID=no` turns this off.

## Decided without asking (open to change)

- **Snoozes live in the tool's state, not its config.** The hourly run honors them. They're visible and removable on the Drives and Calendar screens.
- **The planner is shared.** The calendar's projection uses the same scheduling code as the hourly run (`smart-scheduler/planner.py`), simulated night by night (one start per night, skipping scrub days, backup windows and snoozes), so the calendar and the real schedule can't drift apart.
- **Reads are live and writes are guarded.** Screens read the NAS directly (Drive API, smartctl, state). Saves and actions take the same flock as the hourly run (`<tool>/.lock`), so they never collide.
- **Tools don't import the core.** The scheduled run and CLI work without the app. smart-scheduler keeps its own copy of the value rules and config writer (`settings.py`), tested against the core's.
- **Colors** use the terminal's own background (light or dark). Green means good or done, yellow means planned or warning, red means failed or alert, and dim means past or disabled. `NO_COLOR` is respected.
- **Rejected:** mouse support and auto-update.

## Layout on the NAS

```
/persistent/better-unas/
  better-unas            the entry point: the app with no arguments, the CLI with them
  core/                  TUI, installer, setup wizard, update checker
  VERSION                installed suite version
  .release/              the installed release, so a tool installs without a download
  .update-check.json     the cached update check
  .setup-done            written by the wizard; setup doesn't run again
  tui.log                the app's log
  smart-scheduler/       tool: scripts, tool.conf, config, state.json, .lock,
                         settings.json, plugin and screens, trigger templates
/usr/local/bin/better-unas -> /persistent/better-unas/better-unas  (restored by update)
```

An uninstalled tool keeps its folder with `tool.conf.uninstalled` in place of
`tool.conf`.

## Open questions

- How to measure upload speed to each provider when a backup task has no run
  history yet, and which column (if any) gives a task's source size.
- The unit of `bw_limit` in `remote_backup_tasks` (assumed Mbit/s, not
  confirmed on hardware), and the real column types and `status` values in
  `remote_backup_task_runs`.
- What health-monitor and scrub-repair need from the plugin interface. This
  will be checked when the first of them is designed.
