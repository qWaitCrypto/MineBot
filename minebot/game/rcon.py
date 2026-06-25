"""Minimal Minecraft RCON client."""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

from minebot.game.errors import RconError
from minebot.game.transport import BodyTransport


@dataclass(frozen=True)
class RconConfig:
    host: str = "127.0.0.1"
    port: int = 25576
    password: str = "test"
    timeout_s: float = 20.0


class RconClient(BodyTransport):
    def __init__(self, config: RconConfig):
        self.config = config
        self._sock: socket.socket | None = None
        self._req_id = 1

    def __enter__(self) -> "RconClient":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        if self._sock is not None:
            return
        self._sock = socket.create_connection(
            (self.config.host, self.config.port),
            timeout=self.config.timeout_s,
        )
        self._request(3, self.config.password)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def command(self, command: str) -> str:
        if self._sock is None:
            self.connect()
        return self._request(2, command)

    def request(self, command: str) -> str:
        return self.command(command)

    def _request(self, kind: int, payload: str) -> str:
        if self._sock is None:
            raise RconError("RCON socket is not connected")
        req_id = self._req_id
        self._req_id += 1
        body = struct.pack("<ii", req_id, kind) + payload.encode("utf-8") + b"\x00\x00"
        self._sock.sendall(struct.pack("<i", len(body)) + body)
        size = struct.unpack("<i", self._recv_exact(4))[0]
        data = self._recv_exact(size)
        resp_id, _resp_kind = struct.unpack("<ii", data[:8])
        if resp_id == -1:
            raise RconError("RCON authentication failed")
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
