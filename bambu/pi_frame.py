"""Grab one JPEG frame from the Raspberry Pi MJPEG stream."""
import sys, time, urllib.request

URL = "http://raspberrypi.local:8080/stream.mjpg"


def pi_frame(path="pi_frame.jpg", skip=3, url=URL):
    """Save the (skip+1)-th complete frame so we don't get a stale buffered one."""
    buf = b""
    n = 0
    with urllib.request.urlopen(url, timeout=5) as r:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            buf += r.read(16384)
            s = buf.find(b"\xff\xd8")
            e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
            if e >= 0:
                if n >= skip:
                    open(path, "wb").write(buf[s:e + 2])
                    return path
                n += 1
                buf = buf[e + 2:]
    raise TimeoutError("no frame")


if __name__ == "__main__":
    print(pi_frame(sys.argv[1] if len(sys.argv) > 1 else "pi_frame.jpg"))
