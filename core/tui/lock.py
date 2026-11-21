"""The run lock: the same exclusive flock a tool's scheduled run takes.

Saves and actions from the TUI hold it so they never collide with the hourly
run (docs/plugin-contract.md, `ctx.lock()`). smart-scheduler's run opens
`<tool>/.lock` and takes `flock(LOCK_EX)`; this does the same.
"""

import contextlib
import fcntl
import os
import time


class LockBusy(Exception):
    """The lock stayed taken for the whole timeout."""

    def __init__(self, path, waited):
        self.path, self.waited = path, waited
        super().__init__("the scheduled run is still going; try again in a minute")


@contextlib.contextmanager
def run_lock(path, timeout=10, on_wait=None, poll=0.2, clock=time.monotonic, sleep=time.sleep):
    """Hold an exclusive flock on path for the duration of the with-block.

    If another process holds it, on_wait(elapsed_seconds) is called every
    `poll` seconds (the TUI redraws "waiting for the scheduled run" there)
    until `timeout`, then LockBusy is raised. The file is created if missing
    and never truncated or removed.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    f = open(path, "a")
    try:
        start = clock()
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                waited = clock() - start
                if waited >= timeout:
                    raise LockBusy(path, waited) from None
                if on_wait:
                    on_wait(waited)
                sleep(poll)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()
