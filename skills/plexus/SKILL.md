---
name: plexus
description: Integrate with the Plexus telemetry API — send data, query metrics, subscribe to live streams, send commands to devices. Use when the user mentions Plexus, plexus.company, gateway.plexus.company, plexus-data-api.fly.dev, or plx_ API keys. ALSO USE when the request involves IoT/hardware telemetry, fleet observability, sending sensor data to a backend, querying device time-series, building a fleet dashboard, monitoring drones/satellites/robots/edge devices, or any phrase like "send my sensor readings somewhere", "store telemetry", "track a fleet", "ingest metrics", or "device observability" — even if "Plexus" is never said.
tools: Read, Write, Edit, Bash, WebFetch
---

# Plexus

Plexus is a telemetry/observability platform for hardware fleets (drones, satellites, robots, edge devices). This skill teaches Claude how to integrate with its public API.

## When to use this skill

Trigger on any of:

- The user mentions "Plexus", "plexus.company", `plx_` keys, `gateway.plexus.company`, or `plexus-data-api.fly.dev`
- The user wants to ingest telemetry, query device metrics, stream live points, or send commands to a device
- The user is building a dashboard, alert pipeline, or analysis on top of fleet telemetry
- The user pastes a Plexus curl example and asks for help

If the user wants to build a **frontend dashboard** specifically, also invoke `plexus-dashboard`.
If they're writing **firmware / edge code**, also invoke `plexus-firmware`.

## Hosts and auth

Two base URLs. Authenticate with an `x-api-key` header on HTTP.

| Purpose                                            | Host                             |
| -------------------------------------------------- | -------------------------------- |
| Ingest                                              | `https://gateway.plexus.company` |
| Read API (sources, metrics, logs, fleet, commands, live stream) | `https://plexus-data-api.fly.dev` |

```
x-api-key: plx_...
```

The OpenAPI spec for the read API lives at `https://plexus-data-api.fly.dev/openapi.json` — fetch it when you need exact schemas. **It does not list the WebSocket endpoints** (FastAPI omits them), so use the Live section below for those.

Read keys from env, never hardcode:

- `PLEXUS_API_KEY` — server / CLI
- `NEXT_PUBLIC_PLEXUS_API_KEY` — client-side Next.js (only if the user explicitly accepts the trade-off)

## Endpoint cheat sheet

### Send (gateway)

`POST /ingest` — body `{ source_id, points: [...] }`, response `{ success, count, source_id }`.

```json
{
  "source_id": "drone-001",
  "points": [
    { "class": "metric", "metric": "battery.voltage", "value": 11.8, "timestamp": 1787848320000 }
  ]
}
```

Per point:

- **`class` is required** and must be `"metric"` or `"event"`. Omitting it is a 400.
- **The array is `points`, not `metrics`.** Sending `metrics: [...]` returns `400 {"error":"'points' array is required"}`. This is the single most common mistake.
- `metric` (string) and `value` are required.
- `timestamp` must be a **number** — an ISO-8601 string is rejected with `points[i].timestamp must be a number`. Epoch **milliseconds** is the intended unit; a positive value under `1e12` is interpreted as **seconds** and scaled up automatically.
- Omitting `timestamp` is safe: the point is stamped with gateway receive time. (This used to land at 1970 and be invisible — fixed, the loader now falls back to `ingested_at`.)
- `source_id` may be set at the envelope level, per point, or both — per point wins.
- The gateway creates the source on first write; no registration step.

`POST /api/v1/write` also exists as a Prometheus/Alloy/OTel/Telegraf remote-write receiver. Do not reach for it unless the user already runs one of those.

### Read (data API)

Paths are `/v1/sources/...`. `/v1/devices/...` is a deprecated alias — bare `/v1/devices` 307s to `/v1/devices/`, which 308s to `/v1/sources/`. Both preserve method and body, but just use `sources` (and `curl -L` if you inherit an old path).

- `GET /v1/sources` → `{ devices: [{ source_id, online, last_seen_ms }] }`
  - Takes `?status=online|offline`.
  - Note the mismatch: the **path** is `sources`, the **response key** is still `devices`. Renaming stopped halfway. Read `body.devices`.
- `GET /v1/sources/{id}` → `{ source_id, online, last_seen_ms }`
- `GET /v1/sources/{id}/metrics` → `string[]` (metric names)
- `GET /v1/sources/{id}/metrics/latest` → `{ metrics: { [name]: number } }`
- `GET /v1/sources/{id}/metrics/query?metrics=a,b&last=1h` → columnar, see below. Also takes `start`, `end`, `interval`.
- `GET /v1/sources/{id}/logs?last=1h&limit=1000` → log rows (also `tail`, `name`, `start`, `end`)
- `GET /v1/fleet/health` → `{ sources_total, sources_online }`
- `GET /v1/fleet/metrics?metric=X&last=1h` → `{ metric, interval, sources_online, sources_w_metric, sources: [...], truncated }`

There is **no per-source health endpoint** — `/v1/sources/{id}/health` 404s. Liveness is already on the list: each entry carries `online` and `last_seen_ms`. Use `/v1/fleet/health` for the roll-up.

#### The query response is columnar

Not an array of points. Each metric maps to parallel arrays:

```json
{
  "interval": "1m",
  "auto_downsampled": true,
  "truncated": false,
  "series": {
    "my.metric": {
      "timestamp_ms": [1787848320000, 1787848380000],
      "min": [46.5, -0.05],
      "max": [49.9, 49.9],
      "avg": [48.2, 26.6],
      "count": [4, 60]
    }
  }
}
```

