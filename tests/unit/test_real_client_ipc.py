from __future__ import annotations

import json
import socket
import struct
import threading

import pytest

from minebot.game.real_client_ipc import RealClientIpc, RealClientIpcError, RealClientNotAccepted, RealClientRejected, RealClientTerminal


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


def test_ipc_control_preserves_a_correlated_client_refusal() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def server() -> None:
        connection, _ = listener.accept()
        with connection:
            request = _recv(connection)
            _send(connection, {"type": "rejected", "request_id": request["request_id"],
                               "reason": "destination_occupied"})
        listener.close()

    thread = threading.Thread(target=server)
    port = listener.getsockname()[1]
    thread.start()
    with RealClientIpc(port=port) as client:
        with pytest.raises(RealClientRejected, match="destination_occupied"):
            client.control("primitive", action_name="moveItem", params={"from_slot": 9, "to_slot": 27})
    thread.join()


def _serve(replies):
    """A one-request server; ``replies`` maps the command to what the client sends back."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def server() -> None:
        connection, _ = listener.accept()
        with connection:
            request = _recv(connection)
            cancel = {}
            for reply in replies(request):
                if reply in ("await_cancel", "await_command"):
                    received = _recv(connection)
                    assert received["type"] == reply.split("_")[1]
                    if reply == "await_cancel":
                        cancel = received
                    continue
                # The client echoes the request_id of the request it refuses.
                if reply.get("request_id") == "<cancel>":
                    reply = {**reply, "request_id": cancel["request_id"]}
                _send(connection, reply)
        listener.close()

    thread = threading.Thread(target=server)
    thread.start()
    return listener.getsockname()[1], thread


def test_a_command_refused_before_it_started_is_not_accepted() -> None:
    port, thread = _serve(lambda request: [
        {"type": "rejected", "action_id": request["action_id"], "request_id": "", "reason": "invalid_fixture_size"},
    ])
    with RealClientIpc(port=port) as client:
        with pytest.raises(RealClientNotAccepted, match="invalid_fixture_size") as raised:
            client.command("build:oak_planks:0:70:0:30:1:20", action_id="b1")
    thread.join()
    assert raised.value.reason == "invalid_fixture_size"


def test_a_refusal_after_acceptance_stays_unresolved() -> None:
    port, thread = _serve(lambda request: [
        {"type": "accepted", "action_id": request["action_id"]},
        {"type": "rejected", "action_id": request["action_id"], "request_id": "", "reason": "request_failed"},
    ])
    with RealClientIpc(port=port) as client:
        with pytest.raises(RealClientIpcError) as raised:
            client.command("goto:4:70:9", action_id="b2")
    thread.join()
    assert not isinstance(raised.value, RealClientRejected)


def test_a_reused_action_id_is_not_read_as_not_started() -> None:
    port, thread = _serve(lambda request: [
        {"type": "rejected", "action_id": request["action_id"], "request_id": "", "reason": "duplicate_action_id"},
    ])
    with RealClientIpc(port=port) as client:
        with pytest.raises(RealClientIpcError) as raised:
            client.command("goto:4:70:9", action_id="b3")
    thread.join()
    assert not isinstance(raised.value, RealClientRejected)


def test_a_refused_cancel_waits_for_the_action_terminal() -> None:
    port, thread = _serve(lambda request: [
        {"type": "accepted", "action_id": request["action_id"]},
        "await_cancel",
        {"type": "rejected", "action_id": request["action_id"], "request_id": "<cancel>", "reason": "action_not_active"},
        {"type": "terminal", "action_id": request["action_id"], "status": "completed", "reason": "goal_reached"},
    ])
    with RealClientIpc(port=port) as client:
        result = client.command("goto:4:70:9", action_id="b4", cancel_after_s=0.05)
    thread.join()
    assert result.completed and result.reason == "goal_reached"


def test_a_lost_client_is_reported_and_a_new_connection_can_be_opened() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = listener.getsockname()[1]
    second = threading.Event()

    def server() -> None:
        connection, _ = listener.accept()
        connection.close()  # the client process quits
        again, _ = listener.accept()
        second.set()
        again.close()
        listener.close()

    thread = threading.Thread(target=server)
    thread.start()
    events = []
    lost = threading.Event()
    client = RealClientIpc(port=port)
    client.set_event_listener(lambda event: (events.append(event), lost.set()))
    client.connect()
    assert lost.wait(2.0)
    assert events[0]["name"] == "world_disconnected"
    assert events[0]["data"]["source"] == "python_ipc"
    client.connect()  # not skipped because of the dead socket
    assert second.wait(2.0)
    client.close()
    thread.join()


def test_a_cancel_that_reaches_the_client_before_its_command_is_not_a_refused_command() -> None:
    """Review 2026-10-17: an interrupt can cancel a reserved action before the
    worker sends the command; the client refuses the cancel, then runs it."""
    def replies(cancel):
        assert cancel["type"] == "cancel"
        return [
            {"type": "rejected", "action_id": cancel["action_id"], "request_id": cancel["request_id"],
             "reason": "action_not_active"},
            "await_command",
            {"type": "accepted", "action_id": cancel["action_id"]},
            {"type": "terminal", "action_id": cancel["action_id"], "status": "completed", "reason": "goal_reached"},
        ]
    port, thread = _serve(replies)
    with RealClientIpc(port=port) as client:
        client.cancel("b5")
        result = client.command("goto:4:70:9", action_id="b5")
    thread.join()
    assert result.completed


def test_a_reply_that_arrived_before_the_loss_survives_a_reconnect() -> None:
    """Review 2026-10-17: connect() emptied the inbox, so a terminal the reader
    had queued before EOF was gone when its waiter looked."""
    import time

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = listener.getsockname()[1]

    def server() -> None:
        connection, _ = listener.accept()
        _send(connection, {"type": "terminal", "action_id": "c1", "status": "completed", "reason": "goal_reached"})
        connection.close()
        again, _ = listener.accept()
        again.close()
        listener.close()

    thread = threading.Thread(target=server)
    thread.start()
    lost = threading.Event()
    client = RealClientIpc(port=port)
    client.set_event_listener(lambda event: lost.set())
    client.connect()
    assert lost.wait(2.0)
    client.connect()
    reply = client._take(lambda message: message.get("action_id") == "c1", time.monotonic() + 1.0)
    assert reply["status"] == "completed"
    client.close()
    thread.join()


def test_ipc_command_carries_look_down_only_when_asked() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    requests: list[dict] = []

    def server() -> None:
        connection, _ = listener.accept()
        with connection:
            for _ in range(2):
                request = _recv(connection)
                requests.append(request)
                _send(connection, {"type": "accepted", "action_id": request["action_id"]})
                _send(connection, {"type": "terminal", "action_id": request["action_id"],
                                   "status": "completed", "reason": "goal_reached"})
        listener.close()

    thread = threading.Thread(target=server)
    thread.start()
    with RealClientIpc(port=port) as client:
        client.command("near:4:70:9:1", action_id="ld1", look_down=True)
        client.command("near:4:70:9:1", action_id="ld2")
    thread.join()

    assert requests[0]["look_down"] is True
    assert "look_down" not in requests[1]
