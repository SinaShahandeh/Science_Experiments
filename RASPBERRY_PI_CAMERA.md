# Raspberry Pi camera server

This document records the existing camera service discovered on the local network. It is an operational reference for developer agents, not a proposed implementation.

Last verified: **2026-10-11, approximately 17:04 Europe/Madrid**.

## Connection details

| Item | Verified value |
| --- | --- |
| mDNS hostname | `raspberrypi.local` |
| IPv4 address at verification | `192.168.4.177` |
| Camera page | `http://raspberrypi.local:8080/` |
| IP fallback | `http://192.168.4.177:8080/` |
| HTTP port | `8080` |
| SSH | TCP port `22` accepted a connection; login was not attempted |
| Network at verification | `192.168.4.0/22`, gateway `192.168.4.1` |

Prefer the hostname because the address may change. Both the page and video stream were accessible from the local Mac without credentials. Remote or internet access was not tested.

## HTTP interface

All paths below are relative to `http://raspberrypi.local:8080`.

| Method and path | Purpose | Verification |
| --- | --- | --- |
| `GET /` | Browser interface titled “Pi Camera”, with heading “Pi Camera Stream” | HTTP 200; HTML inspected |
| `GET /stream.mjpg` | Continuous MJPEG video stream | HTTP 200; three distinct JPEG frames received and one visually inspected |
| `GET /status.json` | Server uptime, video configuration, and audio status | JSON retrieved successfully |
| `GET /audio.ogg` | Audio stream referenced by the page, intended as Ogg/Opus | Advertised by page only; audio was not requested or verified |

The MJPEG response uses `Content-Type: multipart/x-mixed-replace; boundary=FRAME` and no-cache headers. The observed HTTP server header was `BaseHTTP/0.6 Python/3.13.5`.

### Video configuration

The status endpoint reported:

```json
{
  "video": {
    "width": 1280,
    "height": 720,
    "framerate": 30,
    "quality": "medium",
    "stream_fps": 30
  }
}
```

The inspected frame was 1280 × 720. The frame-rate values are server configuration, not a measured sustained throughput. Three frames with different SHA-256 hashes arrived in approximately 0.18 seconds, confirming changing frame data rather than merely a reachable HTML page.

At verification the image showed a tilted room view with windows, a white wall, and a cable across the top. This is only a historical orientation check; the camera may have moved.

### Browser functions

The page displays the MJPEG feed and provides:

- **Take photo:** copies the current image to a browser canvas and downloads a JPEG locally.
- **Record video + audio:** uses browser `MediaRecorder` and canvas capture to download a WebM recording locally; the page attempts VP8/Opus and falls back to the browser default.
- **Audio toggle:** starts or pauses the `/audio.ogg` playback; audio initially defaults to off.
- **Status display:** polls `/status.json` every two seconds.

These controls were identified in the HTML/JavaScript. Photo download, recording, and audio playback were not exercised. No server-side recording or capture API was established.

### Audio status and limitations

The server reported device `default`, sample rate `48000`, bitrate `64k`, and FFmpeg path `/usr/bin/ffmpeg`. Its ALSA capture-device listing contained only the heading, with no capture devices listed. Audio client and byte counters were zero; the last-error field was null.

An advertised audio endpoint and no recorded error do **not** establish that a microphone is attached or working. Verify audio separately if required.

## Read-only connection checks

Run these from a computer on the same network:

```sh
# macOS mDNS lookup; stop with Ctrl-C after the address appears.
dns-sd -G v4 raspberrypi.local

# Fetch the current status with a bounded timeout.
curl --fail --max-time 5 http://raspberrypi.local:8080/status.json
```

To verify that the video contains a decodable frame using Python's standard library, run:

