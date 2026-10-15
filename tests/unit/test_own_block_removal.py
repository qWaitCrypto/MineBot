"""Naming one of the bot's own blocks removes it; searched collection still cannot."""
from types import SimpleNamespace

from minebot.app.phase1_runtime import _mine_collect_tool
from minebot.contract import BreakContext, ToolResult
from minebot.contract.governance import BotPlacement
from minebot.game.governance import GovernancePolicy


def tool_with(placements):
    seen = []
    work = SimpleNamespace(
        governance=SimpleNamespace(bot_placements=placements),
        mine_block_collect=lambda pos, *, context, **kwargs: seen.append((pos, context)) or ToolResult(True, "ok", False),
    )
    return _mine_collect_tool(work), seen


def test_a_named_block_the_bot_placed_is_removed_as_its_own_cleanup():
    door = (171, 70, 169)
    tool, seen = tool_with({door: object()})
    tool.callable({"pos": list(door), "target_block_types": ["oak_door"]})
    tool.callable({"pos": [10, 64, 10]})
    assert seen == [(door, BreakContext.BOT_CLEANUP), ((10, 64, 10), BreakContext.COLLECT)]


def test_governance_lets_the_bot_remove_its_own_block_only_when_named():
    policy = GovernancePolicy(bot_placements=[BotPlacement(pos=(1, 64, 1), block_type="oak_door", purpose="structure", bot="Bot")],
                              bot_name="Bot")
    assert policy.can_break((1, 64, 1), "oak_door", BreakContext.COLLECT).reason == "bot_owned_not_temporary"
    assert policy.can_break((1, 64, 1), "oak_door", BreakContext.BOT_CLEANUP).allowed
    assert policy.can_break((1, 64, 1), "cobblestone", BreakContext.BOT_CLEANUP).reason == "bot_ledger_type_mismatch"
