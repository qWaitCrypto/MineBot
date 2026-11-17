"""Whose things are whose, in the governance checks (vision-and-home-20261028.md §7.7 item 7)."""
from pathlib import Path

from minebot.app.life_profile import LifeStore
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.app.runtime_state import RuntimeScope
from minebot.body.container import _guard_container_target
from minebot.game.governance import BreakContext, GovernancePolicy, InteractionContext, Region
from tests.unit.test_agent_a6_runtime import REGION, RuntimeBody

NATURAL = Region("work", (-10, 0, -10), (10, 100, 10))
PROFILE = {
    "belongings": [
        {"owner": "qwait", "pos": [50, 64, 50], "dimension": "minecraft:overworld", "said_by": "qwait",
         "said_at": "2026-10-28T20:00:00Z"},
        {"owner": "qwait", "area": {"from": [60, 60, 60], "to": [70, 70, 70]}, "dimension": "minecraft:overworld",
         "said_by": "qwait", "said_at": "day 3"},
        {"owner": "shared", "pos": [80, 64, 80], "dimension": "minecraft:overworld", "said_by": "qwait",
         "said_at": "day 4"},
        {"owner": "Bot1", "pos": [90, 64, 90], "dimension": "minecraft:overworld", "said_by": "qwait"},
        {"owner": "qwait", "pos": [5, 64, 5], "dimension": "minecraft:the_nether", "said_by": "qwait"},
    ],
    "mine": {"containers": [[40, 64, 40]],
             "areas": [{"name": "camp", "from": [30, 60, 30], "to": [35, 70, 35]}]},
}


def policy(profile=PROFILE, dimension="minecraft:overworld"):
    return GovernancePolicy(natural_regions=[NATURAL], bot_name="Bot1", dimension=lambda: dimension,
                            ownership=lambda: profile)


def test_a_players_container_is_not_opened_and_nothing_of_theirs_is_broken():
    rules = policy()
    opened = rules.can_interact((50, 64, 50), "minecraft:chest", InteractionContext.ACTIVATE)
    assert not opened.allowed and opened.reason == "player_owned" and opened.protected
    assert opened.details["said"] == "This is qwait's (qwait said so at 2026-10-28T20:00:00Z)."
    assert opened.details["owner"] == "qwait" and opened.details["said_by"] == "qwait"
    broken = rules.can_break((50, 64, 50), "chest", BreakContext.DIRECT)
    assert not broken.allowed and broken.reason == "player_owned"
    # Inside a player's area: their furnace is not opened, their stone not dug through.
    assert rules.can_interact((65, 64, 65), "furnace", InteractionContext.ACTIVATE).reason == "player_owned"
    assert rules.can_break((65, 64, 65), "stone", BreakContext.COLLECT).reason == "player_owned"
    assert rules.can_interact((65, 64, 65), "barrel", InteractionContext.ACTIVATE).details["said"] == (
        "This is qwait's (qwait said so at day 3).")


def test_what_a_player_owns_but_holds_no_items_keeps_the_usual_rules():
    rules = policy()
    # Their door outside the natural region: unknown provenance, as before.
    assert rules.can_interact((65, 64, 65), "oak_door", InteractionContext.ACTIVATE).reason == "unknown_provenance"


def test_shared_things_are_used_anywhere_but_not_broken():
    rules = policy()
    opened = rules.can_interact((80, 64, 80), "chest", InteractionContext.ACTIVATE)
    assert opened.allowed and opened.reason == "allowed_shared" and opened.natural_region is None
    assert opened.details["said"] == "This is shared (qwait said so at day 4)."
    broken = rules.can_break((80, 64, 80), "chest", BreakContext.DIRECT)
    assert not broken.allowed and broken.reason == "shared_not_broken"


def test_own_things_are_used_outside_the_natural_region_and_broken_by_the_ledger_rules():
    rules = policy()
    for pos, source in (((40, 64, 40), "mine.containers"), ((32, 64, 33), "mine.areas"),
                        ((90, 64, 90), "belongings")):
        opened = rules.can_interact(pos, "chest", InteractionContext.ACTIVATE)
        assert opened.allowed and opened.reason == "allowed_own" and opened.details["source"] == source
        assert not opened.bot_owned
    # Breaking is not widened: a chest outside the natural region that the Bot did not place stays protected.
    assert not rules.can_break((40, 64, 40), "chest", BreakContext.DIRECT).allowed
    rules.record_bot_placement((100, 64, 100), "barrel", "storage", "Bot1")
    placed = rules.can_interact((100, 64, 100), "barrel", InteractionContext.ACTIVATE)
    assert placed.allowed and placed.reason == "allowed_own" and placed.bot_owned


