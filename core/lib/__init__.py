"""The library better-unas tools import (docs/plugin-contract.md section 11).

    settings   the settings types: parse, validate, read and write config files
    config     a tool's Config: its config file plus the suite's shared settings
    notify     webhook messages and heartbeat pings
    alerts     deduplicated alerts with an outbox
    tool       a tool's folder: paths, state, the run lock, triggers and self-healing
    util       small helpers (time, logging, commands, atomic writes)

A tool's script finds it from its own folder: the tool sits in
<home>/<tool>/ and the library in <home>/core/lib/, in a checkout and on the
NAS alike, so `sys.path.insert(0, <home>)` then `from core.lib import ...`.
Nothing here imports the TUI. Standard library only.
"""
