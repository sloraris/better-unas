"""core/lib/drive.py: read-only psql and the console owner's id. Run from the
repo root:

    python3 -m unittest discover -s core/tests
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.lib import drive  # noqa: E402

OWNER = "0b2c8f4e-1111-2222-3333-444455556666"


class Psql(unittest.TestCase):
    def test_read_only_as_postgres(self):
        seen = {}

        def sh(*cmd, timeout=60, env=None):
            seen.update(cmd=cmd, env=env)
            return 0, "a\tb\nc\td\n"
        rows, err = drive.psql(sh, "unifi-drive", "SELECT 1")
        self.assertEqual((rows, err), ([["a", "b"], ["c", "d"]], None))
        self.assertEqual(seen["cmd"][:4], ("runuser", "-u", "postgres", "--"))
        self.assertIn("-c default_transaction_read_only=on", seen["env"]["PGOPTIONS"])

    def test_failure(self):
        rows, err = drive.psql(lambda *c, **k: (2, "no such database"), "x", "SELECT 1")
        self.assertIsNone(rows)
        self.assertEqual(err, "psql on x failed (no such database)")


class Owner(unittest.TestCase):
    def test_found(self):
        self.assertEqual(drive.owner_id(lambda *c, **k: (0, OWNER)), (OWNER, None))

    def test_missing_or_odd(self):
        for out in ("", "not-a-uuid"):
            uid, err = drive.owner_id(lambda *c, **k: (0, out))
            self.assertIsNone(uid)
            self.assertIn("console owner", err)


if __name__ == "__main__":
    unittest.main()
