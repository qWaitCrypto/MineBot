"""Pro plan P7: protection chosen while a result is judged really runs."""
import asyncio
import time
from types import SimpleNamespace

from minebot.app.body_loop import BodyLoop

ZOMBIE = {"id": "z1", "name": "Zombie", "hostile": True, "dist": 4.0, "pos": [14.0, 64.0, 10.0]}
AT_TERMINAL = {"position": [10.5, 64.0, 10.5], "start_position": [0.5, 64.0, 0.5],
               "inventory": {"oak_log": 4}, "inventory_delta": {"oak_log": 4},
               "health": 20, "food": 20, "oxygen": 300}


class _Lane:
    async def run(self, fn, *args):
        return fn(*args)


def _loop(body_now, answers, *, timeout_s=None):
    calls = {"stops": [], "protections": [], "told": [], "asked": []}
    snapshot = {"operation_id": "direct", "tool": "move_to", "state": "succeeded", "facts": dict(AT_TERMINAL)}

    def read():
        if body_now is None:
            raise RuntimeError("read failed")
        return dict(body_now)

    async def stop(reason, **kwargs):
        calls["stops"].append((reason, kwargs))
        return {"settled": True}

    runtime = SimpleNamespace(
        trace=SimpleNamespace(emit=lambda *a, **k: None),
        body=SimpleNamespace(get_reflex_facts=read),
        _read_lane=_Lane(),
        last_known_body_state=dict(AT_TERMINAL),
        execution_snapshots=SimpleNamespace(current=lambda: None, last=lambda: snapshot),
        registry=SimpleNamespace(get=lambda name: SimpleNamespace(name=name)),
        direct_call_handoffs={},
        cancel_active_execution_with_facts=stop,
        notify_reflex_fact=calls["told"].append,
    )
    loop = BodyLoop(runtime)

    async def judge(call_id, tool, params, execution, idle_s, *, purpose, options=None, **kwargs):
        calls["asked"].append((purpose, dict(options or {}), dict(execution["facts"])))
        return answers.pop(0)

    async def protect(choice, action, record=None, *, call_id=None):
        calls["protections"].append((choice, action))
        outcome = {"success": True, "reason": "moved_away"}
        record["protective_result"], record["protective_action"] = outcome, (choice, action)
        return choice, action, outcome, "reflex-default-1"

    loop._judge_terminal_frame = judge
    loop._execute_protection = protect
    record = {"call_id": "direct", "tool": "move_to", "params": {"pos": [10, 64, 10]}, "questions": 0,
              "unknown_answers": 0, "started_at": time.monotonic(),
              "timeout_s": timeout_s, "supervision_context": {"intent": "Reach the point"}}
    if timeout_s is None:
        record.pop("timeout_s")
    return loop, record, calls


RESULT = {"success": True, "reason": "arrived"}


def test_a_hostile_seen_only_by_the_fresh_read_is_in_the_result_question():
    loop, record, calls = _loop({**AT_TERMINAL, "nearby_entities": [ZOMBIE]}, ["next_step"])
    assert asyncio.run(loop._decide_result(record, SimpleNamespace(name="move_to"), record["params"], RESULT)) == "step_done"
    purpose, options, facts = calls["asked"][0]
    assert purpose == "result_frame" and "move_away" in options
    assert facts["position"] == AT_TERMINAL["position"]  # the call's own movement stays its own


def test_protection_chosen_in_a_result_question_runs_and_the_same_result_is_judged_again():
    loop, record, calls = _loop({**AT_TERMINAL, "nearby_entities": [ZOMBIE]},
                                ["move_away", "next_step"])
    choice = asyncio.run(loop._decide_result(record, SimpleNamespace(name="move_to"), record["params"], RESULT))
    assert [name for name, _ in calls["protections"]] == ["move_away"]
    tool, arguments = calls["protections"][0][1]
    assert tool == "move_away" and arguments["danger_pos"] == ZOMBIE["pos"]
    assert calls["stops"][0][1] == {"cancel_reflex": False, "invalidate_generation": False}
    assert [purpose for purpose, _, _ in calls["asked"]] == ["result_frame", "result_frame"]
    # Judged again, not run again: one history entry, the call's own result kept.
    assert choice == "step_done" and record["history_total"] == 1
    handoff = loop.runtime.direct_call_handoffs["direct"]["facts"]
    assert handoff["result"]["reason"] == "arrived"
    assert handoff["default_action"]["choice"] == "move_away" and handoff["default_action"]["then"] == "judge_again"


