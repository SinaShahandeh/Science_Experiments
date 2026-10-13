"""Hardware_Safety_spec.md "Test plan", the cases marked (sim) and those the simulator can stand in for,
end to end: the real service on the simulated printer and camera, driven through the real CLI."""
import json
import subprocess
import time
import unittest

from tests.helpers import PY, REPO, SimService, wait_until


def sim_target(svc):
    return svc.sim_state()["printer"]["target"]


class TemperatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = SimService().start()

    @classmethod
    def tearDownClass(cls):
        cls.svc.stop()

    def test_01_target_too_high(self):
        self.svc.cli("set_temperature", 30)
        code, e, _ = self.svc.cli("set_temperature", 120)
        self.assertEqual((code, e["error"]), (1, "temperature_limit"))
        time.sleep(0.5)
        self.assertEqual(sim_target(self.svc), 30.0)                  # unchanged

    def test_02_bad_target(self):
        code, e, _ = self.svc.cli("set_temperature", -5)
        self.assertEqual((code, e["error"]), (1, "invalid_argument"))
        code, e, _ = self.svc.cli("set_temperature", "abc")
        self.assertEqual((code, e["error"]), (2, "invalid_argument"))

    def test_12_hands_on_a_hot_bed(self):
        self.svc.fault("set_bed", 70)
        wait_until(lambda: self.svc.cli("get_temperature")[1]["bed_c"] > 60, what="bed above 60")
        code, e, _ = self.svc.cli("load_well", "P3-A1", "--timeout", "10")
        self.assertEqual((code, e["error"]), (1, "bed_hot"))
        self.svc.fault("set_bed", 55)
        wait_until(lambda: 40 < self.svc.cli("get_temperature")[1]["bed_c"] < 60, what="bed at 55")
        code, e, _ = self.svc.cli("clean_all_wells", "--timeout", "10")
        self.assertEqual((code, e["error"]), (1, "bed_hot"))


class WatchdogTest(unittest.TestCase):
    def test_03_agent_goes_quiet(self):
        with SimService(overrides={"heat.heater_watchdog_s": 6}) as svc:
            svc.cli("set_temperature", 50)
            # only the passive status read, the console and the simulator are polled: none is an API call
            wait_until(lambda: svc.status()["safety"] == "heater_timeout", timeout=20, what="heater_timeout")
            self.assertEqual(svc.console()["safety"]["state"], "heater_timeout")
            wait_until(lambda: sim_target(svc) == 0.0, what="heater off")
            code, s, _ = svc.cli("get_status")
            self.assertEqual((s["safety"], s["ready"]), ("heater_timeout", False))
            self.assertEqual(set(svc.status()), set(s))                  # the same body as get_status
            self.assertIn("heater_timeout", [e["event"] for e in svc.events()])
            code, r, _ = svc.cli("set_temperature", 50)                # resumes heating
            self.assertEqual(code, 0)
            self.assertEqual(svc.cli("get_status")[1]["safety"], "ok")
            wait_until(lambda: sim_target(svc) == 50.0, what="heating again")


