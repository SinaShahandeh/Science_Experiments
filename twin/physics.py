"""Clock, motion and heat for the twin.

- SimClock: simulated Unix time. At speed 1 it is the wall clock; faster speeds compress waits, moves,
  heating and the chemistry together (but not the agent's thinking time).
- Segment / Gantry: trapezoidal moves at the service's speeds (F1800 XY, F300 Z, 500 mm/s^2).
- Bed: the heated bed (heater + aluminium, where the thermistor sits) and the build plate on it (where the
  wells are, lagging a little). A PID on the thermistor reading, which reaches the control computer ~6 s
  late (MQTT reports trail the printer). Fitted to the 2026-10-11 trial: 50 -> 75 C reads 75 C after ~44 s and
  peaks at 76.3 C after ~60 s (fit RMS 1.0 C over 10 min); jumps overshoot by 1-2 C; 75 -> 40 C takes ~13 min.
"""
import math
import threading
import time
from dataclasses import dataclass


class SimClock:
    def __init__(self, speed=1.0):
        self._lock = threading.Lock()
        self._wall0 = time.time()
        self._sim0 = self._wall0
        self.speed = float(speed)

    def now(self):
        with self._lock:
            return self._sim0 + (time.time() - self._wall0) * self.speed

    def set_speed(self, speed):
        speed = float(min(60.0, max(0.1, speed)))
        with self._lock:
            sim_now = self._sim0 + (time.time() - self._wall0) * self.speed
            self._sim0, self._wall0, self.speed = sim_now, time.time(), speed

    def sleep(self, sim_s, stop=None):
        """Sleep `sim_s` simulated seconds. Returns False if `stop` (an Event) was set first."""
        end = self.now() + max(0.0, sim_s)
        while True:
            left = end - self.now()
            if left <= 0:
                return True
            wall = min(left / self.speed, 0.05)
            if stop is not None:
                if stop.wait(wall):
                    return False
            else:
                time.sleep(wall)

    def sleep_until(self, t_sim, stop=None):
        return self.sleep(t_sim - self.now(), stop)


def trapezoid(d, v, a):
    """Duration and accel time of a move of length d at max speed v and acceleration a."""
    if d <= 0:
        return 0.0, 0.0, 0.0
    if d >= v * v / a:
        ta = v / a
        return d / v + ta, ta, v
    vp = math.sqrt(a * d)
    return 2 * vp / a, vp / a, vp


@dataclass
class Segment:
    t0: float
    p0: tuple
    p1: tuple
    v: float
    a: float
    kind: str = "move"  # move | z | home

    def __post_init__(self):
        self.d = math.dist(self.p0, self.p1)
        self.T, self.ta, self.vp = trapezoid(self.d, self.v, self.a)

    @property
    def t1(self):
        return self.t0 + self.T

    def s_at(self, t):
        tau = min(max(t - self.t0, 0.0), self.T)
        a, ta, vp, T = self.a, self.ta, self.vp, self.T
        if tau < ta:
            return 0.5 * a * tau * tau
        if tau > T - ta:
            return self.d - 0.5 * a * (T - tau) ** 2
        return 0.5 * a * ta * ta + vp * (tau - ta)

    def pos(self, t):
        if self.d == 0:
            return self.p1
        f = self.s_at(t) / self.d
        return tuple(p0 + (p1 - p0) * f for p0, p1 in zip(self.p0, self.p1))

    def to_json(self):
        return {"t0": self.t0, "p0": self.p0, "p1": self.p1, "v": self.v, "a": self.a, "T": self.T,
                "ta": self.ta, "vp": self.vp, "d": self.d, "kind": self.kind}


