"""Commands protocol v1 — `@px.command`, `px.serve()` and `command_status`.

Same shape as test_ws.py: a `websockets` server on localhost impersonates the
gateway's /ws/device endpoint, and the assertions are about the frames that
cross it:

    device_auth(protocol:1, commands:[...]) → authenticated(store_results)
    command_run → command_status(acknowledged → running → succeeded|failed)
    command_status_ack → the client stops holding that status
    reconnect → command_sync + replay of whatever was never acked

Nothing here leaves the host; conftest.py points every endpoint at the discard
port.
"""

from __future__ import annotations

import asyncio
import json
import signal
import threading
import time
from typing import Any

import pytest

websockets = pytest.importorskip("websockets")
from websockets.server import serve  # noqa: E402

from plexus.client import Plexus  # noqa: E402
from plexus.commands import (  # noqa: E402
    CommandDeclarationError,
    CommandParamError,
    build_schema,
    coerce_params,
)
from plexus.ws import WebSocketTransport  # noqa: E402


class _Gateway:
    """Gateway stub that speaks the v1 command frames."""

    def __init__(self, store_results: bool = True):
        self.store_results = store_results
        self.received: list[dict[str, Any]] = []
        self.auth_frames: list[dict[str, Any]] = []
        self.connections = 0
        self.port = 0
        self._ws = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=3), "stub server did not start"

    def stop(self) -> None:
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread:
            self._thread.join(timeout=2)

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

    async def _handler(self, ws, path="/ws/device"):
        self._ws = ws
        raw = await ws.recv()
        msg = json.loads(raw)
        self.auth_frames.append(msg)
        authenticated: dict[str, Any] = {
            "type": "authenticated",
            "source_id": msg.get("source_id"),
            "server_time_ms": int(time.time() * 1000),
            "protocol": 1,
            "max_frame_bytes": 65536,
        }
        if not self.store_results:
            authenticated["store_results"] = False
        await ws.send(json.dumps(authenticated))
        self.connections += 1
        try:
            async for raw in ws:
                self.received.append(json.loads(raw))
        except websockets.ConnectionClosed:
            return

    # -- driving the client ------------------------------------------------

    def _call(self, coro) -> None:
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=2)

    def send_run(
        self,
        run_id: str,
        command: str,
        params: dict[str, Any] | None = None,
        *,
        ttl_ms: int | None = 30_000,
        attempt: int = 1,
    ) -> None:
        frame: dict[str, Any] = {
            "type": "command_run",
            "run_id": run_id,
            "command": command,
            "params": params or {},
            "attempt": attempt,
        }
        if ttl_ms is not None:
            frame["ttl_ms"] = ttl_ms
        self._call(self._ws.send(json.dumps(frame)))

    def send_status_ack(self, run_id: str, seq: int) -> None:
        self._call(self._ws.send(json.dumps({
            "type": "command_status_ack", "run_id": run_id, "seq": seq,
        })))

    def drop_connection(self) -> None:
        """Close the socket under the client, as a network blip would."""
        self._call(self._ws.close())

    # -- reading what arrived ---------------------------------------------

    def statuses(self, run_id: str) -> list[dict[str, Any]]:
        return [
            m for m in list(self.received)
            if m.get("type") == "command_status" and m.get("run_id") == run_id
        ]

    def frames(self, ftype: str) -> list[dict[str, Any]]:
        return [m for m in list(self.received) if m.get("type") == ftype]


@pytest.fixture
def gateway():
    g = _Gateway()
    g.start()
    yield g
    g.stop()


@pytest.fixture
def metadata_only_gateway():
    g = _Gateway(store_results=False)
    g.start()
    yield g
    g.stop()


def _url(port: int) -> str:
    return f"ws://127.0.0.1:{port}"


def _wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _client(gateway: _Gateway, source_id: str = "pod-07") -> Plexus:
    return Plexus(
        api_key="plx_test_abc",
        source_id=source_id,
        ws_url=_url(gateway.port),
        persistent_buffer=False,
    )


