"""The safety layer's rules against a fake printer (Hardware_Safety_spec.md H1-H7, M6, P3)."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from mhapi.service.config import Params
from mhapi.service.errors import ApiError
from mhapi.service.safety import Safety


class FakePrinter:
    def __init__(self):
        self.listeners, self.sent = [], []
        self.state = {"bed_temper": 25.0, "bed_target_temper": 0.0}
        self.t_report = time.time()
        self.up = True

    @property
    def bed_c(self):
        return self.state.get("bed_temper")

    @property
    def target_c(self):
        return self.state.get("bed_target_temper")

    @property
    def report_age(self):
        return time.time() - self.t_report

    def send(self, lines, homing=False):
        if not self.up:
            raise ApiError("printer_unavailable", "link down")
        self.sent.append(list(lines))
        return "1", time.time()

    def report(self, **fields):
        t = time.time()
        self.t_report = t
        self.state.update(fields)
        for fn in self.listeners:
            fn(t, fields, dict(self.state))


class Announcer:
    def __init__(self):
        self.said = []

    def say(self, text, chimes=3):
        self.said.append(text)


class SafetyTest(unittest.TestCase):
    def make(self, **over):
        o = {"heat.overshoot_time_s": 0.5, "heat.heater_watchdog_s": 1.0, "heat.lost_reports_s": 1.0,
             "heat.target_resend_s": 0.2, "heat.target_alert_s": 0.6}
        o.update(over)
        self.tmp = tempfile.TemporaryDirectory()
        self.printer = FakePrinter()
        self.ann = Announcer()
        self.events = []
        self.prompt = None
        self.loaded = 0
        self.s = Safety(Params.load(overrides=o), self.printer, self.ann, lambda e, **d: self.events.append((e, d)),
                        Path(self.tmp.name) / "heartbeat.json", loaded_count=lambda: self.loaded,
                        prompt_limit=lambda: self.prompt, sim=True)
        self.printer.report(bed_temper=25.0, bed_target_temper=0.0, home_flag=0)  # first report: adopt
        return self.s

    def tearDown(self):
        self.tmp.cleanup()

    def last_m140(self):
        return [l for lines in self.printer.sent for l in lines if l.startswith("M140")][-1]

    def test_h1_target_validation(self):
        s = self.make()
        for bad, code in ((True, "invalid_argument"), ("75", "invalid_argument"), (float("nan"), "invalid_argument"),
                          (-1, "invalid_argument"), (100.5, "temperature_limit"), (120, "temperature_limit")):
            with self.assertRaises(ApiError) as e:
                s.set_target(bad)
            self.assertEqual(e.exception.code, code, bad)
        self.assertEqual(s.set_target(100), 100.0)
        self.assertEqual(self.last_m140(), "M140 S100")
        s.set_target(62.5)
        self.assertEqual(self.last_m140(), "M140 S62.5")

    def test_h7_adopts_startup_target(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = FakePrinter()
        p.report(bed_temper=49.9, bed_target_temper=50, home_flag=0)   # found heating at start-up
        s = Safety(Params.load(), p, Announcer(), lambda e, **d: None, Path(self.tmp.name) / "hb.json",
                   loaded_count=lambda: 0, prompt_limit=lambda: None, sim=True)
        self.assertEqual((s.expected, s.state, s.confirmed), (50.0, "ok", True))
        self.assertEqual(p.sent, [])  # left as it is
        p.report(home_flag=0b111)                                    # a change after start-up is judged
        self.assertEqual((s.state, s.reason), ("halted", "foreign_control"))

    def test_m6_foreign_target(self):
        s = self.make()
        s.set_target(50)
        self.printer.report(bed_target_temper=50)
        self.printer.report(bed_target_temper=65)
        self.assertEqual((s.state, s.reason), ("halted", "foreign_control"))
        self.assertEqual(self.last_m140(), "M140 S0")

    def test_in_flight_target_is_not_foreign(self):
        s = self.make()
        s.set_target(50)
        self.printer.report(bed_target_temper=50)
        s.set_target(75)
        self.printer.report(bed_target_temper=50)  # the old target, still in flight
        self.assertEqual(s.state, "ok")
        self.assertFalse(s.confirmed)
        self.printer.report(bed_target_temper=75)
        self.assertTrue(s.confirmed)

    def test_stale_report_of_the_replaced_target_is_not_foreign(self):
        # 2026-10-13 on the rig: target 0 adopted at start-up, set_temperature(50), and the next report still
        # carried 0 (M140 waits behind queued moves); that halted as foreign_control
        s = self.make()
        s.set_target(50)
        self.printer.report(bed_target_temper=0.0)
        self.assertEqual(s.state, "ok")
        self.assertFalse(s.confirmed)
        self.printer.report(bed_target_temper=50)
        self.assertTrue(s.confirmed)
        self.printer.report(bed_target_temper=0.0)   # once confirmed, the old value is someone else's
        self.assertEqual((s.state, s.reason), ("halted", "foreign_control"))

    def test_halt_error_says_what_happened(self):
        s = self.make()
        s.set_target(50)
        self.printer.report(bed_target_temper=50)
        self.printer.report(bed_target_temper=65)
        with self.assertRaises(ApiError) as e:
            s.set_target(60)
        body = e.exception.body()
        self.assertEqual((body["error"], body["safety_reason"]), ("halted", "foreign_control"))
        self.assertIn("65 °C", body["safety_detail"])
        self.assertIn("65 °C", body["message"])
        self.assertIn("Clear", body["message"])
        self.assertEqual(s.as_record()["detail"], body["safety_detail"])

    def test_h3_resend_and_alert(self):
        s = self.make()
        s.set_target(0)
        n0 = len(self.printer.sent)
        time.sleep(0.3)
        s.tick()
        self.assertGreater(len(self.printer.sent), n0)  # resent
        time.sleep(0.5)
        s.tick()
        self.assertTrue(any("not confirmed" in t for t in self.ann.said))
        self.printer.report(bed_target_temper=0)
        self.assertTrue(s.confirmed)

    def test_h2_hard_limit(self):
        s = self.make()
        self.printer.report(bed_temper=105.5)
        s.tick()
        self.assertEqual((s.state, s.reason), ("halted", "over_temperature"))
        self.assertEqual(self.last_m140(), "M140 S0")

    def test_h2_overshoot_after_reaching(self):
        s = self.make()
        s.set_target(50)
        self.printer.report(bed_target_temper=50, bed_temper=45)
        s.tick()
        self.printer.report(bed_temper=56)  # not reached yet... 56 >= 49.5 counts as reached
        s.tick()
        self.assertEqual(s.state, "ok")
        time.sleep(0.6)
        self.printer.report(bed_temper=56)
        s.tick()
        self.assertEqual((s.state, s.reason), ("halted", "over_temperature"))

    def test_h2_cooling_to_lower_target_is_not_overshoot(self):
        s = self.make()
        s.set_target(75)
        self.printer.report(bed_target_temper=75, bed_temper=75)
        s.set_target(50)
        self.printer.report(bed_target_temper=50, bed_temper=70)
        for _ in range(3):
            time.sleep(0.3)
            s.tick()
        self.assertEqual(s.state, "ok")

    def test_h2_step_down_69_to_50(self):
        """Between runs the bed goes from 69 °C back to 50 °C for loading: cooling is not overshoot."""
        s = self.make()
        s.set_target(69)
        self.printer.report(bed_target_temper=69, bed_temper=69)
        s.tick()
        s.set_target(50)
        self.printer.report(bed_target_temper=50, bed_temper=66)
        for bed in (65, 63, 60, 57, 55.5):
            time.sleep(0.15)
            self.printer.report(bed_temper=bed)
            s.tick()
        self.assertEqual(s.state, "ok")
        self.printer.report(bed_temper=50.2)                 # reached on the way down
        s.tick()
        self.printer.report(bed_temper=56)                   # then drifting 6 °C above for too long
        s.tick()
        time.sleep(0.6)
        s.tick()
        self.assertEqual((s.state, s.reason), ("halted", "over_temperature"))

    def test_h2_target_adopted_while_cooling(self):
        """Started while the bed cools toward a lower target (H7 adopts it): not overshoot either."""
        self.tmp = tempfile.TemporaryDirectory()
        p = FakePrinter()
        p.report(bed_temper=65.0, bed_target_temper=50, home_flag=0)
        s = Safety(Params.load(overrides={"heat.overshoot_time_s": 0.5}), p, Announcer(), lambda e, **d: None,
                   Path(self.tmp.name) / "hb.json", loaded_count=lambda: 0, prompt_limit=lambda: None, sim=True)
        for bed in (64.5, 63.0, 61.0):
            time.sleep(0.3)
            p.report(bed_temper=bed)
            s.on_api_call()
            s.tick()
        self.assertEqual(s.state, "ok")

    def test_h2_switched_off_mid_ramp_is_not_stuck(self):
        """Off during a jump, the reported reading keeps climbing ~6 °C for ~10 s (report delay, plate
        inertia): inside the grace period that is not a stuck heater; a rise after it is."""
        s = self.make(**{"heat.stuck_grace_s": 0.6})
        s.set_target(75)
        self.printer.report(bed_target_temper=75, bed_temper=60)
        s.set_target(0)
        self.printer.report(bed_target_temper=0)
        for bed in (62.0, 64.0, 66.0):
            self.printer.report(bed_temper=bed)
            s.tick()
            time.sleep(0.15)
        self.assertEqual(s.state, "ok")
        time.sleep(0.5)
        for bed in (66.0, 68.0, 70.0):
            self.printer.report(bed_temper=bed)
            s.tick()
        self.assertEqual((s.state, s.reason), ("halted", "heater_stuck_on"))

    def test_h2_heater_stuck_on(self):
        s = self.make(**{"heat.stuck_grace_s": 0})
        s.set_target(0)
        self.printer.report(bed_target_temper=0)
        for b in (30.0, 31.0, 32.0, 33.5):
            self.printer.report(bed_temper=b)
            s.tick()
        self.assertEqual((s.state, s.reason), ("halted", "heater_stuck_on"))

    def test_h4_watchdog_and_resume(self):
        s = self.make()
        s.set_target(50)
        self.printer.report(bed_target_temper=50, bed_temper=50)
        s.on_api_call()
        s.tick()
        self.assertEqual(s.state, "ok")
        time.sleep(1.1)
        self.printer.report(bed_temper=50)
        s.tick()
        self.assertEqual(s.state, "heater_timeout")
        self.assertEqual(self.last_m140(), "M140 S0")
        s.set_target(50)
        self.assertEqual(s.state, "ok")
        self.assertEqual(self.last_m140(), "M140 S50")

    def test_h6_lost_contact_while_heating(self):
        s = self.make()
        s.set_target(50)
        self.printer.report(bed_target_temper=50)
        self.printer.up = False
        time.sleep(1.1)
        s.on_api_call()
        s.tick()
        self.assertEqual((s.state, s.reason), ("halted", "lost_contact"))

    def test_no_halt_on_lost_contact_when_cold(self):
        s = self.make()
        time.sleep(1.1)
        s.on_api_call()
        s.tick()
        self.assertEqual(s.state, "ok")

    def test_m6_foreign_homing_and_print_job(self):
        s = self.make()
        s.homing = True
        self.printer.report(home_flag=0b111)
        self.assertEqual(s.state, "ok")
        s.homing, s.t_homing_end = False, 0.0
        self.printer.report(home_flag=0)                # home lost when idle: not foreign
        self.assertEqual(s.state, "ok")
        self.printer.report(home_flag=0b011)            # somebody homed X/Y
        self.assertEqual((s.state, s.reason), ("halted", "foreign_control"))
        s2 = self.make()
        self.printer.report(gcode_state="RUNNING")
        self.assertEqual((s2.state, s2.reason), ("halted", "foreign_control"))

    def test_p3_no_heating_while_a_person_works(self):
        s = self.make()
        self.prompt = ("loading a well", 60.0)
        with self.assertRaises(ApiError) as e:
            s.set_target(75)
        self.assertEqual(e.exception.code, "bed_hot")
        s.set_target(55)

    def test_halt_blocks_heating_until_clear(self):
        s = self.make()
        cleared = []
        s.on_clear.append(lambda: cleared.append(True))
        s.halt("operator_stop", "test")
        with self.assertRaises(ApiError) as e:
            s.set_target(40)
        self.assertEqual(e.exception.code, "halted")
        with self.assertRaises(ApiError):
            s.check_motion()
        s.clear("test")
        self.assertEqual((s.state, cleared), ("ok", [True]))
        self.assertEqual(s.expected, 0.0)  # clearing does not restore the target

    def test_heartbeat(self):
        s = self.make()
        s.set_target(50)
        s.write_heartbeat()
        hb = json.loads(s.heartbeat_path.read_text())
        self.assertEqual(hb["target_c"], 50.0)
        s.set_target(0)
        self.printer.report(bed_target_temper=0)
        s.write_heartbeat()
        self.assertEqual(json.loads(s.heartbeat_path.read_text())["target_c"], 0.0)


if __name__ == "__main__":
    unittest.main()
