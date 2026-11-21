# better-unas TUI: design

Status: agreed design, not built yet. Replaces the `install.sh` menu installer.

## Goal

One command, `better-unas`, opens a full-screen terminal app on the UNAS. The
first time, it sets up the tools you pick. After that it's where you see what
the tools are doing, change their settings, update them, and look at a
calendar of drive tests.

## Decisions

| Area | Decision |
|---|---|
| Runtime | Python standard library `curses`. It ships in the firmware (`libpython3.13-stdlib`), 256 colors work, and it needs no `dialog`/`whiptail`. |
| First install | A curl one-liner downloads `better-unas` to `/persistent/better-unas/` and opens the TUI. Nothing is installed until you confirm in the TUI. |
| First-run setup | 1) pick tools, 2) each tool's few essential settings, prefilled with defaults, 3) review screen listing exactly what will change, 4) install. |
| Home screen | Dashboard: tools and update badges, next planned test, last results, active alerts, next scrub. |
| Navigation | Tab bar at the top (Home, Calendar, Drives, Settings, Alerts, Tools), switched with number keys or Tab. Arrows and Enter within a screen, Esc goes back, `q` quits. The footer always lists the keys. |
| Minimum size | 80x24. Smaller terminals get a "please widen" screen and nothing else. |
| Calendar | Month grid. Each day shows the slot to be tested, scrub days are marked, and past results are colored. Arrows move days, PgUp/PgDn move months, Enter shows the day's details. |
| Planning horizon | Forward until every drive has one planned test. Planned days look different from past results and are recalculated every time the screen opens. |
| Drives screen | Per drive: schedule (last/next, interval and why, snooze, exclusion), key SMART values (temperature, power-on hours, 5/187/197/198/199 with changes since the last test highlighted), UniFi test history. |
| Drive actions | Run a test now (extended or quick); snooze a drive ("not before <date>", expires on its own); exclude a drive or set its interval. |
| Run now | Shows which safety checks fail and why. A busy drive or an upcoming scrub can be overridden with a second confirm. A running test or a degraded pool always blocks. |
| Settings | Every setting, grouped like the config file, with validation. Changed fields are marked; `s` saves, Esc offers to discard. The calendar previews the effect before you save. The config file stays the source of truth and is still hand-editable. |
| Alerts | Pick the format (text/ntfy, Slack, Discord, JSON), paste the URL, then "send test". The same goes for the heartbeat URL. The Alerts screen lists active and recent alerts. |
| Updates | Checked on launch against GitHub's latest release, cached for a few hours, and shown as badges. Updating is one keypress plus a confirm. Never automatic. |
| Versions | One suite version (e.g. v0.2.0) covers better-unas and every tool. `update` updates everything together. |
| CLI | Plain commands stay for scripts and quick checks: `better-unas status`, `better-unas update --yes`, `better-unas smart-scheduler run --dry-run`, and so on. With no arguments, `better-unas` opens the TUI. |
| Plugins | Each tool ships a settings schema that the TUI turns into forms. A tool can optionally ship its own screens (smart-scheduler's calendar and drives) as a Python module the TUI loads. A new tool adds screens without touching the core. |
| Removal | "Remove better-unas" uninstalls every tool, removes the command link, and optionally deletes `/persistent/better-unas`. |

## Avoiding backups and replication

The planner (and the hourly run's start gate) treats scheduled UniFi Drive
backup and replication tasks as busy windows, the same way it treats scrubs.

- **Schedules** come from Drive's `remote_backup_tasks` table: enabled tasks only,
  with weekdays/monthdays, first run time and interval. That covers UniFi Cloud
  backups, UNAS-to-UNAS replication and local tasks. Credential columns are never read.
- **How long each run takes** is estimated in this order:
  1. **History.** `remote_backup_task_runs` start and finish times for that
     task, taking the longest recent run plus a margin. On the test UNAS the weekly cloud
     backup's last runs took 7 and 50 minutes.
  2. **Size ÷ speed**, when there's no history: the task's source size divided by
     its bandwidth limit if set, otherwise a measured or configured upload speed
     to that provider.
  3. **A configurable default window** (e.g. 4 h) when neither is available.
- **Overlap rule:** a test may not start if its estimated end falls inside a
  backup window minus a margin, same as for scrubs. Windows are shown on the calendar.
- Snapshot schedules are ignored, since they're short and metadata-only.

## Decided without asking (open to change)

- **Snoozes live in the tool's state, not its config.** The hourly run honors them. They're visible and removable on the Drives and Calendar screens.
- **The planner is shared.** The calendar's projection uses the same scheduling code as the hourly run, simulated night by night (one start per night, skipping scrub days and snoozes), so the calendar and the real schedule can't drift apart.
- **Reads are live and writes are guarded.** Screens read the NAS directly (Drive API, smartctl, state). Saves and actions take the same lock as the hourly run, so they never collide.
- **Colors** use the terminal's own background (light or dark). Green means good or done, yellow means planned or warning, red means failed or alert, and dim means past or disabled. `NO_COLOR` is respected.
- **Rejected:** mouse support and auto-update.

## Layout on the NAS

```
/persistent/better-unas/
  better-unas            the app (entry point; also the CLI)
  core/                  TUI framework, planner, update checker
  VERSION                installed suite version
  smart-scheduler/       tool: script, config, state, triggers, schema, screens
/usr/local/bin/better-unas -> /persistent/better-unas/better-unas  (restored by update)
```

## Open questions

- How to measure upload speed to each provider when a task has no run history yet.
- What health-monitor and scrub-repair need from the plugin interface. This will be checked when the first of them is designed.