class HeatFaultTests(unittest.TestCase):
    def test_05_over_temperature(self):
        with SimService() as svc:
            svc.cli("set_temperature", 50)
            svc.fault("bed_override", 106)
            wait_until(lambda: svc.cli("get_status")[1]["safety"] == "halted", what="halt")
            s = svc.cli("get_status")[1]
            self.assertEqual(s["safety_reason"], "over_temperature")
            wait_until(lambda: sim_target(svc) == 0.0 and svc.console()["target_confirmed"], what="heater off confirmed")

    def test_h2_step_down_is_not_overshoot(self):
        with SimService(overrides={"heat.overshoot_time_s": 4}) as svc:
            svc.fault("set_bed", 69)
            svc.cli("set_temperature", 69)
            wait_until(lambda: svc.cli("get_temperature")[1]["bed_c"] > 68.4, what="bed at 69")
            time.sleep(2)
            svc.cli("set_temperature", 50)                               # back to 50 °C for loading
            t0 = time.time()
            while time.time() - t0 < 12:                                 # the bed cools ~0.25 °C/s
                s = svc.cli("get_status")[1]
                self.assertEqual(s["safety"], "ok", s)
                time.sleep(1)
            self.assertGreater(s["bed_c"], 55)                           # still well above the new target

    def test_06_heater_stuck_on(self):
        with SimService(overrides={"heat.stuck_grace_s": 5}) as svc:
            svc.cli("set_temperature", 0)
            svc.fault("heater_stuck", True)                              # the bed rises with the heater off
            wait_until(lambda: svc.cli("get_status")[1]["safety"] == "halted", timeout=40, what="halt")
            self.assertEqual(svc.cli("get_status")[1]["safety_reason"], "heater_stuck_on")

    def test_07_printer_link_lost_while_heating(self):
        with SimService(overrides={"heat.lost_reports_s": 6}) as svc:
            svc.cli("set_temperature", 50)
            wait_until(lambda: sim_target(svc) == 50.0, what="heating")
            svc.fault("link_down", True)
            wait_until(lambda: svc.console()["safety"]["state"] == "halted", timeout=20, what="halt")
            self.assertEqual(svc.console()["safety"]["reason"], "lost_contact")
            self.assertEqual(sim_target(svc), 50.0)                      # can't reach it yet ...
            svc.fault("link_down", None)
            wait_until(lambda: sim_target(svc) == 0.0, timeout=20, what="heater off once the link is back")
            wait_until(lambda: svc.console()["target_confirmed"], what="confirmed")


class MotionFaultTests(unittest.TestCase):
    def test_08_lost_command(self):
        with SimService() as svc:
            self.assertEqual(svc.cli("get_image", "C-A1")[0], 0)
            svc.fault("swallow_next_gcode", True)
            code, e, _ = svc.cli("get_image", "C-A2")
            self.assertEqual((code, e["error"]), (1, "move_failed"))
            n = svc.sim_state()["printer"]["commands"]
            code, e, _ = svc.cli("get_image", "C-A3")                   # refused until prepare()
            self.assertEqual((code, e["error"]), (1, "move_failed"))
            self.assertEqual(svc.sim_state()["printer"]["commands"], n)
            self.assertFalse(svc.cli("get_status")[1]["ready"])
            code, s, _ = svc.cli("prepare")
            self.assertEqual(code, 0, s)
            self.assertEqual(svc.cli("get_image", "C-A3")[0], 0)
            self.assertEqual(svc.sim_state()["printer"]["violations"], [])

    def test_08b_acknowledged_but_nothing_moved(self):
        with SimService() as svc:
            self.assertEqual(svc.cli("get_image", "C-A1")[0], 0)
            svc.fault("ack_no_motion", True)
            code, e, _ = svc.cli("get_image", "C-A2")
            self.assertEqual((code, e["error"]), (1, "move_failed"))

    def test_m3_z_first_after_a_reconnect(self):
        with SimService() as svc:
            self.assertEqual(svc.cli("get_image", "C-A1")[0], 0)
            svc.fault("link_down", True)
            wait_until(lambda: not svc.cli("get_status")[1]["printer_ok"], timeout=25, what="link down noticed")
            code, e, _ = svc.cli("get_image", "C-A2")                   # N2: nothing moves while it's down
            self.assertEqual(e["error"], "printer_unavailable")
            svc.fault("link_down", None)
            wait_until(lambda: svc.cli("get_status")[1]["printer_ok"], timeout=25, what="link back")
            self.assertEqual(svc.cli("get_image", "C-A2")[0], 0)
            ev = [(e["event"], json.loads(e["detail"])) for e in svc.events()]
            i = [k for k, (name, d) in enumerate(ev) if name == "capture" and d["label"] == "C-A1"][-1]
            moves = [d for name, d in ev[i:] if name == "move"]
            self.assertIn("z", moves[0])                                # G1 Z50 on its own first
            self.assertIn("x", moves[1])

    def test_09_wrong_speed_mode(self):
        with SimService() as svc:
            svc.fault("speed_mode", "sport")
            wait_until(lambda: not svc.cli("get_status")[1]["printer_ok"], what="printer_ok false")
            n = svc.sim_state()["printer"]["commands"]
            code, e, _ = svc.cli("get_image", "C-A1")
            self.assertEqual((code, e["error"]), (1, "printer_unavailable"))
            self.assertIn("speed mode", e["message"])
            self.assertEqual(svc.sim_state()["printer"]["commands"], n)

    def test_13_low_nozzle_before_homing(self):
        with SimService(homed=False) as svc:
            svc.fault("set_z", 10.0)                                     # an interrupted Z homing
            code, s, _ = svc.cli("prepare")
            self.assertEqual(code, 0, s)
            st = svc.sim_state()["printer"]
            self.assertEqual(st["violations"], [])                       # no XY sweep below Z20
            ev = [e["event"] for e in svc.events()]
            self.assertLess(ev.index("lift"), ev.index("homing_step"))  # lifted before G28 X
            check = [json.loads(e["detail"]) for e in svc.events() if e["event"] == "homing_height_check"][0]
            self.assertTrue(check["z_image"] is None or check["z_image"] < 30, check)


