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


class SendTestTests(unittest.TestCase):
    def setUp(self):
        self.ep = FakeEndpoint()
        self.addCleanup(self.ep.close)

    def values(self, fmt="text", **kw):
        v = {"WEBHOOK_URL": self.ep.url(), "WEBHOOK_FORMAT": fmt,
             "HEARTBEAT_URL": self.ep.url("/ping"), "HEARTBEAT_FAIL_URL": self.ep.url("/ping/fail")}
        v.update(kw)
        return v

    def test_every_format_matches_the_scheduled_runs_payload(self):
        for fmt in ("text", "json", "slack", "discord"):
            with self.subTest(fmt=fmt):
                self.ep.requests.clear()
                text = ss.send_test(self.values(fmt), "WEBHOOK_URL", host="nas")
                self.assertEqual(text, f"test alert sent ({fmt} format)")
                (got,) = self.ep.requests
                self.assertEqual((got["method"], got["path"]), ("POST", "/hook"))
                # The same builder the scheduled run's Alerts._send uses.
                m = {"level": "info", "msg": ss.TEST_MESSAGE, "at": "X"}
                want = ss.webhook_request("http://x/", fmt, m, "nas")
                self.assertEqual(got["headers"]["Content-Type"], want.get_header("Content-type"))
                self.assertEqual(got["headers"]["Title"], "nas smart-scheduler: info")
                if fmt == "text":
                    self.assertEqual(got["body"], f"nas smart-scheduler: info\n{ss.TEST_MESSAGE}")
                else:
                    body = json.loads(got["body"])
                    expect = json.loads(want.data)
                    if fmt == "json":
                        self.assertTrue(dt.datetime.fromisoformat(body.pop("at")))
                        expect.pop("at")
                    self.assertEqual(body, expect)
        self.assertEqual(json.loads(ss.webhook_request("http://x/", "slack", m, "nas").data),
                         {"text": f"*nas smart-scheduler: info*\n{ss.TEST_MESSAGE}"})
        self.assertEqual(json.loads(ss.webhook_request("http://x/", "discord", m, "nas").data),
                         {"content": f"**nas smart-scheduler: info**\n{ss.TEST_MESSAGE}"})

    def test_heartbeat_urls_get_a_get(self):
        self.assertEqual(ss.send_test(self.values(), "HEARTBEAT_URL"), "heartbeat ping answered")
        self.assertEqual(ss.send_test(self.values(), "HEARTBEAT_FAIL_URL"), "fail ping answered")
        self.assertEqual([(r["method"], r["path"]) for r in self.ep.requests],
                         [("GET", "/ping"), ("GET", "/ping/fail")])

    def test_http_error_is_reported(self):
        self.ep.status = 404
        with self.assertRaises(ss.SendError) as e:
            ss.send_test(self.values("slack"), "WEBHOOK_URL")
        self.assertEqual(str(e.exception), "HTTP 404 Not Found")
        self.ep.status = 500
        with self.assertRaises(ss.SendError) as e:
            ss.send_test(self.values(), "HEARTBEAT_URL")
        self.assertEqual(str(e.exception), "HTTP 500 Internal Server Error")

    def test_unreachable_and_missing(self):
        with self.assertRaises(ss.SendError) as e:
            ss.send_test(self.values(WEBHOOK_URL=closed_port_url()), "WEBHOOK_URL", timeout=2)
        self.assertIn("refused", str(e.exception).lower())
        with self.assertRaises(ss.SendError) as e:
            ss.send_test(self.values(WEBHOOK_URL=""), "WEBHOOK_URL")
        self.assertEqual(str(e.exception), "no URL set")
        self.assertIsNone(ss.send_test(self.values(), "DRIVE_USER"))
        self.assertEqual(self.ep.requests, [])


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
