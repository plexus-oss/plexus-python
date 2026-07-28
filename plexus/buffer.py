"""
Pluggable buffer backends for local point storage.

Two implementations:
  - MemoryBuffer: In-memory list (default, matches original behavior)
  - SqliteBuffer: WAL-mode SQLite for persistence across restarts

Store-and-forward:
  Both backends support drain(batch_size) for incremental backlog upload.
  SqliteBuffer can run uncapped (max_size=None) for intermittently connected
  devices like satellites or field robots that buffer for hours/days.
"""

import json
import logging
import os
import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class BufferBackend(ABC):
    """Abstract buffer backend for storing telemetry points locally."""

    @abstractmethod
    def add(self, points: list[dict[str, Any]]) -> None:
        """Add points to the buffer, evicting oldest if over capacity."""

    @abstractmethod
    def get_all(self) -> list[dict[str, Any]]:
        """Return a copy of all buffered points without clearing."""

    @abstractmethod
    def clear(self) -> None:
        """Remove all buffered points."""

    @abstractmethod
    def size(self) -> int:
        """Return current number of buffered points."""

    @abstractmethod
    def resize(self, max_size: int) -> None:
        """Update the maximum buffer capacity."""

    def drain(self, batch_size: int = 5000) -> tuple[list[dict[str, Any]], int]:
        """Remove and return the oldest batch_size points atomically.

        Returns (points, remaining_count). Points are deleted from the buffer
        only by this call — if the caller fails to upload them, they are lost.
        Use this for backlog drain where each batch is uploaded then confirmed.

        Default implementation uses get_all/clear (non-atomic). Subclasses
        should override with atomic implementations.
        """
        all_pts = self.get_all()
        batch = all_pts[:batch_size]
        if not batch:
            return [], 0
        # Replace buffer with remaining points
        self.clear()
        remaining = all_pts[batch_size:]
        if remaining:
            self.add(remaining)
        return batch, len(remaining)


class MemoryBuffer(BufferBackend):
    """In-memory buffer with FIFO eviction. Thread-safe.

    This extracts the original behavior from Plexus client._failed_buffer.
    """

    def __init__(self, max_size: int = 10_000, on_overflow: Callable[[int], None] | None = None):
        self._max_size = max_size
        self._on_overflow = on_overflow
        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def add(self, points: list[dict[str, Any]]) -> None:
        with self._lock:
            self._buffer.extend(points)
            if len(self._buffer) > self._max_size:
                overflow = len(self._buffer) - self._max_size
                logger.warning("Buffer full, dropped %d oldest points", overflow)
                self._buffer = self._buffer[overflow:]
                if self._on_overflow:
                    self._on_overflow(overflow)

    def get_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._buffer)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._buffer)

    def resize(self, max_size: int) -> None:
        with self._lock:
            self._max_size = max_size

    def drain(self, batch_size: int = 5000) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            batch = self._buffer[:batch_size]
            self._buffer = self._buffer[batch_size:]
            return batch, len(self._buffer)


class SqliteBuffer(BufferBackend):
    """SQLite-backed persistent buffer using WAL mode. Thread-safe.

    Survives process restarts. Points are stored as JSON blobs in a single
    table with auto-incrementing rowid for FIFO ordering.

    Args:
        path: Path to the SQLite database file.
              Defaults to ~/.plexus/buffer.db
        max_size: Maximum number of points to retain. None = unlimited
                  (disk-bound, suitable for store-and-forward).
        max_bytes: Maximum database file size in bytes. None = no limit.
                   Safety valve to prevent filling the disk.
    """

    def __init__(
        self,
        path: str | None = None,
        max_size: int | None = 100_000,
        max_bytes: int | None = None,
        on_overflow: Callable[[int], None] | None = None,
    ):
        self._max_size = max_size
        self._max_bytes = max_bytes
        self._on_overflow = on_overflow
        self._lock = threading.Lock()

        if path is None:
            plexus_dir = os.path.join(os.path.expanduser("~"), ".plexus")
            os.makedirs(plexus_dir, exist_ok=True)
            try:
                os.chmod(plexus_dir, 0o700)
            except OSError:
                pass  # Windows or restricted filesystem
            path = os.path.join(plexus_dir, "buffer.db")

        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS points ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  data TEXT NOT NULL"
            ")"
        )
        self._conn.commit()

    def add(self, points: list[dict[str, Any]]) -> None:
        if not points:
            return
        with self._lock:
            # Disk-space safety valve
            if self._max_bytes is not None:
                try:
                    if os.path.getsize(self._path) >= self._max_bytes:
                        logger.warning("Buffer at disk limit (%d bytes), dropping oldest", self._max_bytes)
                        self._evict_pct(10)  # Drop 10% to make room
                except OSError:
                    pass
            self._conn.executemany(
                "INSERT INTO points (data) VALUES (?)",
                [(json.dumps(p),) for p in points],
            )
            self._conn.commit()
            self._evict()

    def get_all(self) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute("SELECT data FROM points ORDER BY id")
            return [json.loads(row[0]) for row in cursor.fetchall()]

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM points")
            self._conn.commit()

    def size(self) -> int:
        with self._lock:
            cursor = self._conn.execute("SELECT COUNT(*) FROM points")
            return cursor.fetchone()[0]

    def resize(self, max_size: int) -> None:
        with self._lock:
            self._max_size = max_size

    def drain(self, batch_size: int = 5000) -> tuple[list[dict[str, Any]], int]:
        """Remove and return the oldest batch_size points atomically.

        Uses a single transaction: SELECT then DELETE by rowid. If the process
        crashes after drain() but before the caller uploads, points are gone —
        this is the intended trade-off for simplicity. For crash-safe drain,
        the caller should use smaller batch sizes.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, data FROM points ORDER BY id LIMIT ?",
                (batch_size,),
            )
            rows = cursor.fetchall()
            if not rows:
                return [], 0

            points = [json.loads(row[1]) for row in rows]
            ids = [row[0] for row in rows]

            # Delete the exact rows we read
            placeholders = ",".join("?" * len(ids))
            self._conn.execute(
                f"DELETE FROM points WHERE id IN ({placeholders})",
                ids,
            )
            self._conn.commit()

            remaining = self._conn.execute("SELECT COUNT(*) FROM points").fetchone()[0]
            return points, remaining

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._conn.close()

    def _evict(self) -> None:
        """Remove oldest rows if over max_size. Must be called with lock held."""
        if self._max_size is None:
            return
        cursor = self._conn.execute("SELECT COUNT(*) FROM points")
        count = cursor.fetchone()[0]
        if count > self._max_size:
            overflow = count - self._max_size
            self._conn.execute(
                "DELETE FROM points WHERE id IN ("
                "  SELECT id FROM points ORDER BY id LIMIT ?"
                ")",
                (overflow,),
            )
            self._conn.commit()
            logger.warning("Buffer full, dropped %d oldest points", overflow)
            if self._on_overflow:
                self._on_overflow(overflow)

    def _evict_pct(self, pct: int) -> None:
        """Evict a percentage of buffered points. Must be called with lock held."""
        cursor = self._conn.execute("SELECT COUNT(*) FROM points")
        count = cursor.fetchone()[0]
        to_drop = max(1, count * pct // 100)
        self._conn.execute(
            "DELETE FROM points WHERE id IN ("
            "  SELECT id FROM points ORDER BY id LIMIT ?"
            ")",
            (to_drop,),
        )
        self._conn.commit()
        logger.warning("Disk safety: dropped %d oldest points", to_drop)
        if self._on_overflow:
            self._on_overflow(to_drop)