class ForeignControlTests(unittest.TestCase):
    def test_10_foreign_target(self):
        with SimService() as svc:
            svc.cli("set_temperature", 40)
            wait_until(lambda: svc.console()["target_confirmed"], what="target confirmed")
            svc.fault("foreign_target", 60)                              # e.g. set on the printer's screen
            wait_until(lambda: svc.cli("get_status")[1]["safety"] == "halted", what="halt")
            self.assertEqual(svc.cli("get_status")[1]["safety_reason"], "foreign_control")
            wait_until(lambda: sim_target(svc) == 0.0, what="heater off")

    def test_10b_print_job_and_foreign_homing(self):
        with SimService() as svc:
            svc.fault("start_print")
            wait_until(lambda: svc.cli("get_status")[1]["safety"] == "halted", what="halt")
        with SimService(homed=False) as svc:
            svc.fault("foreign_home")
            wait_until(lambda: svc.cli("get_status")[1]["safety"] == "halted", what="halt")
            self.assertEqual(svc.cli("get_status")[1]["safety_reason"], "foreign_control")


class StopTests(unittest.TestCase):
    def test_11_stop_during_a_move_and_a_prompt(self):
        with SimService() as svc:
            self.assertEqual(svc.cli("get_image", "P3-B2")[0], 0)
            svc.cli("set_temperature", 45)
            move = svc.cli_bg("get_image", "P1-A1")                      # ~5 s across the grid
            time.sleep(0.4)
            queued = svc.cli_bg("get_image", "C-B2")
            time.sleep(1.0)
            code, r, _ = svc.cli("stop")
            self.assertEqual((code, r["safety"]), (0, "halted"))
            out_move = json.loads(move.communicate(timeout=30)[0])
            out_queued = json.loads(queued.communicate(timeout=30)[0])
            self.assertEqual(out_move["error"], "halted")               # the move finished, nothing new started
            self.assertEqual(out_queued["error"], "halted")
            pos = svc.sim_state()["printer"]["pos"]
            self.assertAlmostEqual(pos[0], 69.83, delta=0.01)            # the move under way finished
            wait_until(lambda: sim_target(svc) == 0.0, what="heater off")
            code, e, _ = svc.cli("set_temperature", 40)
            self.assertEqual(e["error"], "halted")
            p = subprocess.run([PY, str(REPO / "bin" / "mhapi"), "clear"], capture_output=True, text=True,
                               env={"MHAPI_URL": svc.url, "PATH": "/usr/bin"})
            self.assertEqual(p.returncode, 2)                            # no clear command in the CLI
            status, body = svc.http("clear")
            self.assertEqual(status, 404)
            self.assertEqual(svc.clear()[0], 200)                        # only the console clears
            code, e, _ = svc.cli("get_image", "C-B2")                   # position unknown: prepare() first
            self.assertEqual(e["error"], "not_homed")
            self.assertEqual(svc.cli("prepare")[0], 0)
            # stop during a load prompt
            load = svc.cli_bg("load_well", "C-A1")
            wait_until(lambda: svc.console()["prompt"], what="prompt")
            svc.cli("stop")
            out = json.loads(load.communicate(timeout=10)[0])
            self.assertEqual(out["error"], "halted")
            self.assertIsNone(svc.console()["prompt"])
            self.assertEqual(svc.sim_state()["printer"]["violations"], [])


