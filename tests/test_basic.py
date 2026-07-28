"""Basic tests for plexus-python."""

import time

from plexus import __version__
from plexus.client import Plexus
from plexus.config import DEFAULT_CONFIG


def test_version():
    """Version should be a string."""
    assert isinstance(__version__, str)
    assert len(__version__) > 0


def test_default_config():
    """Default config should have expected keys."""
    assert "api_key" in DEFAULT_CONFIG
    assert "source_id" in DEFAULT_CONFIG


def test_client_init():
    """Client should initialize without error."""
    px = Plexus(api_key="test_key", endpoint="http://localhost", persistent_buffer=False)
    assert px.api_key == "test_key"
    assert px.endpoint == "http://localhost"


def test_make_point():
    """Client should create valid data points."""
    px = Plexus(api_key="test", endpoint="http://localhost", persistent_buffer=False)
    point = px._make_point("temperature", 72.5)

    assert point["metric"] == "temperature"
    assert point["value"] == 72.5
    assert "timestamp" in point
    assert "source_id" not in point


def test_make_point_with_tags():
    """Data points should include tags when provided."""
    px = Plexus(api_key="test", endpoint="http://localhost", persistent_buffer=False)
    point = px._make_point("temperature", 72.5, tags={"sensor": "A1"})

    assert point["tags"] == {"sensor": "A1"}


def test_normalize_ts_ms_applies_clock_offset():
    px = Plexus(api_key="test", endpoint="http://localhost", persistent_buffer=False)
    px._clock_offset_ms = 5000
    before = int(time.time() * 1000)
    ts = px._normalize_ts_ms(None)
    after = int(time.time() * 1000)
    assert before + 5000 <= ts <= after + 5000


def test_normalize_ts_ms_ignores_offset_for_supplied_timestamp():
    px = Plexus(api_key="test", endpoint="http://localhost", persistent_buffer=False)
    px._clock_offset_ms = 5000
    ts = px._normalize_ts_ms(1_700_000_000.0)
    assert ts == 1_700_000_000_000


def test_send_batch_shared_timestamp():
    """All 2-tuple points share the same timestamp when none is supplied per-point."""
    px = Plexus(api_key="test", endpoint="http://localhost", persistent_buffer=False)
    t = 1_700_000_000.0

    # Build points directly to inspect timestamps
    default_ts_ms = px._normalize_ts_ms(t)
    batch_points = [("temp", 22.0), ("humidity", 55.0)]
    data = []
    for p in batch_points:
        m, v = p
        data.append(px._make_point(m, v, default_ts_ms, None))

    assert data[0]["timestamp"] == data[1]["timestamp"] == int(t * 1000)


def test_send_batch_per_point_timestamps():
    """3-tuple entries use their own timestamp; 2-tuple entries use the shared default."""
    px = Plexus(api_key="test", endpoint="http://localhost", persistent_buffer=False)
    t_shared = 1_700_000_000.0
    t_imu = 1_700_000_001.0
    t_baro = 1_700_000_002.0

    default_ts_ms = px._normalize_ts_ms(t_shared)
    batch = [
        ("imu.accel_x", 0.12, t_imu),
        ("pressure", 1013.2, t_baro),
        ("temperature", 22.4),
    ]
    data_points = []
    for p in batch:
        if len(p) == 3:
            m, v, t = p
            data_points.append(px._make_point(m, v, px._normalize_ts_ms(t), None))
        else:
            m, v = p
            data_points.append(px._make_point(m, v, default_ts_ms, None))

    assert data_points[0]["timestamp"] == int(t_imu * 1000)
    assert data_points[1]["timestamp"] == int(t_baro * 1000)
    assert data_points[2]["timestamp"] == int(t_shared * 1000)


def test_error_message_uses_plexus_init():
    """AuthenticationError for missing API key must reference 'plexus init', not 'plexus start'."""
    import pytest

    from plexus.client import AuthenticationError
    px = Plexus(api_key="test_key", endpoint="http://localhost", persistent_buffer=False)
    px.api_key = ""
    with pytest.raises(AuthenticationError, match="plexus init"):
        px._send_points([{"metric": "x", "value": 1, "timestamp": 0, "class": "metric"}])
