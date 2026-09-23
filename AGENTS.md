# AGENTS.md — plexus-python

Machine-readable interface for AI assistants and automation scripts.

## Environment Variables

| Variable                | Description                                     | Default                          |
| ----------------------- | ----------------------------------------------- | -------------------------------- |
| `PLEXUS_API_KEY`        | API key for authentication (required)           | none                             |
| `PLEXUS_GATEWAY_URL`    | Gateway HTTP ingest URL                         | `https://gateway.plexus.company` |
| `PLEXUS_GATEWAY_WS_URL` | Gateway WebSocket URL                           | `wss://gateway.plexus.company`   |
| `PLEXUS_ENDPOINT`       | Product app URL (runs, frame uploads, CLI auth) | `https://app.plexus.company`     |
| `PLEXUS_QUIET`          | Set `1`/`true`/`yes` to silence `[plexus]` stderr status lines | unset (status lines on) |

## CLI Commands

```bash
plexus init                            # Authorize this machine, save an API key locally (alias: login)
plexus init --name my-key              # Label for the issued key (default: cli-<hostname>)
plexus init --force                    # Overwrite an existing local key
plexus logout                          # Forget the local API key
plexus whoami                          # Show the local credential summary
```

`plexus init` opens a browser to `app.plexus.company/auth/cli`, waits for the callback, and
persists the issued key to `~/.plexus/config.json`. Alternatively, set `PLEXUS_API_KEY` (or pass
`api_key=` to `Plexus()`) instead of running `init`; get a key at app.plexus.company/api.

## Exit Codes

| Code  | Meaning                               |
| ----- | ------------------------------------- |
| `0`   | Clean shutdown                        |
| `1`   | Configuration or authentication error |
| `130` | Interrupted (SIGINT / Ctrl+C)         |

## Python SDK

```python
from plexus import Plexus

px = Plexus(api_key="plx_xxxxx", source_id="device-001")
px.send("temperature", 72.5)
px.send("pressure", 1013.25, tags={"unit": "hPa"})

# One message for a list of readings
px.send_batch([
    ("temperature", 72.5),
    ("pressure", 1013.25),
])

# send() is one message per call and does not batch. Above a few readings
# a second, let a background thread group them:
with px.batch(interval_ms=50) as b:
    b.send("temperature", 72.5)

# Events (faults, state changes, log lines). No log upload, no logging.Handler.
px.event("log", {"level": "error", "msg": "IMU read timed out"})

# The on-disk (SQLite) buffer is on by default; this keeps it in memory only
px = Plexus(api_key="plx_xxxxx", persistent_buffer=False)
```

## Key Conventions

- Config lives in `~/.plexus/config.json`
- API keys are prefixed with `plx_`
- Source IDs (device slugs) namespace metrics
- HTTP ingest → `POST /ingest` on gateway; WebSocket → `/ws/device` for streaming (paid plans; the SDK falls back to HTTP on Free)
- Gateway resolves `org_id` server-side from the API key — clients do not supply it
