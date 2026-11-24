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
| [health-monitor](health-monitor/) | Early warning when a drive or filesystem error count rises: watched SMART counters on every drive in a bay, btrfs device errors and parity-check mismatches, every 15 minutes | Built and tested against fixtures; not yet run on a NAS |
| [scrub-repair](scrub-repair/) | Finds damaged files from btrfs checksum errors (and read-only scrubs, urgent or scheduled), then restores a verified copy from UniFi Cloud backups or your own backup folders, after you confirm | Built and tested against fixtures; not yet run on a NAS |

## Install

You need root SSH on the UNAS:

1. Go to UniFi Drive Settings -> Management -> SSH.
2. Enable SSH and set a password.
3. Log in with `ssh root@<YOUR_UNAS_IP>`.

Then run this on the UNAS, in that SSH session:

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/sloraris/better-unas/main/bootstrap.sh)"
```

The bootstrap first checks the box: that you're root, that it's a UNAS
(`/usr/lib/version`), and that Python 3 with curses, `curl` and `tar` are
there. It then downloads the latest release into a temporary folder and
opens the setup wizard from it:

1. **Pick tools.** Tick the ones you want.
2. **Essential settings.** First the webhook every tool sends alerts to, then
   the few settings per tool worth a look before the first run, with the
   defaults filled in. Everything else is on the Settings tab later.
3. **Review.** Exactly what will change: the files copied, each tool's
   config, the timers and triggers it sets up, and the `better-unas` command.
4. **Install.** Only now is anything written.

Quitting before you confirm the review leaves the box as it was, and the
temporary folder is removed either way. Options go after the command's
closing `)`:

| Option | What it does |
|---|---|
| `--ref <tag\|branch>` | Install that version instead of the latest release (`--ref main` for the development version). |
| `--dry-run` | Walk through the wizard and show the review; change nothing. |
| `--force` | Continue on a box that doesn't identify as a UNAS. |

Running the one-liner again on a box that's set up just opens the app.
Updates are done from the app (see [Updating](#updating)), not by running
the one-liner again.

Prefer to read the script before running it as root?

```bash
curl -fsSLo /tmp/better-unas-bootstrap.sh https://raw.githubusercontent.com/sloraris/better-unas/main/bootstrap.sh
less /tmp/better-unas-bootstrap.sh
sh /tmp/better-unas-bootstrap.sh
```

Everything goes in `/persistent/better-unas/` (the `/persistent` partition
survives firmware updates), and the `better-unas` command is linked from
`/usr/local/bin/better-unas`.

## The app

Run `better-unas` to open it. Tabs along the top switch with the number keys
or Tab, arrows and Enter work within a screen, Esc goes back, `?` lists the
keys and `q` quits. The terminal needs to be at least 80x28; the app asks a
smaller one to grow where the terminal allows it. Colors follow your
terminal (green good, yellow planned or warning, red failed, dim past), the
words always say the same thing, and `NO_COLOR` is respected. The tabs depend
on which tools are installed: with all three you get Home, Calendar, Damaged,
Drives, Alerts and Settings. The sample below and the captures show
smart-scheduler alone.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│    1 Home     │  2 Calendar   │   3 Drives    │   4 Alerts    │  5 Settings  │
└──────────────────────────────────────────────────────────────────────────────┘

 › ! 1 active alert
     ! smart-scheduler: Extended SMART test failed on Drive 4 (…  12 Nov 03:15

  ┌─ smart-scheduler 0.1.1 ────────────────────────────────────── ! warning ──┐
  │ 7 drives scheduled, 4 due now                                             │
  │ Running       ▶ Drive 1 (SERIAL1)                               › Drive 1 │
  │ Next test     ○ Sun 22 Nov, Drive 4 (SERIAL4)                             │
  │ Last result   ✗ failed, Drive 4, Thu 12 Nov                     › Drive 4 │
  │ Next scrub    ↻ Mon 30 Nov, pool 2 scrub 02:30                            │
  └───────────────────────────────────────────────────────────────────────────┘
```

