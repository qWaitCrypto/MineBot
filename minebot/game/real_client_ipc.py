"""Small live IPC client for the disposable real-client Body probe.

This module intentionally speaks the probe wire directly. It is not the
production Body adapter: it proves that Python can submit one objective to the
Fabric client and receive an observed terminal plus state facts.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import socket
import struct
import time
from typing import Any, Mapping

from .real_client_protocol import MAX_FRAME_BYTES, RealClientProtocolError


class RealClientIpcError(RuntimeError):
    """The disposable client process did not complete the IPC exchange."""


@dataclass(frozen=True)
class RealClientTerminal:
    action_id: str
    status: str
    reason: str
    payload: dict[str, Any]

    @property
    def completed(self) -> bool:
        return self.status == "completed"


class RealClientIpc:
    """Length-prefixed JSON client for the loopback Phase A probe."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8768, *, timeout_s: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._socket: socket.socket | None = None
        self._next_request = 0

    def __enter__(self) -> "RealClientIpc":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        if self._socket is not None:
            return
        self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        self._socket.settimeout(self.timeout_s)

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def wait_ready(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (self.timeout_s if timeout_s is None else timeout_s)
        while True:
            message = self.receive(timeout_s=max(0.01, deadline - time.monotonic()))
            if message.get("type") == "ready":
                return message
            if time.monotonic() >= deadline:
                raise RealClientIpcError("client did not become ready")

    def state(self, *, request_id: str | None = None) -> dict[str, Any]:
        request_id = request_id or self._request_id()
        self.send({"type": "state", "request_id": request_id})
        deadline = time.monotonic() + self.timeout_s
        while True:
            message = self.receive(timeout_s=max(0.01, deadline - time.monotonic()))
            if message.get("type") == "state" and message.get("request_id") == request_id:
                return message

    def scan(self, target: str) -> dict[str, Any]:
        request_id = self._request_id()
        self.send({"type": "scan", "request_id": request_id, "target": target})
        deadline = time.monotonic() + self.timeout_s
        while True:
            message = self.receive(timeout_s=max(0.01, deadline - time.monotonic()))
            if message.get("type") == "scan" and message.get("request_id") == request_id:
                return message
            if time.monotonic() >= deadline:
                raise RealClientIpcError(f"scan timed out: {target}")

    def command(
        self,
        objective: str,
        *,
        action_id: str | None = None,
        timeout_ms: int = 60_000,
        cancel_after_s: float | None = None,
    ) -> RealClientTerminal:
        action_id = action_id or self._request_id("action")
        self.send({
            "type": "command",
            "action_id": action_id,
            "objective": objective,
            "timeout_ms": timeout_ms,
        })
        sent_at = time.monotonic()
        cancel_sent_at: float | None = None
        deadline = sent_at + max(self.timeout_s, timeout_ms / 1000 + 5)
        while True:
            remaining = max(0.01, deadline - time.monotonic())
            message = self.receive(timeout_s=remaining)
            if message.get("type") == "rejected" and message.get("action_id") == action_id:
                raise RealClientIpcError(str(message.get("reason", "command rejected")))
            if (
                cancel_after_s is not None
                and cancel_sent_at is None
                and time.monotonic() - sent_at >= cancel_after_s
            ):
                self.cancel(action_id)
                cancel_sent_at = time.monotonic()
            if message.get("type") == "terminal" and message.get("action_id") == action_id:
                payload = dict(message)
                if cancel_sent_at is not None:
                    payload["python_cancel_to_terminal_ms"] = int((time.monotonic() - cancel_sent_at) * 1000)
                return RealClientTerminal(
                    action_id=action_id,
                    status=str(message.get("status", "unknown")),
                    reason=str(message.get("reason", "unknown")),
                    payload=payload,
                )
            if time.monotonic() >= deadline:
                raise RealClientIpcError(f"command timed out: {objective}")

    def cancel(self, action_id: str) -> None:
        self.send({"type": "cancel", "action_id": action_id})

    def send(self, message: Mapping[str, object]) -> None:
        if self._socket is None:
            raise RealClientIpcError("IPC is not connected")
        raw = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if not 1 <= len(raw) <= MAX_FRAME_BYTES:
            raise RealClientProtocolError("IPC frame is outside the bounded payload size")
        self._socket.sendall(struct.pack(">I", len(raw)) + raw)

    def receive(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        if self._socket is None:
            raise RealClientIpcError("IPC is not connected")
        self._socket.settimeout(self.timeout_s if timeout_s is None else timeout_s)
        try:
            header = self._receive_exact(4)
            length = struct.unpack(">I", header)[0]
            if not 1 <= length <= MAX_FRAME_BYTES:
                raise RealClientProtocolError(f"IPC frame length is invalid: {length}")
            raw = self._receive_exact(length)
        except (socket.timeout, OSError, EOFError) as error:
            raise RealClientIpcError("IPC receive failed") from error
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RealClientProtocolError("IPC frame is not JSON") from error
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            raise RealClientProtocolError("IPC message must be an object with type")
        return message

    def _receive_exact(self, length: int) -> bytes:
        assert self._socket is not None
        data = bytearray()
        while len(data) < length:
            chunk = self._socket.recv(length - len(data))
            if not chunk:
                raise EOFError("IPC peer closed connection")
            data.extend(chunk)
        return bytes(data)

    def _request_id(self, prefix: str = "request") -> str:
        self._next_request += 1
        return f"{prefix}-{self._next_request}"
