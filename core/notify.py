"""Notifications: the suite's shared webhook and heartbeat (docs/tui-style.md §6).

The settings live in better-unas.conf (core/settings.json, group
"notifications"). Tools don't import the core: each reads these keys from
<home>/better-unas.conf, a plain KEY=value file, and falls back to its own
config (docs/plugin-contract.md, "Shared settings").

    KEYS          the shared keys
    send_test     Settings › Notifications' `t`: one test through a URL setting
    describe      "webhook: ntfy.sh · heartbeat: on", for the Settings list
    sent_to       where alerts go, for the Alerts header
    migrate       moves the keys a tool's config still holds into better-unas.conf

The payloads match smart-scheduler's webhook_request(), so a test looks like
a real alert.
"""

import datetime as dt
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import install as I
from . import plugins as P

KEYS = P.SHARED_KEYS
URL_KEYS = ("WEBHOOK_URL", "HEARTBEAT_URL", "HEARTBEAT_FAIL_URL")
SOURCE = "better-unas"
TEST_MESSAGE = "Test message from better-unas. If you can read this, alerts will arrive here."


# Some hosts (Discord, others behind Cloudflare) refuse urllib's default
# "Python-urllib/3.x" with HTTP 403, so every request names itself.
USER_AGENT = "better-unas"


def _with_agent(req):
    if isinstance(req, str):
        req = urllib.request.Request(req)
    if not req.has_header("User-agent"):
        req.add_header("User-Agent", USER_AGENT)
    return req


def _why(e):
    """'HTTP 403 Forbidden', plus the answer's first line when it's short text
    (Cloudflare's "error code: 1010" says why a host refused)."""
    out = f"HTTP {e.code} {e.reason}".strip()
    try:
        body = e.read(200).decode("utf-8", "replace").strip().splitlines()
    except Exception:
        body = []
    first = body[0].strip() if body else ""
    if first and len(first) <= 60 and not first.startswith(("<", "{", "[")):
        out += f" ({first})"
    return out


class SendError(Exception):
    """A test send failed; the message is shown as is (e.g. "HTTP 404 Not Found")."""


def webhook_request(url, fmt, level, msg, at, host):
    title = f"{host} {SOURCE}: {level}"
    fmt = (fmt or "text").lower()
    if fmt == "json":
        body, ctype = json.dumps({"host": host, "source": SOURCE, "level": level,
                                  "message": msg, "at": at}), "application/json"
    elif fmt == "slack":
        body, ctype = json.dumps({"text": f"*{title}*\n{msg}"}), "application/json"
    elif fmt == "discord":
        body, ctype = json.dumps({"content": f"**{title}**\n{msg}"}), "application/json"
    else:
        body, ctype = f"{title}\n{msg}", "text/plain; charset=utf-8"
    req = urllib.request.Request(url, data=body.encode(), method="POST")
    req.add_header("Content-Type", ctype)
    req.add_header("Title", title)
    return req


def open_url(req, timeout=10):
    """None on a 2xx answer, else a short reason."""
    try:
        with urllib.request.urlopen(_with_agent(req), timeout=timeout) as r:
            r.read()
            if 200 <= r.status < 300:
                return None
            return f"HTTP {r.status} {r.reason}".strip()
    except urllib.error.HTTPError as e:
        return _why(e)
    except (urllib.error.URLError, OSError, ValueError) as e:
        e = getattr(e, "reason", e)
        if isinstance(e, socket.timeout) or "timed out" in str(e):
            return "timed out"
        return str(e) or type(e).__name__


def send_test(values, key, host=None, timeout=10):
    """A test through one URL setting, using values (typed, unsaved edits
    included): the webhook gets a test alert in WEBHOOK_FORMAT, a heartbeat
    URL a GET. Returns a short success text, None for a key with no test;
    raises SendError with the reason."""
    if key not in URL_KEYS:
        return None
    url = values.get(key) or ""
    if not url:
        raise SendError("no URL set")
    if key == "WEBHOOK_URL":
        fmt = (values.get("WEBHOOK_FORMAT") or "text").lower()
        try:
            req = webhook_request(url, fmt, "info", TEST_MESSAGE,
                                  dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                                  host or socket.gethostname())
        except ValueError as e:
            raise SendError(str(e))
        err, what = open_url(req, timeout), f"test alert sent ({fmt} format)"
    else:
        err = open_url(url, timeout)
        what = "heartbeat ping answered" if key == "HEARTBEAT_URL" else "fail ping answered"
    if err:
        raise SendError(err)
    return what


def host_of(url):
    """The host a URL reaches ("ntfy.sh"), never its path, which often holds a token."""
    try:
        host = urllib.parse.urlsplit(url or "").hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def describe(values):
    """One line for the Settings list."""
    hook = host_of(values.get("WEBHOOK_URL")) or "off"
    beat = "on" if values.get("HEARTBEAT_URL") else "off"
    return f"webhook: {hook} · heartbeat: {beat}"


def sent_to(values):
    """Where alerts go, for the Alerts header, or None with no webhook."""
    return host_of(values.get("WEBHOOK_URL")) or None


# --------------------------------------------------------------------- moving old values

def tool_configs(home):
    """[(tool name, config path)] for the installed and uninstalled tools."""
    out = []
    for name, manifest, where in I.catalog(home):
        if where == "available":
            continue
        cfg = manifest.get("CONFIG") or "config"
        if "/" not in cfg:
            out.append((name, os.path.join(home, name, cfg)))
    return out


def migrate(home, schema=None):
    """Copy shared keys that better-unas.conf doesn't have yet from the tools'
    configs (the first tool that sets one wins). Runs once in effect: a key
    in better-unas.conf is never copied again, so later edits there stand.
    A value that doesn't fit the schema is left behind. Returns the keys
    copied ([] when there was nothing to do)."""
    from . import configfile
    from . import updates as U
    schema = schema or U.core_schema()
    path = os.path.join(home, U.CORE_CONFIG)
    try:
        have, _ = P.read_keyvalue(path)
    except OSError:
        have = {}
    fields = P.field_map(schema)
    want = [k for k in KEYS if k in fields and k not in have]
    edits = {}
    for _, cfg in tool_configs(home):
        if not want:
            break
        try:
            raw, _ = P.read_keyvalue(cfg)
        except OSError:
            continue
        for k in list(want):
            if k in raw:
                want.remove(k)
                try:
                    edits[k] = P.format_value(fields[k], P.parse_value(fields[k], raw[k]))
                except P.FieldError:
                    pass
    if edits:
        configfile.save(path, edits, schema)
    return sorted(edits)
