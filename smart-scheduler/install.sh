#!/bin/sh
# Install or repair smart-scheduler. Safe to run again at any time,
# including after a firmware update. Your config is never overwritten.
set -eu

BASE=$(cd "$(dirname "$0")" && pwd)
cd "$BASE"

[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }

case "$BASE" in
  /persistent/*) ;;
  *) echo "Warning: $BASE is not under /persistent and may not survive firmware updates." ;;
esac

for tool in python3 smartctl lsblk runuser psql systemctl; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Missing required tool: $tool"; exit 1; }
done

if [ ! -f config ]; then
  cp config.example config
  echo "Created config from config.example (all defaults)."
fi
chmod 600 config

if grep -q '^ENABLED=no' config; then
  echo "config has ENABLED=no; set it to yes first if you want it running."
  exit 1
fi
chmod 755 smart_scheduler.py install.sh uninstall.sh

# Sentinel the script checks before trusting its trigger templates.
sha256sum smart_scheduler.py planner.py backups.py settings.py settings.json plugin.py screens_calendar.py install.sh uninstall.sh triggers/* > manifest.sha256

python3 smart_scheduler.py heal
echo
if python3 smart_scheduler.py check; then
  echo
  python3 smart_scheduler.py status || true
  echo
  echo "Installed. It checks hourly and starts tests only inside START_WINDOW."
  echo "See the schedule any time:  better-unas smart-scheduler status"
  echo "Logs:                       journalctl -t smart-scheduler"
else
  echo
  echo "Installed, but the self-check found problems (above)."
  echo "If the identity check failed and DRIVE_USER is set in config, that account must"
  echo "be a UniFi OS admin with Drive management. Clear DRIVE_USER to use the console owner."
  exit 1
fi
