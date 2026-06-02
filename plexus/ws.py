"""
WebSocket transport for the Plexus Python SDK.

Wire-compatible with the C SDK (`plexus_ws.c`). Targets the gateway's
`/ws/device` endpoint and exchanges the same JSON frames:

    client → {"type": "device_auth", "api_key": ..., "source_id": ...,
              "install_id": ..., "platform": "python-sdk",
              "agent_version": ..., "commands": [...]}
    server → {"type": "authenticated", "source_id": ...}

The server-returned `source_id` in the `authenticated` frame is
authoritative: if the gateway auto-suffixed on a collision (e.g. the
desired name was already claimed by a different install_id), the
client's `source_id` is updated in place to match.
    client → {"type": "telemetry", "points": [...]}
    client → {"type": "heartbeat", "source_id": ..., "agent_version": ...}   # every 30s
    server → {"type": "typed_command", "id": ..., "command": ..., "params": {...}}
    client → {"type": "command_result", "id": ..., "command": ..., "event": "ack"}
    client → {"type": "command_result", "id": ..., "command": ...,
              "event": "result" | "error", "result": {...} | "error": "..."}

Runs the read loop on a background daemon thread so callers can stay sync.
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import random
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

try:
    import websocket  # websocket-client
except ImportError as e:  # pragma: no cover - import-time failure is obvious
    raise ImportError(
        "WebSocket transport requires 'websocket-client'. "
        "Install with: pip install websocket-client"
    ) from e

from plexus._log import _say

logger = logging.getLogger(__name__)

AUTH_TIMEOUT_S = 10.0
HEARTBEAT_INTERVAL_S = 30.0
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0

CommandHandler = Callable[[str, Dict[str, Any]], Optional[Dict[str, Any]]]


@dataclass
class _RegisteredCommand:
    name: str
    handler: CommandHandler
    description: Optional[str] = None
    params: List[Dict[str, Any]] = field(default_factory=list)

    def to_manifest(self) -> Dict[str, Any]:
        m: Dict[str, Any] = {"name": self.name}
        if self.description:
            m["description"] = self.description
        if self.params:
            m["params"] = self.params
        return m


class WebSocketTransport:
    """Background WebSocket connection to the Plexus gateway.

    Lifecycle:
        t = WebSocketTransport(api_key, source_id, ws_url)
        t.start()
        t.wait_authenticated(timeout=5)
        t.send_points([...])
        t.stop()
    """

    def __init__(
        self,
        api_key: str,
        source_id: str,
        ws_url: str,
        *,
        agent_version: str = "0.0.0",
        platform: str = "python-sdk",
        auto_reconnect: bool = True,
        on_clock_synced: Optional[Callable[[int], None]] = None,
    ):
        if not api_key:
            raise ValueError("api_key required")
        if not source_id:
            raise ValueError("source_id required")

        self.api_key = api_key
        self.source_id = source_id
        self.ws_url = _ensure_device_path(ws_url)
        self.agent_version = agent_version
        self.platform = platform
        self.auto_reconnect = auto_reconnect
        self._on_clock_synced = on_clock_synced

        self._commands: Dict[str, _RegisteredCommand] = {}
        self._ws: Optional[websocket.WebSocket] = None
        self._ws_lock = threading.Lock()
        self._authenticated = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._backoff_attempt = 0
        self._clock_offset_ms: int = 0
        self._video_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=2)
        self._video_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ public

    def register_command(
        self,
        name: str,
        handler: CommandHandler,
        *,
        description: Optional[str] = None,
        params: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Register a command handler. Must be called before start() to be
        advertised in the auth frame."""
        self._commands[name] = _RegisteredCommand(
            name=name, handler=handler, description=description, params=params or []
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="plexus-ws", daemon=True
        )
        self._thread.start()
        self._video_thread = threading.Thread(
            target=self._video_sender_loop, name="plexus-video", daemon=True
        )
        self._video_thread.start()
        atexit.register(self.stop)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._ws_lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # pragma: no cover
                pass
        if self._thread:
            self._thread.join(timeout=timeout)
        if self._video_thread:
            self._video_thread.join(timeout=timeout)

    def wait_authenticated(self, timeout: float = AUTH_TIMEOUT_S) -> bool:
        return self._authenticated.wait(timeout=timeout)

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated.is_set()

    @property
    def clock_offset_ms(self) -> int:
        return self._clock_offset_ms

    def send_points(self, points: List[Dict[str, Any]]) -> bool:
        """Send a telemetry frame. Returns False if the socket is not
        authenticated — caller is expected to fall back to HTTP."""
        if not points:
            return True
        if not self._authenticated.is_set():
            return False
        frame = {"type": "telemetry", "points": points}
        return self._send_frame(frame)

    def send_video_frame_async(
        self,
        source_id: str,
        camera_id: str,
        jpeg_bytes: bytes,
        width: int,
        height: int,
        timestamp_ms: int,
    ) -> bool:
        """Encode and enqueue a binary video frame. Non-blocking — drops the
        frame if the queue is full rather than blocking the caller."""
        if not self._authenticated.is_set():
            return False
        payload = _encode_binary_video_frame(
            source_id, camera_id, jpeg_bytes, width, height, timestamp_ms
        )
        try:
            self._video_queue.put_nowait(payload)
            return True
        except queue.Full:
            return False

    def send_json_video_frame(self, msg: Dict[str, Any]) -> bool:
        """Send a JSON video_frame message. Used for frames that carry extra
        metadata (e.g. thermal cameras) that the binary format cannot express."""
        if not self._authenticated.is_set():
            return False
        return self._send_frame(msg)

    # ------------------------------------------------------------------ thread

    def _run(self) -> None:
        first_attempt = True
        while not self._stop.is_set():
            try:
                self._connect_and_serve()
            except Exception as e:
                msg = str(e)
                logger.warning("plexus ws loop error: %s", msg)
                # Loud the first time so users running a script see it,
                # quieter on subsequent retries to avoid log spam.
                if first_attempt:
                    if "auth failed" in msg.lower() or "invalid api key" in msg.lower():
                        _say(f"✗ Auth rejected by gateway: {msg}")
                        _say("  Check your key — `plexus whoami` shows what's on disk.")
                    else:
                        _say(f"✗ Connection failed: {msg}")
                        _say("  SDK will keep retrying with backoff.")
            finally:
                self._authenticated.clear()
                with self._ws_lock:
                    self._ws = None

            if not self.auto_reconnect or self._stop.is_set():
                break

            delay = _backoff_delay(self._backoff_attempt)
            self._backoff_attempt = min(self._backoff_attempt + 1, 10)
            logger.info("plexus ws reconnect in %.1fs", delay)
            first_attempt = False
            if self._stop.wait(timeout=delay):
                break

    def _video_sender_loop(self) -> None:
        """Drain _video_queue and send binary WebSocket frames.

        Runs on a dedicated thread so slow sends never block the caller.
        Drops frames during reconnect rather than queuing stale video.
        """
        while not self._stop.is_set():
            try:
                payload = self._video_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not self._authenticated.is_set():
                continue  # drop during auth / reconnect
            with self._ws_lock:
                ws = self._ws
            if ws is None:
                continue
            try:
                ws.send_binary(payload)
            except Exception as e:
                logger.debug("plexus video send failed: %s", e)

    def _connect_and_serve(self) -> None:
        ws = websocket.create_connection(self.ws_url, timeout=AUTH_TIMEOUT_S)
        with self._ws_lock:
            self._ws = ws

        # 1. Send device_auth
        auth = {
            "type": "device_auth",
            "api_key": self.api_key,
            "source_id": self.source_id,
            "platform": self.platform,
            "agent_version": self.agent_version,
        }
        if self._commands:
            auth["commands"] = [c.to_manifest() for c in self._commands.values()]
        ws.send(json.dumps(auth))

        # 2. Wait for authenticated
        ws.settimeout(AUTH_TIMEOUT_S)
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException as e:
            raise TimeoutError("auth timeout") from e

        msg = _safe_json(raw)
        if msg.get("type") != "authenticated":
            raise RuntimeError(f"auth failed: {msg}")

        server_ts = msg.get("server_time_ms")
        if isinstance(server_ts, (int, float)) and server_ts > 0:
            self._clock_offset_ms = int(server_ts) - int(time.time() * 1000)
            if self._on_clock_synced is not None:
                try:
                    self._on_clock_synced(self._clock_offset_ms)
                except Exception as e:
                    logger.debug("on_clock_synced callback raised: %s", e)

        was_reconnect = self._backoff_attempt > 0
        self._authenticated.set()
        self._backoff_attempt = 0
        logger.info("plexus ws authenticated as %s", self.source_id)
        if was_reconnect:
            _say(f"✓ Reconnected as {self.source_id}")
        else:
            _say(f"✓ Connected to gateway as {self.source_id}")

        # 3. Read loop with heartbeat pump
        ws.settimeout(1.0)
        last_heartbeat = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                self._send_frame({
                    "type": "heartbeat",
                    "source_id": self.source_id,
                    "agent_version": self.agent_version,
                })
                last_heartbeat = now

            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketConnectionClosedException, OSError):
                logger.info("plexus ws closed")
                return

            if not raw:
                continue
            self._dispatch(_safe_json(raw))

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "typed_command":
            self._handle_command(msg)
        elif mtype == "error":
            logger.warning("plexus ws server error: %s", msg.get("detail") or msg)
        # ignore unknown types — forward-compat

    def _handle_command(self, msg: Dict[str, Any]) -> None:
        cmd_id = msg.get("id") or ""
        command = msg.get("command") or ""
        params = msg.get("params") or {}

        # Ack immediately (matches C SDK: plexus_ws.c:275-280)
        self._send_frame({
            "type": "command_result",
            "id": cmd_id,
            "command": command,
            "event": "ack",
        })

        reg = self._commands.get(command)
        if reg is None:
            self._send_frame({
                "type": "command_result",
                "id": cmd_id,
                "command": command,
                "event": "error",
                "error": f"unknown command: {command}",
            })
            return

        # Run the handler off the read-loop thread so a slow handler doesn't
        # block heartbeats or other inbound frames.
        threading.Thread(
            target=self._run_handler,
            args=(reg, cmd_id, command, params),
            daemon=True,
        ).start()

    def _run_handler(
        self,
        reg: _RegisteredCommand,
        cmd_id: str,
        command: str,
        params: Dict[str, Any],
    ) -> None:
        try:
            result = reg.handler(command, params)
        except Exception as e:
            self._send_frame({
                "type": "command_result",
                "id": cmd_id,
                "command": command,
                "event": "error",
                "error": str(e),
            })
            return
        self._send_frame({
            "type": "command_result",
            "id": cmd_id,
            "command": command,
            "event": "result",
            "result": result if result is not None else {},
        })

    def _send_frame(self, frame: Dict[str, Any]) -> bool:
        with self._ws_lock:
            ws = self._ws
        if ws is None:
            return False
        try:
            ws.send(json.dumps(frame))
            return True
        except Exception as e:
            logger.debug("plexus ws send failed: %s", e)
            return False


