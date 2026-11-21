"""A fake better-unas release for the update tests (#14): a GitHub-style
archive (one top folder) and a "latest release" JSON, served from file://
URLs so nothing touches the network.

Tool scripts are real POSIX sh: install.sh writes installed.flag and logs to
calls.log; uninstall.sh removes the flag and, with --purge, the folder.
"""

import io
import json
import os
import tarfile

INSTALL_SH = """#!/bin/sh
set -eu
echo "install $(cat ../VERSION 2>/dev/null || echo ?)" >> calls.log
[ -f config ] || cp config.example config
touch installed.flag
echo "{name} installed"
"""

FAILING_INSTALL_SH = """#!/bin/sh
echo "{name}: missing smartctl"
exit 1
"""

UNINSTALL_SH = """#!/bin/sh
set -eu
BASE=$(pwd)
echo uninstall >> calls.log
rm -f installed.flag
if [ "${{1:-}}" = "--purge" ]; then rm -rf "$BASE"; echo "Deleted {name}."; fi
echo "{name} uninstalled"
"""


def tool_files(name, summary, extra=None, failing=False):
    files = {
        "tool.conf": (f"NAME={name}\nVERSION=0.9.0\nSUMMARY={summary}\n"
                      f"CHANGES=Adds a {name} timer.\nCLI={name}.py\n"),
        f"{name}.py": "print('hello')\n",
        "install.sh": (FAILING_INSTALL_SH if failing else INSTALL_SH).format(name=name),
        "uninstall.sh": UNINSTALL_SH.format(name=name),
        "config.example": "ENABLED=yes\n",
    }
    files.update(extra or {})
    return files


def release_tree(version, tools=None):
    """{relative path: text} for a release."""
    tools = tools if tools is not None else {
        "alpha": tool_files("alpha", "Alpha tool for the update tests",
                            {f"new_in_{version}.txt": version}),
        "beta": tool_files("beta", "Beta tool for the update tests"),
        "gamma": tool_files("gamma", "Gamma tool, never installed"),
    }
    files = {
        "VERSION": version + "\n",
        "better-unas": "#!/usr/bin/env python3\nprint('better-unas')\n",
        "README.md": f"better-unas {version}\n",
        "core/__init__.py": f"RELEASE = {version!r}\n",
        "smart-scheduler-like/README.md": "no tool.conf: not a tool\n",
    }
    for name, tf in tools.items():
        for rel, text in tf.items():
            files[f"{name}/{rel}"] = text
    return files


def tarball(files, top, extra_members=()):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        dirs = set()
        for rel in sorted(files):
            parts = rel.split("/")[:-1]
            for i in range(len(parts)):
                dirs.add("/".join(parts[:i + 1]))
        for d in [""] + sorted(dirs):
            info = tarfile.TarInfo(f"{top}/{d}".rstrip("/"))
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(info)
        for rel, text in sorted(files.items()):
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(data)
            info.mode = 0o755 if rel.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))
        for info, data in extra_members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


class FakeGitHub:
    """Releases in a folder; .api_url and .tarball_url are file:// URLs."""

    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.api_url = "file://" + os.path.join(folder, "latest.json")
        self.tarball_url = "file://" + os.path.join(folder, "{tag}.tar.gz")

    def publish(self, version, tools=None, latest=True, extra_members=()):
        data = tarball(release_tree(version, tools), f"better-unas-{version.lstrip('v')}", extra_members)
        with open(os.path.join(self.folder, f"{version}.tar.gz"), "wb") as f:
            f.write(data)
        if latest:
            with open(os.path.join(self.folder, "latest.json"), "w") as f:
                json.dump({"tag_name": version, "name": f"better-unas {version}"}, f)
