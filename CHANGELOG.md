# Changelog

## [0.7.1] - 2026-06-02 - Remove install_id / source_id auto-suffix

### Changed

- Removed `install_id` from the device auth frame and `WebSocketTransport`.
- Removed server-side source_id auto-suffix handling (`on_source_id_assigned` callback, `set_source_id` persistence).
- Removed `get_install_id` and `set_source_id` from `plexus.config`.

## [0.7.0] - 2026-05-29 - SDK hardening

### Fixed

- Buffer overflow now surfaces on stderr (`⚠ buffer full, dropped N oldest points`) instead
  of going silently to `logging.warning`. Both `MemoryBuffer` and `SqliteBuffer` are covered.
- `on_command()` late-registration warning now goes through `logging.warning()` so it is not
  suppressed by `PLEXUS_QUIET=1`.
- `PLEXUS_QUIET` env var is now read at call time rather than import time — setting it after
  module load now takes effect.
- `max_buffer_size` setter no longer directly mutates `_buffer._max_size`; routes through a
  new `resize()` method on `BufferBackend`.

### Changed

- `PlexusError`, `AuthenticationError`, and `RetryConfig` are now exported from the top-level
  `plexus` package. `from plexus import PlexusError` now works as expected.
- `WebSocketTransport` and `read_mjpeg_frames` removed from `__all__` — both remain importable
  via their source modules but are no longer part of the top-level public API.

### Breaking

- `Plexus.__init__` no longer accepts a `transport` parameter. WebSocket with automatic HTTP
  fallback is the only send path.
- `ThermalSource.open()` now requires an explicit hint (`"sim"`, `"mlx90640"`, `"mlx90641"`,
  `"usb"`, or a device index). Auto-detection has been removed — it could not reliably
  distinguish USB thermal cameras from regular webcams.

## [0.6.2] - 2026-05-28 - Dependency fix

### Fixed

- Replaced `opencv-python` with `opencv-python-headless` in the `[video]` extra —
  headless is the correct choice for embedded/server use and avoids pulling in Qt/GUI
  dependencies that don't belong on Raspberry Pi or headless Linux.
- Merged the `[thermal]` extra into `[video]`. OpenCV is needed for all video frame
  encoding, not just thermal cameras. Install with `pip install plexus-python[video]`.
- Synced `plexus.__version__` with `pyproject.toml` (both now `0.6.2`).

## [0.6.1] - 2026-05-28 - Thermal camera streaming

### Added

- `plexus.cameras.thermal` module with hardware-agnostic drivers for I2C sensors
  (MLX90640, MLX90641) and USB cameras (Y16 pixel format). All drivers return a
  unified `ThermalFrame` containing a colorized JPEG plus temperature metadata
  (min, max, mean °C).
- `ThermalSource.open()` auto-detects connected hardware; pass `"sim"` for a
  simulated source with no physical device required.
- `client.send_thermal_frame()` sends thermal frames over the existing WebSocket
  connection. Small sensors (≤4096 pixels, e.g. 32×24 MLX90640) include the full
  temperature array inline; larger USB sensors omit it to keep frame sizes manageable.
- `examples/thermal_camera.py` — end-to-end example for streaming a thermal camera.

## [0.5.2] - 2026-05-21 - DX hardening for hardware engineers

### Fixed

- Error messages now reference `plexus init` instead of the non-existent `plexus start`.
- `close()` now attempts to flush any buffered points before tearing down the transport,
  preventing silent data loss on graceful shutdown.
- `persistent_buffer` default changed from `False` to `True` — store-and-forward is now
  on by default, matching the `~/.plexus/config.json` default and the right choice for
  field hardware. Pass `persistent_buffer=False` to opt out (e.g. in test fixtures).

### Added

- `send_batch()` now accepts 3-tuples `(metric, value, timestamp)` alongside the existing
  2-tuple form. Per-point timestamps let sensors on different interrupt timers share a
  single batch call. 2-tuples continue to use the shared `timestamp` argument.
- `on_command()` now warns immediately via stderr if called after the WebSocket has already
  authenticated, making the "register before first send()" ordering requirement visible
  rather than silently broken.
- `source_id` is validated against `^[a-z0-9][a-z0-9_-]{1,62}$` at construction time.
  Invalid names now raise `ValueError` with a clear message instead of failing obscurely
  at the gateway.

### Changed

- `_say()` / `_QUIET` consolidated into a new internal `plexus/_log.py` module.
  Previously duplicated verbatim between `client.py` and `ws.py`.

## [0.5.1] - 2026-05-19 - Binary video frames + non-blocking send

