#!/bin/sh
# Remove scrub-repair's timer and cron line so nothing runs or self-heals.
# Keeps this folder (config, state, the damaged-file list) unless you pass --purge.
exec python3 "$(dirname "$0")/scrub_repair.py" uninstall "$@"