def _states(statuses: list[dict[str, Any]]) -> list[str]:
    return [s["state"] for s in statuses]


def _reached(gateway: _Gateway, run_id: str, state: str) -> bool:
    return any(s["state"] == state for s in gateway.statuses(run_id))


# ------------------------------------------------------------------ manifest


def test_manifest_shape_in_auth_frame(gateway):
    px = _client(gateway)

    @px.command(
        "power_off",
        title="Power off",
        description="Cut power to one outlet",
        danger="critical",
        idempotent=True,
        expires_in=45,
        concurrency="reject",
        params={
            "outlet": {"type": "integer", "minimum": 1, "maximum": 8, "unit": "outlet"},
            "reason": {"type": "string", "maxLength": 80, "default": "manual"},
        },
    )
    def power_off(run, outlet, reason):
        return {"outlet": outlet, "reason": reason}

    try:
        assert px._ws.wait_authenticated(timeout=3)
        auth = gateway.auth_frames[0]
        assert auth["protocol"] == 1
        assert auth["commands"] == [{
            "name": "power_off",
            "title": "Power off",
            "description": "Cut power to one outlet",
            "danger": "critical",
            "idempotent": True,
            "expires_in_s": 45.0,
            "concurrency": "reject",
            # The flat map the gateway and the dashboard parse: name → spec,
            # every entry with an explicit `required`.
            "params": {
                "outlet": {
                    "type": "integer", "minimum": 1, "maximum": 8, "unit": "outlet",
                    "required": True,
                },
                "reason": {
                    "type": "string", "maxLength": 80, "default": "manual",
                    "required": False,
                },
            },
        }]
    finally:
        px.close()


def test_legacy_handlers_are_not_advertised(gateway):
    """The gateway validates every protocol-1 manifest entry as v1, so a
    legacy on_command entry there would draw invalid_command on every connect."""
    px = _client(gateway)

    @px.command("power_off")
    def power_off(run):
        return {}

    with pytest.deprecated_call():
        px.on_command("reboot", lambda name, params: {"ok": True}, description="reboot")

    try:
        assert px._ws.wait_authenticated(timeout=3)
        names = [c["name"] for c in gateway.auth_frames[0]["commands"]]
        assert names == ["power_off"]
    finally:
        px.close()


def test_only_legacy_handlers_sends_protocol_without_commands(gateway):
    px = _client(gateway)
    with pytest.deprecated_call():
        px.on_command("reboot", lambda name, params: {"ok": True})
    try:
        assert px._ws.wait_authenticated(timeout=3)
        auth = gateway.auth_frames[0]
        assert auth["protocol"] == 1
        assert "commands" not in auth
    finally:
        px.close()


# --------------------------------------------------------- param validation


