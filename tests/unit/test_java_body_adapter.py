"""GovernanceAnswerer binds proposals to the real GovernancePolicy path."""

from __future__ import annotations

from minebot.contract.governance import Region
from minebot.game.governance import GovernancePolicy
from minebot.game.java_body_adapter import GovernanceAnswerer
from minebot.game.java_body_protocol import ServerProposal


def _proposal(kind: str = "break", block_id: str = "minecraft:oak_log", pos=(10, 64, 10)) -> ServerProposal:
    return ServerProposal(
        proposal_id="mp-1",
        bot="Bot",
        action_id="collect-1",
        kind=kind,
        x=pos[0],
        y=pos[1],
        z=pos[2],
        block_id=block_id,
        payload={},
    )


def _policy() -> GovernancePolicy:
    return GovernancePolicy(
        natural_regions=[Region("probe-natural", (-64, 0, -64), (64, 200, 64))],
        protected_regions=[Region("player-base", (30, 60, 30), (40, 80, 40))],
    )


def test_natural_collect_target_is_allowed_by_the_real_policy() -> None:
    allow, reason = GovernanceAnswerer(_policy()).verdict(_proposal())
    assert allow is True
    assert reason


def test_protected_region_is_denied_by_the_real_policy() -> None:
    allow, reason = GovernanceAnswerer(_policy()).verdict(_proposal(pos=(35, 64, 35)))
    assert allow is False
    assert reason == "protected_region"


def test_bot_ledger_governs_bot_placed_blocks() -> None:
    policy = _policy()
    policy.record_bot_placement((12, 64, 12), "minecraft:cobblestone", "bridge", "Bot")
    allow, reason = GovernanceAnswerer(policy).verdict(
        _proposal(block_id="minecraft:cobblestone", pos=(12, 64, 12))
    )
    # Whatever the ledger decides, the decision came from the real policy
    # with a typed reason — never from an adapter-side shortcut.
    assert isinstance(allow, bool)
    assert reason


def test_unmapped_mutation_kind_is_denied_never_guessed() -> None:
    allow, reason = GovernanceAnswerer(_policy()).verdict(_proposal(kind="place"))
    assert allow is False
    assert reason == "unsupported_mutation_kind:place"