class Gantry:
    """Where the nozzle is. Moves block the calling (executor) thread for their simulated duration."""

    def __init__(self, clock, start=(128.0, 128.0, 62.0)):
        self.clock = clock
        self.p = tuple(start)
        self.seg = None
        self.last_move_end = clock.now() - 600.0
        self._lock = threading.Lock()

    def position(self, t=None):
        t = self.clock.now() if t is None else t
        with self._lock:
            if self.seg is not None and t < self.seg.t1:
                return self.seg.pos(t)
            return self.seg.p1 if self.seg is not None else self.p

    def moving(self, t=None):
        t = self.clock.now() if t is None else t
        with self._lock:
            return self.seg is not None and self.seg.t0 <= t < self.seg.t1

    def segment(self):
        with self._lock:
            return self.seg

    def run(self, target, v, a, kind="move", latency=0.12, stop=None):
        """Move in a straight line to `target` (x, y, z); returns the simulated duration."""
        start = self.position()
        seg = Segment(self.clock.now() + latency, start, tuple(float(c) for c in target), v, a, kind)
        with self._lock:
            self.seg = seg
        self.clock.sleep_until(seg.t1, stop)  # a started move always finishes (no M112 on this rig)
        if seg.t1 > self.clock.now():
            self.clock.sleep_until(seg.t1)
        with self._lock:
            self.p, self.seg = seg.p1, None
        self.last_move_end = self.clock.now()
        return seg.T + latency


class Bed:
    """Heated bed H (the thermistor is in it), build plate P on top (the wells sit on P), PID on the reading.

    The reading is the thermistor (lag TAU_S) reported TD seconds late. Fitted to the trial's 50 -> 75 C step
    (RMS 1.0 C), with heat losses constrained so 75 -> 40 C with the heater off takes ~13 min (the trial has no
    cooling data). Switching the heater off mid-ramp lets the reading climb only ~2 C more.""" 
    A, K_HP, K_PH, B_H, B_P = 0.979585, 0.052398, 0.475442, 0.001148, 0.004     # plant
    KP, KI, KD = 0.665795, 0.026777, 4.978182                                    # controller
    TAU_S, TD = 2.966237, 5.987845                                               # thermistor lag, report delay (s)
    STEP = 0.05  # the controller runs at a fixed period, like firmware (the fit used 0.05 s)

    def __init__(self, t_amb=23.0):
        self.t_amb = t_amb
        self.th = self.tp = self.ts = t_amb   # heated bed, plate, thermistor
        self.target = 0.0
        self.u = 0.0
        self.i = 0.0
        self.prev_e = None
        self.stuck_on = False  # fault injection: heater stuck at full power
        self._acc = 0.0
        self._t = 0.0
        self._hist = [(0.0, t_amb)]

    def set_target(self, target):
        target = float(target)
        if target <= 0:
            self.i, self.prev_e = 0.0, None
        self.target = max(0.0, target)

    def step(self, dt):
        self._acc += dt
        while self._acc >= self.STEP:
            self._acc -= self.STEP
            self._step(self.STEP)

    def reported(self):
        """The thermistor reading as the control computer receives it: TD seconds old."""
        t = self._t - self.TD
        h = self._hist
        if t <= h[0][0]:
            return h[0][1]
        for (t0, v0), (t1, v1) in zip(reversed(h[:-1]), reversed(h[1:])):
            if t0 <= t <= t1:
                return v0 + (v1 - v0) * (t - t0) / max(t1 - t0, 1e-9)
        return h[-1][1]

    def _step(self, dt):
        if self.stuck_on:
            self.u = 1.0
        elif self.target <= 0:
            self.u = 0.0
        else:  # the firmware controls on its own (undelayed) reading
            e = self.target - self.ts
            d = 0.0 if self.prev_e is None else (e - self.prev_e) / dt
            self.prev_e = e
            raw = self.KP * e + self.KI * self.i + self.KD * d
            self.u = min(1.0, max(0.0, raw))
            if 0.0 < raw < 1.0 or (raw >= 1.0 and e < 0) or (raw <= 0.0 and e > 0):
                self.i += e * dt
        th, tp, ta = self.th, self.tp, self.t_amb
        self.th += (self.A * self.u - self.K_HP * (th - tp) - self.B_H * (th - ta)) * dt
        self.tp += (self.K_PH * (th - tp) - self.B_P * (tp - ta)) * dt
        self.ts += (self.th - self.ts) / self.TAU_S * dt
        self._t += dt
        self._hist.append((self._t, self.ts))
        if len(self._hist) > 400:   # keep ~20 s
            del self._hist[:200]
