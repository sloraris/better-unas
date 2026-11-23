"""UniFi Drive's local API and read-only database access, for tools that
read what Drive knows (storage, backups) or ask it to do something.

    status, body = drive.api("GET", "/storage")
    rows, err = drive.psql(sh, "unifi-drive", "SELECT id FROM ...")
    user_id, err = drive.owner_id(sh)

The API listens on 127.0.0.1:16080 with no login: calls that act for a user
pass that user's id in X-UserId. The console owner always exists and always
has Drive management rights, so tools act as the owner by default, looked up
read-only in UniFi OS's ulp-go database.

Queries run as the postgres user with read-only transactions. Never select a
credential column: Drive's backup tables keep access keys and secrets next to
the schedule.

Standard library only.
"""

import json
import os
import re
import urllib.error
import urllib.request

API = "http://127.0.0.1:16080/api/v2"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

OWNER_QUERY = """SELECT u.unique_id FROM "user" u
 JOIN role_user ru ON ru.user_id = u.unique_id
 JOIN role r ON r.unique_id = ru.role_id
 WHERE r.system_key = 'owner' AND u.deleted_at IS NULL AND u.status = 'ACTIVE'
 ORDER BY u.created_at LIMIT 1"""


class ApiError(Exception):
    def __init__(self, kind, detail, status=None):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail, self.status = kind, detail, status


def api(method, path, user_id=None, body=None, timeout=15, base=API):
    """Call UniFi Drive's local API. Returns (HTTP status, parsed JSON or None)."""
    req = urllib.request.Request(base + path, method=method)
    if user_id:
        req.add_header("X-UserId", user_id)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except (urllib.error.URLError, OSError) as e:
        raise ApiError("unreachable", f"{method} {path}: {e}")
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    return status, parsed


def download(path, dest, user_id=None, timeout=60, base=API, chunk=1 << 20):
    """GET path into the file dest (created or truncated). Returns the HTTP
    status; the file holds the body only when it's 200."""
    req = urllib.request.Request(base + path)
    if user_id:
        req.add_header("X-UserId", user_id)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
            while True:
                block = r.read(chunk)
                if not block:
                    break
                f.write(block)
            f.flush()
            os.fsync(f.fileno())
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError) as e:
        raise ApiError("unreachable", f"GET {path}: {e}")


def psql(sh, db, query):
    """Run a read-only query as the postgres user with `sh` (core.lib.util.sh
    or a tool's wrapper). Returns (rows as lists, error or None)."""
    env = dict(os.environ, PGOPTIONS="-c default_transaction_read_only=on")
    rc, out = sh("runuser", "-u", "postgres", "--", "psql", "-p", "5432", "-d", db,
                 "-At", "-F", "\t", "-c", query, env=env, timeout=20)
    if rc != 0:
        return None, f"psql on {db} failed ({out[:200] or 'exit ' + str(rc)})"
    return [line.split("\t") for line in out.splitlines() if line.strip()], None


def owner_id(sh):
    """(the console owner's UniFi OS user id, None) or (None, why not)."""
    rows, err = psql(sh, "ulp-go", OWNER_QUERY)
    if err:
        return None, err
    if not rows or not UUID_RE.fullmatch(rows[0][0]):
        return None, "couldn't find the console owner in UniFi OS's user database"
    return rows[0][0], None
