from minebot.app.life_profile import life_intent
from minebot.app import reflex_question as rq

def test_idle_profile_adds_only_generic_player_and_home_actions():
    facts={"position":[10,64,10],"nearby_entities":[{"type":"player","id":"Visitor","name":"Visitor","pos":[14,64,10]}],
           "life_profile":{"routine":"Stay near home.","home":{"pos":[0,64,0]},"relationships":[{"player":"Visitor","note":"Say hello."}]}}
    options=rq.idle_options(facts)
    assert list(options)[:2] == ["approach_player","follow_player"]
    assert "go_home" in options and "stay" in options
    assert "Stay near home." in life_intent(facts["life_profile"])

def test_idle_profile_has_no_world_write_option_without_profile_targets():
    assert rq.idle_options({"position":[0,64,0]}) == {"stay":"Stay where the Body is and wait for the next step."}
