"""End-to-end proof that batching changes what reaches the gateway.

test_batching.py stubs the client and asserts the queueing logic. This one
runs a real `Plexus` over a real socket against the same stub gateway
test_ws.py uses, and counts the frames that actually arrive — because the
defect being fixed is about frames on the wire, and the gateway's ceiling
counts exactly these.
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

from plexus.client import Plexus  # noqa: E402


class _CountingGateway:
    """Accepts device_auth, then records every telemetry frame it receives."""

    def __init__(self) -> None:
        self.telemetry_frames: list[dict[str, Any]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self.port = 0
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=3), "stub gateway did not start"

    def stop(self) -> None:
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def point_count(self) -> int:
        return sum(len(f.get("points", [])) for f in self.telemetry_frames)

    async def _handler(self, ws, path="/ws/device"):
        raw = await ws.recv()
        await ws.send(
            json.dumps(
                {
                    "type": "authenticated",
                    "source_id": json.loads(raw).get("source_id"),
                    "server_time_ms": int(time.time() * 1000),
                }
            )
        )
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "telemetry":
                    self.telemetry_frames.append(msg)
        except websockets.ConnectionClosed:
            return

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
    g = _CountingGateway()
    g.start()
    yield g
    g.stop()


def _client(port: int) -> Plexus:
    return Plexus(
        api_key="plx_test_abc",
        source_id="bench-01",
        endpoint=f"http://127.0.0.1:{port}",
        ws_url=f"ws://127.0.0.1:{port}",
        persistent_buffer=False,
    )


def _wait_until(pred, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_unbatched_sends_are_one_frame_each(gateway):
    """The behaviour that loses data at bench rates, pinned so nobody 'fixes'
    the batcher by quietly changing what plain send() does."""
    px = _client(gateway.port)
    try:
        for i in range(30):
            px.send("att.rate_x", float(i))
        assert _wait_until(lambda: len(gateway.telemetry_frames) >= 30)
        assert len(gateway.telemetry_frames) == 30
    finally:
        px.close()


def test_batched_sends_collapse_into_far_fewer_frames(gateway):
    """240 readings must not be 240 frames — that is the whole fix."""
    px = _client(gateway.port)
    try:
        with px.batch(interval_ms=50) as b:
            for i in range(80):
                b.send("att.pos_x", float(i))
                b.send("att.rate_x", float(i))
                b.send("frames.captured", i)
            # Let a couple of intervals elapse so this is not purely the
            # close-time flush doing the work.
            time.sleep(0.2)

        assert _wait_until(lambda: gateway.point_count >= 240)
        assert gateway.point_count == 240, "every reading must still arrive"
        assert len(gateway.telemetry_frames) < 25, (
            f"expected coalescing, got {len(gateway.telemetry_frames)} frames"
        )
    finally:
        px.close()


def test_nothing_is_stranded_when_the_block_exits_immediately(gateway):
    """A reading taken microseconds before the `with` ends still reaches the
    gateway — the flush thread never gets a chance to run here."""
    px = _client(gateway.port)
    try:
        with px.batch(interval_ms=10_000) as b:
            b.send("motor.temp_c", 41.5)

        assert _wait_until(lambda: gateway.point_count == 1)
        point = gateway.telemetry_frames[0]["points"][0]
        assert point["metric"] == "motor.temp_c"
        assert point["value"] == 41.5
        assert point["class"] == "metric"
    finally:
        px.close()


def test_batched_points_keep_their_own_timestamps(gateway):
    """Queueing must not restamp a reading with its flush time — a bench
    correlating channels would see them smeared onto interval boundaries."""
    px = _client(gateway.port)
    try:
        with px.batch(interval_ms=10_000) as b:
            b.send("a", 1.0)
            time.sleep(0.05)
            b.send("b", 2.0)

        assert _wait_until(lambda: gateway.point_count == 2)
        points = {p["metric"]: p for f in gateway.telemetry_frames for p in f["points"]}
        assert points["a"]["timestamp"] < points["b"]["timestamp"]
    finally:
        px.close()
