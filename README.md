# plexus-python

Plexus is telemetry dashboards for hardware teams: stream data from drones, robots and IoT devices, or connect the database you already run, and get live dashboards and alerts. Website: [plexus.company](https://plexus.company). Docs: [docs.plexus.company](https://docs.plexus.company).

**This is the thin Python SDK for Plexus.** Send telemetry to the Plexus gateway in one line. Storage, dashboards, alerts, and fleet management live in the platform — this package just ships your data.

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

Get an API key at [app.plexus.company/api](https://app.plexus.company/api), or run `plexus init` to authorize the machine in a browser.

## Device identity

Every device needs a unique `source_id`. The recommended way to set one on a real host is the bootstrap script, which requires a device name up front:

```bash
curl -sL https://app.plexus.company/setup | bash -s -- \
  --key plx_xxx --name drone-01
```

The name is turned into the device's `source_id`, which must match `^[a-z0-9][a-z0-9._-]*$` (max 256 chars). Pass `--name` every time. Without it, and without `source_id=...` in code, the SDK makes up a random id like `source-1a2b3c4d` on first run and saves it to `~/.plexus/config.json`. Don't use the hostname: cloned SD-card images all boot as `raspberrypi`, and their telemetry merges into one source.

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

### `batch()` — coalesce a fast stream of readings

Use this above a few readings per second. Every `send()` is one WebSocket message, and the gateway limits **messages**, not points — 2,000/s on a connection. 25 channels at 100 Hz sent one at a time is 2,500 messages/s, and the overflow is discarded before it is stored.

```python
with px.batch(interval_ms=50) as b:
    while running:
        b.send("att.pos_x", att.x)
        b.send("att.rate_x", gyro.x)
        b.send("frames.captured", grabber.count)
```

`b.send()` takes the same arguments as `px.send()`. A background thread flushes the queue every `interval_ms`, and leaving the block flushes what is left, so nothing is stranded. Readings keep the timestamp they were taken at, not the one they were flushed at.

If the gateway does discard frames it reports `RATE_LIMITED`; the SDK counts those on `px.rate_limited_frames` and raises `RateLimitedError` on the next send rather than letting the loss pass unnoticed.

### `run(name)` — mark a test run

A run is a named window on a source. Runs are recalled on `/runs`, compared against each other aligned at T+0, and checked against their pass criteria when they close.

```python
with px.run("hotfire-03", pass_criteria=[
    {"metric": "motor.temp_c", "operator": "<", "value": 85},
]) as run:
    bench.execute()
```

Leaving the block closes the run as `completed`; an exception closes it as `aborted` and re-raises. Use `px.start_run()` / `px.end_run()` when the two halves happen in different places — `end_run()` returns the run with its verdict in `test_result`.

### `event(name, data)` — record a discrete occurrence

Use `event()` for things that *happen* rather than things you *measure continuously*. Faults, state transitions, operator actions, log entries — anything you'd put on a timeline as a marker rather than plot as a graph.

```python
px.event("fault",        "E-stop triggered")
px.event("state_change", {"from": "IDLE", "to": "RUNNING"})
px.event("sensor_error", {"sensor": "imu", "code": 42}, tags={"motor": "A"})
```

The platform displays events as markers overlaid on your telemetry charts, not as time-series lines.

Limits per event: a string value up to 256 bytes, a dict or list value up to 4,096 bytes of JSON, and up to 16 tags. The gateway rejects anything larger.

### Logs

There is no log-file upload and no `logging.Handler` in this package. To get important log lines into Plexus, send them as events:

```python
px.event("log", {"level": "error", "msg": "IMU read timed out"})
```

Forward the lines you would want on the timeline next to your telemetry (errors, warnings, state changes), not every debug line. Each call is one message, and the gateway limits messages (see [`batch()`](#batch--coalesce-a-fast-stream-of-readings)).

## Video streaming

Two methods depending on whether you control the capture loop or just have a URL.

Video needs a paid plan: frames go over the WebSocket, which the gateway refuses on the Free plan. Frames are relayed live to anyone watching. They are stored only when someone presses **Record** in the app, for up to 4 hours per recording.

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

Every send buffers locally before hitting the network, retries with exponential backoff, and keeps your data safe across outages. The buffer is on disk (SQLite) by default, so it survives restarts and power loss. To keep it in memory only:

```python
px = Plexus(persistent_buffer=False)
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

By default the SDK connects over a **WebSocket** to `/ws/device` on the gateway — the gateway's device wire protocol. This gives you:

- lower-latency streaming of telemetry,
- the channel that will carry actions triggered from a Plexus dashboard.

If the socket is unavailable, sends transparently fall back to `POST /ingest` so no data is lost.

```python
# ws with transparent http fallback — this is the only mode
px = Plexus()
```

There is no transport selector: the SDK always prefers the WebSocket and falls back to `POST /ingest` on its own when the socket is unavailable.

Either way, plain `px.send()` is one message per call; it does not batch. `px.send_batch()` sends one list as one message, and `px.batch()` groups a fast stream for you in the background.

**On the Free plan** the gateway refuses the device WebSocket (`streaming_requires_plan`). The SDK falls back to HTTP by itself, so `send()`, `send_batch()`, `batch()` and `event()` all still work. Live streaming and video need a paid plan. Free also caps you at 3 devices and 7 days of history.

### Commands

Declare what your code can be asked to do. Declare before the first `send()`:
the declaration travels in the auth frame.

```python
from plexus import Plexus

px = Plexus(source_id="pod-07")

@px.command("power_off", title="Power off", danger="critical", idempotent=True,
            expires_in=30,
            params={"outlet": {"type": "integer", "minimum": 1, "maximum": 8}})
def power_off(run, outlet):
    pdu.outlet(outlet).off()                 # your code
    return {"outlet": outlet, "state": "off"}

px.serve()   # blocks until Ctrl+C / SIGTERM; or keep calling px.send(...)
```

Use an API key created with **Receive commands** on the Plexus API Keys page.
Other keys keep sending telemetry, but their commands are ignored.

The handler is called as `handler(run, **params)`, with every parameter already
checked and coerced; a bad parameter is refused before your code runs. The
return value becomes the run's result; an exception makes the run `failed`.

| Argument      | What it does |
| ------------- | ------------ |
| `params`      | `{name: spec}`: `string` (`maxLength`, `enum`), `integer`/`number` (`minimum`, `maximum`, `unit`), `boolean`, plus `title`, `description`, `default`, `required`. At most 16. Flat: no nesting, no arrays |
| `danger`      | `normal` (one click), `dangerous` (confirm dialog), `critical` (confirm + type the device slug) |
| `idempotent`  | True when running it twice is harmless. Only idempotent runs are redelivered after a drop |
| `expires_in`  | Seconds a run stays worth doing, 5–3600. A late run is refused, not queued |
| `concurrency` | `accept` allows overlapping runs; `reject` refuses a second while one is going |

The SDK acknowledges each run, never runs the same run id twice, judges expiry
on a monotonic clock, and replays unacknowledged statuses after a reconnect. If
your org stores command metadata only, no result or error text leaves the device.

Run them from the **Commands** page or a dashboard panel in Plexus.

`px.on_command(name, handler, ...)` is deprecated. It still answers commands
with its old `handler(command_name, params_dict)` signature, but is no longer
advertised in the auth frame.

## Environment Variables

| Variable                | Description                  | Default                          |
| ----------------------- | ---------------------------- | -------------------------------- |
| `PLEXUS_API_KEY`        | API key (required)           | none                             |
| `PLEXUS_GATEWAY_URL`    | HTTP ingest URL              | `https://gateway.plexus.company` |
| `PLEXUS_GATEWAY_WS_URL` | WebSocket URL              | `wss://gateway.plexus.company`   |

## Agent skills

Three skills ship with the package and teach a coding agent the Plexus API —
the endpoints, the live stream, and the mistakes that produce a silent 400.

```bash
plexus skills install          # -> ~/.claude/skills
plexus skills install --project  # -> ./.claude/skills, travels with the repo
```

Then ask for what you want in plain language: *"send my ESP32's battery voltage
to Plexus"*, *"build me a fleet dashboard"*. Plain Markdown, no install, no
credentials. See [skills/README.md](skills/README.md).

## Architecture

```
Your code ── px.send() ── WebSocket /ws/device (or HTTP POST /ingest) ──> plexus-gateway ──> ClickHouse + Dashboard
```

One thin path. No agent, no daemon, no adapters. If you want the full HardwareOps platform — dashboards, alerts, RCA, fleet views — that's the web UI at app.plexus.company. This package gets your data there.

## License

Apache 2.0
