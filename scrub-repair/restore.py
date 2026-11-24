"""Finding, checking and swapping in a good copy of a damaged file.

A damaged file is one btrfs refused to read some block of, because the
block failed its checksum. A copy is good when:

  - it is exactly as long as the damaged file;
  - it reads with no errors;
  - every block of the damaged file that still reads matches it byte for byte
    (`compare`). The blocks that won't read are skipped.

Copies come from, in order:

  1. UniFi Cloud backups, through UniFi Drive's local API (`UnifiCloud`),
     newest version first. Drive does the authenticating; no credential is
     read. A file's id in a backup is the unpadded base64url of
     "<backup drive id>:/<path in the share>".
  2. Folders listed in FOLDER_SOURCES (`folder_candidates`): a USB backup
     drive, a share a replication task copies into, a mount from another
     machine. Each is tried as <folder>/<share>/<path> and <folder>/<path>,
     newest modified time first.

The first copy that passes wins. It is staged on the damaged file's own
subvolume (a rename can't cross subvolumes), checked there, and later
renamed over the damaged file in one step (`swap`), keeping the damaged
file's owner, group, mode and extended attributes and the copy's modified
time. A file that changed since it was found is never swapped.

Standard library only.
"""

import base64
import errno
import os
import shutil

BLOCK = 1 << 16          # compare in 64 KiB reads ...
SMALL = 4096             # ... and retry a read that fails in 4 KiB pieces, so one bad block skips only itself
STAGING = ".better-unas-staging"
ROOM = 1 << 30           # free space left on the volume after staging a copy


class Candidate:
    """One copy that might be good. fetch(dest) writes it to dest and returns
    None, or why it couldn't. key identifies the bytes (two backup versions
    holding the same unchanged file share a key, so it's only tried once)."""

    def __init__(self, source, label, size, mtime, fetch, key=None, version_time=None):
        self.source, self.label, self.size, self.mtime = source, label, size, mtime
        self.fetch, self.key, self.version_time = fetch, key, version_time

    def describe(self):
        return {"source": self.source, "label": self.label, "size": self.size, "mtime": self.mtime,
                "version_time": self.version_time}


# --------------------------------------------------------------------------- checking

def _pread_all(fd, n, off):
    out = b""
    while len(out) < n:
        b = os.pread(fd, n - len(out), off + len(out))
        if not b:
            break
        out += b
    return out


def compare(damaged, copy, block=BLOCK, small=SMALL):
    """Check copy against damaged. Returns {"ok", "reason", "compared",
    "unreadable", "size"}: compared and unreadable are byte counts of the
    damaged file."""
    out = {"ok": False, "reason": None, "compared": 0, "unreadable": 0, "size": None}
    try:
        size = os.stat(damaged).st_size
        csize = os.stat(copy).st_size
    except OSError as e:
        out["reason"] = f"couldn't stat: {e.strerror or e}"
        return out
    out["size"] = size
    if size != csize:
        out["reason"] = f"a different size ({csize} bytes, not {size})"
        return out
    try:
        dfd = os.open(damaged, os.O_RDONLY)
    except OSError as e:
        out["reason"] = f"couldn't open the damaged file: {e.strerror or e}"
        return out
    try:
        cfd = os.open(copy, os.O_RDONLY)
    except OSError as e:
        os.close(dfd)
        out["reason"] = f"couldn't open the copy: {e.strerror or e}"
        return out
    try:
        off = 0
        while off < size:
            n = min(block, size - off)
            try:
                theirs = _pread_all(cfd, n, off)
            except OSError as e:
                out["reason"] = f"the copy has a read error at byte {off} ({e.strerror or e})"
                return out
            if len(theirs) != n:
                out["reason"] = f"the copy ended early at byte {off + len(theirs)}"
                return out
            try:
                pieces = [(off, _pread_all(dfd, n, off))]
            except OSError:
                pieces = []
                for o in range(off, off + n, small):
                    m = min(small, off + n - o)
                    try:
                        pieces.append((o, _pread_all(dfd, m, o)))
                    except OSError:
                        out["unreadable"] += m
            for o, mine in pieces:
                ref = theirs[o - off: o - off + len(mine)]
                if mine != ref:
                    at = next((i for i, (a, b) in enumerate(zip(mine, ref)) if a != b), len(mine))
                    out["reason"] = f"it differs from the damaged file at byte {o + at}"
                    return out
                out["compared"] += len(mine)
            off += n
        out["ok"] = True
        return out
    finally:
        os.close(dfd)
        os.close(cfd)