def test_build_schema_rejects_what_the_wire_cannot_carry():
    with pytest.raises(CommandDeclarationError, match="type must be one of"):
        build_schema({"rows": {"type": "array"}})
    with pytest.raises(CommandDeclarationError, match="unsupported keyword"):
        build_schema({"outlet": {"type": "integer", "multipleOf": 2}})
    with pytest.raises(CommandDeclarationError, match="minimum"):
        build_schema({"outlet": {"type": "integer", "minimum": 9, "maximum": 8}})
    with pytest.raises(CommandDeclarationError, match="\\^\\[a-z\\]"):
        build_schema({"Outlet": {"type": "integer"}})
    # Defaults are already typed: no coercion at declaration time.
    with pytest.raises(CommandDeclarationError, match="default must be a integer"):
        build_schema({"outlet": {"type": "integer", "default": "8"}})
    with pytest.raises(CommandDeclarationError, match="default must be a integer"):
        build_schema({"outlet": {"type": "integer", "default": True}})
    with pytest.raises(CommandDeclarationError, match="default must be a boolean"):
        build_schema({"force": {"type": "boolean", "default": "true"}})
    with pytest.raises(CommandDeclarationError, match="bad default"):
        build_schema({"outlet": {"type": "integer", "maximum": 8, "default": 9}})
    with pytest.raises(CommandDeclarationError, match="bad default"):
        build_schema({"mode": {"type": "string", "enum": ["a", "b"], "default": "c"}})

    # Keywords belong to their types, exactly as the gateway enforces them.
    with pytest.raises(CommandDeclarationError, match="enum does not apply to integer"):
        build_schema({"outlet": {"type": "integer", "enum": ["1"]}})
    with pytest.raises(CommandDeclarationError, match="maxLength does not apply to number"):
        build_schema({"gain": {"type": "number", "maxLength": 4}})
    with pytest.raises(CommandDeclarationError, match="minimum does not apply to string"):
        build_schema({"name": {"type": "string", "minimum": 1}})
    with pytest.raises(CommandDeclarationError, match="unit does not apply to boolean"):
        build_schema({"force": {"type": "boolean", "unit": "V"}})
    with pytest.raises(CommandDeclarationError, match="enum must be a list of 1 to 64"):
        build_schema({"mode": {"type": "string", "enum": [f"m{i}" for i in range(65)]}})
    with pytest.raises(CommandDeclarationError, match="enum must be a list"):
        build_schema({"mode": {"type": "string", "enum": [1, 2]}})
    with pytest.raises(CommandDeclarationError, match="at most 16 params"):
        build_schema({f"p{i}": {"type": "boolean"} for i in range(17)})
    with pytest.raises(CommandDeclarationError, match="required must be True or False"):
        build_schema({"outlet": {"type": "integer", "required": "yes"}})


def test_build_schema_emits_the_flat_map_with_explicit_required():
    assert build_schema({
        "outlet": {"type": "integer", "minimum": 1, "maximum": 8},
        "window": {"type": "number", "default": 24},
        "note": {"type": "string", "required": False},
    }) == {
        "outlet": {"type": "integer", "minimum": 1, "maximum": 8, "required": True},
        "window": {"type": "number", "default": 24, "required": False},
        "note": {"type": "string", "required": False},
    }
    assert build_schema(None) == {}
    assert len(build_schema({f"p{i}": {"type": "boolean"} for i in range(16)})) == 16


def test_declaration_is_checked_at_decoration_time(gateway):
    px = _client(gateway)
    try:
        with pytest.raises(CommandDeclarationError, match="danger"):
            px.command("power_off", danger="extremely")(lambda run: None)
        with pytest.raises(CommandDeclarationError, match="expires_in"):
            px.command("power_off", expires_in=1)(lambda run: None)
        with pytest.raises(CommandDeclarationError, match="\\^\\[a-z\\]"):
            px.command("Power Off")(lambda run: None)
        with pytest.raises(CommandDeclarationError, match="concurrency"):
            px.command("power_off", concurrency="queue")(lambda run: None)
    finally:
        px.close()


def test_coerce_params_types_and_bounds():
    schema = build_schema({
        "outlet": {"type": "integer", "minimum": 1, "maximum": 8},
        "window_hours": {"type": "number", "minimum": 1, "maximum": 168, "default": 24},
        "mode": {"type": "string", "enum": ["fast", "slow"], "required": False},
        "force": {"type": "boolean", "default": False},
    })

    # Coercion: the strings a form or a curl produces become real types, and
    # declared defaults fill in.
    assert coerce_params(schema, {"outlet": "3"}) == {
        "outlet": 3, "window_hours": 24.0, "force": False,
    }
    assert coerce_params(schema, {"outlet": 3.0, "force": "true"})["force"] is True
    assert coerce_params(schema, {"outlet": 3, "window_hours": 1})["window_hours"] == 1.0

    # An optional parameter with no default is left out, so the handler's own
    # default applies.
    assert "mode" not in coerce_params(schema, {"outlet": 1})

    with pytest.raises(CommandParamError, match="missing required parameter: outlet"):
        coerce_params(schema, {})
    with pytest.raises(CommandParamError, match="at most 8"):
        coerce_params(schema, {"outlet": 9})
    with pytest.raises(CommandParamError, match="must be an integer"):
        coerce_params(schema, {"outlet": "three"})
    with pytest.raises(CommandParamError, match="must be an integer, got a boolean"):
        coerce_params(schema, {"outlet": True})
    with pytest.raises(CommandParamError, match="one of: fast, slow"):
        coerce_params(schema, {"outlet": 1, "mode": "sideways"})
    with pytest.raises(CommandParamError, match="unknown parameter"):
        coerce_params(schema, {"outlet": 1, "otlet": 2})


