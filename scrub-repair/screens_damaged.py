"""scrub-repair's Damaged files screen for the better-unas TUI.

One row per damaged file: where it is, how and when it was found, and the
state of its best copy. Under the list, the selected file's details: the
copy, how it was checked and where it looked.

  Enter  restore the staged copy, after a confirm
  r      look for a good copy again now
  a      restore every staged copy, after a confirm
  x      forget the file (you replaced or deleted it yourself)
  s      start a read-only scrub now (manual tier)

Each action runs the same function as the CLI (`better-unas scrub-repair
restore PATH` and so on) under the tool's run lock, so nothing collides with
the 15-minute run. With ctx.dry_run nothing changes and the message says
what would have happened.

Loaded by plugin.py, which hands it `env()` returning the scrub_repair
module. Standard library only.
"""

import contextlib
import io

STATUS = {"staged": ("ok", "verified copy staged"), "no copy": ("failed", "no good copy"),
          "found": ("warning", "not looked for yet"), "changed": ("warning", "changed since found"),
          "snapshot": (None, "in a snapshot"), "error": ("failed", "restore failed"),
          "searching": ("running", "looking")}


class Args:
    def __init__(self, args, dry_run):
        self.args, self.dry_run = args, dry_run


def short(rec):
    """share/path for a file in a share, else the full path."""
    if rec.get("share") and rec.get("rel"):
        return f"{rec['share']}/{rec['rel']}"
    return rec["path"]


def detail_rows(sr, rec):
    """(label, value) rows for the selected file."""
    rows = [("Path", rec["path"])]
    found = sr.parse_time(rec.get("found"))
    if found:
        rows.append(("Found", f"by a {rec['how']}, {found:%a %d %b %Y %H:%M}"))
    copy = rec.get("copy")
    if rec["status"] == "staged" and copy:
        check = rec.get("check") or {}
        size = check.get("size") or 0
        compared, unread = check.get("compared") or 0, check.get("unreadable") or 0
        rows.append(("Copy", copy["label"]))
        rows.append(("Check", f"{compared:,} of {size:,} bytes matched; {unread:,} unreadable bytes skipped"
                     if compared else "same size, but no block of the damaged file could be read to compare"))
    elif rec["status"] == "snapshot":
        rows.append(("Copy", "none: snapshots are read-only and share blocks with the live file"))
    looked = rec.get("looked") or []
    if looked:
        rows.append(("Looked", "; ".join(looked)))
    last = sr.parse_time(rec.get("last_try"))
    if last and rec["status"] == "no copy":
        rows.append(("Next try", f"after {(last + sr.RETRY):%a %d %b %H:%M}, or after a backup run"))
    return rows


