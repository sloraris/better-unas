"""The plain `better-unas` commands (#16): output and exit codes.

    python3 -m unittest discover -s core/tests

Homes are built in temp folders from the fake releases in fakerelease.py
(file:// URLs, real sh scripts); nothing touches the network or a NAS.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from core import cli as C           # noqa: E402
from core import install as I       # noqa: E402
from core import updates as U       # noqa: E402
from fakerelease import FakeGitHub, tool_files  # noqa: E402

STATUS_PY = """import sys
args = sys.argv[1:]
if args[:1] == ["status"]:
    print("{name}: all good" if {ok} else "{name}: drive sda failed its test")
    sys.exit(0 if {ok} else 1)
print("{name} got " + " ".join(args))
sys.exit(7 if "--fail" in args else 0)
"""


def tools(alpha_ok=True, beta_ok=True, beta_setup=""):
    def t(name, summary, ok, extra=None):
        f = tool_files(name, summary, extra)
        f[f"{name}.py"] = STATUS_PY.format(name=name, ok=ok)
        return f
    beta = t("beta", "Beta tool for the update tests", beta_ok)
    if beta_setup:
        beta["tool.conf"] += f"SETUP={beta_setup}\n"
    gamma = t("gamma", "Gamma tool, never installed", True)
    gamma["tool.conf"] = gamma["tool.conf"].replace("CLI=gamma.py\n", "")
    return {"alpha": t("alpha", "Alpha tool for the update tests", alpha_ok),
            "beta": beta, "gamma": gamma}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "persistent", "better-unas")
        self.bin = os.path.join(self.tmp, "usr-local-bin")
        os.makedirs(self.bin)
        self.link = os.path.join(self.bin, "better-unas")
        self.gh = FakeGitHub(os.path.join(self.tmp, "github"))
        self.up = U.Updater(self.home, api_url=self.gh.api_url, tarball_url=self.gh.tarball_url,
                            link=self.link)
        for k in ("NO_COLOR", "BETTER_UNAS_HOME"):
            os.environ.pop(k, None)

    def installed_at(self, version, names=("alpha", "beta"), **kw):
        self.gh.publish(version, tools(**kw))
        work = self.up.fetch_release(version)
        try:
            I.install_core(work, self.home)
            for t in names:
                ok, out = I.install_tool(I.release_source(self.home), self.home, t)
                self.assertTrue(ok, out)
        finally:
            shutil.rmtree(work)

    def run_cli(self, *argv, ask=False, answers=None):
        out, err = io.StringIO(), io.StringIO()
        if answers is not None:
            it = iter(answers)
            asked = []

            def ask(q):
                asked.append(q)
                return next(it)
            self.asked = asked
        code = C.main(list(argv), out=out, err=err, home=self.home, updater=self.up, ask=ask)
        return code, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------ help, version, usage

class HelpTests(Base):
    def test_help_documents_every_command_and_exit_code(self):
        for flag in ("--help", "-h", "help"):
            code, out, err = self.run_cli(flag)
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
        for word in ("status", "list", "update", "--check", "--yes", "install <tool>",
                     "uninstall <tool>", "--purge", "<tool> <command...>",
                     "smart-scheduler run --dry-run", "--version", "NO_COLOR",
                     "0 ", "1 ", "2 ", "3 ", "130"):
            self.assertIn(word, out)

    def test_help_after_a_command(self):
        code, out, _ = self.run_cli("update", "--help")
        self.assertEqual(code, 0)
        self.assertIn("Exit codes:", out)

    def test_version(self):
        self.installed_at("v0.2.0")
        code, out, _ = self.run_cli("--version")
        self.assertEqual((code, out), (0, "better-unas v0.2.0\n"))

    def test_usage_errors_exit_2(self):
        self.installed_at("v0.2.0")
        for argv, msg in [(["frobnicate"], "unknown command or tool 'frobnicate'"),
                          (["--frob"], "unknown option --frob"),
                          (["status", "extra"], "unexpected argument extra"),
                          (["update", "--purge"], "unknown option --purge for update"),
                          (["uninstall"], "which tool?"),
                          (["uninstall", "alpha", "beta"], "unexpected argument beta"),
                          (["install"], "which tool?"),
                          (["uninstall", "Bad_Name"], "bad tool name")]:
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, C.USAGE_ERROR, argv)
            self.assertIn(msg, err)
            self.assertTrue(err.startswith("better-unas: "))

    def test_no_arguments_opens_the_tui(self):
        calls = []
        code = C.main([], home=C.ROOT, tui=lambda argv: calls.append(argv) or 0)
        self.assertEqual((code, calls), (0, [[]]))
        code = C.main([], home=self.home, tui=lambda argv: calls.append(argv) or 5)
        self.assertEqual((code, calls[-1]), (5, ["--home", self.home]))

    def test_no_arguments_default_is_the_real_tui_main(self):
        with mock.patch("core.tui.__main__.main", return_value=0) as m:
            self.assertEqual(C.main([], home=C.ROOT), 0)
        m.assert_called_once_with([])


# ------------------------------------------------------------------ status

class StatusTests(Base):
    def test_all_good(self):
        self.installed_at("v0.2.0")
        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0, out + err)
        self.assertIn("better-unas v0.2.0", out)
        self.assertIn("never checked for updates", out)
        self.assertIn("== alpha 0.9.0", out)
        self.assertIn("alpha: all good", out)
        self.assertIn("beta: all good", out)
        self.assertNotIn("\033[", out)

    def test_a_problem_exits_1(self):
        self.installed_at("v0.2.0", beta_ok=False)
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, C.PROBLEM)
        self.assertIn("beta: drive sda failed its test", out)
        self.assertIn("Problem reported by: beta", out)

    def test_shows_cached_update_and_uninstalled_tools(self):
        self.installed_at("v0.2.0")
        self.gh.publish("v0.3.0", tools())
        self.up.check(force=True)
        I.uninstall_tool(self.home, "beta")
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("update available: v0.2.0 -> v0.3.0", out)
        self.assertIn("== beta: uninstalled (config kept)", out)

    def test_tool_without_cli(self):
        self.installed_at("v0.2.0", names=("gamma",))
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("(no status command)", out)

    def test_nothing_installed(self):
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("No tools installed yet", out)


# ------------------------------------------------------------------ list

class ListTests(Base):
    def test_lists_installed_uninstalled_and_available(self):
        self.installed_at("v0.2.0")
        I.uninstall_tool(self.home, "beta")
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("alpha\n     Alpha tool for the update tests\n     Installed (0.9.0)", out)
        self.assertIn("beta\n     Beta tool for the update tests\n     Uninstalled", out)
        self.assertIn("gamma\n     Gamma tool, never installed\n     Not installed (0.9.0)", out)

    def test_without_a_kept_release(self):
        self.installed_at("v0.2.0", names=("alpha",))
        shutil.rmtree(os.path.join(self.home, I.RELEASE_DIR))
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("alpha", out)
        self.assertNotIn("gamma", out)
        self.assertIn("No release kept here", out)


# ------------------------------------------------------------------ update

class UpdateTests(Base):
    def test_up_to_date(self):
        self.installed_at("v0.2.0")
        code, out, _ = self.run_cli("update")
        self.assertEqual(code, 0)
        self.assertIn("Everything is up to date (v0.2.0).", out)
        self.assertTrue(os.path.islink(self.link), "update restores the short command")

    def test_check_only(self):
        self.installed_at("v0.2.0")
        self.assertEqual(self.run_cli("update", "--check")[0], 0)
        self.gh.publish("v0.3.0", tools())
        code, out, _ = self.run_cli("update", "--check")
        self.assertEqual(code, C.UPDATE_AVAILABLE)
        self.assertIn("Update available: v0.2.0 -> v0.3.0", out)
        self.assertIn("reinstalls: alpha, beta", out)
        self.assertEqual(U.suite_version(self.home), "v0.2.0")

    def test_without_yes_and_no_terminal_does_nothing(self):
        self.installed_at("v0.2.0")
        self.gh.publish("v0.3.0", tools())
        code, out, _ = self.run_cli("update")
        self.assertEqual(code, C.UPDATE_AVAILABLE)
        self.assertIn("update --yes", out)
        self.assertEqual(U.suite_version(self.home), "v0.2.0")

    def test_asks_and_cancel(self):
        self.installed_at("v0.2.0")
        self.gh.publish("v0.3.0", tools())
        code, out, _ = self.run_cli("update", answers=["n\n"])
        self.assertEqual(code, 0)
        self.assertIn("Cancelled.", out)
        self.assertEqual(self.asked, ["Update to v0.3.0? [y/N] "])
        self.assertEqual(U.suite_version(self.home), "v0.2.0")

    def test_yes_updates_and_keeps_config(self):
        self.installed_at("v0.2.0")
        cfg = os.path.join(self.home, "alpha", "config")
        with open(cfg, "w") as f:
            f.write("ENABLED=no\n")
        I.uninstall_tool(self.home, "beta")
        self.gh.publish("v0.3.0", tools())
        code, out, err = self.run_cli("update", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Updated v0.2.0 -> v0.3.0.", out)
        self.assertIn("OK      alpha", out)
        self.assertEqual(U.suite_version(self.home), "v0.3.0")
        with open(cfg) as f:
            self.assertEqual(f.read(), "ENABLED=no\n")
        self.assertFalse(os.path.exists(os.path.join(self.home, "beta", "tool.conf")),
                         "an uninstalled tool stays off")

    def test_a_failing_tool_exits_1(self):
        self.installed_at("v0.2.0")
        bad = tools()
        bad["beta"]["install.sh"] = "#!/bin/sh\necho 'beta: missing smartctl'\nexit 1\n"
        self.gh.publish("v0.3.0", bad)
        code, out, _ = self.run_cli("update", "-y")
        self.assertEqual(code, C.PROBLEM)
        self.assertIn("FAILED  beta", out)
        self.assertIn("    beta: missing smartctl", out)

    def test_github_unreachable_exits_1(self):
        self.installed_at("v0.2.0")
        os.remove(os.path.join(self.gh.folder, "latest.json"))
        code, out, err = self.run_cli("update", "--check")
        self.assertEqual(code, C.PROBLEM)
        self.assertIn("couldn't reach GitHub", err)

    def test_root_needed_on_the_real_home(self):
        cli = C.CLI(I.DEFAULT_HOME, io.StringIO(), io.StringIO(), updater=self.up, ask=False)
        with mock.patch("os.geteuid", return_value=1000):
            with self.assertRaises(C.Problem):
                cli.need_root()
        with mock.patch("os.geteuid", return_value=0):
            cli.need_root()


# ------------------------------------------------------------------ install / uninstall

class InstallUninstallTests(Base):
    def test_uninstall_needs_yes_without_a_terminal(self):
        self.installed_at("v0.2.0")
        code, _, err = self.run_cli("uninstall", "alpha")
        self.assertEqual(code, C.USAGE_ERROR)
        self.assertIn("pass --yes", err)
        self.assertIn("alpha", I.installed_tools(self.home))

    def test_uninstall_keeps_config(self):
        self.installed_at("v0.2.0")
        code, out, _ = self.run_cli("uninstall", "alpha", answers=["y\n"])
        self.assertEqual(code, 0)
        self.assertIn("config is kept", self.asked[0])
        self.assertIn("alpha uninstalled", out)
        self.assertEqual(I.uninstalled_tools(self.home), ["alpha"])
        self.assertTrue(os.path.isfile(os.path.join(self.home, "alpha", "config")))

    def test_uninstall_purge(self):
        self.installed_at("v0.2.0")
        code, out, _ = self.run_cli("uninstall", "beta", "--purge", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Deleted beta.", out)
        self.assertFalse(os.path.exists(os.path.join(self.home, "beta")))

    def test_uninstall_not_installed_exits_1(self):
        self.installed_at("v0.2.0")
        code, _, err = self.run_cli("uninstall", "gamma", "--yes")
        self.assertEqual(code, C.PROBLEM)
        self.assertIn("gamma isn't installed", err)

    def test_uninstall_script_failing_exits_1(self):
        self.installed_at("v0.2.0")
        with open(os.path.join(self.home, "alpha", "uninstall.sh"), "w") as f:
            f.write("echo nope\nexit 1\n")
        code, out, err = self.run_cli("uninstall", "alpha", "-y")
        self.assertEqual(code, C.PROBLEM)
        self.assertIn("nope", out)
        self.assertIn("uninstall.sh failed", err)
        self.assertIn("alpha", I.installed_tools(self.home))

    def test_install_and_reinstall_after_uninstall(self):
        self.installed_at("v0.2.0", names=("alpha",), beta_setup="Turn on SSH first")
        code, out, _ = self.run_cli("install", "beta", answers=["y\n"])
        self.assertEqual(code, 0, out)
        self.assertIn("Before installing beta: Turn on SSH first", out)
        self.assertIn("OK      beta", out)
        self.assertIn("beta", I.installed_tools(self.home))
        I.uninstall_tool(self.home, "alpha")
        code, out, _ = self.run_cli("install", "alpha", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("alpha", I.installed_tools(self.home))

    def test_install_unknown_tool_exits_1(self):
        self.installed_at("v0.2.0")
        code, _, err = self.run_cli("install", "delta", "--yes")
        self.assertEqual(code, C.PROBLEM)
        self.assertIn("not in the installed release: delta", err)


# ------------------------------------------------------------------ passthrough

class PassthroughTests(Base):
    def test_runs_the_tools_cli_with_its_arguments(self):
        self.installed_at("v0.2.0")
        code, out, _ = self.run_cli("alpha", "run", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(out, "alpha got run --dry-run\n")

    def test_exit_code_is_the_tools(self):
        self.installed_at("v0.2.0")
        self.assertEqual(self.run_cli("alpha", "run", "--fail")[0], 7)
        self.assertEqual(self.run_cli("beta", "status")[0], 0)

    def test_tool_options_are_not_ours(self):
        self.installed_at("v0.2.0")
        code, out, _ = self.run_cli("alpha", "--help", "--yes")
        self.assertEqual((code, out), (0, "alpha got --help --yes\n"))

    def test_tool_without_cli_exits_1(self):
        self.installed_at("v0.2.0", names=("gamma",))
        code, _, err = self.run_cli("gamma", "status")
        self.assertEqual(code, C.PROBLEM)
        self.assertIn("gamma has no commands to run", err)

    def test_uninstalled_tool_exits_1(self):
        self.installed_at("v0.2.0")
        I.uninstall_tool(self.home, "alpha")
        code, _, err = self.run_cli("alpha", "status")
        self.assertEqual(code, C.PROBLEM)
        self.assertIn("alpha is uninstalled", err)

    def test_cli_path_must_stay_in_the_tool_folder(self):
        self.installed_at("v0.2.0")
        conf = os.path.join(self.home, "alpha", "tool.conf")
        with open(conf) as f:
            text = f.read()
        with open(conf, "w") as f:
            f.write(text.replace("CLI=alpha.py", "CLI=../beta/beta.py"))
        self.assertEqual(self.run_cli("alpha", "status")[0], C.PROBLEM)


# ------------------------------------------------------------------ colors and the entry point

class ColorTests(Base):
    class TTY(io.StringIO):
        def isatty(self):
            return True

    def test_color_only_on_a_tty_and_not_with_no_color(self):
        self.installed_at("v0.2.0")
        for env, colored in [({"TERM": "xterm"}, True), ({"TERM": "xterm", "NO_COLOR": "1"}, False),
                             ({"TERM": "dumb"}, False)]:
            with mock.patch.dict(os.environ, env):
                out = self.TTY()
                C.main(["list"], out=out, err=io.StringIO(), home=self.home, updater=self.up,
                       ask=False)
                self.assertEqual("\033[" in out.getvalue(), colored, env)


class EntryPointTests(Base):
    def entry(self, *args, env=None):
        e = dict(os.environ, BETTER_UNAS_HOME=self.home, **(env or {}))
        return subprocess.run([sys.executable, os.path.join(ROOT, "better-unas"), *args],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=e)

    def test_entry_point_runs_from_a_link(self):
        link = os.path.join(self.bin, "better-unas")
        os.symlink(os.path.join(ROOT, "better-unas"), link)
        p = subprocess.run([sys.executable, link, "--version"], stdout=subprocess.PIPE,
                           text=True, env=dict(os.environ, BETTER_UNAS_HOME=ROOT))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, f"better-unas {U.suite_version(ROOT)}\n")

    def test_passthrough_and_exit_codes_through_the_entry_point(self):
        self.installed_at("v0.2.0", beta_ok=False)
        p = self.entry("alpha", "run", "--fail")
        self.assertEqual((p.returncode, p.stdout), (7, "alpha got run --fail\n"))
        self.assertEqual(self.entry("status").returncode, 1)
        self.assertEqual(self.entry("nope").returncode, 2)


if __name__ == "__main__":
    unittest.main()
