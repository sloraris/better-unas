"""Installing, uninstalling and removing tools in a better-unas home (#14).

The home is the folder the suite is installed in, /persistent/better-unas on
the NAS. It holds:

    VERSION            the installed suite version, e.g. v0.2.0
    better-unas        the entry point (also the CLI)
    core/              this code
    .release/          the release the suite was installed from, so a tool
                       can be installed later without a download
    <tool>/            an installed tool (tool.conf), or an uninstalled one
                       (tool.conf.uninstalled: kept off by updates)

A tool's own files are replaced and its config, state.json and lock are
kept; each tool's own install.sh / uninstall.sh does the work on the NAS;
uninstalling without --purge renames tool.conf to tool.conf.uninstalled so
an update won't turn it back on.

Nothing here prints or asks. The callers (the setup wizard, the Tools
screen, the CLI) confirm first and show the output the tool scripts return.
"""

import os
import re
import shutil
import subprocess

from . import plugins as P

DEFAULT_HOME = "/persistent/better-unas"
LINK = "/usr/local/bin/better-unas"
ENTRY = "better-unas"
RELEASE_DIR = ".release"
UNINSTALLED = "tool.conf.uninstalled"
# Always kept when a tool's files are replaced (plus its manifest CONFIG and LOCK).
KEEP = ("config", "state.json", ".lock", "busy.json", UNINSTALLED)
# Suite files copied from a release into the home. The entry point is handled apart.
CORE_ITEMS = ("core", "VERSION", "README.md")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class InstallError(Exception):
    pass


def valid_name(name):
    return bool(NAME_RE.match(name or ""))


def tool_dir(home, name):
    if not valid_name(name):
        raise InstallError(f"bad tool name {name!r}")
    return os.path.join(home, name)


def _manifest(path):
    try:
        values, _ = P.read_keyvalue(path)
    except OSError:
        return {}
    return values


# --------------------------------------------------------------------- what's where

def release_tools(src):
    """Tools in an extracted release: {name: manifest} for folders whose
    tool.conf NAME matches the folder and that have an install.sh."""
    found = {}
    if not src or not os.path.isdir(src):
        return found
    for entry in sorted(os.listdir(src)):
        conf = os.path.join(src, entry, "tool.conf")
        if not os.path.isfile(conf):
            continue
        m = _manifest(conf)
        if m.get("NAME") == entry and valid_name(entry) and \
                os.path.isfile(os.path.join(src, entry, "install.sh")):
            found[entry] = m
    return found


def installed_tools(home):
    """Names of installed tools: folders with a tool.conf."""
    if not os.path.isdir(home):
        return []
    return sorted(e for e in os.listdir(home)
                  if valid_name(e) and os.path.isfile(os.path.join(home, e, "tool.conf")))


def uninstalled_tools(home):
    """Names of tools uninstalled with their config kept (tool.conf.uninstalled)."""
    if not os.path.isdir(home):
        return []
    return sorted(e for e in os.listdir(home)
                  if valid_name(e) and os.path.isfile(os.path.join(home, e, UNINSTALLED))
                  and not os.path.isfile(os.path.join(home, e, "tool.conf")))


def release_source(home):
    """The release kept in the home, or None (installed by the old install.sh)."""
    d = os.path.join(home, RELEASE_DIR)
    return d if os.path.isdir(d) else None


def catalog(home):
    """Every tool the home knows about: [(name, manifest, where)], where is
    "installed", "uninstalled" or "available" (in the kept release only)."""
    out = {}
    for name, m in release_tools(release_source(home)).items():
        out[name] = (m, "available")
    for name in uninstalled_tools(home):
        out[name] = (_manifest(os.path.join(home, name, UNINSTALLED)) or out.get(name, ({},))[0],
                     "uninstalled")
    for name in installed_tools(home):
        out[name] = (_manifest(os.path.join(home, name, "tool.conf")), "installed")
    return [(n, m, w) for n, (m, w) in sorted(out.items())]


# --------------------------------------------------------------------- running scripts

