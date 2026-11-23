# scrub-repair design

Agreed design for scrub-repair (#19). It also covers two pieces of core work that come first: a shared core library and a core Calendar. Nothing here is built yet.

## What it is

When a file on the volume is damaged, scrub-repair finds it, finds a good copy in a backup, checks that copy, and swaps it in after you confirm. It never repairs anything by guessing. Files without a verified good copy are reported and left as they are.

## What UNAS gives us (checked on hardware, Drive 4.4.31)

- **UniFi's "data scrubbing" is an md parity check.** Drive drives md's `sync_action` and runs no btrfs scrub. A parity check counts the sectors that disagree. It can't say which file they belong to or which copy is right, so it can't tell scrub-repair what to fix.
- **btrfs data checksums are on.** The volume is mounted without `nodatasum`, so every data block has a checksum. When a read fails one, the kernel logs a `csum failed` line with the inode, which resolves to a path.
- **Kernel messages are not in the journal** on this firmware: they are only in the kernel ring buffer (dmesg). scrub-repair reads `/dev/kmsg` itself.
- **Drive has its own restore.** It has a `remote_backup_restorations` table and restore code for every backup type. UniFi Cloud backups can be read one file at a time: `/tasks/:taskId/ucs/versions/:version/drives/:driveId/files/:itemId/content`.
- **UNAS-to-UNAS replication runs on rsync.** Its single-file restore route isn't mapped yet. It will be mapped during the build by watching the requests the UniFi web UI makes when you open its restore browser, which only reads. Until then, files that only a replication task has go to the folder sources.
- **Snapshots don't help.** A UniFi snapshot shares the damaged blocks, so its copy is damaged too.

## Finding damaged files

- **Passive, the default.** Every 15 minutes, scrub-repair reads the new btrfs checksum lines in the kernel log, from a saved cursor (sequence number plus boot ID). Each line is resolved to a path. So a damaged file turns up as soon as anything reads it: Jellyfin, a backup run, a copy. Lines logged before a reboot are lost, and the scheduled scrub covers that gap.
- **Scheduled btrfs scrub, off by default.** When it's turned on, it runs a read-only scrub (`btrfs scrub start -r`). It runs only inside a night window, pausing at the end of the window and resuming the next night until done.
  - Defaults: every quarter, a 01:00–07:00 window.
  - Frequency, start day and window are all settings.
  - Its nights go in the busy registry, so smart-scheduler stays clear of them, and it stays clear of the others' nights.
  - It can also be started by hand.
- **Damage outside a file** (btrfs metadata, or an inode that won't resolve) is an error alert, with no automatic action.

## Restoring

- **Mode, a setting:**
  - **Alert and stage**, the default: alert, find and verify a good copy, then wait for you to confirm in the TUI or CLI, one file or all of them.
  - **Auto-restore:** swap in any copy that passes, then report.
  - **Alert only:** finding a copy and restoring both wait for you.
- **Where copies come from, in order:**
  1. UniFi's own backups, through Drive's local API. Credentials are never read: Drive does the authenticating.
  2. Folders you list on the NAS: a USB backup drive, a share that a replication task copies into, or a mount from another machine.
- **Which version:** newest first. The first copy that passes the check wins. A damaged file can't be read, so later backup runs usually failed on it and kept the older good copy.
- **The check:** the damaged file is read block by block, skipping blocks that won't read. Every block that does read must match the copy byte for byte. The sizes must match, and the copy must read with no errors.
- **The swap:**
  - The copy is downloaded or copied into a hidden staging folder on the same filesystem and checked there.
  - It is then renamed over the damaged file in one atomic step.
  - It keeps the original's owner, group and mode, and the copy's modified time.
  - If the damaged file changed since it was found, the swap is skipped and the file is checked again.
  - The damaged file is deleted once the copy is in place.
- **No good copy:** the alert stays open and says where it looked. It retries daily, and right away when a folder source is added or a backup run finishes.

## TUI

- **Damaged files screen**, owned by scrub-repair. Each row shows:
  - the path
  - how the damage was found (a read or a scrub) and when
  - the best copy and its state: staged, verified, no copy or restored
  - Enter restores, after a confirm. `r` retries the search for a copy.
- **Home card:** the number of damaged files and the scrub state (next run, or progress while running). It links to the Damaged files screen.
- **Alerts:** each damaged file is an alert that links to the Damaged files screen.
- **Calendar:** scheduled scrub nights appear with their own glyph.

## Settings

- General: Enabled, plus Mode (alert and stage, auto-restore or alert only).
- Sources: folder sources (paths), and whether to use UniFi's backups (on).
- Scrub: scheduled scrub (off), frequency (quarterly), start day, night window (01:00–07:00).
- Heartbeat: its own heartbeat URL pair, the same as the other tools.

## Core work that comes first

### 1. Core library

Tools stay separate modules but import a shared core library, replacing the code each tool now carries its own copy of:

- settings parsing and Config
- Alerts with its outbox
- webhook and heartbeat
- the self-healing timer and cron triggers
- the lock and state files
- the busy registry

Each tool keeps only its own logic, `plugin.py` and `settings.json`. The copied `settings.py` files and the tests that keep them identical go away. Tools are installed through the suite only: `bootstrap.sh` always installs core, and each tool's install and uninstall scripts become thin hooks the suite runs. smart-scheduler and health-monitor move onto the library with no change in behavior.

### 2. Calendar in core, and the busy registry

- **Busy registry:** each tool lists its planned jobs and busy windows in one shared registry in the core library. Each tool's own planner reads the others' windows and stays clear of them. smart-scheduler keeps its per-drive planner. UniFi's scrubs and backup tasks are listed there too, read once for every tool.
- **Calendar:** core draws the month from the registry. It uses a full box grid: lines between every day and every week, two lines per day (the day number with its scrub and backup glyphs, then chips). The selected day gets heavy side bars. The grid takes the whole body, and day details open on Enter.

### 3. scrub-repair

Built on the two above.

## Build order

1. The core library, with smart-scheduler and health-monitor ported to it. One PR.
2. The Calendar in core, plus the busy registry. One PR.
3. scrub-repair, with the replication restore route mapped during the build. One PR.

Each one lands and gets tested before the next starts.
