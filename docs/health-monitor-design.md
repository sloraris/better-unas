# health-monitor design

Agreed design for health-monitor (#18), built in `health-monitor/` (see its README for what it reads on the NAS). Not yet run on a NAS.

## What it is

An early-warning tool. It reads drive and filesystem error counters every 15 minutes and raises an alert when one goes up. It keeps no history: `state.json` holds the last reading and the baseline for each counter, nothing more. It has no screens of its own, only a card on Home.

It works on its own. It does not need smart-scheduler, and smart-scheduler does not need it.

## Sampling

- Runs every 15 minutes on a timer that puts itself back if it goes missing, the same way smart-scheduler's does.
- Reads SMART data with `smartctl -n standby`, so a sleeping drive is skipped and never woken.

## SMART counters

- **Which drives:** every drive in a bay, pool drives and backup drives alike. A setting leaves drives out by serial.
- **Which counters:** S5, S187, S197 and S198 by default. S199 (CRC errors, usually a cable) is on the list but off. They are picked in a new core **checklist** setting type: named options plus an "other IDs" line for anything not listed. smart-scheduler's attribute warning list moves to the same type.
- **Rise:** any rise in a watched counter is a warning, written like `Drive 3 - S197 0->3`.
- **Escalation to error:** the alert becomes an error if the same counter on the same drive rises again while the alert is open, or rises by 10 or more in one reading (setting).
- **Clearing:** the alert clears after 7 days with no further rise (setting). A later rise opens it again.

## At install

The first reading becomes the baseline, without alerts, except for drives that are already near failure. Those get a warning at install only. Near failure means either:

- a normalized value within 10 of the manufacturer's threshold, or
- a raw count at or above its limit: S5 100, S187 10, S197 10, S198 100 (settings).

## btrfs

The btrfs device error counters cover the whole volume.

- A rise in `corruption_errs` is an error.
- A rise in any other counter (write, read, flush, generation) is a warning.

## md arrays

- **Parity check:** if a check finishes with `mismatch_cnt` above 0, that is a warning, once per check.
- **Array state:** alerts on degraded or failed arrays are a setting, **off by default**, because UNAS already alerts on them. When on, only data arrays count (not the swap array), and the alert says when the array has recovered.

## Left out

UniFi's own risk reasons and health score, drive temperatures, and system temperatures and fans. UNAS covers these already.

## Alerts and heartbeat

- Alerts use the shared core alert model and notification settings, including repeat hours.
- Each tool gets an optional Heartbeat URL of its own. When it is empty the tool pings the shared Heartbeat URL. A separate URL per tool means one tool dying is not hidden by the other's pings, and each check can have its own grace period (15 minutes here, hourly for smart-scheduler). smart-scheduler gets the same setting.

## Home card

- Overall state in the border.
- One line each for SMART, btrfs, parity and arrays.
- The time of the last reading.
- When smart-scheduler is installed, the card links to its Drives page.

## Wording

Suite-wide: drives are "Drive N", not "Slot N", and SMART attributes are written "S197". Both go in the glossary in `tui-style.md` and in the wording lint.

## Build order

1. This design, posted here and on #18.
2. Wording change PR ("Drive N", "S197").
3. Core checklist setting type PR.
4. health-monitor PR, including the per-tool heartbeat.
