"""The camera stream: reads frames continuously, keeps the latest one and a frame-to-frame difference
for detecting motion (bambu/camstream.py)."""
import logging
import threading
import time
import urllib.request
from collections import deque

import cv2
import numpy as np

from .errors import ApiError
from .vision import decode

log = logging.getLogger("mhapi.camera")


def mjpeg_frames(url, stop):
    """Yields (jpeg bytes, arrival time) from an MJPEG stream, reconnecting after hiccups."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while not stop.is_set():
        try:
            with opener.open(url, timeout=5) as r:
                buf = b""
                while not stop.is_set():
                    chunk = r.read(16384)
                    if not chunk:
                        raise ConnectionError("stream ended")
                    buf += chunk
                    while True:
                        s = buf.find(b"\xff\xd8")
                        e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                        if e < 0:
                            if s > 0:
                                buf = buf[s:]
                            elif len(buf) > 8_000_000:
                                buf = b""
                            break
                        jpg, buf = buf[s:e + 2], buf[e + 2:]
                        yield jpg, time.time()
        except Exception as ex:  # noqa: BLE001 - any stream failure: reconnect
            log.warning("camera stream: %s; reconnecting", ex)
            stop.wait(1.0)


class Frame:
    __slots__ = ("seq", "t", "jpg", "thumb", "diff", "_bgr")

    def __init__(self, seq, t, jpg, thumb, diff):
        self.seq, self.t, self.jpg, self.thumb, self.diff, self._bgr = seq, t, jpg, thumb, diff, None

    def bgr(self):
        if self._bgr is None:
            self._bgr = decode(self.jpg)
        return self._bgr

    def gray(self):
        return cv2.cvtColor(self.bgr(), cv2.COLOR_BGR2GRAY)


def thumb_diff(a, b):
    """Mean absolute difference of two 1/8-scale grayscale thumbnails."""
    if a is None or b is None or a.shape != b.shape:
        return None
    return float(np.abs(a - b).mean())


class CameraStream:
    """Runs a frame source in a thread. `source(stop)` yields (jpeg, arrival time)."""

    MAX_AGE_S = 2.0

    def __init__(self, source):
        self._source = source
        self.cv = threading.Condition()
        self.latest = None
        self.seq = 0
        self.history = deque(maxlen=100)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def _run(self):
        prev = None
        for jpg, t in self._source(self._stop):
            img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_8)
            if img is None:
                continue
            th = img.astype(np.int16)
            f = Frame(self.seq + 1, t, jpg, th, thumb_diff(th, prev))
            prev = th
            with self.cv:
                self.seq = f.seq
                self.latest = f
                self.history.append(f)
                self.cv.notify_all()
            if self._stop.is_set():
                break

    @property
    def ok(self):
        f = self.latest
        return f is not None and time.time() - f.t < self.MAX_AGE_S

    def latest_frame(self, max_age=MAX_AGE_S):
        f = self.latest
        if f is None or time.time() - f.t > max_age:
            return None
        return f

    def frame_after(self, seq, timeout=3.0):
        """The first frame newer than `seq`. Raises camera_unavailable when none arrives in time."""
        with self.cv:
            if not self.cv.wait_for(lambda: self.seq > seq, timeout):
                raise ApiError("camera_unavailable", f"no camera frame for {timeout:g} s")
            for f in self.history:
                if f.seq > seq:
                    return f
            return self.latest

    def next(self, timeout=3.0):
        """The first frame that arrives after now (a fresh frame)."""
        return self.frame_after(self.seq, timeout)

    def close(self):
        self._stop.set()
