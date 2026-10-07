"""Small default regression for the current real-client Body seam.

These tests exercise public Body/provider behavior with an in-process IPC
double. World behavior is checked by the explicit live task experiments in
``TESTING.md``.
"""

from __future__ import annotations

import time

from minebot.app.body_provider import build_body_provider
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.contract import Action, Region
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientRejected
from tests.unit.test_real_client_body import FakeClientIpc


REGION = Region("quick-smoke", (-64, -64, -64), (64, 320, 64))


def test_body_reads_authoritative_state_and_inventory_slots() -> None:
    body = RealClientBody("QuickProbe", FakeClientIpc())  # type: ignore[arg-type]

    state = body.get_state()
    inventory = body.perceive("inventory", {"limit": 46})

    assert state.pos == (4.5, 70.0, 9.5)
    assert state.inventory_counts == {"oak_log": 2, "wheat_seeds": 3}
    assert inventory.ok and inventory.data["slots"][2]["item"] == "minecraft:oak_log"


def test_body_executes_navigation_and_delivers_one_terminal() -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("QuickProbe", ipc)  # type: ignore[arg-type]

    accepted = body.execute(Action("quick-move", "navigate", {
        "goal": {"kind": "near", "x": 10, "y": 70, "z": 9, "range": 1},
    }))
    terminal = body.await_action_terminal("quick-move", timeout_s=1)

    assert accepted.ok and accepted.accepted
    assert terminal.data["success"] is True
    assert ipc.commands[0][0] == "near:10:70:9:1"


def test_body_cancel_stops_the_active_wire_action() -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("QuickProbe", ipc)  # type: ignore[arg-type]

    body.execute(Action("quick-cancel", "baritoneMine", {"block": "oak_log"}))
    time.sleep(0.005)
    interrupted = body.interrupt("user_cancel")
    terminal = body.await_action_terminal("quick-cancel", timeout_s=1)

    assert interrupted.ok and interrupted.accepted
    assert ipc.canceled == ["quick-cancel"]
    assert terminal.data["status"] == "canceled"


def test_body_routes_structured_actions_without_provider_specific_calls() -> None:
    ipc = FakeClientIpc()
    body = RealClientBody("QuickProbe", ipc)  # type: ignore[arg-type]

    accepted = body.execute(Action("quick-craft", "craftItem", {"item": "stick", "count": 1}))
    terminal = body.await_action_terminal("quick-craft", timeout_s=1)

    assert accepted.ok and accepted.accepted
    assert terminal.data["success"] is True
    assert ipc.actions[0][0] == "craftItem"


def test_agent_registry_reaches_the_same_body_boundary() -> None:
    ipc = FakeClientIpc()
    runtime = build_body_provider(
        "real_client",
        bot_name="QuickProbe",
        natural_region=REGION,
        real_client_ipc=ipc,  # type: ignore[arg-type]
    )
    registry = build_phase1_registry(
        runtime.body,
        Phase1RuntimeConfig(REGION, body_provider="real_client", governance_policy=runtime.governance),
    )

    result = registry.get("move_to").callable({"pos": [10, 70, 9], "timeout_s": 1})

    assert result.success and result.reason == "arrived"
    assert ipc.commands[0][0] == "near:10:70:9:1"


def test_body_reports_a_concrete_physical_refusal() -> None:
    class RefusingIpc(FakeClientIpc):
        def control(self, request_type, **fields):
            raise RealClientRejected("destination_occupied")

    body = RealClientBody("QuickProbe", RefusingIpc())  # type: ignore[arg-type]
    body.execute(Action("quick-refusal", "moveItem", {"from_slot": 9, "to_slot": 0, "count": 1}))
    terminal = body.await_action_terminal("quick-refusal", timeout_s=1)

    assert not terminal.data["success"]
    assert terminal.data["status"] == "failed"
    assert terminal.data["reason"] == "destination_occupied"
