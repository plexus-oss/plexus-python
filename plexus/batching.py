"""Coalescing sender — one frame per interval instead of one per reading.

`px.send()` transmits immediately: every call is its own WebSocket frame. That
is the right shape for a script sampling a sensor once a second, and the wrong
one for a test bench. The gateway allows 500 telemetry messages per second on a
connection and hard-drops a source above 2000/s, and both ceilings count
*messages*, not points — so eight channels at 100 Hz is 800 frames/s and lands
over the limit, while the same 800 readings coalesced into ten frames is not
close to it. The dropped frames come back as `RATE_LIMITED`, after `send()` has
already returned True, which is why this is a batching problem rather than a
retry one: by the time anyone can react, the points are gone.

Downstream the same shape decides throughput. The ClickHouse loader measures
~400k rows/s at 100 points per message and ~65k at one point per message — a 6x
penalty paid entirely at the producer.

    with px.batch(interval_ms=50) as b:
        while running:
            b.send("att.rate_x", gyro.x)
            b.send("att.rate_y", gyro.y)
            b.send("frames.captured", grabber.count)

Points accumulate in memory and a background thread flushes them on the
interval. `close()` (and so the `with` block) flushes what is left before
returning, so exiting normally never strands a reading.

Delivery semantics are the client's, unchanged: a failed flush leaves the
points in the client's local store-and-forward buffer to go out with the next
send. Nothing here drops data on the floor — the one bounded exception is
`max_pending`, which exists so a permanently unreachable gateway cannot grow
the process's memory without limit, and which reports every point it evicts.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from plexus._log import _say

if TYPE_CHECKING:  # pragma: no cover - typing only
    from plexus.client import FlexValue, Plexus

logger = logging.getLogger(__name__)

# Frames per second implied by the default interval: 10. Two orders of
# magnitude under the gateway's per-connection ceiling, so a caller has room
# to shorten it without having to know what the ceiling is.
DEFAULT_INTERVAL_MS = 100.0

# Hard floor on the flush interval. Below ~2ms the flush thread spends more
# time waking up than sending, and 500 frames/s is the gateway's limit anyway.
MIN_INTERVAL_MS = 2.0

# Frames a periodic flush may send in one cycle. At the default interval and
# `max_points` this is 200k points/s of drain capacity while holding the frame
# rate at 40/s — an order of magnitude under the ceiling even while catching up.
_FRAMES_PER_FLUSH = 4


class BatchSender:
    """Accumulates points and flushes them on an interval.

    Construct via `Plexus.batch()` rather than directly — the client owns the
    connection, the buffer and the retry policy, and this only decides when a
    frame leaves.
    """

    def __init__(
        self,
        client: "Plexus",
        interval_ms: float = DEFAULT_INTERVAL_MS,
        max_points: int = 5000,
        max_pending: int = 200_000,
    ):
        if interval_ms < MIN_INTERVAL_MS:
            raise ValueError(
                f"interval_ms must be >= {MIN_INTERVAL_MS} "
                f"({1000 / MIN_INTERVAL_MS:.0f} flushes/s is already past the "
                "gateway's per-connection ceiling)"
            )
        if max_points < 1:
            raise ValueError("max_points must be >= 1")
        if max_pending < max_points:
            raise ValueError("max_pending must be >= max_points")

        self._client = client
        self._interval_s = interval_ms / 1000.0
        self._max_points = max_points
        self._max_pending = max_pending

        self._pending: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._dropped = 0
        self._announced_overflow = False
        self._closed = False

    # ------------------------------------------------------------------ public

    def send(
        self,
        metric: str,
        value: "FlexValue",
        timestamp: float | None = None,
        tags: dict[str, str] | None = None,
        data_class: str | None = None,
    ) -> None:
        """Queue one reading. Same arguments as `Plexus.send()`.

        Returns None rather than bool: the point has not been sent yet, and a
        True here would mean the same thing `send()`'s True means, which it
        does not. Value-type errors still raise immediately — that is a bug in
        the caller's code and should surface at the call site, not on a
        background thread a hundred milliseconds later.
        """
        point = self._client._make_point(metric, value, timestamp, tags, data_class)
        self._append([point])

    def send_batch(
        self,
        points: list[tuple[str, "FlexValue"] | tuple[str, "FlexValue", float]],
        timestamp: float | None = None,
        tags: dict[str, str] | None = None,
    ) -> None:
        """Queue several readings at once. Same arguments as `Plexus.send_batch()`."""
        default_ts_ms = self._client._normalize_ts_ms(timestamp)
        built: list[dict[str, Any]] = []
        for p in points:
            if len(p) == 3:
                m, v, t = p
                built.append(
                    self._client._make_point(m, v, self._client._normalize_ts_ms(t), tags)
                )
            else:
                m, v = p
                built.append(self._client._make_point(m, v, default_ts_ms, tags))
        self._append(built)

    def flush(self) -> None:
        """Send everything queued right now. Blocks until the send returns.

        Raises whatever `Plexus.send()` raises. The points are already in the
        client's local buffer by then, so a raise here means "not delivered
        yet", not "lost".
        """
        self._flush_once(raise_on_error=True)

    @property
    def pending(self) -> int:
        """Points queued but not yet handed to the client."""
        with self._lock:
            return len(self._pending)

    @property
    def dropped(self) -> int:
        """Points evicted because the queue hit `max_pending`."""
        return self._dropped

    def start(self) -> "BatchSender":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run, name="plexus-batch", daemon=True
        )
        self._thread.start()
        return self

    def close(self) -> None:
        """Stop the flush thread and send what is left.

        Idempotent. The final flush is best-effort: anything it cannot deliver
        stays in the client's buffer for the next send or for `close()` on the
        client itself, which flushes again.
        """
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=max(5.0, self._interval_s * 10))
        self._flush_once(raise_on_error=False)

    def __enter__(self) -> "BatchSender":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    # ----------------------------------------------------------------- internal

    def _append(self, points: list[dict[str, Any]]) -> None:
        if self._closed:
            raise RuntimeError("BatchSender is closed")
        overflow = 0
        with self._lock:
            self._pending.extend(points)
            excess = len(self._pending) - self._max_pending
            if excess > 0:
                # Drop oldest. A bench cares more about the reading it just
                # took than one from several minutes of unreachable gateway.
                del self._pending[:excess]
                overflow = excess
                self._dropped += excess
        if overflow:
            self._report_overflow(overflow)

    def _report_overflow(self, dropped: int) -> None:
        if not self._announced_overflow:
            _say(
                f"⚠ batch queue full at {self._max_pending} points — dropping "
                f"oldest ({dropped} so far). The gateway is not keeping up."
            )
            self._announced_overflow = True
        logger.warning("plexus batch overflow: dropped %d oldest points", dropped)

    def _take(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self._pending:
                return []
            batch = self._pending[: self._max_points]
            del self._pending[: len(batch)]
            return batch

    def _flush_once(self, raise_on_error: bool, max_frames: int | None = None) -> None:
        frames = 0
        while max_frames is None or frames < max_frames:
            batch = self._take()
            if not batch:
                return
            frames += 1
            try:
                self._client._send_points(batch)
            except Exception as e:
                # _send_points has already returned the points to the client's
                # buffer, so they are not lost — but there is no point spinning
                # through the rest of the queue into the same failure.
                logger.debug("plexus batch flush failed: %s", e)
                if raise_on_error:
                    raise
                return

    def _run(self) -> None:
        # Pace off a fixed schedule rather than sleeping `interval` *after*
        # each flush: a flush that runs long would otherwise stretch the
        # effective interval, and a queue that is filling faster than it
        # drains would stretch it further each cycle.
        next_flush = time.monotonic() + self._interval_s
        while not self._stop.is_set():
            delay = next_flush - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                break
            # Bounded per cycle so a large backlog drains at a predictable
            # frame rate instead of bursting straight back into the gateway's
            # per-connection limit — the thing this class exists to avoid.
            self._flush_once(raise_on_error=False, max_frames=_FRAMES_PER_FLUSH)
            next_flush = max(time.monotonic(), next_flush + self._interval_s)
