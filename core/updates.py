"""One suite version, the update check and the update itself (#14).

The suite version is the first line of VERSION at the root of the repository
and of the installed home (e.g. v0.2.0). It covers better-unas and every
tool; a tool.conf VERSION is informational only.

The check asks GitHub for the latest release and caches the answer in the
home for CHECK_HOURS, so starting the app is quick and doesn't hit the API
every time. Nothing updates on its own: update() runs only after the user
presses the key and confirms.

    cached_status(home)   what the last check found; no network
    check(home)           the cached answer if fresh, else ask GitHub
    Updater(home).update(tag)
                          download the release, replace the suite's files
                          (config, state and locks stay), re-run the installer
                          of every installed tool, restore the link
"""

import datetime as dt
import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import install as I

REPO = "sloraris/better-unas"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TARBALL_URL = f"https://github.com/{REPO}/archive/{{tag}}.tar.gz"
COMMIT_URL = f"https://api.github.com/repos/{REPO}/commits/{{ref}}"
BRANCHES_URL = f"https://api.github.com/repos/{REPO}/branches?per_page=100"
CACHE = ".update-check.json"
CORE_CONFIG = "better-unas.conf"   # in the home: better-unas's own settings (core/settings.json)
COMMIT_FILE = ".commit"            # in the home: the commit installed in developer mode or by --ref
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CHECK_HOURS = 6
ERROR_RETRY_MINUTES = 30      # a failed check is retried sooner than a good one
TIMEOUT = 10
TAG_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


# --------------------------------------------------------------------- versions

def suite_version(home):
    """The installed suite version, or "" if there is no VERSION file."""
    try:
        with open(os.path.join(home, "VERSION"), encoding="utf-8") as f:
            return f.readline().strip()
    except OSError:
        return ""


def parse_version(v):
    """v0.2.0 -> (0, 2, 0); None if it isn't a version (a branch name, "local")."""
    m = re.match(r"^v?(\d+(?:\.\d+)*)$", (v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(latest, current):
    if not latest:
        return False
    a, b = parse_version(latest), parse_version(current)
    if a is not None and b is not None:
        n = max(len(a), len(b))
        return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))
    return latest != current


def _now():
    return dt.datetime.now().astimezone()


# --------------------------------------------------------------------- GitHub

