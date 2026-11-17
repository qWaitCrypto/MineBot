"""Plan ladder S4: notices instead of wakes, low water, the LADDER section (plan-ladder-20261028.md §7.4-§7.5)."""
import asyncio
import time

from minebot.app.ladder_view import LADDER_SECTION_CHARS, LadderNotices, ladder_section, notices_message
from minebot.app.session import AgentSession, SessionCommand
from tests.unit.test_agent_session import build_parts

WALK = {"kind": "call", "tool": "fake", "arguments": {}, "intent": "x"}


def _state(states):
    def state_of(item):
        return states.get(item["id"], {"state": "waiting"})
    return state_of


def test_the_ladder_section_shows_states_in_the_agents_order():
    now = time.time()
    record = {"profile": {"projects": [
        {"id": "mine-iron", "intent": "Collect 24 raw_iron.", "status": "active", "work": WALK,
         "when": "it is daytime and a stone_pickaxe is held"},
        {"id": "smelt", "intent": "Smelt iron.", "status": "active", "work": WALK},
        {"id": "hunt", "intent": "Hunt cows.", "status": "active", "work": WALK},
        {"id": "portal", "intent": "Build a nether portal.", "status": "active", "when": "it is day"},
        {"id": "stronghold", "intent": "Find the stronghold.", "status": "active"},
        {"id": "fence", "intent": "Fence.", "status": "paused", "work": WALK},
    ], "rules": [{"when": "a", "then": "b"}]}}
    states = {"mine-iron": {"state": "running", "since": now - 134},
              "hunt": {"state": "set_aside", "since": now - 300,
                       "last_result": {"outcome": "blocked", "reason": "target_not_found"}}}
    notices = [{"at": now - 5, "item": "farm", "result": "completed"}]
    text = ladder_section(record, _state(states), timeline={"busy_since": now - 840, "runs": 3},
                          notices=notices, now=now)
    lines = text.splitlines()
    assert lines[0] == "LADDER (your plan, in order; states are the Body's own record):"
    assert lines[1] == ("- running 2m14s: mine-iron [walk] Collect 24 raw_iron. "
                        "Fits when it is daytime and a stone_pickaxe is held.")
    assert lines[2] == "- waiting: smelt [walk] Smelt iron."
    assert lines[3] == ("- set aside 5m00s ago: hunt [walk] Hunt cows. "
                        "Last: blocked (target_not_found); not changed since.")
    assert lines[4] == "- portal [look] Build a nether portal. Fits when it is day."
    assert lines[5] == "- stronghold [think] Find the stronghold."
    assert lines[6] == "- paused by you: fence [walk] Fence."
    assert lines[7] == "- rules: 1 standing rules (see read_life)."
    assert lines[8].startswith("BODY TIMELINE: busy since ") and lines[8].endswith("(14m00s, 3 runs); idle 0s.")
    assert lines[9:] == ["NOTICES since your last turn: 1 (newest first):", "- 5s ago: farm completed"]


def test_the_ladder_section_keeps_its_limit_by_dropping_the_oldest_notices():
    now = time.time()
    record = {"profile": {"projects": [{"id": f"item-{n}", "intent": "x" * 250, "work": WALK} for n in range(12)]}}
    notices = [{"at": now - n, "item": f"n{n}", "result": "completed", "reason": "r" * 250} for n in range(20)]
    text = ladder_section(record, _state({}), notices=notices, now=now)
    assert len(text) <= LADDER_SECTION_CHARS
    assert "- 0s ago: n0 completed" in text and "earlier notices not shown." in text
    huge = {"profile": {"projects": [{"id": f"item-{n}", "intent": "x" * 900, "work": WALK,
                                      "when": "y" * 300} for n in range(32)]}}
    text = ladder_section(huge, _state({}), notices=notices, now=now)
    # Every item keeps a line, shorter (S7: the ladder was cut after its first items).
    assert len(text) <= LADDER_SECTION_CHARS
    assert all(f"- waiting: item-{n} [walk]" in text for n in range(32))


