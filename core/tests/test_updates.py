"""Suite version, update check, update, install/uninstall and remove (#14).

    python3 -m unittest discover -s core/tests

Releases come from a fake GitHub in a temp folder (file:// URLs, see
fakerelease.py); tool scripts are real sh. Nothing touches the network or
anything outside the temp folders.
"""

import datetime as dt
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from core import install as I       # noqa: E402
from core import updates as U       # noqa: E402
from fakerelease import FakeGitHub, tool_files  # noqa: E402

T0 = dt.datetime(2026, 11, 22, 1, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7)))


def read(path):
    with open(path) as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


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
                            link=self.link, commit_url=self.gh.commit_url,
                            branches_url=self.gh.branches_url)

    def installed_at(self, version, tools=("alpha", "beta")):
        """A home installed from release `version` with these tools."""
        self.gh.publish(version)
        work = self.up.fetch_release(version)
        try:
            I.install_core(work, self.home)
            for t in tools:
                ok, out = I.install_tool(I.release_source(self.home), self.home, t)
                self.assertTrue(ok, out)
            I.restore_link(self.home, self.link)
        finally:
            shutil.rmtree(work)
        return self.home


# ------------------------------------------------------------------ versions

class VersionTests(unittest.TestCase):
    def test_repo_has_one_suite_version(self):
        v = U.suite_version(ROOT)
        self.assertIsNotNone(U.parse_version(v), f"VERSION at the repo root: {v!r}")
        self.assertTrue(v.startswith("v"))

    def test_parse_and_compare(self):
        self.assertEqual(U.parse_version("v0.2.0"), (0, 2, 0))
        self.assertEqual(U.parse_version("1.10"), (1, 10))
        self.assertIsNone(U.parse_version("main"))
        self.assertTrue(U.is_newer("v0.10.0", "v0.9.1"))
        self.assertTrue(U.is_newer("v1.0", "v0.9.9"))
        self.assertFalse(U.is_newer("v0.2.0", "v0.2.0"))
        self.assertFalse(U.is_newer("v0.2", "v0.2.0"))
        self.assertFalse(U.is_newer("v0.1.0", "v0.2.0"))
        self.assertTrue(U.is_newer("v0.2.0", "main"))      # a branch install: offer the release
        self.assertTrue(U.is_newer("v0.2.0", ""))
        self.assertFalse(U.is_newer(None, "v0.2.0"))


# ------------------------------------------------------------------ the check

