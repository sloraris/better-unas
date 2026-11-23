#!/bin/sh
# Install or repair smart-scheduler. Safe to run again at any time, including
# after a firmware update. Your config is never overwritten. The better-unas
# suite runs this; the steps are in core/lib/tool.py (Tool.install).
exec python3 "$(dirname "$0")/smart_scheduler.py" install
