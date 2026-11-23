"""Small helpers every tool uses. Standard library only."""

import datetime as dt
import hashlib
import os
import subprocess
import syslog


def now():
    return dt.datetime.now().astimezone()


def parse_time(s):
    """An ISO time from state, or None. A time without a zone is taken as local."""
    try:
        t = dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


def log(name, msg, level=syslog.LOG_INFO):
    """Print msg and send it to syslog tagged `name` (journalctl -t name)."""
    print(msg, flush=True)
    try:
        syslog.openlog(name)
        syslog.syslog(level, msg)
    except Exception:
        pass


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def write_atomic(path, data, mode=0o644, tag="better-unas"):
    """Write text to path through a temporary file and a rename."""
    tmp = f"{path}.tmp-{tag}"
    with open(tmp, "w") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def sh(*cmd, timeout=60, env=None):
    """Run a command; return (exit code, stdout). Never raises for a missing tool."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def read_file(path):
    """A small file's text, stripped, or None if it can't be read."""
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None
