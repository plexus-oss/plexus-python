"""BatchSender: coalescing, pacing, and the guarantees around losing nothing.

The class exists because the gateway's telemetry ceiling counts *messages*.
These tests assert the property that follows from that — N readings become far
fewer than N sends — plus the two things a bench operator has to be able to
trust: a clean exit strands no reading, and a failed flush leaves the points
with the client rather than dropping them.

The client is stubbed: what matters here is how many times `_send_points` is
called and with what, not that a socket exists.
"""

import threading
import time

import pytest

from plexus.batching import BatchSender
from plexus.client import Plexus


class _RecordingClient:
    """Stands in for Plexus, recording each send as one 'frame'."""

    def __init__(self, fail_times: int = 0):
        self._real = Plexus(
            api_key="test", endpoint="http://localhost", persistent_buffer=False
        )
        self.frames: list[list[dict]] = []
        self._fail_times = fail_times
        self._lock = threading.Lock()

    # BatchSender only borrows point construction from the client.
    def _make_point(self, *args, **kwargs):
        return self._real._make_point(*args, **kwargs)

    def _normalize_ts_ms(self, *args, **kwargs):
        return self._real._normalize_ts_ms(*args, **kwargs)

    def _send_points(self, points):
        with self._lock:
            if self._fail_times > 0:
                self._fail_times -= 1
                raise ConnectionError("gateway unreachable")
            self.frames.append(list(points))
        return True

    @property
    def sent(self) -> list[dict]:
        with self._lock:
            return [p for frame in self.frames for p in frame]

    @property
    def frame_count(self) -> int:
        with self._lock:
            return len(self.frames)


def test_many_readings_become_few_frames():
    """The whole point: 600 readings must not be 600 messages."""
    client = _RecordingClient()
    with BatchSender(client, interval_ms=50) as b:
        for i in range(200):
            b.send("att.pos_x", float(i))
            b.send("att.rate_x", float(i))
            b.send("frames.captured", i)
        time.sleep(0.25)

    assert len(client.sent) == 600
    # Under a tenth of one frame per reading, with wide margin for a slow CI box.
    assert client.frame_count < 30, f"expected coalescing, got {client.frame_count} frames"


def test_close_flushes_everything_queued():
    """A reading taken just before the block exits still lands."""
    client = _RecordingClient()
    b = BatchSender(client, interval_ms=10_000)  # never fires on its own
    b.start()
    b.send("motor.temp_c", 41.5)
    b.close()

    assert [p["metric"] for p in client.sent] == ["motor.temp_c"]


def test_context_manager_flushes_on_exit():
    client = _RecordingClient()
    with BatchSender(client, interval_ms=10_000) as b:
        b.send("frames.captured", 7)
    assert len(client.sent) == 1


def test_send_batch_queues_every_point():
    client = _RecordingClient()
    with BatchSender(client, interval_ms=10_000) as b:
        b.send_batch([("a", 1.0), ("b", 2.0), ("c", 3.0)])
    assert sorted(p["metric"] for p in client.sent) == ["a", "b", "c"]


def test_per_point_timestamps_survive_batching():
    client = _RecordingClient()
    with BatchSender(client, interval_ms=10_000) as b:
        b.send_batch([("imu.x", 0.1, 1_700_000_000.0), ("baro", 1013.2)])
    by_metric = {p["metric"]: p for p in client.sent}
    assert by_metric["imu.x"]["timestamp"] == 1_700_000_000_000


def test_value_type_errors_raise_at_the_call_site():
    """A bad value is the caller's bug — it must not surface on a background
    thread a hundred milliseconds later, where no traceback points at it."""
    from plexus.client import PlexusError

    client = _RecordingClient()
    with BatchSender(client, interval_ms=10_000) as b:
        with pytest.raises(PlexusError):
            b.send("temp", "not a number", data_class="metric")


def test_failed_flush_does_not_drop_points():
    """_send_points re-buffers on failure, so a raise means 'not yet', not
    'lost'. The sender must not swallow the points on top of that."""
    client = _RecordingClient(fail_times=1)
    b = BatchSender(client, interval_ms=10_000)
    b.start()
    b.send("motor.temp_c", 41.5)

    with pytest.raises(ConnectionError):
        b.flush()

    # The client took them (and, in the real client, buffered them).
    assert b.pending == 0
    b.close()


def test_overflow_drops_oldest_and_counts_them():
    client = _RecordingClient()
    b = BatchSender(client, interval_ms=10_000, max_points=10, max_pending=10)
    b.send("m", 0.0)  # not started — nothing drains
    for i in range(1, 15):
        b.send("m", float(i))

    assert b.dropped == 5
    b.close()
    values = [p["value"] for p in client.sent]
    assert values == [float(i) for i in range(5, 15)], "should drop oldest, keep newest"


def test_rejects_an_interval_past_the_gateway_ceiling():
    client = _RecordingClient()
    with pytest.raises(ValueError):
        BatchSender(client, interval_ms=0.5)


def test_send_after_close_is_an_error_not_a_silent_drop():
    client = _RecordingClient()
    b = BatchSender(client, interval_ms=10_000)
    b.start()
    b.close()
    with pytest.raises(RuntimeError):
        b.send("m", 1.0)


def test_close_is_idempotent():
    client = _RecordingClient()
    b = BatchSender(client, interval_ms=10_000)
    b.start()
    b.send("m", 1.0)
    b.close()
    b.close()
    assert len(client.sent) == 1


def test_plexus_batch_returns_a_started_sender():
    px = Plexus(api_key="test", endpoint="http://localhost", persistent_buffer=False)
    sender = px.batch(interval_ms=50)
    assert isinstance(sender, BatchSender)
    sender.close()