def test_after_protection_the_agent_can_be_called_with_both_results():
    loop, record, calls = _loop({**AT_TERMINAL, "nearby_entities": [ZOMBIE]}, ["move_away", "call_agent"])
    assert asyncio.run(loop._decide_result(record, SimpleNamespace(name="move_to"), record["params"], RESULT)) == "call_agent"
    told = calls["told"][-1]
    assert told["status"] == "handoff_requested" and told["result"]["reason"] == "arrived"
    assert told["default_action"]["tool"] == "move_away"


def test_no_time_left_means_no_protection_and_no_answer():
    loop, record, calls = _loop({**AT_TERMINAL, "nearby_entities": [ZOMBIE]}, ["move_away"], timeout_s=0.0)
    assert asyncio.run(loop._decide_result(record, SimpleNamespace(name="move_to"), record["params"], RESULT)) == "unknown"
    assert calls["protections"] == [] and calls["stops"] == []


def test_a_failed_read_leaves_the_call_facts_not_the_cached_state():
    loop, record, calls = _loop(None, ["next_step"])
    loop.runtime.last_known_body_state = {**AT_TERMINAL, "nearby_entities": [ZOMBIE]}
    asyncio.run(loop._decide_result(record, SimpleNamespace(name="move_to"), record["params"], RESULT))
    assert "move_away" not in calls["asked"][0][1]


# -- plan calls ---------------------------------------------------------------

from minebot.app.work_runtime import WorkPlanExecutor  # noqa: E402

PLAN = {"language": "minebot-work/0.2", "goal": "get logs", "timeout_s": 60,
        "work": {"kind": "call", "id": "logs", "tool": "collect_resource",
                 "intent": "Have four oak logs", "arguments": {"name": "oak_log", "count": 4}}}


def _plan(answers):
    loop, _, calls = _loop({**AT_TERMINAL, "nearby_entities": [ZOMBIE]}, answers)
    questions = []
    judge = loop._judge_terminal_frame

    async def recording(*args, **kwargs):
        questions.append(kwargs.get("question"))
        return await judge(*args, **kwargs)

    loop._judge_terminal_frame = recording
    runs = []

    async def call(name, arguments, *, call_id, timeout_s):
        runs.append(call_id)
        return {"success": True, "reason": "collected"}

    async def state():
        return dict(AT_TERMINAL)  # the plan's own read carries no entities

    executor = WorkPlanExecutor(PLAN, call=call, state=state, result_judge=loop.decide_plan_result)
    return loop, executor, calls, questions, runs


def test_a_plan_result_question_asks_about_danger_on_a_fresh_read_and_protection_runs():
    loop, executor, calls, questions, runs = _plan(["move_away", "next_step"])
    outcome = asyncio.run(executor.run())
    assert outcome.status == "completed" and runs == ["logs"]  # judged again, not collected again
    first = questions[0]["questions"]
    assert "d" in first and "move_away" in first["p"]["criteria"]
    assert [name for name, _ in calls["protections"]] == ["move_away"]
    assert calls["stops"][0][1] == {"cancel_reflex": False, "invalidate_generation": False}
    assert [purpose for purpose, _, _ in calls["asked"]] == ["plan_result_frame", "plan_result_frame"]


def test_a_plan_handed_back_after_protection_says_what_was_done():
    loop, executor, calls, questions, runs = _plan(["move_away", "call_agent"])
    outcome = asyncio.run(executor.run())
    assert outcome.status == "needs_agent"
    facts = executor.records["logs"].needs_agent["facts"]
    assert facts["protection"]["tool"] == "move_away" and facts["protection"]["then"] == "judge_again"


def test_without_a_body_loop_the_plan_does_not_offer_protection_it_cannot_run():
    executor = WorkPlanExecutor(PLAN, call=None)
    options = executor._call_options(PLAN["work"], facts={**AT_TERMINAL, "nearby_entities": [ZOMBIE]},
                                     result={"success": True})
    assert "move_away" not in options