def test_bad_params_are_refused_before_the_handler_runs(gateway):
    calls: list[Any] = []
    px = _client(gateway)

    @px.command("power_off", params={"outlet": {"type": "integer", "maximum": 8}})
    def power_off(run, outlet):
        calls.append(outlet)

    try:
        assert px._ws.wait_authenticated(timeout=3)
        gateway.send_run("run-bad", "power_off", {"outlet": 99})
        assert _wait_until(lambda: gateway.statuses("run-bad"))
        status = gateway.statuses("run-bad")[0]
        assert status["state"] == "failed"
        assert status["error_code"] == "bad_params"
        assert "at most 8" in status["error_message"]
        assert status["seq"] == 1
        assert calls == []
    finally:
        px.close()


def test_params_reach_the_handler_as_keyword_arguments(gateway):
    seen: dict[str, Any] = {}
    px = _client(gateway)

    @px.command("run_detection", params={
        "norad_id": {"type": "integer", "minimum": 1},
        "window_hours": {"type": "number", "minimum": 1, "maximum": 168, "default": 24},
    })
    def run_detection(run, norad_id, window_hours=1):
        seen["norad_id"] = norad_id
        seen["window_hours"] = window_hours
        seen["run_id"] = run.id
        seen["command"] = run.command
        seen["attempt"] = run.attempt
        seen["remaining"] = run.time_remaining
        return {"rows": 3}

    try:
        assert px._ws.wait_authenticated(timeout=3)
        gateway.send_run("run-1", "run_detection", {"norad_id": "25544"}, ttl_ms=20_000)
        assert _wait_until(lambda: len(gateway.statuses("run-1")) >= 3)
        assert seen["norad_id"] == 25544
        assert seen["window_hours"] == 24.0
        assert seen["run_id"] == "run-1"
        assert seen["command"] == "run_detection"
        assert seen["attempt"] == 1
        assert 15.0 < seen["remaining"] <= 20.0
    finally:
        px.close()


# -------------------------------------------------------------- status flow


def test_status_sequence_on_success(gateway):
    px = _client(gateway)

    @px.command("power_off", params={"outlet": {"type": "integer"}})
    def power_off(run, outlet):
        return {"outlet": outlet, "state": "off"}

    try:
        assert px._ws.wait_authenticated(timeout=3)
        gateway.send_run("run-ok", "power_off", {"outlet": 3})
        assert _wait_until(lambda: len(gateway.statuses("run-ok")) >= 3)
        statuses = gateway.statuses("run-ok")
        assert _states(statuses) == ["acknowledged", "running", "succeeded"]
        assert [s["seq"] for s in statuses] == [1, 2, 3]
        assert all(s["type"] == "command_status" for s in statuses)
        assert statuses[-1]["result"] == {"outlet": 3, "state": "off"}
        assert "error_code" not in statuses[-1]
    finally:
        px.close()


