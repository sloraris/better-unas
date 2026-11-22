"""The `better-unas` command (#16).

With no arguments it opens the terminal app (core.tui). With arguments it
never draws a screen, so it works in scripts, cron and over plain ssh:

    better-unas                          open the terminal app
    better-unas status                   suite version, then each tool's status
    better-unas list                     installed, uninstalled and available tools
    better-unas update [--check] [--yes] [--ref <branch|tag>]
                                         update the suite and installed tools
    better-unas install <tool>... [--yes]
    better-unas uninstall <tool> [--purge] [--yes]
    better-unas <tool> <command...>      a tool's own command (tool.conf CLI)
    better-unas --help | --version

The work is done by core.updates and core.install, the same code the Settings
screen uses. The home is the folder holding core/ (BETTER_UNAS_HOME
overrides it, for testing). See USAGE for the exit codes.
"""

import os
import re
import subprocess
import sys

from . import install as I
from . import plugins as P
from . import updates as U

PROG = "better-unas"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Exit codes, for scripts.
OK = 0
PROBLEM = 1            # a check found a problem, or a command failed
USAGE_ERROR = 2        # bad arguments; nothing was done
UPDATE_AVAILABLE = 3   # `update --check` (or `update` without --yes and no terminal)
INTERRUPTED = 130

USAGE = f"""\
Usage: {PROG} [command] [options]

With no command, {PROG} opens the terminal app. Commands never open a
screen, so they work in scripts and over plain ssh.

Commands:
  status                    suite version, update status (from the last
                            check, no network) and each installed tool's
                            own status
  list                      installed, uninstalled and available tools
  update                    update the suite and every installed tool to the
                            latest release (in developer mode: the newest
                            commit on its branch); config and state are kept,
                            and an uninstalled tool stays off
      --check               only check for an update, change nothing
      --yes, -y             don't ask before updating
      --ref <branch|tag>    install that branch or tag from GitHub instead,
                            released or not (e.g. --ref main to test
                            unreleased changes); plain `update` goes back
                            to the latest release
  install <tool>...         install tools from the installed release
      --yes, -y             don't ask first
  uninstall <tool>          run the tool's uninstall.sh; its config is kept
                            and updates won't turn it back on
      --purge               also delete its folder, config and state
      --yes, -y             don't ask first
  <tool> <command...>       run a tool's own command, e.g.
                              {PROG} smart-scheduler status
                              {PROG} smart-scheduler run --dry-run
                            (`{PROG} <tool> --help` lists that tool's commands)

Options:
  -h, --help                show this help
  --version                 show the installed suite version

Exit codes:
  0    ok (for update --check: up to date)
  1    a problem: a tool's status found one, a script failed, a tool isn't
       installed, or GitHub couldn't be reached
  2    usage error (unknown command or option, missing argument, or a
       question with no terminal to ask on: pass --yes); nothing was done
  3    an update is available (update --check, or update without --yes and
       no terminal)
  130  interrupted
  A tool's own command exits with that tool's code.

Colors only on a terminal; NO_COLOR turns them off. Changes need root on
the NAS.
"""

CLI_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class UsageError(Exception):
    pass


class Problem(Exception):
    pass


class Style:
    def __init__(self, stream):
        on = (hasattr(stream, "isatty") and stream.isatty()
              and os.environ.get("TERM", "dumb") != "dumb" and not os.environ.get("NO_COLOR"))
        codes = {"bold": "1", "dim": "2", "green": "32", "yellow": "33", "red": "31"}
        for name, code in codes.items():
            setattr(self, name, (lambda c: lambda s: f"\033[{c}m{s}\033[0m")(code) if on
                    else (lambda s: s))


