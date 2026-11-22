# Better UNAS

Small tools that fill gaps in UniFi's UNAS systems until (hopefully) UniFi
fills them on its own. They work through what the firmware already ships
(UniFi Drive's own local API, `smartctl`, Python 3), and results show up in
the UniFi UI where possible. Nothing else gets installed, and when a tool
can't do something safely it says so.

`better-unas` is one command on the NAS: a terminal app where you set the
tools up, see what they're doing, change their settings and update them, plus
plain commands for scripts.

> **Unofficial.** Not affiliated with Ubiquiti. These tools use undocumented
> interfaces that a firmware or Drive update can change. Every tool checks its
> assumptions on each run and alerts you when they stop holding.

## Tools

| Tool | What it does | Status |
|---|---|---|
| [smart-scheduler](smart-scheduler/) | Schedules UniFi Drive's own extended SMART tests: per-drive intervals that shorten on warning signs, avoids scrubs, UniFi backups, rebuilds and busy drives | Built and tested against fixtures; not yet run on a NAS |
| health-monitor | Custom thresholds on SMART attribute changes (5/187/197/198/199) and btrfs error counters, alerting via webhook | Idea |
| scrub-repair | When a scrub finds a corrupt file, find it and restore it from a backup copy, with your confirmation | Idea |

## Install

SSH into your UNAS if you haven't already:

1. Go to UniFi Drive Settings -> Management -> SSH
2. Enable SSH and set a password
3. Log in as `root@<YOUR_UNAS_IP>`

Run this on the UNAS:

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/sloraris/better-unas/main/bootstrap.sh)"
```

> Until v0.2.0 is published as a release, add `--ref main` at the end:
> `sh -c "$(curl -fsSL https://raw.githubusercontent.com/sloraris/better-unas/main/bootstrap.sh)" --ref main`

It checks that the box is a UNAS with what better-unas needs (root, Python 3
with curses, `curl`, `tar`), downloads the latest release to a temporary
folder and opens the setup wizard:

1. **Pick tools.** Tick the ones you want.
2. **Essential settings.** The few settings worth a look before the first
   run, with the defaults filled in. Everything else is on the Settings tab.
3. **Review.** Exactly what will change: the files copied, each tool's
   config, the timers and triggers it sets up, and the `better-unas` command.
4. **Install.** Only now is anything written.

Quitting before you confirm the review leaves the box as it was. Options go
after the command's closing `)`: `--dry-run` walks through the wizard and
shows the review without changing anything, `--ref <tag|branch>` picks a
version, and `--force` continues on a box that doesn't identify as a UNAS.
Running the one-liner again on a box that's set up just opens the app.

Prefer to read it before running it as root?

```bash
curl -fsSLo /tmp/better-unas-bootstrap.sh https://raw.githubusercontent.com/sloraris/better-unas/main/bootstrap.sh
less /tmp/better-unas-bootstrap.sh
sh /tmp/better-unas-bootstrap.sh
```

Everything goes in `/persistent/better-unas/` (the `/persistent` partition
survives firmware updates). `better-unas` is linked from `/usr/local/bin`.

## The app

Run `better-unas` to open it. Tabs along the top switch with the number keys
or Tab, arrows and Enter work within a screen, Esc goes back, `?` lists the
keys and `q` quits. The terminal needs to be at least 80x24. Colors follow
your terminal (green good, yellow planned or warning, red failed, dim past),
the words always say the same thing, and `NO_COLOR` is respected.

```
 1 Home │ 2 Calendar │ 3 Drives │ 4 Settings │ 5 Alerts │ 6 Tools
 Home ────────────────────────────────────────────────────────────────────────
›better-unas v0.2.0  Up to date  checked Sun 22 Nov 00:30

 Alerts  1 active
   warn  smart-scheduler: Extended SMART test failed on slot 4 (SERIAL4)

 smart-scheduler  0.1.1  warn  7 drives scheduled, 4 due now
   Running          slot 1 (SERIAL1)
   Next test        Mon 23 Nov, slot 6 (SERIAL6)
   Last result      Failed, slot 4, Thu 12 Nov
   Next scrub       Mon 30 Nov, pool 2 scrub 02:30