### Performance

- `send_video_frame()` now sends a compact binary WebSocket frame instead of
  JSON+base64. The binary header encodes source_id, camera_id, width, height,
  and timestamp_ms; the JPEG payload follows raw. Eliminates the 33% base64
  wire overhead, reducing per-frame bandwidth by ~25% and raising the
  sustainable FPS ceiling from ~15–20 fps to ~20–25 fps at 1280×720 quality 85.
- Gateway decodes the binary header and re-encodes as JSON+base64 before
  relaying to browsers — no changes required in the frontend, data_api, or any
  other consumer.

### Reliability

- `send_video_frame()` is now non-blocking. Frames are placed into a
  `queue.Queue(maxsize=2)` drained by a dedicated `plexus-video` daemon thread.
  When the queue is full (sender backlogged) frames are dropped rather than
  blocking the capture pipeline, preventing deadlocks at any FPS.
- `stop()` / `close()` now exits cleanly within 0.5 s regardless of in-flight
  sends. Previously a slow or hung WebSocket write could stall shutdown
  indefinitely.

### Changed

- Removed `import base64` from `client.py` (no longer needed on the send path).
- `send_video_frame()` calls `ws.send_video_frame_async()` instead of the
  internal `ws._send_frame()`.

### Wire protocol

- Gateway handles both binary frames (SDK ≥ 0.5.1) and legacy JSON text frames
  transparently — older SDKs continue to work unchanged.

## [0.5.0] - 2026-05-19 - Security hardening, dep cleanup, Python 3.10+ only

### Security

