# AGENTS.md — plexus-python

Machine-readable interface for AI assistants and automation scripts.

## Environment Variables

| Variable                | Description                           | Default                          |
| ----------------------- | ------------------------------------- | -------------------------------- |
| `PLEXUS_API_KEY`        | API key for authentication (required) | none                             |
| `PLEXUS_GATEWAY_URL`    | Gateway HTTP ingest URL               | `https://gateway.plexus.company` |
| `PLEXUS_GATEWAY_WS_URL` | Gateway WebSocket URL                 | `wss://gateway.plexus.company`   |

## CLI Commands

```bash
plexus start                           # Stream with PLEXUS_API_KEY env var
plexus start --key plx_xxxxx           # Pass key inline
plexus start --device-id my-device     # Set device identifier
plexus reset                           # Clear config
```

An API key is required — set `PLEXUS_API_KEY` or pass `--key`. There is no interactive signup; get a key at app.plexus.company/devices.

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

# Batch
px.send_batch([
    ("temperature", 72.5),
    ("pressure", 1013.25),
])

# Persistent buffering for reliability
px = Plexus(api_key="plx_xxxxx", persistent_buffer=True)
```

## Key Conventions

- Config lives in `~/.plexus/config.json`
- API keys are prefixed with `plx_`
- Source IDs (device slugs) namespace metrics
- HTTP ingest → `POST /ingest` on gateway; WebSocket → `/ws/device` for streaming + commands
- Gateway resolves `org_id` server-side from the API key — clients do not supply it