# --------------------------------------------------------------------------- sources

def folder_candidates(folders, share, rel, isfile=os.path.isfile, stat=os.stat):
    """Copies of <share>/<rel> in the folder sources, newest modified first."""
    found, seen = [], set()
    for folder in folders:
        paths = ([os.path.join(folder, share, rel)] if share else []) + [os.path.join(folder, rel)]
        for p in paths:
            real = os.path.realpath(p)
            if real in seen or not isfile(p):
                continue
            seen.add(real)
            try:
                st = stat(p)
            except OSError:
                continue

            def fetch(dest, src=p):
                try:
                    shutil.copyfile(src, dest)
                    with open(dest, "rb+") as f:
                        os.fsync(f.fileno())
                except OSError as e:
                    return f"couldn't copy it: {e.strerror or e}"
                return None
            found.append(Candidate("folder", f"folder {folder}", st.st_size, st.st_mtime, fetch,
                                   key=("folder", real)))
    found.sort(key=lambda c: -c.mtime)
    return found


def item_id(drive_id, rel):
    """A file's id in a UniFi Cloud backup version."""
    raw = f"{drive_id}:/{rel.strip('/')}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _items(body, *keys):
    data = body.get("data") if isinstance(body, dict) else None
    for k in keys:
        if isinstance(data, dict) and isinstance(data.get(k), list):
            return data[k]
    return data if isinstance(data, list) else []


class UnifiCloud:
    """UniFi Cloud backup versions as copies. api(method, path) returns
    (status, body); download(path, dest) returns an HTTP status. tasks:
    [(task id, label)] of the UniFi Cloud backup tasks. Versions and drives
    are read once per instance (one run)."""

    BASE = "/remote-backup/tasks/{task}/ucs/versions"

    def __init__(self, api, download, tasks, parse_time):
        self.api, self.download, self.tasks, self.parse_time = api, download, list(tasks), parse_time
        self._versions, self._drives = {}, {}
        self.problems = []

    def versions(self, task):
        """[(version id, created datetime or None)] newest first, failed ones left out."""
        if task not in self._versions:
            status, body = self.api("GET", self.BASE.format(task=task))
            if status != 200:
                self.problems.append(f"UniFi Cloud backup versions returned HTTP {status}")
                self._versions[task] = []
            else:
                vs = [v for v in _items(body, "versions", "items")
                      if isinstance(v, dict) and v.get("version") and v.get("status") not in ("failed", "error")]
                vs.sort(key=lambda v: str(v.get("createdAt") or ""), reverse=True)
                self._versions[task] = [(str(v["version"]), self.parse_time(v.get("createdAt"))) for v in vs]
        return self._versions[task]

    def drive_id(self, task, version, share):
        key = (task, version)
        if key not in self._drives:
            status, body = self.api("GET", f"{self.BASE.format(task=task)}/{version}/drives")
            self._drives[key] = {} if status != 200 else {
                str(d.get("driveName")): str(d.get("driveId")) for d in _items(body, "drives", "items")
                if isinstance(d, dict) and d.get("driveId") and d.get("restorable", True) is not False}
        names = self._drives[key]
        if share in names:
            return names[share]
        folded = {k.casefold(): v for k, v in names.items()}
        return folded.get(share.casefold())

    def candidates(self, share, rel):
        """Copies of <share>/<rel>, newest version first, lazily (each version
        costs a request or two)."""
        for task, task_label in self.tasks:
            for version, created in self.versions(task):
                did = self.drive_id(task, version, share)
                if not did:
                    continue
                path = f"{self.BASE.format(task=task)}/{version}/drives/{did}/files/{item_id(did, rel)}"
                status, body = self.api("GET", path)
                if status != 200:
                    continue
                meta = body.get("data") if isinstance(body, dict) else None
                if not isinstance(meta, dict) or meta.get("isDir"):
                    continue
                size = meta.get("size")
                mtime = self.parse_time(meta.get("modifyTime"))
                when = f"{created:%d %b %Y}" if created else version[:8]

                def fetch(dest, p=path + "/content"):
                    code = self.download(p, dest)
                    return None if code == 200 else f"the download returned HTTP {code}"
                yield Candidate("unifi", f"{task_label}, version of {when}", size,
                                mtime.timestamp() if mtime else None, fetch,
                                key=("unifi", share, rel, size, meta.get("modifyTime"))
                                if meta.get("modifyTime") else None,
                                version_time=created.isoformat() if created else None)


