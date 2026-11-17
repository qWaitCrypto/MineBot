import asyncio
from types import SimpleNamespace

import pytest

from minebot.app import reflex_question as rq
from minebot.app.body_loop import BodyLoop
from minebot.app.direct_call_supervision import CallSupervisor
from minebot.app.life_profile import LifeProfileError, normalize_profile

HOME = [39, 72, 164]


def _facts(home, position=(10.5, 70.0, 150.5), **extra):
    return {"position": list(position), "health": 20, "food": 20, "oxygen": 300, "time": 1000,
            "life_profile": {"home": home, "routine": "At night I stay home."}, **extra}


def _cell(x, y, z, block, state):
    return {"x": x, "y": y, "z": z, "type": f"minecraft:{block}", "state": state}


def _open(x, y, z, floor="oak_planks"):
    return [_cell(x, y, z, "air", "CLEAR"), _cell(x, y + 1, z, "air", "CLEAR"), _cell(x, y - 1, z, floor, "SOLID")]


def test_a_home_cell_with_a_plank_at_head_height_is_not_offered():
    # Villa run 2026-10-16: oak_planks at [39,73,164]; go_home failed 163 times.
    cells = [_cell(39, 72, 164, "air", "CLEAR"), _cell(39, 73, 164, "oak_planks", "SOLID"),
             _cell(39, 71, 164, "oak_planks", "SOLID")]
    arrival = rq.home_arrival([HOME], cells)
    assert arrival == [{"pos": HOME, "standable": False, "blocked": ["oak_planks at head height"]}]
    options = rq.idle_options(_facts({"pos": HOME}, home_arrival=arrival))
    assert "go_home" not in options and "stay" in options
    assert "oak_planks at head height" in rq.home_blocked_sentence(arrival)


def test_going_home_walks_to_the_first_arrival_cell_the_body_fits_in():
    home = {"pos": HOME, "arrival_positions": [HOME, [41, 72, 166]]}
    cells = [_cell(39, 72, 164, "air", "CLEAR"), _cell(39, 73, 164, "oak_planks", "SOLID"),
             _cell(39, 71, 164, "oak_planks", "SOLID"), *_open(41, 72, 166)]
    facts = _facts(home, home_arrival=rq.home_arrival(rq.home_candidates(_facts(home)), cells))
    assert rq.idle_options(facts)["go_home"] == f"Walk to the home position {rq._position([41, 72, 166])}."
    assert CallSupervisor._default_action("go_home", {"facts": facts}) == (
        "move_to", {"pos": [41, 72, 166], "radius": 1, "timeout_s": 60.0})


def test_an_unread_cell_is_unknown_not_blocked():
    arrival = rq.home_arrival([HOME], [_cell(39, 72, 164, "air", "CLEAR")])
    assert arrival == [{"pos": HOME, "standable": None}]
    assert "go_home" in rq.idle_options(_facts({"pos": HOME}, home_arrival=arrival))


def test_standing_at_any_arrival_cell_is_being_home():
    home = {"pos": HOME, "arrival_positions": [[45, 72, 164]]}
    facts = _facts(home, position=(45.5, 72.0, 164.5))
    assert rq.at_home(facts) and "go_home" not in rq.idle_options(facts)


def test_without_a_read_the_old_home_position_is_still_offered():
    assert rq.idle_options(_facts({"pos": HOME}))["go_home"] == f"Walk to the home position {rq._position(HOME)}."


def test_profile_accepts_arrival_positions_and_limits_them():
    profile = normalize_profile({"home": {"pos": HOME, "arrival_positions": [[41, 72, 166]]}})
    assert profile["home"]["arrival_positions"] == [[41, 72, 166]]
    with pytest.raises(LifeProfileError):
        normalize_profile({"home": {"pos": HOME, "arrival_positions": [HOME] * 5}})
    with pytest.raises(LifeProfileError):
        normalize_profile({"home": {"pos": HOME, "arrival_positions": [[1, 2]]}})


class _Lane:
    async def run(self, fn, *args):
        return fn(*args)


