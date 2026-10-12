"""Minimal local MQTT link to a Bambu Lab printer in LAN mode (exploration)."""
import json, re, socket, ssl, threading, time
from pathlib import Path
import paho.mqtt.client as mqtt

PRINTER_IP = "192.168.4.54"  # DHCP: changes after power cycles; find_printer() rediscovers it
SERIAL = "03919D532700850"
_STUDIO_CONF = Path.home() / "Library/Application Support/BambuStudio/BambuStudio.conf"


def access_code():
    txt = re.sub(r"^#.*$", "", _STUDIO_CONF.read_text(), flags=re.M)
    return json.loads(txt)["access_code"][SERIAL]


def find_printer(serial=SERIAL, timeout=15):
    """Listen for the printer's SSDP broadcast (UDP 2021, every few seconds) and return its IP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", 2021))
    s.settimeout(1)
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            if serial in data.decode(errors="ignore"):
                return addr[0]
    finally:
        s.close()
    raise TimeoutError(f"printer {serial} not found on the LAN")


def _reachable(ip, port=8883, timeout=2):
    try:
        socket.create_connection((ip, port), timeout).close()
        return True
    except OSError:
        return False


class Printer:
    def __init__(self, ip=PRINTER_IP, serial=SERIAL, code=None):
        if not _reachable(ip):
            ip = find_printer(serial)
            print(f"printer rediscovered at {ip}; update PRINTER_IP in bambu_link.py")
        self.ip = ip
        self.serial = serial
        self.state = {}
        self.reports = []
        self._seq = 0
        self._connected = threading.Event()
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
        self.c.username_pw_set("bblp", code or access_code())
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # printer uses a self-signed BBL CA cert
        self.c.tls_set_context(ctx)
        self.c.on_connect = self._on_connect
        self.c.on_message = self._on_message
        self.c.connect(ip, 8883, 60)
        self.c.loop_start()
        if not self._connected.wait(10):
            raise TimeoutError("MQTT connect timed out")

    def _on_connect(self, c, u, flags, rc, props=None):
        if rc != 0:
            print("connect failed:", rc)
            return
        c.subscribe(f"device/{self.serial}/report")
        self._connected.set()

    def _on_message(self, c, u, msg):
        d = json.loads(msg.payload)
        self.reports.append((time.time(), d))
        if "print" in d:
            self.state.update(d["print"])

    def publish(self, payload):
        self.c.publish(f"device/{self.serial}/request", json.dumps(payload))

    def _next_seq(self):
        self._seq += 1
        return str(self._seq)

    def pushall(self):
        self.publish({"pushing": {"sequence_id": self._next_seq(), "command": "pushall"}})

    def gcode(self, lines):
        if isinstance(lines, (list, tuple)):
            lines = "\n".join(lines)
        self.publish({"print": {"sequence_id": self._next_seq(), "command": "gcode_line",
                                "param": lines + "\n"}})

    def close(self):
        self.c.loop_stop()
        self.c.disconnect()
