"""ladder-item-consolidation-20261108 step 2: rules on ladder items."""
from types import SimpleNamespace

import pytest

from minebot.app import reflex_question as rq
from minebot.app.body_loop import BodyLoop, running_item_rules
from minebot.app.life_profile import LifeProfileError, LifeStore
from minebot.app.runtime_state import RuntimeScope

SCOPE = RuntimeScope("127.0.0.1:25569", "world-a", "RulesBot")
FIGHT = {"when": "a zombie is near in the open", "then": "fight it",
         "call": {"tool": "engage_entity", "arguments": {"target": "nearest_hostile"}}}
HOLD = {"when": "sealed in and a mob is outside", "then": "stay in the cell"}


def test_an_item_keeps_its_own_rules_and_an_empty_list_means_none(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    record = store.update_project("upsert", {"id": "night-seal", "intent": "seal in", "rules": [HOLD]},
                                  expected_revision=0, reason="r")
    record = store.update_project("upsert", {"id": "dig", "intent": "dig", "rules": []},
                                  expected_revision=record["revision"], reason="r")
    record = store.update_project("upsert", {"id": "wood", "intent": "wood"},
                                  expected_revision=record["revision"], reason="r")
    projects = {row["id"]: row for row in record["profile"]["projects"]}
    assert projects["night-seal"]["rules"] == [HOLD]
    assert projects["dig"]["rules"] == [] and "rules" not in projects["wood"]
    with pytest.raises(LifeProfileError, match="bad.rules"):
        store.update_project("upsert", {"id": "bad", "intent": "x", "rules": [{"when": "w"}]},
                             expected_revision=record["revision"], reason="r")


def test_only_the_running_items_rules_enter_the_frames():
    profile = {"rules": [FIGHT], "projects": [{"id": "night-seal", "intent": "seal", "rules": [HOLD]},
                                              {"id": "dig", "intent": "dig", "rules": []},
                                              {"id": "wood", "intent": "wood"}]}
    assert running_item_rules(profile, None) is None
    assert running_item_rules(profile, "wood") is None
    assert running_item_rules(profile, "dig") == []
    assert running_item_rules(profile, "night-seal") == [HOLD]
    zombie = {"nearby_entities": [{"type": "minecraft:zombie", "pos": [3.0, 64.0, 0.0], "distance": 3.0,
                                   "hostile": True}], "position": [0.0, 64.0, 0.0]}
    base = {**zombie, "life_profile": profile}
    assert rq.agent_rules(base) == [FIGHT]
    assert rq.agent_rules({**base, "item_rules": [HOLD]}) == [HOLD]
    assert rq.agent_rules({**base, "item_rules": []}) == []
    assert rq.rule_options({**base, "item_rules": []}) == {}
    assert "stay in the cell" in (rq.agent_rules_sentence({**base, "item_rules": [HOLD]}) or "")
    assert rq.agent_rules_sentence({**base, "item_rules": [HOLD]}).count("fight") == 0


def test_the_loop_adds_the_running_items_rules_and_the_manager_tracks_the_run(tmp_path):
    from minebot.app.reflex_tools import ReflexManager
    store = LifeStore(tmp_path, SCOPE)
    record = store.update_project("upsert", {"id": "night-seal", "intent": "seal", "rules": [HOLD]},
                                  expected_revision=0, reason="r")
    manager = ReflexManager(SimpleNamespace(life_store=store, trace=SimpleNamespace(emit=lambda *a, **k: None)))
    loop = BodyLoop(SimpleNamespace(decision_client=SimpleNamespace(), trace=SimpleNamespace(emit=lambda *a, **k: None),
                                    reflex_manager=manager))
    facts = {"life_profile": record["profile"]}
    loop._add_item_rules(facts)
    assert "item_rules" not in facts
    manager._item_stance_begin("night-seal")
    assert manager.item_running() == "night-seal"
    loop._add_item_rules(facts)
    assert facts["item_rules"] == [HOLD]
    manager._item_stance_end("night-seal", "completed")
    assert manager.item_running() is None