def test_status_sequence_on_raising_handler(gateway):
    px = _client(gateway)

    @px.command("power_off")
    def power_off(run):
        raise RuntimeError("relay stuck")

    try:
        assert px._ws.wait_authenticated(timeout=3)
        gateway.send_run("run-bad", "power_off")
        assert _wait_until(lambda: len(gateway.statuses("run-bad")) >= 3)
        statuses = gateway.statuses("run-bad")
        assert _states(statuses) == ["acknowledged", "running", "failed"]
        assert statuses[-1]["error_code"] == "handler_error"
        assert statuses[-1]["error_message"] == "RuntimeError: relay stuck"
        assert "result" not in statuses[-1]
    finally:
        px.close()


def test_unserialisable_result_still_reports_the_outcome(gateway):
    px = _client(gateway)

    class _Opaque:
        def __repr__(self):
            return "<pdu handle>"

    @px.command("power_off")
    def power_off(run):
        return {"handle": _Opaque()}

    try:
        assert px._ws.wait_authenticated(timeout=3)
        gateway.send_run("run-odd", "power_off")
        assert _wait_until(lambda: _reached(gateway, "run-odd", "succeeded"))
        final = gateway.statuses("run-odd")[-1]
        assert "pdu handle" in final["result"]["value"]
    finally:
        px.close()


def test_oversized_result_is_dropped_not_the_outcome(gateway):
    px = _client(gateway)

    @px.command("dump")
    def dump(run):
        return {"rows": "x" * 200_000}

    try:
        assert px._ws.wait_authenticated(timeout=3)
        gateway.send_run("run-big", "dump")
        assert _wait_until(lambda: _reached(gateway, "run-big", "succeeded"))
        final = gateway.statuses("run-big")[-1]
        assert "result" not in final
    finally:
        px.close()


def test_status_size_is_checked_with_its_real_seq():
    """A frame that fits with seq 0 but not with seq 10 must still be trimmed:
    the gateway measures the frame as sent and drops anything over the cap."""
    t = WebSocketTransport(
        api_key="plx_test_abc", source_id="pod-07", ws_url="ws://127.0.0.1:1",
    )
    t._max_status_bytes = 1024
    for _ in range(9):
        t._emit_status("r", "running")
    empty = {"type": "command_status", "run_id": "r", "seq": 0,
             "state": "succeeded", "result": {"blob": ""}}
    blob = "x" * (1024 - len(json.dumps(empty)))
    t._emit_status("r", "succeeded", result={"blob": blob})

    final = t._unacked[-1]
    assert final["seq"] == 10
    assert "result" not in final
    assert len(json.dumps(final)) <= 1024


def test_unknown_command_is_refused(gateway):
    px = _client(gateway)

    @px.command("power_off")
    def power_off(run):
        return {}

    try:
        assert px._ws.wait_authenticated(timeout=3)
        gateway.send_run("run-x", "self_destruct")
        assert _wait_until(lambda: gateway.statuses("run-x"))
        status = gateway.statuses("run-x")[0]
        assert status["state"] == "failed"
        assert status["error_code"] == "unknown_command"
    finally:
        px.close()


def test_expired_on_arrival_never_runs_the_handler(gateway):
    calls: list[int] = []
    px = _client(gateway)

    @px.command("power_off")
    def power_off(run):
        calls.append(1)

    try:
        assert px._ws.wait_authenticated(timeout=3)
        gateway.send_run("run-late", "power_off", ttl_ms=0)
        assert _wait_until(lambda: gateway.statuses("run-late"))
        statuses = gateway.statuses("run-late")
        assert _states(statuses) == ["failed"]
        assert statuses[0]["error_code"] == "expired_on_arrival"
        assert calls == []
    finally:
        px.close()


