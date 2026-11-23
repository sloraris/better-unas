"""scrub-repair's detection rules: kernel log records in, damage reports out.
Pure, no I/O, so the tests drive it with text fixtures.

Where damage shows up (checked on a UNAS Pro, kernel 5.10, btrfs-progs 6.14):

  - The kernel ring buffer, read from /dev/kmsg. UNAS doesn't copy kernel
    messages into the journal. A record reads `pri,seq,usec,flags;message`
    with continuation lines starting with a space.
  - A failed read logs, per bad block:
      BTRFS warning (device dm-0): csum failed root 258 ino 1234 off 8192
        csum 0x... expected csum 0x... mirror 1
  - A scrub logs, per bad block:
      BTRFS warning (device dm-0): checksum error at logical 123 on dev
        /dev/dm-0, physical 456, root 258, inode 1234, offset 8192,
        length 4096, links 1 (path: Photos/2019/a.jpg)
    and for metadata
      ... checksum error at logical 123 on dev /dev/dm-0, physical 456:
        metadata leaf (level 0) in tree 2
  - `root` is the subvolume's id and `ino` the inode within it. scrub_repair.py
    turns the pair into a path with `btrfs inspect-internal inode-resolve`.

The cursor is (boot id, sequence number): sequence numbers restart at each
boot, so a different boot id means every record is new.
"""

import calendar as _calendar
import datetime as dt
import re

# --------------------------------------------------------------------------- kmsg

Record = tuple  # (seq, message)