```sh
python3 - <<'PY'
import pathlib
import time
import urllib.request

url = 'http://raspberrypi.local:8080/stream.mjpg'
output = pathlib.Path('/tmp/raspberrypi-camera-preview.jpg')
deadline = time.monotonic() + 10
buffer = b''

with urllib.request.urlopen(url, timeout=5) as response:
    print('Content-Type:', response.headers.get('Content-Type'))
    while time.monotonic() < deadline:
        chunk = response.read(8192)
        if not chunk:
            raise RuntimeError('Stream ended before a complete JPEG arrived')
        buffer += chunk
        start = buffer.find(b'\xff\xd8')
        end = buffer.find(b'\xff\xd9', start + 2) if start >= 0 else -1
        if end >= 0:
            output.write_bytes(buffer[start:end + 2])
            print('Saved:', output)
            break
        if len(buffer) > 16 * 1024 * 1024:
            raise RuntimeError('No complete JPEG within 16 MiB')
    else:
        raise TimeoutError('No complete JPEG within the capture window')
PY
```

Open or inspect the resulting JPEG to confirm actual image content. The MJPEG endpoint is continuous; do not wait for the HTTP response to finish normally. If mDNS fails, try the last observed IP, then rediscover the device rather than assuming that address still belongs to it.

## Unknowns for future development

The following have not been established:

- Raspberry Pi model, operating system, camera model, and camera connection type.
- SSH username, credentials, or authorized keys.
- Server source location, launch command, service manager, and restart procedure.
- Whether DHCP reserves the observed IP address.
- Working microphone/audio capture, sustained video performance, and recording compatibility.

Discovery used mDNS resolution, TCP connection probes, and HTTP GET requests. No configuration changes, SSH login, service restart, or device-control action was performed.

## Update 2026-10-11: SSH access and fixed exposure

- **Device:** Raspberry Pi 5 Model B, Debian 13 (trixie); camera module **OV5647** via Picamera2/libcamera.
- **SSH:** `ssh raspberrypi` from this Mac (user and key in the `raspberrypi` entry of `~/.ssh/config`). The user has passwordless `sudo`.
- **Server:** `mjpeg_server.py` in a folder under the user's home (`ssh raspberrypi systemctl cat mjpeg-stream.service` shows the path), run by systemd unit `mjpeg-stream.service`. Not under git; original saved as `mjpeg_server.py.bak-20261011`.
- **Added flags:** `--exposure-us` (disables auto exposure), `--gain` (analogue gain), `--awb-gains red,blue` (disables auto white balance). Also fixed a SIGTERM deadlock that made `systemctl stop` hang for 90 s.
- **Current locked values** live in drop-in `/etc/systemd/system/mjpeg-stream.service.d/fixed-exposure.conf`, written by `bambu/calibrate_exposure.py`. Since 2026-10-11 23:50 (v2): white paper in cell C-A1 → ~232 and the bare bed's darkest channel → ~8 (bed reads about RGB 30, 8, 35), so there is room above white and below black. Exposure 10 ms, analogue gain 1.56; the script only picks exposures that are whole multiples of 10 ms, so 100 Hz flicker from mains room lighting cannot beat against it. The camera must be parked so C-A1 sits on the nadir pixel. Result and frames: `calibration/exposure/`; the v1 result (paper → 255, bed → 0, 21.33 ms) is kept as `result_v1_paper255.json`. Delete the drop-in, `daemon-reload` and restart to return to auto.
- **Lighting:** the toolhead LED bar is battery powered and steady (frame-to-frame SD ≈ 0.2 %). The room ceiling light adds a ±2 % brightness oscillation with a period of 2–3 s; keep it off during runs. Test data: `calibration/flicker/`.
- **Also added:** `--brightness`, `--contrast`; camera now outputs full-range `YUV420/sYCC` (the default RGB path made the encoder squeeze to 16–235 while tagging JPEG as full range); adaptive contrast enhancement off via tuning copy `ov5647_fixed_tone.json` (`LIBCAMERA_RPI_TUNING_FILE` in the drop-in).
