"""
Plexus client for sending sensor data.

Usage:
    from plexus import Plexus

    px = Plexus()
    px.send("temperature", 72.5)

    # With tags
    px.send("motor.rpm", 3450, tags={"motor_id": "A1"})

    # Flexible value types (not just numbers!)
    px.send("robot.state", "MOVING")                    # String states
    px.send("error.code", "E_MOTOR_STALL")              # Error codes
    px.send("position", {"x": 1.5, "y": 2.3, "z": 0.8}) # Complex objects
    px.send("joint_angles", [0.5, 1.2, -0.3, 0.0])      # Arrays
    px.send("motor.enabled", True)                      # Booleans

    # Batch send
    px.send_batch([
        ("temperature", 72.5),
        ("humidity", 45.2),
        ("pressure", 1013.25),
    ])

    # Run recording
    with px.run("motor-test-001"):
        while True:
            px.send("temperature", read_temp())
            time.sleep(0.01)

Note: Requires authentication. Run 'plexus init' or set PLEXUS_API_KEY.
"""

import gzip
import json
import logging
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from plexus._log import _say
from plexus.buffer import BufferBackend, MemoryBuffer, SqliteBuffer
from plexus.config import (
    RetryConfig,
    get_api_key,
    get_endpoint,
    get_gateway_url,
    get_gateway_ws_url,
    get_source_id,
)

logger = logging.getLogger(__name__)


class _Response:
    __slots__ = ("status_code", "text")

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class _Session:
    def __init__(self):
        self.headers: Dict[str, str] = {}

    def post(self, url: str, data: bytes = b"", headers: Optional[Dict[str, str]] = None, timeout: float = 10.0) -> "_Response":
        req_headers = {**self.headers, **(headers or {})}
        req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _Response(resp.status, resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            return _Response(e.code, e.read().decode("utf-8", errors="replace"))
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.timeout):
                raise _Timeout(str(e.reason))
            raise _ConnError(str(e.reason))
        except (TimeoutError, socket.timeout) as e:
            raise _Timeout(str(e))

    def close(self) -> None:
        pass


class _Timeout(OSError):
    pass


class _ConnError(OSError):
    pass


# Flexible value type - supports any JSON-serializable value
FlexValue = Union[int, float, str, bool, Dict[str, Any], List[Any]]

_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"
_FRAME_JPEG_MAX = 750_000  # gateway is 1MB; base64 × 1.33 + envelope ≈ 998KB at this size


def read_mjpeg_frames(pipe, chunk: int = 65536) -> Generator[bytes, None, None]:
    """Read a raw MJPEG byte stream (e.g. FFmpeg stdout) and yield complete JPEG frames.

    Scans for SOI (\xff\xd8) / EOI (\xff\xd9) markers to delimit frames.
    Useful when building custom FFmpeg pipelines and handing off bytes to
    send_video_frame().
    """
    buf = b""
    while True:
        data = pipe.read(chunk)
        if not data:
            break
        buf += data
        while True:
            start = buf.find(_JPEG_SOI)
            if start == -1:
                buf = b""
                break
            end = buf.find(_JPEG_EOI, start + 2)
            if end == -1:
                buf = buf[start:]  # keep partial frame
                break
            yield buf[start:end + 2]
            buf = buf[end + 2:]


class PlexusError(Exception):
    """Base exception for Plexus errors."""

    pass


class AuthenticationError(PlexusError):
    """Raised when API key is missing or invalid."""

    pass


_SOURCE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{1,62}$')


def _validate_source_id(source_id: str) -> None:
    if not _SOURCE_ID_RE.match(source_id):
        raise ValueError(
            f"Invalid source_id {source_id!r}. "
            "Must match ^[a-z0-9][a-z0-9_-]{1,62}$ "
            "(lowercase letters, digits, hyphens, underscores; start with letter or digit)."
        )


