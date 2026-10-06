from __future__ import annotations

import json
import socket
import struct
import threading

from minebot.game.real_client_ipc import RealClientIpc, RealClientTerminal


def _send(connection: socket.socket, payload: dict[str, object]) -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    connection.sendall(struct.pack(">I", len(raw)) + raw)


def _recv(connection: socket.socket) -> dict[str, object]:
    header = connection.recv(4)
    length = struct.unpack(">I", header)[0]
    raw = connection.recv(length)
    while len(raw) < length:
        raw += connection.recv(length - len(raw))
    return json.loads(raw)


def test_ipc_command_returns_structured_terminal() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def server() -> None:
        connection, _ = listener.accept()
        with connection:
            request = _recv(connection)
            assert request["type"] == "command"
            _send(connection, {"type": "accepted", "action_id": request["action_id"]})
            _send(connection, {
                "type": "terminal",
                "action_id": request["action_id"],
                "status": "completed",
                "reason": "goal_reached",
                "position": [4.5, 70.0, 9.5],
                "inventory_delta": 0,
            })
        listener.close()

    thread = threading.Thread(target=server)
    thread.start()
    with RealClientIpc(port=port) as client:
        result = client.command("goto:4:70:9", action_id="a1")
    thread.join()

    assert isinstance(result, RealClientTerminal)
    assert result.completed
    assert result.reason == "goal_reached"
    assert result.payload["position"] == [4.5, 70.0, 9.5]


def test_ipc_control_is_correlated_and_returns_client_state() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def server() -> None:
        connection, _ = listener.accept()
        with connection:
            request = _recv(connection)
            assert request["type"] == "settings"
            assert request["allow_break"] is False
            _send(connection, {
                "type": "settings",
                "request_id": request["request_id"],
                "allow_break": False,
                "position": [4.5, 70.0, 9.5],
            })
        listener.close()

    thread = threading.Thread(target=server)
    thread.start()
    with RealClientIpc(port=port) as client:
        result = client.settings({"allow_break": False})
    thread.join()

    assert result["type"] == "settings"
    assert result["allow_break"] is False
