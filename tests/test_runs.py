"""Run lifecycle from the SDK.

`POST /api/runs` and `PATCH /api/runs/{id}` have existed on the app API for a
while; nothing in the SDK reached them and API.md said they did not exist.
These tests pin the contract the routes actually validate — an ISO-8601
`started_at` with an offset, a source slug, and a close that carries
`ended_at` — and the abort-on-exception behaviour a bench depends on.

The HTTP layer is stubbed at `_Session.request`, so no socket is opened.
"""

import json
import time

import pytest

from plexus.client import AuthenticationError, Plexus, PlexusError, _Response


class _StubSession:
    def __init__(self, responses=None):
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self._responses = responses or {}

    def request(self, method, url, data=b"", headers=None, timeout=10.0):
        body = json.loads(data.decode()) if data else {}
        self.calls.append((method, url, body))
        canned = self._responses.get(method)
        if canned is not None:
            return canned
        if method == "POST":
            return _Response(201, json.dumps({"run": {"id": "run_1", "name": body.get("name")}}))
        return _Response(200, json.dumps({"run": {"id": "run_1", "status": body.get("status")}}))

    def close(self):
        pass


def _client(session: _StubSession) -> Plexus:
    px = Plexus(
        api_key="test",
        endpoint="http://localhost",
        source_id="bench-01",
        persistent_buffer=False,
    )
    px._session = session
    return px


def test_start_run_posts_to_the_app_api_not_the_gateway():
    session = _StubSession()
    px = _client(session)
    run = px.start_run("hotfire-03")

    method, url, body = session.calls[0]
    assert (method, url) == ("POST", "http://localhost/api/runs")
    assert run["id"] == "run_1"
    assert body["name"] == "hotfire-03"


def test_started_at_matches_the_shape_the_route_validates():
    """The route uses z.string().datetime({offset: true}) — a naive local
    timestamp is a 400."""
    session = _StubSession()
    px = _client(session)
    px.start_run("hotfire-03")

    started = session.calls[0][2]["started_at"]
    assert started.endswith("Z")
    from datetime import datetime

    datetime.fromisoformat(started.replace("Z", "+00:00"))  # parses or raises


def test_run_boundaries_are_stamped_on_the_server_clock():
    """The window must be measured by the same clock as the points inside it.

    `_normalize_ts_ms` corrects generated timestamps by the offset the gateway
    reports on connect. Stamping run boundaries from the local clock instead
    leaves `ended_at` behind every point taken in the final `offset`
    milliseconds — and those points then fall outside the window their own run
    is evaluated over. Measured on production before the fix: a 300-point run
    on a device 58ms behind the server was judged on 284 of them, and the
    shortfall reads exactly like ingest lag.
    """
    from datetime import datetime

    session = _StubSession()
    px = _client(session)
    px._clock_offset_ms = 5_000  # device 5s behind the server

    px.start_run("run")
    px.end_run({"id": "run_1"})

    def as_ms(iso: str) -> int:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)

    local_ms = int(time.time() * 1000)
    started = as_ms(session.calls[0][2]["started_at"])
    ended = as_ms(session.calls[1][2]["ended_at"])

    # Both carry the offset, so they bound points that also carry it.
    assert started - local_ms > 4_000
    assert ended - local_ms > 4_000


def test_an_explicit_boundary_is_left_alone():
    """A caller passing its own timestamp owns it — we do not shift it."""
    session = _StubSession()
    px = _client(session)
    px._clock_offset_ms = 5_000
    px.start_run("run", started_at="2026-09-01T10:00:00.000Z")
    assert session.calls[0][2]["started_at"] == "2026-09-01T10:00:00.000Z"


def test_run_defaults_to_the_clients_own_source():
    session = _StubSession()
    px = _client(session)
    px.start_run("hotfire-03")
    assert session.calls[0][2]["source_id"] == "bench-01"


def test_explicit_none_source_makes_an_org_wide_run():
    """None is a real value here, distinct from 'not given' — the route
    accepts a null source_id."""
    session = _StubSession()
    px = _client(session)
    px.start_run("fleet-wide", source_id=None)
    assert session.calls[0][2]["source_id"] is None


def test_pass_criteria_and_tags_are_forwarded():
    session = _StubSession()
    px = _client(session)
    px.start_run(
        "hotfire-03",
        tags={"build": "a41f"},
        pass_criteria=[{"metric": "motor.temp_c", "operator": "<", "value": 85}],
    )
    body = session.calls[0][2]
    assert body["tags"] == {"build": "a41f"}
    assert body["pass_criteria"][0]["metric"] == "motor.temp_c"


