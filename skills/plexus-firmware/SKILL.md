---
name: plexus-firmware
description: Add Plexus telemetry ingestion to firmware, edge gateways, or device-side code (Python, C/C++, MicroPython, Rust, Go). Use when the user is writing code that runs on a device — ESP32, Arduino, RP2040, Raspberry Pi, NVIDIA Jetson, drone autopilot, satellite OBC, embedded Linux gateway — and needs to send sensor data, metrics, or telemetry up to a server. Triggering phrases include "send sensor readings to the cloud", "ingest from my ESP32", "report battery voltage from the device", "edge gateway ingestion", "telemetry from firmware", "log readings from a Pi", or "ship metrics from MicroPython" — even if "Plexus" isn't said, as long as the destination is the Plexus gateway. Includes batching, exponential backoff, threading guidance, and memory-safe templates.
tools: Read, Write, Edit, Bash, WebFetch
---

# Plexus Firmware

Adds a small, well-behaved Plexus ingest client to device-side code. Optimized for constrained environments: limited memory, intermittent connectivity, no fancy SDKs.

## When to use this skill

- The user is writing code that runs **on a device**, not on a server
- Targets include: ESP32 / Arduino / RP2040, Raspberry Pi, NVIDIA Jetson, embedded Linux gateways, drone autopilot companion computers, satellite OBC software
- Languages: C / C++, MicroPython, CPython, Rust, Go (for edge gateways)

**If the target runs CPython (Pi, Jetson, any embedded Linux with room), stop and use the SDK instead** — `pip install plexus-python`, then `Plexus(api_key=..., source_id=...)`. It handles backoff, store-and-forward buffering and the WebSocket transport. Hand-rolling is for targets the SDK can't reach.

**`send()` does NOT batch.** Every call is its own message on the wire, and the ceiling below counts messages. Above a few readings a second, use `px.batch()`:

```python
with px.batch(interval_ms=50) as b:      # one message per interval
    while running:
        b.send("imu.accel_x", imu.x)     # same signature as px.send()
        b.send("imu.accel_y", imu.y)
```

Leaving the block flushes what is queued, and readings keep the timestamp they were taken at. Requires plexus-python >= 0.11.0.

For **server-side ingestion** (a backend collecting metrics and forwarding), the generic `plexus` skill is fine.
For **dashboards / UI**, use `plexus-dashboard`.

**On a test bench, open a run.** Software driving a test should name its window
rather than leave someone to find it on a chart later: `POST /api/runs` when the
test starts, `PATCH /api/runs/{id}` with `status` and `ended_at` when it ends
(app host, same `x-api-key`), or `with px.run(...)` from plexus-python. Declared
`pass_criteria` are evaluated against every sample in the window on close. See
the `plexus` skill for the contract.

## The ingest contract

```
POST https://gateway.plexus.company/ingest
Headers:
  x-api-key:    plx_...
  Content-Type: application/json
Body:
  {
    "source_id": "drone-001",
    "points": [
      { "class": "metric", "metric": "battery.voltage", "value": 11.8, "timestamp": 1787848320000 },
      { "class": "metric", "metric": "altitude_m",      "value": 142.5 }
    ]
  }
Response:
  { "success": true, "count": 2, "source_id": "drone-001" }
```

Four things the gateway will reject you for. Get these right or nothing lands:

1. **The array is `points`.** `"metrics": [...]` returns `400 {"error":"'points' array is required"}`.
2. **Every point needs `class`**, either `"metric"` or `"event"`. There is no default.
3. **`timestamp` is a number, never a string.** An ISO-8601 string returns `points[i].timestamp must be a number`. Epoch **milliseconds**; a positive value below `1e12` is read as **seconds** and scaled for you, so either unit works as long as it's numeric.
4. **`source_id` must match `^[a-z0-9][a-z0-9._-]*$` (max 256 chars)** and is not deduplicated — two devices declaring the same id merge into one source. This is what SD-card clones do when they all boot as `raspberrypi`.

