"""better-unas TUI framework (issue #4): app shell, widgets, headless renderer.

    app.py        App (tabs, screen stacks, key routing, drawing) and Context
    canvas.py     Canvas (clipping, sub-regions) and the headless TextSurface
    terminal.py   CursesTerminal and HeadlessTerminal
    theme.py      tones and colors, NO_COLOR
    keys.py       key names
    widgets.py    list, table, form fields, confirm dialog, toast, scroll text, spinner
    lock.py       the tool's run lock
    screens.py    the core's own screens
    demo.py       placeholder tabs for `python3 -m core.tui --demo`

Standard library only. See docs/plugin-contract.md for what tools get.
"""