class CameraTests(unittest.TestCase):
    def test_14_loose_camera(self):
        with SimService() as svc:
            self.assertEqual(svc.cli("get_image", "C-B2")[0], 0)
            svc.fault("camera_shift", [6, 0])                            # 1.2 mm
            code, e, _ = svc.cli("get_image", "C-B3")
            self.assertEqual((code, e["error"]), (1, "camera_moved"))
            n = svc.sim_state()["printer"]["commands"]
            code, e, _ = svc.cli("prepare")                              # motion stays stopped
            self.assertEqual(e["error"], "camera_moved")
            self.assertEqual(svc.sim_state()["printer"]["commands"], n)
            s = svc.cli("get_status")[1]
            self.assertTrue(s["position_warning"])
            self.assertFalse(s["ready"])
            svc.fault("camera_shift", [0, 0])                            # a person fixed the mount
            svc.clear()
            self.assertEqual(svc.cli("prepare")[0], 0)
            self.assertEqual(svc.cli("get_image", "C-B3")[0], 0)

    def test_14b_jump_and_warning(self):
        with SimService() as svc:
            self.assertEqual(svc.cli("get_image", "C-B2")[0], 0)
            svc.fault("camera_shift", [1.5, 0])                          # 0.31 mm: fine
            code, r, _ = svc.cli("get_image", "C-B3")
            self.assertEqual((code, r["position_warning"]), (0, False))
            svc.fault("camera_shift", [3.0, 0])                          # 0.62 mm total: warning, still valid
            code, r, _ = svc.cli("get_image", "C-B4")
            self.assertEqual((code, r["position_warning"]), (0, True))
            self.assertAlmostEqual(r["camera_tilt_mm"][0], 0.62, delta=0.05)
            svc.fault("camera_shift", [0.6, 0])                          # back 0.5 mm since the last capture: a jump
            code, e, _ = svc.cli("get_image", "C-B3")
            self.assertEqual(e["error"], "camera_moved")
            self.assertIn("jumped", e["message"])

    def test_position_check_warns_when_an_island_moved(self):
        with SimService() as svc:
            svc.fault("island_offset", {"island": "P3", "dx": 1.0, "dy": 0.0})
            code, s, _ = svc.cli("prepare")
            self.assertEqual(code, 0, s)                                 # returns normally ...
            self.assertTrue(s["position_warning"])                       # ... with ready false
            self.assertFalse(s["ready"])
            self.assertGreater(s["position_check"]["max_mm"], 0.5)


class StartupTests(unittest.TestCase):
    def test_h7_starts_with_the_heater_on(self):
        with SimService(target=50.0, overrides={"heat.heater_watchdog_s": 8}) as svc:
            s = svc.cli("get_status")[1]
            self.assertEqual((s["target_c"], s["safety"]), (50.0, "ok"))     # left as found
            self.assertIn("adopted_target", [e["event"] for e in svc.events()])
            wait_until(lambda: svc.console()["safety"]["state"] == "heater_timeout", timeout=20,
                       what="the H4 clock started at start-up")

    def test_n4_refuses_other_firmware(self):
        svc = SimService(overrides={"printer.firmware": "01.09.00.00"})
        with self.assertRaises(RuntimeError) as e:
            svc.start()
        self.assertIn("refusing to run on printer firmware", str(e.exception))
        svc.stop()


if __name__ == "__main__":
    unittest.main()