class Plexus:
    """
    Client for sending sensor data to Plexus.

    Args:
        api_key: Your Plexus API key. If not provided, reads from
                 PLEXUS_API_KEY env var or ~/.plexus/config.json
        endpoint: API endpoint URL. Defaults to https://app.plexus.company
        source_id: Unique identifier for this source. Auto-generated if not provided.
        timeout: Request timeout in seconds. Default 10s.
        retry_config: Configuration for retry behavior. If None, uses defaults.
        max_buffer_size: Maximum number of points to buffer locally on failures. Default 10000.

    Raises:
        RuntimeError: If not logged in (no API key configured)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        source_id: Optional[str] = None,
        timeout: float = 10.0,
        retry_config: Optional[RetryConfig] = None,
        max_buffer_size: int = 10000,
        persistent_buffer: bool = True,
        buffer_path: Optional[str] = None,
        ws_url: Optional[str] = None,
    ):
        self.api_key = api_key or get_api_key()
        if not self.api_key:
            raise ValueError(
                "No API key. Pass api_key=... or set PLEXUS_API_KEY. "
                "Get a key at app.plexus.company/devices."
            )

        self.endpoint = (endpoint or get_endpoint()).rstrip("/")
        self.gateway_url = get_gateway_url()
        self.source_id = source_id or get_source_id()
        _validate_source_id(self.source_id)
        self.timeout = timeout
        self.retry_config = retry_config or RetryConfig()
        self._max_buffer_size = max_buffer_size

        self._run_id: Optional[str] = None
        self._session: Optional[_Session] = None
        self._store_frames: bool = False
        self._cv2 = None
        self._pil_image = None  # lazy PIL.Image import
        self._fit_warned: bool = False

        self._ws_url = (ws_url or get_gateway_ws_url())
        self._ws = None  # lazily constructed in _ensure_ws()
        self._clock_offset_ms: int = 0

        # Pluggable buffer backend for failed sends
        if persistent_buffer:
            self._buffer: BufferBackend = SqliteBuffer(
                path=buffer_path, max_size=max_buffer_size,
                on_overflow=self._on_buffer_overflow,
            )
        else:
            self._buffer: BufferBackend = MemoryBuffer(
                max_size=max_buffer_size,
                on_overflow=self._on_buffer_overflow,
            )

        # State that drives the [plexus] stderr status line.
        self._announced_first_send = False
        self._announced_http_fallback = False
        self._announced_buffering = False
        self._send_count = 0

    @property
    def max_buffer_size(self):
        return self._max_buffer_size

    @max_buffer_size.setter
    def max_buffer_size(self, value):
        self._max_buffer_size = value
        self._buffer.resize(value)

    def _get_session(self) -> _Session:
        if self._session is None:
            self._session = _Session()
            if self.api_key:
                self._session.headers["x-api-key"] = self.api_key
            self._session.headers["Content-Type"] = "application/json"
            from plexus import __version__
            self._session.headers["User-Agent"] = f"plexus-python/{__version__}"
        return self._session

    def _normalize_ts_ms(self, timestamp: Optional[float] = None) -> int:
        """Normalize a timestamp to milliseconds.

        Accepts:
            - None: returns current time in ms, corrected by server clock offset
            - float seconds (e.g. time.time()): converts to ms (no offset applied)
            - int/float ms: returned as-is (no offset applied)
        """
        if timestamp is None:
            return int(time.time() * 1000) + self._clock_offset_ms
        # Heuristic: values < 1e12 are seconds
        if timestamp > 0 and timestamp < 1e12:
            return int(timestamp * 1000)
        return int(timestamp)

    def _make_point(
        self,
        metric: str,
        value: FlexValue,
        timestamp: Optional[float] = None,
        tags: Optional[Dict[str, str]] = None,
        data_class: str = "metric",
    ) -> Dict[str, Any]:
        """Create a data point dictionary.

        Value can be:
            - number (int/float): Traditional sensor readings
            - string: State machines, error codes, status
            - bool: Binary flags, enabled/disabled states
            - dict: Complex objects, vectors, nested data
            - list: Arrays, coordinates, multi-value readings
        """
        point = {
            "class": data_class,
            "metric": metric,
            "value": value,
            "timestamp": self._normalize_ts_ms(timestamp),
        }
        if tags:
            point["tags"] = tags
        if self._run_id:
            point["run_id"] = self._run_id
        return point

    def send(
        self,
        metric: str,
        value: FlexValue,
        timestamp: Optional[float] = None,
        tags: Optional[Dict[str, str]] = None,
        data_class: str = "metric",
    ) -> bool:
        """
        Send a single metric value to Plexus.

        Args:
            metric: Name of the metric (e.g., "temperature", "motor.rpm")
            value: Value to send. Can be:
                   - number (int/float): px.send("temp", 72.5)
                   - string: px.send("state", "RUNNING")
                   - bool: px.send("enabled", True)
                   - dict: px.send("pos", {"x": 1, "y": 2})
                   - list: px.send("angles", [0.5, 1.2, -0.3])
            timestamp: Unix timestamp. If not provided, uses current time.
            tags: Optional key-value tags for the metric
            data_class: Pipeline data class - "metric" (default) or "event"

        Returns:
            True if successful

        Raises:
            AuthenticationError: If API key is missing or invalid (cloud mode only)
            PlexusError: If the request fails

        Example:
            px.send("temperature", 72.5)
            px.send("motor.rpm", 3450, tags={"motor_id": "A1"})
            px.send("gps.status", {"fix": "lost"}, data_class="event")
        """
        point = self._make_point(metric, value, timestamp, tags, data_class)
        return self._send_points([point])

    def event(
        self,
        name: str,
        data: FlexValue,
        timestamp: Optional[float] = None,
        tags: Optional[Dict[str, str]] = None,
    ) -> bool:
        """
        Send a named event with text or structured data.

        Args:
            name: Event type (e.g., "fault", "state_change", "log")
            data: Text or JSON-serializable value (string, dict, list, bool, number)
            timestamp: Unix timestamp. If not provided, uses current time.
            tags: Optional key-value tags

        Example:
            px.event("fault", "E-stop triggered")
            px.event("state_change", {"from": "IDLE", "to": "RUNNING"})
            px.event("sensor_error", {"sensor": "imu", "code": 42}, tags={"motor": "A"})
        """
        point = self._make_point(name, data, timestamp, tags, data_class="event")
        return self._send_points([point])

    def send_batch(
        self,
        points: List[Union[Tuple[str, FlexValue], Tuple[str, FlexValue, float]]],
        timestamp: Optional[float] = None,
        tags: Optional[Dict[str, str]] = None,
    ) -> bool:
        """
        Send multiple metrics at once.

        Args:
            points: List of (metric, value) or (metric, value, timestamp) tuples.
                    Values can be any FlexValue type. Per-point timestamps override
                    the shared timestamp argument.
            timestamp: Shared timestamp for points that don't supply their own.
                       If not provided, uses current time.
            tags: Shared tags for all points

        Returns:
            True if successful

        Example:
            px.send_batch([
                ("temperature", 72.5),
                ("humidity", 45.2),
                ("robot.state", "RUNNING"),
                ("position", {"x": 1.0, "y": 2.0}),
            ])

            # Per-point timestamps (e.g. sensors on different interrupt timers):
            px.send_batch([
                ("imu.accel_x", 0.12, t_imu),
                ("pressure",    1013.2, t_baro),
                ("temperature", 22.4),   # uses shared timestamp
            ])
        """
        default_ts_ms = self._normalize_ts_ms(timestamp)
        data_points = []
        for p in points:
            if len(p) == 3:
                m, v, t = p
                data_points.append(self._make_point(m, v, self._normalize_ts_ms(t), tags))
            else:
                m, v = p
                data_points.append(self._make_point(m, v, default_ts_ms, tags))
        return self._send_points(data_points)

    def _ensure_ws(self):
        """Lazily construct and start the WebSocket transport."""
        if self._ws is not None:
            return self._ws
        from plexus.ws import WebSocketTransport
        from plexus import __version__
        self._ws = WebSocketTransport(
            api_key=self.api_key,
            source_id=self.source_id,
            ws_url=self._ws_url,
            agent_version=__version__,
            on_clock_synced=self._on_clock_synced,
        )
        self._ws.start()
        return self._ws

    def _on_buffer_overflow(self, dropped: int) -> None:
        _say(f"⚠ buffer full, dropped {dropped} oldest points (gateway unreachable?)")

    def _on_clock_synced(self, offset_ms: int) -> None:
        self._clock_offset_ms = offset_ms

    def _encode_frame(self, frame, quality: int) -> Tuple[bytes, int, int]:
        """Normalize any supported frame type to (jpeg_bytes, width, height).

        Accepted inputs:
          - bytes/bytearray: raw JPEG passthrough (magic \\xff\\xd8), or any
            Pillow-readable format (PNG, BMP, WebP) which is decoded and re-encoded
          - numpy ndarray: encoded via OpenCV (cv2 must be installed)
        """
        import io

        # --- bytes input ---
        if isinstance(frame, (bytes, bytearray)):
            if frame[:2] == b"\xff\xd8":
                # Already JPEG — passthrough, extract dimensions via Pillow if available
                try:
                    pil = self._get_pil()
                    img = pil.open(io.BytesIO(frame))
                    return bytes(frame), img.width, img.height
                except Exception:
                    # Pillow unavailable or unreadable — send as-is, dimensions unknown
                    return bytes(frame), 0, 0
            # Non-JPEG bytes (PNG, BMP, WebP, …) — Pillow decode then re-encode as JPEG
            pil = self._get_pil(required=True)
            img = pil.open(io.BytesIO(frame))
            return self._pil_to_jpeg(img, quality)

        # --- numpy array (OpenCV path) ---
        if hasattr(frame, "shape"):
            cv2 = self._get_cv2(required=True)
            height, width = frame.shape[:2]
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if not ok:
                raise PlexusError("cv2.imencode failed to encode frame")
            return buf.tobytes(), width, height

        raise ValueError(
            f"Unsupported frame type: {type(frame).__name__}. "
            "Expected bytes/bytearray (JPEG or Pillow-readable) or numpy ndarray."
        )

    def _get_cv2(self, required: bool = False):
        if self._cv2 is None:
            try:
                import cv2 as _cv2
                self._cv2 = _cv2
            except ImportError as e:
                if required:
                    raise ImportError(
                        "This frame type requires opencv-python-headless. "
                        "Install with: pip install plexus-python[video]"
                    ) from e
        return self._cv2

    def _get_pil(self, required: bool = False):
        if self._pil_image is None:
            try:
                import PIL.Image as _PILImage
                self._pil_image = _PILImage
            except ImportError as e:
                if required:
                    raise ImportError(
                        "This frame type requires Pillow. "
                        "Install with: pip install plexus-python[video]"
                    ) from e
        return self._pil_image

    def _pil_to_jpeg(self, img, quality: int) -> Tuple[bytes, int, int]:
        import io
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), img.width, img.height

    def _fit_to_wire(self, jpeg_bytes: bytes, requested_quality: int) -> bytes:
        """Re-encode JPEG at lower quality if it would exceed the gateway 1MB limit.

        Warns once per Plexus instance so the user sees the issue at startup
        without being flooded during a live stream.
        """
        import io
        if len(jpeg_bytes) <= _FRAME_JPEG_MAX:
            return jpeg_bytes
        target_quality = max(10, int(requested_quality * _FRAME_JPEG_MAX / len(jpeg_bytes)))
        pil = self._get_pil()
        if pil is None:
            if not self._fit_warned:
                self._fit_warned = True
                wire_kb = len(jpeg_bytes) * 4 // 3 // 1024
                _say(
                    f"frame too large (~{wire_kb}KB on wire) and Pillow is not installed — "
                    "install plexus-python[video] to enable automatic downsampling"
                )
            return jpeg_bytes
        try:
            img = pil.open(io.BytesIO(jpeg_bytes))
            buf = io.BytesIO()
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=target_quality)
            result = buf.getvalue()
            if not self._fit_warned:
                self._fit_warned = True
                wire_kb = len(jpeg_bytes) * 4 // 3 // 1024
                _say(
                    f"frame too large (quality={requested_quality}, ~{wire_kb}KB on wire), "
                    f"re-encoded at quality={target_quality} — lower quality or resolution to silence"
                )
            return result
        except Exception as e:
            logger.debug("_fit_to_wire re-encode failed: %s", e)
            return jpeg_bytes

    def send_video_frame(
        self,
        frame,
        camera_id: str = "camera:0",
        quality: int = 85,
        timestamp: Optional[float] = None,
    ) -> bool:
        """Send a single video frame to Plexus (WebSocket transport only).

        Args:
            frame: One of:
                - numpy ndarray (H, W, C) — from cv2.VideoCapture or picamera2
                - bytes/bytearray — raw JPEG passthrough (zero re-encode), or any
                  Pillow-readable format (PNG, BMP, WebP) which is decoded and re-encoded
            camera_id: Logical camera identifier (e.g. "picam:0", "usb:1")
            quality: JPEG compression quality, 1-100. Default 85. Also used as the
                baseline when adaptive downsampling kicks in for oversized frames.
            timestamp: Unix timestamp in seconds. If not provided, uses current time.

        Returns:
            True if the frame was sent successfully.

        Raises:
            PlexusError: If transport is not 'ws'.
            ValueError: If frame type is not supported.
            ImportError: If a required optional dependency is missing.
        """
        jpeg_bytes, width, height = self._encode_frame(frame, quality)
        jpeg_bytes = self._fit_to_wire(jpeg_bytes, quality)

        ws = self._ensure_ws()
        if not ws.is_authenticated:
            ws.wait_authenticated(timeout=min(self.timeout, 5.0))

        return ws.send_video_frame_async(
            self.source_id, camera_id, jpeg_bytes, width, height,
            self._normalize_ts_ms(timestamp),
        )

    def send_thermal_frame(
        self,
        temps,
        camera_id: str = "thermal:0",
        quality: int = 85,
        timestamp: Optional[float] = None,
    ) -> bool:
        """Send a thermal camera frame to Plexus (WebSocket transport only).

        Args:
            temps: 2-D float32 numpy array of temperatures in Celsius,
                   shape (height, width). Obtained from ThermalCamera.read_frame().
            camera_id: Logical camera identifier.
            quality: JPEG quality for the colorized image, 1-100.
            timestamp: Unix timestamp in seconds. Defaults to current time.

        Returns:
            True if the frame was sent successfully.

        Raises:
            PlexusError: If transport is not 'ws'.
            ImportError: If opencv-python-headless is not installed.
        """
        try:
            from plexus.cameras.thermal import build_thermal_frame
        except ImportError as e:
            raise ImportError(
                "send_thermal_frame requires opencv-python-headless. "
                "Install with: pip install plexus-python[video]"
            ) from e

        frame = build_thermal_frame(temps, timestamp_ms=self._normalize_ts_ms(timestamp))
        msg = frame.to_message(
            camera_id=camera_id, source_id=self.source_id, quality=quality
        )

        ws = self._ensure_ws()
        if not ws.is_authenticated:
            ws.wait_authenticated(timeout=min(self.timeout, 5.0))

        return ws.send_json_video_frame(msg)

    def stream_camera(
        self,
        url: str,
        camera_id: str = "camera:0",
        fps: int = 15,
        quality: int = 85,
    ) -> "threading.Event":
        """Stream video from an RTSP URL or file path via FFmpeg (WebSocket only).

        Requires FFmpeg to be installed and available on $PATH.

        Args:
            url: RTSP stream URL (rtsp://...), video file path, or any FFmpeg-supported source.
            camera_id: Logical camera identifier forwarded in each frame.
            fps: Maximum frames per second to send. Default 15.
            quality: JPEG quality for re-encoded frames, 1-100. Default 85.

        Returns:
            A threading.Event. Call .set() on it to stop streaming.

        Raises:
            PlexusError: If transport is not 'ws' or FFmpeg is not found.

        Example:
            stop = px.stream_camera("rtsp://192.168.1.100/stream", camera_id="front:0")
            time.sleep(60)
            stop.set()
        """
        if shutil.which("ffmpeg") is None:
            raise PlexusError(
                "FFmpeg not found. Install it: https://ffmpeg.org/download.html"
            )

        stop_event = threading.Event()

        def _run():
            cmd = [
                "ffmpeg", "-loglevel", "error",
                "-i", url,
                "-vf", f"fps={fps}",
                "-f", "image2pipe",
                "-vcodec", "mjpeg",
                "pipe:1",
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for jpeg in read_mjpeg_frames(proc.stdout):
                    if stop_event.is_set():
                        break
                    try:
                        self.send_video_frame(jpeg, camera_id=camera_id, quality=quality)
                    except Exception as e:
                        logger.debug("stream_camera send error: %s", e)
            finally:
                proc.terminate()
                proc.wait()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return stop_event

    def on_command(
        self,
        name: str,
        handler,
        *,
        description: Optional[str] = None,
        params: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Register a command handler (WebSocket transport only).

        The handler is called as `handler(command_name, params_dict)` and may
        return a dict (→ `result`) or raise (→ `error`). An `ack` is sent
        automatically before the handler runs.

        Must be called before the first send() so the command is advertised
        in the auth frame.
        """
        ws = self._ensure_ws()
        if ws.is_authenticated:
            logger.warning(
                "on_command('%s') called after connection is already authenticated — "
                "command will not be advertised to the dashboard until next reconnect. "
                "Call on_command() before the first send().",
                name,
            )
        ws.register_command(name, handler, description=description, params=params)

    def _send_points(self, points: List[Dict[str, Any]]) -> bool:
        """Send data points to the gateway with retry and buffering.

        Tries WebSocket first; if not yet authenticated or the socket fails,
        falls through to HTTP POST so points still land.

        Retry behavior (HTTP path):
        - Retries on: Timeout, ConnectionError, HTTP 429, HTTP 5xx
        - No retry on: HTTP 401/403 (auth), HTTP 400/422 (bad request)
        - After max retries: buffers points locally for next send attempt
        """
        if not self.api_key:
            raise AuthenticationError(
                "No API key configured. Run 'plexus init' or set PLEXUS_API_KEY"
            )

        # Include any previously buffered points
        all_points = self._get_buffered_points() + points

        # Preferred path: WebSocket.
        ws = self._ensure_ws()
        # Brief wait on first call so startup races don't dump every point
        # into the HTTP fallback path.
        if not ws.is_authenticated:
            ws.wait_authenticated(timeout=min(self.timeout, 5.0))
        if ws.send_points(all_points):
            self._clear_buffer()
            self._note_send(len(all_points), via="ws")
            return True
        # Socket unavailable → fall through to HTTP.
        if not self._announced_http_fallback:
            _say(
                f"⚠ WebSocket unavailable, falling back to POST {self.gateway_url}/ingest"
            )
            self._announced_http_fallback = True

        url = f"{self.gateway_url}/ingest"
        last_error: Optional[Exception] = None

        for attempt in range(self.retry_config.max_retries + 1):
            try:
                payload = json.dumps({"source_id": self.source_id, "points": all_points})
                payload_bytes = payload.encode("utf-8")

                # Gzip compress payloads > 1KB for bandwidth efficiency
                if len(payload_bytes) > 1024:
                    body = gzip.compress(payload_bytes, compresslevel=6)
                    headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
                else:
                    body = payload_bytes
                    headers = {"Content-Type": "application/json"}

                response = self._get_session().post(
                    url,
                    data=body,
                    headers=headers,
                    timeout=self.timeout,
                )

                # Auth errors - don't retry, raise immediately
                if response.status_code == 401:
                    _say("✗ Gateway rejected the API key (401).")
                    _say("  Run `plexus whoami` to confirm what's on disk.")
                    raise AuthenticationError("Invalid API key")
                elif response.status_code == 403:
                    _say("✗ API key lacks write scope (403).")
                    raise AuthenticationError("API key doesn't have write permissions")

                # Bad request errors - don't retry (client error)
                elif response.status_code in (400, 422):
                    raise PlexusError(
                        f"Bad request: {response.status_code} - {response.text}"
                    )

                # Rate limit - retry with backoff
                elif response.status_code == 429:
                    last_error = PlexusError("Rate limited (429)")
                    if attempt < self.retry_config.max_retries:
                        time.sleep(self.retry_config.get_delay(attempt))
                        continue
                    break

                # Server errors - retry with backoff
                elif response.status_code >= 500:
                    last_error = PlexusError(
                        f"Server error: {response.status_code} - {response.text}"
                    )
                    if attempt < self.retry_config.max_retries:
                        time.sleep(self.retry_config.get_delay(attempt))
                        continue
                    break

                # Success - clear the buffer and return
                elif response.status_code < 400:
                    self._clear_buffer()
                    self._note_send(len(all_points), via="http")
                    return True

                # Other 4xx errors - don't retry
                else:
                    raise PlexusError(
                        f"API error: {response.status_code} - {response.text}"
                    )

            except _Timeout:
                last_error = PlexusError(f"Request timed out after {self.timeout}s")
                if attempt < self.retry_config.max_retries:
                    time.sleep(self.retry_config.get_delay(attempt))
                    continue
                break

            except _ConnError as e:
                last_error = PlexusError(f"Connection failed: {e}")
                if attempt < self.retry_config.max_retries:
                    time.sleep(self.retry_config.get_delay(attempt))
                    continue
                break

        # All retries failed - buffer the points for later
        self._add_to_buffer(points)
        if not self._announced_buffering:
            _say(
                f"⏸ Send failed, buffering points locally ({self.buffer_size()} queued). "
                f"Will retry on next call."
            )
            self._announced_buffering = True

        if last_error:
            raise last_error
        raise PlexusError("Send failed after all retries")

    def _note_send(self, count: int, via: str) -> None:
        """Bookkeeping so the user sees the moment data starts flowing.

        First successful send → "✓ First N points landed (via WS/HTTP)".
        Recovery from a buffering state → "✓ Sending again (was buffered)".
        Otherwise silent — every-send chatter would be unbearable at 100 Hz.
        """
        self._send_count += count
        if not self._announced_first_send:
            _say(
                f"✓ First {count} point{'s' if count != 1 else ''} landed "
                f"(via {via}). source_id={self.source_id!r}"
            )
            self._announced_first_send = True
        elif self._announced_buffering:
            _say("✓ Sending again (drained the local buffer).")
            self._announced_buffering = False

    def _add_to_buffer(self, points: List[Dict[str, Any]]) -> None:
        """Add points to the local buffer for later retry."""
        self._buffer.add(points)

    def _get_buffered_points(self) -> List[Dict[str, Any]]:
        """Get a copy of buffered points without clearing."""
        return self._buffer.get_all()

    def _clear_buffer(self) -> None:
        """Clear the failed points buffer."""
        self._buffer.clear()

    def buffer_size(self) -> int:
        """Return the number of points currently buffered locally.

        Points are buffered when sends fail after all retries.
        They will be included in the next send attempt.
        """
        return self._buffer.size()

    def flush_buffer(self) -> bool:
        """Attempt to send all buffered points.

        Returns:
            True if buffer is empty (either was empty or successfully flushed)

        Raises:
            PlexusError: If flush fails (points remain in buffer)
        """
        if self.buffer_size() == 0:
            return True

        # Send with empty new points list - will include buffered points
        return self._send_points([])

    @contextmanager
    def run(self, run_id: str, tags: Optional[Dict[str, str]] = None, store_frames: bool = False):
        """
        Context manager for recording a run.

        All sends within this context will be tagged with the run ID,
        making it easy to replay and analyze later.

        Args:
            run_id: Unique identifier for this run (e.g., "motor-test-001")
            tags: Optional tags to apply to all points in this run
            store_frames: If True, camera frames are uploaded to the Plexus API
                         for persistent storage alongside the live WebSocket stream.

        Example:
            with px.run("motor-test-001", store_frames=True):
                while True:
                    px.send("temperature", read_temp())
                    time.sleep(0.01)
        """
        self._run_id = run_id
        self._store_frames = store_frames

        # Notify API that run started
        try:
            self._get_session().post(
                f"{self.endpoint}/api/runs",
                data=json.dumps({
                    "run_id": run_id,
                    "source_id": self.source_id,
                    "status": "started",
                    "tags": tags,
                    "timestamp": (int(time.time() * 1000) + self._clock_offset_ms) / 1000,
                }).encode("utf-8"),
                timeout=self.timeout,
            )
        except Exception as e:
            logger.debug(f"Run start notification failed: {e}")

        try:
            yield
        finally:
            # Notify API that run ended
            try:
                self._get_session().post(
                    f"{self.endpoint}/api/runs",
                    data=json.dumps({
                        "run_id": run_id,
                        "source_id": self.source_id,
                        "status": "ended",
                        "timestamp": (int(time.time() * 1000) + self._clock_offset_ms) / 1000,
                    }).encode("utf-8"),
                    timeout=self.timeout,
                )
            except Exception as e:
                logger.debug(f"Run end notification failed: {e}")
            self._run_id = None
            self._store_frames = False

    def close(self):
        """Close the client, flush any buffered points, and release resources."""
        if self.buffer_size() > 0:
            try:
                self.flush_buffer()
            except Exception as e:
                logger.debug("flush on close failed: %s", e)
        if self._ws is not None:
            self._ws.stop()
            self._ws = None
        if self._session:
            self._session.close()
            self._session = None
        if hasattr(self._buffer, "close"):
            self._buffer.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