class CheckTests(Base):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.home, "VERSION"), "v0.2.0\n")
        self.calls = 0

    def fetch(self, tag="v0.3.0"):
        def f():
            self.calls += 1
            if isinstance(tag, Exception):
                raise tag
            return tag
        return f

    def test_never_checked_is_none(self):
        self.assertIsNone(U.cached_status(self.home))

    def test_update_available_is_cached(self):
        s = U.check(self.home, now=T0, fetch=self.fetch())
        self.assertEqual(s, {"current": "v0.2.0", "latest": "v0.3.0", "available": True,
                             "checked_at": "2026-11-22T01:00:00-07:00", "error": None,
                             "branch": None, "latest_ref": "v0.3.0", "latest_date": None})
        self.assertEqual(U.cached_status(self.home), s)
        # Fresh for CHECK_HOURS: no second request.
        U.check(self.home, now=T0 + dt.timedelta(hours=U.CHECK_HOURS - 1), fetch=self.fetch())
        self.assertEqual(self.calls, 1)
        U.check(self.home, now=T0 + dt.timedelta(hours=U.CHECK_HOURS), fetch=self.fetch())
        self.assertEqual(self.calls, 2)
        U.check(self.home, force=True, now=T0 + dt.timedelta(hours=U.CHECK_HOURS), fetch=self.fetch())
        self.assertEqual(self.calls, 3)

    def test_no_update(self):
        s = U.check(self.home, now=T0, fetch=self.fetch("v0.2.0"))
        self.assertFalse(s["available"])
        self.assertEqual(s["latest"], "v0.2.0")
        self.assertIsNone(s["error"])

    def test_cached_status_follows_version_file(self):
        U.check(self.home, now=T0, fetch=self.fetch())
        write(os.path.join(self.home, "VERSION"), "v0.3.0\n")   # as after an update
        s = U.cached_status(self.home)
        self.assertEqual((s["current"], s["available"]), ("v0.3.0", False))

    def test_errors_are_cached_briefly(self):
        s = U.check(self.home, now=T0, fetch=self.fetch(OSError("no route to host")))
        self.assertIsNone(s["latest"])
        self.assertFalse(s["available"])
        self.assertIn("no route to host", s["error"])
        U.check(self.home, now=T0 + dt.timedelta(minutes=10), fetch=self.fetch(OSError("x")))
        self.assertEqual(self.calls, 1)
        s = U.check(self.home, now=T0 + dt.timedelta(minutes=U.ERROR_RETRY_MINUTES), fetch=self.fetch())
        self.assertEqual(self.calls, 2)
        self.assertTrue(s["available"])

    def test_no_release_yet(self):
        s = U.check(self.home, now=T0, fetch=self.fetch(LookupError("no release has been published yet")))
        self.assertEqual(s["error"], "no release has been published yet")

    def test_broken_cache_counts_as_never_checked(self):
        write(os.path.join(self.home, U.CACHE), "{not json")
        self.assertIsNone(U.cached_status(self.home))
        write(os.path.join(self.home, U.CACHE), json.dumps({"latest": "v9"}))
        self.assertIsNone(U.cached_status(self.home))

    def test_fetch_latest_from_fake_github(self):
        self.gh.publish("v0.3.0")
        self.assertEqual(U.fetch_latest(self.gh.api_url), "v0.3.0")
        s = self.up.check(now=T0)
        self.assertTrue(s["available"])

    def test_unreachable_github(self):
        s = U.Updater(self.home, api_url="file:///nonexistent/latest.json").check(now=T0)
        self.assertTrue(s["error"].startswith("couldn't reach GitHub"))

    def test_bad_answer(self):
        write(os.path.join(self.gh.folder, "latest.json"), json.dumps({"tag_name": "v1; rm -rf /"}))
        with self.assertRaises(OSError):
            U.fetch_latest(self.gh.api_url)

    def test_background_check_writes_the_cache(self):
        U.check_in_background(self.home, fetch=self.fetch()).join(5)
        self.assertTrue(U.cached_status(self.home)["available"])


# ------------------------------------------------------------------ download

class DownloadTests(Base):
    def test_download_strips_the_top_folder(self):
        self.gh.publish("v0.3.0")
        work = self.up.fetch_release("v0.3.0")
        self.addCleanup(shutil.rmtree, work)
        self.assertEqual(read(os.path.join(work, "VERSION")), "v0.3.0\n")
        self.assertEqual(sorted(I.release_tools(work)), ["alpha", "beta", "gamma"])

    def test_unsafe_members_are_refused(self):
        for name, kind, target in (("better-unas-0.3.0/../../escape", tarfile.REGTYPE, ""),
                                   ("better-unas-0.3.0/evil", tarfile.SYMTYPE, "/etc/passwd"),
                                   ("better-unas-0.3.0/up", tarfile.SYMTYPE, "../../x")):
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, target
            self.gh.publish("v0.3.1", extra_members=[(info, b"" if kind == tarfile.REGTYPE else None)])
            with self.assertRaises(OSError, msg=name):
                self.up.fetch_release("v0.3.1")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape")))

    def test_missing_release(self):
        with self.assertRaises(OSError):
            self.up.fetch_release("v9.9.9")

    def test_bad_tag(self):
        with self.assertRaises(OSError):
            U.download("../../etc", self.tmp, self.gh.tarball_url)


# ------------------------------------------------------------------ update