class CLI:
    """One invocation. Tests pass a temp home, a fake Updater, string streams
    and `ask` instead of a terminal."""

    def __init__(self, home, out, err, updater=None, ask=None, root_check=True):
        self.home = os.path.abspath(home)
        self.out, self.err = out, err
        self.up = updater or U.Updater(self.home)
        self.ask = ask if ask is not None else _tty_ask
        self.root_check = root_check
        self.c = Style(out)
        self.e = Style(err)

    # ------------------------------------------------------------- output
    def say(self, text=""):
        print(text, file=self.out)

    def warn(self, text):
        print(f"{self.e.red(PROG + ':')} {text}", file=self.err)

    def confirm(self, question, yes):
        if yes:
            return True
        if self.ask is False:
            raise UsageError(f"no terminal to ask on: {question} (pass --yes)")
        answer = self.ask(f"{question} [y/N] ")
        return (answer or "").strip().lower() in ("y", "yes")

    def need_root(self):
        """Changes on the real install need root."""
        if self.root_check and self.home == I.DEFAULT_HOME and hasattr(os, "geteuid") \
                and os.geteuid() != 0:
            raise Problem("run this as root")

    # ------------------------------------------------------------- tools
    def manifest(self, name, conf="tool.conf"):
        try:
            values, _ = P.read_keyvalue(os.path.join(self.home, name, conf))
        except OSError:
            return {}
        return values

    def tool_cli(self, name):
        """The tool's CLI path from tool.conf, or None."""
        cli = self.manifest(name).get("CLI", "")
        if not CLI_RE.match(cli) or cli.startswith("."):
            return None
        path = os.path.join(self.home, name, cli)
        return path if os.path.isfile(path) else None

    def run_cli(self, path, args, capture):
        """Run a tool's CLI. With capture its output goes to self.out (status);
        else it gets the terminal (passthrough). Returns the exit code."""
        cmd = ([sys.executable, path] if path.endswith(".py") else ["sh", path]) + list(args)
        try:
            if not capture:
                self.out.flush()
                return subprocess.run(cmd, cwd=os.path.dirname(path)).returncode
            p = subprocess.run(cmd, cwd=os.path.dirname(path), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as e:
            raise Problem(f"couldn't run {os.path.basename(path)}: {e}") from None
        text = p.stdout.decode("utf-8", "replace")
        if text:
            self.out.write(text if text.endswith("\n") else text + "\n")
        return p.returncode

    def show_script_output(self, text):
        for line in (text or "").rstrip("\n").splitlines():
            self.say(f"    {line}")

    # ------------------------------------------------------------- commands
    def version_text(self, missing):
        ref = U.installed_ref(self.home)
        ver = self.up.current() or missing
        notes = []
        if ref:
            notes.append(f"from {U.commit_label(ref, U.installed_commit(self.home))}, not a release")
        branch = U.dev_branch(self.home)
        if branch:
            notes.append(f"developer mode, following {branch}")
        return f"{ver} ({'; '.join(notes)})" if notes else ver

    def cmd_version(self):
        self.say(f"{PROG} {self.version_text('unknown')}")
        return OK

    def update_line(self):
        st = U.cached_status(self.home)
        if st is None:
            return self.c.dim(f"never checked for updates (run: {PROG} update --check)")
        if st["error"]:
            return self.c.yellow(f"last update check failed: {st['error']}")
        if st["available"]:
            return self.c.yellow(f"update available: {st['current'] or '?'} -> {st['latest']}"
                                 f" (run: {PROG} update)")
        return self.c.green(f"up to date (checked {st['checked_at']})")

    def cmd_status(self):
        self.say(f"{self.c.bold(PROG)} {self.version_text('unknown version')}")
        self.say(f"  {self.update_line()}")
        tools = I.installed_tools(self.home)
        if not tools:
            self.say("")
            self.say(f"No tools installed yet. Run {PROG} to choose some.")
            return OK
        failed = []
        for name in tools:
            self.say("")
            ver = self.manifest(name).get("VERSION", "")
            self.say(self.c.bold(f"== {name} {ver}".rstrip()))
            cli = self.tool_cli(name)
            if not cli:
                self.say("(no status command)")
                continue
            if self.run_cli(cli, ["status"], capture=True) != 0:
                failed.append(name)
        for name in I.uninstalled_tools(self.home):
            self.say("")
            self.say(self.c.dim(f"== {name}: uninstalled (config kept)"))
        if failed:
            self.say("")
            self.say(self.c.red(f"Problem reported by: {', '.join(failed)}"))
            return PROBLEM
        return OK

    def cmd_list(self):
        rows = I.catalog(self.home)
        rel = I.release_source(self.home)
        self.say(f"{self.c.bold(PROG)} {self.version_text('unknown version')}")
        if not rows:
            self.say("")
            self.say("No tools here yet.")
            return OK
        offered = I.release_tools(rel) if rel else {}
        for name, m, where in rows:
            ver = m.get("VERSION", "")
            if where == "installed":
                state = self.c.green(f"Installed ({ver})" if ver else "Installed")
            elif where == "uninstalled":
                state = self.c.dim("Uninstalled (config kept; updates leave it off)")
            else:
                state = self.c.dim(f"Not installed ({ver})" if ver else "Not installed")
            if where != "available" and rel and name not in offered:
                state += self.c.dim(" - not in the installed release")
            self.say("")
            self.say(f"  {self.c.bold(name)}")
            if m.get("SUMMARY"):
                self.say(f"     {m['SUMMARY']}")
            self.say(f"     {state}")
        if not rel:
            self.say("")
            self.say(self.c.dim("(No release kept here, so only installed tools are listed;"
                                f" {PROG} update keeps one.)"))
        return OK

    def cmd_update(self, check_only, yes):
        st = self.up.check(force=True)
        cur = st["current"] or "?"
        if st["error"]:
            raise Problem(st["error"])
        if not st["available"]:
            if not check_only:
                self.need_root()
                if self.up.link:
                    I.restore_link(self.home, self.up.link)
            self.say(f"Everything is up to date ({cur}).")
            return OK
        self.say(f"Update available: {cur} -> {self.c.bold(st['latest'])}")
        tools = I.installed_tools(self.home)
        if tools:
            self.say(f"  reinstalls: {', '.join(tools)} (config and state are kept)")
        if check_only:
            return UPDATE_AVAILABLE
        self.need_root()
        if not yes and self.ask is False:
            self.say(f"Not updating without a terminal to ask on; run: {PROG} update --yes")
            return UPDATE_AVAILABLE
        if not self.confirm(f"Update to {st['latest']}?", yes):
            self.say("Cancelled.")
            return OK
        self.say(f"Downloading {st['latest']} ...")
        try:
            report = self.up.update_to(st)
        except (OSError, LookupError, I.InstallError) as e:
            raise Problem(f"update failed: {e}") from None
        return self.report(report)

    def cmd_update_ref(self, ref, yes):
        """Install a branch or tag from GitHub, release or not: for testing
        unreleased changes. A later plain `update` goes back to releases."""
        if not U.valid_ref(ref):
            raise UsageError(f"invalid --ref {ref!r}")
        self.need_root()
        tools = I.installed_tools(self.home)
        self.say(f"Install {self.c.bold(ref)} from GitHub over {self.up.current() or '?'}.")
        self.say(self.c.yellow("  This can be unreleased development code. "
                               f"`{PROG} update` goes back to the latest release."))
        if tools:
            self.say(f"  reinstalls: {', '.join(tools)} (config and state are kept)")
        if not yes and self.ask is False:
            self.say(f"Not updating without a terminal to ask on; run: {PROG} update --ref {ref} --yes")
            return UPDATE_AVAILABLE
        if not self.confirm(f"Install {ref}?", yes):
            self.say("Cancelled.")
            return OK
        self.say(f"Downloading {ref} ...")
        try:
            try:   # a branch: install its newest commit and remember which
                sha, _ = self.up.fetch_head(ref)
            except (LookupError, OSError):
                sha = None   # a tag or a commit: GitHub's archive takes it as is
            if sha:
                report = self.up.update(sha, ref=True, label=ref, commit=sha)
            else:
                report = self.up.update(ref, ref=True)
        except (OSError, LookupError, I.InstallError) as e:
            raise Problem(f"update failed: {e}") from None
        return self.report(report)

    def report(self, report):
        self.say("")
        self.say(self.c.bold(f"Updated {report['from'] or '?'} -> {report['to']}."))
        failed = False
        for name, ok, out in report["tools"]:
            if ok:
                self.say(f"  {self.c.green('OK')}      {name}")
            else:
                failed = True
                self.say(f"  {self.c.red('FAILED')}  {name}")
                self.show_script_output(out)
        for name in report["skipped"]:
            self.say(f"  {self.c.dim('SKIPPED')} {name} (not in this release; left as it is)")
        return PROBLEM if failed else OK

    def cmd_install(self, names, yes):
        if not names:
            raise UsageError(f"which tool? usage: {PROG} install <tool>... [--yes]")
        for n in names:
            if not I.valid_name(n):
                raise UsageError(f"bad tool name {n!r}")
        self.need_root()
        rel = I.release_tools(I.release_source(self.home))
        if rel:
            missing = [n for n in names if n not in rel]
            if missing:
                raise Problem(f"not in the installed release: {', '.join(missing)}")
            setups = [(n, rel[n].get("SETUP")) for n in names
                      if rel[n].get("SETUP") and n not in I.installed_tools(self.home)]
            for n, setup in setups:
                self.say(f"Before installing {n}: {setup}")
        if not self.confirm(f"Install {', '.join(names)}?", yes):
            self.say("Cancelled.")
            return OK
        failed = False
        for n in names:
            try:
                ok, out = self.up.install_tool(n)
            except (OSError, I.InstallError) as e:
                ok, out = False, f"{e}\n"
            self.say(f"  {self.c.green('OK') if ok else self.c.red('FAILED')}"
                     f"{'      ' if ok else '  '}{n}")
            if not ok:
                failed = True
                self.show_script_output(out)
        return PROBLEM if failed else OK

    def cmd_uninstall(self, name, purge, yes):
        if not name:
            raise UsageError(f"which tool? usage: {PROG} uninstall <tool> [--purge] [--yes]")
        if not I.valid_name(name):
            raise UsageError(f"bad tool name {name!r}")
        if name not in I.installed_tools(self.home):
            raise Problem(f"{name} isn't installed")
        self.need_root()
        q = (f"Uninstall {name} and delete its config and state?" if purge
             else f"Uninstall {name}? (config is kept; add --purge to delete it)")
        if not self.confirm(q, yes):
            self.say("Cancelled.")
            return OK
        try:
            ok, out = self.up.uninstall_tool(name, purge=purge)
        except (OSError, I.InstallError) as e:
            raise Problem(str(e)) from None
        if out and out.strip():
            self.out.write(out if out.endswith("\n") else out + "\n")
        if not ok:
            self.warn(f"{name}'s uninstall.sh failed")
            return PROBLEM
        self.say(f"{name} uninstalled" + ("" if purge else
                 f"; config kept. {PROG} install {name} turns it back on."))
        return OK

    def cmd_tool(self, name, args):
        cli = self.tool_cli(name)
        if not cli:
            raise Problem(f"{name} has no commands to run")
        return self.run_cli(cli, args, capture=self.out is not sys.stdout)

    # ------------------------------------------------------------- dispatch
    def run(self, argv):
        if not argv:
            raise AssertionError("no arguments: the caller opens the TUI")
        first, rest = argv[0], argv[1:]
        if first in ("-h", "--help", "help"):
            self.say(USAGE.rstrip("\n"))
            return OK
        if first == "--version":
            return self.cmd_version()
        commands = {"status", "list", "update", "install", "uninstall"}
        if first in commands:
            ref = None
            if first == "update":
                ref, rest = _take_value(rest, "--ref")
            flags, pos = _split(rest)
            if "-h" in flags or "--help" in flags:
                self.say(USAGE.rstrip("\n"))
                return OK
            allowed = {"status": set(), "list": set(), "update": {"--check", "--yes", "-y"},
                       "install": {"--yes", "-y"}, "uninstall": {"--purge", "--yes", "-y"}}[first]
            bad = [f for f in flags if f not in allowed]
            if bad:
                raise UsageError(f"unknown option {bad[0]} for {first} (try --help)")
            yes = "--yes" in flags or "-y" in flags
            if first in ("status", "list", "update") and pos:
                raise UsageError(f"unexpected argument {pos[0]} (try --help)")
            if first == "status":
                return self.cmd_status()
            if first == "list":
                return self.cmd_list()
            if first == "update":
                if ref is not None:
                    if "--check" in flags:
                        raise UsageError("--ref and --check don't go together (try --help)")
                    return self.cmd_update_ref(ref, yes)
                return self.cmd_update("--check" in flags, yes)
            if first == "install":
                return self.cmd_install(pos, yes)
            if len(pos) > 1:
                raise UsageError(f"unexpected argument {pos[1]} (try --help)")
            return self.cmd_uninstall(pos[0] if pos else "", "--purge" in flags, yes)
        if first.startswith("-"):
            raise UsageError(f"unknown option {first} (try --help)")
        if I.valid_name(first) and first in I.installed_tools(self.home):
            return self.cmd_tool(first, rest)
        if I.valid_name(first) and first in I.uninstalled_tools(self.home):
            raise Problem(f"{first} is uninstalled; {PROG} install {first} turns it back on")
        raise UsageError(f"unknown command or tool {first!r} (try --help)")


def _take_value(args, name):
    """Pull `name value` or `name=value` out of args. Returns (value or None, rest)."""
    rest, value, i = [], None, 0
    while i < len(args):
        a = args[i]
        if a == name:
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                raise UsageError(f"{name} needs a value (try --help)")
            value, i = args[i + 1], i + 2
            continue
        if a.startswith(name + "="):
            value = a[len(name) + 1:]
            if not value:
                raise UsageError(f"{name} needs a value (try --help)")
        else:
            rest.append(a)
        i += 1
    return value, rest


def _split(args):
    flags, pos = [], []
    for a in args:
        if a == "--":
            continue
        (flags if a.startswith("-") else pos).append(a)
    return flags, pos


def _tty_ask(question):
    """Ask on the terminal; UsageError when there isn't one."""
    # Separate read and write handles: a terminal can't be opened "r+" in
    # text mode (Python wants it seekable and raises UnsupportedOperation).
    try:
        with open("/dev/tty", "w") as out, open("/dev/tty") as inp:
            out.write(question)
            out.flush()
            return inp.readline()
    except OSError:
        raise UsageError(f"no terminal to ask on: {question.strip()} (pass --yes)") from None


def _has_tty():
    try:
        with open("/dev/tty"):
            return True
    except OSError:
        return False


def default_home():
    return os.environ.get("BETTER_UNAS_HOME") or ROOT


def main(argv=None, out=None, err=None, home=None, updater=None, ask=None, tui=None):
    """Entry point. No arguments: the terminal app. Returns the exit code."""
    argv = sys.argv[1:] if argv is None else list(argv)
    out, err = out or sys.stdout, err or sys.stderr
    home = home or default_home()
    if not argv:
        if tui is None:
            from .tui import __main__ as tui_main
            tui = tui_main.main
        return tui(["--home", home] if os.path.abspath(home) != ROOT else [])
    if ask is None:
        ask = _tty_ask if _has_tty() else False
    cli = CLI(home, out, err, updater=updater, ask=ask)
    try:
        return cli.run(argv)
    except UsageError as e:
        cli.warn(str(e))
        return USAGE_ERROR
    except Problem as e:
        cli.warn(str(e))
        return PROBLEM
    except KeyboardInterrupt:
        print(file=err)
        return INTERRUPTED