# --------------------------------------------------------------------------- staging and the swap

def staging_dir(subvol_dir, target):
    """Where a copy of target is staged: a hidden folder at the top of its
    subvolume, or target's own folder when that isn't on the same subvolume
    (a rename can't cross subvolumes). Returns (folder, hidden name prefix)."""
    folder = os.path.join(subvol_dir, STAGING)
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        if os.stat(folder).st_dev == os.stat(os.path.dirname(target)).st_dev:
            return folder, ""
    except OSError:
        pass
    return os.path.dirname(target), f".{STAGING}-"


def stage_path(subvol_dir, target, token):
    folder, prefix = staging_dir(subvol_dir, target)
    return os.path.join(folder, f"{prefix}{token}")


def free_space(folder):
    try:
        return shutil.disk_usage(folder).free
    except OSError:
        return None


def find_copy(target, candidates, dest, compare=compare, tried=None, free=free_space):
    """Try candidates in order: fetch each into dest and compare it with
    target. Returns {"status": "staged", "copy", "check", "looked"} for the
    first that passes, else {"status": "no copy", "looked"}. tried: keys
    already checked (and failed) this run, updated in place."""
    tried = set() if tried is None else tried
    looked = []
    try:
        size = os.stat(target).st_size
    except OSError as e:
        return {"status": "gone" if e.errno == errno.ENOENT else "error",
                "looked": [f"couldn't read the damaged file: {e.strerror or e}"]}
    for c in candidates:
        if c.size is not None and c.size != size:
            looked.append(f"{c.label}: a different size")
            continue
        if c.key is not None and c.key in tried:
            continue
        room = free(os.path.dirname(dest))
        if c.size and room is not None and c.size > room - ROOM:
            looked.append(f"{c.label}: not enough free space to stage it ({c.size:,} bytes)")
            continue
        err = c.fetch(dest)
        if err:
            looked.append(f"{c.label}: {err}")
            _remove(dest)
            continue
        r = compare(target, dest)
        if r["ok"]:
            return {"status": "staged", "copy": c.describe(), "check": r, "looked": looked}
        if c.key is not None:
            tried.add(c.key)
        looked.append(f"{c.label}: {r['reason']}")
        _remove(dest)
    return {"status": "no copy", "looked": looked}


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def tidy(staged):
    """Remove a staged copy if it's still there, and its staging folder once empty."""
    if not staged:
        return
    _remove(staged)
    folder = os.path.dirname(staged)
    if os.path.basename(folder) == STAGING:
        try:
            os.rmdir(folder)
        except OSError:
            pass


def fingerprint(path):
    """What the swap checks hasn't changed: inode, size and modified time."""
    st = os.lstat(path)
    return {"ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def swap(target, staged, expected, copy_mtime=None):
    """Rename staged over target. Returns None, or why not (and leaves both).

    expected: target's fingerprint() when it was found. Keeps target's owner,
    group, mode and extended attributes; the modified time becomes the copy's."""
    try:
        st = os.lstat(target)
    except OSError as e:
        return "the damaged file is gone" if e.errno == errno.ENOENT else f"couldn't read it: {e.strerror or e}"
    if {"ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns} != expected:
        return "the file changed since it was found"
    if st.st_nlink > 1:
        return f"it has {st.st_nlink} hard links"
    if not os.path.isfile(staged):
        return "the staged copy is gone"
    try:
        for name in _xattrs(target):
            try:
                os.setxattr(staged, name, os.getxattr(target, name, follow_symlinks=False))
            except OSError:
                pass
        os.chown(staged, st.st_uid, st.st_gid)
        os.chmod(staged, st.st_mode & 0o7777)
        if copy_mtime is not None:
            os.utime(staged, (copy_mtime, copy_mtime))
        with open(staged, "rb") as f:
            os.fsync(f.fileno())
        os.rename(staged, target)
    except OSError as e:
        return f"the swap failed: {e.strerror or e}"
    try:
        dfd = os.open(os.path.dirname(target), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
    return None


def _xattrs(path):
    try:
        return os.listxattr(path, follow_symlinks=False)
    except (OSError, AttributeError):
        return []
