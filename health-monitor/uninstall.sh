#!/bin/sh
# Remove health-monitor's timer and cron line so nothing runs or self-heals.
# Keeps this folder (config, state) unless you pass --purge.
exec python3 "$(dirname "$0")/health_monitor.py" uninstall "$@"