def run_script(folder, script, *args):
    """Run a tool's shell script in its folder. Returns (ok, output)."""
    try:
        p = subprocess.run(["sh", f"./{script}", *args], cwd=folder, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{script}: {e}"
    return p.returncode == 0, p.stdout.decode("utf-8", "replace")


# --------------------------------------------------------------------- tools

def copy_tool(src, home, name):
    """Replace a tool's files with the release's, keeping its config, state and lock."""
    s = os.path.join(src, name)
    if not os.path.isfile(os.path.join(s, "tool.conf")):
        raise InstallError(f"{name} isn't in this release")
    dest = tool_dir(home, name)
    old = _manifest(os.path.join(dest, "tool.conf")) or _manifest(os.path.join(dest, UNINSTALLED))
    new = _manifest(os.path.join(s, "tool.conf"))
    keep = set(KEEP)
    for m in (old, new):
        for key in ("CONFIG", "LOCK"):
            if m.get(key) and "/" not in m[key]:
                keep.add(m[key])
    os.makedirs(dest, exist_ok=True)
    for entry in os.listdir(dest):
        if entry in keep:
            continue
        p = os.path.join(dest, entry)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.remove(p)
    for entry in os.listdir(s):
        if entry in keep or entry == "__pycache__":
            continue
        a, b = os.path.join(s, entry), os.path.join(dest, entry)
        if os.path.isdir(a) and not os.path.islink(a):
            shutil.copytree(a, b, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(a, b, follow_symlinks=False)
    return dest


def install_tool(src, home, name, run=run_script):
    """Copy a tool from the release and run its install.sh. Clears the
    uninstalled mark. Returns (ok, output)."""
    dest = copy_tool(src, home, name)
    mark = os.path.join(dest, UNINSTALLED)
    if os.path.exists(mark):
        os.remove(mark)
    return run(dest, "install.sh")


def uninstall_tool(home, name, purge=False, run=run_script):
    """Run the tool's uninstall.sh. Without purge the folder (config, state)
    stays and tool.conf becomes tool.conf.uninstalled; with purge the folder
    goes. Returns (ok, output)."""
    dest = tool_dir(home, name)
    if not os.path.isfile(os.path.join(dest, "tool.conf")):
        raise InstallError(f"{name} isn't installed")
    if os.path.isfile(os.path.join(dest, "uninstall.sh")):
        ok, out = run(dest, "uninstall.sh", *(["--purge"] if purge else []))
    else:
        ok, out = True, "(no uninstall.sh; nothing to undo outside its folder)\n"
    if not ok:
        return ok, out
    if purge:
        if os.path.isdir(dest):
            shutil.rmtree(dest)
    elif os.path.isfile(os.path.join(dest, "tool.conf")):
        os.replace(os.path.join(dest, "tool.conf"), os.path.join(dest, UNINSTALLED))
    return ok, out


# --------------------------------------------------------------------- the suite itself

def _replace(src_path, dest_path):
    """Put a copy of src_path at dest_path, swapping it in at the end."""
    tmp = dest_path + ".new"
    old = dest_path + ".old"
    for p in (tmp, old):
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        elif os.path.lexists(p):
            os.remove(p)
    if os.path.isdir(src_path):
        shutil.copytree(src_path, tmp, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
    else:
        shutil.copy2(src_path, tmp)
    if os.path.lexists(dest_path):
        os.replace(dest_path, old)
    os.replace(tmp, dest_path)
    if os.path.isdir(old) and not os.path.islink(old):
        shutil.rmtree(old)
    elif os.path.lexists(old):
        os.remove(old)


def install_core(src, home):
    """Copy the suite's own files (core, VERSION, entry point) and keep the
    whole release as .release/ for installing tools later."""
    if not os.path.isfile(os.path.join(src, "VERSION")):
        raise InstallError("the release has no VERSION file")
    os.makedirs(home, exist_ok=True)
    for item in CORE_ITEMS:
        p = os.path.join(src, item)
        if os.path.exists(p):
            _replace(p, os.path.join(home, item))
    entry = os.path.join(src, ENTRY)
    if os.path.isfile(entry):
        _replace(entry, os.path.join(home, ENTRY))
        os.chmod(os.path.join(home, ENTRY), 0o755)
    keep_release(src, home)


def keep_release(src, home):
    """Keep a copy of the release as .release/ (unless src already is it)."""
    if os.path.abspath(src) != os.path.abspath(os.path.join(home, RELEASE_DIR)):
        _replace(src, os.path.join(home, RELEASE_DIR))


def restore_link(home, link=LINK):
    """Point the short command at the home's entry point. Returns True if the
    link is in place. Skipped (False) when the link's folder doesn't exist."""
    if not link or not os.path.isdir(os.path.dirname(link)):
        return False
    target = os.path.join(home, ENTRY)
    if os.path.islink(link) and os.readlink(link) == target:
        return True
    tmp = link + ".new"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)
    return True


def remove_link(home, link=LINK):
    """Remove the short command if it points into this home."""
    if link and os.path.islink(link):
        target = os.readlink(link)
        if os.path.abspath(os.path.dirname(target) or "/") == os.path.abspath(home):
            os.remove(link)
            return True
    return False


def looks_like_home(home):
    """Only delete a folder that is clearly a better-unas home."""
    home = os.path.abspath(home)
    if home in ("/", os.path.expanduser("~")) or home.count(os.sep) < 2:
        return False
    return os.path.isfile(os.path.join(home, "VERSION")) or \
        os.path.isdir(os.path.join(home, "core")) or os.path.basename(home) == "better-unas"


def remove_all(home, delete_home=False, link=LINK, run=run_script):
    """Uninstall every tool, remove the link and, if asked, delete the home.
    Returns [(name, ok, output)] for the tools. A tool whose uninstall.sh
    fails stops the home from being deleted (its triggers may still be in
    place), and InstallError says which."""
    results = []
    for name in installed_tools(home):
        ok, out = uninstall_tool(home, name, run=run)
        results.append((name, ok, out))
    remove_link(home, link)
    failed = [n for n, ok, _ in results if not ok]
    if delete_home:
        if failed:
            raise InstallError(f"not deleting {home}: {', '.join(failed)} didn't uninstall cleanly")
        if not looks_like_home(home):
            raise InstallError(f"not deleting {home}: it doesn't look like a better-unas folder")
        shutil.rmtree(home)
    return results
