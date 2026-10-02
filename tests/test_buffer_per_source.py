"""A device's buffered readings must only ever be sent as that device.

Buffered points do not carry a source id: the client that sends them supplies
it. Every client used to share one file, `~/.plexus/buffer.db`, so when two
devices were bridged from one machine (or one script) and a send failed, the
next client to send drained the other's backlog and posted it under its own
source id. Readings landed on the wrong device.
"""

import os

import pytest

from plexus.buffer import SqliteBuffer, buffer_path_for_source
from plexus.client import Plexus


class _Socket:
    """Stands in for the live connection: up, authenticated, nothing dropped."""

    is_authenticated = True
    reconnect_pending = False

    def wait_authenticated(self, timeout=None):
        return True


@pytest.fixture
def home(tmp_path):
    """The home directory the suite runs in (conftest points HOME at it)."""
    return tmp_path


@pytest.fixture
def sent(monkeypatch):
    """Every chunk a client sends, as (source_id, [metric, ...])."""
    out = []
    monkeypatch.setattr(Plexus, "_ensure_ws", lambda self: _Socket())
    monkeypatch.setattr(
        Plexus, "_send_chunk", lambda self, ws, points: out.append((self.source_id, [p["metric"] for p in points]))
    )
    return out


def _client(source_id: str) -> Plexus:
    return Plexus(api_key="test", endpoint="http://localhost", source_id=source_id)


def _fail_to_send(px: Plexus, metric: str) -> None:
    """What a failed send leaves behind: the point, in the buffer."""
    px._add_to_buffer([px._make_point(metric, 1.0, None, None, None)])


def test_each_source_has_its_own_buffer_file(home):
    a, b = _client("drone-a"), _client("drone-b")
    assert a._buffer._path != b._buffer._path
    assert a._buffer._path == os.path.join(str(home), ".plexus", "buffer-drone-a.db")


def test_one_device_never_sends_anothers_backlog(home, sent):
    a, b = _client("drone-a"), _client("drone-b")
    _fail_to_send(a, "a.battery")

    b.send("b.temp", 51.0)
    assert sent == [("drone-b", ["b.temp"])]

    a.send("a.rpm", 3000)
    assert sent[1] == ("drone-a", ["a.battery", "a.rpm"])


def test_backlog_survives_a_restart_of_the_same_device(home, sent):
    first = _client("drone-a")
    _fail_to_send(first, "a.battery")
    first._buffer.close()

    again = _client("drone-a")
    again.send("a.rpm", 3000)
    assert sent == [("drone-a", ["a.battery", "a.rpm"])]


def test_points_left_in_the_old_shared_file_are_not_lost_on_upgrade(home, sent):
    """An older release left a backlog in buffer.db. The first client to start
    takes it, and the old file is left empty."""
    legacy_dir = home / ".plexus"
    legacy_dir.mkdir(exist_ok=True)  # conftest already made it for config
    old = SqliteBuffer(path=str(legacy_dir / "buffer.db"))
    old.add([{"class": "metric", "metric": "old.reading", "value": 1.0, "timestamp": 1}])
    old.close()

    px = _client("drone-a")
    assert px.buffer_size() == 1
    px.send("a.rpm", 3000)
    assert sent == [("drone-a", ["old.reading", "a.rpm"])]

    leftover = SqliteBuffer(path=str(legacy_dir / "buffer.db"))
    assert leftover.size() == 0
    leftover.close()

    # A second device starting later finds nothing to take.
    assert _client("drone-b").buffer_size() == 0


def test_an_explicit_buffer_path_is_used_as_given(home, tmp_path):
    path = str(tmp_path / "mine.db")
    px = Plexus(api_key="test", endpoint="http://localhost", source_id="drone-a", buffer_path=path)
    assert px._buffer._path == path


def test_a_very_long_source_id_still_gets_a_usable_file_name(home):
    long_id = "d" + "x" * 250
    path = buffer_path_for_source(long_id)
    assert len(os.path.basename(path)) < 80
    assert path != buffer_path_for_source("d" + "y" * 250)
