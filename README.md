# plexus-python

**Thin Python SDK for [Plexus](https://plexus.company).** Send telemetry to the Plexus gateway in one line. Storage, dashboards, alerts, and fleet management live in the platform — this package just ships your data.

[![PyPI](https://img.shields.io/pypi/v/plexus-python)](https://pypi.org/project/plexus-python/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

## Quick Start

```bash
pip install plexus-python
```

```python
from plexus import Plexus

px = Plexus(api_key="plx_xxx", source_id="device-001")
px.send("temperature", 72.5)
```

Get an API key at [app.plexus.company](https://app.plexus.company) → Devices → Add Device.

## Device identity

Every device needs a unique `source_id`. The recommended way to set one on a real host is the bootstrap script, which requires a device name up front:

```bash
curl -sL https://app.plexus.company/setup | bash -s -- \
  --key plx_xxx --name drone-01
```

The name must match `^[a-z0-9][a-z0-9_-]{1,62}$`. `setup.sh` refuses to run without `--name` (or without a TTY to prompt for one) — this is deliberate, because the previous `hostname` fallback silently merged telemetry from cloned SD-card images that all booted as `raspberrypi`.

**Names are not auto-deduplicated.** The gateway echoes back whatever `source_id` you declare, unchanged — pick a unique name per device (that's what `--name` and `source_id=...` are for). Two devices that declare the same name write into the same source.

In normal code, you usually just pass `source_id=...` explicitly to `Plexus(...)` and never have to think about it.

## Core methods

### `send(metric, value)` — stream a reading

The main method. Call it every time you have a new sensor reading.

```python
px = Plexus(source_id="rig-01")   # reads PLEXUS_API_KEY from env

px.send("engine.rpm", 3450)
px.send("coolant.temp", 82.3)
```

`metric` is a dot-namespaced string (`"motor.rpm"`, `"gps.fix_quality"`). `value` accepts any JSON-serializable type:

| Type | Example | When to use |
|------|---------|-------------|
| `float` / `int` | `72.5`, `3450` | Sensor readings, counters |
| `str` | `"RUNNING"`, `"E_STALL"` | State machines, error codes |
| `bool` | `True` | Binary flags |
| `dict` | `{"x": 1.5, "y": 2.3}` | Vectors, structured readings |
| `list` | `[0.5, 1.2, -0.3]` | Waveforms, joint angles |

Optional arguments:
- `tags={"motor_id": "A1"}` — key-value labels for filtering in the dashboard
- `timestamp=t` — explicit Unix timestamp in seconds; omit to let the SDK pick (see [Timestamps](#timestamps-and-clock-correction))

### `send_batch(points)` — send multiple readings at once

Use this when you sample several sensors together and want them to share a timestamp and land in one network call.

```python
px.send_batch([
    ("temperature", 22.4),
    ("humidity",    58.1),
    ("pressure",    1013.2),
])
```

`points` is a list of `(metric, value)` tuples, or `(metric, value, timestamp)` 3-tuples when you need a per-point timestamp. Points without their own timestamp share the batch timestamp (now, unless you pass `timestamp=t`).

### `event(name, data)` — record a discrete occurrence

Use `event()` for things that *happen* rather than things you *measure continuously*. Faults, state transitions, operator actions, log entries — anything you'd put on a timeline as a marker rather than plot as a graph.

```python
px.event("fault",        "E-stop triggered")
px.event("state_change", {"from": "IDLE", "to": "RUNNING"})
px.event("sensor_error", {"sensor": "imu", "code": 42}, tags={"motor": "A"})
```

The platform displays events as markers overlaid on your telemetry charts, not as time-series lines.

### `run(run_id)` — group data into a named recording

```python
with px.run("thermal-cycle-001"):
    while running:
        px.send("temperature", read_temp())
```

All `send()` calls inside the context are tagged with `run_id`, making it easy to isolate and replay that slice of data in the dashboard.

## Video streaming

Two methods depending on whether you control the capture loop or just have a URL.

### `send_video_frame(frame, camera_id)` — send frames you capture yourself

Use this when your code owns the capture loop — a `picamera2` callback, an OpenCV `VideoCapture` loop, or an FFmpeg pipe you manage. Pass each frame and the SDK ships it to Plexus over WebSocket.

```python
import cv2

cap = cv2.VideoCapture(0)
while True:
    ok, frame = cap.read()
    if ok:
        px.send_video_frame(frame, camera_id="front")
```

Accepted frame types:
- **numpy ndarray** (H × W × C) — from OpenCV or picamera2; requires `opencv-python`
- **JPEG bytes** — passed through as-is, zero re-encode overhead
- **Other image bytes** (PNG, BMP, WebP) — decoded and re-encoded as JPEG via Pillow; requires `pip install plexus-python[video]`

`camera_id` identifies which camera the frame came from. Use distinct IDs when streaming from multiple cameras simultaneously (`"front"`, `"rear"`, `"cam:0"`).

### `stream_camera(url, camera_id)` — stream from an RTSP URL or file

Use this when you have an RTSP stream or video file and don't want to manage the capture loop yourself. The SDK runs FFmpeg internally and handles the rest. Requires FFmpeg on `$PATH`.

```python
stop = px.stream_camera("rtsp://192.168.1.100/stream", camera_id="front")
# ... do other work ...
stop.set()  # stop streaming
```

Returns a `threading.Event` — call `.set()` to stop. Runs in a background thread so it doesn't block your main loop.

**Which to use:** if you're piping from `rpicam-vid`, `picamera2`, or your own capture process, use `send_video_frame()`. If you have an RTSP URL or file path, use `stream_camera()`.

## Bring Your Own Protocol

This package ships no adapters, auto-detection, or daemons — just the client. Use whatever library you'd use anyway and pipe values into `px.send()`.

```python
# MAVLink (pymavlink)
for msg in conn:
    if msg.get_type() == "ATTITUDE":
        px.send("attitude.roll", msg.roll)

# CAN (python-can)
for msg in bus:
    px.send(f"can.0x{msg.arbitration_id:x}", int.from_bytes(msg.data, "big"))

# MQTT (paho-mqtt)
def on_message(_c, _u, msg):
    px.send(msg.topic.replace("/", "."), float(msg.payload))

# I2C sensor (Adafruit CircuitPython)
px.send("temperature", bme.temperature)
```

See [`examples/`](examples/) for runnable versions of each.

## Reliability

Every send buffers locally before hitting the network, retries with exponential backoff, and keeps your data safe across outages. Enable SQLite persistence to survive restarts and power loss:

```python
px = Plexus(persistent_buffer=True)
```

Point counts and flush:

```python
px.buffer_size()
px.flush_buffer()
```

## Timestamps and clock correction

By default — `px.send("temp", 72.5)` with no `timestamp` argument — the SDK picks the time itself. Over WebSocket, it synchronizes with the gateway clock on every connection, so data lands at the right place on the timeline even if the device's system clock is wrong (no NTP on first boot, stale RTC, fresh OS image).

```python
px.send("temperature", 72.5)                # SDK picks time; gateway-synced over WS
px.send("temperature", 72.5, timestamp=t)   # your timestamp, used as-is, no correction
```

**Pass an explicit timestamp when** you have a reliable external time source (GPS, trusted RTC, host NTP) or are replaying historical data with known timestamps.

**Omit timestamp when** the device may have booted without NTP — which is the default on Raspberry Pi, Jetson, and most embedded Linux boards without a network connection at first boot.

**Known limits:**
- Clock sync refreshes on WebSocket (re)connect. A device with a drifting RTC that stays connected for many days accumulates uncorrected drift between reconnects.
- The HTTP fallback path (used when the WebSocket is unavailable) does not receive clock sync — timestamps default to the uncorrected device clock.
- `send_batch()` shares one timestamp across the batch by default; pass `(metric, value, timestamp)` 3-tuples for per-point timestamps.

## Transport

By default the SDK connects over a **WebSocket** to `/ws/device` on the gateway — same wire protocol as the C SDK. This gives you:

- lower-latency streaming of telemetry,
- live command delivery from the UI / API to the device.

If the socket is unavailable, sends transparently fall back to `POST /ingest` so no data is lost.

```python
# ws with transparent http fallback — this is the only mode
px = Plexus()
```

There is no transport selector: the SDK always prefers the WebSocket and falls back to `POST /ingest` on its own when the socket is unavailable.

### Handling commands

Register a handler before the first `send()` so the command is advertised in the auth frame:

```python
def reboot(name, params):
    delay = params.get("delay_s", 0)
    # ... reboot logic ...
    return {"ok": True, "delay": delay}

px = Plexus()
px.on_command("reboot", reboot, description="reboot the device")
px.send("temperature", 72.5)   # opens the socket, waits for auth
```

The SDK sends an `ack` frame before invoking the handler, then a `result` frame with whatever the handler returns (or an `error` frame if it raises).

## Environment Variables

| Variable                | Description                  | Default                          |
| ----------------------- | ---------------------------- | -------------------------------- |
| `PLEXUS_API_KEY`        | API key (required)           | none                             |
| `PLEXUS_GATEWAY_URL`    | HTTP ingest URL              | `https://gateway.plexus.company` |
| `PLEXUS_GATEWAY_WS_URL` | WebSocket URL              | `wss://gateway.plexus.company`   |

## Architecture

```
Your code ── px.send() ── HTTP POST /ingest ──> plexus-gateway ──> ClickHouse + Dashboard
```

One thin path. No agent, no daemon, no adapters. If you want the full HardwareOps platform — dashboards, alerts, RCA, fleet views — that's the web UI at app.plexus.company. This package gets your data there.

## License

Apache 2.0
