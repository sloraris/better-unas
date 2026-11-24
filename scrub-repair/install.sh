#!/bin/sh
# Install or repair scrub-repair. Safe to run again at any time, including
# after a firmware update. Your config is never overwritten. The better-unas
# suite runs this; the steps are in core/lib/tool.py (Tool.install).
exec python3 "$(dirname "$0")/scrub_repair.py" install