def test_expiry_is_measured_from_arrival_on_the_monotonic_clock():
    """The deadline comes from the monotonic reading taken when the frame
    arrived — never from the wall clock, which jumps when NTP lands."""
    t = WebSocketTransport(
        api_key="plx_test_abc", source_id="pod-07", ws_url="ws://127.0.0.1:1",
    )
    runs: list[Any] = []
    t.declare_command(_declaration("hold", lambda run: runs.append(run) or {}))

    # No socket: _send_frame no-ops and the statuses stay in the replay buffer.
    arrived = time.monotonic()
    t._handle_command_run(
        {"type": "command_run", "run_id": "r1", "command": "hold", "ttl_ms": 30_000},
        arrived,
    )
    assert _wait_until(lambda: runs)
    run = runs[0]
    assert run.deadline == pytest.approx(arrived + 30.0)
    assert run.time_remaining == pytest.approx(30.0, abs=1.0)
    assert _wait_until(lambda: len(t._unacked) >= 3)
    assert _states(list(t._unacked)) == ["acknowledged", "running", "succeeded"]


def _declaration(name: str, handler, **kwargs):
    from plexus.commands import declare
    return declare(name, handler, **kwargs)


# ------------------------------------------------------------ de-duplication


def test_redelivered_run_id_is_not_run_twice(gateway):
    calls: list[str] = []
    px = _client(gateway)

    @px.command("power_off", idempotent=True)
    def power_off(run):
        calls.append(run.id)
        return {"state": "off"}

    try:
        assert px._ws.wait_authenticated(timeout=3)
        gateway.send_run("run-dup", "power_off")
        assert _wait_until(lambda: len(gateway.statuses("run-dup")) >= 3)

        gateway.send_run("run-dup", "power_off", attempt=2)
        assert _wait_until(lambda: len(gateway.statuses("run-dup")) >= 4)
        time.sleep(0.2)

        statuses = gateway.statuses("run-dup")
        assert _states(statuses) == [
            "acknowledged", "running", "succeeded", "succeeded",
        ]
        # The redelivery re-reports the known outcome, with a fresh seq, and
        # the handler stays at one call.
        assert statuses[-1]["seq"] == 4
        assert statuses[-1]["result"] == {"state": "off"}
        assert calls == ["run-dup"]
    finally:
        px.close()


def test_redelivery_while_still_running_reports_running(gateway):
    release = threading.Event()
    started = threading.Event()
    calls: list[str] = []
    px = _client(gateway)

    @px.command("slow_job")
    def slow_job(run):
        calls.append(run.id)
        started.set()
        release.wait(timeout=3)
        return {"done": True}

    try:
        assert px._ws.wait_authenticated(timeout=3)
        gateway.send_run("run-slow", "slow_job")
        assert started.wait(timeout=3)
        gateway.send_run("run-slow", "slow_job", attempt=2)
        assert _wait_until(lambda: len(gateway.statuses("run-slow")) >= 3)
        assert _states(gateway.statuses("run-slow"))[:3] == [
            "acknowledged", "running", "running",
        ]
        release.set()
        assert _wait_until(lambda: _reached(gateway, "run-slow", "succeeded"))
        assert calls == ["run-slow"]
    finally:
        release.set()
        px.close()


def test_duplicate_before_any_state_is_refused():
    """Two deliveries racing into dispatch: the second is told `duplicate`."""
    t = WebSocketTransport(
        api_key="plx_test_abc", source_id="pod-07", ws_url="ws://127.0.0.1:1",
    )
    t.declare_command(_declaration("power_off", lambda run: {}))
    # Reserve the id the way _handle_command_run does, without reporting yet.
    from plexus.ws import _RunRecord
    t._runs["run-race"] = _RunRecord(run_id="run-race")

    t._handle_command_run(
        {"type": "command_run", "run_id": "run-race", "command": "power_off"},
        time.monotonic(),
    )
    statuses = list(t._unacked)
    assert _states(statuses) == ["failed"]
    assert statuses[0]["error_code"] == "duplicate"


def test_remembered_runs_are_bounded():
    from plexus.ws import MAX_REMEMBERED_RUNS

    t = WebSocketTransport(
        api_key="plx_test_abc", source_id="pod-07", ws_url="ws://127.0.0.1:1",
    )
    t.declare_command(_declaration("noop", lambda run: {}))
    for i in range(MAX_REMEMBERED_RUNS + 20):
        t._handle_command_run(
            {"type": "command_run", "run_id": f"r{i}", "command": "noop"},
            time.monotonic(),
        )
    assert len(t._runs) == MAX_REMEMBERED_RUNS


