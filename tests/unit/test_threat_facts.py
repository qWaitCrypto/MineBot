"""Focused wording and lifetime proofs for threat and death facts F1--F3."""

from minebot.app import reflex_question as rq
from minebot.app.body_events import agent_event_data
from minebot.app.direct_call_supervision import CallSupervisor
from minebot.contract import Event


def creeper(hostile=True, **extra):
    return {
        "id": "creeper-1", "name": "Creeper", "type": "creeper", "hostile": hostile,
        "dist": 6.0, "pos": [6.0, 64.0, 0.0], **extra,
    }


def test_f1_creeper_memory_keeps_last_seen_fact_for_five_seconds_then_expires():
    memory = {}
    first = rq.update_creeper_memory({"entities": [creeper(swelling=True)]}, memory, now=100.0)
    assert first == [{
        "id": "creeper-1", "name": "Creeper", "last_seen_position": [6.0, 64.0, 0.0],
        "last_seen_distance": 6.0, "seconds_since_seen": 0.0, "in_view": True, "swelling": True,
    }]

    gone = rq.update_creeper_memory({"entities": []}, memory, now=103.0)
    assert gone[0]["seconds_since_seen"] == 3.0
    sentence = rq._threat_sentence({"entities": [], "creeper_memory": gone})
    assert "A creeper was 6 blocks away 3 seconds ago and is not in view now; it was swelling." in sentence

    assert rq.update_creeper_memory({"entities": []}, memory, now=105.01) == []


def test_f1_visible_creeper_sentence_preserves_observed_swelling():
    memory = {}
    facts = {"entities": [creeper(swelling=True)]}
    entries = rq.update_creeper_memory(facts, memory, now=10.0)
    sentence = rq._threat_sentence({**facts, "creeper_memory": entries})
    assert sentence == "Nearest hostile mob: Creeper, 6 blocks away. The creeper 6 blocks away is swelling."


def test_f1_visible_calm_creeper_adds_nothing():
    facts = {"entities": [creeper(swelling=False)]}
    entries = rq.update_creeper_memory(facts, {}, now=10.0)
    assert rq._threat_sentence({**facts, "creeper_memory": entries}) == "Nearest hostile mob: Creeper, 6 blocks away."


def test_f2_death_projection_adds_observed_cause_for_agent_wake_text():
    before = agent_event_data(Event(seq=1, tick=1, bot="Bot", name="death", data={"health": 0}))
    assert "cause_of_death" not in before
    after = agent_event_data(Event(seq=2, tick=2, bot="Bot", name="death", data={
        "health": 0,
        "hurt_source": {"id": "z-1", "name": "Zombie", "type": "zombie", "damage_type": "mob_attack"},
    }))
    assert after["cause_of_death"] == (
        "The Body died after being hit by Zombie (z-1); damage type was mob_attack."
    )
    assert after["hurt_source"]["id"] == "z-1"


def test_f3_hurt_source_names_the_attacker_in_facts_and_engage_option():
    facts = {
        "health": 10, "health_before": 20, "vital_age_s": 1,
        "selected_item": "stone_sword", "inventory": {"stone_sword": 1},
        "entities": [
            {"id": "nearby", "name": "Spider", "type": "spider", "hostile": True, "dist": 2.0},
            {"id": "attacker", "name": "Zombie", "type": "zombie", "hostile": True, "dist": 4.0},
        ],
        "hurt_source": {
            "id": "attacker", "name": "Zombie", "type": "zombie",
            "distance": 4.0, "seconds_since_hurt": 1.0,
        },
    }
    assert "The Zombie at 4 blocks hit the Body 1 second ago." in rq._body_sentence(facts)
    engage = rq.protective_options(facts)["engage"]
    assert "Fight back against the Zombie" in engage
    assert "The Zombie at 4 blocks hit the Body 1 second ago." in engage
    assert CallSupervisor._default_action("engage", {"facts": facts})[1]["target"] == "attacker"



def test_f3_an_old_hit_is_not_said_once_health_is_steady():
    facts = {
        "health": 20, "health_before": 20, "vital_age_s": 1,
        "entities": [{"id": "attacker", "name": "Zombie", "type": "zombie", "hostile": True, "dist": 4.0}],
        "hurt_source": {"id": "attacker", "name": "Zombie", "distance": 4.0, "seconds_since_hurt": 600.0},
    }
    assert "hit the Body" not in rq._body_sentence(facts)
    assert "hit the Body" not in rq.protective_options(facts).get("engage", "")
