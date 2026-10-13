"""The real rig's two cameras for the dashboard, re-served by the harness in instrument mode (GET /feeds/...).

  a1  the Bambu A1's own camera: TLS on port 6000 (bambu/snapshot.py), one 1536 x 1080 JPEG about every 2 s.
      The access code comes from Bambu Studio's config. The printer's serial number and last known address are
      the service's (mhapi/safety.toml); when that address doesn't answer, the printer is found by serial number.
  pi  the Pi camera on the toolhead: MJPEG from http://raspberrypi.local:8080/stream.mjpg, 1280 x 720 at about
      20 frames a second (RASPBERRY_PI_CAMERA.md). By name, so a new address on the Pi doesn't matter.

Each feed holds one upstream connection while anyone watches and drops it when nobody does. Both are read-only:
the camera port takes no commands, and the harness never talks to the printer over MQTT.
"""
import json
import socket
import ssl
import struct
import threading
import time
import tomllib
import urllib.request
from collections import deque
from contextlib import contextmanager

from .env import ROOT

PI_URL = "http://raspberrypi.local:8080/stream.mjpg"
PI_FRAME = (1280, 720)   # the frame the calibration's pixels refer to
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Feed:
    """One camera: the latest frame, read by one upstream connection while anyone watches."""

    def __init__(self, name, source):
        self.name = name
        self.source = source   # source(problem) yields JPEG bytes, or None now and then while it can't
        self.cv = threading.Condition()
        self.watchers = 0
        self.thread = None
        self.seq = 0           # counts frames over the feed's life, so "newer than" survives reconnections
        self.jpg = self.t = None
        self.error = None      # the last problem, until the next frame arrives
        self.arrivals = deque(maxlen=20)

    @contextmanager
    def watch(self):
        with self.cv:
            self.watchers += 1
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name=f"feed-{self.name}", daemon=True)
                self.thread.start()
        try:
            yield self
        finally:
            with self.cv:
                self.watchers -= 1

    def _problem(self, message):
        with self.cv:
            self.error = message

    def _stopped(self):
        """Under the lock: the upstream is going away, and with it the frames from it (never hand out an old one)."""
        self.thread = None
        self.jpg = self.t = None
        self.arrivals.clear()

    def _run(self):
        frames = self.source(self._problem)
        try:
            for jpg in frames:
                with self.cv:
                    if self.watchers == 0:
                        self._stopped()   # decided under the lock, so a new watcher starts a new thread
                        return
                    if jpg is not None:
                        self.seq += 1
                        self.jpg, self.t, self.error = jpg, time.time(), None
                        self.arrivals.append(self.t)
                        self.cv.notify_all()
        except Exception as e:  # noqa: BLE001 - a bug in a source: report it; the next watcher starts afresh
            self._problem(f"{type(e).__name__}: {e}")
        finally:
            with self.cv:
                if self.thread is threading.current_thread():
                    self._stopped()
            frames.close()   # closes the upstream connection

    def latest(self):
        """(seq, arrival time, JPEG) of the newest frame, or None."""
        with self.cv:
            return None if self.jpg is None else (self.seq, self.t, self.jpg)

    def wait_newer(self, seq, timeout):
        """The newest frame once there is one newer than `seq`; None if none comes within `timeout` s."""
        with self.cv:
            self.cv.wait_for(lambda: self.jpg is not None and self.seq > seq, timeout)
            return (self.seq, self.t, self.jpg) if self.jpg is not None and self.seq > seq else None

    def health(self):
        with self.cv:
            a = self.arrivals
            fps = (len(a) - 1) / (a[-1] - a[0]) if len(a) >= 3 and a[-1] > a[0] else None
            return {"watched": self.thread is not None, "seq": self.seq,
                    "age_s": None if self.t is None else round(time.time() - self.t, 1),
                    "fps": None if fps is None else round(fps, 2 if fps < 2 else 1), "error": self.error}


