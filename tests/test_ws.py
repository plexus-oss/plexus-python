"""Wire-compatibility tests for plexus.ws.WebSocketTransport.

Spins up a tiny `websockets`-based server on localhost that impersonates the
gateway's /ws/device endpoint and asserts the frames the SDK exchanges match
the gateway contract:

    device_auth → authenticated → telemetry → heartbeat → typed_command roundtrip
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest

websockets = pytest.importorskip("websockets")
from websockets.server import serve  # noqa: E402

from plexus.ws import WebSocketTransport  # noqa: E402


class _StubGateway:
    """Minimal gateway stub. Records every frame the client sends."""

    def __init__(self, assigned_source_id: str | None = None):
        self.received: list[dict[str, Any]] = []
        self.auth_frame: dict[str, Any] = {}
        # If set, the stub returns this value in the authenticated frame
        # regardless of what the client asked for — used to exercise the
        # auto-suffix path.
        self.assigned_source_id = assigned_source_id
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self.port = 0
        self._ws = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=3), "stub server did not start"

    def stop(self) -> None:
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread:
            self._thread.join(timeout=2)

    async def _handler(self, ws, path="/ws/device"):
        self._ws = ws
        # First frame must be device_auth.
        raw = await ws.recv()
        msg = json.loads(raw)
        self.auth_frame = msg
        returned_source_id = self.assigned_source_id or msg.get("source_id")
        await ws.send(json.dumps({
            "type": "authenticated",
            "source_id": returned_source_id,
            "server_time_ms": int(time.time() * 1000),
        }))
        try:
            async for raw in ws:
                self.received.append(json.loads(raw))
        except websockets.ConnectionClosed:
            return

    async def send_command(self, cmd_id: str, name: str, params: dict[str, Any]):
        assert self._ws is not None
        await self._ws.send(json.dumps({
            "type": "typed_command",
            "id": cmd_id,
            "command": name,
            "params": params,
        }))

    def send_command_sync(self, cmd_id: str, name: str, params: dict[str, Any]):
        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(
            self.send_command(cmd_id, name, params), self._loop
        )
        fut.result(timeout=2)

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
def gateway():
    g = _StubGateway()
    g.start()
    yield g
    g.stop()


def _url(port: int) -> str:
    return f"ws://127.0.0.1:{port}"


def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_auth_handshake_and_telemetry(gateway):
    t = WebSocketTransport(
        api_key="plx_test_abc",
        source_id="drone-001",
        ws_url=_url(gateway.port),
        agent_version="9.9.9",
    )
    t.start()
    try:
        assert t.wait_authenticated(timeout=3)

        # Auth frame shape
        assert gateway.auth_frame["type"] == "device_auth"
        assert gateway.auth_frame["api_key"] == "plx_test_abc"
        assert gateway.auth_frame["source_id"] == "drone-001"
        assert "install_id" not in gateway.auth_frame
        assert gateway.auth_frame["platform"] == "python-sdk"
        assert gateway.auth_frame["agent_version"] == "9.9.9"
        # commands is omitted when none registered
        assert "commands" not in gateway.auth_frame

        # Telemetry frame shape
        assert t.send_points([
            {"metric": "battery_voltage", "value": 12.4, "timestamp": 1700000000000}
        ])
        assert _wait_until(
            lambda: any(m.get("type") == "telemetry" for m in gateway.received)
        )
        tele = next(m for m in gateway.received if m["type"] == "telemetry")
        assert tele["points"][0]["metric"] == "battery_voltage"
        assert tele["points"][0]["value"] == 12.4
    finally:
        t.stop()


def test_command_roundtrip(gateway):
    got: dict[str, Any] = {}

    def reboot(name: str, params: dict[str, Any]) -> dict[str, Any]:
        got["name"] = name
        got["params"] = params
        return {"ok": True, "delay": params.get("delay_s")}

    t = WebSocketTransport(
        api_key="plx_test_abc",
        source_id="drone-001",
        ws_url=_url(gateway.port),
    )
    t.register_command("reboot", reboot, description="reboot device")
    t.start()
    try:
        assert t.wait_authenticated(timeout=3)
        # Advertised in auth frame
        assert gateway.auth_frame["commands"] == [
            {"name": "reboot", "description": "reboot device"}
        ]

        gateway.send_command_sync("cmd-42", "reboot", {"delay_s": 10})

        # Expect ack then result
        assert _wait_until(
            lambda: sum(
                1 for m in gateway.received
                if m.get("type") == "command_result" and m.get("id") == "cmd-42"
            ) >= 2
        )
        results = [
            m for m in gateway.received
            if m.get("type") == "command_result" and m.get("id") == "cmd-42"
        ]
        assert results[0]["event"] == "ack"
        assert results[0]["command"] == "reboot"
        assert results[1]["event"] == "result"
        assert results[1]["result"] == {"ok": True, "delay": 10}

        assert got == {"name": "reboot", "params": {"delay_s": 10}}
    finally:
        t.stop()


def test_unknown_command_returns_error(gateway):
    t = WebSocketTransport(
        api_key="plx_test_abc",
        source_id="drone-001",
        ws_url=_url(gateway.port),
    )
    t.start()
    try:
        assert t.wait_authenticated(timeout=3)
        gateway.send_command_sync("cmd-1", "nope", {})
        assert _wait_until(lambda: any(
            m.get("type") == "command_result" and m.get("event") == "error"
            for m in gateway.received
        ))
        err = next(
            m for m in gateway.received
            if m.get("type") == "command_result" and m.get("event") == "error"
        )
        assert "unknown command" in err["error"]
    finally:
        t.stop()


def test_handler_exception_returns_error(gateway):
    def bad(name, params):
        raise RuntimeError("boom")

    t = WebSocketTransport(
        api_key="plx_test_abc",
        source_id="drone-001",
        ws_url=_url(gateway.port),
    )
    t.register_command("bad", bad)
    t.start()
    try:
        assert t.wait_authenticated(timeout=3)
        gateway.send_command_sync("cmd-9", "bad", {})
        assert _wait_until(lambda: any(
            m.get("type") == "command_result"
            and m.get("event") == "error"
            and m.get("id") == "cmd-9"
            for m in gateway.received
        ))
        err = next(
            m for m in gateway.received
            if m.get("type") == "command_result"
            and m.get("event") == "error"
            and m.get("id") == "cmd-9"
        )
        assert err["error"] == "boom"
    finally:
        t.stop()


def test_reject_concurrency_refuses_overlap(gateway):
    release = threading.Event()
    started = threading.Event()

    def slow(name, params):
        started.set()
        release.wait(timeout=3)
        return {"done": True}

    t = WebSocketTransport(
        api_key="plx_test_abc",
        source_id="drone-001",
        ws_url=_url(gateway.port),
    )
    t.register_command("init", slow, concurrency="reject")
    t.start()
    try:
        assert t.wait_authenticated(timeout=3)

        # First invocation starts and blocks inside the handler.
        gateway.send_command_sync("cmd-1", "init", {})
        assert started.wait(timeout=3)

        # Second invocation arrives while the first is still running.
        gateway.send_command_sync("cmd-2", "init", {})

        # cmd-2 is acked, then rejected with an error — the handler never runs
        # a second time.
        assert _wait_until(lambda: any(
            m.get("type") == "command_result"
            and m.get("id") == "cmd-2"
            and m.get("event") == "error"
            for m in gateway.received
        ))
        err = next(
            m for m in gateway.received
            if m.get("type") == "command_result"
            and m.get("id") == "cmd-2" and m.get("event") == "error"
        )
        assert "already in progress" in err["error"]

        # Let the first finish; it still returns its result normally.
        release.set()
        assert _wait_until(lambda: any(
            m.get("type") == "command_result"
            and m.get("id") == "cmd-1" and m.get("event") == "result"
            for m in gateway.received
        ))

        # A fresh invocation after the first completes is accepted again.
        gateway.send_command_sync("cmd-3", "init", {})
        assert _wait_until(lambda: any(
            m.get("type") == "command_result"
            and m.get("id") == "cmd-3" and m.get("event") == "result"
            for m in gateway.received
        ))
    finally:
        release.set()
        t.stop()


def test_accept_concurrency_allows_overlap(gateway):
    release = threading.Event()
    active = []
    lock = threading.Lock()

    def slow(name, params):
        with lock:
            active.append(1)
        release.wait(timeout=3)
        return {"done": True}

    t = WebSocketTransport(
        api_key="plx_test_abc",
        source_id="drone-001",
        ws_url=_url(gateway.port),
    )
    # Default concurrency is "accept".
    t.register_command("init", slow)
    t.start()
    try:
        assert t.wait_authenticated(timeout=3)
        gateway.send_command_sync("cmd-1", "init", {})
        gateway.send_command_sync("cmd-2", "init", {})
        # Both handlers run at the same time (neither has returned yet).
        assert _wait_until(lambda: len(active) == 2)
    finally:
        release.set()
        t.stop()


def test_reject_releases_lock_if_thread_start_fails(monkeypatch):
    import plexus.ws as wsmod

    t = WebSocketTransport(
        api_key="plx_test_abc",
        source_id="drone-001",
        ws_url="ws://127.0.0.1:1",
    )
    t.register_command("init", lambda n, p: None, concurrency="reject")

    class _BoomThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("cannot start thread")

    monkeypatch.setattr(wsmod.threading, "Thread", _BoomThread)

    # Dispatch directly (no socket needed; _send_frame no-ops while _ws is None).
    # The failed thread start must not leave the concurrency lock held.
    t._handle_command({"id": "cmd-1", "command": "init", "params": {}})

    reg = t._commands["init"]
    assert reg._lock.acquire(blocking=False), "lock leaked after thread start failure"
    reg._lock.release()


def test_register_command_rejects_bad_concurrency(gateway):
    t = WebSocketTransport(
        api_key="plx_test_abc",
        source_id="drone-001",
        ws_url=_url(gateway.port),
    )
    with pytest.raises(ValueError, match="concurrency"):
        t.register_command("x", lambda n, p: None, concurrency="bogus")


def test_ensure_device_path():
    from plexus.ws import _ensure_device_path
    assert _ensure_device_path("wss://foo") == "wss://foo/ws/device"
    assert _ensure_device_path("wss://foo/") == "wss://foo/ws/device"
    assert _ensure_device_path("wss://foo/ws/device") == "wss://foo/ws/device"


def test_clock_offset_computed_from_authenticated_frame():
    # Stub sends a server_time_ms that is 30 seconds ahead of real time.
    # The transport's clock_offset_ms should be close to +30_000.
    fake_offset_ms = 30_000

    class _OffsetStubGateway(_StubGateway):
        async def _handler(self, ws, path="/ws/device"):
            self._ws = ws
            raw = await ws.recv()
            msg = json.loads(raw)
            self.auth_frame = msg
            returned_source_id = self.assigned_source_id or msg.get("source_id")
            await ws.send(json.dumps({
                "type": "authenticated",
                "source_id": returned_source_id,
                "server_time_ms": int(time.time() * 1000) + fake_offset_ms,
            }))
            try:
                async for raw in ws:
                    self.received.append(json.loads(raw))
            except websockets.ConnectionClosed:
                return

    g = _OffsetStubGateway()
    g.start()
    try:
        seen: list[int] = []
        t = WebSocketTransport(
            api_key="plx_test_abc",
            source_id="drone-001",
            ws_url=_url(g.port),
            on_clock_synced=lambda offset: seen.append(offset),
        )
        t.start()
        try:
            assert t.wait_authenticated(timeout=3)
            assert abs(t.clock_offset_ms - fake_offset_ms) < 500
            assert len(seen) == 1
            assert abs(seen[0] - fake_offset_ms) < 500
        finally:
            t.stop()
    finally:
        g.stop()
