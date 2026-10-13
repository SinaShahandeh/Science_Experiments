"""Grab one JPEG frame from the A1/P1 camera stream (TLS port 6000)."""
import socket, ssl, struct, sys
from bambu_link import PRINTER_IP, access_code


def snapshot(path="snapshot.jpg", ip=PRINTER_IP):
    auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
    auth += b"bblp".ljust(32, b"\0") + access_code().encode().ljust(32, b"\0")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((ip, 6000), timeout=10) as raw, ctx.wrap_socket(raw) as s:
        s.sendall(auth)

        def read(n):
            buf = b""
            while len(buf) < n:
                chunk = s.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("stream closed")
                buf += chunk
            return buf

        size = struct.unpack("<I", read(16)[:4])[0]
        img = read(size)
    assert img[:2] == b"\xff\xd8", "not a JPEG"
    open(path, "wb").write(img)
    return path


if __name__ == "__main__":
    print(snapshot(sys.argv[1] if len(sys.argv) > 1 else "snapshot.jpg"))