| Tab | What's on it | Capture |
|---|---|---|
| Home | An update notice when there is one, active alerts, and a card per tool. Each card is one stop that opens the tool; an item that leads elsewhere, like the last failed test's drive, is a stop of its own. | [home](docs/screens/home.txt) |
| Calendar | One month from every tool with a calendar: smart-scheduler's past and planned tests, scrub and backup days, and scrub-repair's scrub nights and the days damaged files were found or restored. Enter shows why a night has or hasn't got a test. | [month](docs/screens/calendar.txt), [day](docs/screens/calendar-day.txt) |
| Damaged | scrub-repair's damaged files: how and when each was found and the state of its best copy. Enter restores a staged copy (after a confirm), `r` looks again, `a` restores every staged copy, `x` forgets a file, `s` starts a scrub. | |
| Drives | Every drive's last and next test, temperature and interval, with the selected drive explained under the list. Enter opens one drive (schedule, health counts, UniFi test history); `t` runs a test now, `z` snoozes, `x` excludes, `i` sets its interval. | [list](docs/screens/drives.txt), [drive](docs/screens/drive.txt) |
| Alerts | Active and recent alerts, and where they're sent. | [alerts](docs/screens/alerts.txt) |
| Settings | **better-unas**: version, update (`u`), check (`c`), developer mode, remove (`R`). **Notifications**: the webhook and heartbeat every tool shares; `t` sends a test. **Each tool**, installed or not: its settings as a form, checked as you type (`s` saves, Esc offers to discard, `p` previews the calendar), plus install (`i`) and uninstall (`x`). | [list](docs/screens/settings-list.txt), [better-unas](docs/screens/settings-better-unas.txt), [notifications](docs/screens/settings-notifications.txt), [smart-scheduler](docs/screens/settings.txt) |

The Calendar is the core's, drawn from each tool's calendar hook; Drives
comes from smart-scheduler's plugin and Damaged from scrub-repair's.
health-monitor has neither, so it gets a Home card, its alerts and a Settings
section. The captures are the
real app at 80x28 against the test fixtures (`python3 docs/screens/make.py`
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

The app checks GitHub's latest release when it opens (cached for 6 hours).
The tab bar shows the installed version, or `▲ v…` when an update is out,
and Home shows a notice that opens Settings › better-unas. Update there with
`u`, or with `better-unas update`. Nothing updates on its own. One version
covers better-unas and every tool, and updates keep each tool's `config` and
state. A tool you uninstalled stays off. After an update the app offers to
restart itself, so the new version loads without retyping the command.

To try unreleased changes, turn on **Developer mode** in Settings →
better-unas. Updates then follow the newest commit on a GitHub branch
(`main` unless you pick another in **Branch**, which lists the branches on
GitHub) instead of releases: Settings › better-unas offers each new commit
as an update, and `better-unas update` installs the branch's newest commit. For a
one-off, `better-unas update --ref <branch|tag|commit>` works without it.
Turn developer mode off and the next update goes back to the latest release.

## Removing

- One tool: `x` on its page in Settings, or `better-unas uninstall <tool>`.
  Its config and state are kept unless you add `--purge`.
- Everything: `R` in Settings › better-unas uninstalls every tool, removes the
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

Every tool follows these, so they behave alike:

- **One folder per tool.** A tool lives in `/persistent/better-unas/<tool>/`
  with its own logic, `settings.json`, `config.example`, README and tests,
  thin `install.sh` and `uninstall.sh` hooks, and a `tool.conf` manifest:
  name, summary, CLI script, what it changes on the NAS (`CHANGES`), any
  UniFi UI step (`SETUP`), and where its settings schema (`SCHEMA`, `CONFIG`)
  and plugin module (`PLUGIN`) are. See the
  [plugin contract](docs/plugin-contract.md).
- **Shared plumbing lives in the core library.** Settings, config, alerts,
  webhooks and heartbeats, the run lock, the install manifest and the
  self-healing timer are in `core/lib/`, which every tool imports. A tool
  keeps only what is its own.
- **Installed through better-unas.** The setup wizard, a tool's page in
  Settings and `better-unas install` copy the tool from the release, next to
  the core, and run its `install.sh`. Tools are installed with the suite
  only. No tool depends on another.
- **Works without the app.** The scheduled run and the tool's CLI import the
  core library, never the TUI. The TUI loads the tool's `settings.json`
  (forms) and `plugin.py` (Home card, alerts, its own screens); removing
  `plugin.py` doesn't break the tool.
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