def _pause(seconds):
    """Wait before reconnecting, yielding None every second so the feed can stop meanwhile."""
    for _ in range(int(seconds)):
        yield None
        time.sleep(1.0)


def mjpeg_source(url):
    """Frames from an MJPEG stream (the Pi camera's server), reconnecting after any failure."""
    def frames(problem):
        wait = 2
        while True:
            try:
                with _opener.open(url, timeout=5) as r:
                    buf = b""
                    while True:
                        chunk = r.read1(65536)
                        if not chunk:
                            raise ConnectionError("the stream ended")
                        buf += chunk
                        while True:
                            s = buf.find(b"\xff\xd8")
                            e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                            if s < 0:
                                buf = buf[-1:]   # a 0xff here may start the next frame
                                break
                            if e < 0:
                                buf = buf[s:] if len(buf) - s < 8_000_000 else b""
                                break
                            jpg, buf = buf[s:e + 2], buf[e + 2:]
                            wait = 2
                            yield jpg
            except Exception as e:  # noqa: BLE001 - any failure: reconnect
                problem(f"{url}: {e}")
            yield from _pause(wait)
            wait = min(30, wait * 2)
    return frames


def printer_config():
    """(serial number, last known address) of the printer, as mhapi-service has them."""
    p = tomllib.loads((ROOT / "mhapi" / "safety.toml").read_text())["printer"]
    return p["serial"], p["ip_hint"]


def a1_frames(sock):
    """JPEGs from the printer's camera socket: each is a 16-byte header, whose first 4 bytes are the image's size
    (little-endian), then the image."""
    def read(n):
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(min(n - len(buf), 1 << 16))
            if not chunk:
                raise ConnectionError("the printer closed the camera connection")
            buf += chunk
        return bytes(buf)

    while True:
        size = struct.unpack("<I", read(16)[:4])[0]
        if not 0 < size < 16_000_000:
            raise ConnectionError(f"unexpected camera frame header (size {size})")
        jpg = read(size)
        if not jpg.startswith(b"\xff\xd8"):
            raise ConnectionError("the camera sent something other than a JPEG")
        yield jpg


def a1_stream(ip, code, timeout=15.0):
    """Log in to the camera port (user bblp, the LAN access code) and yield its frames."""
    auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") + code.encode().ljust(32, b"\0")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the printer's certificate is self-signed
    with socket.create_connection((ip, 6000), timeout=timeout) as raw, ctx.wrap_socket(raw) as s:
        s.sendall(auth)
        yield from a1_frames(s)


def a1_source():
    """Frames from the A1's camera, reconnecting after any failure and finding the printer again when it moved."""
    found = {"ip": None}   # where the printer was found last, kept across reconnections

    def frames(problem):
        wait = 2
        while True:
            try:
                from mhapi import bambu   # needs paho-mqtt, which only the rig needs
                serial, ip_hint = printer_config()
                code = bambu.access_code(serial)
                ip = found["ip"] or ip_hint
                if not bambu.reachable(ip, 6000):
                    problem(f"no answer from {ip}; looking for printer {serial} on the network")
                    ip = bambu.find_printer(serial)
                found["ip"] = ip
                for jpg in a1_stream(ip, code):
                    wait = 2
                    yield jpg
            except Exception as e:  # noqa: BLE001 - any failure: reconnect
                problem(f"printer camera: {e}")
            yield from _pause(wait)
            wait = min(30, wait * 2)
    return frames


def instrument_feeds(pi_url=PI_URL):
    return {"a1": Feed("a1", a1_source()), "pi": Feed("pi", mjpeg_source(pi_url))}


def nadir_px():
    """The pixel straight below the Pi camera (calibration/nadir.json), or None."""
    try:
        return json.loads((ROOT / "calibration" / "nadir.json").read_text())["nadir_px"]
    except (OSError, ValueError, KeyError):
        return None
