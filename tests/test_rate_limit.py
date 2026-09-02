"""RATE_LIMITED must never be silent.

The gateway drops a telemetry frame that exceeds the per-connection message
rate and reports it asynchronously, long after `send()` returned True. Before
this path existed the entire handling was a `logger.warning`, so a bench could
lose a third of its data and see nothing. These tests pin the two properties
that make the loss visible: it is counted, and it raises on the caller's own
thread.
"""

import pytest

from plexus.client import Plexus, RateLimitedError


def _client() -> Plexus:
    return Plexus(api_key="test", endpoint="http://localhost", persistent_buffer=False)


def test_rate_limited_frames_are_counted():
    px = _client()
    assert px.rate_limited_frames == 0
    px._on_server_error("RATE_LIMITED", "")
    px._on_server_error("RATE_LIMITED", "")
    assert px.rate_limited_frames == 2


def test_other_server_errors_are_not_counted_as_drops():
    """INVALID_MESSAGE and friends are rejections of one frame the caller can
    see fail; only RATE_LIMITED is invisible loss."""
    px = _client()
    px._on_server_error("INVALID_MESSAGE", "points[0].metric invalid")
    px._on_server_error("", "")
    assert px.rate_limited_frames == 0


def test_next_send_raises_so_the_loss_reaches_the_caller():
    px = _client()
    px._on_server_error("RATE_LIMITED", "")

    with pytest.raises(RateLimitedError) as exc:
        px._raise_if_rate_limited()

    # The message has to say what to do, not just what happened.
    assert "batch" in str(exc.value).lower()


def test_notice_is_consumed_so_it_does_not_raise_forever():
    """One raise per burst. A permanent failure state would make the client
    unusable after a single overrun."""
    px = _client()
    px._on_server_error("RATE_LIMITED", "")

    with pytest.raises(RateLimitedError):
        px._raise_if_rate_limited()
    px._raise_if_rate_limited()  # nothing new to report

    # The lifetime counter still remembers.
    assert px.rate_limited_frames == 1


def test_burst_reports_the_whole_burst_once():
    px = _client()
    for _ in range(5):
        px._on_server_error("RATE_LIMITED", "")

    with pytest.raises(RateLimitedError) as exc:
        px._raise_if_rate_limited()
    assert "5 telemetry frames" in str(exc.value)


def test_ws_transport_forwards_error_frames():
    """The transport must hand error frames to the client rather than logging
    them into the void — that swallow was the original defect."""
    from plexus.ws import WebSocketTransport

    seen: list[tuple[str, str]] = []
    t = WebSocketTransport(
        api_key="test",
        source_id="bench-01",
        ws_url="ws://127.0.0.1:9",
        on_server_error=lambda code, detail: seen.append((code, detail)),
    )
    t._dispatch({"type": "error", "code": "RATE_LIMITED", "detail": "too fast"})
    assert seen == [("RATE_LIMITED", "too fast")]


def test_a_raising_callback_cannot_kill_the_read_loop():
    from plexus.ws import WebSocketTransport

    def boom(code: str, detail: str) -> None:
        raise RuntimeError("callback bug")

    t = WebSocketTransport(
        api_key="test",
        source_id="bench-01",
        ws_url="ws://127.0.0.1:9",
        on_server_error=boom,
    )
    t._dispatch({"type": "error", "code": "RATE_LIMITED"})  # must not raise