def test_end_run_closes_with_a_timestamp():
    session = _StubSession()
    px = _client(session)
    px.end_run({"id": "run_1"})

    method, url, body = session.calls[0]
    assert (method, url) == ("PATCH", "http://localhost/api/runs/run_1")
    assert body["status"] == "completed"
    assert body["ended_at"].endswith("Z")


def test_end_run_accepts_a_bare_id():
    session = _StubSession()
    px = _client(session)
    px.end_run("run_9")
    assert session.calls[0][1].endswith("/api/runs/run_9")


def test_end_run_without_an_id_is_an_error():
    session = _StubSession()
    px = _client(session)
    with pytest.raises(PlexusError):
        px.end_run({})


def test_context_manager_completes_a_clean_run():
    session = _StubSession()
    px = _client(session)
    with px.run("hotfire-03") as run:
        assert run["id"] == "run_1"

    assert [c[0] for c in session.calls] == ["POST", "PATCH"]
    assert session.calls[1][2]["status"] == "completed"


def test_an_exception_aborts_the_run_and_still_propagates():
    session = _StubSession()
    px = _client(session)

    with pytest.raises(ZeroDivisionError):
        with px.run("hotfire-03"):
            1 / 0

    assert session.calls[1][2]["status"] == "aborted"


def test_a_failed_close_does_not_replace_the_callers_exception():
    """If the bench blew up and the network is also down, the traceback the
    operator needs is the bench's, not a urllib error."""

    class _FailPatch(_StubSession):
        def request(self, method, url, data=b"", headers=None, timeout=10.0):
            if method == "PATCH":
                raise ConnectionError("no route to host")
            return super().request(method, url, data, headers, timeout)

    px = _client(_FailPatch())
    with pytest.raises(ZeroDivisionError):
        with px.run("hotfire-03"):
            1 / 0


def test_a_fresh_bench_registers_its_source_then_opens_the_run():
    """The order a bench actually works in: open the run, THEN stream.

    The source does not exist until its first point, so the route 404s on a
    bench that has not started yet — which is every bench, on its first run.
    """
    calls: list[tuple[str, str, dict]] = []

    class _FreshBench(_StubSession):
        def request(self, method, url, data=b"", headers=None, timeout=10.0):
            body = json.loads(data.decode()) if data else {}
            calls.append((method, url, body))
            if url.endswith("/api/sources"):
                return _Response(201, json.dumps({"source": {"slug": "bench-01"}}))
            if url.endswith("/api/runs") and len([c for c in calls if c[1].endswith("/api/runs")]) == 1:
                return _Response(404, '{"error":"NOT_FOUND","message":"Device not found"}')
            return _Response(201, json.dumps({"run": {"id": "run_1"}}))

    px = _client(_FreshBench())
    run = px.start_run("first-ever-run")

    assert run["id"] == "run_1"
    assert [c[1].rsplit("/", 1)[-1] for c in calls] == ["runs", "sources", "runs"]
    assert calls[1][2]["slug"] == "bench-01"
    assert calls[1][2]["source_type"] == "device"


def test_an_unknown_explicit_source_still_404s():
    """Only the client's OWN source is auto-registered. A source_id someone
    typed by hand is far more likely a typo than a bench that hasn't started,
    and silently creating it would hide the mistake."""
    session = _StubSession({"POST": _Response(404, '{"error":"NOT_FOUND"}')})
    px = _client(session)

    with pytest.raises(PlexusError):
        px.start_run("run", source_id="probably-a-typo")

    # One attempt, no source creation.
    assert len(session.calls) == 1


def test_a_source_that_already_exists_is_not_an_error():
    """The gateway auto-creates on first point too, so start_run can race it."""
    class _Conflict(_StubSession):
        def request(self, method, url, data=b"", headers=None, timeout=10.0):
            body = json.loads(data.decode()) if data else {}
            self.calls.append((method, url, body))
            runs_calls = [c for c in self.calls if c[1].endswith("/api/runs")]
            if url.endswith("/api/sources"):
                return _Response(409, '{"error":"Source already exists"}')
            if len(runs_calls) == 1:
                return _Response(404, '{"error":"NOT_FOUND"}')
            return _Response(201, json.dumps({"run": {"id": "run_1"}}))

    px = _client(_Conflict())
    assert px.start_run("run")["id"] == "run_1"


def test_rejected_key_raises_authentication_error():
    session = _StubSession({"POST": _Response(401, '{"error":"Invalid API key"}')})
    px = _client(session)
    with pytest.raises(AuthenticationError):
        px.start_run("hotfire-03")


def test_server_error_surfaces_the_status_and_body():
    session = _StubSession({"POST": _Response(400, '{"error":"name is required"}')})
    px = _client(session)
    with pytest.raises(PlexusError) as exc:
        px.start_run("hotfire-03")
    assert "400" in str(exc.value)
    assert "name is required" in str(exc.value)
