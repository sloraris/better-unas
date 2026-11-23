#!/bin/sh
# Remove health-monitor's triggers so nothing runs or self-heals.
# Keeps this folder (config, state) unless you pass --purge.
set -eu

BASE=$(cd "$(dirname "$0")" && pwd)
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }

systemctl disable --now health-monitor.timer 2>/dev/null || true
rm -f /etc/systemd/system/health-monitor.service \
      /etc/systemd/system/health-monitor.timer \
      /etc/cron.d/health-monitor
systemctl daemon-reload
echo "Removed the timer, service and cron line."

if [ "${1:-}" = "--purge" ]; then
  case "$BASE" in
    */health-monitor) rm -rf "$BASE"; echo "Deleted $BASE." ;;
    *) echo "Not deleting $BASE (unexpected folder name)." ;;
  esac
else
  echo "Kept $BASE. Run install.sh to turn it back on, or uninstall.sh --purge to delete it."
fi