def test_a_players_word_about_one_block_beats_the_ledger_and_the_ledger_beats_an_area():
    rules = policy()
    rules.record_bot_placement((50, 64, 50), "chest", "storage", "Bot1")
    assert rules.can_interact((50, 64, 50), "chest", InteractionContext.ACTIVATE).reason == "player_owned"
    # A scaffold the Bot placed in a player's area is still the Bot's to take down.
    rules.record_bot_placement((61, 64, 61), "dirt", "scaffold", "Bot1")
    assert rules.can_break((61, 64, 61), "dirt", BreakContext.BOT_CLEANUP).reason == "allowed_bot_owned"


def test_ownership_is_per_dimension():
    assert policy().can_interact((5, 64, 5), "chest", InteractionContext.ACTIVATE).reason == "allowed_interaction"
    nether = policy(dimension="minecraft:the_nether")
    assert nether.can_interact((5, 64, 5), "chest", InteractionContext.ACTIVATE).reason == "player_owned"
    # The overworld's claims do not reach the same numbers in the Nether.
    assert nether.can_interact((50, 64, 50), "chest", InteractionContext.ACTIVATE).reason == "unknown_provenance"


def test_without_a_profile_nothing_changes():
    rules = GovernancePolicy(natural_regions=[NATURAL], bot_name="Bot1")
    assert rules.ownership((50, 64, 50)) is None
    assert rules.can_interact((50, 64, 50), "chest", InteractionContext.ACTIVATE).reason == "unknown_provenance"
    failing = GovernancePolicy(natural_regions=[NATURAL], ownership=lambda: 1 / 0)
    assert failing.ownership((50, 64, 50)) is None


def test_the_claims_follow_a_new_profile():
    current = {"profile": {}}
    rules = GovernancePolicy(natural_regions=[NATURAL], ownership=lambda: current["profile"])
    assert rules.ownership((50, 64, 50)) is None
    current["profile"] = PROFILE
    assert rules.ownership((50, 64, 50)).whose == "player"


def test_a_refused_container_says_whose_it_is():
    denied = _guard_container_target(policy(), (50, 64, 50), "chest")
    assert denied.reason == "container_denied"
    assert denied.next_suggestion == ("This is qwait's (qwait said so at 2026-10-28T20:00:00Z). Use a container "
                                      "that is yours or shared.")
    assert denied.metrics["legality"]["reason"] == "player_owned"


def test_the_composed_runtime_reads_ownership_from_the_life_store(tmp_path: Path):
    life = LifeStore(tmp_path, RuntimeScope("server", "world", "Bot1"))
    rules = GovernancePolicy(natural_regions=[REGION], bot_name="Bot1")
    build_phase1_agent_runtime(body=RuntimeBody(), goal_text="", model_provider=None,
                               config=Phase1RuntimeConfig(natural_region=REGION, life_store=life,
                                                          governance_policy=rules))
    assert rules.ownership((500, 64, 500)) is None
    life.update({"belongings": [{"owner": "qwait", "pos": [500, 64, 500], "dimension": "overworld",
                                 "said_by": "qwait"}]}, expected_revision=0, reason="qwait said the chest is theirs")
    assert rules.ownership((500, 64, 500)).whose == "player"


def test_iron_bars_break_as_the_named_target_without_an_area():
    # end run-33: the End crystal cage refused with not_natural_breakable even
    # after the Agent wrote the area as its own; end run-35: refused because
    # no area was written, and the Agent gave the cage up.
    rules = policy()
    direct = rules.can_break((32, 64, 33), "minecraft:iron_bars", BreakContext.DIRECT)
    assert direct.allowed and direct.reason == "allowed_named_target" and direct.details["source"] == "mine.areas"
    assert rules.can_break((32, 64, 33), "iron_bars", BreakContext.COLLECT, explicit_target=True).allowed
    # Not the named target, or a way through: unchanged.
    assert not rules.can_break((32, 64, 33), "iron_bars", BreakContext.COLLECT).allowed
    assert not rules.can_break((32, 64, 33), "iron_bars", BreakContext.COLLECT_APPROACH).allowed
    # Outside any area the Agent wrote: still the named target.
    outside = rules.can_break((5, 64, 5), "iron_bars", BreakContext.DIRECT)
    assert outside.allowed and outside.reason == "allowed_named_target"
    assert not rules.can_break((5, 64, 5), "iron_bars", BreakContext.TRAVEL).allowed
    # A player's area stays theirs.
    assert rules.can_break((65, 64, 65), "iron_bars", BreakContext.DIRECT).reason == "player_owned"
