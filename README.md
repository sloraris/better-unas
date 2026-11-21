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
  `tool.conf` (name, version, summary, any UI setup step) that the setup
  wizard reads. No tool depends on another.
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

Run this on the UNAS:
```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/sloraris/better-unas/main/bootstrap.sh)"
```

It checks that the box is a UNAS with what better-unas needs (root, Python 3,
`curl`, `tar`), downloads the latest release to a temporary folder and opens
the setup wizard:

1. **Pick tools.** Tick the ones you want.
2. **Essential settings.** The few settings to look at before the first run,
   with the defaults filled in. Everything else is on the Settings tab later.
3. **Review.** Exactly what will change: the files copied, each tool's
   config, the timers and triggers it sets up, and the `better-unas` command.
4. **Install.** Only now is anything written.

Quitting before the review is confirmed leaves the box as it was. Add
`--dry-run` after the command's closing `)` to walk through the wizard and see
the review without changing anything, `--ref <tag|branch>` for a specific
version (`--ref main` for the development version), or `--force` on a box
that doesn't identify as a UNAS.

Everything goes in `/persistent/better-unas/` (the `/persistent` partition
survives firmware updates): the `better-unas` command, `core/`, `VERSION`, a
copy of the release for installing more tools later, and one folder per tool.
The short name `better-unas` is a link in `/usr/local/bin`. Run it any time to
open the app again; updates, installing and uninstalling tools are on its
Tools tab. Updates keep your `config` and state.

Prefer to read it before running it as root?
```bash
curl -fsSLo /tmp/better-unas-bootstrap.sh https://raw.githubusercontent.com/sloraris/better-unas/main/bootstrap.sh
less /tmp/better-unas-bootstrap.sh
sh /tmp/better-unas-bootstrap.sh
```

**Installed with the old `install.sh`?** Run the command above. The wizard
finds the old install, keeps smart-scheduler's `config`, `state.json` and lock
as they are, and moves it to the new layout once you confirm.

## Compatibility

Developed on a UNAS Pro, UniFi OS 6.0.18, UniFi Drive 4.4.31. Other UNAS
models likely work, since they run the same Drive app, but have not been tested.