- Removed `requests` (and its transitive deps `urllib3`, `idna`) entirely —
  replaced with stdlib `urllib.request`. Closes 6 Dependabot alerts (#6, #9,
  #10, #11, #12, #13, #19) by eliminating the vulnerability surface rather than
  patching it.
- Bumped `Pillow>=12.2.0` (fixes #14, #15, #16, #17, #18, #20 — OOB write,
  FITS decompression bomb, font integer overflow, PDF parsing DoS, and related
  CVEs).
- Bumped `pytest>=9.0.3` in dev deps (fixes #7).

### Changed

- Dropped Python 3.8 and 3.9 support — both are past EOL and the patched
  versions of Pillow and pytest all require `>=3.10`. `requires-python` is now
  `>=3.10`.
- CI matrix: removed 3.8/3.9 runners, added 3.13.

## [0.4.9] - 2026-05-19 - Video input broadening and wire safety

### Added

- `send_video_frame` now accepts raw bytes/bytearray: JPEG bytes are passed
  through without re-encoding (zero CPU cost on hardware that outputs JPEG
  natively); other formats (PNG, BMP, WebP) are decoded via Pillow and
  re-encoded as JPEG. Install `plexus-python[video]` for Pillow support.
- `stream_camera(url, camera_id, fps, quality)` — streams from any
  FFmpeg-supported source (RTSP, video file, capture device). Requires FFmpeg
  on `$PATH`. Returns a `threading.Event`; call `.set()` to stop.
- `read_mjpeg_frames(pipe)` — public generator that parses raw MJPEG byte
  streams (e.g. FFmpeg stdout) into individual JPEG frames by SOI/EOI markers.
  Useful for custom FFmpeg pipelines before handing off to `send_video_frame`.
- Optional `video` extras group: `pip install plexus-python[video]` installs
  Pillow for non-JPEG input decoding and automatic oversized-frame downsampling.

### Changed

- Frames that would exceed the gateway's 1 MB wire limit are automatically
  re-encoded at a proportionally lower quality. A one-time warning is printed
  to stderr; subsequent frames are silently clamped.
- `stream_camera` raises `PlexusError` synchronously (before spawning a thread)
  when FFmpeg is not found, rather than silently dying in the background.
- Minimum `requests` bumped to `>=2.32.4` (fixes CVE in `extract_zipped_paths`).
- Minimum `Pillow` bumped to `>=11.2.1` (fixes OOB write, FITS decompression bomb,
  font integer overflow, PDF parsing DoS).
- Dropped Python 3.8 support (EOL October 2024); minimum is now Python 3.9.

## [0.4.7] - 2026-05-14 - Video streaming API

### Added

- `Plexus.send_video_frame(frame, camera_id, quality, timestamp)` — high-level
  API for streaming camera frames. Accepts a numpy array (e.g. from
  `cv2.VideoCapture.read()`), handles JPEG encoding, base64, dimensions, and
  auth wait internally. Requires `transport="ws"` and `opencv-python`.

### Changed

- Gateway WebSocket URL (`wss://plexus-gateway.fly.dev`) is now the SDK
  default — no need to pass `ws_url` explicitly.
- Removed the `[plexus]   endpoint: …` line from the connection printout.

### Performance

- Eliminated per-frame `buf.tobytes()` copy in `send_video_frame` by passing
  the numpy buffer directly to `base64.b64encode` (buffer protocol).
- `base64` imported at module level; `cv2` imported once on first call and
  cached, removing repeated import overhead from the hot path.

## [0.4.5] - 2026-04-27 - Stderr status output (re-release of 0.4.4)

Same code as 0.4.4 — the 0.4.4 publish workflow failed lint on a stray
`f`-prefix in `plexus/client.py:488`. PyPI doesn't allow re-uploading a
version, so 0.4.5 is the corrected re-release.

## [0.4.4] - 2026-04-27 - Stderr status output

### Added

- `[plexus] …` status lines on stderr at every meaningful state change so
  scripts that don't configure the `logging` module still tell the user
  what's going on. Set `PLEXUS_QUIET=1` to suppress.
  - `✓ Connected to gateway as <source_id>` on first WS auth
  - `✓ Reconnected as <source_id>` after a drop
  - `✓ First N points landed (via ws|http)` on first successful send
  - `⚠ WebSocket unavailable, falling back to POST /ingest` on WS failure
  - `✗ Auth rejected by gateway: …` / `✗ Gateway rejected the API key (401)`
    on auth failures, with a `plexus whoami` hint
  - `⏸ Send failed, buffering points locally (N queued)` when offline
  - `✓ Sending again (drained the local buffer)` on recovery

### Why

Users running `python my_script.py` saw nothing — by default Python's
`logging` module emits at WARNING and above only on the console, so a
silent SDK was indistinguishable from "everything's working" until they
checked the dashboard. This makes the trip from `python my_script.py` to
"first row visible in the UI" auditable in one terminal.

## [0.4.3] - 2026-04-27 - Re-release of 0.4.2 with correct __version__

The 0.4.2 wheel shipped with `plexus.__version__ == "0.4.1"` because the
tag was cut before the `__init__.py` bump landed. 0.4.3 is the same code
with `__version__ = "0.4.3"`. 0.4.2 has been yanked.

## [0.4.2] - 2026-04-27 - CLI auth: branded success page + auto-redirect

### Changed

- `plexus/cli.py` — the localhost callback's success and error pages now
  match the Plexus app's dark aesthetic (black background, zinc-800
  bordered card, white headlines, monospace URL, status-color badge).
- After a successful `plexus init`, the browser tab now auto-redirects
  to the configured app endpoint (`PLEXUS_ENDPOINT`, default
  `https://app.plexus.company`) after a 10-second countdown, so first-
  time users land on their dashboard without having to navigate there
  manually. Falls back to `<meta http-equiv="refresh">` when JS is off.

## [0.4.1] - 2026-04-27 - CI fixes for 0.4.0

### Fixed

- `plexus/cli.py` — drop a stray `f` prefix on a non-interpolated string
  that ruff (`F541`) caught in CI.
- `tests/test_retry.py::test_concurrent_sends` — move `patch.object` out
  of the per-thread closure. `mock.patch.object` mutates instance
  attributes and is not thread-safe; under 20 concurrent threads the
  state would leak and surface as a spurious `AttributeError` on Python
  3.8.

## [0.4.0] - 2026-04-27 - Stable device identity + CLI

The gateway is now authoritative for a device's `source_id`. The SDK sends a
locally-generated `install_id` in the auth frame; the gateway atomically
claims `(org, source_id)` and, if the desired name is already owned by a
different install, returns an auto-suffixed name (`drone-01` → `drone-01_2`
→ `drone-01_3`…) in the `authenticated` frame. The SDK adopts and persists
the assigned name so subsequent reconnects are stable.

This fixes the silent stream-merging that happened when cloned SD-card
images shared a hostname or when two operators picked the same name.

### Added

- `plexus init` (alias `plexus login`) — fly.io / vercel-style browser auth
  flow. Spins up a localhost listener, opens `${PLEXUS_ENDPOINT}/auth/cli`
  with a state-protected callback, and persists the issued key to
  `~/.plexus/config.json`. Console script registered in `pyproject.toml`
  (`plexus = "plexus.cli:main"`); stdlib-only, no new runtime deps.
- `plexus.config.get_install_id()` — lazy per-installation UUID, persisted
  to `~/.plexus/config.json`. **Not** written by `setup.sh`: it's minted by
  the SDK on first run so pre-baked images get distinct IDs per boot.
- `PLEXUS_INSTALL_ID` env var — override for `get_install_id()` so
  ephemeral containers (Fly machines, k8s pods, CI runners) can pin a
  stable identity across restarts when the config filesystem is ephemeral.
  Without this, every redeploy gets a fresh UUID and the gateway
  auto-suffixes the source_id.
- `plexus.config.set_source_id()` — persist the gateway-assigned name after
  auto-suffix resolution.
- `WebSocketTransport(install_id=..., on_source_id_assigned=...)` — the
  transport sends `install_id` in the `device_auth` frame and invokes the
  callback whenever the gateway returns a different `source_id` than
  requested.

### Changed

- `WebSocketTransport` now reads the `source_id` back from the
  `authenticated` frame and updates `self.source_id` in place if the gateway
  auto-suffixed. The rename is logged at INFO level on first occurrence.
- `Plexus` wires `install_id` into the transport and persists the assigned
  `source_id` to config on rename.
- `scripts/setup.sh` — `--name` is **required**. The hostname fallback is
  removed (it was the main source of cloned-image collisions). In a TTY the
  script prompts interactively; in non-TTY it exits with an error. Names are
  validated against `^[a-z0-9][a-z0-9_-]{1,62}$`. Stale `plexus start` /
  `plexus reset` hints were dropped.

### Wire-protocol (compatible)

- `device_auth` frame gains an optional `install_id` field. The gateway
  treats a missing `install_id` as legacy pass-through, so older SDKs and
  the C SDK continue to work unchanged.

## [0.3.0] - WebSocket transport

Adds a wire-compatible WebSocket transport matching the `plexus-c` SDK. WS is now the default; failed sends transparently fall back to `POST /ingest`.

### Added

- `plexus.WebSocketTransport` — connects to `/ws/device` on the gateway. Exchanges the same `device_auth` / `authenticated` / `telemetry` / `heartbeat` / `typed_command` / `command_result` frames as `plexus-c`.
- `Plexus(transport="ws" | "http")` — defaults to `"ws"`.
- `Plexus.on_command(name, handler, description=..., params=...)` — register command handlers; automatic `ack`, handler return becomes `result`, exceptions become `error`.
- `Plexus.close()` — stops the WebSocket thread.
- Runtime dep: `websocket-client>=1.7`.
- Tests: `tests/test_ws.py` (auth handshake, telemetry, command roundtrip, error paths).

## [0.2.0] - Thin SDK rewrite

Breaking. `plexus-python` is now just the thin client — no agent, adapters, sensors, CLI, or TUI. The package is 886 lines with one runtime dependency (`requests`). Protocol integrations (MAVLink, CAN, MQTT, Modbus, OPC-UA, BLE, I2C sensors) now live as standalone recipes in `examples/`, using the upstream library directly (`pymavlink`, `python-can`, `paho-mqtt`, etc.) plus `px.send()`.

### Added

- 5 runnable example scripts: `basic.py`, `mavlink.py`, `can.py`, `mqtt.py`, `i2c_bme280.py`

### Removed

- `plexus/adapters/` (MAVLink, CAN, MQTT, Modbus, OPC-UA, BLE, Serial — use the upstream lib directly)
- `plexus/sensors/` (I2C drivers + auto-detect — use Adafruit CircuitPython or smbus2 directly)
- `plexus/cameras/` (frame upload — out of scope)
- `plexus/cli.py`, `plexus/connector.py`, `plexus/streaming.py`, `plexus/detect.py`, `plexus/tui.py`, `plexus/deps.py`
- `plexus` console script, `python -m plexus`
- Extras: `[sensors]`, `[system]`, `[tui]`, `[mqtt]`, `[can]`, `[mavlink]`, `[modbus]`, `[opcua]`, `[ble]`, `[serial]`, `[ros]`, `[camera]`, `[picamera]`, `[all]`
- Runtime deps: `click`, `websockets`

### Changed

- Default ingest endpoint points directly at the Plexus gateway (`https://plexus-gateway.fly.dev/ingest`), not the Next.js app proxy
- Client raises `ValueError` clearly when no API key is available, instead of invoking a login flow

## [0.1.0] - Initial release

- `Plexus` thin client for HTTP ingest
- `plexus start` daemon with WebSocket streaming
- Protocol adapters: MAVLink, CAN, MQTT, Modbus, OPC-UA, Serial, BLE
- I2C sensor auto-detection and drivers
- Store-and-forward buffering (SQLite)
