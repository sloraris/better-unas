"""The core's config-file writer: change the edited keys of a tool's KEY=VALUE
config and keep everything else (comments, blank lines, order, line endings,
quotes, lines it doesn't understand). The Settings screen (core/tui/settings.py)
saves through it, under the tool's run lock.

It is core/lib/settings.py's writer, the same one every tool uses.
"""

from core.lib.settings import (HEADING_RE, LINE_RE, ValidationError,  # noqa: F401
                               apply_edits, save, write_atomic)