# --------------------------------------------------------------------- helpers


def _encode_binary_video_frame(
    source_id: str,
    camera_id: str,
    jpeg_bytes: bytes,
    width: int,
    height: int,
    timestamp_ms: int,
) -> bytes:
    """Pack a video frame into the binary wire format.

    Wire layout:
        [0x01]          1 byte   version
        [src_len]       1 byte   source_id byte length (capped at 255)
        [source_id]     N bytes
        [cam_len]       1 byte   camera_id byte length (capped at 255)
        [camera_id]     M bytes
        [width]         4 bytes  uint32 big-endian
        [height]        4 bytes  uint32 big-endian
        [timestamp_ms]  8 bytes  int64  big-endian
        [jpeg_bytes]    rest
    """
    src = source_id.encode("utf-8")[:255]
    cam = camera_id.encode("utf-8")[:255]
    header = (
        bytes([0x01, len(src)]) + src
        + bytes([len(cam)]) + cam
        + struct.pack(">IIq", width, height, timestamp_ms)
    )
    return header + jpeg_bytes


def _ensure_device_path(url: str) -> str:
    url = url.rstrip("/")
    if url.endswith("/ws/device"):
        return url
    return url + "/ws/device"


def _safe_json(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with ±25% jitter, capped at BACKOFF_MAX_S.
    Matches plexus_ws.c:44-52."""
    base = min(BACKOFF_BASE_S * (2 ** attempt), BACKOFF_MAX_S)
    jitter = base * 0.25 * (2 * random.random() - 1)
    return max(0.1, base + jitter)
