"""The `mhapi` CLI against a stand-in service: output contract, exit status, argument handling."""
import json
import os
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.helpers import PY, REPO, free_port


class FakeService(BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        command = self.path.rsplit("/", 1)[-1]
        FakeService.calls.append((command, body))
        if command in ("slow", "prepare"):
            time.sleep(3)
        status, out = {
            "get_status": (200, {"t": 1.0, "ready": True}),
            "get_image": (200, {"label": body.get("label"), "image": "/x.png"}),
            "set_temperature": (400, {"error": "temperature_limit", "message": "too hot", "t": 1.0}),
            "describe": (200, {"tools": [{"name": "get_image", "description": "from the service", "input_schema": {}}]}),
            "stop": (503, {"error": "service_unavailable", "message": "shutting down", "t": 1.0}),
            "prepare": (200, {"t": 1.0}),
        }.get(command, (200, {"ok": True}))
        raw = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), FakeService)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def run_cli(self, *args, url=None):
        env = {**os.environ, "MHAPI_URL": url or f"http://127.0.0.1:{self.port}"}
        p = subprocess.run([PY, str(REPO / "bin" / "mhapi"), *args], capture_output=True, text=True, env=env, timeout=30)
        lines = p.stdout.splitlines()
        self.assertEqual(len(lines), 1, f"exactly one JSON line on stdout, got {p.stdout!r}")
        return p.returncode, json.loads(lines[0])

    def test_success_exit_0(self):
        code, out = self.run_cli("get_status")
        self.assertEqual((code, out["ready"]), (0, True))

    def test_service_error_exit_1(self):
        code, out = self.run_cli("set_temperature", "120")
        self.assertEqual((code, out["error"]), (1, "temperature_limit"))
        self.assertEqual(FakeService.calls[-1], ("set_temperature", {"temperature": 120}))

    def test_usage_errors_exit_2(self):
        for args in ([], ["bogus"], ["set_temperature"], ["set_temperature", "abc"], ["set_temperature", "inf"],
                     ["get_image"], ["get_image", "P3-A1", "P3-A2"], ["get_status", "x"], ["get_status", "--nope"],
                     ["get_status", "--timeout", "0"], ["get_status", "--timeout"]):
            code, out = self.run_cli(*args)
            self.assertEqual((code, out["error"]), (2, "invalid_argument"), args)
            self.assertIn("message", out)
            self.assertIn("t", out)

    def test_negative_temperature_goes_to_the_service(self):
        code, out = self.run_cli("set_temperature", "-5")  # a number: the service answers invalid_argument
        self.assertEqual(FakeService.calls[-1], ("set_temperature", {"temperature": -5}))

    def test_unreachable_exit_3(self):
        code, out = self.run_cli("get_status", url=f"http://127.0.0.1:{free_port()}")
        self.assertEqual((code, out["error"]), (3, "service_unavailable"))

    def test_shutting_down_service_exit_3(self):
        code, out = self.run_cli("stop")
        self.assertEqual((code, out["error"]), (3, "service_unavailable"))

    def test_client_timeout_exit_1(self):
        t0 = time.time()
        code, out = self.run_cli("prepare", "--timeout", "0.5")
        self.assertEqual((code, out["error"]), (1, "client_timeout"))
        self.assertIn("last_operation", out["message"])
        self.assertLess(time.time() - t0, 2.5)

    def test_timeout_against_slow_call(self):
        # The fake answers "slow" after 3 s; the CLI only accepts known commands, so call the client directly.
        from mhapi import client
        with self.assertRaises(client.ClientTimeout):
            client.call("slow", {}, timeout=0.5, url=f"http://127.0.0.1:{self.port}")

    def test_inline_image_and_label(self):
        code, out = self.run_cli("get_image", "P3-A1", "--inline-image", "--timeout=5")
        self.assertEqual(code, 0)
        self.assertEqual(FakeService.calls[-1], ("get_image", {"label": "P3-A1", "inline_image": True}))

    def test_help_from_the_service(self):
        code, out = self.run_cli("get_image", "--help")
        self.assertEqual((code, out["description"]), (0, "from the service"))

    def test_help_local_fallback(self):
        code, out = self.run_cli("get_raw_opacity", "--help", url=f"http://127.0.0.1:{free_port()}")
        self.assertEqual(code, 0)
        self.assertIn("gray", out["description"])
        self.assertIn("enum", out["input_schema"]["properties"]["label"])

    def test_general_help(self):
        code, out = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("load_well", out["commands"])


class ToolsTest(unittest.TestCase):
    def test_definitions(self):
        from mhapi.tools import LABELS, tool_definitions
        tools = tool_definitions()
        self.assertEqual([t["name"] for t in tools], ["get_status", "prepare", "set_temperature", "get_temperature",
                                                      "get_image", "get_raw_opacity", "load_well", "clean_all_wells",
                                                      "list_wells"])
        self.assertEqual(len(LABELS), 32)
        self.assertEqual(LABELS[0], "C-A1")
        self.assertEqual(LABELS[-1], "P4-B2")
        by = {t["name"]: t for t in tools}
        self.assertEqual(by["get_image"]["input_schema"]["properties"]["label"]["enum"], LABELS)
        self.assertEqual(by["set_temperature"]["input_schema"]["required"], ["temperature"])
        self.assertEqual(by["get_status"]["input_schema"], {"type": "object", "properties": {}, "additionalProperties": False})


if __name__ == "__main__":
    unittest.main()
