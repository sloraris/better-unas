"""Run the TUI from the repository (or install) root:

    python3 -m core.tui --demo                 placeholder tabs + the space-watch example
    python3 -m core.tui                        the tools found next to core/
    python3 -m core.tui --demo --headless 80x24 --keys "3 down enter"
                                               render to text and print the last frame

The headless keys are space-separated key names (see core/tui/keys.py), or
resize:WxH. The real `better-unas` command (#15, #16) will call main().
"""

import argparse
import os
import shutil
import sys
import tempfile

from .. import plugins as P
from .. import updates
from . import demo
from .app import App
from .terminal import HeadlessTerminal, ScriptDone, run_curses

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLE = os.path.join(ROOT, "docs", "examples", "space-watch")


def parse_size(text):
    w, _, h = text.lower().partition("x")
    return int(w), int(h)


def parse_keys(text):
    out = []
    for k in text.split():
        if k.startswith("resize:"):
            w, h = parse_size(k[7:])
            out.append(("resize", w, h))
        elif k == "space":
            out.append(" ")
        else:
            out.append(k)
    return out


def main(argv=None, out=sys.stdout):
    ap = argparse.ArgumentParser(prog="better-unas", description="better-unas terminal app")
    ap.add_argument("--home", default=ROOT, help="folder holding the tools (default: next to core/)")
    ap.add_argument("--demo", action="store_true",
                    help="placeholder Calendar and Drives tabs plus the space-watch example tool")
    ap.add_argument("--dry-run", action="store_true", help="screens must not change anything")
    ap.add_argument("--headless", metavar="WxH", help="render to text instead of the terminal")
    ap.add_argument("--keys", default="", help="with --headless: keys to press, space-separated")
    args = ap.parse_args(argv)

    tmp = None
    extra = []
    if args.demo:
        # A throwaway copy, so the demo's lock and state files never land in the repo.
        tmp = tempfile.mkdtemp(prefix="better-unas-demo-")
        shutil.copytree(EXAMPLE, os.path.join(tmp, "space-watch"),
                        ignore=shutil.ignore_patterns("__pycache__", "state.json", "config", ".lock"))
        home, extra, log_path = tmp, demo.TABS, os.path.join(tmp, "tui.log")
    else:
        home, log_path = args.home, os.path.join(args.home, "tui.log")
    try:
        tools, broken = P.discover(home)

        def build(term):
            app = App(term, tools, broken, extra_tabs=extra, dry_run=args.dry_run, log_path=log_path)
            app.home = home     # read by the Tools screen (#14)
            return app

        if args.headless:
            w, h = parse_size(args.headless)
            term = HeadlessTerminal(w, h, parse_keys(args.keys))
            app = build(term)
            try:
                app.run()
            except ScriptDone:
                pass
            app.draw()
            print(term.text(), file=out)
        else:
            if not args.demo:
                # Cached for a few hours, so this rarely touches the network;
                # Home and Tools read the answer with updates.cached_status().
                updates.check_in_background(home)
            run_curses(build)
        return 0
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