def test_finished_items_are_named_once_off_the_ladder():
    # S7: camp-kit had completed hours before the Agent said it would still
    # set the respawn point (s7-later-20261029.md §8 D4-D5).
    now = time.time()
    reported = {"outcome": "completed", "reported_steps": [
        {"node": "work.steps[2]", "tool": "craft_item", "result": "missing_ingredients"}],
        "inventory_change": {"feather": 2, "flint": -1}}
    record = {"profile": {"projects": [
        {"id": "camp-kit", "intent": "Sleep in the bed.", "status": "active", "work": WALK},
        {"id": "arrows", "intent": "Make arrows.", "status": "active", "work": WALK},
        {"id": "farm", "intent": "Harvest.", "status": "active", "work": WALK, "standing": True},
        {"id": "stronghold", "intent": "Find the stronghold.", "status": "done"},
        {"id": "hunt", "intent": "Hunt cows.", "status": "active", "work": WALK},
    ]}}
    states = {"camp-kit": {"state": "done", "last_result": {"outcome": "completed"}},
              "arrows": {"state": "done", "last_result": reported},
              "farm": {"state": "waiting", "last_result": {"outcome": "completed", "inventory_change": {}}},
              "hunt": {"state": "set_aside", "since": now - 60, "last_result": {
                  "outcome": "needs_agent", "inventory_change": {},
                  "stopped_at": {"node": "work.steps[1]", "tool": "search_for_entity",
                                 "result": "target_not_found", "why": "decision_call_agent"}}}}
    lines = ladder_section(record, _state(states), now=now).splitlines()
    assert lines[1:] == [
        "- done: arrows [walk] Make arrows. Last: completed with 1 reported step (work.steps[2] craft_item: "
        "missing_ingredients); inventory +2 feather, -1 flint; it stays until you mark it done, change it or "
        "remove it.",
        "- waiting: farm [walk] Harvest. Last: completed; inventory unchanged. (standing)",
        "- set aside 1m00s ago: hunt [walk] Hunt cows. Last: needs_agent at work.steps[1] search_for_entity: "
        "target_not_found; inventory unchanged; not changed since.",
        "- off the ladder now: camp-kit (completed), stronghold (done by you). read_life lists finished items; "
        "writing one's id again (standing, if it should repeat) brings it back.",
    ]


def test_an_item_line_carries_its_account_once_it_has_run_twice():
    # S7: arrow-kit ran 11 times, 9 handed back (s7-later-20261029.md §8 D6).
    now = time.time()
    stop = {"node": "work.steps[3]", "tool": "search_for_entity", "result": "target_not_found",
            "why": "decision_call_agent"}
    record = {"profile": {"projects": [
        {"id": "arrow-kit", "intent": "Make arrows.", "status": "active", "work": WALK},
        {"id": "farm", "intent": "Harvest.", "status": "active", "work": WALK}]}}
    states = {"arrow-kit": {"state": "set_aside", "since": now - 300, "last_result": {
        "outcome": "needs_agent", "inventory_change": {}, "stopped_at": stop}}}
    accounts = {"arrow-kit": {"runs": 11, "seconds": 3480, "outcomes": {"needs_agent": 9, "completed": 2},
                              "rewrites": 6, "same_end_twice": stop},
                "farm": {"runs": 1, "seconds": 40, "outcomes": {"completed": 1}, "rewrites": 0}}
    lines = ladder_section(record, _state(states), now=now, account_of=accounts.get).splitlines()
    assert lines[1] == (
        "- set aside 5m00s ago: arrow-kit [walk] Make arrows. Last: needs_agent at work.steps[3] "
        "search_for_entity: target_not_found; inventory unchanged; not changed since. 11 runs, 58m00s: "
        "9 handed back, 2 completed; the last two ended at the same step for the same reason "
        "(work.steps[3] search_for_entity: target_not_found); rewritten 6 times.")
    assert lines[2] == "- waiting: farm [walk] Harvest."


def test_a_notice_says_what_changed_and_that_the_item_left():
    notices = LadderNotices()
    notices.add({"activity_id": "a1", "item": "camp-kit", "status": "completed", "off_ladder": "completed",
                 "ended": {"outcome": "completed", "position": [80.5, 64.0, 150.5], "inventory_change": {"bed": -1},
                           "inventory_counts": {"stick": 3}, "duration_s": 8.5}})
    taken = notices.take()
    assert taken[0]["off_ladder"] == "completed" and taken[0]["inventory_counts"] == {"stick": 3}
    text = ladder_section({"profile": {}}, _state({}), notices=taken, now=taken[0]["at"] + 3)
    assert text.splitlines()[-1] == "- 3s ago: camp-kit completed; inventory -1 bed; off your ladder now"


def test_protection_notice_names_tool_reason_and_terminal_body_facts():
    notices = LadderNotices()
    notices.add({"activity_id": "p1", "kind": "protection", "tool": "pillar_up",
                 "status": "protection_result", "wake": False, "reason": "decision_call_agent",
                 "position": [1, 67, 2], "health_before": 18, "health_after": 16,
                 "ended": {"outcome": "decision_call_agent", "reason": "decision_call_agent"}})
    notice = notices.take()[0]
    assert notice["item"] == "protection:pillar_up"
    assert notice["result"] == "decision_call_agent"
    assert notice["reason"] == "decision_call_agent"
    assert notice["position"] == [1, 67, 2]
    assert notice["health_before"] == 18 and notice["health_after"] == 16