class UpdateTests(Base):
    def test_update_keeps_config_and_state(self):
        self.installed_at("v0.2.0")
        alpha = os.path.join(self.home, "alpha")
        write(os.path.join(alpha, "config"), "ENABLED=yes\n# my edits\nWINDOW=01:00-05:00\n")
        write(os.path.join(alpha, "state.json"), '{"tests": 3}')
        write(os.path.join(alpha, ".lock"), "")
        write(os.path.join(alpha, "stale.py"), "removed from the release")
        I.uninstall_tool(self.home, "beta")
        write(os.path.join(self.home, "beta", "config"), "ENABLED=no\n")
        write(os.path.join(self.home, "tui.log"), "keep me\n")
        os.remove(self.link)

        self.gh.publish("v0.3.0")
        self.assertTrue(self.up.check(now=T0)["available"])
        report = self.up.update()

        self.assertEqual(report["from"], "v0.2.0")
        self.assertEqual(report["to"], "v0.3.0")
        self.assertEqual([(n, ok) for n, ok, _ in report["tools"]], [("alpha", True)])
        self.assertIn("alpha installed", report["tools"][0][2])
        self.assertTrue(report["link"])
        self.assertEqual(U.suite_version(self.home), "v0.3.0")
        self.assertIn("v0.3.0", read(os.path.join(self.home, "core", "__init__.py")))
        self.assertEqual(read(os.path.join(self.home, ".release", "VERSION")), "v0.3.0\n")
        # Config, state and lock untouched; the release's files replaced the old ones.
        self.assertEqual(read(os.path.join(alpha, "config")), "ENABLED=yes\n# my edits\nWINDOW=01:00-05:00\n")
        self.assertEqual(read(os.path.join(alpha, "state.json")), '{"tests": 3}')
        self.assertTrue(os.path.exists(os.path.join(alpha, ".lock")))
        self.assertFalse(os.path.exists(os.path.join(alpha, "stale.py")))
        self.assertFalse(os.path.exists(os.path.join(alpha, "new_in_v0.2.0.txt")))
        self.assertTrue(os.path.exists(os.path.join(alpha, "new_in_v0.3.0.txt")))
        # The installer ran again, against the new suite.
        self.assertEqual(read(os.path.join(alpha, "calls.log")).split("\n")[-2], "install v0.3.0")
        # The uninstalled tool stays off and its files aren't touched.
        beta = os.path.join(self.home, "beta")
        self.assertFalse(os.path.exists(os.path.join(beta, "tool.conf")))
        self.assertTrue(os.path.exists(os.path.join(beta, I.UNINSTALLED)))
        self.assertEqual(read(os.path.join(beta, "config")), "ENABLED=no\n")
        self.assertNotIn("install v0.3.0", read(os.path.join(beta, "calls.log")))
        # Gamma was never installed and isn't now; the home's own files stay.
        self.assertFalse(os.path.exists(os.path.join(self.home, "gamma")))
        self.assertEqual(read(os.path.join(self.home, "tui.log")), "keep me\n")
        # The link is back, and points at the entry point.
        self.assertEqual(os.readlink(self.link), os.path.join(self.home, "better-unas"))
        self.assertTrue(os.access(os.path.join(self.home, "better-unas"), os.X_OK))
        # The cached check now says up to date.
        self.assertFalse(U.cached_status(self.home)["available"])
        self.assertEqual(U.cached_status(self.home)["current"], "v0.3.0")
        self.assertFalse([e for e in os.listdir(self.home) if e.endswith((".new", ".old"))])

    def test_no_update_available(self):
        self.installed_at("v0.3.0")
        s = self.up.check(now=T0)
        self.assertEqual((s["current"], s["latest"], s["available"]), ("v0.3.0", "v0.3.0", False))

    def test_tool_not_in_new_release_is_left_alone(self):
        self.installed_at("v0.2.0")
        self.gh.publish("v0.3.0", tools={"alpha": tool_files("alpha", "Alpha")})
        report = self.up.update("v0.3.0")
        self.assertEqual(report["skipped"], ["beta"])
        self.assertTrue(os.path.exists(os.path.join(self.home, "beta", "tool.conf")))

    def test_failing_installer_is_reported(self):
        self.installed_at("v0.2.0", tools=("alpha",))
        self.gh.publish("v0.3.0", tools={"alpha": tool_files("alpha", "Alpha", failing=True)})
        report = self.up.update()
        self.assertEqual([(n, ok) for n, ok, _ in report["tools"]], [("alpha", False)])
        self.assertIn("missing smartctl", report["tools"][0][2])
        self.assertEqual(U.suite_version(self.home), "v0.3.0")

    def test_default_link_only_for_the_real_home(self):
        self.assertIsNone(U.Updater(self.home).link)
        self.assertEqual(U.Updater(I.DEFAULT_HOME).link, I.LINK)


# ------------------------------------------------------------------ tools

