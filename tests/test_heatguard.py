"""mhapi-heatguard's decision (safety spec H5), in --dry-run mode: it never connects to the printer here."""
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import PY, REPO


class HeatGuardTest(unittest.TestCase):
    def run_once(self, hb):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "heartbeat.json"
            if hb is not None:
                path.write_text(json.dumps(hb))
            p = subprocess.run([PY, str(REPO / "bin" / "mhapi-heatguard"), "--heartbeat", str(path), "--dry-run",
                                "--once"], capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            after = json.loads(path.read_text()) if path.exists() else None
            return json.loads(p.stdout.strip().splitlines()[-1]), after

    def test_service_died_while_heating(self):
        out, after = self.run_once({"t": time.time() - 200, "pid": 1, "target_c": 50.0, "wells_loaded": 2})
        self.assertEqual(out["action"], "heater_off")
        self.assertEqual(after["target_c"], 0.0)
        self.assertIn("heatguard", after)

    def test_no_action(self):
        now = time.time()
        for hb in (None,
                   {"t": now - 5, "pid": 1, "target_c": 50.0},          # service alive
                   {"t": now - 500, "pid": 1, "target_c": 0.0},         # dead, but the heater was off
                   {"t": now - 500, "pid": 1, "target_c": 50.0, "sim": True}):  # a simulator's heartbeat
            out, _ = self.run_once(hb)
            self.assertIsNone(out["action"], hb)

    def test_guard_does_not_import_the_service(self):
        code = ("import sys, mhapi.heatguard; bad = [m for m in sys.modules if m.startswith('mhapi.service')]; "
                "print(bad); sys.exit(1 if bad else 0)")
        p = subprocess.run([PY, "-c", code], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)


if __name__ == "__main__":
    unittest.main()
