# Plexus Python SDK — Full Reference

## Installation

```bash
# Full device setup (Linux — handles Python, venv, auth, hardware support)
curl -sL https://app.plexus.company/setup | bash -s -- --key plx_xxx --name drone-01

# Manual install
pip install plexus-python

# With video support (Pillow for non-OpenCV frame formats)
pip install "plexus-python[video]"

# Authenticate interactively (saves to ~/.plexus/config.json)
plexus init
```

## Client Initialization

```python
from plexus import Plexus

# Reads PLEXUS_API_KEY from env or ~/.plexus/config.json
px = Plexus(source_id="drone-01")

# Explicit key
px = Plexus(api_key="plx_xxx", source_id="drone-01")

# With persistent buffering (survives restarts)
px = Plexus(source_id="drone-01", persistent_buffer=True)

# Force HTTP transport (no WebSocket, no clock sync)
px = Plexus(source_id="drone-01", transport="http")
```

## send(metric, value)

Send a single reading. The primary method.

```python
px.send("engine.rpm", 3450)
px.send("coolant.temp", 82.3)

# With tags
px.send("motor.rpm", 3200, tags={"motor_id": "A1"})

# With explicit timestamp (Unix seconds)
import time
px.send("battery.voltage", 12.4, timestamp=time.time())
```

**Value types accepted:**

| Type | Example | When to use |
|------|---------|-------------|
| `float` / `int` | `72.5`, `3450` | Sensor readings, counters |
| `str` | `"RUNNING"`, `"E_STALL"` | State machines, error codes |
| `bool` | `True` | Binary flags |
| `dict` | `{"x": 1.5, "y": 2.3}` | Vectors, structured readings |
| `list` | `[0.5, 1.2, -0.3]` | Waveforms, joint angles |

## send_batch(points)

Send multiple readings in one network call. All points share the same timestamp.

```python
px.send_batch([
    ("temperature", 22.4),
    ("humidity",    58.1),
    ("pressure",    1013.2),
])

# With shared tags and explicit timestamp
px.send_batch(
    points=[("battery.voltage", 12.4), ("motor.rpm", 3200)],
    tags={"firmware": "1.4.2"},
    timestamp=time.time(),
)
```

For per-point timestamps, call `send()` in a loop instead.

## event(name, data)

Record a discrete occurrence — faults, state transitions, operator actions. Stored separately from metrics; appears as timeline markers in the dashboard.

```python
px.event("fault", "E-stop triggered")
px.event("state_change", {"from": "IDLE", "to": "RUNNING"})
px.event("sensor_error", {"sensor": "imu", "code": 42}, tags={"motor": "A"})
```

## run(run_id)

Group telemetry into a named recording. All `send()` calls inside the context are tagged with `run_id`.

```python
with px.run("thermal-cycle-001"):
    while running:
        px.send("temperature", read_temp())
        px.send("pressure", read_pressure())
```

## send_video_frame(frame, camera_id)

Send a single video frame. Use when your code owns the capture loop.

```python
import cv2

cap = cv2.VideoCapture(0)
while True:
    ok, frame = cap.read()
    if ok:
        px.send_video_frame(frame, camera_id="front")
```

**Accepted frame types:**
- `numpy ndarray` (H × W × C) — from OpenCV or picamera2; requires `opencv-python`
- JPEG `bytes` — passed through as-is, zero re-encode overhead
- Other image `bytes` (PNG, BMP, WebP) — decoded and re-encoded as JPEG via Pillow; requires `plexus-python[video]`

Frames are JPEG-encoded at `quality=85` by default. Frames over ~750 KB are adaptively re-encoded; gateway hard limit is 1 MB.

## stream_camera(url, camera_id)

Stream from an RTSP feed, HLS stream, or file path. SDK runs FFmpeg internally. Requires FFmpeg on `$PATH`.

```python
stop = px.stream_camera("rtsp://192.168.1.100/stream", camera_id="front")
# runs in a background thread
stop.set()  # stop streaming
```

Returns a `threading.Event`. Call `.set()` to stop.

## on_command(name, handler)

Register a handler for commands sent from the dashboard or API. Register before the first `send()`.

```python
def reboot(name, params):
    delay = params.get("delay_s", 0)
    # ... reboot logic ...
    return {"ok": True, "delay": delay}

px.on_command("reboot", reboot, description="reboot the device")
px.send("temperature", 72.5)  # opens socket, advertises command in auth frame
```

The SDK sends an `ack` frame before invoking the handler, then a `result` or `error` frame.

## Buffer Methods

```python
px.buffer_size()    # number of buffered points
px.flush_buffer()   # force flush to gateway
```

## Transport

Default: WebSocket to `/ws/device` with HTTP fallback to `POST /ingest`.

WebSocket gives lower latency and enables live command delivery. Falls back to HTTP transparently if the socket is unavailable.

```python
px = Plexus()                         # WS with HTTP fallback (default)
px = Plexus(transport="http")         # HTTP only (no commands, no clock sync)
```

## Timestamps and Clock Correction

Over WebSocket, the SDK synchronizes with the gateway clock on every connection. Useful on boards that boot without NTP (Raspberry Pi, Jetson).

```python
px.send("temp", 72.5)                  # SDK picks time, gateway-synced
px.send("temp", 72.5, timestamp=t)     # explicit timestamp, no correction applied
```

**Pass explicit timestamp when:** you have a reliable external time source (GPS, trusted RTC).  
**Omit timestamp when:** the device may have booted without NTP.

Limits:
- Clock sync refreshes on reconnect only — drifting RTC accumulates error between reconnects
- `transport="http"` gets no clock sync
- `send_batch()` shares one timestamp across the whole batch

## Bring Your Own Protocol

```python
# MAVLink
for msg in conn:
    if msg.get_type() == "ATTITUDE":
        px.send("attitude.roll", msg.roll)

# CAN
for msg in bus:
    px.send(f"can.0x{msg.arbitration_id:x}", int.from_bytes(msg.data, "big"))

# MQTT
def on_message(_c, _u, msg):
    px.send(msg.topic.replace("/", "."), float(msg.payload))

# I2C (Adafruit CircuitPython)
px.send("temperature", bme.temperature)
```

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `PLEXUS_API_KEY` | API key (required if not in config) | none |
| `PLEXUS_GATEWAY_URL` | HTTP ingest URL | `https://gateway.plexus.company` |
| `PLEXUS_GATEWAY_WS_URL` | WebSocket URL | `wss://gateway.plexus.company` |

## CLI Commands

```bash
plexus init              # browser-based auth, saves key to ~/.plexus/config.json
plexus whoami            # show current key + endpoint
plexus logout            # clear local key
plexus init --force      # replace existing key
plexus init --name foo   # label for the issued key (default: cli-<hostname>)
```

Config lives at `~/.plexus/config.json`.