`timestamp` is optional. Omit it and the gateway stamps the point with its receive time — which is the right move on a device whose clock has never been NTP-synced. Per-point `tags` (a flat string→string map) are supported and optional.

## Non-negotiables

Every firmware integration must do these. Skip any of them and you'll lose data or melt batteries.

### 1. Batch

**The gateway meters messages, not points.** One request carrying 200 points costs
exactly what one carrying 1 costs, so the shape of your sends — not their volume —
decides whether you hit a limit:

| Limit | Value |
| --- | --- |
| Telemetry messages per WebSocket connection | 500/s sustained, 2000 burst |
| Hard ceiling per source (WS and HTTP) | 2000 messages/s |
| Points per message | 10,000 |
| Body size | 1 MB (WS) / 5 MB (HTTP) |

Eight channels at 100 Hz sent one at a time is 800 messages/s — over the limit,
and **the overflow is discarded**. Batched every 50 ms it is 20 messages/s.

Over the limit the gateway drops the whole message and answers `RATE_LIMITED`,
which arrives *after* the send returned. Those points cannot be recovered, so
this is a batching problem, not a retry one.

Never POST one point at a time. Buffer until **64 points OR 5 seconds**, whichever
first, then flush. Constants worth exposing:

```c
#define PLEXUS_BATCH_SIZE   64
#define PLEXUS_BATCH_MS   5000
```

### 2. Retry with backoff

On 429 (rate limit) or 5xx (server), retry with exponential backoff:

