"""Continuous reader for the Pi MJPEG stream: keeps the latest frame + arrival time,
and a small grayscale thumbnail for motion detection (frame-to-frame difference)."""
import threading, time, urllib.request
import cv2
import numpy as np

URL = "http://raspberrypi.local:8080/stream.mjpg"


class CamStream:
    def __init__(self, url=URL):
        self.url = url
        self.cv = threading.Condition()
        self.seq = 0
        self.jpg = None
        self.t = None
        self.thumb = None
        self.diff = None  # mean abs diff of thumbnail vs previous frame
        self._stop = False
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()
        self.wait_seq(1, 10)

    def _run(self):
        while not self._stop:
            try:
                with urllib.request.urlopen(self.url, timeout=5) as r:
                    buf = b""
                    while not self._stop:
                        buf += r.read(16384)
                        while True:
                            s = buf.find(b"\xff\xd8")
                            e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                            if e < 0:
                                break
                            jpg = buf[s:e + 2]
                            buf = buf[e + 2:]
                            t = time.time()
                            img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_8)
                            th = img.astype(np.int16) if img is not None else None
                            with self.cv:
                                d = None
                                if th is not None and self.thumb is not None and th.shape == self.thumb.shape:
                                    d = float(np.abs(th - self.thumb).mean())
                                self.jpg, self.t, self.thumb, self.diff = jpg, t, th, d
                                self.seq += 1
                                self.cv.notify_all()
            except Exception as ex:  # reconnect on stream hiccups
                print("camstream reconnect:", ex, flush=True)
                time.sleep(0.5)

    def wait_seq(self, seq, timeout):
        with self.cv:
            if not self.cv.wait_for(lambda: self.seq >= seq, timeout):
                raise TimeoutError("no camera frame")
            return self.seq, self.jpg, self.t, self.diff

    def next(self, timeout=5):
        return self.wait_seq(self.seq + 1, timeout)

    def wait_settled(self, t_cmd, move_thr=3.0, still_thr=0.8, still_n=4, timeout=8):
        """After a move command sent at t_cmd: wait for motion (diff > move_thr), then for
        still_n consecutive still frames (diff < still_thr). Returns (settle_time, saw_motion)."""
        t0 = time.time()
        moving, still = False, 0
        while time.time() - t0 < timeout:
            _, _, t, d = self.next()
            if d is None:
                continue
            if not moving:
                if d > move_thr:
                    moving = True
                continue
            still = still + 1 if d < still_thr else 0
            if still >= still_n:
                return t, True
        return time.time(), moving

    def close(self):
        self._stop = True
