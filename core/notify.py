"""Notifications: the suite's shared webhook and heartbeat (docs/tui-style.md §6).

The settings live in better-unas.conf (core/settings.json, group
"notifications"). Tools read these keys from <home>/better-unas.conf through
core.lib.config, falling back to their own config (docs/plugin-contract.md,
"Shared settings").

    KEYS          the shared keys
    send_test     Settings › Notifications' `t`: one test through a URL setting
    describe      "webhook: ntfy.sh · heartbeat: on", for the Settings list
    sent_to       where alerts go, for the Alerts header
    migrate       moves the keys a tool's config still holds into better-unas.conf

Messages are built and sent by core/lib/notify.py, the same code a tool's run
uses, so a test looks like a real alert.
"""

import datetime as dt
import os
import socket
import urllib.parse

from . import install as I
from . import plugins as P
from .lib import notify as lib_notify

KEYS = P.SHARED_KEYS
URL_KEYS = ("WEBHOOK_URL", "HEARTBEAT_URL", "HEARTBEAT_FAIL_URL")
SOURCE = "better-unas"
TEST_MESSAGE = "Test message from better-unas. If you can read this, alerts will arrive here."


class SendError(Exception):
    """A test send failed; the message is shown as is (e.g. "HTTP 404 Not Found")."""


def webhook_request(url, fmt, level, msg, at, host):
    return lib_notify.webhook_request(url, fmt, {"level": level, "msg": msg, "at": at}, host, SOURCE)


def open_url(req, timeout=10):
    """None on a 2xx answer, else a short reason."""
    return lib_notify.open_url(req, timeout, SOURCE)


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
