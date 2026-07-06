import struct
from unittest import mock
import unittest

from minebot.game.errors import RconError
from minebot.game.rcon import MAX_PACKET_BYTES, RconClient, RconConfig


def _packet(resp_id: int, kind: int, body: str) -> bytes:
    """One full RCON response packet: size prefix + (resp_id, kind, body\\0\\0)."""
    body_bytes = struct.pack("<ii", resp_id, kind) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(body_bytes)) + body_bytes


class _FakeSock:
    """Serves a fixed byte buffer as if it were a connected RCON socket."""

    def __init__(self, buf: bytes):
        self._buf = buf

    def sendall(self, _data: bytes) -> None:
        pass

    def recv(self, n: int) -> bytes:
        if not self._buf:
            # Mimic a closed socket so _recv_exact raises RconError("RCON socket closed").
            return b""
        chunk = self._buf[:n]
        self._buf = self._buf[n:]
        return chunk

    def close(self) -> None:
        self._buf = b""

    def settimeout(self, _t: float) -> None:
        pass


class _SocketQueueClient(RconClient):
    """Hands out a queued socket per (re)connect, bypassing real connect + auth."""

    def __init__(self, sockets, *, reconnect_attempts: int = 1):
        super().__init__(RconConfig(reconnect_attempts=reconnect_attempts, reconnect_backoff_s=0.0))
        self._queue = list(sockets)
        self.connect_count = 0
        self._sock = self._queue.pop(0)

    def connect(self) -> None:
        self.connect_count += 1
        if not self._queue:
            raise RconError("RCON socket closed")
        self._sock = self._queue.pop(0)


class ReconnectingRconClient(RconClient):
    def __init__(self):
        super().__init__(RconConfig(reconnect_backoff_s=0.0))
        self.connected = 0
        self.requests = 0

    def connect(self) -> None:
        self.connected += 1
        self._sock = object()  # type: ignore[assignment]

    def close(self) -> None:
        self._sock = None

    def _request(self, kind: int, payload: str) -> str:
        self.requests += 1
        if self.requests == 1:
            raise RconError("RCON socket closed")
        return f"{kind}:{payload}"


class RconRuntimeTests(unittest.TestCase):
    def test_connect_sets_socket_read_timeout_after_tcp_connect(self):
        class Socket:
            def __init__(self):
                self.timeout = None
                self.sent = []
                self._buf = _packet(1, 2, "")

            def settimeout(self, value):
                self.timeout = value

            def sendall(self, data):
                self.sent.append(data)

            def recv(self, n):
                chunk = self._buf[:n]
                self._buf = self._buf[n:]
                return chunk

            def close(self):
                pass

        sock = Socket()
        with mock.patch("socket.create_connection", return_value=sock) as create_connection:
            client = RconClient(RconConfig(timeout_s=2.5))

            client.connect()

        create_connection.assert_called_once_with(("127.0.0.1", 25576), timeout=2.5)
        self.assertEqual(sock.timeout, 2.5)

    def test_command_reconnects_once_after_closed_socket(self):
        client = ReconnectingRconClient()

        result = client.command("list")

        self.assertEqual(result, "2:list")
        self.assertEqual(client.connected, 2)
        self.assertEqual(client.requests, 2)
        self.assertEqual(client.stats_snapshot()["reconnects"], 1)
        self.assertEqual(client.stats_snapshot()["retry_successes"], 1)
        self.assertEqual(client.stats_snapshot()["transport_failures"], 1)
        self.assertEqual(client.stats_snapshot()["consecutive_failures"], 0)

    def test_normal_command_returns_body_when_resp_id_matches(self):
        # req_id starts at 1; a faithful Carpet echo returns the same id.
        client = _SocketQueueClient([_FakeSock(_packet(1, 2, "ok"))])

        self.assertEqual(client.command("list"), "ok")
        self.assertEqual(client.connect_count, 0)  # no reconnect needed

    def test_command_reconnects_after_response_id_mismatch(self):
        # Desync: the first packet echoes the wrong id (leftover bytes from a
        # prior truncated/fragmented response). The client must treat this as a
        # stream desync, reconnect to flush the socket, and retry — NOT parse
        # the garbage (which would surface as a protocol-envelope misalignment
        # like "expected state, got perception").
        bad = _FakeSock(_packet(999, 2, "garbage"))  # resp_id 999 != req_id 1
        good = _FakeSock(_packet(2, 2, "ok"))  # retry uses req_id 2
        client = _SocketQueueClient([bad, good])

        result = client.command("list")

        self.assertEqual(result, "ok")
        self.assertEqual(client.connect_count, 1)
        self.assertEqual(client.stats_snapshot()["reconnects"], 1)
        self.assertEqual(client.stats_snapshot()["retry_successes"], 1)

    def test_implausible_response_size_raises_desync_without_hang(self):
        # Leftover bytes read as the 4-byte size prefix can decode to a huge
        # value; _recv_exact would hang the socket until timeout. The size
        # ceiling must reject it before reading.
        huge_size = struct.pack("<i", MAX_PACKET_BYTES + 1000)
        client = _SocketQueueClient([_FakeSock(huge_size + b"x" * 8)], reconnect_attempts=0)

        with self.assertRaises(RconError) as cm:
            client.command("list")
        self.assertIn("size above ceiling", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
