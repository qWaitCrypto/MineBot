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

# Minecraft's RCON implementation splits a command response every 4096 Java
# string characters, with no end marker. UTF-8 encoding can make one such
# chunk larger than 4096 bytes, so the framing guard must allow the protocol's
# real upper bound while still rejecting implausible stream-desync sizes.
RCON_RESPONSE_CHUNK_CHARS = 4096
MAX_PACKET_BYTES = (RCON_RESPONSE_CHUNK_CHARS * 3) + 10
RCON_RESPONSE_DRAIN_TIMEOUT_S = 0.25
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
    response_drain_timeout_s: float = RCON_RESPONSE_DRAIN_TIMEOUT_S


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
            self._sock.settimeout(self.config.timeout_s)
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

    def request_once(self, command: str) -> str:
        """Dispatch one command without replaying it after a lost response.

        This is reserved for Body mutation dispatches.  The caller owns the
        action-level reconciliation decision after a transport failure.
        """

        with self._lock:
            if self._sock is None:
                self.connect()
            try:
                return self._request(2, command)
            except (OSError, RconError):
                self._transport_failures += 1
                self._consecutive_failures += 1
                raise

    def reconnect(self) -> None:
        """Reset the socket so a caller can perform read-only reconciliation."""

        with self._lock:
            self.close()
            self.connect()
            self._reconnects += 1
            self._consecutive_failures = 0

    def _request(self, kind: int, payload: str) -> str:
        if self._sock is None:
            raise RconError("RCON socket is not connected")
        if kind == 2:
            self._requests += 1
        req_id = self._req_id
        self._req_id += 1
        body = struct.pack("<ii", req_id, kind) + payload.encode("utf-8") + b"\x00\x00"
        self._sock.sendall(struct.pack("<i", len(body)) + body)
        response_parts: list[str] = []
        packet = self._read_response_packet()
        while True:
            _size, resp_id, _resp_kind, text = packet
            if resp_id == -1:
                raise RconError("RCON authentication failed")
            # Carpet echoes the request id on every response. A mismatch means
            # an older packet was already left in the stream; reconnect before
            # parsing it as a new logical response.
            if kind == 2 and resp_id != req_id:
                raise RconError(_DESYNC_ID)
            response_parts.append(text)

            # Minecraft sends no response terminator. A short packet is the
            # final chunk; a full chunk may have a continuation, so probe for
            # one with a bounded read timeout. This consumes all packets before
            # returning, preventing a large read-only response from poisoning
            # the next request while preserving request_once's no-replay rule.
            if len(text) < RCON_RESPONSE_CHUNK_CHARS:
                break
            packet = self._read_optional_response_packet()
            if packet is None:
                break
            # Continue with the next packet; a short packet terminates the
            # loop, while another full packet asks for one more bounded probe.
        return "".join(response_parts)

    def _read_response_packet(self) -> tuple[int, int, int, str]:
        header = self._recv_exact(4)
        return self._decode_response_packet(header)

    def _decode_response_packet(self, header: bytes) -> tuple[int, int, int, str]:
        size = struct.unpack("<i", header)[0]
        # Desync guard: a leftover packet's bytes read as this request's size
        # prefix can be implausible. Reject before _recv_exact hangs the socket.
        if size < 10 or size > MAX_PACKET_BYTES:
            raise RconError(_DESYNC_SIZE)
        data = self._recv_exact(size)
        resp_id, resp_kind = struct.unpack("<ii", data[:8])
        if data[-2:] != b"\x00\x00":
            raise RconError("RCON response missing terminator")
        return size, resp_id, resp_kind, data[8:-2].decode("utf-8", errors="replace")

    def _read_optional_response_packet(self) -> tuple[int, int, int, str] | None:
        if self._sock is None:
            raise RconError("RCON socket is not connected")
        try:
            self._sock.settimeout(
                min(
                    self.config.timeout_s,
                    max(0.001, self.config.response_drain_timeout_s),
                )
            )
            # A timeout before reading any byte means the full previous packet
            # was the final chunk. Once a header byte has arrived, however,
            # timeout is a partial packet and must fail closed; discarding it
            # would leave the stream misframed for the next request.
            header = self._recv_optional_exact(4)
            if header is None:
                return None
            return self._decode_response_packet(header)
        except socket.timeout as exc:
            raise RconError("RCON response drain timed out mid-packet") from exc
        finally:
            # Keep the configured timeout for the next logical request.
            if self._sock is not None:
                try:
                    self._sock.settimeout(self.config.timeout_s)
                except OSError:
                    # The peer may have closed the socket while the optional
                    # read was in flight; preserve the original transport
                    # error for the caller instead of masking it here.
                    pass

    def _recv_optional_exact(self, n: int) -> bytes | None:
        if self._sock is None:
            raise RconError("RCON socket is not connected")
        chunks: list[bytes] = []
        remaining = n
        while remaining:
            try:
                chunk = self._sock.recv(remaining)
            except socket.timeout:
                if not chunks:
                    return None
                raise
            if not chunk:
                raise RconError("RCON socket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

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
