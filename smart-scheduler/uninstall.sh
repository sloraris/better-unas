#!/bin/sh
# Remove smart-scheduler's timer and cron line so nothing runs or self-heals.
# Keeps this folder (config, state) unless you pass --purge. Tests already
# running on a drive are left to finish.
exec python3 "$(dirname "$0")/smart_scheduler.py" uninstall "$@"
