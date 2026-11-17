"""The Agent's stance in frames, staying put under it, and the stance check
(closed-loop-architecture-20261107.md §3 A/B)."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from minebot.app import reflex_question as rq
from minebot.app.body_loop import BodyLoop
from minebot.app.life_profile import LifeProfileError, LifeStore, life_summary, stance_written_at
from minebot.app.runtime_state import RuntimeScope

SCOPE = RuntimeScope("127.0.0.1:25569", "world-a", "StanceBot")
POCKET = ("Sealed in a pocket at [51, 114, -86]: dirt on every side and above the Body. Holding here until "
          "daylight; the mobs outside cannot get in, and going out at night is what would be dangerous. This "
          "ends if a block of the pocket is broken, a mob is inside with the Body, the Body is hurt, or it is day.")
CREEPER = [{"id": "c1", "name": "Creeper", "type": "creeper", "hostile": True, "dist": 19.0,
            "pos": [60.0, 114.0, -86.0]}]


def body(**extra):
    return {"health": 20, "food": 18, "oxygen": 300, "position": [51.4, 114.0, -85.5],
            "nearby_entities": CREEPER, "inventory": {"stone_sword": 1, "dirt": 12}, **extra}


def stance(age_s=45.0, text=POCKET):
    return {"agent_stance": {"text": text, "at": "2026-11-07T13:17:09Z", "age_s": age_s}}


def bench_line(seconds, text=POCKET):
    # The wording replayed on the run-3 night (bench.py on desktop2).
    age = f"{seconds} seconds" if seconds < 120 else f"{seconds // 60} minutes"
    return f"The Agent's reading of the situation, written {age} ago: “{text}”"


def test_stance_is_kept_with_when_it_was_written_and_cleared_with_null(tmp_path):
    store = LifeStore(tmp_path, SCOPE)
    record = store.update({"stance": POCKET}, expected_revision=0, reason="sealed in for the night")
    written = record["profile"]["stance"]
    assert written["text"] == POCKET and stance_written_at(record["profile"])[0] == POCKET
    # Another field changes; the stance keeps its time.
    record = store.update({"routine": "Work under cover at night."}, expected_revision=record["revision"],
                          reason="r")
    assert record["profile"]["stance"] == written
    assert life_summary(record)["stance"] == written
    record = store.update({"stance": None}, expected_revision=record["revision"], reason="day")
    assert "stance" not in record["profile"]
    with pytest.raises(LifeProfileError, match="stance is 501 characters; the limit is 500"):
        store.update({"stance": "x" * 501}, expected_revision=record["revision"], reason="r")


def test_stance_line_is_the_replayed_wording_and_comes_first():
    for seconds in (45, 119, 120, 300):
        assert rq.stance_sentence(stance(seconds)) == bench_line(seconds)
    assert rq.stance_sentence(stance(1.4)).startswith("The Agent's reading of the situation, written 1 second ago")
    work = rq.frame_state(goal="dig", intent="", tool="dig_down", params={}, facts={**body(), **stance()},
                          running=True)
    assert next(iter(work)) == "agent_stance" and work["agent_stance"] == bench_line(45)
    idle = rq.idle_question({**body(), **stance()}, rq.idle_options({**body(), **stance()}))
    assert next(iter(idle["state"])) == "agent_stance"
    # Without a stance the frames are as before.
    assert "agent_stance" not in rq.frame_state(goal="dig", intent="", tool="dig_down", params={},
                                                facts=body(), running=True)
    assert "agent_stance" not in rq.idle_question(body(), rq.idle_options(body()))["state"]


def test_staying_put_is_offered_last_in_danger_only_under_a_stance():
    facts = {**body(), **stance()}
    questions = rq.idle_question(facts, rq.idle_options(facts))["questions"]
    assert list(questions["p"]["criteria"])[-1] == "stay_put"
    assert questions["p"]["criteria"]["stay_put"] == rq.STAY_PUT
    assert "stay_put" not in questions["q"]["criteria"]
    plain = rq.idle_question(body(), rq.idle_options(body()))["questions"]
    assert "stay_put" not in plain["p"]["criteria"]
    hurting = {**facts, "hurt_here": {"times": 2, "seconds": 3}}
    assert rq.hold_option(hurting) == rq.STAY_PUT + " Doing it, the Body stays where it is losing health."


def test_staying_put_in_danger_does_the_work_choice_of_the_same_answer():
    facts = {**body(), **stance()}
    questions = rq.idle_question(facts, rq.idle_options(facts))["questions"]
    raw = {"answers": {"d": {"choice": "in_danger"}, "p": {"choice": "stay_put"}, "q": {"choice": "stay"}}}
    assert rq.frame_choice(raw, questions) == "stay"
    raw["answers"]["q"] = {"choice": "not_offered"}
    assert rq.frame_choice(raw, questions) == "unknown"
    raw["answers"]["p"] = {"choice": "move_away"}
    assert rq.frame_choice(raw, questions) == "move_away"


def test_the_frame_says_the_agent_holds_the_body_when_it_does():
    assert rq.agent_state_sentence({"ladder_held": True, "agent_thinking": True}) == rq.HELD_WORDS
    assert rq.agent_state_sentence({}) == "The Agent is not directing the Body right now; the Body acts on its own."


def test_stance_check_question_says_the_stance_the_body_now_and_how_far_it_has_moved():
    facts = {**body(position=[53.0, 116.0, -86.0]), **stance(60.0)}
    request = rq.stance_check_question(facts, chosen_at=[51.4, 114.0, -85.5], chosen_s_ago=8.2)
    state = request["state"]
    assert list(state) == ["agent_stance", "body", "threats", "position", "movement_since_chosen"]
    assert state["movement_since_chosen"] == ("Since the current action was chosen 8 seconds ago, the Body has "
                                              "moved 3 blocks: 2 blocks up and 2 blocks across.")
    assert state["position"] == "now at [53.0, 116.0, -86.0]."
    # The replayed wording (bench5, run-3 12:15:00).
    assert rq.moved_since_sentence([42.6, 77.4, 4.5], [41.4, 88.2, 4.5], 9) == (
        "Since the current action was chosen 9 seconds ago, the Body has moved 11 blocks: 11 blocks up and "
        "1 block across.")
    assert rq.moved_since_sentence([1, 2, 3], [1.2, 2, 3.3], 4) == (
        "Since the current action was chosen 4 seconds ago, the Body has not moved.")
    assert request["questions"] == {"k": rq.STANCE_CHECK}
    assert rq.stance_check_choice({"answers": {"k": {"choice": "broken"}}}) == "broken"
    assert rq.stance_check_choice({"answers": {"k": {"choice": "maybe"}}}) is None
    assert rq.stance_check_question(body()) is None


def test_falling_air_asks_a_running_protection_again_once():
    due = BodyLoop._protection_frame_due
    record = {"supervision_context": {"is_reflex_action": True, "reflex_action_choice": "rule_2"}}
    assert not due(record, {"facts": {"health": 9.5, "oxygen": 300}})   # chosen with full air
    assert due(record, {"facts": {"health": 9.5, "oxygen": 270}})       # it starts to fall
    assert not due(record, {"facts": {"health": 9.5, "oxygen": 200}})   # said once
    under = {"supervision_context": {"is_reflex_action": True, "reflex_action_choice": "surface"}}
    assert not due(under, {"facts": {"health": 20, "oxygen": 150}})     # chosen under water
    assert not due(under, {"facts": {"health": 20, "oxygen": 120}})



def test_a_fall_asks_a_running_protection_again_once_per_fall():
    # end run-33 19:47: thrown off a tower during engage, nothing asked until 2.7 blocks were left.
    due = BodyLoop._protection_frame_due
    record = {"supervision_context": {"is_reflex_action": True, "reflex_action_choice": "engage"}}
    fall = {"fallen": 2.1, "ground_below": 27.0, "lands_on": "end_stone"}
    assert not due(record, {"facts": {"health": 18, "on_ground": True}})
    assert due(record, {"facts": {"health": 18, "on_ground": False, "fall": fall}})
    assert not due(record, {"facts": {"health": 18, "on_ground": False, "fall": {**fall, "fallen": 9.0}}})
    assert not due(record, {"facts": {"health": 18, "on_ground": True}})        # landed
    assert due(record, {"facts": {"health": 18, "on_ground": False, "fall": fall}})  # the next fall

def _loop(profile):
    loop = BodyLoop(SimpleNamespace(decision_client=SimpleNamespace(), trace=SimpleNamespace(emit=lambda *a, **k: None)))
    loop.current_life_profile = lambda: profile
    return loop


def test_a_death_or_a_broken_reading_ends_the_stance_in_frames():
    at = datetime.now(timezone.utc) - timedelta(seconds=30)
    profile = {"stance": {"text": POCKET, "at": at.strftime("%Y-%m-%dT%H:%M:%SZ")}}
    loop = _loop(profile)
    fact = loop._stance_fact(profile)
    assert fact["text"] == POCKET and 29 <= fact["age_s"] <= 40
    assert loop._stance_after_death() == {}
    loop._died_at = datetime.now(timezone.utc)
    assert loop._stance_fact(profile) is None
    assert "was written before this death" in loop._stance_after_death()["stance"]
    later = {"stance": {"text": "Back in the pocket.", "at": (loop._died_at + timedelta(seconds=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")}}
    assert loop._stance_fact(later)["text"] == "Back in the pocket."
    loop._stance_left = stance_written_at(later)
    assert loop._stance_fact(later) is None
    # A new stance in the same second as the broken one is not the broken one.
    rewritten = {"stance": {"text": "Sealed in at the new spot.", "at": later["stance"]["at"]}}
    assert loop._stance_fact(rewritten)["text"] == "Sealed in at the new spot."


def test_a_stance_written_in_the_second_of_the_death_after_it_still_holds():
    loop = _loop({})
    loop._died_at = datetime(2026, 11, 7, 13, 17, 9, 400000, tzinfo=timezone.utc)
    same_second = {"stance": {"text": POCKET, "at": "2026-11-07T13:17:09Z"}}
    assert loop._stance_fact(same_second)["text"] == POCKET
    before = {"stance": {"text": POCKET, "at": "2026-11-07T13:17:08Z"}}
    assert loop._stance_fact(before) is None


class _Client:
    def __init__(self, choice):
        self.choice, self.requests = choice, []

    async def ask(self, request, **_kwargs):
        self.requests.append(request)
        return SimpleNamespace(raw={"answers": {"k": {"choice": self.choice}}})


@pytest.mark.parametrize("choice", ["holds", "broken"])
def test_the_stance_check_wakes_the_agent_only_when_the_reading_breaks(choice):
    told = []
    loop = _loop({})
    loop.runtime.notify_reflex_fact = told.append
    loop._client = _Client(choice)
    record = {"call_id": "reflex-idle-7", "tool": "engage_entity", "cancel_requested": False,
              "supervision_context": {"is_reflex_action": True, "reflex_action_choice": "engage",
                                      "reflex_action_text": "Fight back against the Creeper (19 blocks away).",
                                      "reflex_action_at": [51.4, 114.0, -85.5]}}
    wake = asyncio.Event()

    async def run():
        worker = asyncio.create_task(asyncio.sleep(5))
        loop._check_stance_soon(record, {"facts": {**body(), **stance()}}, wake, worker)
        await record["stance_task"]
        # At most once a second.
        loop._check_stance_soon(record, {"facts": {**body(), **stance()}}, wake, worker)
        assert record["stance_task"].done()
        worker.cancel()

    asyncio.run(run())
    assert len(loop._client.requests) == 1
    assert loop._client.requests[0]["state"]["agent_stance"] == bench_line(45)
    if choice == "holds":
        assert not told and not wake.is_set() and "stance_due" not in record
        return
    assert record["stance_broken"] and record["stance_due"] and wake.is_set()
    assert told[0]["kind"] == "stance" and told[0]["status"] == "stance_left" and told[0]["stance"] == POCKET
    assert loop._stance_left == (POCKET, datetime(2026, 11, 7, 13, 17, 9, tzinfo=timezone.utc))


def test_the_stance_check_takes_the_position_from_the_latest_facts_when_the_frame_lacks_it():
    """easy run-5, 17:44:51 and 17:44:56: running frames without a position."""
    loop = _loop({})
    loop._client = _Client("holds")
    seen = body()
    loop._latest_facts = seen
    without = {key: value for key, value in seen.items() if key not in ("position", "pos")}
    record = {"call_id": "reflex-idle-8", "tool": "move_away", "cancel_requested": False,
              "supervision_context": {"is_reflex_action": True, "reflex_action_choice": "move_away",
                                      "reflex_action_text": "Move away from the Zombie.",
                                      "reflex_action_at": [51.4, 114.0, -85.5]}}

    async def run():
        worker = asyncio.create_task(asyncio.sleep(5))
        loop._check_stance_soon(record, {"facts": {**without, **stance()}}, asyncio.Event(), worker)
        await record["stance_task"]
        worker.cancel()

    asyncio.run(run())
    assert "position" in loop._client.requests[0]["state"]


def test_the_stance_follows_what_the_body_has_done(tmp_path):
    """ladder-item-consolidation-20261108 §3: an item's stance is set by the item's
    first successful Body result and stays after the run; an item without one
    leaves the stance as it is; the Agent's later update_life stance replaces
    it; a death, a broken reading or the Agent clearing it ends whichever is current."""
    from minebot.app.reflex_tools import ReflexActivity, ReflexManager
    store = LifeStore(tmp_path, SCOPE)
    ahead = "Sealed in the night cell at [10, 64, 10]; mobs cannot reach the Body. This ends at day."
    record = store.update_project("upsert", {"id": "dusk-shelter", "intent": "seal in for the night",
                                             "stance": ahead}, expected_revision=0, reason="r")
    record = store.update_project("upsert", {"id": "collect-wood", "intent": "collect wood"},
                                  expected_revision=record["revision"], reason="r")
    assert record["profile"]["projects"][0]["stance"] == {"text": ahead}
    assert "stance" not in record["profile"]           # the item's stance never becomes the profile's
    events = []
    manager = ReflexManager(SimpleNamespace(life_store=store, trace=SimpleNamespace(
        emit=lambda name, **kw: events.append((name, kw.get("status") or kw.get("reason"))))))
    activity = ReflexActivity("project-dusk-shelter-1", {"goal": "", "work": {}}, holder="life",
                              project_id="dusk-shelter", status="running")
    manager._activities[activity.activity_id] = activity
    loop = _loop({})
    loop.runtime.reflex_manager = manager
    # Pending: the Agent's earlier update_life stance is still what the hand reads.
    earlier = {"stance": {"text": "At home, idle.", "at": (datetime.now(timezone.utc) - timedelta(seconds=60)
                                                          ).strftime("%Y-%m-%dT%H:%M:%SZ")}}
    manager._item_stance_begin("dusk-shelter")
    assert manager.stance_set() is None and loop._stance_fact(earlier)["text"] == "At home, idle."
    manager._item_stance_result({"success": False, "reason": "blocked"})
    assert manager.stance_set() is None
    # The first success sets the item's stance; it outlives the run.
    manager._item_stance_result({"success": True, "reason": "placed"})
    fact = loop._stance_fact(earlier)
    assert fact["text"] == ahead and 0 <= fact["age_s"] < 5 and stance_written_at({"stance": fact})[0] == ahead
    assert manager._item_stance_end("dusk-shelter", "completed") is None
    activity.status = "completed"
    assert loop._stance_fact(earlier)["text"] == ahead and loop._stance_fact({})["text"] == ahead
    # The Agent writes a stance later: that one is read; an older write is not.
    later = {"stance": {"text": "Back out at the farm.", "at": (datetime.now(timezone.utc) + timedelta(seconds=2)
                                                               ).strftime("%Y-%m-%dT%H:%M:%SZ")}}
    assert loop._stance_fact(later)["text"] == "Back out at the farm."
    assert loop._stance_fact(earlier)["text"] == ahead
    # An item without a stance leaves the stance as it is.
    activity.project_id, activity.status = "collect-wood", "running"
    manager._item_stance_begin("collect-wood")
    manager._item_stance_result({"success": True})
    assert loop._stance_fact(earlier)["text"] == ahead and loop._stance_fact(later)["text"] == "Back out at the farm."
    # Handed back before any success: the Agent is told its stance was never set; the set one stays.
    manager._item_stance_begin("dusk-shelter")
    assert manager._item_stance_end("dusk-shelter", "needs_agent") == ReflexManager.STANCE_NOT_SET
    assert loop._stance_fact({})["text"] == ahead
    # The Agent clearing its stance with update_life ends the item's stance too.
    from minebot.app.life_profile import stance_cleared_at
    record = store.update({"stance": "In the cell."}, expected_revision=record["revision"], reason="r")
    assert "stance_cleared_at" not in record["profile"]
    record = store.update({"stance": None}, expected_revision=record["revision"], reason="r")
    assert "stance" not in record["profile"] and stance_cleared_at(record["profile"]) is not None
    soon = (datetime.now(timezone.utc) + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert loop._stance_fact({"stance_cleared_at": soon}) is None
    record = store.update({"stance": "Out again."}, expected_revision=record["revision"], reason="r")
    assert "stance_cleared_at" not in record["profile"]
    # A protective move does not end it: the stance's own words and the stance check do.
    manager._item_stance_begin("dusk-shelter")
    manager._item_stance_result({"success": True})
    assert loop._stance_fact({})["text"] == ahead
    assert not hasattr(loop, "_stance_moved_at")
    # A broken reading ends the item's stance by identity, as it does the profile's.
    fact = loop._stance_fact({})
    loop._stance_left = stance_written_at({"stance": fact})
    assert loop._stance_fact({}) is None
    loop._stance_left = None
    assert loop._stance_fact({})["text"] == ahead
    # A death ends it too.
    loop._died_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert loop._stance_fact({}) is None
    assert [status for name, status in events if name == "ladder_item_stance"] == [
        "pending", "in_force", "pending", "not_set", "pending", "in_force"]


def test_a_running_protective_action_offers_staying_only_under_a_stance():
    """easy run-5, 17:44:51: a flight under a stance that still held went on 22 s."""
    from minebot.app.direct_call_supervision import CallSupervisor
    record = {"tool": "move_away", "params": {},
              "supervision_context": {"is_reflex_action": True, "reflex_action_choice": "move_away"}}
    under = CallSupervisor._options_for(SimpleNamespace(name="move_away"), record,
                                        execution={"state": "running", "facts": {**body(), **stance()}})
    assert under["stay_put"] == rq.STAY_PUT
    assert list(under)[-2:] == ["stay_put", "call_agent"]
    bare = CallSupervisor._options_for(SimpleNamespace(name="move_away"), record,
                                       execution={"state": "running", "facts": body()})
    assert "stay_put" not in bare
    question = rq.frame_questions(under, protecting=True, running_protection=True)
    assert set(question) == {"q"} and "stay_put" in question["q"]["criteria"]
    assert rq.frame_choice({"answers": {"q": {"choice": "stay_put"}}}, question) == "stay_put"
    falling = {**body(), **stance(), "fall": {"fallen": 3.0, "ground_below": 9, "lands_on": "end_stone"}}
    assert rq.hold_option(falling) == ""
    assert "stay_put" not in CallSupervisor._options_for(SimpleNamespace(name="move_away"), record,
                                                         execution={"state": "running", "facts": falling})