class DamagedScreen:
    title = "Damaged files"

    COLUMNS = [("File", None), ("Found", 14), ("Best copy", 26)]
    DETAIL_H = 6

    def __init__(self, ctx, env):
        self.ctx, self.env = ctx, env
        self.state = {}
        self.files = []
        self.table = ctx.widgets.Table(self.COLUMNS, empty="No damaged files. Every block read so far matched "
                                                            "its checksum.")

    # ------------------------------------------------------------ data

    def show(self):
        self.load()

    def load(self):
        sr = self.env()
        self.state = sr.load_state()
        self.files = sorted((self.state.get("files") or {}).values(), key=lambda r: r.get("found") or "")
        self.table.set_rows(self.rows())

    def rows(self):
        sr = self.env()
        out = []
        for rec in self.files:
            found = sr.parse_time(rec.get("found"))
            kind, text = STATUS.get(rec["status"], (None, rec["status"]))
            out.append([short(rec), (f"{rec['how']} {found:%d %b}" if found else rec["how"], "dim"),
                        ("status", kind, text)])
        return out

    @property
    def current(self):
        i = self.table.index
        return self.files[i] if i is not None and i < len(self.files) else None

    def open(self, path):
        for i, rec in enumerate(self.files):
            if rec["path"] == path:
                self.table.scroll.index = i
                return True
        self.ctx.message("That file isn't on the list any more.", "dim")
        return False

    # ------------------------------------------------------------ keys

    def keys(self):
        rec = self.current
        out = []
        if rec and rec["status"] == "staged":
            out.append(("Enter", "restore"))
        if rec and rec["status"] in ("found", "no copy", "staged", "changed"):
            out.append(("r", "find copy"))
        if any(r["status"] == "staged" for r in self.files):
            out.append(("a", "restore all"))
        if rec:
            out.append(("x", "forget"))
        out.append(("s", "scrub now"))
        return out + self.table.keys()

    def key(self, k):
        rec = self.current
        if k == "enter" and rec and rec["status"] == "staged":
            self.restore([rec])
        elif k == "r" and rec and rec["status"] in ("found", "no copy", "staged", "changed"):
            self.run("cmd_find", rec["path"], f"Looking for a copy of {short(rec)}", rec)
        elif k == "a" and any(r["status"] == "staged" for r in self.files):
            self.restore([r for r in self.files if r["status"] == "staged"])
        elif k == "x" and rec:
            if self.ctx.confirm(f"Forget {short(rec)}?", ["It leaves the list and its alert clears. The file isn't "
                                                          "touched. Use this after replacing or deleting it yourself."]):
                self.run("cmd_forget", rec["path"], "Forgetting", rec)
        elif k == "s":
            if self.ctx.confirm("Start a read-only scrub now?",
                                ["It reads every block of the volume and changes nothing. It runs as a manual job: "
                                 "other tools' jobs wait for it, UniFi's own don't."]):
                self.run("cmd_scrub", "start", "Starting the scrub")
        elif self.table.key(k):
            pass
        else:
            return False
        return True

    def restore(self, recs):
        one = len(recs) == 1
        lines = []
        for rec in recs[:4]:
            lines.append(f"{short(rec)}  from {rec['copy']['label']}")
        if len(recs) > 4:
            lines.append(f"... and {len(recs) - 4} more")
        lines.append("The damaged file is replaced in one step and deleted. Owner, group and mode stay as they are.")
        title = f"Restore {short(recs[0])}?" if one else f"Restore {len(recs)} files?"
        if self.ctx.confirm(title, lines):
            self.run("cmd_restore", recs[0]["path"] if one else "--all", "Restoring", recs[0] if one else None)

    def run(self, name, target, label, rec=None):
        """Run a scrub_repair command function under the run lock, and say
        the last line it printed (rec's path shortened to share/path)."""
        sr = self.env()
        fn = getattr(sr, name)
        buf = io.StringIO()
        try:
            cfg = sr.Config(sr.CONFIG_FILE)
            lock = contextlib.nullcontext() if self.ctx.dry_run else self.ctx.lock()
            with lock, contextlib.redirect_stdout(buf):
                rc = self.ctx.run_slow(lambda: fn(cfg, Args([target], self.ctx.dry_run)), label)
        except self.ctx.LockBusy:
            self.ctx.message("The 15-minute run is busy (it may be copying a file). Try again in a minute.", "warn")
            return
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        text = lines[-1] if lines else ("Done." if rc == 0 else "Nothing done.")
        if rec:
            text = text.replace(rec["path"], short(rec))
        self.ctx.message(text, "good" if rc == 0 else "bad")
        self.load()

    # ------------------------------------------------------------ drawing

    def render(self, c):
        W = self.ctx.widgets
        sr = self.env()
        s = self.state.get("scrub") or {}
        c.styled(0, 0, "Scrub", "label")
        c.styled(0, 7, W.fit(sr.scrub_line(s), c.width - 8), "value")
        detail_h = self.DETAIL_H if self.current and c.height >= 14 else 0
        self.table.render(c.sub(2, 0, max(2, c.height - 2 - detail_h), c.width))
        rec = self.current
        if detail_h and rec:
            inner = W.panel(c, c.height - detail_h, 0, detail_h, c.width, "Selected file")
            W.KeyValue(detail_rows(sr, rec)).render(inner)