def test_notices_are_handed_over_once_and_shown_until_the_next_turn():
    notices = LadderNotices()
    notices.add({"activity_id": "a1", "item": "smelt", "status": "completed",
                 "ended": {"outcome": "completed", "position": [1, 2, 3], "duration_s": 12.0}})
    taken = notices.take()
    assert taken[0]["item"] == "smelt" and taken[0]["position"] == [1, 2, 3]
    assert "1 plan ends since your last turn" in notices_message(taken)
    notices.add({"activity_id": "a2", "item": "hunt", "status": "put_down",
                 "ended": {"outcome": "put_down", "reason": "target_not_found"}})
    assert [n["item"] for n in notices.since_last_turn()] == ["hunt", "smelt"]
    assert [n["item"] for n in notices.take()] == ["hunt"]


def test_a_notice_does_not_wake_the_agent_and_arrives_with_its_next_turn():
    calls, bodies = [], []
    session = AgentSession(lambda goal: build_parts(goal, calls, bodies))
    session.submit(SessionCommand.message("hello"))
    asyncio.run(session.step())
    session.parts.runtime.ladder_notices = LadderNotices()
    session.parts.runtime.notify_reflex_fact({"activity_id": "a1", "item": "smelt", "status": "completed",
                                              "wake": False, "ended": {"outcome": "completed"}})
    assert not session.has_pending_work
    session.submit(SessionCommand.message("how is it going?"))
    asyncio.run(session.step())
    assert "NOTICES (1 plan ends" in calls[-1] and "smelt" in calls[-1]
    # A hand-back by Decision still wakes it.
    session.parts.runtime.notify_reflex_fact({"activity_id": "a2", "status": "needs_agent", "wake": True})
    assert session.has_pending_work
    session.close()


def test_low_water_wakes_the_agent_once_and_not_while_it_thinks(tmp_path):
    from minebot.app.body_loop import BodyLoop
    from minebot.app.life_profile import LifeStore
    from minebot.app.runtime_state import RuntimeScope
    from tests.unit.test_body_loop import _runtime

    store = LifeStore(tmp_path, RuntimeScope("s", "w", "LadderBot"))
    store.update_project("upsert", {"id": "a", "intent": "a", "work": WALK}, expected_revision=0, reason="r")
    runtime, trace, _decision = _runtime()
    woken = []
    runtime.notify_reflex_fact = woken.append
    runtime.agent_thinking = True
    loop = BodyLoop(runtime)
    loop.attach_life_store(store)
    frame = lambda: loop._frame({"facts": {"position": [0, 64, 0]}})  # noqa: E731
    frame()
    assert woken == []
    store.record_project_event("a", "started", who="decision")
    frame()
    assert woken == []  # the Agent is thinking already
    runtime.agent_thinking = False
    frame()
    frame()
    assert [(fact["kind"], fact["running"], fact["wake"]) for fact in woken] == [("ladder_low", "a", True)]
    # A new walk item ends this low water; the next one wakes again.
    store.update_project("upsert", {"id": "b", "intent": "b", "work": WALK}, expected_revision=1, reason="r")
    frame()
    store.record_project_event("b", "started", who="decision")
    frame()
    assert len(woken) == 2


def test_an_item_that_would_run_as_it_did_last_time_is_low_water_and_says_so(tmp_path):
    # T7 2026-10-29: standing items completing with nothing changed kept the
    # Body "busy" and the Agent asleep (s7-later-20261029.md §8.14).
    from minebot.app import reflex_question as rq
    from minebot.app.body_loop import BodyLoop
    from minebot.app.life_profile import LifeStore
    from minebot.app.runtime_state import RuntimeScope
    from tests.unit.test_body_loop import _runtime

    store = LifeStore(tmp_path, RuntimeScope("s", "w", "LadderBot"))
    store.update_project("upsert", {"id": "wood", "intent": "logs", "work": WALK, "standing": True},
                         expected_revision=0, reason="r")
    runtime, trace, _decision = _runtime()
    woken = []
    runtime.notify_reflex_fact = woken.append
    loop = BodyLoop(runtime)
    loop.attach_life_store(store)
    here = {"position": [24.5, 48.0, 1.5], "inventory": {"jungle_log": 6, "oak_log": 3}}
    frame = lambda facts: loop._frame({"facts": facts})["facts"]  # noqa: E731
    assert "ladder_same_run" not in frame(here) and woken == []
    store.record_project_event("wood", "started", who="decision", activity_id="r1")
    store.record_project_result("wood", {"outcome": "completed", "activity_id": "r1",
                                         "start_position": [24.3, 48.0, 1.4], "position": [24.3, 48.0, 1.4],
                                         "inventory_counts": {"jungle_log": 6, "oak_log": 3},
                                         "inventory_change": {}})
    facts = frame(here)
    assert facts["ladder_same_run"] == ["wood"] and rq.idle_options(facts).get("project_wood") is None
    frame(here)
    assert [(fact["kind"], fact["same_as_last_run"]) for fact in woken] == [("ladder_low", ["wood"])]
    assert "wood completed last time with nothing changed" in woken[0]["note"]
    assert [event["project"] for event in trace.events if event["event"] == "ladder_item_withheld"] == ["wood"]
    # The Body changes: the item is offered again and the low water ends.
    assert "ladder_same_run" not in frame({**here, "position": [30.0, 64.0, 2.0]})


