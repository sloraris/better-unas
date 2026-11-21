# Better UNAS

Small, self-contained tools that fill gaps in UniFi's UNAS systems until (hopefully) UniFi fills
them on their own. Each tool works through what the firmware already ships (UniFi Drive's
own local API, `smartctl`, Python 3), and results show up in the UniFi UI where
possible. It installs nothing else, and when it can't do something safely it
says so.

> **Unofficial.** Not affiliated with Ubiquiti. These tools use undocumented
> interfaces that a firmware or Drive update can change. Every tool checks its
> assumptions on each run and alerts you when they stop holding.

## Tools

| Tool | What it does | Status |
|---|---|---|
| [smart-scheduler](smart-scheduler/) | Schedules UniFi Drive's own extended SMART tests: per-drive intervals that shorten on warning signs, avoids scrubs, rebuilds, and busy drives | Drafted, logic tested; not yet run on a NAS |
| health-monitor | Custom thresholds on SMART attribute changes (5/187/197/198/199) and btrfs error counters, alerting via webhook | Idea |
| scrub-repair | When a scrub finds a corrupt file, find it and restore it from a backup copy, with your confirmation | Idea |

## Shared conventions

Every tool follows these, so they behave alike and can be installed alone:

- **Self-contained folder.** A tool lives in `/persistent/better-unas/<tool>/`
  (the `/persistent` partition survives firmware updates) and has its own
  `install.sh`, `uninstall.sh`, `config.example`, README and tests, plus a
  `tool.conf` (name, version, summary, any UI setup step) that the installer
  reads. No tool depends on another.
- **Firmware-only dependencies.** Python 3 standard library plus tools already
  in the firmware. No `apt`, no pip, no binaries.
- **Few files outside its folder.** Only the triggers it needs (a systemd
  timer and a cron line in `/etc`), restored from templates in its folder if
  they go missing.
- **It repairs only what it owns.** It checks its own files against a manifest
  before self-healing, and reports anything it doesn't own instead of fixing it.
- **Off switches are respected.** `ENABLED=no`, `uninstall.sh`, or removing
  an account you told the tool to use turns it off, and self-healing never
  undoes that.
- **It tells you.** Deduplicated webhook alerts (text/ntfy, json, slack,
  discord), messages held while the webhook is down, everything in the
  journal under the tool's name, and an optional heartbeat URL as an outside
  dead-man's switch.
- **Read before write.** `status`, `check` and `--dry-run` show what a tool
  would do without changing anything.
- **Tests run anywhere.** Logic tests use anonymized API samples and fake the
  NAS: `python3 -m unittest discover -s tests` in a tool's folder.

When a second tool exists, code they truly share (API client, alerts,
self-heal) may move into a common module. Until then each tool carries its own
copy, so installing one never requires another.

## Install
SSH into your UNAS if you haven't already:
1. Go to UniFi Drive Settings -> Management -> SSH
2. Enable SSH and set a password
3. Log into your UNAS using `root@<YOUR_UNAS_IP>`

Run the installer on the UNAS:
```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/sloraris/better-unas/main/install.sh)"
```

It downloads the latest release and shows the tools with their install
status, then asks which ones you want. If a tool needs a step in the UniFi UI
first, it asks about that before running anything. Each chosen tool is copied to
`/persistent/better-unas/<tool>/`, and then the tool's `install.sh` is run.

Prefer to read it before running it as root?
```bash
curl -fsSLo /tmp/better-unas-install.sh https://raw.githubusercontent.com/sloraris/better-unas/main/install.sh
less /tmp/better-unas-install.sh
sh /tmp/better-unas-install.sh
```

`better-unas` with no arguments opens the terminal app. With a command it
prints plain text and exits, for scripts and quick checks:
```bash
better-unas status                    # what each installed tool is doing
better-unas smart-scheduler status    # one tool's own commands (here: the drive schedule)
better-unas update                    # update to the latest release (--check: only check, --yes: don't ask)
better-unas list                      # available and installed tools
better-unas install <tool>            # install a tool from the installed release
better-unas uninstall <tool>          # add --purge to also delete its config
better-unas --help                    # every command and the exit codes
```
Exit codes: 0 ok, 1 a problem was found or a command failed, 2 usage
error, 3 an update is available (`update --check`).
The command itself lives at `/persistent/better-unas/better-unas`. The short
name is a link in `/usr/local/bin`, which `better-unas update` restores if a
firmware update removes it.
Updates keep your `config` and state. An uninstalled tool stays off: `update`
won't turn it back on.

Other options: `--ref <tag|branch>` for a specific version (`--ref main` for
the development version), `--tools a,b --yes` to skip the questions,
`--dry-run` to see what would happen.

## Compatibility

Developed on a UNAS Pro, UniFi OS 6.0.18, UniFi Drive 4.4.31. Other UNAS
models likely work, since they run the same Drive app, but have not been tested.
