"""Points the gateway drops over HTTP must never be silent.

`POST /ingest` answers 200 even when it stored only part of the request (a
device over its rate, or an organisation at its device limit) and reports how
many points it left out in `dropped`. The SDK used to treat any status under
400 as success and never read the body, so on the HTTP path (every Free
organisation) data was lost with no sign. These tests pin that the loss is
counted and raised on the caller's own thread, once.
"""

import pytest

from plexus.client import Plexus, RateLimitedError, _Response


def _client() -> Plexus:
    return Plexus(api_key="test", endpoint="http://localhost", persistent_buffer=False)


def test_a_dropped_count_in_the_answer_is_counted():
    px = _client()
    px._note_http_dropped(_Response(200, '{"success": true, "count": 7, "dropped": 3}'))
    assert px.dropped_points == 3


def test_a_full_success_counts_nothing():
    px = _client()
    px._note_http_dropped(_Response(200, '{"success": true, "count": 10}'))
    px._note_http_dropped(_Response(200, ""))
    px._note_http_dropped(_Response(200, "not json"))
    px._note_http_dropped(_Response(200, "[1, 2]"))
    assert px.dropped_points == 0
    px._raise_if_rate_limited()  # nothing to report


def test_the_next_send_raises_and_says_how_many():
    px = _client()
    px._note_http_dropped(_Response(200, '{"dropped": 3}'))

    with pytest.raises(RateLimitedError) as exc:
        px._raise_if_rate_limited()
    assert "3 points" in str(exc.value)


def test_the_notice_is_consumed_but_the_total_is_kept():
    px = _client()
    px._note_http_dropped(_Response(200, '{"dropped": 3}'))
    with pytest.raises(RateLimitedError):
        px._raise_if_rate_limited()

    px._raise_if_rate_limited()  # told once; not a permanent failure state
    assert px.dropped_points == 3


def test_http_send_reads_the_answer(monkeypatch):
    px = _client()

    class _Session:
        def post(self, url, data=b"", headers=None, timeout=10.0):
            return _Response(200, '{"success": true, "count": 1, "dropped": 2}')

    monkeypatch.setattr(px, "_get_session", lambda: _Session())
    px._send_http([px._make_point("battery.voltage", 48.0, None, None, None)])
    assert px.dropped_points == 2
