"""After a protective action: danger stopping the same work again at the same
place hands the choice to go back to the Agent (step 3, user 2026-10-14)."""
import asyncio
from types import SimpleNamespace

from minebot.app.body_loop import BodyLoop


def interruption(pos):
    return {"result": {"metrics": {"direct_call_handoff": {"facts": {"reason": "decision_default_tool"}}}},
            "body": {"position": pos, "inventory": {"leaf_litter": 1}}}


class Answer:
    def __init__(self, choice):
        self.choice, self.asked = choice, []

    async def ask(self, payload, **kwargs):
        self.asked.append(payload)
        raw = None if self.choice is None else {"answers": {"r": {"choice": self.choice}}}
        return SimpleNamespace(raw=raw)


def loop_with(answer):
    events = []
    runtime = SimpleNamespace(decision_client=answer, trace=SimpleNamespace(emit=lambda event, **data: events.append((event, data))))
    loop = BodyLoop(runtime)
    loop._client = answer
    return loop, events


def record(history, **context):
    return {"call_id": "work", "tool": "collect_resource", "params": {"item": "logs", "count": 8},
            "history": history, "supervision_context": {"intent": "at least 8 oak_log in the inventory", **context}}


def test_first_interruption_is_not_asked_and_keeps_the_ordinary_question():
    answer = Answer("same")
    loop, _ = loop_with(answer)
    assert not asyncio.run(loop._stopped_again_at_the_same_place(record([interruption([1, 64, 1])])))
    assert answer.asked == []


def test_two_stops_compare_only_the_two_recorded_places():
    answer = Answer("same")
    loop, events = loop_with(answer)
    failed_attempt = {"result": {"success": False, "reason": "no_path"}, "body": {"position": [90, 64, 90]}}
    history = [interruption([151.5, 75.6, 164.1]), failed_attempt, interruption([151.5, 74.7, 164.1])]
    assert asyncio.run(loop._stopped_again_at_the_same_place(record(history)))
    state = answer.asked[0]["state"]
    assert "[151.5, 75.6, 164.1]" in state["last_return"] and "[151.5, 74.7, 164.1]" in state["this_interruption"]
    assert list(answer.asked[0]["questions"]) == ["r"]
    assert [e for e, d in events if d.get("purpose") == "return_place_frame"] == [
        "call_supervision_request", "call_supervision_result"]


def test_different_place_or_no_valid_answer_keeps_the_ordinary_question():
    for choice in ("different", "maybe", None):
        loop, _ = loop_with(Answer(choice))
        history = [interruption([1, 64, 1]), interruption([1, 64, 2])]
        assert not asyncio.run(loop._stopped_again_at_the_same_place(record(history)))


def test_same_place_hands_the_choice_to_the_agent_before_any_result_question():
    loop, _ = loop_with(Answer("same"))
    history = [interruption([1, 64, 1]), interruption([1, 64, 1])]
    # No registry: the ordinary result question could not even be compiled.
    loop.runtime.registry = None
    assert asyncio.run(loop._follow_default_result(
        record(history), ("go_to_surface", {}), {"success": True}, "id")) == "call_agent"
    idle = record(history, idle_protection=True)
    answer = Answer("same")
    loop, _ = loop_with(answer)
    loop.runtime.registry = None
    asyncio.run(loop._follow_default_result(idle, ("go_to_surface", {}), {"success": True}, "id"))
    assert answer.asked == []
