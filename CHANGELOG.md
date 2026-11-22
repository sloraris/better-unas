# Changelog

## v0.2.0 (unreleased)

The first release with one version for the whole suite. Not yet run on a
real NAS.

### better-unas

- `better-unas` terminal app (curses, standard library only): Home, Settings,
  Alerts and Tools tabs, plus tabs a tool brings. Minimum 80x24, `NO_COLOR`
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
- Developer mode (Settings → better-unas): follow the newest commit on a
  GitHub branch instead of releases, pick the branch with `b` on Tools, and
  `better-unas update` installs that commit. `update --ref` takes a branch,
  tag or commit for a one-off.
- Plain commands: `better-unas status`, `list`, `update`, `install`,
  `uninstall`, `<tool> <command>`, with exit codes for scripts (#16).

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