So it is `series[m].timestamp_ms[i]` and `series[m].avg[i]`, not `series[m][i].t`. Zip the arrays by index to plot:

```js
const s = res.series[metric];
const points = s.timestamp_ms.map((t, i) => ({ t, v: s.avg[i] }));
```

`/v1/fleet/metrics` uses the same columnar shape, one entry per source under `sources[]`.

### Live (data API, not the gateway)

```
WS wss://plexus-data-api.fly.dev/v1/sources/{source_id}/metrics/stream?metrics=a,b
```

Also `/logs/stream` and `/video/stream` under the same source prefix.

**Auth is the first message, not a header.** Immediately after connect, send:

```json
{ "type": "auth", "api_key": "plx_..." }
```

Then just listen — there is no separate `subscribe` message, and the `?metrics=` query param does the filtering server-side. Because auth is a WS message rather than a header, a **browser can connect directly** — no backend relay needed for auth reasons alone (though you still shouldn't ship a key in public client code).

Frames you receive:

- `{"type":"telemetry","points":[ {class, metric, value, timestamp, ...}, ... ]}` — **batched**, an array per frame, same point shape as ingest. Iterate `points`.
- `{"type":"gateway_reconnecting","attempt":N,"delay_s":N}` — informational; the server is reconnecting upstream and will resume.

Close codes: `4401` unauthorized (bad or missing key, or no auth message within 10s), `4402` payment required (org access disabled).

You do **not** need to answer application-level pings on this endpoint; keepalive is handled at the protocol layer.

The gateway's own sockets (`/ws/device`, `/ws/browser`) are for the Python SDK and the Plexus app respectively. Don't write third-party clients against them. There is no `/v1/stream` on the gateway.

### Control (data API)

`POST /v1/sources/{id}/commands` — body `{ command, params? }`, response `{ queued: bool }`. Confirm with the user before sending — these hit physical hardware.

## Standard scaffolding

When asked to "set up Plexus" in a project, do this:

1. **Detect the language** from the project's manifest (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`). For Python, prefer the SDK: `pip install plexus-python`.
2. **Create one client module** under `src/lib/plexus.{ts,py,go}` (or the project's idiomatic location). One function per endpoint the user actually needs — don't scaffold the whole surface if they only want ingest.
3. **Throw a typed error class** (`PlexusError` with `status` + `message`) on non-2xx responses. Map 401 to "check your PLEXUS_API_KEY".
4. **Read the API key from env**, never hardcode. If the user is shipping a public client app, warn them about exposing keys and recommend a backend proxy.
5. **Add a single usage example** in the project — one obvious place, not five — so they can verify it works.
6. **Update README** with a short section: "Set `PLEXUS_API_KEY`, then run X."

## Idioms

- **Polling cadences for dashboards**: latest values 5s, charts 10s, fleet health 10s, source list 30s. Use SWR or TanStack Query with `refreshInterval`.
- **Time ranges**: prefer `last=1h` (relative) over `start`/`end` (absolute) — easier to reason about and less timezone footgun. `start`/`end` are ISO date-times, not epoch ms.
- **Batching ingest**: buffer up to 64 points or 5 seconds, whichever first. On 429 / 5xx, exponential backoff with max 3 attempts.
- **Source IDs are slugs**: `drone-001`, `sat-alpha-3`. Must match `^[a-z0-9][a-z0-9_-]{1,62}$`. Stable, lowercase, hyphenated. Don't use UUIDs in user-facing surfaces.
- **Source IDs are not deduplicated.** The gateway writes whatever `source_id` you declare. Two devices declaring the same name merge into one source.
- **Metric names are opaque and may be long.** Anything that round-trips a name must use the identical string on both sides or the series and its metadata will not join.

## Common pitfalls

- **`points`, not `metrics`, on ingest**, and every point needs `class`. The two most common 400s.
- **Numeric timestamps only.** ISO strings 400. Milliseconds unless the value is under `1e12`, in which case it's read as seconds.
- **The query response is columnar.** `series[m].avg[i]`, not `series[m][i].v`. Reaching for `.t`/`.v` yields `undefined` and an empty chart with no error.
- **The live stream is on the data API, and auths by first message.** There is no `/v1/stream` on the gateway.
- **Telemetry frames are batched** — `points` is an array. Handling one frame as one point silently drops data.
- `start`/`end` are **ISO date-times on every endpoint** that takes them — `query`, `logs` and `fleet/metrics` alike. `last=1h` is easier and works on all three.
- `auto_downsampled: true` in a query response means the bucket size was picked for you — surface it in the UI so users understand what they're looking at.
- Commands queue on the device; they don't execute synchronously. Don't promise the user "it rebooted" — promise "reboot queued".

## When unsure

Fetch `https://plexus-data-api.fly.dev/openapi.json` for the authoritative HTTP schema (it will not show WebSocket routes), and check a real response before writing parsing code. `scripts/verify_skills.py` in this repo checks these docs against the live spec — run it if something here looks stale.

This cheat sheet has drifted before. Corrected 2026-08-27 against Data API 0.1.0 (ingest array name, `sources`/`devices` paths, removed per-source health, columnar query) and again 2026-08-28 against gateway + API source (the live-stream host/path/auth/frame shape, the required `class` field, numeric-only timestamps, the redirect chain, and the now-fixed 1970 timestamp behavior).
