"""Run the TUI from the repository (or install) root:

    python3 -m core.tui --demo                 placeholder tabs + the space-watch example
    python3 -m core.tui                        the tools found next to core/
    python3 -m core.tui --demo --headless 80x24 --keys "3 down enter"
                                               render to text and print the last frame

The headless keys are space-separated key names (see core/tui/keys.py), or
resize:WxH. The `better-unas` command calls main() to open the TUI.

First run (#15): when core.setup.needs_setup(home) says so (no tools yet, or
an install made by the old install.sh), or with --setup, the setup wizard
opens first. --source names the release to install from; bootstrap.sh passes
the folder it downloaded the release to. --dry-run shows the review and
changes nothing.
"""

import argparse
import os
import shutil
import sys
import tempfile

from .. import plugins as P
from .. import setup as S
from .. import updates
from . import demo
from . import setup as wizard
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


def restart_command(home, argv):
    """argv for starting better-unas again after an update: the launcher in
    the home (the code the update just put there) when it exists, otherwise
    this same command."""
    launcher = os.path.join(os.path.abspath(home), "better-unas")
    if os.path.isfile(launcher):
        return [sys.executable, launcher]
    return [sys.executable, os.path.abspath(sys.argv[0])] + list(argv or [])


def main(argv=None, out=sys.stdout, execv=os.execv):
    ap = argparse.ArgumentParser(prog="better-unas", description="better-unas terminal app")
    ap.add_argument("--home", default=ROOT, help="folder holding the tools (default: next to core/)")
    ap.add_argument("--demo", action="store_true",
                    help="placeholder Calendar and Drives tabs plus the space-watch example tool")
    ap.add_argument("--dry-run", action="store_true", help="screens must not change anything")
    ap.add_argument("--headless", metavar="WxH", help="render to text instead of the terminal")
    ap.add_argument("--keys", default="", help="with --headless: keys to press, space-separated")
    ap.add_argument("--setup", action="store_true", help="open the setup wizard even if set up already")
    ap.add_argument("--source", help="release folder setup installs from (default: the one kept in home)")
    ap.add_argument("--force", action="store_true", help="setup: continue on a box that isn't a UNAS")
    args = ap.parse_args(argv)

    if not args.demo and (args.setup or args.source or S.needs_setup(args.home)):
        code = first_run(args, out)
        if code is not None:
            return code

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
        if not args.dry_run:
            # Notifications moved to better-unas.conf: copy what a tool's
            # config still holds, once. Updating from an older version lands here.
            updates.move_shared(home)
    try:
        tools, broken = P.discover(home)

        def build(term):
            app = App(term, tools, broken, extra_tabs=extra, dry_run=args.dry_run, log_path=log_path)
            app.home = home     # read by Settings › better-unas and the tab bar
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
                # the tab bar, Home and Settings read it with updates.cached_status().
                updates.check_in_background(home)
            app = run_curses(build)
            if app is not None and app.restart:
                # curses has put the terminal back; replace this process.
                cmd = restart_command(home, argv if argv is not None else sys.argv[1:])
                print("Restarting better-unas...", file=out, flush=True)
                execv(cmd[0], cmd)
        return 0
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def first_run(args, out):
    """Run the setup wizard. Returns an exit code to stop with, or None to
    go on to the normal app (setup installed something)."""
    home = os.path.abspath(args.home)
    if args.source and not args.setup and not S.needs_setup(home):
        # The bootstrap one-liner on a box that is set up already.
        print(f"better-unas is already set up in {home}; opening it. "
              "(Updates are in Settings › better-unas.)", file=sys.stderr)
        return None
    source = args.source or S.default_source(home, ROOT)
    if not source:
        print("better-unas: no release to set up from. Run the install one-liner from the README.",
              file=sys.stderr)
        return 1
    problems = [] if os.environ.get("BETTER_UNAS_SKIP_CHECKS") == "1" else \
        S.preflight(home, force=args.force)
    if problems and not (args.dry_run or args.headless):
        for p in problems:
            print(f"better-unas: {p}", file=sys.stderr)
        return 1
    warnings = [f"Not checked in a dry run: {p}" for p in problems] if args.dry_run else []
    setup = S.Setup(home, source)
    if not setup.offered:
        print(f"better-unas: no tools found in {source}.", file=sys.stderr)
        return 1
    log_path = os.path.join(home, "tui.log") if os.path.isdir(home) and not args.dry_run else None

    def build(term):
        return wizard.SetupApp(term, setup, warnings, dry_run=args.dry_run, log_path=log_path)

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
        return 0
    run_curses(build)
    if not setup.done:
        print("Setup left; nothing was changed." if not args.dry_run else
              "Dry run finished; nothing was changed.", file=sys.stderr)
        return 0
    args.home = home
    return None


if __name__ == "__main__":
    sys.exit(main())
