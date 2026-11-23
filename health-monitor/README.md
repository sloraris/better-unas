# health-monitor

Early warning when a drive or filesystem error count goes up. Every 15
minutes it reads the counters below and sends an alert through the shared
webhook when one rises. It keeps no history: `state.json` holds each
counter's last value and the open alerts. The agreed design is in
[docs/health-monitor-design.md](../docs/health-monitor-design.md).

Built and tested against fixtures; not yet run on a NAS.

## What it watches

| Signal | Alert |
|---|---|
| Watched SMART counters (S5, S187, S197, S198 by default; S199 offered) on every drive in a bay | A rise is a warning, e.g. `Drive 3 - S197 0->3 (SERIAL)`. It's an error if the same counter rises again before its alert clears, or rises by `RISE_ERROR_JUMP` (10) or more in one reading. |
| btrfs device error counters, per volume | A rise in corruption errors is an error; a rise in write, read, flush or generation errors is a warning. |
| md parity checks | A check that finishes with mismatches is a warning, once per check. The next clean check clears it. |
| md array state (`MD_STATE_ALERTS`, off by default) | A degraded data array is an error, cleared with "has recovered". Off by default because UNAS already alerts on it. Swap arrays never count. |

SMART and btrfs alerts clear after `QUIET_DAYS` (7) with no further rise. A
later rise opens them again.

Drives that are asleep are skipped, never woken (`smartctl -n standby`).
Drives get their bay number ("Drive 3") from UniFi Drive's local API; if that
can't be read, every SATA disk counts and is named by its serial.

### At install

The first check records every counter as it stands, without alerting, except
for drives that are already close to failing. Those get a warning at install
only. Close to failing means either:

- a normalized SMART value within `PREFAIL_MARGIN` (10) of the
  manufacturer's threshold, or
- a raw count at or above its limit: S5 100, S187 10, S197 10, S198 100.

A drive added later gets a quiet baseline.

### Not watched

UniFi's own risk reasons and health score, drive temperatures, and system
temperatures and fans. UNAS covers these already.

## Settings

Set them in the app (Settings › health-monitor) or in `config` (see
[config.example](config.example)). Webhook and repeat settings are shared by
every tool, in Settings › Notifications.

**Heartbeat:** by default each check pings the shared heartbeat URLs. Set
`OWN_HEARTBEAT_URL` (and optionally `OWN_HEARTBEAT_FAIL_URL`) to give
health-monitor its own check with a 15-minute period, so a stopped
health-monitor isn't hidden by smart-scheduler's hourly pings. The own pair
is used whole: with only the success URL set, a failed check pings nothing.

## Commands

```
better-unas health-monitor status    # the last check and open alerts
better-unas health-monitor check     # self-check: files, timer, tools, drives, btrfs, arrays
better-unas health-monitor run --dry-run
```

Logs: `journalctl -t health-monitor`.

## On the NAS

| What | How |
|---|---|
| Timer | `health-monitor.timer` every 15 minutes, plus a cron line as backup; both restored by each run if removed or changed |
| SMART | `smartctl -j -n standby -A /dev/sdX` |
| Bay numbers | `GET http://127.0.0.1:16080/api/v2/storage` (read only, no account) |
| btrfs | `btrfs device stats <mount>` for each btrfs filesystem in `/proc/mounts` |
| md arrays | `/sys/block/md*/md/{array_state,degraded,sync_action,mismatch_cnt,level}`; an array with holders or a mount that isn't in `/proc/swaps` is a data array |

Nothing is installed on the NAS and nothing is written outside this folder,
the timer and the cron line.

## Tests

```
cd health-monitor && python3 -m unittest discover -s tests
```