def test_items_offered_for_a_minute_and_none_started_wake_the_agent_once(tmp_path):
    # S7 2026-10-29: after the yield food-stock-2 was offered in every idle
    # frame and Decision chose stay 18,172 times; nobody heard of it for 6.5
    # hours (s7-later-20261029.md §8 D3).
    from minebot.app.body_loop import BodyLoop
    from minebot.app.life_profile import LifeStore
    from minebot.app.runtime_state import RuntimeScope
    from tests.unit.test_body_loop import _runtime

    store = LifeStore(tmp_path, RuntimeScope("s", "w", "LadderBot"))
    food = {"id": "food", "intent": "Cook 8 beef.", "when": "fewer than 8 cooked food are carried", "work": WALK}
    store.update_project("upsert", food, expected_revision=0, reason="r")
    runtime, _trace, _decision = _runtime()
    woken = []
    runtime.notify_reflex_fact = woken.append
    loop = BodyLoop(runtime)
    loop.attach_life_store(store)
    facts, options = {"position": [0, 64, 0]}, {"project_food": "work on food", "stay": "stay"}
    check = loop._check_none_started
    check(facts, options, "stay", now=0.0)
    check(facts, options, "unknown", now=20.0)  # a timeout is no choice
    check(facts, options, "seek_player", now=59.0)
    assert woken == []
    check({**facts, "agent_thinking": True}, options, "stay", now=61.0)
    assert woken == []  # not while the Agent thinks
    check(facts, options, "stay", now=62.0)
    assert [(fact["kind"], fact["reason"], fact["wake"], fact["offered"], fact["chosen_instead"], fact["idle_s"])
            for fact in woken] == [("ladder_low", "none_started", True, ["food"], {"stay": 3, "seek_player": 1}, 62)]
    check(facts, options, "stay", now=300.0)
    assert len(woken) == 1  # once per stretch

    # The Agent changes its ladder: a new stretch.
    store.update_project("upsert", {**food, "when": "it is day"}, expected_revision=1, reason="r")
    loop.current_life_profile()
    check(facts, options, "stay", now=310.0)
    check(facts, options, "stay", now=369.0)
    assert len(woken) == 1
    check(facts, options, "stay", now=371.0)
    assert len(woken) == 2 and woken[-1]["idle_s"] == 61

    # An item starts, or none is offered: the stretch ends.
    check(facts, options, "project_food", now=380.0)
    check(facts, options, "stay", now=400.0)
    check(facts, {"stay": "stay"}, "stay", now=430.0)
    check(facts, options, "stay", now=440.0)
    check(facts, options, "stay", now=499.0)
    assert len(woken) == 2
    check(facts, options, "stay", now=500.0)
    assert len(woken) == 3 and woken[-1]["idle_s"] == 60

    # Decision calling the Agent itself is that stretch's one wake.
    check(facts, options, "project_food", now=510.0)
    check(facts, options, "call_agent", now=520.0)
    check(facts, options, "stay", now=600.0)
    assert len(woken) == 3


def test_shots_say_who_loosed_the_arrows_and_at_what():
    from minebot.app.ladder_view import shots_section
    assert shots_section([]) is None
    log = ([{"released": 10.0, "target_type": "ender_dragon", "hit": False, "by": "decision"}] * 3
           + [{"released": 11.0, "target_type": "end_crystal", "hit": True, "by": "decision"},
              {"released": 50.0, "target_type": "end_crystal", "hit": True, "by": "you"},
              {"released": 60.0, "target_type": "ender_dragon", "hit": True, "by": "decision"}])
    assert shots_section(log, since=40.0) == "\n".join([
        "SHOTS (arrows the Body loosed this session, kept across deaths): 6.",
        "- by Decision on its own (protective shots; no call, plan or rule of yours asked for them): 5:"
        " ender dragon 4 (1 hit), end crystal 1 (1 hit).",
        "- by your own calls and plans: 1: end crystal 1 (1 hit).",
        "- since your last turn, Decision on its own: 1: ender dragon 1 (1 hit).",
        "- since your last turn, you: 1: end crystal 1 (1 hit)."])
    assert shots_section(log, since=70.0).endswith("- since your last turn: none.")


def test_notices_remember_when_the_previous_turn_started():
    notices = LadderNotices()
    notices.take()
    first = notices.turn_started
    notices.take()
    assert notices.last_turn_started == first and notices.turn_started >= first