# ---------------------------------------------------------------- concurrency


def test_concurrency_reject_answers_busy(gateway):
    release = threading.Event()
    started = threading.Event()
    calls: list[str] = []
    px = _client(gateway)

    @px.command("init_pump", concurrency="reject")
    def init_pump(run):
        calls.append(run.id)
        started.set()
        release.wait(timeout=3)
        return {"done": True}

    try:
        assert px._ws.wait_authenticated(timeout=3)
        gateway.send_run("run-a", "init_pump")
        assert started.wait(timeout=3)

        gateway.send_run("run-b", "init_pump")
        assert _wait_until(lambda: gateway.statuses("run-b"))
        busy = gateway.statuses("run-b")[0]
        assert busy["state"] == "failed"
        assert busy["error_code"] == "busy"
        assert calls == ["run-a"]

        release.set()
        assert _wait_until(lambda: _reached(gateway, "run-a", "succeeded"))

        # The next run is accepted again once the first finishes.
        gateway.send_run("run-c", "init_pump")
        assert _wait_until(lambda: _reached(gateway, "run-c", "succeeded"))
    finally:
        release.set()
        px.close()


def test_concurrency_accept_allows_overlap(gateway):
    release = threading.Event()
    active: list[int] = []
    px = _client(gateway)

    @px.command("scan")
    def scan(run):
        active.append(1)
        release.wait(timeout=3)
        return {}

    try:
        assert px._ws.wait_authenticated(timeout=3)
        gateway.send_run("run-1", "scan")
        gateway.send_run("run-2", "scan")
        assert _wait_until(lambda: len(active) == 2)
    finally:
        release.set()
        px.close()


# ------------------------------------------------------ reconnect and replay


def test_unacked_statuses_replay_after_reconnect(gateway):
    px = _client(gateway)

    @px.command("power_off")
    def power_off(run):
        return {"state": "off"}

    try:
        assert px._ws.wait_authenticated(timeout=3)
        gateway.send_run("run-keep", "power_off")
        assert _wait_until(lambda: len(gateway.statuses("run-keep")) >= 3)

        # The server acknowledges the first two statuses only.
        gateway.send_status_ack("run-keep", 2)
        time.sleep(0.2)

        gateway.received.clear()
        gateway.drop_connection()
        assert _wait_until(lambda: gateway.connections >= 2, timeout=10)
        assert _wait_until(lambda: gateway.frames("command_sync"), timeout=10)

        sync = gateway.frames("command_sync")[0]
        assert sync["runs"] == [{"run_id": "run-keep", "state": "succeeded", "seq": 3}]

        # Only the unacknowledged status is replayed, unchanged.
        assert _wait_until(lambda: gateway.statuses("run-keep"))
        replayed = gateway.statuses("run-keep")
        assert len(replayed) == 1
        assert replayed[0]["seq"] == 3
        assert replayed[0]["state"] == "succeeded"
        assert replayed[0]["result"] == {"state": "off"}
    finally:
        px.close()


def test_status_produced_while_disconnected_is_sent_after_reconnect(gateway):
    release = threading.Event()
    started = threading.Event()
    px = _client(gateway)

    @px.command("slow_job")
    def slow_job(run):
        started.set()
        release.wait(timeout=5)
        return {"done": True}

    try:
        assert px._ws.wait_authenticated(timeout=3)
        gateway.send_run("run-blip", "slow_job")
        assert started.wait(timeout=3)

        # The socket dies mid-run; the handler finishes into a dead socket.
        gateway.received.clear()
        gateway.drop_connection()
        assert _wait_until(lambda: not px._ws.is_authenticated, timeout=5)
        release.set()

        assert _wait_until(lambda: gateway.connections >= 2, timeout=10)
        assert _wait_until(lambda: _reached(gateway, "run-blip", "succeeded"), timeout=10)
        final = [s for s in gateway.statuses("run-blip") if s["state"] == "succeeded"][0]
        assert final["result"] == {"done": True}
    finally:
        release.set()
        px.close()


