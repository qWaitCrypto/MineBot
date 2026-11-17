from types import SimpleNamespace

import pytest

from minebot.app.body_capability_tools import _pillar_up_tool
from minebot.app.body_capability_tools import _place_bed_and_sleep_tool, _raise_shield_tool
from minebot.body.protection import ProtectionTransactions
from minebot.contract import Event, PerceptionResult, Result, ToolResult
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientTerminal
from tests.unit.test_real_client_body import FakeClientIpc


class FakeWork:
    def __init__(self, *, y=70, result=None, final=None):
        self._reads = 0
        def state():
            self._reads += 1
            observed_y = y if self._reads == 1 else (final if final is not None else y)
            return SimpleNamespace(pos=(4.5, observed_y, 9.5))
        self.body = SimpleNamespace(get_state=state)
        self.result = result or ToolResult(True, "dig_up_target_reached", False,
                                           metrics={"target_y": y + 3, "steps_completed": 3})
        self.calls = []

    def dig_up_to_y(self, target_y, **kwargs):
        self.calls.append((target_y, kwargs))
        return self.result


def test_pillar_up_reuses_guarded_dig_up_with_default_three_block_target():
    work = FakeWork(final=73)
    tool = _pillar_up_tool(ProtectionTransactions(work))
    result = tool.callable({})
    assert result.success and result.reason == "pillar_height_reached"
    target_y, kwargs = work.calls[0]
    assert target_y == 73 and kwargs["max_steps"] == 3 and kwargs["current_pos"] == (4, 70, 9)
    assert result.metrics["position"] == [4, 73, 9]
    assert result.metrics["blocks_raised"] == 3


@pytest.mark.parametrize(("dig_reason", "position", "expected"), [
    ("dig_up_no_scaffold_available", 70, "not_enough_blocks"),
    ("dig_up_liquid_above", 70, "dig_up_liquid_above"),
    ("dig_up_no_height_gain", 71, "dig_up_no_height_gain"),
])
def test_pillar_up_returns_physical_failure_facts(dig_reason, position, expected):
    result = ToolResult(False, dig_reason, False, metrics={"steps_completed": 0})
    tool = _pillar_up_tool(ProtectionTransactions(FakeWork(result=result, final=position)))
    output = tool.callable({"blocks": 3})
    assert not output.success and output.reason == expected


def test_pillar_up_reports_displacement_from_its_column():
    work = FakeWork(final=73)
    reads = 0
    def state():
        nonlocal reads
        reads += 1
        return SimpleNamespace(pos=(4.5 if reads == 1 else 5.5, 70 if reads == 1 else 73, 9.5))
    work.body.get_state = state
    result = _pillar_up_tool(ProtectionTransactions(work)).callable({"blocks": 3})
    assert not result.success and result.reason == "displaced"
    assert result.metrics["position"] == [5, 73, 9]


@pytest.mark.parametrize("blocks", [0, 17, True, 1.5])
def test_pillar_up_rejects_unbounded_or_non_integer_counts(blocks):
    tool = _pillar_up_tool(ProtectionTransactions(FakeWork()))
    with pytest.raises(ValueError):
        tool.callable({"blocks": blocks})


class ActionIpc(FakeClientIpc):
    def __init__(self, reason, **facts):
        super().__init__()
        self.reason, self.facts = reason, facts

    def action(self, name, params, *, action_id, timeout_ms, on_progress=None):
        self.actions.append((name, dict(params), action_id, timeout_ms))
        return RealClientTerminal(action_id, "completed" if self.reason != "cannot_sleep_now" else "failed",
                                  self.reason, self.facts)


class WorkBody:
    def __init__(self, ipc):
        self.body = RealClientBody("ProtectionBot", ipc)


class ShieldActionBody:
    def __init__(self, entities=None):
        self.entities = entities if entities is not None else [{
            "id": "11111111-1111-4111-8111-111111111111", "type": "skeleton",
            "name": "Skeleton", "pos": [14.5, 70, 9.5], "dist2": 100,
        }]
        self.actions = []

    def perceive(self, scope, params):
        assert scope == "nearbyEntities"
        return PerceptionResult("ProtectionBot", scope, "perception", True, True,
                                {"entities": self.entities})

    def execute(self, action):
        self.actions.append(action)
        return Result(action.id, "ProtectionBot", "result", True, True, False)

    def await_action_terminal(self, action_id, timeout_s):
        return Event(1, 1, "ProtectionBot", "raiseShieldDone", {
            "success": True, "stopped_reason": "shield_duration_elapsed", "hits_blocked": 2,
            "hits_taken": 0, "health_before": 20, "health_after": 20,
        })