class ToolTests(Base):
    def test_single_tool_uninstall_and_reinstall(self):
        self.installed_at("v0.2.0")
        alpha = os.path.join(self.home, "alpha")
        write(os.path.join(alpha, "config"), "ENABLED=yes\nMINE=1\n")
        ok, out = self.up.uninstall_tool("alpha")
        self.assertTrue(ok, out)
        self.assertIn("alpha uninstalled", out)
        self.assertFalse(os.path.exists(os.path.join(alpha, "installed.flag")))
        self.assertFalse(os.path.exists(os.path.join(alpha, "tool.conf")))
        self.assertTrue(os.path.exists(os.path.join(alpha, I.UNINSTALLED)))
        self.assertEqual(read(os.path.join(alpha, "config")), "ENABLED=yes\nMINE=1\n")
        self.assertEqual(I.installed_tools(self.home), ["beta"])
        self.assertEqual(I.uninstalled_tools(self.home), ["alpha"])
        self.assertEqual([(n, w) for n, _, w in I.catalog(self.home)],
                         [("alpha", "uninstalled"), ("beta", "installed"), ("gamma", "available")])
        with self.assertRaises(I.InstallError):
            self.up.uninstall_tool("alpha")
        # Back on, with its config, from the kept release (no download).
        shutil.rmtree(self.gh.folder)
        ok, out = self.up.install_tool("alpha")
        self.assertTrue(ok, out)
        self.assertTrue(os.path.exists(os.path.join(alpha, "tool.conf")))
        self.assertFalse(os.path.exists(os.path.join(alpha, I.UNINSTALLED)))
        self.assertEqual(read(os.path.join(alpha, "config")), "ENABLED=yes\nMINE=1\n")

    def test_install_a_new_tool(self):
        self.installed_at("v0.2.0", tools=())
        ok, out = self.up.install_tool("gamma")
        self.assertTrue(ok, out)
        self.assertEqual(I.installed_tools(self.home), ["gamma"])
        self.assertEqual(read(os.path.join(self.home, "gamma", "config")), "ENABLED=yes\n")
        with self.assertRaises(I.InstallError):
            self.up.install_tool("nope")
        with self.assertRaises(I.InstallError):
            self.up.install_tool("../etc")

    def test_install_without_kept_release_downloads_the_installed_version(self):
        self.installed_at("v0.2.0", tools=())
        shutil.rmtree(os.path.join(self.home, ".release"))   # as installed by the old install.sh
        self.gh.publish("v0.3.0")                            # latest moved on; stay on v0.2.0
        ok, out = self.up.install_tool("gamma")
        self.assertTrue(ok, out)
        self.assertTrue(os.path.exists(os.path.join(self.home, "gamma", "gamma.py")))
        self.assertEqual(read(os.path.join(self.home, ".release", "VERSION")), "v0.2.0\n")
        self.assertEqual(U.suite_version(self.home), "v0.2.0")

    def test_purge(self):
        self.installed_at("v0.2.0")
        ok, out = self.up.uninstall_tool("beta", purge=True)
        self.assertTrue(ok, out)
        self.assertFalse(os.path.exists(os.path.join(self.home, "beta")))
        self.assertEqual([(n, w) for n, _, w in I.catalog(self.home)],
                         [("alpha", "installed"), ("beta", "available"), ("gamma", "available")])

    def test_tool_without_uninstaller(self):
        self.installed_at("v0.2.0", tools=("alpha",))
        os.remove(os.path.join(self.home, "alpha", "uninstall.sh"))
        ok, _ = self.up.uninstall_tool("alpha")
        self.assertTrue(ok)
        self.assertEqual(I.uninstalled_tools(self.home), ["alpha"])


class RemoveTests(Base):
    def test_remove_all_keeps_the_folder(self):
        self.installed_at("v0.2.0")
        write(os.path.join(self.home, "alpha", "config"), "MINE=1\n")
        results = self.up.remove_all()
        self.assertEqual([(n, ok) for n, ok, _ in results], [("alpha", True), ("beta", True)])
        self.assertFalse(os.path.lexists(self.link))
        self.assertEqual(I.installed_tools(self.home), [])
        self.assertEqual(I.uninstalled_tools(self.home), ["alpha", "beta"])
        self.assertEqual(read(os.path.join(self.home, "alpha", "config")), "MINE=1\n")

    def test_remove_all_and_delete(self):
        self.installed_at("v0.2.0")
        self.up.remove_all(delete_home=True)
        self.assertFalse(os.path.exists(self.home))
        self.assertFalse(os.path.lexists(self.link))
        self.assertTrue(os.path.isdir(self.bin))

    def test_link_elsewhere_is_left_alone(self):
        self.installed_at("v0.2.0", tools=())
        os.remove(self.link)
        os.symlink("/opt/other/better-unas", self.link)
        self.up.remove_all()
        self.assertEqual(os.readlink(self.link), "/opt/other/better-unas")

    def test_failed_uninstall_keeps_the_folder(self):
        self.installed_at("v0.2.0", tools=("alpha",))
        write(os.path.join(self.home, "alpha", "uninstall.sh"), "echo cannot; exit 1\n")
        with self.assertRaises(I.InstallError):
            self.up.remove_all(delete_home=True)
        self.assertTrue(os.path.isdir(self.home))

    def test_refuses_to_delete_an_odd_folder(self):
        odd = os.path.join(self.tmp, "photos")
        os.makedirs(odd)
        write(os.path.join(odd, "keep.jpg"), "x")
        with self.assertRaises(I.InstallError):
            I.remove_all(odd, delete_home=True, link=None)
        self.assertTrue(os.path.exists(os.path.join(odd, "keep.jpg")))
        self.assertFalse(I.looks_like_home("/"))


