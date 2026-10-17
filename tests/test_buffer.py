"""Tests for plexus.buffer — MemoryBuffer and SqliteBuffer backends."""

import os
import tempfile

from plexus.buffer import MemoryBuffer, SqliteBuffer

# ===========================================================================
# Shared test cases (run against both backends)
# ===========================================================================


def _make_points(n, offset=0):
    return [{"metric": f"m{i + offset}", "value": i + offset} for i in range(n)]


class BufferTests:
    """Mixin with tests common to all buffer backends."""

    def make_buffer(self, max_size=100):
        raise NotImplementedError

    def test_add_and_get(self):
        buf = self.make_buffer()
        points = _make_points(5)
        buf.add(points)
        assert buf.get_all() == points
        assert buf.size() == 5

    def test_clear(self):
        buf = self.make_buffer()
        buf.add(_make_points(3))
        buf.clear()
        assert buf.size() == 0
        assert buf.get_all() == []

    def test_fifo_eviction(self):
        buf = self.make_buffer(max_size=5)
        buf.add(_make_points(3, offset=0))
        buf.add(_make_points(4, offset=10))
        # Total 7, max 5 → oldest 2 evicted
        result = buf.get_all()
        assert len(result) == 5
        # Oldest points should have been dropped
        assert result[0]["metric"] == "m2"

    def test_empty_add(self):
        buf = self.make_buffer()
        buf.add([])
        assert buf.size() == 0

    def test_multiple_adds(self):
        buf = self.make_buffer()
        buf.add(_make_points(2, offset=0))
        buf.add(_make_points(3, offset=10))
        assert buf.size() == 5


# ===========================================================================
# MemoryBuffer
# ===========================================================================


class TestMemoryBuffer(BufferTests):
    def make_buffer(self, max_size=100):
        return MemoryBuffer(max_size=max_size)

    def test_overflow_callback(self):
        """on_overflow callback should receive the number of dropped points."""
        dropped = []
        buf = MemoryBuffer(max_size=5, on_overflow=lambda n: dropped.append(n))
        buf.add(_make_points(3))
        assert dropped == []
        buf.add(_make_points(4, offset=10))
        assert dropped == [2]  # 7 - 5 = 2 dropped

    def test_no_callback_when_no_overflow(self):
        dropped = []
        buf = MemoryBuffer(max_size=10, on_overflow=lambda n: dropped.append(n))
        buf.add(_make_points(5))
        assert dropped == []


# ===========================================================================
# SqliteBuffer
# ===========================================================================


class TestSqliteBuffer(BufferTests):
    def make_buffer(self, max_size=100):
        # Use a temp file so tests don't pollute ~/.plexus/
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmpfile.close()
        return SqliteBuffer(path=self._tmpfile.name, max_size=max_size)

    def test_overflow_callback(self):
        """on_overflow callback should receive the number of dropped points."""
        dropped = []
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            buf = SqliteBuffer(path=tmp.name, max_size=5, on_overflow=lambda n: dropped.append(n))
            buf.add(_make_points(3))
            assert dropped == []
            buf.add(_make_points(4, offset=10))
            assert dropped == [2]
            buf.close()
        finally:
            os.unlink(tmp.name)

    def test_persistence_across_instances(self):
        """Points survive when a new SqliteBuffer instance opens the same file."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()

        try:
            buf1 = SqliteBuffer(path=tmp.name, max_size=1000)
            buf1.add(_make_points(5))
            buf1.close()

            buf2 = SqliteBuffer(path=tmp.name, max_size=1000)
            assert buf2.size() == 5
            assert buf2.get_all() == _make_points(5)
            buf2.close()
        finally:
            os.unlink(tmp.name)

    def test_clear_persists(self):
        """Clear should remove data from the database."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()

        try:
            buf1 = SqliteBuffer(path=tmp.name, max_size=1000)
            buf1.add(_make_points(3))
            buf1.clear()
            buf1.close()

            buf2 = SqliteBuffer(path=tmp.name, max_size=1000)
            assert buf2.size() == 0
            buf2.close()
        finally:
            os.unlink(tmp.name)


def test_sqlite_byte_cap_keeps_the_buffer_full_once_reached(tmp_path):
    """The cap measures pages in use, not the file. SQLite never shrinks a
    file, so measured by file size a buffer that had once reached the cap
    evicted 10% on every add() for the rest of its life: measured before the
    fix, 1,500 single-point adds under a 64 KB cap left 19 points."""
    cap = 64 * 1024
    buf = SqliteBuffer(path=str(tmp_path / "b.db"), max_size=None, max_bytes=cap)
    try:
        point = {"class": "event", "metric": "log", "value": "x" * 200, "timestamp": 1}
        sizes = []
        for _ in range(1500):
            buf.add([dict(point)])
            sizes.append(buf.size())
        peak = max(sizes)
        assert peak < 1500, "the cap is real: something was evicted"
        assert sizes[-1] >= peak * 0.6, f"spiralled down to {sizes[-1]} of a peak of {peak}"
        assert buf._used_bytes() <= cap + 2 * 4096  # checked before each insert
    finally:
        buf.close()
