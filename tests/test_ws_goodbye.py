"""The gateway says goodbye before a deploy: a WebSocket close, "going away".

Behind a proxy the connection itself can stay open for seconds after that
close. The transport used to ignore the close message and keep writing, and
every reading written in that time was accepted locally and never arrived
(measured 2026-10-05: 2 to 14 seconds lost per gateway deploy).

The stub below is a raw socket on purpose. A real WebSocket server closes the
connection right after its close message, which hides the bug: the proxy case
is a close message on a connection that STAYS OPEN.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time

from plexus.ws import WebSocketTransport

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise ConnectionError("client went away")
        data += chunk
    return data


def _read_client_text(conn: socket.socket) -> str:
    """One masked client frame (the only kind a client sends)."""
    _first, second = _recv_exact(conn, 2)
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", _recv_exact(conn, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _recv_exact(conn, 8))
    mask = _recv_exact(conn, 4)
    payload = _recv_exact(conn, length)
    return bytes(b ^ mask[i % 4] for i, b in enumerate(payload)).decode()


def _server_frame(opcode: int, payload: bytes) -> bytes:
    assert len(payload) < 126
    return bytes([0x80 | opcode, len(payload)]) + payload


class _GoodbyeGateway:
    """Accepts one device, authenticates it, says goodbye, keeps the socket open."""

    def __init__(self) -> None:
        self.auth_frame: dict = {}
        self.said_goodbye = threading.Event()
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self.port = self._listener.getsockname()[1]
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        conn, _ = self._listener.accept()
        self._conns.append(conn)
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(4096)
        key = next(
            line.split(":", 1)[1].strip()
            for line in request.decode().split("\r\n")
            if line.lower().startswith("sec-websocket-key")
        )
        accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
        )
        self.auth_frame = json.loads(_read_client_text(conn))
        authenticated = {"type": "authenticated", "server_time_ms": int(time.time() * 1000)}
        conn.sendall(_server_frame(0x1, json.dumps(authenticated).encode()))
        time.sleep(0.3)
        # 1001 "going away", then nothing: the socket is deliberately left open.
        conn.sendall(_server_frame(0x8, struct.pack("!H", 1001) + b"gateway restarting"))
        self.said_goodbye.set()

    def stop(self) -> None:
        for conn in self._conns:
            conn.close()
        self._listener.close()


def test_stops_using_the_socket_the_moment_the_gateway_says_goodbye():
    gateway = _GoodbyeGateway()
    transport = WebSocketTransport(
        api_key="plx_test_abc",
        source_id="rack-01",
        ws_url=f"ws://127.0.0.1:{gateway.port}",
        auto_reconnect=False,
    )
    transport.start()
    try:
        assert transport.wait_authenticated(timeout=3)
        # The gateway only says goodbye to clients that ask for it.
        assert gateway.auth_frame["hears_goodbye"] is True

        assert gateway.said_goodbye.wait(timeout=3)
        deadline = time.monotonic() + 0.5
        while transport.is_authenticated and time.monotonic() < deadline:
            time.sleep(0.01)

        # The connection is still open, and the transport must not trust it:
        # send_points() returning False is what makes the client fall back to
        # HTTP or its local buffer instead of writing into nothing.
        assert not transport.is_authenticated
        assert transport.send_points([{"metric": "temp", "value": 1, "timestamp": 1}]) is False
    finally:
        transport.stop()
        gateway.stop()
