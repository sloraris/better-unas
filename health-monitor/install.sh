#!/bin/sh
# Install or repair health-monitor. Safe to run again at any time,
# including after a firmware update. Your config is never overwritten.
set -eu

BASE=$(cd "$(dirname "$0")" && pwd)
cd "$BASE"

[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }

case "$BASE" in
  /persistent/*) ;;
  *) echo "Warning: $BASE is not under /persistent and may not survive firmware updates." ;;
esac

for tool in python3 smartctl lsblk systemctl; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Missing required tool: $tool"; exit 1; }
done

if [ ! -f config ]; then
  cp config.example config
  echo "Created config from config.example (all defaults)."
fi
chmod 600 config

if grep -q '^ENABLED=no' config; then
  systemctl disable --now health-monitor.timer 2>/dev/null || true
  rm -f /etc/systemd/system/health-monitor.service \
        /etc/systemd/system/health-monitor.timer \
        /etc/cron.d/health-monitor
  systemctl daemon-reload
  echo "Turned off (ENABLED=no): removed the timer and cron line."
  exit 0
fi
chmod 755 health_monitor.py install.sh uninstall.sh

# Sentinel the script checks before trusting its trigger templates.
sha256sum health_monitor.py monitor.py settings.py settings.json plugin.py install.sh uninstall.sh triggers/* > manifest.sha256

python3 health_monitor.py heal
echo
if python3 health_monitor.py check; then
  echo
  # The first check records every counter as it stands and warns only about
  # drives already close to failing.
  python3 health_monitor.py run --source manual >/dev/null || true
  python3 health_monitor.py status || true
  echo
  echo "Installed. It checks every 15 minutes."
  echo "See the last check any time:  better-unas health-monitor status"
  echo "Logs:                         journalctl -t health-monitor"
else
  echo
  echo "Installed, but the self-check found problems (above)."
  exit 1
fi
