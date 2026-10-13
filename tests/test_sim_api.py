"""End to end on the simulator: the real service and the real CLI, for every command (API spec)."""
import base64
import json
import signal
import tempfile
import time
import unittest
from pathlib import Path

import cv2
import numpy as np

from tests.helpers import SimService, wait_until


class ApiFlowTest(unittest.TestCase):
    """One simulated rig, homed at start, used by the tests in order."""

    @classmethod
    def setUpClass(cls):
        cls.svc = SimService(homed=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.svc.stop()

    def test_01_status_describe_list(self):
        code, s, _ = self.svc.cli("get_status")
        self.assertEqual(code, 0)
        for k in ("t", "ready", "homed", "printer_ok", "camera_ok", "camera_tilt_mm", "position_warning",
                  "position_check", "safety", "idle_s", "loaded_wells", "bed_c", "target_c", "bed_age_s",
                  "session", "calibration_id", "last_operation"):
            self.assertIn(k, s)
        self.assertTrue(s["homed"] and s["printer_ok"] and s["camera_ok"])
        self.assertEqual(s["safety"], "ok")
        code, d, _ = self.svc.cli("describe")
        self.assertEqual(len(d["tools"]), 9)
        self.assertEqual(len(d["tools"][4]["input_schema"]["properties"]["label"]["enum"]), 32)
        code, w, _ = self.svc.cli("list_wells")
        self.assertEqual((len(w["labels"]), w["labels"][0], w["labels"][-1]), (32, "C-A1", "P4-B2"))
        code, t, _ = self.svc.cli("get_temperature")
        self.assertEqual(set(t), {"bed_c", "target_c", "t", "age_s"})

    def test_02_reads(self):
        # the simulated printer starts over C-B2 with no saved position: the detour path proves the move
        code, r, _ = self.svc.cli("get_raw_opacity", "C-B2")
        self.assertEqual(code, 0, r)
        self.assertAlmostEqual(r["gray"], 20, delta=6)
        self.assertAlmostEqual(r["wall_gray"], 180, delta=10)
        self.assertEqual((r["pixels_total"], r["rehomed"], r["position_warning"]), (1156, False, False))
        img = cv2.imread(r["image"])
        self.assertEqual(img.shape, (272, 272, 3))
        stem = Path(r["image"]).stem
        self.assertTrue((Path(r["image"]).parent / f"{stem}_crop.png").exists())
        self.assertTrue((Path(r["image"]).parents[1] / "frames" / f"{stem}.jpg").exists())
        t0 = time.time()
        code, r2, _ = self.svc.cli("get_raw_opacity", "C-B2")
        self.assertLess(time.time() - t0, 1.0)  # already over the well: one fresh frame
        self.assertGreater(r2["t"], r["t"])
        t0 = time.time()
        code, g, _ = self.svc.cli("get_image", "C-B3", "--inline-image")
        self.assertEqual(code, 0, g)
        self.assertLess(time.time() - t0, 2.5)  # neighbouring well
        self.assertEqual((g["size_px"], g["mm_per_px"]), ([272, 272], 0.0512))
        png = cv2.imdecode(np.frombuffer(base64.b64decode(g["image_base64"]), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(png.shape, (272, 272, 3))

    def test_03_argument_errors(self):
        code, e, _ = self.svc.cli("get_image", "Z9-A1")
        self.assertEqual((code, e["error"]), (1, "unknown_label"))
        code, e, _ = self.svc.cli("set_temperature", "-5")
        self.assertEqual((code, e["error"]), (1, "invalid_argument"))
        code, e, _ = self.svc.cli("set_temperature", "120")
        self.assertEqual((code, e["error"]), (1, "temperature_limit"))
        status, e = self.svc.http("get_image", {"label": "C-A1", "extra": 1})
        self.assertEqual((status, e["error"]), (400, "invalid_argument"))
        status, e = self.svc.http("set_temperature", {"temperature": "75"})
        self.assertEqual((status, e["error"]), (400, "invalid_argument"))
        status, e = self.svc.http("set_temperature", {"temperature": True})
        self.assertEqual((status, e["error"]), (400, "invalid_argument"))
        status, e = self.svc.http("nonsense", {})
        self.assertEqual((status, e["error"]), (404, "invalid_argument"))

    def test_04_load_read_clean(self):
        code, r, _ = self.svc.cli("set_temperature", 45)
        self.assertEqual((code, r["target_c"]), (0, 45.0))
        p = self.svc.cli_bg("load_well", "P3-A1")
        prompt = wait_until(lambda: self.svc.console()["prompt"], what="load prompt")
        self.assertIn("Load egg white into well P3-A1", prompt["text"])
        code, s, _ = self.svc.cli("get_status")  # answers at once while a person works
        self.assertEqual(s["last_operation"]["command"], "load_well")
        self.assertEqual(s["last_operation"]["state"], "running")
        self.svc.answer("done")
        out = json.loads(p.communicate(timeout=10)[0])
        self.assertEqual(p.returncode, 0, out)
        self.assertGreaterEqual(out["t_loaded"], out["t_requested"])
        code, s, _ = self.svc.cli("get_status")
        self.assertEqual([w["label"] for w in s["loaded_wells"]], ["P3-A1"])
        code, r, _ = self.svc.cli("get_raw_opacity", "P3-A1")
        self.assertEqual(code, 0, r)
        self.assertGreater(r["saturated_px"], 0)        # the LED reflection on the liquid ...
        self.assertLess(r["pixels_used"], 1156)          # ... is dropped
        self.assertLess(r["gray"], 60)                   # clear egg white is dark
        code, e, _ = self.svc.cli("load_well", "P3-A1")
        self.assertEqual((code, e["error"]), (1, "already_loaded"))
        p = self.svc.cli_bg("load_well", "P3-A2")
        self.svc.answer("problem", note="no egg white left")
        out = json.loads(p.communicate(timeout=10)[0])
        self.assertEqual((p.returncode, out["error"], out["note"]), (1, "operator_declined", "no egg white left"))
        self.svc.fault("set_bed", 45)
        wait_until(lambda: self.svc.cli("get_temperature")[1]["bed_c"] > 40, what="bed above 40 reported")
        code, e, _ = self.svc.cli("clean_all_wells", "--timeout", "10")
        self.assertEqual((code, e["error"]), (1, "bed_hot"))
        self.assertGreater(e["bed_c"], 40)
        self.svc.cli("set_temperature", 0)
        self.svc.fault("set_bed", 30)
        wait_until(lambda: self.svc.cli("get_temperature")[1]["bed_c"] < 40, what="bed below 40")
        p = self.svc.cli_bg("clean_all_wells")
        prompt = wait_until(lambda: self.svc.console()["prompt"], what="clean prompt")
        self.assertIn("Do not lift the plate or the grid", prompt["text"])
        self.svc.answer("done")
        out = json.loads(p.communicate(timeout=120)[0])
        self.assertEqual(p.returncode, 0, out)
        self.assertLess(out["position_check"]["max_mm"], 0.5)
        self.assertFalse(out["position_warning"])
        self.assertEqual(self.svc.cli("get_status")[1]["loaded_wells"], [])

    def test_05_calls_run_in_arrival_order(self):
        labels = ["P1-A1", "P3-B2", "C-C2"]
        procs = []
        for label in labels:
            procs.append(self.svc.cli_bg("get_image", label))
            time.sleep(0.3)
        t0 = time.time()
        code, s, _ = self.svc.cli("get_status")      # not queued: answers during the moves
        self.assertLess(time.time() - t0, 1.0)
        outs = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
        self.assertEqual([o["label"] for o in outs], labels)
        self.assertTrue(outs[0]["t"] < outs[1]["t"] < outs[2]["t"])

    def test_06_client_timeout_then_recover_from_last_operation(self):
        code, e, _ = self.svc.cli("load_well", "P4-A1", "--timeout", "1")
        self.assertEqual((code, e["error"]), (1, "client_timeout"))
        self.svc.answer("done")                        # the operation went on in the service
        def done():
            op = self.svc.cli("get_status")[1]["last_operation"]
            return op if op["command"] == "load_well" and op["state"] == "done" else None
        op = wait_until(done, what="load_well done")
        self.assertIn("t_loaded", op["result"])
        self.assertIn("P4-A1", [w["label"] for w in self.svc.cli("get_status")[1]["loaded_wells"]])

    def test_07_queued_call_dropped_when_its_caller_leaves(self):
        load = self.svc.cli_bg("load_well", "P4-A2")
        wait_until(lambda: self.svc.console()["prompt"], what="load prompt")
        code, e, _ = self.svc.cli("get_image", "C-A1", "--timeout", "1")   # queued behind the person
        self.assertEqual(e["error"], "client_timeout")
        time.sleep(1.0)
        self.svc.answer("done")
        load.communicate(timeout=10)
        time.sleep(1.5)
        calls = [json.loads(l) for l in (self.svc.session_dir() / "calls.jsonl").read_text().splitlines()]
        dropped = [c for c in calls if c["command"] == "get_image" and c.get("status") == 499]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(self.svc.cli("get_status")[1]["last_operation"]["command"], "load_well")

    def test_08_home_lost_with_wells_loaded(self):
        self.assertTrue(self.svc.cli("get_status")[1]["loaded_wells"])
        self.svc.fault("lose_home")
        wait_until(lambda: not self.svc.cli("get_status")[1]["homed"], what="home lost")
        n = self.svc.sim_state()["printer"]["commands"]
        code, e, _ = self.svc.cli("get_raw_opacity", "P4-A1")
        self.assertEqual((code, e["error"]), (1, "not_homed"))
        self.assertEqual(self.svc.sim_state()["printer"]["commands"], n)  # nothing was sent
        code, s, _ = self.svc.cli("prepare")                             # accepts the slosh risk
        self.assertEqual(code, 0, s)
        self.assertTrue(s["ready"], s)
        code, r, _ = self.svc.cli("get_raw_opacity", "P4-A1")
        self.assertEqual((code, r["rehomed"]), (0, False))
        self.assertEqual(self.svc.sim_state()["printer"]["violations"], [])

    def test_09_rehomes_on_its_own_without_wells(self):
        self.svc.fault("set_bed", 30)
        p = self.svc.cli_bg("clean_all_wells")
        self.svc.answer("done")
        self.assertEqual(json.loads(p.communicate(timeout=120)[0]).get("position_warning"), False)
        self.svc.fault("lose_home")
        wait_until(lambda: not self.svc.cli("get_status")[1]["homed"], what="home lost")
        code, r, _ = self.svc.cli("get_raw_opacity", "C-D1")
        self.assertEqual((code, r["rehomed"]), (0, True), r)
        self.assertEqual(self.svc.sim_state()["printer"]["violations"], [])


class PrepareTest(unittest.TestCase):
    def test_prepare_homes_safely_and_checks_positions(self):
        with SimService(homed=False) as svc:
            code, s, _ = svc.cli("get_status")
            self.assertEqual((s["ready"], s["homed"]), (False, False))
            session0 = s["session"]
            code, s, _ = svc.cli("prepare")
            self.assertEqual(code, 0, s)
            self.assertTrue(s["ready"] and s["homed"])
            self.assertNotEqual(s["session"], session0)          # no wells loaded: a new session
            self.assertLess(s["position_check"]["max_mm"], 0.5)
            self.assertEqual(s["last_operation"]["state"], "done")
            st = svc.sim_state()["printer"]
            self.assertEqual(st["violations"], [])
            self.assertEqual(st["pos"][2], 50.0)
            ev = [e["event"] for e in svc.events()]
            self.assertLess(ev.index("homing_started"), ev.index("homing_done"))
            self.assertIn("position_check", ev)
            d = svc.session_dir()
            self.assertTrue((d / "calibration.json").exists() and (d / "camera_ref.png").exists())
            self.assertEqual(len(list((d / "frames").glob("*_check_*.jpg"))), 6)
            # already homed, no wells: no homing, only the position check
            code, s, _ = svc.cli("prepare")
            self.assertEqual(code, 0, s)
            self.assertEqual([e["event"] for e in svc.events()].count("homing_started"), 0)


class RestartTest(unittest.TestCase):
    def test_loaded_wells_survive_a_restart_and_shutdown_switches_the_heater_off(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            svc = SimService(homed=True, auto_operator=0.5, sessions=tmp.name).start()
            svc.cli("set_temperature", 45)
            code, r, _ = svc.cli("load_well", "C-B2")
            self.assertEqual(code, 0, r)
            session = svc.cli("get_status")[1]["session"]
            svc.stop(signal.SIGINT)
            hb = json.loads((Path(tmp.name) / "heartbeat.json").read_text())
            self.assertEqual(hb["target_c"], 0.0)                 # H7: off, and confirmed
            svc2 = SimService(homed=True, sessions=tmp.name).start()
            try:
                code, s, _ = svc2.cli("get_status")
                self.assertEqual(s["session"], session)
                self.assertEqual([w["label"] for w in s["loaded_wells"]], ["C-B2"])
                self.assertEqual(s["loaded_wells"][0]["t_loaded"], r["t_loaded"])
            finally:
                svc2.stop()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
