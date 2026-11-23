"""core/notify.py: the shared webhook's test send, the summary lines and the
one-time move out of the tools' configs. Run from the repository root:

    python3 -m unittest discover -s core/tests

Test sends go to a throwaway http.server on 127.0.0.1.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "smart-scheduler", "tests"))

from core import notify as N                      # noqa: E402
from core import updates as U                     # noqa: E402
from test_notify import FakeEndpoint, closed_port_url   # noqa: E402

spec = importlib.util.spec_from_file_location("ss_for_notify", os.path.join(ROOT, "smart-scheduler",
                                                                            "smart_scheduler.py"))
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


class SendTestTests(unittest.TestCase):
    def setUp(self):
        self.ep = FakeEndpoint()
        self.addCleanup(self.ep.close)

    def values(self, fmt="text", **kw):
        v = {"WEBHOOK_URL": self.ep.url(), "WEBHOOK_FORMAT": fmt,
             "HEARTBEAT_URL": self.ep.url("/ping"), "HEARTBEAT_FAIL_URL": self.ep.url("/ping/fail")}
        v.update(kw)
        return v

    def test_every_format_has_the_shape_of_a_real_alert(self):
        for fmt in ("text", "json", "slack", "discord"):
            with self.subTest(fmt=fmt):
                self.ep.requests.clear()
                self.assertEqual(N.send_test(self.values(fmt), "WEBHOOK_URL", host="nas"),
                                 f"test alert sent ({fmt} format)")
                (got,) = self.ep.requests
                self.assertEqual((got["method"], got["path"]), ("POST", "/hook"))
                self.assertEqual(got["headers"]["Title"], "nas better-unas: info")
                # smart-scheduler's scheduled run builds the same request, with its own name.
                m = {"level": "info", "msg": N.TEST_MESSAGE, "at": "X"}
                tool = ss.notify.webhook_request("http://x/", fmt, m, "nas", "smart-scheduler")
                self.assertEqual(got["headers"]["Content-Type"], tool.get_header("Content-type"))
                body = got["body"].replace("better-unas: info", "smart-scheduler: info") \
                                  .replace('"source": "better-unas"', '"source": "smart-scheduler"')
                if fmt == "text":
                    self.assertEqual(body, tool.data.decode())
                else:
                    body, want = json.loads(body), json.loads(tool.data)
                    if fmt == "json":
                        body.pop("at"), want.pop("at")
                    self.assertEqual(body, want)

    def test_heartbeat_urls_get_a_get(self):
        self.assertEqual(N.send_test(self.values(), "HEARTBEAT_URL"), "heartbeat ping answered")
        self.assertEqual(N.send_test(self.values(), "HEARTBEAT_FAIL_URL"), "fail ping answered")
        self.assertEqual([(r["method"], r["path"]) for r in self.ep.requests],
                         [("GET", "/ping"), ("GET", "/ping/fail")])

    def test_requests_name_themselves(self):
        # Discord and other hosts behind Cloudflare answer urllib's default
        # User-Agent with "403 error code: 1010".
        N.send_test(self.values("discord"), "WEBHOOK_URL")
        N.send_test(self.values(), "HEARTBEAT_URL")
        self.assertEqual([r["headers"].get("User-Agent") for r in self.ep.requests],
                         ["better-unas", "better-unas"])
        self.assertIsNone(ss.TOOL.ping(self.ep.url("/ping")))
        self.assertEqual(self.ep.requests[-1]["headers"].get("User-Agent"), "smart-scheduler (better-unas)")
        m = {"level": "info", "msg": "x", "at": "X"}
        self.assertIsNone(ss.notify.send_webhook(self.ep.url(), "slack", m, "nas", "smart-scheduler"))
        self.assertEqual(self.ep.requests[-1]["headers"].get("User-Agent"), "smart-scheduler (better-unas)")

    def test_a_refusal_says_why(self):
        self.ep.status, self.ep.body = 403, b"error code: 1010\n"
        with self.assertRaises(N.SendError) as e:
            N.send_test(self.values(), "WEBHOOK_URL")
        self.assertEqual(str(e.exception), "HTTP 403 Forbidden (error code: 1010)")
        self.assertEqual(ss.notify.open_url(self.ep.url()), "HTTP 403 Forbidden (error code: 1010)")
        self.ep.body = b"<html>long page</html>"                 # markup isn't a reason
        self.assertEqual(ss.notify.open_url(self.ep.url()), "HTTP 403 Forbidden")

    def test_errors(self):
        self.ep.status = 404
        with self.assertRaises(N.SendError) as e:
            N.send_test(self.values("slack"), "WEBHOOK_URL")
        self.assertEqual(str(e.exception), "HTTP 404 Not Found")
        with self.assertRaises(N.SendError) as e:
            N.send_test(self.values(WEBHOOK_URL=closed_port_url()), "WEBHOOK_URL", timeout=2)
        self.assertIn("refused", str(e.exception).lower())
        with self.assertRaises(N.SendError) as e:
            N.send_test(self.values(WEBHOOK_URL=""), "WEBHOOK_URL")
        self.assertEqual(str(e.exception), "no URL set")
        self.assertIsNone(N.send_test(self.values(), "WEBHOOK_FORMAT"))


class SummaryTests(unittest.TestCase):
    def test_host_only_never_the_path(self):
        self.assertEqual(N.describe({"WEBHOOK_URL": "https://www.ntfy.sh/secret", "HEARTBEAT_URL": "https://h/x"}),
                         "webhook: ntfy.sh · heartbeat: on")
        self.assertEqual(N.describe({}), "webhook: off · heartbeat: off")
        self.assertEqual(N.sent_to({"WEBHOOK_URL": "https://hooks.slack.com/services/T/B/x"}), "hooks.slack.com")
        self.assertIsNone(N.sent_to({"WEBHOOK_URL": ""}))


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        self.conf = os.path.join(self.home, U.CORE_CONFIG)

    def tool(self, name, config, where="tool.conf"):
        d = os.path.join(self.home, name)
        os.makedirs(d)
        with open(os.path.join(d, where), "w") as f:
            f.write(f"NAME={name}\nCONFIG=config\n")
        with open(os.path.join(d, "config"), "w") as f:
            f.write(config)

    def read(self):
        with open(self.conf) as f:
            return f.read()

    def test_copies_once_and_keeps_later_edits(self):
        self.tool("a-tool", "ENABLED=yes\nWEBHOOK_URL=https://ntfy.sh/a\nNOTIFY_SUCCESS=true\n"
                            "ALERT_REPEAT_HOURS=0\n")
        self.tool("b-tool", "WEBHOOK_URL=https://ntfy.sh/b\nHEARTBEAT_URL=https://hc.example/p\n",
                  where="tool.conf.uninstalled")
        with open(self.conf, "w") as f:
            f.write("DEV_MODE=no\n")
        self.assertEqual(N.migrate(self.home), ["HEARTBEAT_URL", "NOTIFY_SUCCESS", "WEBHOOK_URL"])
        text = self.read()
        self.assertTrue(text.startswith("DEV_MODE=no\n"))
        self.assertIn("WEBHOOK_URL=https://ntfy.sh/a\n", text)         # the first tool wins
        self.assertIn("NOTIFY_SUCCESS=yes\n", text)                     # canonical form
        self.assertIn("HEARTBEAT_URL=https://hc.example/p\n", text)
        self.assertNotIn("ALERT_REPEAT_HOURS", text)                    # a bad value stays behind
        self.assertEqual(os.stat(self.conf).st_mode & 0o777, 0o600)
        with open(self.conf, "a") as f:
            f.write("WEBHOOK_URL=\n")                                   # cleared by hand later
        self.assertEqual(N.migrate(self.home), [])
        self.assertEqual(U.core_settings(self.home)["HEARTBEAT_URL"], "https://hc.example/p")

    def test_nothing_to_move_writes_nothing(self):
        self.tool("a-tool", "ENABLED=yes\n")
        self.assertEqual(N.migrate(self.home), [])
        self.assertFalse(os.path.exists(self.conf))
        self.assertEqual(U.move_shared(os.path.join(self.home, "missing")), [])


if __name__ == "__main__":
    unittest.main()