def _get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={
        "User-Agent": "better-unas", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_latest(api_url=API_URL, timeout=TIMEOUT):
    """The latest release's tag. Raises LookupError when none is published,
    OSError (URLError and friends) when GitHub can't be reached."""
    try:
        with _get(api_url, timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise LookupError("no release has been published yet") from None
        raise OSError(f"GitHub answered {e.code}") from None
    except ValueError:
        raise OSError("GitHub's answer wasn't JSON") from None
    tag = data.get("tag_name") if isinstance(data, dict) else None
    if not isinstance(tag, str) or not TAG_RE.match(tag):
        raise OSError("GitHub's answer had no usable tag_name")
    return tag


def _safe_members(tar, dest):
    """Members of a GitHub archive with the top folder stripped; refuses
    anything that would land outside dest."""
    root = os.path.realpath(dest)
    out = []
    for m in tar.getmembers():
        parts = m.name.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            continue
        name = parts[1]
        target = os.path.realpath(os.path.join(root, name))
        if os.path.isabs(name) or not (target == root or target.startswith(root + os.sep)):
            raise OSError(f"unsafe path in the release archive: {m.name}")
        if m.issym() or m.islnk():
            link = m.linkname
            base = os.path.dirname(target) if m.issym() else root
            if os.path.isabs(link) or not os.path.realpath(os.path.join(base, link)).startswith(root + os.sep):
                raise OSError(f"unsafe link in the release archive: {m.name}")
        elif not (m.isfile() or m.isdir()):
            continue
        m.name = name
        out.append(m)
    return out


REF_FILE = ".ref"   # in the home: the branch/tag installed with `update --ref`, if any


def valid_ref(ref):
    return isinstance(ref, str) and bool(TAG_RE.match(ref)) and ".." not in ref \
        and not ref.startswith(("-", "/")) and not ref.endswith("/")


def installed_ref(home):
    """The branch or tag installed with `update --ref`, or None (a release)."""
    try:
        with open(os.path.join(home, REF_FILE)) as f:
            ref = f.read().strip()
    except OSError:
        return None
    return ref if valid_ref(ref) else None


def download(tag, dest, tarball_url=TARBALL_URL, timeout=60):
    """Download and unpack release `tag` (or any branch or tag name GitHub's
    archive URL accepts) into the empty folder dest."""
    if not valid_ref(tag or ""):
        raise OSError(f"invalid release tag {tag!r}")
    with tempfile.TemporaryFile() as tmp:
        try:
            with _get(tarball_url.format(tag=tag), timeout) as r:
                shutil.copyfileobj(r, tmp)
        except urllib.error.HTTPError as e:
            raise OSError(f"download of {tag} failed: {e.code}") from None
        tmp.seek(0)
        try:
            with tarfile.open(fileobj=tmp, mode="r:gz") as tar:
                members = _safe_members(tar, dest)
                for m in members:
                    tar.extract(m, dest, set_attrs=m.isfile(), **_filter())
        except tarfile.TarError as e:
            raise OSError(f"the release archive for {tag} is damaged: {e}") from None
    if not os.path.isfile(os.path.join(dest, "VERSION")):
        raise OSError(f"release {tag} has no VERSION file")
    return dest


def _filter():
    # Python 3.12+ warns without a filter; members are already checked above.
    return {"filter": "fully_trusted"} if hasattr(tarfile, "fully_trusted_filter") else {}


# --------------------------------------------------------------------- developer mode

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


def core_schema():
    from . import plugins as P
    return P.load_schema(SCHEMA_PATH)


def core_settings(home):
    """better-unas's own settings: {"DEV_MODE": bool, "DEV_BRANCH": str}.
    Defaults when the file is missing; a bad value falls back to its default."""
    from . import plugins as P
    schema = core_schema()
    try:
        raw, _ = P.read_keyvalue(os.path.join(home, CORE_CONFIG))
    except OSError:
        raw = {}
    typed, _ = P.validate_config(schema, raw)
    return typed


def dev_branch(home):
    """The branch developer mode follows, or None when it's off."""
    cfg = core_settings(home)
    branch = cfg.get("DEV_BRANCH") or "main"
    return branch if cfg.get("DEV_MODE") and valid_ref(branch) else None


def installed_commit(home):
    try:
        with open(os.path.join(home, COMMIT_FILE)) as f:
            sha = f.read().strip()
    except OSError:
        return None
    return sha if SHA_RE.match(sha) else None


def commit_label(ref, sha):
    return f"{ref} @ {sha[:7]}" if sha else ref


def fetch_head(branch, commit_url=COMMIT_URL, timeout=TIMEOUT):
    """(sha, ISO date) of the newest commit on branch. LookupError when the
    branch doesn't exist, OSError when GitHub can't be reached."""
    if not valid_ref(branch):
        raise LookupError(f"invalid branch {branch!r}")
    try:
        with _get(commit_url.format(ref=urllib.parse.quote(branch, safe="/")), timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (404, 422):
            raise LookupError(f"no branch {branch!r} on GitHub") from None
        raise OSError(f"GitHub answered {e.code}") from None
    except ValueError:
        raise OSError("GitHub's answer wasn't JSON") from None
    sha = data.get("sha") if isinstance(data, dict) else None
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        raise OSError("GitHub's answer had no commit")
    date = ((data.get("commit") or {}).get("committer") or {}).get("date")
    return sha, date if isinstance(date, str) else None


def fetch_branches(branches_url=BRANCHES_URL, timeout=TIMEOUT):
    """Branch names on GitHub (first 100), main first."""
    try:
        with _get(branches_url, timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise OSError(f"GitHub answered {e.code}") from None
    except ValueError:
        raise OSError("GitHub's answer wasn't JSON") from None
    names = [b.get("name") for b in data if isinstance(b, dict)] if isinstance(data, list) else []
    names = [n for n in names if isinstance(n, str) and valid_ref(n)]
    return sorted(names, key=lambda n: (n != "main", n))


# --------------------------------------------------------------------- the check

def _read_cache(home):
    try:
        with open(os.path.join(home, CACHE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("checked_at"), str):
        return None
    return data


def _write_cache(home, data):
    path = os.path.join(home, CACHE)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass   # a read-only home just means checking again next time


def _status(home, cache):
    """The status dict. In developer mode current and latest are labels like
    "main @ 1a2b3c4", "latest_ref" is the commit to install and "branch" is set."""
    latest = cache.get("latest") if isinstance(cache.get("latest"), str) else None
    error = cache.get("error") if isinstance(cache.get("error"), str) else None
    branch = cache.get("branch") if cache.get("mode") == "dev" else None
    if branch:
        have = installed_commit(home) if installed_ref(home) == branch else None
        return {"current": commit_label(branch, have) if have else (suite_version(home) or "?"),
                "latest": commit_label(branch, latest) if latest else None,
                "available": bool(latest) and latest != have,
                "checked_at": cache["checked_at"], "error": error,
                "branch": branch, "latest_ref": latest, "latest_date": cache.get("latest_date")}
    current = suite_version(home)
    return {"current": current, "latest": latest,
            "available": bool(latest) and is_newer(latest, current),
            "checked_at": cache["checked_at"], "error": error,
            "branch": None, "latest_ref": latest, "latest_date": None}


def _matches_mode(home, cache):
    """A cached answer counts only for the mode (and branch) it was made in."""
    branch = dev_branch(home)
    if branch:
        return cache.get("mode") == "dev" and cache.get("branch") == branch
    return cache.get("mode", "release") == "release"


def cached_status(home):
    """What the last check found, from the cache only (never the network).
    None if never checked, else {"current", "latest", "available",
    "checked_at", "error"}. current is read from VERSION now, so the answer
    is right straight after an update."""
    cache = _read_cache(home)
    return None if cache is None or not _matches_mode(home, cache) else _status(home, cache)


def _fresh(cache, now, hours):
    try:
        t = dt.datetime.fromisoformat(cache["checked_at"])
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.astimezone()
    age = now - t
    limit = dt.timedelta(minutes=ERROR_RETRY_MINUTES) if cache.get("error") else dt.timedelta(hours=hours)
    return dt.timedelta(0) <= age < limit


def check(home, force=False, now=None, hours=CHECK_HOURS, fetch=None, fetch_head_fn=None):
    """The update status, from the cache when it's fresh, else from GitHub
    (the answer, or the error, is cached). Never raises for network trouble.
    In developer mode it asks for the newest commit on the branch instead of
    the latest release."""
    now = now or _now()
    cache = _read_cache(home)
    if cache is not None and not force and _matches_mode(home, cache) and _fresh(cache, now, hours):
        return _status(home, cache)
    branch = dev_branch(home)
    date = None
    try:
        if branch:
            latest, date = (fetch_head_fn or fetch_head)(branch)
        else:
            latest = (fetch or fetch_latest)()
        error = None
    except LookupError as e:
        latest, error = None, str(e)
    except (OSError, ValueError) as e:
        latest, error = None, f"couldn't reach GitHub ({getattr(e, 'reason', e)})"
    data = {"latest": latest, "error": error, "checked_at": now.isoformat(timespec="seconds"),
            "checked_with": suite_version(home), "mode": "dev" if branch else "release"}
    if branch:
        data.update(branch=branch, latest_date=date)
    _write_cache(home, data)
    return _status(home, data)


def check_in_background(home, fetch=None):
    """Run check() on a daemon thread (app launch). Returns the thread."""
    t = threading.Thread(target=lambda: check(home, fetch=fetch), daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------- updating

def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


class Updater:
    """Where updates come from and what they touch. Tests point the URLs at
    file:// fixtures, the link at a temporary folder and run at a fake."""

    def __init__(self, home, api_url=API_URL, tarball_url=TARBALL_URL, link="default",
                 run=None, commit_url=COMMIT_URL, branches_url=BRANCHES_URL):
        self.home = os.path.abspath(home)
        self.api_url, self.tarball_url = api_url, tarball_url
        self.commit_url, self.branches_url = commit_url, branches_url
        if link == "default":
            # As install.sh: the short command only for the real install.
            link = I.LINK if self.home == I.DEFAULT_HOME else None
        self.link = link
        self.run = run or I.run_script

    # --- reads
    def fetch_latest(self):
        return fetch_latest(self.api_url)

    def fetch_head(self, branch):
        return fetch_head(branch, self.commit_url)

    def fetch_branches(self):
        return fetch_branches(self.branches_url)

    def check(self, force=False, now=None):
        return check(self.home, force=force, now=now, fetch=self.fetch_latest,
                     fetch_head_fn=self.fetch_head)

    def dev_branch(self):
        return dev_branch(self.home)

    def update_to(self, status):
        """Install what a check offered: the release, or in developer mode
        the commit (recorded with its branch)."""
        if status.get("branch"):
            return self.update(status["latest_ref"], ref=True, label=status["branch"],
                               commit=status["latest_ref"])
        return self.update(status["latest"])

    def cached_status(self):
        return cached_status(self.home)

    def current(self):
        return suite_version(self.home)

    # --- writes
    def fetch_release(self, tag):
        """Download release tag into a fresh temp folder; the caller removes it."""
        work = tempfile.mkdtemp(prefix="better-unas-")
        try:
            download(tag, work, self.tarball_url)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        return work

    def source(self):
        """A release to install tools from: the one kept in the home, or a
        download of the installed version. Returns (path, temp folder or None)."""
        src = I.release_source(self.home)
        if src:
            return src, None
        tag = self.current()
        if not tag:
            raise I.InstallError("no VERSION here, so there's no release to install from")
        work = self.fetch_release(tag)
        return work, work

    def update(self, tag=None, ref=False, label=None, commit=None):
        """Install release tag (default: the latest), or with ref=True any
        branch, tag or commit (label: the branch to remember it by; commit: the
        exact commit, when known). Returns a report dict: {"from", "to",
        "tools": [(name, ok, output)], "skipped": [names not in the release],
        "link": bool}. The home remembers a ref install (REF_FILE, COMMIT_FILE);
        a release install clears both."""
        tag = tag or self.fetch_latest()
        before = self.installed_label()
        work = self.fetch_release(tag)
        try:
            report = self.update_from(work, tag)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        ref_path, commit_path = os.path.join(self.home, REF_FILE), os.path.join(self.home, COMMIT_FILE)
        report["from"] = before or report["from"]
        if ref:
            name = label or tag
            with open(ref_path, "w") as f:
                f.write(name + "\n")
            if commit and SHA_RE.match(commit):
                with open(commit_path, "w") as f:
                    f.write(commit + "\n")
                report["to"] = commit_label(name, commit)
            else:
                _remove(commit_path)
                report["to"] = f"{name} ({self.current() or '?'})"
        else:
            _remove(ref_path)
            _remove(commit_path)
        return report

    def installed_label(self):
        """What's installed, for people: "v0.2.0", or "main @ 1a2b3c4"."""
        ref = installed_ref(self.home)
        if ref:
            return commit_label(ref, installed_commit(self.home))
        return self.current()

    def update_from(self, src, tag=None):
        old = self.current()
        have = I.installed_tools(self.home)
        offered = I.release_tools(src)
        I.install_core(src, self.home)
        src = I.release_source(self.home) or src
        results, skipped = [], []
        for name in have:
            if name in offered:
                ok, out = I.install_tool(src, self.home, name, run=self.run)
                results.append((name, ok, out))
            else:
                skipped.append(name)
        linked = I.restore_link(self.home, self.link) if self.link else False
        return {"from": old, "to": tag or self.current(), "tools": results,
                "skipped": skipped, "link": linked}

    def install_tool(self, name):
        src, work = self.source()
        try:
            if name not in I.release_tools(src):
                raise I.InstallError(f"{name} isn't in release {self.current() or '?'}")
            if work:  # keep it, so the next install needs no download
                I.keep_release(src, self.home)
                src = I.release_source(self.home)
            return I.install_tool(src, self.home, name, run=self.run)
        finally:
            if work:
                shutil.rmtree(work, ignore_errors=True)

    def uninstall_tool(self, name, purge=False):
        return I.uninstall_tool(self.home, name, purge=purge, run=self.run)

    def remove_all(self, delete_home=False):
        return I.remove_all(self.home, delete_home=delete_home, link=self.link, run=self.run)