if __name__ == "__main__":
    unittest.main()


class DevModeTests(Base):
    """Developer mode: follow the newest commit on a branch instead of releases."""
    SHA1, SHA2 = "1111111111111111111111111111111111111111", "a2b3c4d5e6a2b3c4d5e6a2b3c4d5e6a2b3c4d5e6"

    def dev(self, on=True, branch="main"):
        with open(os.path.join(self.home, U.CORE_CONFIG), "w") as f:
            f.write(f"DEV_MODE={'yes' if on else 'no'}\nDEV_BRANCH={branch}\n")

    def test_off_by_default(self):
        self.installed_at("v0.2.0")
        self.assertEqual(U.core_settings(self.home), {"DEV_MODE": False, "DEV_BRANCH": "main"})
        self.assertIsNone(U.dev_branch(self.home))

    def test_follows_the_branch_head_and_installs_the_commit(self):
        self.installed_at("v0.2.0")
        self.dev()
        self.gh.push("main", self.SHA1)
        s = self.up.check(force=True, now=T0)
        self.assertEqual((s["branch"], s["latest_ref"], s["available"]), ("main", self.SHA1, True))
        self.assertEqual(s["latest"], "main @ 1111111")
        report = self.up.update_to(s)
        self.assertEqual(report["to"], "main @ 1111111")
        self.assertEqual(report["from"], "v0.2.0")
        self.assertEqual((U.installed_ref(self.home), U.installed_commit(self.home)), ("main", self.SHA1))
        self.assertEqual(self.up.installed_label(), "main @ 1111111")
        self.assertFalse(self.up.cached_status()["available"])
        self.assertEqual(self.up.cached_status()["current"], "main @ 1111111")
        # A new commit lands.
        self.gh.push("main", self.SHA2)
        s = self.up.check(force=True, now=T0)
        self.assertTrue(s["available"])
        self.assertEqual(s["latest"], "main @ a2b3c4d")

    def test_switching_mode_or_branch_makes_the_cache_stale(self):
        self.installed_at("v0.2.0")
        self.up.check(force=True, now=T0, )      # release mode: no release published here
        self.assertIsNotNone(self.up.cached_status())
        self.dev()
        self.assertIsNone(self.up.cached_status())
        self.gh.push("main", self.SHA1)
        self.gh.push("feature/x", self.SHA2)
        self.assertEqual(self.up.check(now=T0)["latest_ref"], self.SHA1)
        self.dev(branch="feature/x")
        self.assertIsNone(self.up.cached_status())
        self.assertEqual(self.up.check(now=T0)["latest_ref"], self.SHA2)

    def test_missing_branch_is_an_error_not_a_crash(self):
        self.installed_at("v0.2.0")
        self.dev(branch="gone")
        s = self.up.check(force=True, now=T0)
        self.assertFalse(s["available"])
        self.assertTrue(s["error"])

    def test_release_update_forgets_the_branch(self):
        self.installed_at("v0.2.0")
        self.dev()
        self.gh.push("main", self.SHA1)
        self.up.update_to(self.up.check(force=True, now=T0))
        self.dev(on=False)
        self.gh.publish("v0.3.0")
        self.up.update_to(self.up.check(force=True, now=T0))
        self.assertIsNone(U.installed_ref(self.home))
        self.assertIsNone(U.installed_commit(self.home))
        self.assertEqual(self.up.installed_label(), "v0.3.0")

    def test_branch_list(self):
        self.gh.push("feature/x", self.SHA2)
        self.gh.push("main", self.SHA1)
        self.assertEqual(self.up.fetch_branches(), ["main", "feature/x"])
