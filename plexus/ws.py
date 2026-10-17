"""
WebSocket transport for the Plexus Python SDK.

Implements the gateway's device wire protocol. Targets its
`/ws/device` endpoint and exchanges the same JSON frames:

    client → {"type": "device_auth", "api_key": ..., "source_id": ...,
              "platform": "python-sdk", "agent_version": ...,
              "commands": [...]}
    server → {"type": "authenticated", "source_id": ..., "server_time_ms": ...}

The gateway echoes the declared `source_id` back unchanged; the client
uses the `authenticated` frame only for the `server_time_ms` clock sync.
(install_id and server-side source_id auto-suffixing were removed in 0.7.1.)
    client → {"type": "telemetry", "points": [...]}
    client → {"type": "heartbeat", "source_id": ..., "agent_version": ...}   # every 30s
    server → {"type": "typed_command", "id": ..., "command": ..., "params": {...}}
    client → {"type": "command_result", "id": ..., "command": ..., "event": "ack"}
    client → {"type": "command_result", "id": ..., "command": ...,
              "event": "result" | "error", "result": {...} | "error": "..."}
    server → {"type": "error", "code": "RATE_LIMITED" | ..., "detail": "..."}

Commands protocol v1 (`@px.command`) runs alongside that legacy pair:

    client → device_auth ... "protocol": 1, "commands": [{name, title, params,
              danger, idempotent, expires_in_s, concurrency}, ...]
    server → {"type": "authenticated", ..., "protocol": 1, "max_frame_bytes": int}
              (`store_results: false` is honoured if present; the gateway does
              not send it today, so results are always sent)
    server → {"type": "command_run", "run_id": ..., "command": ..., "params": {...},
              "ttl_ms": ..., "attempt": 1}
    client → {"type": "command_status", "run_id": ..., "seq": 1,
              "state": "acknowledged" | "running" | "progress" | "succeeded" | "failed",
              "result": {...}?, "error_code": ...?, "error_message": ...?}
    server → {"type": "command_status_ack", "run_id": ..., "seq": ...}
    client → {"type": "command_sync", "runs": [{"run_id", "state", "seq"}, ...]}

`ttl_ms` is what remains *at send*, so expiry is judged against this device's
monotonic clock and never against its wall clock — the two disagree badly on a
device that boots without NTP. A status that could not be sent, or that nobody
acknowledged, is kept and replayed after the next reconnect, behind a
`command_sync` that tells the server which runs this client still knows about.

Runs the read loop on a background daemon thread so callers can stay sync.

Server `error` frames are forwarded to `on_server_error` rather than only
logged. They are the sole notice that a frame the socket already accepted was
dropped upstream — `send()` returned True long before the gateway decided —
so a caller that cannot see them cannot know it is losing data.

Telemetry acknowledgements (0.16.0). A local socket write is not delivery: a
gateway parked in a Redis retry stops reading, and behind a proxy the
connection stays open for seconds after the gateway dies, so frames written
then were counted as sent and were gone (2026-10-17: ~57,000 readings from
one device). A gateway that acknowledges says so in `authenticated`:

    client → device_auth ... "acks": true
    server → {"type": "authenticated", ..., "acks": true}
    client → {"type": "telemetry", "seq": N, "points": [...]}   # N from 1, per connection
    server → {"type": "telemetry_ack", "seq": N}                 # stored; covers all ≤ N
    server → {"type": "error", "code": ..., "seq": N}            # handled, not stored,
                                                                  # do not resend; covers ≤ N

Rules: without `acks` in `authenticated` nothing changes (no seq, no window;
byte-identical to 0.15.1). With it, every frame is held in a window until the
gateway acknowledges it; the window is bounded (ACK_WINDOW_FRAMES /
ACK_WINDOW_POINTS) and a full window makes send_points() return False so the
caller takes the HTTP path. Whatever is still in the window when the connection
ends — a close, a dead socket, or no ack for ACK_TIMEOUT_S — goes to
`on_unacked`, which the client puts back in its local buffer to send first on
the next connection, with the original timestamps. `error REDIS_UNAVAILABLE`
carries no seq and is followed by close 1013: nothing is released, everything
is replayed. A frame stored but not yet acknowledged when the connection dies
is sent twice; duplicates are preferred to loss.
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
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

try:
    import websocket  # websocket-client
except ImportError as e:  # pragma: no cover - import-time failure is obvious
    raise ImportError(
        "WebSocket transport requires 'websocket-client'. "
        "Install with: pip install websocket-client"
    ) from e

from plexus._log import _say
from plexus.commands import (
    Cancelled,
    CommandParamError,
    CommandRun,
    DeclaredCommand,
    coerce_params,
)

logger = logging.getLogger(__name__)

AUTH_TIMEOUT_S = 10.0
HEARTBEAT_INTERVAL_S = 30.0
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0

#: Commands wire protocol this client speaks. Advertised in `device_auth`.
PROTOCOL_VERSION = 1

#: Run ids (and their last known state) kept for de-duplication. A redelivered
#: run id is re-reported from here; its handler never runs twice.
MAX_REMEMBERED_RUNS = 256

#: Statuses held while nobody has acknowledged them. Bounded: a client that
#: never reaches the server must not grow without limit. The oldest go first,
#: loudly.
MAX_UNACKED_STATUSES = 256

#: Telemetry frames held while the gateway has not acknowledged them. When
#: either bound is reached send_points() returns False and the caller sends
#: over HTTP instead; nothing blocks. 32 frames of 5,000 points is more than a
#: healthy connection ever has in flight; 10,000 points is one gateway batch.
ACK_WINDOW_FRAMES = 32
ACK_WINDOW_POINTS = 10_000

#: Oldest unacknowledged frame older than this means the connection is dead
#: even if the socket says otherwise (parked gateway, proxy keeping TCP open).
#: The socket is dropped, the window is spilled to the local buffer, and the
#: transport reconnects.
ACK_TIMEOUT_S = 30.0

#: Ceiling for one `command_status` frame. The gateway drops anything larger,
#: so an oversized result is stripped here — losing the payload beats losing
#: the outcome.
MAX_STATUS_FRAME_BYTES = 64 * 1024

#: States that end a run. Nothing follows one for the same run id.
FINAL_STATES = ("succeeded", "failed")

CommandHandler = Callable[[str, dict[str, Any]], dict[str, Any] | None]


@dataclass
class _RegisteredCommand:
    name: str
    handler: CommandHandler
    description: str | None = None
    params: list[dict[str, Any]] = field(default_factory=list)
    # "accept" (default): run overlapping invocations concurrently.
    # "reject": refuse a new invocation with an error result while a
    # previous one of the same command is still running.
    concurrency: str = "accept"
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_manifest(self) -> dict[str, Any]:
        m: dict[str, Any] = {"name": self.name}
        if self.description:
            m["description"] = self.description
        if self.params:
            m["params"] = self.params
        return m


@dataclass
class _RunRecord:
    """What this client remembers about one run id.

    Kept after the run finishes so a redelivery can be answered from memory
    instead of running the handler a second time.
    """

    run_id: str
    state: str | None = None
    seq: int = 0
    final: dict[str, Any] | None = None


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
        on_clock_synced: Callable[[int], None] | None = None,
        on_server_error: Callable[[str, str], None] | None = None,
        on_unacked: Callable[[list[dict[str, Any]]], None] | None = None,
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
        self._on_server_error = on_server_error
        # Called with the points of every telemetry frame the gateway had not
        # acknowledged when a connection ended. The client puts them back in
        # its local buffer; they go out first on the next connection.
        self._on_unacked = on_unacked

        # Telemetry acknowledgements. Only active when the gateway answered
        # `authenticated` with `acks: true`; otherwise this is 0.15.1.
        self._acks_enabled = False
        self._seq = 0
        # seq → (monotonic sent_at, points), in seq order. Guarded by
        # _window_cv, which is also notified on every release so stop() can
        # wait for the last acknowledgements.
        self._window: OrderedDict[int, tuple[float, list[dict[str, Any]]]] = OrderedDict()
        self._window_points = 0
        self._window_cv = threading.Condition()

        self._commands: dict[str, _RegisteredCommand] = {}
        # Commands protocol v1: declarations, what we remember about run ids,
        # and the statuses nobody has acknowledged yet.
        self._declared: dict[str, DeclaredCommand] = {}
        self._command_lock = threading.Lock()
        self._runs: OrderedDict[str, _RunRecord] = OrderedDict()
        self._unacked: deque[dict[str, Any]] = deque()
        self._store_results = True
        self._max_status_bytes = MAX_STATUS_FRAME_BYTES
        self._ws: websocket.WebSocket | None = None
        self._ws_lock = threading.Lock()
        self._authenticated = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._backoff_attempt = 0
        self._closed_by_gateway = False
        self._clock_offset_ms: int = 0
        self._video_queue: queue.Queue[bytes] = queue.Queue(maxsize=2)
        self._video_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ public

    def register_command(
        self,
        name: str,
        handler: CommandHandler,
        *,
        description: str | None = None,
        params: list[dict[str, Any]] | None = None,
        concurrency: str = "accept",
    ) -> None:
        """Register a legacy command handler, answered over `typed_command`.

        Legacy handlers are not advertised in the auth frame: a protocol-1
        manifest carries `@px.command` declarations only (see
        declare_command()).

        concurrency controls what happens when a command arrives while a
        previous invocation of the *same* command is still running:
            "accept" (default) — run it concurrently on a new thread.
            "reject"           — refuse it with an error result until the
                                 in-flight invocation finishes.
        """
        if concurrency not in ("accept", "reject"):
            raise ValueError(
                f"concurrency must be 'accept' or 'reject', got {concurrency!r}"
            )
        self._commands[name] = _RegisteredCommand(
            name=name, handler=handler, description=description,
            params=params or [], concurrency=concurrency,
        )

    def declare_command(self, declaration: DeclaredCommand) -> None:
        """Declare a protocol-v1 command (`@px.command`).

        Advertised in the auth frame, so declare before start() — or before
        the first send(), which starts the transport for you.
        """
        self._declared[declaration.name] = declaration

    @property
    def declared_command_names(self) -> list[str]:
        return list(self._declared)

    @property
    def store_results(self) -> bool:
        """False only if the gateway sent `store_results: false`. It does not
        send that field today, so this is True and results are sent."""
        return self._store_results

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

    def stop(self, timeout: float = 2.0, *, ack_timeout: float | None = None) -> None:
        """Close the connection and join the threads.

        Waits up to `ack_timeout` (default: `timeout`) for the gateway to
        acknowledge the frames still in the window before closing, so a
        normal exit does not turn the last batch into a replay. Whatever is
        still unacknowledged after that goes to `on_unacked` before this
        returns, while the caller's buffer is still open.
        """
        self.wait_for_acks(timeout if ack_timeout is None else ack_timeout)
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
        # The read loop spills on its way out; this covers a join that timed
        # out. Taking an empty window is a no-op, so doing it twice is safe.
        self._spill_window()

    def wait_authenticated(self, timeout: float = AUTH_TIMEOUT_S) -> bool:
        return self._authenticated.wait(timeout=timeout)

    def wait_for_acks(self, timeout: float) -> bool:
        """Block until every frame in the window is acknowledged, or `timeout`
        seconds pass. True if the window is empty on return. Returns at once
        when acknowledgements are off or nothing is outstanding."""
        deadline = time.monotonic() + timeout
        with self._window_cv:
            while self._window:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._window_cv.wait(remaining)
            return True

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated.is_set()

    @property
    def acks_enabled(self) -> bool:
        """True while connected to a gateway that acknowledges telemetry."""
        return self._acks_enabled

    @property
    def unacked_points(self) -> int:
        """Points sent on this connection that the gateway has not confirmed."""
        with self._window_cv:
            return self._window_points

    @property
    def reconnect_pending(self) -> bool:
        """True after a failed connect while the transport is backing off /
        retrying. Callers should not block waiting for auth in this state —
        no connection attempt may even be in flight."""
        return self._backoff_attempt > 0 and not self._authenticated.is_set()

    @property
    def clock_offset_ms(self) -> int:
        return self._clock_offset_ms

    def send_points(self, points: list[dict[str, Any]]) -> bool:
        """Send a telemetry frame. True means accepted for delivery: written to
        an authenticated socket and, when the gateway acknowledges, held until
        it confirms storage or handed to `on_unacked`. False means the caller
        must send these points another way (not authenticated, the window is
        full, or the local write failed); nothing is kept here in that case.
        """
        if not points:
            return True
        if not self._authenticated.is_set():
            return False
        if not self._acks_enabled:
            return self._send_frame({"type": "telemetry", "points": points})

        # Window first, send second (the same discipline as _emit_status): a
        # frame is never on the wire without an entry that can be replayed.
        with self._window_cv:
            if (
                len(self._window) >= ACK_WINDOW_FRAMES
                or self._window_points + len(points) > ACK_WINDOW_POINTS
            ):
                return False
            self._seq += 1
            seq = self._seq
            self._window[seq] = (time.monotonic(), points)
            self._window_points += len(points)

        if self._send_frame({"type": "telemetry", "seq": seq, "points": points}):
            return True
        # The local write failed: forget the entry so the caller's HTTP send
        # is the only copy. If the connection ended in between and the entry
        # was already spilled to the buffer, that copy is the one that counts.
        with self._window_cv:
            entry = self._window.pop(seq, None)
            if entry is None:
                return True
            self._window_points -= len(entry[1])
            self._window_cv.notify_all()
        return False

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

    def send_json_video_frame(self, msg: dict[str, Any]) -> bool:
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
                        _say("  Check the key is active at https://app.plexus.company/api.")
                    else:
                        _say(f"✗ Connection failed: {msg}")
                        _say("  SDK will keep retrying with backoff.")
            finally:
                self._authenticated.clear()
                with self._ws_lock:
                    self._ws = None
                # Nothing more can be acknowledged on this connection. What
                # the gateway did not confirm goes back to the local buffer.
                self._spill_window()

            if not self.auto_reconnect or self._stop.is_set():
                break

            if self._closed_by_gateway:
                # A clean goodbye means another gateway is already up: come
                # straight back (spread out, so a fleet does not arrive as one).
                self._closed_by_gateway = False
                delay = random.uniform(0.1, 0.5)
            else:
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
        auth: dict[str, Any] = {
            "type": "device_auth",
            "api_key": self.api_key,
            "source_id": self.source_id,
            "platform": self.platform,
            "agent_version": self.agent_version,
            "protocol": PROTOCOL_VERSION,
            # We stop using the socket the moment the gateway sends a close,
            # so it may say goodbye to us before a deploy (see the read loop).
            "hears_goodbye": True,
            # We hold every telemetry frame until it is acknowledged, so the
            # gateway may answer each one with telemetry_ack (module docstring).
            "acks": True,
        }
        # Only v1 declarations are advertised. The gateway validates every
        # entry of a protocol-1 manifest as v1 and would answer a legacy
        # on_command entry with an invalid_command error on every connect.
        # Legacy handlers still answer typed_command; nothing ever read their
        # manifest, so leaving them out loses nothing.
        if self._declared:
            auth["commands"] = [c.to_manifest() for c in self._declared.values()]
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

        # Per-org result storage. Absent means "stored": a gateway that has not
        # shipped this field yet must not silently turn results off.
        self._store_results = msg.get("store_results", True) is not False
        max_bytes = msg.get("max_frame_bytes")
        if isinstance(max_bytes, int) and 0 < max_bytes < MAX_STATUS_FRAME_BYTES:
            self._max_status_bytes = max_bytes

        # Acknowledgements are per connection: seq starts again at 1, and the
        # gateway opts in each time. Absent means an older gateway, and the
        # frames go out exactly as they did in 0.15.1. Anything a late sender
        # slipped into the window during the reconnect is spilled, not reused.
        self._spill_window()
        with self._window_cv:
            self._acks_enabled = msg.get("acks") is True
            self._seq = 0

        was_reconnect = self._backoff_attempt > 0
        self._authenticated.set()
        self._backoff_attempt = 0
        logger.info("plexus ws authenticated as %s", self.source_id)
        if was_reconnect:
            _say(f"✓ Reconnected as {self.source_id}")
        else:
            _say(f"✓ Connected to gateway as {self.source_id}")

        # Tell the server which runs we still know about, then re-send every
        # status it has not acknowledged.
        self._resync_commands()

        # 3. Read loop with heartbeat pump
        ws.settimeout(1.0)
        last_heartbeat = time.monotonic()
        while not self._stop.is_set():
            # recv_data() returns at least once a second, so this runs at
            # least that often: an acknowledgement that is ACK_TIMEOUT_S late
            # means the socket is dead whatever TCP says.
            if self._ack_watchdog(ws):
                return
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                self._send_frame({
                    "type": "heartbeat",
                    "source_id": self.source_id,
                    "agent_version": self.agent_version,
                })
                last_heartbeat = now

            try:
                with ws.readlock:
                    opcode, data = ws.recv_data()
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketConnectionClosedException, OSError):
                logger.info("plexus ws closed")
                return

            if opcode == websocket.ABNF.OPCODE_CLOSE:
                # The gateway said it is going away (a deploy). Stop using this
                # socket NOW. `recv()` hides a close message, and the
                # connection itself can stay open for seconds behind a proxy:
                # every reading written in that time was accepted locally and
                # never arrived (measured 2026-10-05: 2 to 14 seconds lost per
                # gateway deploy). Clearing the flag here, not in the caller's
                # `finally`, closes the gap to the very next send().
                self._authenticated.clear()
                self._closed_by_gateway = True
                logger.info("plexus ws closed by the gateway")
                # The close has been answered (recv_data did that). Drop the
                # socket ourselves rather than wait for the far end to.
                try:
                    ws.shutdown()
                except Exception:  # noqa: BLE001 - already gone is fine
                    pass
                return
            if opcode != websocket.ABNF.OPCODE_TEXT or not data:
                continue
            raw = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
            self._dispatch(_safe_json(raw))

    def _dispatch(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "typed_command":
            self._handle_command(msg)
        elif mtype == "command_run":
            self._handle_command_run(msg, time.monotonic())
        elif mtype == "command_status_ack":
            self._handle_status_ack(msg)
        elif mtype == "telemetry_ack":
            seq = msg.get("seq")
            if _is_seq(seq):
                self._release_through(seq)
        elif mtype == "error":
            code = str(msg.get("code") or "")
            detail = str(msg.get("detail") or "")
            logger.warning("plexus ws server error: %s %s", code or "?", detail or msg)
            # An error that names a seq is the gateway's answer to that frame:
            # handled, not stored, do not resend (a rate or plan limit, or a
            # frame it could not parse). REDIS_UNAVAILABLE carries no seq, so
            # the frames stay in the window and are replayed after the close
            # that follows it.
            seq = msg.get("seq")
            if _is_seq(seq):
                self._release_through(seq)
            if self._on_server_error is not None:
                try:
                    self._on_server_error(code, detail)
                except Exception:  # never let a callback kill the read loop
                    logger.exception("on_server_error callback raised")
        # ignore unknown types — forward-compat

    def _handle_command(self, msg: dict[str, Any]) -> None:
        cmd_id = msg.get("id") or ""
        command = msg.get("command") or ""
        params = msg.get("params") or {}

        # Ack immediately, per the gateway wire contract
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

        # concurrency="reject": if an invocation is already running, refuse
        # this one immediately rather than starting a second in parallel.
        holds_lock = False
        if reg.concurrency == "reject":
            if not reg._lock.acquire(blocking=False):
                self._send_frame({
                    "type": "command_result",
                    "id": cmd_id,
                    "command": command,
                    "event": "error",
                    "error": f"command already in progress: {command}",
                })
                return
            holds_lock = True

        # Run the handler off the read-loop thread so a slow handler doesn't
        # block heartbeats or other inbound frames.
        try:
            threading.Thread(
                target=self._run_handler,
                args=(reg, cmd_id, command, params, holds_lock),
                daemon=True,
            ).start()
        except Exception as e:
            # Thread creation failed (e.g. resource exhaustion). Release the
            # concurrency lock so the command isn't wedged as "in progress",
            # and report the failure rather than leaving it silently un-acked.
            if holds_lock:
                reg._lock.release()
            self._send_frame({
                "type": "command_result",
                "id": cmd_id,
                "command": command,
                "event": "error",
                "error": f"failed to start command handler: {e}",
            })

    def _run_handler(
        self,
        reg: _RegisteredCommand,
        cmd_id: str,
        command: str,
        params: dict[str, Any],
        holds_lock: bool = False,
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
        finally:
            # Release the concurrency lock (if held) as soon as the handler
            # returns, regardless of success or failure.
            if holds_lock:
                reg._lock.release()
        self._send_frame({
            "type": "command_result",
            "id": cmd_id,
            "command": command,
            "event": "result",
            "result": result if result is not None else {},
        })

    # -------------------------------------------------------- commands v1

    def _handle_command_run(self, msg: dict[str, Any], arrived: float) -> None:
        """Answer a `command_run` frame. Runs on the read-loop thread.

        Everything here is a fast check; the handler itself goes to its own
        thread so a slow one can't stall heartbeats or the next frame.

        `arrived` is a `time.monotonic()` reading taken the moment the frame
        came off the socket. Expiry is measured from it, so a wall-clock jump
        (NTP landing, an RTC-less device) can neither expire a live run nor
        revive a dead one.
        """
        run_id = msg.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            logger.warning("plexus ws command_run without a run_id, ignored")
            return
        name = msg.get("command") or ""
        attempt = msg.get("attempt")
        attempt = attempt if isinstance(attempt, int) and attempt > 0 else 1

        # 1. De-duplication. Reserve the id before anything can fail, so a
        #    redelivery that overtakes our own answer is still caught.
        with self._command_lock:
            known = self._runs.get(run_id)
            if known is None:
                self._remember(run_id)
        if known is not None:
            self._rereport(known)
            return

        # 2. Is it ours?
        reg = self._declared.get(name)
        if reg is None:
            self._reject(run_id, "unknown_command", f"unknown command: {name}")
            return

        # 3. Params, checked against the declaration the dashboard drew from.
        try:
            params = coerce_params(reg.schema, msg.get("params"))
        except CommandParamError as e:
            self._reject(run_id, "bad_params", str(e))
            return

        # 4. Expiry, measured against the monotonic clock from arrival.
        ttl_ms = msg.get("ttl_ms")
        deadline: float | None = None
        if isinstance(ttl_ms, (int, float)) and not isinstance(ttl_ms, bool):
            if ttl_ms <= 0:
                self._reject(
                    run_id, "expired_on_arrival",
                    f"run expired before it arrived (ttl_ms={ttl_ms})",
                )
                return
            deadline = arrived + (float(ttl_ms) / 1000.0)

        # 5. concurrency="reject": one run of this command at a time.
        holds_lock = False
        if reg.concurrency == "reject":
            if not reg._lock.acquire(blocking=False):
                self._reject(run_id, "busy", f"command already running: {reg.name}")
                return
            holds_lock = True

        run = CommandRun(
            id=run_id,
            command=reg.name,
            params=params,
            attempt=attempt,
            deadline=deadline,
            _emit=self._emit_status,
        )
        self._emit_status(run_id, "acknowledged")
        try:
            threading.Thread(
                target=self._run_declared,
                args=(reg, run, holds_lock),
                name=f"plexus-command-{reg.name}",
                daemon=True,
            ).start()
        except Exception as e:
            # No thread means no handler. Release the concurrency lock so the
            # command isn't wedged, and report the failure rather than leaving
            # the run acknowledged forever.
            if holds_lock:
                reg._lock.release()
            self._reject(run_id, "busy", f"could not start handler thread: {e}")

    def _run_declared(
        self, reg: DeclaredCommand, run: CommandRun, holds_lock: bool
    ) -> None:
        """Run one handler on its own thread and report how it went."""
        self._emit_status(run.id, "running")
        try:
            result = reg.handler(run, **run.params)
        except Cancelled as e:
            self._emit_status(
                run.id, "failed", error_code="cancelled",
                error_message=str(e) or "cancelled by the handler",
            )
        except Exception as e:
            logger.exception("plexus command %s failed", reg.name)
            self._emit_status(
                run.id, "failed", error_code="handler_error",
                error_message=f"{type(e).__name__}: {e}",
            )
        else:
            self._emit_status(run.id, "succeeded", result=_as_result(result))
        finally:
            if holds_lock:
                reg._lock.release()

    def _reject(self, run_id: str, code: str, message: str) -> None:
        """Refuse a run before it starts. Always `failed` plus a code."""
        self._emit_status(run_id, "failed", error_code=code, error_message=message)

    def _emit_status(self, run_id: str, state: str, **fields: Any) -> bool:
        """Send one `command_status`, buffering it until the server acks it.

        `seq` counts up per run. The frame is buffered first and sent second,
        so a status produced while the socket is down survives to the next
        reconnect instead of vanishing the way `command_result` does.
        """
        frame: dict[str, Any] = {
            "type": "command_status",
            "run_id": run_id,
            "seq": 0,  # set under the lock below
            "state": state,
        }
        for key, value in fields.items():
            if value is None:
                continue
            # metadata_only orgs: the state and the code travel, customer bytes
            # do not. Stripped here so no code path can leak them.
            if not self._store_results and key in ("result", "error_message", "message"):
                continue
            frame[key] = value
        with self._command_lock:
            record = self._runs.get(run_id) or self._remember(run_id)
            record.seq += 1
            frame["seq"] = record.seq
            # Sized after seq is set: it is part of the frame the gateway measures.
            frame = self._fit_status(frame)
            if state in FINAL_STATES:
                record.final = dict(frame)
            # progress is advisory: it doesn't move the run's state, and it
            # never pushes a real status out of the replay buffer.
            if state != "progress":
                record.state = state
                if len(self._unacked) >= MAX_UNACKED_STATUSES:
                    dropped = self._unacked.popleft()
                    logger.warning(
                        "plexus dropping unacknowledged command_status "
                        "(run %s seq %s) — %d already waiting",
                        dropped.get("run_id"), dropped.get("seq"), MAX_UNACKED_STATUSES,
                    )
                self._unacked.append(frame)

        return self._send_frame(frame)

    def _remember(self, run_id: str) -> _RunRecord:
        """Start remembering a run id, forgetting the oldest past the bound.
        Caller holds `_command_lock`."""
        record = self._runs[run_id] = _RunRecord(run_id=run_id)
        while len(self._runs) > MAX_REMEMBERED_RUNS:
            self._runs.popitem(last=False)
        return record

    def _fit_status(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Make sure the status can actually go on the wire.

        A handler that returns something JSON can't carry, or a result larger
        than the gateway accepts, must not cost the run its outcome — the
        payload is described or dropped, the state always travels.
        """
        try:
            size = len(json.dumps(frame))
        except (TypeError, ValueError):
            logger.warning(
                "plexus command result for run %s is not JSON-serialisable — "
                "sending its repr instead",
                frame.get("run_id"),
            )
            frame = dict(frame)
            if "result" in frame:
                frame["result"] = {"value": repr(frame["result"])[:512]}
            return frame
        if size <= self._max_status_bytes:
            return frame
        trimmed = {k: v for k, v in frame.items() if k not in ("result", "message")}
        logger.warning(
            "plexus command_status for run %s exceeded %d bytes — result dropped",
            frame.get("run_id"), self._max_status_bytes,
        )
        if len(json.dumps(trimmed)) > self._max_status_bytes:
            trimmed.pop("error_message", None)
        return trimmed

    def _rereport(self, record: _RunRecord) -> None:
        """Answer a redelivered run id from memory. The handler never reruns."""
        if record.final is not None:
            replay = dict(record.final)
            self._emit_status(
                record.run_id,
                replay["state"],
                result=replay.get("result"),
                error_code=replay.get("error_code"),
                error_message=replay.get("error_message"),
            )
            return
        if record.state:
            # Still in flight. Re-report where it actually is rather than
            # inventing an outcome for a job that is still running.
            self._emit_status(record.run_id, record.state)
            return
        # Known id, no state yet: two deliveries raced into dispatch. Refuse
        # the second one — the first is about to answer for both.
        self._reject(record.run_id, "duplicate", f"run {record.run_id} is already being handled")

    def _handle_status_ack(self, msg: dict[str, Any]) -> None:
        """`command_status_ack` — drop buffered statuses up to `seq`."""
        run_id = msg.get("run_id")
        seq = msg.get("seq")
        if not isinstance(run_id, str) or not isinstance(seq, int):
            return
        with self._command_lock:
            self._unacked = deque(
                f for f in self._unacked
                if not (f.get("run_id") == run_id and f.get("seq", 0) <= seq)
            )

    def _resync_commands(self) -> None:
        """After auth: say what we remember, then replay what wasn't acked."""
        with self._command_lock:
            runs = [
                {"run_id": r.run_id, "state": r.state, "seq": r.seq}
                for r in self._runs.values()
                if r.state
            ]
            pending = list(self._unacked)
        if runs:
            self._send_frame({"type": "command_sync", "runs": runs})
        for frame in pending:
            self._send_frame(frame)

    def _send_frame(self, frame: dict[str, Any]) -> bool:
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

    # ------------------------------------------------------ telemetry acks

    def _release_through(self, seq: int) -> None:
        """The gateway has dealt with every frame up to and including `seq`."""
        with self._window_cv:
            while self._window:
                oldest = next(iter(self._window))
                if oldest > seq:
                    break
                _seq, (_sent_at, points) = self._window.popitem(last=False)
                self._window_points -= len(points)
            self._window_cv.notify_all()

    def _spill_window(self) -> None:
        """Hand everything still unacknowledged to `on_unacked`, oldest first.

        Called when a connection ends, for whatever reason. The points keep
        their original timestamps; the client's buffer sends them before
        anything new. Safe to call with an empty window.
        """
        with self._window_cv:
            if not self._window:
                return
            points = [p for _sent_at, pts in self._window.values() for p in pts]
            self._window.clear()
            self._window_points = 0
            self._window_cv.notify_all()
        if self._on_unacked is None:
            logger.warning(
                "plexus ws: %d unacknowledged points dropped (no on_unacked handler)",
                len(points),
            )
            return
        logger.info(
            "plexus ws: %d unacknowledged points returned to the local buffer", len(points)
        )
        try:
            self._on_unacked(points)
        except Exception:  # never let a callback kill the connection loop
            logger.exception("on_unacked callback raised")

    def _ack_watchdog(self, ws: websocket.WebSocket) -> bool:
        """Drop the connection if the oldest frame has waited ACK_TIMEOUT_S.

        Runs on the read loop. Returns True when it did, and the caller must
        leave the loop: the window is then spilled by `_run` and the transport
        reconnects. Mirrors the goodbye handling — the flag is cleared here so
        the very next send() takes the other path.
        """
        if not self._acks_enabled:
            return False
        with self._window_cv:
            if not self._window:
                return False
            sent_at = next(iter(self._window.values()))[0]
        age = time.monotonic() - sent_at
        if age < ACK_TIMEOUT_S:
            return False
        logger.warning("plexus ws: no telemetry_ack for %.0fs, reconnecting", age)
        self._authenticated.clear()
        try:
            ws.shutdown()
        except Exception:  # noqa: BLE001 - already gone is fine
            pass
        return True


# --------------------------------------------------------------------- helpers


def _as_result(value: Any) -> dict[str, Any]:
    """Normalise a handler's return value into the `result` object.

    A dict goes as-is, None becomes `{}`, and anything else is wrapped so a
    handler that returns a bare number or string still reports something the
    dashboard can show.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return {"value": value}


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


def _is_seq(value: Any) -> bool:
    """A frame sequence number: an int, and not the bool that is also an int."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _safe_json(raw: Any) -> dict[str, Any]:
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
    """Exponential backoff with ±25% jitter, capped at BACKOFF_MAX_S."""
    base = min(BACKOFF_BASE_S * (2 ** attempt), BACKOFF_MAX_S)
    jitter = base * 0.25 * (2 * random.random() - 1)
    return max(0.1, base + jitter)
