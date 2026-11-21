# smart-scheduler

Scheduled extended SMART tests for a UniFi UNAS, run by UniFi Drive itself.
Part of [better-unas](../README.md).

UniFi Drive can run quick and extended SMART tests (Storage → Health Monitor →
a drive → Run Extended Test), but it has no schedule for them. This fills that
gap. Tests start through Drive's own local API, so they show up in Health
Monitor and UniFi's notifications exactly as if you had clicked the button.

It decides on its own when each drive is due, stays out of the way of scrubs,
rebuilds and busy drives, repairs its own triggers, and tells you when
something it depends on breaks.

> **Unofficial.** This uses UniFi Drive's undocumented local API. A Drive
> update could change it. The script checks its assumptions on every run and
> alerts you instead of guessing.

Tested on a UNAS Pro, UniFi OS 6.0.18, UniFi Drive 4.4.31.

## How it decides

Once an hour it reads everything from the NAS: drives, pools, scrub schedule,
SMART history and attributes. It keeps no list of drives, so added, removed
or moved drives are handled automatically.

| Drive | Next extended test |
|---|---|
| No extended test on record | Now (baseline) |
| Healthy | 90 days after the last good one |
| Older than ~5 years of power-on time | 60 days |
| Warning signs (attributes 5/187/197/198 above zero, a UniFi risk reason, or a state other than optimal) | 30 days |
| A warning attribute rose since its last test | Next eligible night |
| Last test failed or aborted | 7 days later, and you get an alert |

The most urgent due drive gets a test only when all of these hold:

- the time is inside `START_WINDOW` (00:00–05:00 by default), and tonight's test hasn't started yet
- no other SMART test is running
- every pool is fully operational: not degraded, rebuilding, resyncing or scrubbing
- the test, at the drive's own estimated length × 1.5, would end at least 2 hours before the next scheduled scrub
- the test would stay clear of UniFi Drive's scheduled backup and replication tasks (see below)
- the drive isn't busy (more than 20 MB/s of reads and writes)

`python3 smart_scheduler.py status` shows each drive's last test, its next due
date and why, then the plan: the night each drive's next test will start,
assuming every test passes, and any night skipped because of a scrub or a
backup task, followed by the backup tasks it avoids. The plan
comes from `planner.py`, the same code the hourly run uses to decide whether to
start a test, so the two always agree as long as nothing on the NAS changes.

### Backups and replication

UniFi Drive's own backup tasks (UniFi Cloud backups, UNAS-to-UNAS replication,
local copies) are read from Drive's task list every run, and tests are kept
clear of them the same way as scrubs. Only enabled tasks count; a task's days,
first run time and repeat interval give its upcoming runs.

How long a run takes is estimated from, in order:

1. the longest of its last 10 successful or partial runs;
2. without history, the source size over the task's bandwidth limit or
   `BACKUP_UPLOAD_MBPS` (not used yet: Drive's tables don't give a source size
   we can rely on yet; see [#5](https://github.com/sloraris/better-unas/issues/5));
3. otherwise `BACKUP_DEFAULT_HOURS` (4 h).

A test may not start if it could still be running `BACKUP_MARGIN_HOURS` (1 h)
before a task starts, or before that margin has passed after its estimated end.
A run already going on blocks from its real start. A task that repeats every
hour or so leaves no gap long enough for an extended test; set
`BACKUP_AVOID=no` if you'd rather let tests overlap it.

Only the schedule and timing columns are read. The access keys, secrets,
usernames and addresses stored next to them are never selected. If Drive's
tables change shape, you get a warning and tests carry on without avoiding
backups until it is fixed.

### Snoozing a drive

`snooze <slot|serial> <YYYY-MM-DD>` keeps a drive from starting a test before
that date (from midnight, NAS time), for example while a replacement is on its
way. The snooze is kept in `state.json`, not `config`. It ends on its own once
the date comes, and `unsnooze` removes it early. `status` shows each active
snooze, and the plan puts that drive on the first night after it. Both commands
take the same lock as the hourly run, so they never collide with it.

### Drive actions

`run-now <slot|serial> [quick|extended]` starts a test straight away (extended
unless you say quick), as the same account the hourly run uses. It first lists
anything in the way. A busy drive, a scrub or UniFi backup the test would run
into, or being outside `START_WINDOW` stops it unless you add `--override`. A
test already running on any drive, an unhealthy or resyncing pool, a drive that
can't run SMART tests, or Drive refusing the account always stops it.

`exclude` / `include <slot|serial>` add or remove a drive in `EXCLUDE_SERIALS`,
and `interval <slot|serial> <days|default>` sets or clears its entry in
`INTERVAL_OVERRIDES`. They edit `config` in place, keeping your comments. All
of these take the run lock, and `--dry-run` changes nothing.

## Install

No UniFi setup is needed. It runs as the console owner (see
[Which account it uses](#which-account-it-uses)).

1. On the NAS, run the [better-unas installer](../README.md#install) and pick
   smart-scheduler. To install by hand instead, put this folder at
   `/persistent/better-unas/smart-scheduler/` and run:
   ```bash
   cd /persistent/better-unas/smart-scheduler && ./install.sh
   ```
2. Optional: edit `/persistent/better-unas/smart-scheduler/config` to add a webhook and a
   heartbeat URL. Every setting is explained there.

`install.sh` is safe to re-run at any time. It never touches an existing `config`.
The better-unas TUI's Settings screen edits the same file and changes only the
lines you edit, so hand edits and comments survive.

## What it puts on the NAS

| Where | What |
|---|---|
| `/persistent/better-unas/smart-scheduler/` | the scripts, `config`, `settings.json` (every setting's default and allowed values), `state.json`, trigger templates, `manifest.sha256` |
| `/etc/systemd/system/smart-scheduler.{service,timer}` | main trigger: hourly, 10 min after boot, catches up on missed runs |
| `/etc/cron.d/smart-scheduler` | backup trigger |

Nothing else. It installs no packages and uses only what ships in the
firmware: Python 3 (a dependency of UniFi's own base packages), `smartctl`,
`psql` and systemd.

## Self-healing

- **Two triggers watch each other.** Each run checks the systemd timer and the
  cron line and restores whichever is missing or changed, from the templates in
  `/persistent`. If only one gets wiped, the other brings it back.
- **It repairs only what it owns.** It verifies its own files against
  `manifest.sha256` first. If they don't match, it doesn't self-heal; it alerts
  you until you re-run `install.sh`.
- **It reports what it doesn't own and never repairs it.** If `smartctl`,
  `psql`, the Drive API or the account it uses stops working, it alerts you and
  starts no tests.
- **Firmware updates are noticed.** When the firmware or Drive version changes,
  it runs a full self-check and tells you whether everything survived.
- **It respects deliberate shutdowns:**
  - If you set `DRIVE_USER` and later delete that account, that counts as "turned off on purpose": one notice, no tests, nothing recreated.
  - `ENABLED=no` in `config` removes the triggers on the next run.
  - `./uninstall.sh` removes everything cleanly.

### What it can't heal

All triggers live in the writable `/etc` layer of the firmware. That layer has
survived firmware updates so far, but a factory reset (or an update that resets
it) would remove both triggers at once, and nothing on the NAS would ever run
the repair. **Set `HEARTBEAT_URL`** to a dead-man's switch
([healthchecks.io](https://healthchecks.io), or an Uptime Kuma push monitor)
with a grace period of a few hours. If the pings stop for any reason, it alerts
you from outside. Recovery is then one command: `/persistent/better-unas/smart-scheduler/install.sh`.

## Alerts

Set `WEBHOOK_URL` and `WEBHOOK_FORMAT` (`text` for ntfy and most receivers, or
`json`, `slack`, `discord`). You'll hear about:

- a failed or aborted extended test
- the API, the account it uses, or required tools going missing
- triggers it restored, and firmware updates it noticed
- problems clearing (a "Resolved:" message)

Alerts are deduplicated: a problem that persists repeats every 72 hours, not
every hour. If the webhook can't be reached, messages are kept and sent once it
is back. Everything also goes to the journal: `journalctl -t smart-scheduler`.

## Commands

```bash
python3 smart_scheduler.py status            # per-drive schedule and reasons
python3 smart_scheduler.py run --dry-run     # what a run would do right now, changing nothing
python3 smart_scheduler.py check             # self-check only
python3 smart_scheduler.py heal              # restore triggers now
python3 smart_scheduler.py snooze 3 2026-12-15 --note "waiting for a spare"
python3 smart_scheduler.py unsnooze 3        # slot number or serial
python3 smart_scheduler.py run-now 3 quick   # add --override past a busy drive, scrub, backup or window
python3 smart_scheduler.py exclude 3         # include 3 undoes it
python3 smart_scheduler.py interval 3 30     # interval 3 default undoes it
python3 -m unittest discover -s tests        # logic tests (any machine, no NAS needed)
./uninstall.sh [--purge]                     # remove triggers (and the folder)
```

## How it talks to UniFi Drive

| Purpose | Call (on `127.0.0.1:16080`, local only) |
|---|---|
| Drives, pools, scrub schedule | `GET /api/v2/storage` |
| SMART history | `GET /api/v2/storage/disks/{slot}/smart-tests?serial=…` with `X-UserId` |
| Start a test | `POST /api/v2/storage/disks/{slot}/smart-tests` `{"type":"extended"}` with `X-UserId` |
| Account ID | read-only lookup in UniFi OS's user database |
| Backup tasks and runs | read-only lookup of schedule and timing columns in Drive's `remote_backup_tasks` and `remote_backup_task_runs` |

These are the same calls the Drive web UI makes.

## Which account it uses

UniFi Drive runs every SMART call as a UniFi OS account, given in the
`X-UserId` header. Drive only accepts admins with Drive management rights;
anyone else gets `403 Forbidden`. A basic user won't work, and basic users
can't be local-only accounts anyway.

By default the scheduler uses the **console owner**, which always exists and
always has those rights. It reads the owner's ID from UniFi OS's own database
(read-only) on every run, so nothing needs to be created or kept in sync. The
owner's password is never read or used: as root on the NAS, the script talks to
Drive's local API directly.

To use a different admin instead (for example a local "custom administrator"
with Drive management, so you can turn the scheduler off by deleting it), set
`DRIVE_USER` to that account's username. `check` and every run confirm that
Drive accepts the account before starting anything.

UniFi doesn't record which account started a test, so tests look the same in
Health Monitor either way.
