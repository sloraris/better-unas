"""smart-scheduler's drive actions (#8): run a test now, exclude a drive,
set a drive's interval. Used by the CLI and by the TUI's screens.

    check_run_now(slot, kind)              -> [Check]
    run_now(slot, kind, override=False)    -> Result
    exclude(serial, on)                    -> True if the config changed
    set_interval(serial, days or None)     -> True if the config changed

A Check is one reason not to start a test right now. `overridable` ones (a
busy drive, a scrub or backup coming up, outside the start window) can be
overridden with a second confirm; blocking ones (a test already running on
any drive, an unhealthy pool, a drive that can't run SMART tests, Drive
refusing our identity) never can.

Every write and every start takes the run lock, the same flock on
`<tool>/.lock` that the hourly run, the snooze commands and the TUI's
ctx.lock() take, so nothing collides with a scheduled run. A caller that
already holds it (inside ctx.lock()) passes lock=False: flock locks belong
to an open file, so taking it a second time from the same process would
wait on itself. LockBusy is raised if it stays taken for `wait` seconds.

dry_run=True changes nothing: no API calls that start tests, no config or
state writes, and no lock (it would only make a dry run wait).

Standard library only.
"""

import contextlib
import copy
import datetime as dt
import json
from collections import namedtuple

import smart_scheduler as ss  # tests rebind this to the module they loaded

KINDS = ("quick", "extended")
# Drive's quick (short) test takes about two minutes; this leaves room.
QUICK_MINUTES = 5

# code: short id for the screen ("busy", "scrub", ...). reason: a sentence
# fragment for people. overridable: False means it always blocks.
Check = namedtuple("Check", "code reason overridable")
# started: a test was started (or, in a dry run, would have been).
# checks: what check_run_now found. message: one line for people.
Result = namedtuple("Result", "started checks message")


class LockBusy(Exception):
    """The run lock stayed taken for the whole wait."""

    def __init__(self, waited):
        self.waited = waited
        super().__init__("the scheduled run is still going; try again in a minute")


@contextlib.contextmanager
def run_lock(lock=True, wait=10):
    """Hold the tool's run lock for the with-block (nothing if lock is False)."""
    if not lock:
        yield
        return
    f = ss.take_lock(wait)
    if f is None:
        raise LockBusy(wait)
    try:
        yield
    finally:
        f.close()


def blocking(checks):
    return [c for c in checks if not c.overridable]


def overridable(checks):
    return [c for c in checks if c.overridable]


# --------------------------------------------------------------------------- run now

def _block(code, reason):
    return Check(code, reason, False)


def _soft(code, reason):
    return Check(code, reason, True)


def _identity(cfg, storage, state):
    """(user id, None) or (None, blocking Check), the way the hourly run picks it."""
    user_id, label, err = ss.resolve_identity(cfg)
    if err:
        user_id = state.get("user_id")   # the run carries on with the last known ID too
        if not user_id:
            return None, _block("identity", f"couldn't look up {label}: {err}")
    elif user_id is None:
        return None, _block("identity", f"{label} (DRIVE_USER in config) no longer exists")
    denied = ss.check_access(storage, user_id)
    if denied:
        return None, _block("access", f"can't use {label} for SMART tests: {denied}")
    return user_id, None


def _pool_checks(storage):
    """Unhealthy pools block; a scrub already going is only overridable."""
    calm = copy.deepcopy(storage)
    for p in calm["pools"]:
        (p.get("dataScrubbing") or {})["status"] = "idle"
    out = []
    health = ss.planner.pool_problem(calm)
    if health:
        out.append(_block("pool", health))
    scrub = ss.planner.pool_problem(storage)
    if scrub and scrub != health:
        out.append(_soft("scrub", scrub))
    return out


def _find(storage, slot):
    return next((d for d in storage["disks"] if str(d.get("slotId")) == str(slot)), None)


