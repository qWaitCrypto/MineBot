"""Minimal Minecraft RCON client."""

from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from minebot.game.errors import RconError
from minebot.game.transport import BodyTransport

T = TypeVar("T")

# A single RCON packet's payload is capped at 4096 by the protocol; Carpet
# honors this. Any response whose declared size exceeds this ceiling is the
# signature of a stream desync (leftover bytes from a truncated/fragmented
# prior response being read as this request's 4-byte size prefix) — reading
# that many bytes would hang the socket until timeout. Cap and reconnect.
MAX_PACKET_BYTES = 8192
_DESYNC_SIZE = "RCON response size above ceiling: stream desynced"
_DESYNC_ID = "RCON response id mismatch: stream desynced"


@dataclass(frozen=True)
class RconConfig:
    host: str = "127.0.0.1"
    port: int = 25576
    password: str = "test"
    timeout_s: float = 20.0
    reconnect_attempts: int = 1
    reconnect_backoff_s: float = 0.05


class RconClient(BodyTransport):
    def __init__(self, config: RconConfig):
        self.config = config
        self._sock: socket.socket | None = None
        self._req_id = 1
        self._lock = threading.RLock()
        self._requests = 0
        self._reconnects = 0
        self._retry_successes = 0
        self._transport_failures = 0
        self._consecutive_failures = 0

    def __enter__(self) -> "RconClient":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        with self._lock:
            if self._sock is not None:
                return
            self._sock = socket.create_connection(
                (self.config.host, self.config.port),
                timeout=self.config.timeout_s,
            )
            self._request(3, self.config.password)

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                self._sock.close()
                self._sock = None

    def command(self, command: str) -> str:
        with self._lock:
            if self._sock is None:
                self.connect()
            return self._with_reconnect_retry(lambda: self._request(2, command))

    def request(self, command: str) -> str:
        return self.command(command)

    def _request(self, kind: int, payload: str) -> str:
        if self._sock is None:
            raise RconError("RCON socket is not connected")
        if kind == 2:
            self._requests += 1
        req_id = self._req_id
        self._req_id += 1
        body = struct.pack("<ii", req_id, kind) + payload.encode("utf-8") + b"\x00\x00"
        self._sock.sendall(struct.pack("<i", len(body)) + body)
        size = struct.unpack("<i", self._recv_exact(4))[0]
        # Desync guard: a leftover packet from a truncated/fragmented prior
        # response would be read here as this request's size prefix, often as
        # an implausible value. Reject before _recv_exact hangs the socket.
        if size < 0 or size > MAX_PACKET_BYTES:
            raise RconError(_DESYNC_SIZE)
        data = self._recv_exact(size)
        resp_id, _resp_kind = struct.unpack("<ii", data[:8])
        if resp_id == -1:
            raise RconError("RCON authentication failed")
        # Desync guard: Carpet echoes the request id on every response (live
        # verified). A mismatch means this packet belongs to a previous request
        # whose bytes were left in the stream — reconnect rather than parse
        # garbage (which would surface as a protocol-envelope misalignment like
        # "expected state, got perception").
        if kind == 2 and resp_id != req_id:
            raise RconError(_DESYNC_ID)
        return data[8:-2].decode("utf-8", errors="replace")

    def _recv_exact(self, n: int) -> bytes:
        if self._sock is None:
            raise RconError("RCON socket is not connected")
        chunks: list[bytes] = []
        remaining = n
        while remaining:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise RconError("RCON socket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _with_reconnect_retry(self, call: Callable[[], T]) -> T:
        attempts = max(0, self.config.reconnect_attempts)
        for attempt in range(attempts + 1):
            try:
                result = call()
                self._consecutive_failures = 0
                if attempt:
                    self._retry_successes += 1
                return result
            except (OSError, RconError) as exc:
                self._transport_failures += 1
                self._consecutive_failures += 1
                if attempt >= attempts or not _is_reconnectable(exc):
                    raise
                self.close()
                if self.config.reconnect_backoff_s > 0:
                    time.sleep(self.config.reconnect_backoff_s * (attempt + 1))
                self.connect()
                self._reconnects += 1
        raise RconError("RCON retry loop exited unexpectedly")

    def stats_snapshot(self) -> dict[str, int]:
        return {
            "requests": self._requests,
            "reconnects": self._reconnects,
            "retry_successes": self._retry_successes,
            "transport_failures": self._transport_failures,
            "consecutive_failures": self._consecutive_failures,
        }


def _is_reconnectable(exc: BaseException) -> bool:
    if isinstance(exc, RconError):
        return str(exc) in {
            "RCON socket closed",
            "RCON socket is not connected",
            _DESYNC_SIZE,
            _DESYNC_ID,
        }
    return isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout))
