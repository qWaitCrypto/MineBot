from minebot.app.life_profile import life_intent
from minebot.app import reflex_question as rq

def test_idle_profile_adds_only_generic_player_and_home_actions():
    facts={"position":[10,64,10],"nearby_entities":[{"type":"player","id":"Visitor","name":"Visitor","pos":[14,64,10]}],
           "life_profile":{"routine":"Stay near home.","home":{"pos":[0,64,0]},"relationships":[{"player":"Visitor","note":"Say hello."}]}}
    options=rq.idle_options(facts)
    assert list(options)[:3] == ["look_player", "approach_player","follow_player"]
    assert "go_home" in options and "stay" in options
    assert "Stay near home." in life_intent(facts["life_profile"])

def test_idle_profile_has_no_world_write_option_without_profile_targets():
    assert rq.idle_options({"position":[0,64,0]}) == {"stay":"Stay where the Body is and wait for the next step."}


def test_life_frame_reports_native_time_player_distance_and_arrival_band():
    facts = {"time": 12500, "position": [0, 64, 0], "life_intent": "Return at dusk.",
             "life_profile": {"home": {"pos": [1,64,0]}},
             "nearby_entities": [{"type":"minecraft:player", "name":"Visitor", "pos":[2,64,0]}]}
    options = rq.idle_options(facts)
    assert "approach_player" not in options and "go_home" not in options
    request = rq.idle_question(facts, options)
    assert request["state"]["daylight"] == "It is dusk; night falls soon."
    assert request["state"]["players"] == "Player Visitor is 2 blocks away."


def test_thinking_filters_life_ranges_but_keeps_protection_and_current_first():
    facts = {"agent_thinking": True, "position": [10,64,0], "oxygen":100,
             "life_profile":{"home":{"pos":[0,64,0]},"relationships":[{"player":"Absent"}]},
             "nearby_entities":[{"type":"player","name":"Visitor","pos":[20,64,0]}]}
    options = rq.idle_options(facts, current="walk toward Visitor")
    assert list(options)[0] == "keep_going"
    assert {"look_player", "approach_player", "stay", "surface"} <= options.keys()
    assert not {"go_home", "follow_player", "seek_player"} & options.keys()


def test_missing_profile_does_not_offer_unrequested_social_actions():
    assert set(rq.idle_options({"nearby_entities":[{"type":"player", "name":"Visitor"}]})) == {"stay"}


def test_life_running_uses_same_idle_range_without_completion_option():
    from minebot.app.direct_call_supervision import CallSupervisor
    from types import SimpleNamespace
    facts = {"position":[10,64,0], "life_profile":{"home":{"pos":[0,64,0]}}}
    record = {"tool":"move_to", "params":{"pos":[0,64,0]},
              "supervision_context":{"is_life_action":True, "reflex_action_choice":"go_home"}}
    options = CallSupervisor._options_for(SimpleNamespace(name="move_to"), record,
                                         execution={"facts":facts, "state":"running"})
    assert list(options)[0] == "keep_going"
    assert not {"next_step", "go_home"} & options.keys()
    assert "call_agent" in options and "stay" in options


def test_life_write_scope_disables_and_restores_terrain_edits_on_failure():
    from minebot.app.body_loop import BodyLoop
    from minebot.brain.registry import RegisteredTool, ToolSidecar
    from types import SimpleNamespace
    import pytest
    actions = []
    body = SimpleNamespace(get_reflex_facts=lambda:{"allow_break":True, "allow_place":False},
                           execute=lambda action: actions.append(action) or SimpleNamespace(ok=True))
    def failed(params):
        assert actions[-1].params == {"allow_break":False, "allow_place":False}
        raise RuntimeError("native failure")
    tool = RegisteredTool("move_to", "", {}, failed, ToolSidecar("move_to", True))
    loop = BodyLoop.__new__(BodyLoop)
    loop.runtime = SimpleNamespace(body=body)
    with pytest.raises(RuntimeError, match="native failure"):
        loop._life_tool(tool).callable({})
    assert actions[-1].params == {"allow_break":True, "allow_place":False}


def test_life_movement_waits_for_settings_to_be_applied_not_merely_accepted():
    from minebot.app.body_loop import BodyLoop
    from minebot.brain.registry import RegisteredTool, ToolSidecar
    from types import SimpleNamespace
    timeline=[]
    def accepted(action):
        timeline.append("accepted")
        return SimpleNamespace(ok=True,complete=False)
    def terminal(*args,**kwargs):
        timeline.append("applied")
        return SimpleNamespace(data={"success":True})
    body=SimpleNamespace(get_reflex_facts=lambda:{"allow_break":True,"allow_place":True},
                         execute=accepted, await_action_terminal=terminal)
    tool=RegisteredTool("move_to","",{},lambda params:timeline.append("moved"),
                        ToolSidecar("move_to",True,timeout_s=60))
    loop=BodyLoop.__new__(BodyLoop);loop.runtime=SimpleNamespace(body=body)
    loop._life_tool(tool).callable({})
    assert timeline == ["accepted","applied","moved","accepted","applied"]