def _loop(cells):
    told = []
    reads = []

    def perceive(scope, params):
        reads.append(params["cells"])
        return {"ok": True, "data": {"cells": cells()}}

    runtime = SimpleNamespace(body=SimpleNamespace(perceive=perceive), _read_lane=_Lane(),
                              trace=SimpleNamespace(emit=lambda *a, **k: None),
                              notify_reflex_fact=told.append)
    loop = BodyLoop.__new__(BodyLoop)
    loop.runtime = runtime
    loop._home_blocked_notice = None
    return loop, told, reads


def test_the_agent_is_told_once_while_the_home_stays_blocked_and_again_when_it_changes():
    blocked = [_cell(39, 72, 164, "air", "CLEAR"), _cell(39, 73, 164, "oak_planks", "SOLID"),
               _cell(39, 71, 164, "oak_planks", "SOLID")]
    state = {"cells": blocked}
    loop, told, reads = _loop(lambda: state["cells"])
    facts = _facts({"pos": HOME})

    async def frame():
        arrival = await loop._home_arrival(facts)
        loop._tell_agent_if_home_blocked(arrival)
        return arrival

    asyncio.run(frame())
    asyncio.run(frame())
    assert len(told) == 1 and told[0]["status"] == "home_blocked"
    assert reads[0] == [[39, 72, 164], [39, 73, 164], [39, 71, 164]]
    state["cells"] = _open(39, 72, 164)  # the Agent took the plank out
    assert asyncio.run(frame())[0]["standable"] is True
    state["cells"] = blocked
    asyncio.run(frame())
    assert len(told) == 2


def test_no_read_at_home():
    loop, told, reads = _loop(lambda: [])
    facts = _facts({"pos": HOME}, position=(39.5, 72.0, 164.5))
    assert asyncio.run(loop._home_arrival(facts)) is None and reads == []


def _withholding_loop():
    events = []
    loop = BodyLoop.__new__(BodyLoop)
    loop.runtime = SimpleNamespace(trace=SimpleNamespace(emit=lambda *a, **k: events.append((a, k))))
    loop._last_idle_failure = None
    return loop, events


def _failed_walk(loop, facts, reason="baritone_stopped_without_result"):
    action = BodyLoop._default_action("go_home", {"facts": facts})
    loop._last_idle_failure = ({"choice": "go_home", "action": action, "before": dict(facts), "reason": reason}
                               if reason not in rq._STOPPED_FROM_OUTSIDE else None)


TUNNEL = dict(position=[171.5, 64.0, 165.5], inventory={"cobblestone": 3}, selected_item="stone_pickaxe")


def test_a_walk_home_that_just_failed_is_not_offered_again_while_nothing_changed():
    loop, events = _withholding_loop()
    facts = _facts({"pos": [171, 70, 166]}, **TUNNEL)
    _failed_walk(loop, facts)
    options = loop._without_failed_repeat(facts, rq.idle_options(facts))
    assert "go_home" not in options and "stay" in options
    loop._without_failed_repeat(facts, rq.idle_options(facts))
    assert [a[0] for a, _ in events] == ["idle_option_withheld"]


def test_it_comes_back_when_the_body_moves_or_the_target_changes():
    loop, _ = _withholding_loop()
    facts = _facts({"pos": [171, 70, 166]}, **TUNNEL)
    _failed_walk(loop, facts)
    moved = _facts({"pos": [171, 70, 166]}, **{**TUNNEL, "position": [171.5, 65.0, 165.5]})
    assert "go_home" in loop._without_failed_repeat(moved, rq.idle_options(moved))
    new_entrance = _facts({"pos": [171, 70, 166], "arrival_positions": [[170, 70, 166]]}, **TUNNEL)
    assert "go_home" in loop._without_failed_repeat(new_entrance, rq.idle_options(new_entrance))
    dug = _facts({"pos": [171, 70, 166]}, **{**TUNNEL, "inventory": {"cobblestone": 4}})
    assert "go_home" in loop._without_failed_repeat(dug, rq.idle_options(dug))


def test_a_walk_stopped_from_outside_is_offered_again():
    loop, _ = _withholding_loop()
    facts = _facts({"pos": [171, 70, 166]}, **TUNNEL)
    _failed_walk(loop, facts, reason="preempted")
    assert "go_home" in loop._without_failed_repeat(facts, rq.idle_options(facts))
