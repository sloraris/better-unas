"""Webhook messages and heartbeat pings, the same for every tool and the
TUI's test send. Standard library only."""

import json
import socket
import urllib.error
import urllib.request

FORMATS = ("text", "json", "slack", "discord")


def webhook_request(url, fmt, m, host, source):
    """The POST for one message m ({level, msg, at}) in WEBHOOK_FORMAT fmt,
    from `source` (a tool's name, or "better-unas" for the TUI's test)."""
    title = f"{host} {source}: {m['level']}"
    fmt = (fmt or "text").lower()
    if fmt == "json":
        body, ctype = json.dumps({"host": host, "source": source, "level": m["level"],
                                  "message": m["msg"], "at": m["at"]}), "application/json"
    elif fmt == "slack":
        body, ctype = json.dumps({"text": f"*{title}*\n{m['msg']}"}), "application/json"
    elif fmt == "discord":
        body, ctype = json.dumps({"content": f"**{title}**\n{m['msg']}"}), "application/json"
    else:  # plain text; works with ntfy, Gotify-style relays and most generic receivers
        body, ctype = f"{title}\n{m['msg']}", "text/plain; charset=utf-8"
    req = urllib.request.Request(url, data=body.encode(), method="POST")
    req.add_header("Content-Type", ctype)
    req.add_header("Title", title)
    return req


def http_error(e):
    """A short description of a failed request, for the journal and the TUI."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code} {e.reason}".strip()
    if isinstance(e, urllib.error.URLError):
        e = e.reason
    if isinstance(e, socket.timeout) or "timed out" in str(e):
        return "timed out"
    return str(e) or type(e).__name__


def user_agent(source):
    # Some hosts (Discord, others behind Cloudflare) refuse urllib's default
    # "Python-urllib/3.x" with HTTP 403, so webhooks and pings name themselves.
    return source if source == "better-unas" else f"{source} (better-unas)"


def open_url(req, timeout=10, source="better-unas"):
    """Fetch req (a URL or Request). Returns None on a 2xx answer, else a short error."""
    if isinstance(req, str):
        req = urllib.request.Request(req)
    if not req.has_header("User-agent"):
        req.add_header("User-Agent", user_agent(source))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            if 200 <= r.status < 300:
                return None
            return f"HTTP {r.status} {r.reason}".strip()
    except urllib.error.HTTPError as e:
        # The answer's first line, when it's short text, says why a host refused
        # (Cloudflare's "error code: 1010").
        try:
            first = (e.read(200).decode("utf-8", "replace").strip().splitlines() or [""])[0].strip()
        except Exception:
            first = ""
        why = http_error(e)
        return why + (f" ({first})" if first and len(first) <= 60 and not first.startswith(("<", "{", "[")) else "")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return http_error(e)


def send_webhook(url, fmt, m, host=None, source="better-unas", timeout=10):
    """POST one message to the webhook. Returns None if it was accepted, else why not."""
    try:
        req = webhook_request(url, fmt, m, host or socket.gethostname(), source)
    except ValueError as e:      # not a URL urllib can use
        return str(e)
    return open_url(req, timeout, source)


def heartbeat_urls(cfg):
    """(ok URL, failure URL): the tool's own pair when OWN_HEARTBEAT_URL is
    set, else the shared pair from Notifications."""
    if cfg["OWN_HEARTBEAT_URL"]:
        return cfg["OWN_HEARTBEAT_URL"], cfg["OWN_HEARTBEAT_FAIL_URL"]
    return cfg["HEARTBEAT_URL"], cfg["HEARTBEAT_FAIL_URL"]
