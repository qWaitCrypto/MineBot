"""The bot's own blocks are its own to remove (user 2026-11-11: the permission layer goes)."""
from types import SimpleNamespace

from minebot.app.phase1_runtime import _mine_collect_tool
from minebot.contract import BreakContext, ToolResult
from minebot.contract.governance import BotPlacement
from minebot.game.governance import GovernancePolicy


def tool_with(placements, here="stone", calls=None):
    seen = []
    body = SimpleNamespace(perceive=lambda scope, params: SimpleNamespace(ok=True, data={"type": "minecraft:" + here}))

    def mine(pos, *, context, **kwargs):
        if calls is not None:
            calls.append(kwargs)
        seen.append((pos, context))
        return ToolResult(True, "ok", False)

    work = SimpleNamespace(governance=SimpleNamespace(bot_placements=placements), body=body, mine_block_collect=mine)
    return _mine_collect_tool(work), seen


def test_a_named_block_the_bot_placed_is_removed_as_its_own_cleanup():
    door = (171, 70, 169)
    tool, seen = tool_with({door: object()}, here="oak_door")
    tool.callable({"pos": list(door), "target_block_types": ["oak_door"]})
    tool.callable({"pos": [10, 64, 10]})
    assert seen == [(door, BreakContext.BOT_CLEANUP), ((10, 64, 10), BreakContext.COLLECT)]


def test_governance_lets_the_bot_remove_its_own_block():
    policy = GovernancePolicy(bot_placements=[BotPlacement(pos=(1, 64, 1), block_type="oak_door", purpose="structure", bot="Bot")],
                              bot_name="Bot")
    assert policy.can_break((1, 64, 1), "oak_door", BreakContext.COLLECT).allowed
    assert policy.can_break((1, 64, 1), "oak_door", BreakContext.BOT_CLEANUP).allowed
    assert not policy.can_break((1, 64, 1), "cobblestone", BreakContext.BOT_CLEANUP).bot_owned


def test_a_named_cell_is_broken_whatever_block_stands_there():
    """run-45 15:44: the Agent named cobblestone, the cell held stone; refused five times."""
    calls = []
    tool, seen = tool_with({}, here="stone", calls=calls)
    result = tool.callable({"pos": [11, 63, 32], "expected_drops": ["cobblestone"], "target_block_types": ["cobblestone"]})
    assert result.success and seen == [((11, 63, 32), BreakContext.COLLECT)]
    assert calls[0]["target_block_types"] == ("stone",) and calls[0]["expected_drops"] == ()


def test_a_cell_with_no_block_is_said_so():
    for here in ("air", "water"):
        tool, seen = tool_with({}, here=here)
        result = tool.callable({"pos": [11, 64, 32], "target_block_types": ["cobblestone"]})
        assert (result.success, result.reason, result.metrics["block_type"]) == (False, "nothing_to_break", here)
        assert seen == []


def test_decision_wayside_ore_keeps_the_strict_match():
    calls = []
    tool, seen = tool_with({}, here="stone", calls=calls)
    tool.callable({"pos": [7, 58, 9], "target_block_types": ["iron_ore"], "expected_drops": ["raw_iron"], "strict_type": True})
    assert calls[0]["target_block_types"] == ("iron_ore",)  # the Body's own check then refuses the stone
