"""Local MQTT link to the Bambu A1 in LAN mode (from bambu/bambu_link.py): connect, publish, receive reports.

Used by mhapi-service and by the independent mhapi-heatguard, so it must not import the service. It applies
no command policy; the service's G-code allowlist sits above it.
"""
import json
import logging
import re
import socket
import ssl
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

log = logging.getLogger("mhapi.printer")
STUDIO_CONF = Path.home() / "Library/Application Support/BambuStudio/BambuStudio.conf"


def access_code(serial):
    """The LAN access code, from Bambu Studio's config."""
    txt = re.sub(r"^#.*$", "", STUDIO_CONF.read_text(), flags=re.M)
    return json.loads(txt)["access_code"][serial]


def find_printer(serial, timeout=15.0):
    """Listen for the printer's SSDP broadcast (UDP 2021, every few seconds) and return its IP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", 2021))
    s.settimeout(1.0)
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < timeout:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            if serial in data.decode(errors="ignore"):
                return addr[0]
    finally:
        s.close()
    raise TimeoutError(f"printer {serial} not found on the LAN")


def reachable(ip, port=8883, timeout=2.0):
    try:
        socket.create_connection((ip, port), timeout).close()
        return True
    except OSError:
        return False


class PrinterLink:
    """MQTT connection to the printer. `on_report(t, payload)` runs on the MQTT thread for every report.

    If the connection drops and can't be re-established for `rediscover_after_s`, the printer is looked up
    again by serial number (its address changes after power cycles; safety spec N1).
    """

    def __init__(self, serial, ip_hint, on_report, code=None, rediscover_after_s=30.0):
        self.serial, self.ip = serial, ip_hint
        self.on_report = on_report
        self._code = code
        self._connected = threading.Event()
        self._client = None
        self._stop = threading.Event()
        self._down_since = None
        self.rediscover_after_s = rediscover_after_s
        self.epoch = 0  # incremented on every (re)connection
        self._lock = threading.Lock()

    # -- connection -------------------------------------------------------------------------------------
    def start(self, timeout=10.0):
        code = self._code or access_code(self.serial)
        self._code = code
        if not reachable(self.ip):
            log.info("printer not at %s; looking for serial %s on the LAN", self.ip, self.serial)
            self.ip = find_printer(self.serial)
            log.info("printer found at %s", self.ip)
        self._connect(self.ip)
        if not self._connected.wait(timeout):
            raise TimeoutError(f"MQTT connection to {self.ip} timed out")
        threading.Thread(target=self._supervise, name="printer-link", daemon=True).start()
        return self

    def _connect(self, ip):
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
        c.username_pw_set("bblp", self._code)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # the printer uses a self-signed certificate
        c.tls_set_context(ctx)
        c.reconnect_delay_set(1, 10)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.connect_async(ip, 8883, 60)
        c.loop_start()
        with self._lock:
            self._client = c

    def _on_connect(self, c, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.warning("MQTT connect refused: %s", reason_code)
            return
        c.subscribe(f"device/{self.serial}/report")
        self.epoch += 1
        self._down_since = None
        self._connected.set()
        log.info("MQTT connected to %s", self.ip)

    def _on_disconnect(self, c, userdata, flags, reason_code, properties=None):
        self._connected.clear()
        self._down_since = self._down_since or time.time()
        log.warning("MQTT disconnected (%s)", reason_code)

    def _on_message(self, c, userdata, msg):
        t = time.time()
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            return
        try:
            self.on_report(t, payload)
        except Exception:  # noqa: BLE001 - never let a handler kill the MQTT thread
            log.exception("report handler failed")

    def _supervise(self):
        while not self._stop.wait(2.0):
            down = self._down_since
            if down is None or time.time() - down < self.rediscover_after_s:
                continue
            try:
                ip = find_printer(self.serial)
            except TimeoutError:
                log.warning("printer not found on the LAN; still trying")
                self._down_since = time.time()
                continue
            log.warning("reconnecting to the printer at %s", ip)
            with self._lock:
                old = self._client
            try:
                old.loop_stop()
                old.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.ip = ip
            self._down_since = time.time()
            self._connect(ip)

    @property
    def connected(self):
        return self._connected.is_set()

    def publish(self, payload):
        """Send one request (fire and forget, MQTT QoS 0). Returns False if the link is down."""
        with self._lock:
            c = self._client
        if c is None or not self.connected:
            return False
        info = c.publish(f"device/{self.serial}/request", json.dumps(payload))
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def close(self):
        self._stop.set()
        with self._lock:
            c = self._client
        if c is not None:
            c.loop_stop()
            c.disconnect()
