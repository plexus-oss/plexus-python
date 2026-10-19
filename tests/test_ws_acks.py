"""Telemetry acknowledgements: a reading is not sent until the gateway says so.

On 2026-10-17 Fly rebooted every host. The gateway sat in a Redis retry and
stopped reading its sockets; behind the proxy the TCP connections stayed open.
The SDK wrote frames into them, `ws.send` succeeded locally, and each frame
was counted as delivered and dropped from the buffer. A device at 65 readings
a second lost about 57,000 readings. These tests pin the protocol that closes
that gap, frame by frame, against a stub gateway:

    device_auth {acks: true} → authenticated {acks: true}
    telemetry {seq: N} → telemetry_ack {seq: N}  (covers all ≤ N)
    error {seq: N}                                (handled, not stored, not resent)
    error REDIS_UNAVAILABLE (no seq) + close 1013 (nothing released, all replayed)

and what the SDK does with a connection that dies, stalls, or never
acknowledges: spill the window to the local buffer, reconnect, send the
spilled points first with their original timestamps.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest

websockets = pytest.importorskip("websockets")
from websockets.asyncio.server import serve  # noqa: E402

import plexus.ws as wsmod  # noqa: E402
from plexus.buffer import SqliteBuffer  # noqa: E402
from plexus.client import Plexus  # noqa: E402
from plexus.config import RetryConfig  # noqa: E402
from plexus.ws import WebSocketTransport  # noqa: E402


class _AckGateway:
    """The stub from test_ws.py, with acknowledgements.

    acks: answer `authenticated` with `acks: true`. False is a gateway from
        before this protocol existed.
    ack_mode: "all" acknowledges every telemetry frame (after `ack_delay`),
        "none" never does, "manual" only on `ack_sync()`.
    kill_after_frames: on the first connection, drop the TCP connection with
        no close frame the moment the N-th telemetry frame arrives. That frame
        is not acknowledged.
    redis_unavailable_after_frames: on the first connection, answer the N-th
        telemetry frame with `error REDIS_UNAVAILABLE` (no seq) and close 1013,
        which is what the real gateway does when Redis is down.
    """

    def __init__(
        self,
        *,
        acks: bool = True,
        ack_mode: str = "all",
        ack_delay: float = 0.0,
        kill_after_frames: int | None = None,
        redis_unavailable_after_frames: int | None = None,
    ):
        self.acks = acks
        self.ack_mode = ack_mode
        self.ack_delay = ack_delay
        self.kill_after_frames = kill_after_frames
        self.redis_unavailable_after_frames = redis_unavailable_after_frames
        self.auth_frames: list[dict[str, Any]] = []
        # Telemetry frames per connection, in arrival order.
        self.telemetry: list[list[dict[str, Any]]] = []
        self.connections = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._ws = None
        self._ready = threading.Event()
        self.port = 0

    @property
    def telemetry_frames(self) -> list[dict[str, Any]]:
        return [f for conn in self.telemetry for f in conn]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=3), "stub gateway did not start"

    def stop(self) -> None:
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread:
            self._thread.join(timeout=2)

    async def _handler(self, ws):
        conn = self.connections
        self.connections += 1
        self.telemetry.append([])
        self._ws = ws
        msg = json.loads(await ws.recv())
        self.auth_frames.append(msg)
        reply: dict[str, Any] = {
            "type": "authenticated",
            "source_id": msg.get("source_id"),
            "server_time_ms": int(time.time() * 1000),
        }
        if self.acks:
            reply["acks"] = True
        await ws.send(json.dumps(reply))
        seen = 0
        try:
            async for raw in ws:
                frame = json.loads(raw)
                if frame.get("type") != "telemetry":
                    continue
                self.telemetry[conn].append(frame)
                seen += 1
                if conn == 0 and seen == self.redis_unavailable_after_frames:
                    await ws.send(json.dumps({
                        "type": "error",
                        "code": "REDIS_UNAVAILABLE",
                        "detail": "redis: connection refused",
                    }))
                    await ws.close(code=1013, reason="try again later")
                    return
                if conn == 0 and seen == self.kill_after_frames:
                    ws.transport.abort()
                    return
                if self.acks and self.ack_mode == "all":
                    if self.ack_delay:
                        await asyncio.sleep(self.ack_delay)
                    await ws.send(json.dumps({"type": "telemetry_ack", "seq": frame["seq"]}))
        except websockets.ConnectionClosed:
            return

    def _send_sync(self, frame: dict[str, Any]) -> None:
        assert self._loop is not None and self._ws is not None
        fut = asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(frame)), self._loop)
        fut.result(timeout=2)

    def ack_sync(self, seq: int) -> None:
        self._send_sync({"type": "telemetry_ack", "seq": seq})

    def error_sync(self, code: str, *, seq: int | None = None, detail: str = "") -> None:
        frame: dict[str, Any] = {"type": "error", "code": code, "detail": detail}
        if seq is not None:
            frame["seq"] = seq
        self._send_sync(frame)

    def _run(self) -> None:
        async def main():
            self._server = await serve(self._handler, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._server.wait_closed()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(main())
        finally:
            self._loop.close()


@pytest.fixture
def make_gateway():
    made: list[_AckGateway] = []

    def make(**kwargs) -> _AckGateway:
        g = _AckGateway(**kwargs)
        g.start()
        made.append(g)
        return g

    yield make
    for g in made:
        g.stop()


def _wait_until(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _pt(metric: str, value: float, ts: int) -> dict[str, Any]:
    return {"class": "metric", "metric": metric, "value": value, "timestamp": ts}


def _transport(gateway: _AckGateway, **kwargs) -> WebSocketTransport:
    return WebSocketTransport(
        api_key="plx_test_abc",
        source_id="pump-01",
        ws_url=f"ws://127.0.0.1:{gateway.port}",
        **kwargs,
    )


def _client(gateway: _AckGateway, tmp_path) -> Plexus:
    """A real client with a real on-disk buffer. The HTTP gateway URL is the
    unroutable one conftest sets, so any accidental HTTP fallback fails fast
    and loudly instead of passing quietly."""
    return Plexus(
        api_key="plx_test_abc",
        source_id="pump-01",
        ws_url=f"ws://127.0.0.1:{gateway.port}",
        buffer_path=str(tmp_path / "buffer.db"),
        timeout=0.5,
        retry_config=RetryConfig(max_retries=0, base_delay=0.01, jitter=False),
    )


# ------------------------------------------------------------ (a) the window


def test_ack_releases_the_window_cumulatively(make_gateway):
    g = make_gateway(ack_mode="manual")
    t = _transport(g)
    t.start()
    try:
        assert t.wait_authenticated(timeout=3)
        assert g.auth_frames[0]["acks"] is True
        assert t.acks_enabled

        assert t.send_points([_pt("a", 1.0, 1)])
        assert t.send_points([_pt("b", 2.0, 2), _pt("b", 3.0, 3)])
        assert t.send_points([_pt("c", 4.0, 4), _pt("c", 5.0, 5), _pt("c", 6.0, 6)])
        assert _wait_until(lambda: len(g.telemetry_frames) == 3)
        assert [f["seq"] for f in g.telemetry_frames] == [1, 2, 3]
        assert g.telemetry_frames[0] == {
            "type": "telemetry", "seq": 1, "points": [_pt("a", 1.0, 1)],
        }
        assert t.unacked_points == 6

        # Ack 2 covers 1 and 2; only frame 3's points are still waiting.
        g.ack_sync(2)
        assert _wait_until(lambda: t.unacked_points == 3)
        assert t.wait_for_acks(0.05) is False

        g.ack_sync(3)
        assert t.wait_for_acks(2.0) is True
        assert t.unacked_points == 0
    finally:
        t.stop()


# ------------------------------------------- (b) the socket dies, no ack


def test_socket_death_spills_the_window_and_replays_it_first(make_gateway, tmp_path):
    """Frame 1 is acknowledged. Frame 2 reaches the gateway and the connection
    dies before the ack: exactly frame 2's points land in SQLite, and the
    first frame of the next connection carries them first, timestamps
    untouched, then the new reading."""
    g = make_gateway(kill_after_frames=2)
    px = _client(g, tmp_path)
    spilled: list[list[dict[str, Any]]] = []
    keep = px._on_unacked
    px._on_unacked = lambda points: (spilled.append(list(points)), keep(points))
    try:
        assert px.send("temp", 20.0, timestamp=1_700_000_000.0)
        assert px.send_batch([
            ("temp", 21.0, 1_700_000_001.0),
            ("rpm", 3000, 1_700_000_001.5),
        ])

        assert _wait_until(lambda: spilled)
        lost = spilled[0]
        assert [(p["metric"], p["value"], p["timestamp"]) for p in lost] == [
            ("temp", 21.0, 1_700_000_001_000),
            ("rpm", 3000, 1_700_000_001_500),
        ]
        assert spilled == [lost], "only the unacknowledged frame is spilled"
        assert px.buffer_size() == 2
        assert px._buffer.get_all() == lost

        assert _wait_until(lambda: g.connections == 2 and px._ws.is_authenticated, timeout=6)
        assert px.send("temp", 22.0, timestamp=1_700_000_002.0)
        assert _wait_until(lambda: len(g.telemetry[1]) >= 1)
        first = g.telemetry[1][0]
        assert first["seq"] == 1, "seq starts again on every connection"
        assert [(p["metric"], p["timestamp"]) for p in first["points"]] == [
            ("temp", 1_700_000_001_000),
            ("rpm", 1_700_000_001_500),
            ("temp", 1_700_000_002_000),
        ]
        assert px.buffer_size() == 0
        assert px._ws.wait_for_acks(2.0)
    finally:
        px.close()


# ------------------------------- (c1) Redis down: error without seq + 1013


def test_redis_unavailable_releases_nothing_and_replays_everything(make_gateway, tmp_path):
    g = make_gateway(redis_unavailable_after_frames=1)
    px = _client(g, tmp_path)
    try:
        assert px.send("temp", 20.0, timestamp=1_700_000_000.0)
        # The error carries no seq, so nothing is released: the close that
        # follows spills the frame to the buffer.
        assert _wait_until(lambda: px.buffer_size() == 1)
        assert px.rate_limited_frames == 0, "REDIS_UNAVAILABLE is not a drop"

        assert _wait_until(lambda: g.connections == 2 and px._ws.is_authenticated, timeout=6)
        assert px.send("temp", 21.0, timestamp=1_700_000_001.0)
        assert _wait_until(lambda: len(g.telemetry[1]) >= 1)
        assert [(p["value"], p["timestamp"]) for p in g.telemetry[1][0]["points"]] == [
            (20.0, 1_700_000_000_000),
            (21.0, 1_700_000_001_000),
        ]
        assert px._ws.wait_for_acks(2.0)
        assert px.buffer_size() == 0
    finally:
        px.close()


# ------------------------------------ (c2) a socket that is open but dead


def test_no_ack_for_too_long_drops_the_connection_and_spills(make_gateway, monkeypatch):
    """The 2026-10-17 shape: the socket stays open, the gateway never answers.
    The watchdog gives up after ACK_TIMEOUT_S, spills, and reconnects."""
    monkeypatch.setattr(wsmod, "ACK_TIMEOUT_S", 0.5)
    g = make_gateway(ack_mode="none")
    spilled: list[list[dict[str, Any]]] = []
    t = _transport(g, on_unacked=lambda points: spilled.append(list(points)))
    t.start()
    try:
        assert t.wait_authenticated(timeout=3)
        points = [_pt("temp", 1.0, 1), _pt("temp", 2.0, 2)]
        assert t.send_points(points)
        assert t.unacked_points == 2

        assert _wait_until(lambda: spilled, timeout=5)
        assert spilled == [points]
        assert t.unacked_points == 0
        assert _wait_until(lambda: g.connections == 2 and t.is_authenticated, timeout=6)
        assert t.acks_enabled
    finally:
        t.stop()


# ------------------------------------------- (d) a gateway without acks


def test_gateway_without_acks_behaves_exactly_like_0_15_1(make_gateway):
    g = make_gateway(acks=False, kill_after_frames=2)
    spilled: list[list[dict[str, Any]]] = []
    t = _transport(g, on_unacked=lambda points: spilled.append(list(points)))
    t.start()
    try:
        assert t.wait_authenticated(timeout=3)
        assert g.auth_frames[0]["acks"] is True, "the offer is always made"
        assert t.acks_enabled is False

        assert t.send_points([_pt("a", 1.0, 1)]) is True
        assert t.send_points([_pt("b", 2.0, 2)]) is True
        assert t.unacked_points == 0
        assert _wait_until(lambda: len(g.telemetry_frames) == 2)
        # The 0.15.1 frame, byte for byte: no seq.
        assert g.telemetry_frames[0] == {"type": "telemetry", "points": [_pt("a", 1.0, 1)]}
        assert g.telemetry_frames[1] == {"type": "telemetry", "points": [_pt("b", 2.0, 2)]}

        # The connection died after frame 2. There is no window to spill.
        assert _wait_until(lambda: g.connections == 2, timeout=6)
        assert spilled == []
        assert t.wait_for_acks(0.0) is True
    finally:
        t.stop()


# ---------------------------------------------- (e) a full window → HTTP


def test_full_window_sends_the_rest_over_http(make_gateway, tmp_path, monkeypatch):
    monkeypatch.setattr(wsmod, "ACK_WINDOW_FRAMES", 2)
    posted: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(Plexus, "_send_http", lambda self, points: posted.append(list(points)))
    g = make_gateway(ack_mode="none")
    px = _client(g, tmp_path)
    px._WS_FRAME_BYTE_BUDGET = 1  # every point becomes its own sub-frame
    try:
        assert px.send_batch([
            ("a", 1.0, 1_700_000_000.0),
            ("b", 2.0, 1_700_000_001.0),
            ("c", 3.0, 1_700_000_002.0),
        ])
        assert _wait_until(lambda: len(g.telemetry_frames) == 2)
        time.sleep(0.2)
        assert len(g.telemetry_frames) == 2, "the third sub-frame never touched the socket"
        assert [f["seq"] for f in g.telemetry_frames] == [1, 2]
        assert [p["metric"] for f in g.telemetry_frames for p in f["points"]] == ["a", "b"]
        assert [[p["metric"] for p in chunk] for chunk in posted] == [["c"]]
        assert px._ws.unacked_points == 2
        assert px._send_count == 3
    finally:
        px.close()


# ------------------------------------- (f) error with seq: released, counted


def test_error_with_seq_releases_the_frame_and_is_reported(make_gateway):
    g = make_gateway(ack_mode="manual")
    seen: list[tuple[str, str]] = []
    t = _transport(g, on_server_error=lambda code, detail: seen.append((code, detail)))
    t.start()
    try:
        assert t.wait_authenticated(timeout=3)
        assert t.send_points([_pt("a", 1.0, 1)])
        assert t.send_points([_pt("b", 2.0, 2)])
        assert _wait_until(lambda: len(g.telemetry_frames) == 2)
        assert t.unacked_points == 2

        g.error_sync("RATE_LIMITED", seq=1, detail="too many messages")
        assert _wait_until(lambda: seen == [("RATE_LIMITED", "too many messages")])
        assert t.unacked_points == 1, "frame 1 answered and released; frame 2 still waiting"

        # Without a seq an error answers nothing: the window is untouched.
        g.error_sync("REDIS_UNAVAILABLE", detail="redis down")
        assert _wait_until(lambda: len(seen) == 2)
        assert t.unacked_points == 1

        g.ack_sync(2)
        assert t.wait_for_acks(2.0)
    finally:
        t.stop()


# ------------------------------ (g) close() with a gateway that never acks


def test_close_keeps_unacknowledged_points_on_disk_for_the_next_start(make_gateway, tmp_path):
    g = make_gateway(ack_mode="none")
    px = _client(g, tmp_path)
    assert px.send("temp", 20.0, timestamp=1_700_000_000.0)
    assert px.buffer_size() == 0
    assert px._ws.unacked_points == 1

    started = time.monotonic()
    px.close()
    assert time.monotonic() - started < 4.0, "close() waits for acks for two seconds, not longer"

    kept = SqliteBuffer(path=str(tmp_path / "buffer.db"))
    try:
        assert [(p["value"], p["timestamp"]) for p in kept.get_all()] == [
            (20.0, 1_700_000_000_000),
        ]
    finally:
        kept.close()

    # The next start, against a gateway that is answering again.
    g.ack_mode = "all"
    again = _client(g, tmp_path)
    try:
        assert again.buffer_size() == 1
        assert again.send("temp", 21.0, timestamp=1_700_000_001.0)
        assert _wait_until(lambda: len(g.telemetry[1]) >= 1)
        assert [(p["value"], p["timestamp"]) for p in g.telemetry[1][0]["points"]] == [
            (20.0, 1_700_000_000_000),
            (21.0, 1_700_000_001_000),
        ]
        assert again._ws.wait_for_acks(2.0)
        assert again.buffer_size() == 0
    finally:
        again.close()


# ------------------------------------------------------------ edge cases


def test_a_failed_local_write_is_not_kept_in_the_window(make_gateway):
    """send_points() False means the caller owns the points. If the socket
    write itself fails the entry must go, or the HTTP copy the caller sends
    would be joined by a replay."""
    g = make_gateway(ack_mode="manual")
    t = _transport(g)
    t.start()
    try:
        assert t.wait_authenticated(timeout=3)
        with t._ws_lock:
            live = t._ws

        class _Broken:
            def send(self, data):
                raise OSError("broken pipe")

        with t._ws_lock:
            t._ws = _Broken()
        try:
            assert t.send_points([_pt("a", 1.0, 1)]) is False
        finally:
            with t._ws_lock:
                t._ws = live
        assert t.unacked_points == 0
        assert t.wait_for_acks(0.0) is True
    finally:
        t.stop()


def test_bool_and_missing_seq_release_nothing():
    t = WebSocketTransport(api_key="k", source_id="s", ws_url="ws://127.0.0.1:9")
    t._acks_enabled = True
    with t._window_cv:
        t._window[1] = (time.monotonic(), [_pt("a", 1.0, 1)])
        t._window_points = 1
    t._dispatch({"type": "telemetry_ack"})
    t._dispatch({"type": "telemetry_ack", "seq": True})
    t._dispatch({"type": "telemetry_ack", "seq": "1"})
    assert t.unacked_points == 1
    t._dispatch({"type": "telemetry_ack", "seq": 1})
    assert t.unacked_points == 0
