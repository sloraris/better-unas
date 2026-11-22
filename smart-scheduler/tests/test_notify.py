"""Webhook, heartbeat and "send test" tests. Run from the smart-scheduler folder:

    python3 -m unittest discover -s tests

Everything goes to a throwaway http.server on 127.0.0.1; nothing reaches a
real webhook.
"""

import datetime as dt
import http.server
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ss_notify", os.path.join(HERE, "..", "smart_scheduler.py"))
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


class FakeEndpoint:
    """A local HTTP endpoint that records requests and answers with `status`."""

    def __init__(self, status=200):
        self.status = status
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({"method": self.command, "path": self.path,
                                       "headers": dict(self.headers),
                                       "body": self.rfile.read(n).decode()})
                self.send_response(outer.status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            do_GET = do_POST = _answer

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()

    def url(self, path="/hook"):
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def closed_port_url():
    """A URL on 127.0.0.1 where nothing listens."""
    s = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    port = s.server_port
    s.server_close()
    return f"http://127.0.0.1:{port}/hook"


class SharedSettingsTests(unittest.TestCase):
    """The webhook, heartbeat and repeat come from better-unas.conf, with the
    tool's own config as the fallback (docs/plugin-contract.md)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.tool = os.path.join(self.dir, "config")
        self.core = os.path.join(self.dir, "better-unas.conf")

    def write(self, path, text):
        with open(path, "w") as f:
            f.write(text)

    def config(self):
        return ss.Config(self.tool, core_path=self.core)

    def test_better_unas_conf_wins(self):
        self.write(self.tool, "WEBHOOK_URL=https://old.example/x\nALERT_REPEAT_HOURS=12\n")
        self.write(self.core, "WEBHOOK_URL=https://ntfy.sh/nas\nWEBHOOK_FORMAT=slack\n")
        c = self.config()
        self.assertEqual((c["WEBHOOK_URL"], c["WEBHOOK_FORMAT"]), ("https://ntfy.sh/nas", "slack"))
        self.assertEqual(c.int("ALERT_REPEAT_HOURS"), 12)        # not in better-unas.conf: the tool's
        self.assertEqual(c.errors, [])

    def test_an_empty_value_there_still_counts(self):
        self.write(self.tool, "WEBHOOK_URL=https://old.example/x\n")
        self.write(self.core, "WEBHOOK_URL=\n")
        self.assertEqual(self.config()["WEBHOOK_URL"], "")

    def test_defaults_and_bad_values(self):
        c = self.config()
        self.assertEqual((c["WEBHOOK_URL"], c["WEBHOOK_FORMAT"], c.yes("NOTIFY_SUCCESS"),
                          c.int("ALERT_REPEAT_HOURS")), ("", "text", False, 72))
        self.write(self.core, "WEBHOOK_FORMAT=teams\n")
        c = self.config()
        self.assertEqual(c["WEBHOOK_FORMAT"], "text")
        self.assertEqual(c.errors, ["WEBHOOK_FORMAT: want one of text, json, slack, discord"])

    def test_old_keys_in_the_tool_config_are_not_unknown(self):
        self.write(self.tool, "HEARTBEAT_URL=https://hc.example/ping\nNOPE=1\n")
        self.assertEqual(self.config().errors, ["NOPE: unknown setting"])


class AlertsTests(unittest.TestCase):
    class Cfg(dict):
        def int(self, k):
            return int(self[k])

    def setUp(self):
        self.ep = FakeEndpoint()
        self.addCleanup(self.ep.close)
        self.cfg = self.Cfg(WEBHOOK_URL=self.ep.url(), WEBHOOK_FORMAT="discord", ALERT_REPEAT_HOURS="24")
        self.log = []
        self.orig_log = ss.log
        ss.log = lambda msg, level=None: self.log.append(msg)
        self.addCleanup(setattr, ss, "log", self.orig_log)

    def test_flush_sends_through_the_shared_builder(self):
        state = {}
        a = ss.Alerts(self.cfg, state, dry=False)
        a.raise_("test:ZL1", "Extended test failed.")
        a.flush()
        self.assertEqual(state["outbox"], [])
        (got,) = self.ep.requests
        self.assertEqual(json.loads(got["body"])["content"].split("\n")[1], "Extended test failed.")

    def test_failed_send_keeps_the_outbox(self):
        self.ep.status = 503
        state = {}
        a = ss.Alerts(self.cfg, state, dry=False)
        a.info("hello")
        a.flush()
        self.assertEqual(len(state["outbox"]), 1)
        self.assertIn("webhook unreachable", self.log[-1])

    def test_clear_records_the_resolve_time(self):
        state = {}
        a = ss.Alerts(self.cfg, state, dry=True)
        a.raise_("triggers", "Triggers broken.")
        since = state["alerts"]["triggers"]["since"]
        a.clear("triggers")
        (r,) = state["resolved"]
        self.assertEqual((r["key"], r["msg"], r["level"], r["since"]),
                         ("triggers", "Triggers broken.", "error", since))
        self.assertGreaterEqual(dt.datetime.fromisoformat(r["resolved"]), dt.datetime.fromisoformat(since))
        for i in range(ss.RESOLVED_KEEP + 5):
            a.raise_(f"k{i}", "x")
            a.clear(f"k{i}")
        self.assertEqual(len(state["resolved"]), ss.RESOLVED_KEEP)
        self.assertEqual(state["resolved"][-1]["key"], f"k{ss.RESOLVED_KEEP + 4}")


if __name__ == "__main__":
    unittest.main()