```

| Tab | What's on it | Capture |
|---|---|---|
| Home | Update status, active alerts, and a card per tool. Enter on a line jumps to the tab it's about. | [home](docs/screens/home.txt) |
| Calendar | smart-scheduler's month grid: past tests, planned tests, scrub and backup days. Enter shows why a night has or hasn't got a test. | [month](docs/screens/calendar.txt), [day](docs/screens/calendar-day.txt) |
| Drives | Every drive's schedule, temperature and status. Enter opens one drive (SMART values, UniFi test history); `t` runs a test now, `z` snoozes, `x` excludes, `i` sets its interval. | [list](docs/screens/drives.txt), [drive](docs/screens/drive.txt) |
| Settings | Every setting as a form, grouped like the config file and checked as you type. `s` saves, Esc offers to discard, `p` previews the calendar with your unsaved values. | [settings](docs/screens/settings.txt) |
| Alerts | Active and recent alerts. `n` sets up the webhook and heartbeat URLs and sends a test. | [alerts](docs/screens/alerts.txt) |
| Tools | Installed and available tools, updates (`u`), install, uninstall (`x`) and removing better-unas (`R`). | [tools](docs/screens/tools.txt) |

Calendar and Drives come from smart-scheduler's plugin; a tool without
screens just gets a Home card and a Settings section. The captures are the
real app at 80x24 against the test fixtures (`python3 docs/screens/make.py`
regenerates them).

## Commands

With a command, `better-unas` prints plain text and exits, for scripts and
quick checks:

```bash
better-unas status                    # suite version, update status, what each tool is doing
better-unas smart-scheduler status    # one tool's own commands (here: the drive schedule)
better-unas update                    # update to the latest release (--check: only check, --yes: don't ask)
better-unas list                      # installed, uninstalled and available tools
better-unas install <tool>            # install a tool from the installed release
better-unas uninstall <tool>          # add --purge to also delete its config and state
better-unas --help                    # every command and the exit codes
```

Exit codes: 0 ok, 1 a problem was found or a command failed, 2 usage error
(nothing was done), 3 an update is available (`update --check`), 130
interrupted. A tool's own command exits with that tool's code.

## Updating

The app checks GitHub's latest release when it opens (cached for 6 hours)
and shows it on Home and Tools. Update with `u` on the Tools tab or
`better-unas update`. Nothing updates on its own. One version covers
better-unas and every tool, and updates keep each tool's `config` and state.
A tool you uninstalled stays off. After an update on the Tools tab the app
offers to restart itself, so the new version loads without retyping the
command.

To try unreleased changes, turn on **Developer mode** in Settings →
better-unas. Updates then follow the newest commit on a GitHub branch
(`main` unless you pick another in **Branch**, which lists the branches on
GitHub) instead of releases: Tools shows each
new commit as an update, `b` on the Tools tab lists the branches to pick
from, and `better-unas update` installs the branch's newest commit. For a
one-off, `better-unas update --ref <branch|tag|commit>` works without it.
Turn developer mode off and the next update goes back to the latest release.

## Removing

- One tool: `x` on the Tools tab, or `better-unas uninstall <tool>`. Its
  config and state are kept unless you add `--purge`.
- Everything: `R` on the Tools tab uninstalls every tool, removes the
  `better-unas` link and, if you say so, deletes `/persistent/better-unas`.

## Upgrading from the old `install.sh`

Run the install one-liner. The wizard finds the old install, keeps
smart-scheduler's `config`, `state.json` and lock as they are, and moves it to
the new layout once you confirm the review.

## Compatibility

Developed on a UNAS Pro, UniFi OS 6.0.18, UniFi Drive 4.4.31. Other UNAS
models likely work, since they run the same Drive app, but have not been
tested.

## Shared conventions

Every tool follows these, so they behave alike and can be installed alone:

- **Self-contained folder.** A tool lives in `/persistent/better-unas/<tool>/`
  with its own `install.sh`, `uninstall.sh`, `config.example`, README and
  tests, and a `tool.conf` manifest: name, summary, CLI script, what it
  changes on the NAS (`CHANGES`), any UniFi UI step (`SETUP`), and where its
  settings schema (`SCHEMA`, `CONFIG`) and plugin module (`PLUGIN`) are. See
  the [plugin contract](docs/plugin-contract.md).
- **Installed through better-unas.** The setup wizard, the Tools tab and
  `better-unas install` copy the tool from the release and run its own
  `install.sh`. No tool depends on another.
- **Works without the app.** The scheduled run and the tool's CLI never
  import `core/`. The core loads the tool's `settings.json` (forms) and
  `plugin.py` (Home card, alerts, its own screens); removing them doesn't
  break the tool. Where a tool needs the core's rules at run time (reading
  and writing its config), it carries its own copy and its tests check that
  copy against the core's.
- **Firmware-only dependencies.** Python 3 standard library plus tools already
  in the firmware. No `apt`, no pip, no binaries.
- **The config file is the source of truth.** Plain `KEY=VALUE`, still
  hand-editable. The Settings tab changes only the lines you edit and keeps
  your comments.
- **Few files outside its folder.** Only the triggers it needs (a systemd
  timer and a cron line in `/etc`), restored from templates in its folder if
  they go missing.
- **It repairs only what it owns.** It checks its own files against a manifest
  before self-healing, and reports anything it doesn't own instead of fixing it.
- **Off switches are respected.** `ENABLED=no`, uninstalling, or removing
  an account you told the tool to use turns it off, and neither self-healing
  nor an update undoes that.
- **Writes are guarded.** Saves and actions from the app take the same lock
  as the scheduled run.
- **It tells you.** Deduplicated webhook alerts (text/ntfy, json, slack,
  discord), messages held while the webhook is down, everything in the
  journal under the tool's name, and an optional heartbeat URL as an outside
  dead-man's switch.
- **Read before write.** `status`, `check` and `--dry-run` show what a tool
  would do without changing anything.
- **Tests run anywhere.** Logic and screens are tested against anonymized API
  samples with the NAS faked: `python3 -m unittest discover -s tests` in a
  tool's folder, `python3 -m unittest discover -s core/tests` for the core.