- Attempt 1: immediate
- Attempt 2: +1s
- Attempt 3: +4s
- Then drop the batch (don't queue forever — memory)

On 401/403, **don't retry** — the key is bad. Log loudly and stop.
On network errors, queue if memory allows; otherwise drop oldest.

### 3. Stable source_id

The `source_id` must be stable across reboots **and unique across the fleet**. Use a hardware identifier (MAC address suffix, board serial), not a random UUID generated at boot and not the hostname.

### 4. Read the key from somewhere safe

- ESP32 / Arduino: NVS / preferences storage, **not** hardcoded in firmware
- Raspberry Pi / Linux: `/etc/plexus/key` with mode 0600, or env var
- Never compile keys into a binary that ships to multiple devices — one leak compromises the fleet

### 5. Bound memory

On constrained devices (ESP32, RP2040), the JSON body for a 64-point batch is ~3–5 KB. Pre-allocate a static buffer at boot and bail if it'd overflow — never malloc per-batch on a 320KB-RAM target.

## Templates

### MicroPython (ESP32)

**Important: flush is blocking** — `urequests.post` and the retry sleeps can stall the calling task for 5+ seconds on bad WiFi. Run the flush loop on its own thread so sensor reads never wait on the network.

```python
import urequests, time, machine, _thread

API_KEY   = "plx_..."  # load from NVS in production
SOURCE_ID = "esp-{:012x}".format(int.from_bytes(machine.unique_id(), "big"))
URL       = "https://gateway.plexus.company/ingest"

_buf  = []
_lock = _thread.allocate_lock()

def emit(metric, value):
    """Called from sensor-read context. Cheap — just appends to a buffer."""
    with _lock:
        # class is REQUIRED. timestamp must be a NUMBER (epoch ms) — omit it
        # entirely if this board has never NTP-synced and the gateway will
        # stamp it on receive.
        _buf.append({
            "class": "metric",
            "metric": metric,
            "value": value,
            "timestamp": time.time() * 1000,
        })

def _flush_once():
    with _lock:
        if not _buf:
            return
        # The array is "points", NOT "metrics".
        body = {"source_id": SOURCE_ID, "points": _buf[:64]}
        del _buf[:64]
    for delay in (0, 1, 4):
        time.sleep(delay)
        try:
            r = urequests.post(URL, json=body, headers={"x-api-key": API_KEY})
            try:
                if 200 <= r.status_code < 300:
                    return                       # {"success":true,"count":N,...}
                if r.status_code in (401, 403):
                    return                       # bad key, give up
                if r.status_code == 400:
                    print("plexus rejected batch:", r.text)
                    return                       # malformed — retrying won't help
            finally:
                r.close()
        except Exception:
            pass
    # gave up — batch dropped on the floor (intentional; bounded memory)

def _flush_loop():
    """Runs forever on its own thread. Flushes every 5s or whenever buf >= 64."""
    while True:
        if len(_buf) >= 64:
            _flush_once()
        else:
            time.sleep(5)
            _flush_once()

def start():
    """Call once at boot, after WiFi is up."""
    _thread.start_new_thread(_flush_loop, ())
```

Usage: call `start()` once at boot, then `emit("battery.voltage", 11.8)` from anywhere — sensor reads, ISRs (via a small queue), main loop. The network IO never blocks the caller.

Note `time.time()` on MicroPython is epoch-2000 on some ports. If the value looks wrong, omit `timestamp` and let the gateway stamp it rather than shipping a bad clock.

### CPython (Raspberry Pi / Jetson)

Use the SDK — `pip install plexus-python` — unless there's a reason not to:

```python
from plexus import Plexus

px = Plexus(api_key=os.environ["PLEXUS_API_KEY"], source_id="pi-fieldunit-03")
px.send("battery.voltage", 11.8)          # class=metric, batched for you
px.event("fault", "undervoltage lockout")  # class=event
```

If you must hand-roll: `requests` with a `Session` for connection pooling, same batching + backoff rules, key from `os.environ["PLEXUS_API_KEY"]`, flush on a background thread on a 5-second tick.

### C (ESP-IDF / generic embedded)

- Use `esp_http_client` (ESP-IDF) or libcurl (Linux)
- Allocate one static `char body_buf[8192]` at boot
- Format JSON with a tiny printf-style writer (or cJSON if RAM allows) — remember `"class"` on every point
- TLS root cert: pin or use the system bundle
- Don't block the main task — push to a queue, drain from a worker

Ask the user which RTOS / framework before generating C — the boilerplate differs a lot.

### Rust (embedded or edge)

`reqwest` for std environments, `embedded-svc` + `esp-idf-svc` for ESP32. Use `serde_json` for body construction. Same retry shape with `tokio::time::sleep` or blocking sleeps.

## What NOT to do

- **Don't** POST per-point. You'll burn battery, hit rate limits, and pay more.
- **Don't** retry 4xx errors (except 429). They won't get better — and a 400 means your body shape is wrong, so print the response instead of retrying it.
- **Don't** queue forever on disconnect — bound the buffer, drop oldest, log the loss.
- **Don't** include unbounded labels in `tags`. They cardinality-explode the database. Keep tags to slow-changing strings (firmware version, region, hardware revision).
- **Don't** send an ISO-8601 timestamp string. It is a hard 400. Numbers only.
- **Don't** derive `source_id` from the hostname. Cloned images all report as the same device.

## Testing without a device

Have the user run:

```bash
curl -X POST https://gateway.plexus.company/ingest \
  -H "x-api-key: $PLEXUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"source_id":"test-laptop","points":[{"class":"metric","metric":"cpu_temp","value":45.2}]}'
```

If they see `{"success":true,"count":1,"source_id":"test-laptop"}`, auth + connectivity work. Then port the same body shape into firmware. Confirm it landed:

```bash
curl -H "x-api-key: $PLEXUS_API_KEY" \
  https://plexus-data-api.fly.dev/v1/sources/test-laptop/metrics/latest
```

## When unsure

The ingest contract above is the authority for the device side. For reading data back, fetch `https://plexus-data-api.fly.dev/openapi.json` or use the generic `plexus` skill.

Corrected 2026-08-28 against gateway source (`ingest.go`, `validate.go`): the array is `points` not `metrics`, `class` is required, timestamps must be numeric, and the response is `{success, count, source_id}`. Every template in the previous version of this file would have 400'd.