def test_shield_equips_offhand_and_reports_observed_terminal_facts():
    body = ShieldActionBody()
    calls = []
    inventory = SimpleNamespace(equip_item=lambda **kwargs: calls.append(kwargs) or ToolResult(True, "completed", False))
    protection = ProtectionTransactions(SimpleNamespace(body=body), inventory)  # type: ignore[arg-type]
    registered = _raise_shield_tool(protection)
    result = registered.callable({"target": "11111111-1111-4111-8111-111111111111", "duration_ticks": 80})
    action = body.actions[0]
    assert action.name == "raiseShield"
    assert action.params["target_uuid"] == "11111111-1111-4111-8111-111111111111"
    assert action.params["duration_ticks"] == 80
    assert calls == [{"item": "shield", "target": "offhand"}]
    assert result.success
    assert result.metrics["equipped_offhand"] is True
    assert registered.projector(result.reason, result.metrics) == {
        "hits_blocked": 2, "hits_taken": 0, "health_before": 20, "health_after": 20,
        "equipped_offhand": True,
    }


def test_shield_returns_plain_target_not_found_without_equipping():
    body = ShieldActionBody(entities=[])
    calls = []
    inventory = SimpleNamespace(equip_item=lambda **kwargs: calls.append(kwargs))
    result = _raise_shield_tool(ProtectionTransactions(SimpleNamespace(body=body), inventory)).callable(
        {"target": "11111111-1111-4111-8111-111111111111"}
    )
    assert not result.success and result.reason == "target_not_found"
    assert calls == [] and body.actions == []


def test_shield_reports_no_shield_when_shared_equip_path_cannot_find_one():
    body = ShieldActionBody()
    inventory = SimpleNamespace(equip_item=lambda **kwargs: ToolResult(False, "item_not_available", False))
    result = _raise_shield_tool(ProtectionTransactions(SimpleNamespace(body=body), inventory)).callable(
        {"target": "11111111-1111-4111-8111-111111111111"}
    )
    assert not result.success and result.reason == "no_shield"
    assert result.metrics == {"equipped_offhand": False}
    assert body.actions == []


def test_bed_sleep_denial_keeps_game_message_and_recovery_facts():
    ipc = ActionIpc("cannot_sleep_now", bed_placed=True, bed_recovered=True,
                    bed_game_message="You may not rest now; there are monsters nearby.",
                    bed_inventory_before=1, bed_inventory_after=1, sleeping=False)
    registered = _place_bed_and_sleep_tool(ProtectionTransactions(WorkBody(ipc)))  # type: ignore[arg-type]
    result = registered.callable({})
    assert not result.success and result.reason == "cannot_sleep_now"
    summary = registered.projector(result.reason, result.metrics)
    assert summary["bed_game_message"].startswith("You may not rest now")
    assert summary["bed_recovered"] is True and summary["bed_inventory_after"] == 1


def test_pillar_up_reports_missing_blocks_before_moving():
    work = FakeWork()
    work.body = SimpleNamespace(get_state=lambda: SimpleNamespace(pos=(4.5, 70, 9.5), inventory_counts={"minecraft:cobblestone": 2}))
    result = _pillar_up_tool(ProtectionTransactions(work)).callable({})
    assert not result.success and result.reason == "not_enough_blocks"
    assert result.metrics["blocks_needed"] == 3 and result.metrics["blocks_held"] == 2
    assert work.calls == []


def test_pillar_up_dispatches_the_native_pillar_controller_on_the_real_client():
    sent = []

    class Objective:
        def execute(self, action):
            sent.append(action)
            return SimpleNamespace(ok=True, accepted=True, complete=False, error=None, data={})

        def await_action_terminal(self, action_id, timeout_s):
            return SimpleNamespace(name="pillarUpDone", data={"status": "completed", "reason": "pillar_height_reached"})

    work = FakeWork(final=73)
    work.body = SimpleNamespace(get_state=iter([
        SimpleNamespace(pos=(4.5, 70, 9.5), inventory_counts={"cobblestone": 5}),
        SimpleNamespace(pos=(4.5, 73, 9.5), inventory_counts={"cobblestone": 2}),
    ]).__next__)
    result = _pillar_up_tool(ProtectionTransactions(work, objective_body=Objective())).callable({})
    assert sent[0].name == "pillarUp" and sent[0].params["blocks"] == 3
    assert work.calls == []
    assert result.metrics["blocks_raised"] == 3