def parse_record(raw):
    """One /dev/kmsg record (bytes or str) -> (seq, message) or None."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    head, sep, rest = raw.partition(";")
    if not sep:
        return None
    fields = head.split(",")
    if len(fields) < 4:
        return None
    try:
        seq = int(fields[1])
    except ValueError:
        return None
    message = rest.split("\n", 1)[0]           # continuation lines (" KEY=value") aren't needed
    return seq, message


def new_records(records, cursor, boot):
    """The records after the cursor, and the new cursor.

    records: [(seq, message)] in order; cursor: {"boot", "seq"} or None.
    A different boot (or no cursor) means everything is new."""
    last = cursor.get("seq") if cursor and cursor.get("boot") == boot else None
    out = [r for r in records if last is None or r[0] > last]
    seq = records[-1][0] if records else last
    return out, ({"boot": boot, "seq": seq} if seq is not None else (cursor if last is not None else None))


# --------------------------------------------------------------------------- checksum lines

DEVICE = r"BTRFS (?:warning|error|info|critical) \(device (?P<dev>[^)]+)\)"
READ_RE = re.compile(DEVICE + r": csum failed root (?P<root>-?\d+) ino (?P<ino>\d+) off (?P<off>\d+)")
SCRUB_RE = re.compile(DEVICE + r": checksum error at logical \d+ on dev \S+, physical \d+, root (?P<root>-?\d+), "
                      r"inode (?P<ino>\d+), offset (?P<off>\d+)(?:, length \d+, links \d+ \(path: (?P<path>.*)\))?")
METADATA_RE = re.compile(DEVICE + r": (?:checksum error at logical \d+ on dev \S+, physical \d+: metadata"
                         r"|bad tree block start|parent transid verify failed|csum mismatch on free space cache"
                         r"|tree block .* corrupt|corrupt (?:leaf|node))(?P<what>.*)")
# Seen alongside the lines above; counts only, nothing to resolve.
COUNTER_RE = re.compile(DEVICE + r": bdev \S+ errs:")


def parse_line(message):
    """A kernel message -> a damage report, or None for anything else.

    {"how": "read"|"scrub", "dev", "root", "ino", "off", "path"} for a data block,
    {"how": "metadata", "dev", "text"} for damage outside a file."""
    m = READ_RE.search(message)
    if m:
        return {"how": "read", "dev": m["dev"], "root": int(m["root"]), "ino": int(m["ino"]),
                "off": int(m["off"]), "path": None}
    m = SCRUB_RE.search(message)
    if m:
        return {"how": "scrub", "dev": m["dev"], "root": int(m["root"]), "ino": int(m["ino"]),
                "off": int(m["off"]), "path": m["path"]}
    m = METADATA_RE.search(message)
    if m:
        return {"how": "metadata", "dev": m["dev"], "text": message[m.start():].strip()[:300]}
    return None


def damage(records):
    """[(seq, report)] for the damage reports among the records, one per
    (root, inode) for data and one per device for metadata, first seen first."""
    out, seen = [], set()
    for seq, message in records:
        r = parse_line(message)
        if not r:
            continue
        key = (r["how"] == "metadata", r["dev"]) if r["how"] == "metadata" else (r["root"], r["ino"])
        if key in seen:
            if r["how"] == "scrub":     # a scrub line names the path; keep it with the first report
                for _, earlier in out:
                    if (earlier.get("root"), earlier.get("ino")) == key and not earlier.get("path"):
                        earlier["path"] = r["path"]
            continue
        seen.add(key)
        out.append((seq, r))
    return out


# --------------------------------------------------------------------------- subvolumes

SUBVOL_RE = re.compile(r"^ID (\d+) gen \d+(?: cgen \d+)? top level (\d+)(?: parent_uuid \S+)?(?: uuid \S+)? path (.+)$")


def subvolumes(text, fsroot="/"):
    """`btrfs subvolume list <mount>` -> {id: path relative to the mount}.

    Paths may come back relative to the filesystem's top level (sometimes
    with a `<FS_TREE>/` prefix) or already relative to the mount. When the
    mount shows a subvolume (`fsroot`, e.g. /@unifi) and the listed paths
    start with it, it is taken off, and subvolumes outside it are left out."""
    prefix = fsroot.strip("/")
    rows = []
    for line in text.splitlines():
        m = SUBVOL_RE.match(line.strip())
        if m:
            path = m.group(3)
            if path.startswith("<FS_TREE>/"):
                path = path[len("<FS_TREE>/"):]
            rows.append((int(m.group(1)), path))
    prefixed = bool(prefix) and any(p == prefix or p.startswith(prefix + "/") for _, p in rows)
    out = {}
    for sid, path in rows:
        if prefixed:
            if path == prefix:
                path = ""
            elif path.startswith(prefix + "/"):
                path = path[len(prefix) + 1:]
            else:
                continue
        out[sid] = path
    return out


def is_snapshot(rel):
    """A path inside a UniFi snapshot (read-only, shares blocks with the live file)."""
    return "/.snapshot/" in "/" + rel.strip("/") + "/"


def share_of(rel):
    """(share name, path inside the share) for a path relative to the mount, or
    (None, None) outside the shares. UniFi Drive keeps each share in
    .srv/.unifi-drive/<share>/, its files in a .data folder there."""
    parts = rel.strip("/").split("/")
    if len(parts) < 4 or parts[:2] != [".srv", ".unifi-drive"] or parts[2].startswith("."):
        return None, None
    inner = parts[3:]
    if inner and inner[0] == ".data":
        inner = inner[1:]
    return parts[2], "/".join(inner) or None


# --------------------------------------------------------------------------- scrub status

def parse_scrub_status(text):
    """`btrfs scrub status <mount>` -> {"state", "percent", "left", "errors",
    "uncorrectable", "started", "duration"}. state: none (never run), running,
    finished, aborted (cancelled: resumable), interrupted, or unknown."""
    out = {"state": "unknown", "percent": None, "left": None, "errors": None, "uncorrectable": None,
           "started": None, "duration": None}
    if "no stats available" in text:
        out["state"] = "none"
        return out
    fields = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if sep:
            fields[k.strip().lower()] = v.strip()
    status = fields.get("status", "").lower()
    if status:
        out["state"] = status if status in ("running", "finished", "aborted", "interrupted") else "unknown"
    m = re.search(r"\(([\d.]+)%\)", fields.get("bytes scrubbed", ""))
    if m:
        out["percent"] = float(m.group(1))
    elif out["state"] == "finished":
        out["percent"] = 100.0
    out["left"] = _duration(fields.get("time left"))
    out["duration"] = _duration(fields.get("duration"))
    summary = fields.get("error summary", "")
    if summary:
        out["errors"] = 0 if "no errors" in summary else sum(int(n) for n in re.findall(r"=(\d+)", summary))
    if "uncorrectable" in fields:
        try:
            out["uncorrectable"] = int(fields["uncorrectable"])
        except ValueError:
            pass
    out["started"] = fields.get("scrub started") or fields.get("scrub resumed")
    return out


def _duration(s):
    m = re.fullmatch(r"(\d+):(\d\d):(\d\d)", (s or "").strip())
    if not m:
        return None
    return dt.timedelta(hours=int(m[1]), minutes=int(m[2]), seconds=int(m[3]))


# --------------------------------------------------------------------------- scrub calendar

EVERY = {"monthly": 1, "quarterly": 3, "half-yearly": 6, "yearly": 12}


def period_start(t, frequency, start_day):
    """The date the scrub period containing date t began: start_day of
    January and every EVERY[frequency] months after."""
    step = EVERY[frequency]
    month = ((t.month - 1) // step) * step + 1
    d = dt.date(t.year, month, min(start_day, _calendar.monthrange(t.year, month)[1]))
    if d > t:
        month -= step
        year = t.year
        if month < 1:
            month += 12
            year -= 1
        d = dt.date(year, month, min(start_day, _calendar.monthrange(year, month)[1]))
    return d


def next_period(d, frequency, start_day):
    step = EVERY[frequency]
    month, year = d.month + step, d.year
    while month > 12:
        month -= 12
        year += 1
    return dt.date(year, month, min(start_day, _calendar.monthrange(year, month)[1]))


def window_on(day, window, tz):
    """(start, end) of the night window that opens on `day`; an end at or
    before the start is the next morning."""
    a, b = window
    start = dt.datetime.combine(day, a, tz)
    end = dt.datetime.combine(day, b, tz)
    if end <= start:
        end += dt.timedelta(days=1)
    return start, end


def current_window(t, window):
    """The night window t is inside, or None."""
    for day in (t.date() - dt.timedelta(days=1), t.date()):
        start, end = window_on(day, window, t.tzinfo)
        if start <= t < end:
            return start, end
    return None


def scheduled_due(t, frequency, start_day, last_done):
    """True when this period's scheduled scrub hasn't finished yet: the period
    has begun and no scrub finished since its start."""
    begun = period_start(t.date(), frequency, start_day)
    return last_done is None or last_done.date() < begun


def scheduled_nights(t, until, frequency, start_day, window, last_done, nights_needed=1):
    """[(start, end)] of the planned scrub nights from t until `until`. While
    this period's scrub hasn't finished, the next `nights_needed` windows
    that haven't ended; then `nights_needed` nights from each later
    period's start day."""
    out = []
    begun = period_start(t.date(), frequency, start_day)
    if scheduled_due(t, frequency, start_day, last_done):
        day = t.date() - dt.timedelta(days=1)
        while len(out) < nights_needed:
            start, end = window_on(day, window, t.tzinfo)
            if end > t:
                out.append((start, end))
            day += dt.timedelta(days=1)
    first = next_period(begun, frequency, start_day)
    while first <= until.date():
        for i in range(nights_needed):
            out.append(window_on(first + dt.timedelta(days=i), window, t.tzinfo))
        first = next_period(first, frequency, start_day)
    return [(a, b) for a, b in out if a < until]


# --------------------------------------------------------------------------- md parity

def parity_mismatch(md, seen):
    """[(array, count)] for data arrays whose finished parity check found
    mismatches that haven't triggered a scrub yet. md: {name: {"sync_action",
    "mismatch_cnt", "last_sync_action", "data"}}; seen: {name: count handled}."""
    out = []
    for name, a in sorted((md or {}).items()):
        if not a.get("data") or a.get("sync_action") not in ("idle", None):
            continue
        n = a.get("mismatch_cnt") or 0
        if n and a.get("last_sync_action") == "check" and seen.get(name) != n:
            out.append((name, n))
    return out
