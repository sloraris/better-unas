# scrub-repair

Finds damaged files on the btrfs volume, finds a good copy in a backup,
checks that copy, and swaps it in, after you confirm by default. It never
repairs anything by guessing: a file without a verified good copy is
reported and left as it is. The agreed design is in
[docs/scrub-repair-design.md](../docs/scrub-repair-design.md).

Tested on a UNAS Pro.

## How it finds damaged files

btrfs keeps a checksum for every data block. When a read fails one, the
kernel logs a `csum failed` line naming the subvolume and inode. UNAS
doesn't copy kernel messages into the journal, so scrub-repair reads them
from `/dev/kmsg` itself, every 15 minutes, from a saved cursor (sequence
number and boot ID). Each line is resolved to a path with
`btrfs inspect-internal inode-resolve`. So a damaged file turns up as soon as
anything reads it: Jellyfin, a backup run, a copy.

| Found by | What happens |
|---|---|
| A read (`csum failed ... ino ...`) | The file is listed and its copy search starts. New damage also starts an urgent scrub (`URGENT_SCRUB`), to find the rest. |
| A scrub (`checksum error at logical ... inode ...`) | The file is listed. |
| UniFi's data scrubbing (an md parity check) finding mismatches | An urgent scrub, once per check: the parity check can't say which file is affected. |
| Damage outside a file (btrfs metadata) | An error alert. scrub-repair can't restore metadata. |
| A block whose inode won't resolve (deleted since, usually) | A warning alert. |
| A file in a UniFi snapshot | A warning. Snapshots are read-only and share blocks with the live file. |

### Scrubs

A scrub is always read-only (`btrfs scrub start -r`): it reads every block
and changes nothing. Each scrub is a job in the busy registry
(docs/plugin-contract.md section 11), so the tools keep out of each other's way:

| Scrub | Tier | When |
|---|---|---|
| Urgent | `urgent` | At once, after new checksum errors or a parity mismatch. Ignores the night window. |
| Manual | `manual` | `better-unas scrub-repair scrub start`, or `s` on the Damaged files screen. |
| Scheduled | `scheduled` | `SCRUB_SCHEDULE=yes`: every `SCRUB_FREQUENCY` (quarterly) from `SCRUB_START_DAY` (the 1st), inside `SCRUB_WINDOW` (01:00-07:00). It stops at the end of each night and carries on the next until it's done. |

A scrub waits while a job at its tier or above holds the disks, or while
UniFi runs a parity check or rebuild. A running scrub stops for a higher-tier
job (`btrfs scrub cancel`, which keeps its place) and carries on after
(`btrfs scrub resume -r`). Starting an urgent or manual scrub nudges a
lower-tier job in the way, such as a routine SMART test, so it makes room.

## Restoring

| `MODE` | What it does |
|---|---|
| `stage` (default) | Alert, find and verify a good copy, stage it, then wait for you to restore it. |
| `auto` | Swap in any copy that passes the check, then report. |
| `alert` | Only alert. Finding a copy and restoring both wait for you. |

**Where copies come from**, in this order, newest copy first:

1. **UniFi Cloud backups** (`USE_UNIFI_BACKUPS`), through UniFi Drive's
   local API, acting as the console owner. Drive does the signing in; no
   credential is read. Each version is asked for the file by its share and
   path.
2. **Folder sources** (`FOLDER_SOURCES`): folders on the NAS such as a USB
   backup drive, a share a replication task copies into, or a mount from
   another machine. Each is tried as `<folder>/<share>/<path>` and
   `<folder>/<path>`. Replication to another UNAS is covered this way: UniFi
   Drive has no single-file restore for it, and the copies are plain files.

**The check.** The copy must be exactly as long as the damaged file and read
with no errors. The damaged file is read block by block, skipping blocks
that won't read, and every block that does read must match the copy byte for
byte. The first copy that passes wins.

**The swap.** The copy is downloaded or copied into a hidden
`.better-unas-staging` folder on the damaged file's own subvolume and checked
there. Restoring renames it over the damaged file in one step, keeping the
original's owner, group, mode and extended attributes, with the copy's
modified time. The damaged file is gone once the copy is in place. A file
that changed since it was found is never swapped. It's read again: if it
reads fine now it leaves the list, otherwise it's searched again. A copy is
skipped if staging it would leave less than 1 GiB free.

**No good copy.** The alert stays open and says where it looked. It tries
again daily, after any UniFi backup run finishes, and when the folder
sources change.

## The app

- **Damaged files** tab: each file with how and when it was found and the
  state of its best copy. The selected file's details show the copy, how
  much of it was compared and where it looked. Enter restores the staged
  copy (after a confirm), `r` looks again, `a` restores every staged copy,
  `x` forgets a file you fixed yourself, `s` starts a scrub.
- **Home card:** the damaged-file count, the scrub running or next, the
  last scrub and the last restore.
- **Calendar:** scrub nights, past and planned, and the days damaged files
  were found or restored.
- **Alerts:** one per damaged file, cleared when it's restored, gone or
  reads fine again.

## Commands

```
better-unas scrub-repair status            # damaged files, the scrub, open alerts
better-unas scrub-repair check             # self-check: files, timer, kernel log, volumes, sources
better-unas scrub-repair files             # one line per damaged file
better-unas scrub-repair find PATH|--all   # look for a good copy now and stage it
better-unas scrub-repair restore PATH|--all
better-unas scrub-repair forget PATH       # stop tracking a file you fixed yourself
better-unas scrub-repair scrub start|pause|resume|cancel|status
better-unas scrub-repair run --dry-run
```

Logs: `journalctl -t scrub-repair`.

## Settings

Set them in the app (Settings › scrub-repair) or in `config` (see
[config.example](config.example)). Webhook and repeat settings are shared by
every tool, in Settings › Notifications. An open damaged file is an error,
so with `OWN_HEARTBEAT_URL` set each check pings the failure URL until it's
resolved.

Turning the tool off removes its timer. A scrub already running carries on;
stop it with `scrub cancel`.

## What it changes on the NAS

| What | Where |
|---|---|
| Timer | `scrub-repair.timer` every 15 minutes, plus a cron line as backup; both restored by each run if removed or changed |
| Its folder | `config`, `state.json` (the damaged-file list, the kernel log cursor, the scrub), `busy.json` |
| Staged copies | `.better-unas-staging/` at the top of the damaged file's subvolume, removed when empty |
| Restored files | Only after a verified copy is staged, and only after you confirm unless `MODE=auto` |

## Not yet checked on hardware

- That a share's files sit in `.srv/.unifi-drive/<share>/` (or its `.data`
  folder) and that UniFi Cloud backups name each backed-up drive after the
  share. Both decide whether a file is found in a backup version.
- The kernel's checksum line formats come from the 5.10 sources UNAS runs,
  not from a damaged file on a real NAS.

## Tests

```
cd scrub-repair && python3 -m unittest discover -s tests
```