def test_unacked_buffer_is_bounded():
    from plexus.ws import MAX_UNACKED_STATUSES

    t = WebSocketTransport(
        api_key="plx_test_abc", source_id="pod-07", ws_url="ws://127.0.0.1:1",
    )
    for i in range(MAX_UNACKED_STATUSES + 10):
        t._emit_status(f"run-{i}", "succeeded", result={"i": i})
    assert len(t._unacked) == MAX_UNACKED_STATUSES
    # Oldest go first.
    assert t._unacked[0]["run_id"] == "run-10"


# ------------------------------------------------------------ store_results


def test_store_results_false_suppresses_payloads(metadata_only_gateway):
    gateway = metadata_only_gateway
    px = _client(gateway)

    @px.command("run_detection")
    def run_detection(run):
        run.progress(50, "halfway")
        return {"rows": 4096, "customer": "data"}

    @px.command("explode")
    def explode(run):
        raise RuntimeError("secret detail from the customer network")

    try:
        assert px._ws.wait_authenticated(timeout=3)
        assert px._ws.store_results is False

        gateway.send_run("run-quiet", "run_detection")
        assert _wait_until(lambda: _reached(gateway, "run-quiet", "succeeded"))
        final = gateway.statuses("run-quiet")[-1]
        assert final["state"] == "succeeded"
        assert "result" not in final

        gateway.send_run("run-loud", "explode")
        assert _wait_until(lambda: _reached(gateway, "run-loud", "failed"))
        failed = gateway.statuses("run-loud")[-1]
        assert failed["state"] == "failed"
        assert failed["error_code"] == "handler_error"
        assert "error_message" not in failed

        # Not even a progress message gets through.
        for status in gateway.statuses("run-quiet"):
            assert "message" not in status
    finally:
        px.close()


def test_store_results_defaults_to_true_when_absent(gateway):
    px = _client(gateway)

    @px.command("noop")
    def noop(run):
        return {"ok": True}

    try:
        assert px._ws.wait_authenticated(timeout=3)
        assert px._ws.store_results is True
    finally:
        px.close()


# ------------------------------------------------------------------- serve()


def test_serve_blocks_until_stop_serving(gateway):
    px = _client(gateway)

    @px.command("power_off")
    def power_off(run):
        return {}

    returned = threading.Event()

    def _serve():
        px.serve()
        returned.set()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        assert px._ws.wait_authenticated(timeout=3)
        # Still serving a second later.
        assert not returned.wait(timeout=0.5)
        px.stop_serving()
        assert returned.wait(timeout=3)
    finally:
        px.stop_serving()
        thread.join(timeout=3)
    # serve() closes the client on the way out.
    assert px._ws is None


def test_serve_restores_signal_handlers(gateway):
    px = _client(gateway)
    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    px.serve(timeout=0.1)
    after = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    assert before == after


# ----------------------------------------------------------- legacy fallback


def test_on_command_still_works_and_warns(gateway):
    px = _client(gateway)

    with pytest.deprecated_call():
        px.on_command("reboot", lambda name, params: {"ok": True})

    try:
        assert px._ws.wait_authenticated(timeout=3)
        px._ws._handle_command({"id": "cmd-1", "command": "reboot", "params": {}})
        assert _wait_until(lambda: len([
            m for m in gateway.received
            if m.get("type") == "command_result" and m.get("id") == "cmd-1"
        ]) >= 2)
        results = [
            m for m in gateway.received
            if m.get("type") == "command_result" and m.get("id") == "cmd-1"
        ]
        assert results[0]["event"] == "ack"
        assert results[1]["result"] == {"ok": True}
    finally:
        px.close()