def _gather(slot, kind, cfg, t, state):
    """Everything run_now needs: (checks, context dict)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    cfg = cfg or ss.Config(ss.CONFIG_FILE)
    t = t or ss.now()
    state = ss.load_state() if state is None else state
    ctx = {"cfg": cfg, "t": t, "state": state, "disk": None, "user_id": None, "minutes": None}
    try:
        storage = ss.get_storage()
    except ss.ApiError as e:
        return [_block("api", f"UniFi Drive API problem: {e.detail}")], ctx
    ctx["storage"] = storage
    disk = _find(storage, slot)
    if disk is None:
        return [_block("drive", f"Drive {slot} is empty")], ctx
    ctx["disk"] = disk
    if not disk.get("serial") or not disk.get("smartTestSupported"):
        return [_block("smart", f"Drive {slot} doesn't support SMART tests")], ctx

    checks = []
    user_id, problem = _identity(cfg, storage, state)
    if problem:
        checks.append(problem)
    ctx["user_id"] = user_id
    checks += _pool_checks(storage)

    # A test running anywhere blocks, whatever its type and even on an
    # excluded drive, so every SMART-capable drive's history is read.
    if user_id:
        for d in storage["disks"]:
            if not d.get("serial") or not d.get("smartTestSupported"):
                continue
            try:
                history = ss.get_history(d["slotId"], d["serial"], user_id, d.get("smartTest"))
            except ss.ApiError as e:
                checks.append(_block("history", f"couldn't read SMART history for Drive {d['slotId']} "
                                                f"to check nothing is running: {e.detail}"))
                continue
            if any(ss.is_running(h) for h in history):
                checks.append(_block("running", f"a SMART test is already running on Drive {d['slotId']}"))
    # Tests started outside UniFi Drive (smartctl, other tools) only show on the drive itself.
    devices = ss.serial_to_device()
    for d in storage["disks"]:
        dev = devices.get(d.get("serial"))
        left = ss.self_test_progress(dev) if dev and d.get("smartTestSupported") else None
        if left is not None and not any(c.code == "running" and f"Drive {d['slotId']}" in c.reason
                                        for c in checks):
            checks.append(_block("running", f"a SMART test is already running on Drive {d['slotId']} "
                                            f"(started outside UniFi Drive, {left}% left)"))

    busy = cfg["BUSY_KBPS"]
    kbps = (disk.get("readKBPS") or 0) + (disk.get("writeKBPS") or 0)
    if busy and kbps > busy:
        checks.append(_soft("busy", f"drive busy ({kbps:g} KB/s)"))
    if not ss.in_window(cfg, t):
        checks.append(_soft("window", f"outside the start window ({cfg.values['START_WINDOW']})"))

    minutes = QUICK_MINUTES
    if kind == "extended":
        dev = ss.serial_to_device().get(disk["serial"])
        attr_ids = set(cfg["WARN_ATTRIBUTES"])
        minutes, attrs = ss.smart_facts(dev, attr_ids) if dev else (None, {})
        ctx["attrs"] = attrs
    ctx["minutes"] = minutes
    c = ss.scrub_conflict(cfg, storage, minutes, t)
    if c:
        checks.append(_soft("scrub", c))
    windows, info = ss.backup_windows(cfg, t, t + ss.planner.test_length(cfg, minutes))
    c = ss.planner.conflict(cfg, windows, minutes, t)
    if c:
        checks.append(_soft("backup", c))
    for _, msg in (info or {}).get("problems", ()):
        checks.append(_soft("backup", f"can't tell when backups run: {msg}"))
    c = ss.planner.conflict(cfg, ss.busy_windows(t), minutes, t, "manual")
    if c:
        checks.append(_soft("others", c))
    return checks, ctx


def check_run_now(slot, kind, cfg=None, t=None, state=None):
    """What stands in the way of starting a `kind` test on `slot` right now.

    Returns a list of Checks (empty: nothing does). Reads only; takes no lock.
    """
    return _gather(slot, kind, cfg, t, state)[0]


def run_now(slot, kind, override=False, cfg=None, dry_run=False, lock=True, wait=10, t=None):
    """Start a quick or extended test on `slot` through UniFi Drive's API.

    Blocking checks always stop it; overridable ones stop it unless
    `override`. Uses the same identity as the hourly run. Returns a Result.
    """
    with contextlib.nullcontext() if dry_run else run_lock(lock, wait):
        checks, ctx = _gather(slot, kind, cfg, t, None)
        hard, soft = blocking(checks), overridable(checks)
        if hard:
            return Result(False, checks, "Can't start: " + "; ".join(c.reason for c in hard) + ".")
        if soft and not override:
            return Result(False, checks, "Not started: " + "; ".join(c.reason for c in soft)
                          + ". Override to start anyway.")
        disk = ctx["disk"]
        label = f"Drive {disk['slotId']} ({disk['serial']}, {disk.get('model', '?')})"
        if dry_run:
            return Result(True, checks, f"(dry-run) would start a {kind} test on {label}.")
        try:
            status, body = ss.api("POST", f"/storage/disks/{disk['slotId']}/smart-tests",
                                  ctx["user_id"], {"type": kind})
        except ss.ApiError as e:
            return Result(False, checks, f"Couldn't start a test on {label}: {e.detail}.")
        if status == 409:
            return Result(False, checks, f"{label}: a test is already running (409).")
        if status not in (200, 201, 202):
            return Result(False, checks, f"Starting a test on {label} returned HTTP {status}: "
                                         f"{json.dumps(body)[:200]}")
        how = " (override: " + "; ".join(c.reason for c in soft) + ")" if soft else ""
        ss.log(f"started {kind} test on {label} by hand{how}")
        if kind == "extended":
            state = ss.load_state()
            ds = state.setdefault("drives", {}).setdefault(disk["serial"], {})
            # Same baseline the run records, for "attribute rose since last test".
            ds["attrs_at_last_test"] = ctx.get("attrs", {})
            ds["running_tier"] = "manual"          # other tools' lower-tier jobs give way to it
            ss.save_state(state, False)
            t = t or ss.now()
            end = t + ss.planner.test_length(cfg or ss.Config(ss.CONFIG_FILE), ctx.get("minutes"))
            ss.TOOL.nudge(ss.busy.in_the_way(ss.TOOL.others_busy(t), t, end, "manual"))
        return Result(True, checks, f"Started a {kind} test on {label}.")


# --------------------------------------------------------------------------- config edits

def _edit(key, change, path, dry_run, lock, wait):
    """Read `key` from the config, apply change(typed) -> new typed value, save."""
    path = path or ss.CONFIG_FILE
    with contextlib.nullcontext() if dry_run else run_lock(lock, wait):
        cfg = ss.Config(path)
        if key in cfg.field_errors:
            # Rewriting it from the default would silently drop what's there.
            raise ss.settings.ValidationError({key: f"fix the current value first ({cfg.field_errors[key]})"})
        old = cfg.typed[key]
        new = change(copy.deepcopy(old))
        if new == old:
            return False
        value = new if new else None   # empty: remove the line so the default applies
        if dry_run:
            ss.settings.apply_edits("", {key: value}, cfg.schema)   # validate only
        else:
            ss.settings.save(path, {key: value}, cfg.schema)
        return True


def exclude(serial, on, path=None, dry_run=False, lock=True, wait=10):
    """Add `serial` to EXCLUDE_SERIALS (on) or remove it. True if that changed the config.

    Raises settings.ValidationError for a serial the config can't hold."""
    def change(serials):
        if on and serial not in serials:
            serials.append(serial)
        elif not on and serial in serials:
            serials.remove(serial)
        return serials
    return _edit("EXCLUDE_SERIALS", change, path, dry_run, lock, wait)


def set_interval(serial, days, path=None, dry_run=False, lock=True, wait=10):
    """Set `serial`'s interval in INTERVAL_OVERRIDES, or remove it (days None)
    so the normal rules apply. True if that changed the config.

    Raises settings.ValidationError for an out-of-range value or bad serial."""
    def change(overrides):
        if days is None:
            overrides.pop(serial, None)
        else:
            overrides[serial] = days
        return overrides
    return _edit("INTERVAL_OVERRIDES", change, path, dry_run, lock, wait)
