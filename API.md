# Plexus API

Send telemetry data to Plexus using HTTP or WebSocket.

## Architecture

**Two ways to send data:**

| Method    | Use Case                                        |
| --------- | ----------------------------------------------- |
| HTTP POST | Simple scripts, batch uploads, embedded devices |
| WebSocket | Real-time streaming and video (paid plans)      |

The Python SDK picks for you: it tries the WebSocket and falls back to HTTP on
its own. On the Free plan the gateway refuses the device WebSocket
(`streaming_requires_plan`), so the SDK sends everything over HTTP and
telemetry and events still land. Live streaming and video need a paid plan.
Free also allows up to 3 devices and 7 days of history.

## Quick Start

### Option 1: Web-Controlled Device (Recommended)

Set up your device with one command using an API key:

```bash
# With an API key (get one at app.plexus.company/api)
curl -sL https://app.plexus.company/setup | bash -s -- --key plx_your_api_key

```

Then find the device at [app.plexus.company/devices](https://app.plexus.company/devices).

### Option 2: Direct HTTP

Send data directly via HTTP:

```bash
curl -X POST https://gateway.plexus.company/ingest \
  -H "x-api-key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "points": [{
      "class": "metric",
      "metric": "temperature",
      "value": 72.5,
      "timestamp": 1699900000,
      "source_id": "sensor-001"
    }]
  }'
```

## Authentication

Plexus uses API keys for all authentication:

| Type    | Prefix | Use Case                                     |
| ------- | ------ | -------------------------------------------- |
| API Key | `plx_` | HTTP access and WebSocket device connections |

### Getting an API Key

**Option A: CLI setup (recommended for devices)**

1. Run `plexus init` on your device
2. Authorize the machine in the browser tab it opens
3. API key is saved to `~/.plexus/config.json`

**Option B: Manual creation**

1. Sign up at [app.plexus.company](https://app.plexus.company)
2. Go to [app.plexus.company/api](https://app.plexus.company/api)
3. Create an API key (starts with `plx_`)

**Limit to device slug** (optional, on the same form) binds the key to one
`source_id`. The gateway then refuses it for any other source: `403` on
`/ingest`, and a `source_not_allowed` error on the WebSocket. Use a bound key
on every device you put in a customer's hands, so a key pulled off one unit
cannot write as the rest of your fleet. Leave it empty for a fleet key that can
write as any source in your org.

## HTTP API

### Authentication

All requests require an API key in the header:

```
x-api-key: plx_xxxxx
```

### Send Data

**POST** `https://gateway.plexus.company/ingest`

```json
{
  "points": [
    {
      "class": "metric",
      "metric": "temperature",
      "value": 72.5,
      "timestamp": 1699900000.123,
      "source_id": "sensor-001",
      "tags": { "location": "lab" }
    }
  ]
}
```

| Field        | Type   | Required | Description                                                                                                                                                                                             |
| ------------ | ------ | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `class`      | string | WS only  | `"metric"` (numeric value) or `"event"` (anything else). Set it explicitly. Over HTTP a missing `class` is inferred from the value; over WebSocket it is required.                                        |
| `metric`     | string | Yes      | Metric name (e.g., `temperature`, `motor.rpm`)                                                                                                                                                          |
| `value`      | any    | Yes      | See supported value types below                                                                                                                                                                         |
| `timestamp`  | float  | No       | Unix timestamp in seconds (or ms if ≥ 1e12). Omit to use device time. Over WebSocket, the Python SDK applies a server-synced clock correction when omitted — see [Clock correction](#clock-correction). |
| `source_id`  | string | Yes      | Your source identifier                                                                                                                                                                                  |
| `tags`       | object | No       | Key-value labels                                                                                                                                                                                        |

### Supported Value Types

| Type    | Example                          | Use Case                         |
| ------- | -------------------------------- | -------------------------------- |
| number  | `72.5`, `-40`, `3.14159`         | Numeric readings (most common)   |
| string  | `"error"`, `"idle"`, `"running"` | Status, state, labels            |
| boolean | `true`, `false`                  | On/off, enabled/disabled         |
| object  | `{"x": 1.2, "y": 3.4, "z": 5.6}` | Vector data, structured readings |
| array   | `[1.0, 2.0, 3.0, 4.0]`           | Waveforms, multiple values       |

### Events and logs

Things that happen, rather than things you measure, are `class: "event"`
points: faults, state changes, operator actions, log lines. The Python SDK
sends them with `px.event(name, data)`.

Limits per event point: a string value up to 256 bytes, an object or array
value up to 4,096 bytes of JSON, and up to 16 tags. The gateway rejects larger
points with a 400.

There is no log-file upload and no `logging.Handler`. Forward the log lines
that matter as events:

```python
px.event("log", {"level": "error", "msg": "IMU read timed out"})
```

Send errors, warnings and state changes, not every debug line: each call is
one message, and the gateway limits messages (see
[Rate limits and batching](#rate-limits-and-batching)).

### Runs

A **run** is a named time window on a source — a hot-fire, a bench sequence, a
flight. Runs are recalled on `/runs`, compared against each other aligned at
T+0, and evaluated against declared pass criteria when they close.

The SDK opens and closes them, so the software driving the bench owns the
window rather than someone remembering to drag a time-range picker afterwards:

```python
with px.run("hotfire-03", pass_criteria=[
    {"metric": "motor.temp_c", "operator": "<", "value": 85, "label": "motor stays cool"},
    {"metric": "frames.dropped", "operator": "=", "value": 0},
]) as run:
    bench.execute()

print(run["id"])
```

Leaving the block closes the run as `completed`; an exception closes it as
`aborted` and re-raises. For a bench that starts and stops from different
places, call the two halves directly:

```python
run = px.start_run("hotfire-03", tags={"build": "a41f"})
...
result = px.end_run(run)             # or px.end_run(run, status="aborted")
print(result["test_result"])          # {"passed": bool, "criteria_results": [...]}
```

`start_run()` defaults `source_id` to the client's source; pass
`source_id=None` for an org-wide run. Underneath these are `POST /api/runs`
and `PATCH /api/runs/{id}` on the app API (not the gateway), authenticated
with the same `x-api-key`.

**Pass criteria.** Each is `{metric, operator, value, label?}` with operator
one of `> >= < <= = !=`. On close, every point for that metric inside the run
window is checked; the criterion passes only if all of them satisfy it, and a
criterion whose metric has no data in the window fails. The verdict is stored
on the run's `test_result` and shown on `/runs`.

To group data without a window, plain `tags` on each point still work.

## WebSocket API

For real-time streaming and video, devices connect via WebSocket. This needs a
paid plan: on Free, the gateway answers `device_auth` with an error frame
(`"code": "streaming_requires_plan"`) and closes. Send over HTTP instead; the
Python SDK does this for you.

Video frames go over this socket. They are relayed live to anyone watching and
stored only when someone presses **Record** in the app, for up to 4 hours per
recording.

### Connection Flow

1. Device connects to the gateway
2. Device authenticates with API key (and advertises any registered commands)
3. Device streams `telemetry` frames
4. Plexus invokes registered commands via `typed_command` (see the note under Commands)

### Device Authentication

Devices authenticate using an API key. The gateway echoes the declared `source_id` back unchanged — there is no auto-suffixing, so pick a unique `source_id` per device.

```json
// Device → Server
{
  "type": "device_auth",
  "api_key": "plx_xxxxx",
  "source_id": "drone-01",
  "platform": "python-sdk",
  "agent_version": "0.8.0"
}

// Server → Device
{
  "type": "authenticated",
  "source_id": "drone-01",
  "server_time_ms": 1746100800000
}
```

`server_time_ms` is the gateway's current Unix time in milliseconds. The Python SDK uses it to compute a clock offset (`server_time - device_time`) that is applied to every SDK-generated timestamp for the lifetime of the connection. This corrects for devices that boot without NTP or have an unreliable RTC — a common condition on embedded Linux. See [Clock correction](#clock-correction) for details and limitations.

> **Removed:** `install_id` and server-side `source_id` auto-suffixing were removed in 0.7.1. The device_auth frame no longer carries an `install_id`, and the client does not adopt or persist a server-assigned name.

### Frame Types

**Device → Server**

| Type             | Description                                     |
| ---------------- | ----------------------------------------------- |
| `device_auth`    | Authenticate on connect (see above)             |
| `telemetry`      | Sensor data points                              |
| `heartbeat`      | Liveness ping (every 30s)                       |
| `command_result` | `ack` / `result` / `error` for a typed command  |

**Server → Device**

| Type            | Description                                            |
| --------------- | ----------------------------------------------------- |
| `authenticated` | Auth accepted; carries `server_time_ms`               |
| `typed_command` | Invoke a command the device registered via `on_command` |

### Telemetry

```json
// Device → Server (streamed continuously)
{
  "type": "telemetry",
  "points": [
    { "class": "metric", "metric": "accel_x", "value": 0.12, "timestamp": 1699900000123 },
    { "class": "metric", "metric": "accel_y", "value": 0.05, "timestamp": 1699900000123 },
    { "class": "metric", "metric": "accel_z", "value": 9.81, "timestamp": 1699900000123 }
  ]
}
```

### Commands

Commands reach the device as a single `typed_command` envelope; the device replies with `command_result` frames. Register handlers with `px.on_command(...)` before the first `send()`.

> **Note:** the public API route for sending commands (`POST /v1/sources/{id}/commands`) was turned off on 2026-09-21 and returns `410 Gone`. Nothing in Plexus can currently trigger a custom handler.

```json
// Server → Device
{ "type": "typed_command", "id": "cmd-1", "command": "reboot", "params": { "delay_s": 0 } }

// Device → Server (ack, then result or error)
{ "type": "command_result", "id": "cmd-1", "command": "reboot", "event": "ack" }
{ "type": "command_result", "id": "cmd-1", "command": "reboot", "event": "result", "result": { "ok": true } }
```

> **Removed / not built.** There are no raw `start_stream`, `stop_stream`, `start_session`, `stop_session`, `configure`, `session_started`, or `session_stopped` frames — the earlier agent-style streaming/recording protocol was removed. Dashboard-driven control now flows through the `typed_command` envelope above.

## Code Examples

### Python (Direct HTTP)

```python
import requests
import time

requests.post(
    "https://gateway.plexus.company/ingest",
    headers={"x-api-key": "plx_xxxxx"},
    json={
        "points": [{
            "class": "metric",
            "metric": "temperature",
            "value": 72.5,
            "timestamp": time.time(),
            "source_id": "sensor-001"
        }]
    }
)
```

### JavaScript

```javascript
await fetch("https://gateway.plexus.company/ingest", {
  method: "POST",
  headers: {
    "x-api-key": "plx_xxxxx",
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    points: [
      {
        class: "metric",
        metric: "temperature",
        value: 72.5,
        timestamp: Date.now() / 1000,
        source_id: "sensor-001",
      },
    ],
  }),
});
```

### Go

```go
package main

import (
    "bytes"
    "encoding/json"
    "net/http"
    "time"
)

func main() {
    points := map[string]interface{}{
        "points": []map[string]interface{}{{
            "class":     "metric",
            "metric":    "temperature",
            "value":     72.5,
            "timestamp": float64(time.Now().Unix()),
            "source_id": "sensor-001",
        }},
    }

    body, _ := json.Marshal(points)
    req, _ := http.NewRequest("POST", "https://gateway.plexus.company/ingest", bytes.NewBuffer(body))
    req.Header.Set("x-api-key", "plx_xxxxx")
    req.Header.Set("Content-Type", "application/json")

    http.DefaultClient.Do(req)
}
```

### Arduino / ESP32

```cpp
#include <WiFi.h>
#include <HTTPClient.h>

// Call configTime(0, 0, "pool.ntp.org") in setup() before sending.
// time(nullptr) returns 0 until NTP sync completes — omit the timestamp
// field entirely if you cannot guarantee NTP sync at send time.
void sendToPlexus(const char* metric, float value) {
    HTTPClient http;
    http.begin("https://gateway.plexus.company/ingest");
    http.addHeader("Content-Type", "application/json");
    http.addHeader("x-api-key", "plx_xxxxx");

    String payload = "{\"points\":[{";
    payload += "\"class\":\"metric\",";
    payload += "\"metric\":\"" + String(metric) + "\",";
    payload += "\"value\":" + String(value) + ",";
    payload += "\"timestamp\":" + String(time(nullptr)) + ",";
    payload += "\"source_id\":\"esp32-001\"";
    payload += "}]}";

    http.POST(payload);
    http.end();
}
```

### Bash

```bash
#!/bin/bash
API_KEY="plx_xxxxx"
SOURCE_ID="sensor-001"

curl -X POST https://gateway.plexus.company/ingest \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"points\": [{
      \"class\": \"metric\",
      \"metric\": \"temperature\",
      \"value\": 72.5,
      \"timestamp\": $(date +%s),
      \"source_id\": \"$SOURCE_ID\"
    }]
  }"
```

## Bring Your Own Protocol

For MAVLink, CAN, MQTT, Modbus, OPC-UA, BLE, Serial, or any other protocol, use the upstream Python library directly and pass values to `px.send()`. Plexus stays out of your decode path:

```python
# CAN example — using python-can directly
import can
from plexus import Plexus

px = Plexus(api_key="plx_xxx", source_id="vehicle-001")
bus = can.interface.Bus(channel="can0", bustype="socketcan")

for msg in bus:
    px.send(f"can.0x{msg.arbitration_id:x}", int.from_bytes(msg.data, "big"))
```

```python
# MAVLink example — using pymavlink directly
from pymavlink import mavutil
from plexus import Plexus

px = Plexus(api_key="plx_xxx", source_id="drone-001")
conn = mavutil.mavlink_connection("udpin:0.0.0.0:14550")

while True:
    msg = conn.recv_match(blocking=True)
    if msg.get_type() == "ATTITUDE":
        px.send("attitude.roll", msg.roll)
        px.send("attitude.pitch", msg.pitch)
```

## Python SDK with Sensor Drivers

> **Removed / not built.** There is no `plexus-python[sensors]` extra and no `plexus.sensors` module (`BaseSensor` / `SensorReading` do not exist). The only optional extras are `[video]` and `[dev]`. This SDK stays out of your decode path — read your sensor with whatever library you already use and pass values to `px.send()` (see [Bring Your Own Protocol](#bring-your-own-protocol) above).

## Errors

| Status | Meaning                         |
| ------ | ------------------------------- |
| 200    | Success                         |
| 400    | Bad request (check JSON format) |
| 401    | Invalid or missing API key      |
| 403    | API key lacks permissions, or is limited to another device |
| 404    | Resource not found              |
| 410    | Resource expired                |

## Rate limits and batching

The gateway meters **messages**, not points. One `px.send()` call is one
message, whatever it carries. `send()` does not batch:

| Limit                              | Value            |
| ---------------------------------- | ---------------- |
| Telemetry messages per WS connection | 500/s sustained, 2000 burst |
| Hard ceiling per source (WS + HTTP) | 2000 messages/s  |
| Points per message                 | 10,000           |
| Message size                       | 1 MB             |

Because the ceiling counts messages, the shape of your sends decides whether
you hit it. Eight channels at 100 Hz sent one at a time is 800 messages/s —
over the limit. The same 800 readings/s batched every 100 ms is 10 messages/s,
and the batches are also several times cheaper to store.

**Over the limit, the gateway discards the whole message.** It replies with a
`RATE_LIMITED` error frame, but that arrives after `send()` has already
returned — those points are gone and cannot be resent. The SDK counts the
notices (`px.rate_limited_frames`) and raises `RateLimitedError` on the next
send so the loss cannot pass unnoticed, but the only real fix is to send
fewer, larger messages.

Use `px.batch()` for anything above a few readings per second:

```python
with px.batch(interval_ms=50) as b:
    while running:
        b.send("att.pos_x", att.x)
        b.send("att.rate_x", gyro.x)
        b.send("frames.captured", grabber.count)
```

`b.send()` takes the same arguments as `px.send()` and queues the reading; a
background thread flushes the queue on the interval, and leaving the block
flushes what is left. A failed flush leaves the points in the local
store-and-forward buffer, so nothing is dropped for a transient outage — the
one bounded exception is `max_pending` (default 200,000 points), reached only
when the gateway has been unreachable for a long time, and counted on
`BatchSender.dropped`.

If you are already accumulating readings yourself, `px.send_batch([...])`
sends a list in one message without the background thread.

## Clock correction

Embedded devices commonly boot with a wrong system clock — no hardware RTC, NTP unreachable on first boot, or a fresh OS image whose filesystem timestamp is months in the past. Without correction, all telemetry lands at the wrong place on the timeline.

The Python SDK corrects for this automatically over WebSocket. On every connection the gateway returns `server_time_ms` in the `authenticated` frame. The SDK computes `offset = server_time - device_time` and adds it to every timestamp it generates. Data lands at the right time on the dashboard regardless of what the device clock says.

**When the correction applies:**

The offset is applied when `timestamp` is omitted (the SDK generates the time). If you pass an explicit `timestamp`, it is used as-is — the SDK cannot tell whether your value is a wall-clock time or a hardware-relative counter, so it leaves it alone.

```python
px.send("temperature", 72.5)                    # SDK picks time → correction applied
px.send("temperature", 72.5, timestamp=t)        # your timestamp → used as-is, no correction
```

**When to pass an explicit timestamp:**

- You have a reliable wall-clock source (GPS, trusted hardware RTC, host NTP)
- You are replaying or backfilling historical data
- Your sensor provides its own wall-clock timestamp

**When to omit timestamp:**

- The device may have booted without NTP (Raspberry Pi, Jetson, field robots without network on first boot)
- You have no reliable external time source

**Known limitations:**

- The clock offset refreshes only on WebSocket reconnect. A device with a drifting RTC that stays connected for many days will accumulate uncorrected drift between reconnects proportional to the drift rate.
- The HTTP fallback path (used when the WebSocket is unavailable) does not receive clock sync — timestamps default to the device clock uncorrected.
- `send_batch()` takes one shared `timestamp` by default; pass `(metric, value, timestamp)` 3-tuples for per-point timestamps.

## Best Practices

- **Batch above a few readings per second** - `px.batch()` coalesces into one message per interval; the gateway's ceiling counts messages, not points (see [Rate limits and batching](#rate-limits-and-batching))
- **Omit timestamp when unsure** - The Python SDK applies server-synced clock correction when `timestamp` is omitted over WebSocket; only pass an explicit timestamp when you have a reliable wall-clock source
- **Consistent source_id** - Use the same ID for each physical device/source
- **Use tags** - Label data for filtering and grouping (e.g., `{"location": "lab"}`)
- **Let the SDK pick the transport** - It uses the WebSocket when your plan allows it and falls back to HTTP otherwise
