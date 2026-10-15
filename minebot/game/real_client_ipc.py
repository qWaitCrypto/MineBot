"""Length-prefixed IPC for the real-client Body adapter.

The wire stays intentionally small: correlated state/world/control replies and
one cancellable Baritone objective per client connection. Semantic actions are
assembled by :mod:`minebot.game.real_client_body`, not by this transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
import json
import socket
import struct
import threading
import time
from typing import Any, Callable, Mapping

from .real_client_frame import learn_client_state_fields
from .real_client_protocol import MAX_FRAME_BYTES, RealClientProtocolError


class RealClientIpcError(RuntimeError):
    """The disposable client process did not complete the IPC exchange."""


class RealClientRejected(RealClientIpcError):
    """The client received the request and refused it with a concrete reason."""


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
        self._send_lock = threading.Lock()
        self._connection_lock = threading.Lock()
        self._incoming = threading.Condition()
        self._messages: deque[dict[str, Any]] = deque()
        self._read_error: Exception | None = None
        self._event_listener: Callable[[dict[str, Any]], None] | None = None

    def set_event_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        """Deliver lifecycle events separately from correlated action replies."""
        with self._incoming:
            self._event_listener = listener
            events = [item for item in self._messages if item.get("type") == "body_event"]
            self._messages = deque(item for item in self._messages if item.get("type") != "body_event")
        for event in events:
            listener(event)

    def __enter__(self) -> "RealClientIpc":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        with self._connection_lock:
            if self._socket is not None:
                return
            connection = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
            connection.settimeout(None)
            with self._incoming:
                self._messages.clear()
                self._read_error = None
            self._socket = connection
            threading.Thread(
                target=self._read_loop, args=(connection,),
                name="minebot-client-ipc-reader", daemon=True,
            ).start()

    def close(self) -> None:
        with self._connection_lock:
            connection, self._socket = self._socket, None
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            with self._incoming:
                self._read_error = RealClientIpcError("IPC closed")
                self._incoming.notify_all()

    def wait_ready(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (self.timeout_s if timeout_s is None else timeout_s)
        while True:
            message = self._take(lambda value: value.get("type") == "ready", deadline)
            if message.get("type") == "ready":
                return message
            if time.monotonic() >= deadline:
                raise RealClientIpcError("client did not become ready")

    def state(self, *, request_id: str | None = None) -> dict[str, Any]:
        request_id = request_id or self._request_id()
        self.send({"type": "state", "request_id": request_id})
        deadline = time.monotonic() + self.timeout_s
        while True:
            message = self._take(lambda value: value.get("request_id") == request_id, deadline)
            if message.get("type") == "rejected":
                raise RealClientIpcError(str(message.get("reason", "state rejected")))
            if message.get("type") == "state" and message.get("request_id") == request_id:
                learn_client_state_fields(message)
                return message

    def scan(self, target: str) -> dict[str, Any]:
        request_id = self._request_id()
        self.send({"type": "scan", "request_id": request_id, "target": target})
        deadline = time.monotonic() + self.timeout_s
        while True:
            message = self._take(lambda value: value.get("request_id") == request_id, deadline)
            if message.get("type") == "rejected":
                raise RealClientIpcError(str(message.get("reason", "scan rejected")))
            if message.get("type") == "scan" and message.get("request_id") == request_id:
                return message
            if time.monotonic() >= deadline:
                raise RealClientIpcError(f"scan timed out: {target}")

    def world_read(self, scope: str, params: Mapping[str, object]) -> dict[str, Any]:
        return self.control("world_read", scope=scope, params=dict(params))

    def control(self, request_type: str, **fields: object) -> dict[str, Any]:
        """Send one bounded, non-process control request to the client.

        Controls are deliberately separate from ``command``: they change
        Baritone configuration or process state but do not create a long-lived
        physical objective.  The response is correlated by request id so a
        future client implementation can safely add progress messages.
        """
        if not request_type or request_type in {"command", "cancel", "state", "scan"}:
            raise ValueError(f"invalid real-client control type: {request_type!r}")
        request_id = self._request_id("control")
        self.send({"type": request_type, "request_id": request_id, **fields})
        deadline = time.monotonic() + self.timeout_s
        while True:
            message = self._take(lambda value: value.get("request_id") == request_id, deadline)
            if message.get("type") == "rejected" and message.get("request_id") == request_id:
                raise RealClientRejected(str(message.get("reason", "control rejected")))
            if message.get("type") == request_type and message.get("request_id") == request_id:
                return message
            if time.monotonic() >= deadline:
                raise RealClientIpcError(f"control timed out: {request_type}")

    def settings(self, values: Mapping[str, object]) -> dict[str, Any]:
        return self.control("settings", **dict(values))

    def builder_pause(self) -> dict[str, Any]:
        return self.control("builder_pause")

    def builder_resume(self) -> dict[str, Any]:
        return self.control("builder_resume")

    def close_screen(self) -> dict[str, Any]:
        return self.control("close_screen")

    def explore_filter(self, path: str, *, invert: bool = False) -> dict[str, Any]:
        return self.control("explore_filter", path=path, invert=invert)

    def builder_open_schematic(self) -> dict[str, Any]:
        return self.control("builder_open_schematic")

    def builder_open_litematic(self, index: int = 0) -> dict[str, Any]:
        return self.control("builder_open_litematic", index=index)

    def elytra_repack(self) -> dict[str, Any]:
        return self.control("elytra_repack")

    def elytra_reset(self) -> dict[str, Any]:
        return self.control("elytra_reset")

    def baritone_command(self, command: str) -> dict[str, Any]:
        return self.control("baritone_command", command=command)

    def command(
        self,
        objective: str,
        *,
        action_id: str | None = None,
        timeout_ms: int = 60_000,
        cancel_after_s: float | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> RealClientTerminal:
        action_id = action_id or self._request_id("action")
        self.send({
            "type": "command",
            "action_id": action_id,
            "objective": objective,
            "timeout_ms": timeout_ms,
        })
        return self._wait_terminal(action_id, timeout_ms, cancel_after_s, on_progress)

    def action(
        self, name: str, params: Mapping[str, object], *, action_id: str,
        timeout_ms: int = 60_000,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> RealClientTerminal:
        self.send({
            "type": "command", "action_id": action_id, "timeout_ms": timeout_ms,
            "action": {"name": name, "params": dict(params)},
        })
        return self._wait_terminal(action_id, timeout_ms, None, on_progress)

    def _wait_terminal(
        self, action_id: str, timeout_ms: int, cancel_after_s: float | None,
        on_progress: Callable[[dict[str, Any]], None] | None,
    ) -> RealClientTerminal:
        sent_at = time.monotonic()
        cancel_sent_at: float | None = None
        deadline = sent_at + max(self.timeout_s, timeout_ms / 1000 + 5)
        while True:
            next_deadline = deadline
            if cancel_after_s is not None and cancel_sent_at is None:
                next_deadline = min(deadline, sent_at + cancel_after_s)
            try:
                message = self._take(lambda value: value.get("action_id") == action_id, next_deadline)
            except TimeoutError:
                if cancel_after_s is not None and cancel_sent_at is None:
                    self.cancel(action_id)
                    cancel_sent_at = time.monotonic()
                    continue
                raise RealClientIpcError(f"action timed out: {action_id}") from None
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
            if message.get("type") == "progress" and on_progress is not None:
                try:
                    on_progress(message)
                except Exception:
                    # Progress observers never own action settlement.
                    pass
            if time.monotonic() >= deadline:
                raise RealClientIpcError(f"action timed out: {action_id}")

    def cancel(self, action_id: str) -> None:
        self.send({"type": "cancel", "action_id": action_id})

    def take_late_terminal(self, action_id: str) -> dict[str, Any] | None:
        """Nonblocking archive pickup, only after the original waiter has ended."""
        with self._incoming:
            for index, message in enumerate(self._messages):
                if message.get("type") == "terminal" and message.get("action_id") == action_id:
                    del self._messages[index]
                    return dict(message)
        return None

    def send(self, message: Mapping[str, object]) -> None:
        raw = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if not 1 <= len(raw) <= MAX_FRAME_BYTES:
            raise RealClientProtocolError("IPC frame is outside the bounded payload size")
        with self._connection_lock:
            connection = self._socket
            if connection is None:
                raise RealClientIpcError("IPC is not connected")
            with self._send_lock:
                connection.sendall(struct.pack(">I", len(raw)) + raw)

    def receive(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        return self._take(
            lambda value: True,
            time.monotonic() + (self.timeout_s if timeout_s is None else timeout_s),
        )

    def _take(self, matches: Callable[[dict[str, Any]], bool], deadline: float) -> dict[str, Any]:
        if self._socket is None:
            raise RealClientIpcError("IPC is not connected")
        with self._incoming:
            while True:
                for index, message in enumerate(self._messages):
                    if matches(message):
                        del self._messages[index]
                        return message
                if self._read_error is not None:
                    raise RealClientIpcError("IPC receive failed") from self._read_error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("IPC response deadline exceeded")
                self._incoming.wait(remaining)

    def _read_loop(self, connection: socket.socket) -> None:
        try:
            while True:
                message = self._read_frame(connection)
                listener = self._event_listener
                if message.get("type") == "body_event" and listener is not None:
                    listener(message)
                    continue
                with self._incoming:
                    if self._socket is not connection:
                        return
                    self._messages.append(message)
                    self._incoming.notify_all()
        except Exception as error:
            with self._incoming:
                if self._socket is connection:
                    self._read_error = error
                    self._incoming.notify_all()

    def _read_frame(self, connection: socket.socket) -> dict[str, Any]:
        try:
            header = self._receive_exact(connection, 4)
            length = struct.unpack(">I", header)[0]
            if not 1 <= length <= MAX_FRAME_BYTES:
                raise RealClientProtocolError(f"IPC frame length is invalid: {length}")
            raw = self._receive_exact(connection, length)
        except (socket.timeout, OSError, EOFError) as error:
            raise RealClientIpcError("IPC receive failed") from error
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RealClientProtocolError("IPC frame is not JSON") from error
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            raise RealClientProtocolError("IPC message must be an object with type")
        return message

    def _receive_exact(self, connection: socket.socket, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            chunk = connection.recv(length - len(data))
            if not chunk:
                raise EOFError("IPC peer closed connection")
            data.extend(chunk)
        return bytes(data)

    def _request_id(self, prefix: str = "request") -> str:
        with self._send_lock:
            self._next_request += 1
            return f"{prefix}-{self._next_request}"
