"""A persisted raw Decision frame must reproduce the sent ordered request."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

from minebot.app.direct_call_supervision import CallSupervisor
from minebot.app.observability import JsonlObservationSink
from minebot.app.runtime_trace import RuntimeTrace
from minebot.app.work_runtime import NodeRecord, WorkPlanExecutor


class CapturingDecision:
    def __init__(self):
        self.sent = []

    async def ask(self, request, **kwargs):
        self.sent.append(copy.deepcopy(request))
        return SimpleNamespace(raw=None)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def test_recorded_raw_running_frame_recompiles_the_sent_request_in_the_same_order(tmp_path):
    path = tmp_path / "trace.jsonl"
    client = CapturingDecision()
    trace = RuntimeTrace(sink=JsonlObservationSink(path))
    runtime = SimpleNamespace(trace=trace, decision_client=client,
                              agent_context=SimpleNamespace(goal_text="Collect the requested materials"))
    compiler = CallSupervisor(runtime)
    raw = {"tool": "collect_resource", "state": "running", "phase": "collecting",
           "elapsed_s": 2.5, "since_progress_s": 0.5, "last_progress_signal": "inventory",
           "facts": {"inventory": {"oak_log": 3, "coal": 1}, "health": 20, "food": 20,
                     "oxygen": 300, "pos": [4, 64, 8]}}
    params = {"item": "oak_log", "count": 4, "intent": "Have four oak logs"}
    context = {"intent": "Have four oak logs", "node_description": "Have four oak logs"}
    tool = SimpleNamespace(name="collect_resource")
    with patch("time.monotonic", return_value=100):
        execution = compiler._frame(raw)
        asyncio.run(compiler.judge("call-1", tool, params, execution, 0,
                                  supervision_context=context))
    trace.close()
    record = next(row for row in map(json.loads, path.read_text().splitlines())
                  if row["event"] == "call_supervision_request")
    recompiler = CallSupervisor(runtime)
    saved, inputs = record["frame_input"], record["compiler_input"]
    recompiler._vitals._samples = copy.deepcopy(saved["vital_samples"])
    with patch("time.monotonic", return_value=saved["monotonic_s"]):
        restored = recompiler._frame(saved["execution"])
        options = recompiler._options_for(tool, {"tool": tool.name, "params": inputs["params"],
            "supervision_context": inputs["supervision_context"]}, execution=restored)
        request = recompiler._question("call-1", tool, inputs["params"], restored,
            idle_s=record["idle_s"], purpose=record["purpose"], options=options,
            supervision_context=inputs["supervision_context"])
    request.pop("_request_chars", None)
    request.pop("_request_chars_before", None)
    # The running frame's own "is the intent shown?" request is recorded
    # beside the main one and recompiles the same way.
    assert encoded(request.pop("_done")) == encoded(record["done_request"])
    assert encoded(request) == encoded(client.sent[0])
    assert encoded(record["request"]) == encoded(client.sent[0])


def test_recorded_raw_plan_result_recompiles_the_sent_request_in_the_same_order(tmp_path):
    path = tmp_path / "trace.jsonl"
    client = CapturingDecision()
    trace = RuntimeTrace(sink=JsonlObservationSink(path))
    runtime = SimpleNamespace(trace=trace, decision_client=client,
                              agent_context=SimpleNamespace(goal_text=""))
    plan = {"language": "minebot-work/0.2", "goal": "Collect fuel", "timeout_s": 120,
            "work": {"kind": "call", "intent": "Have twelve coal", "tool": "read_inventory",
                     "arguments": {}}}
    executor = WorkPlanExecutor(plan, call=None, state=None, decision=None)
    node = plan["work"]
    context = executor._call_plan(node, [])
    facts = {"tool_result": {"success": True, "reason": "inventory_counted", "canRetry": False},
             "tool_arguments": {}, "body_state": {"inventory_counts": {"oak_log": 2, "coal": 12},
                "health": 20, "food": 20, "oxygen": 300, "pos": [4, 64, 8]}, "history": []}
    question = executor._call_question(node, "call-1", [], NodeRecord(decision_facts=facts))
    compiler = CallSupervisor(runtime)
    asyncio.run(compiler.judge("call-1", SimpleNamespace(name="read_inventory"), {},
        {"state": "succeeded", "facts": facts["body_state"]}, 0, purpose="plan_result_frame",
        supervision_context=context, decision_context=facts, question=question))
    trace.close()
    record = next(row for row in map(json.loads, path.read_text().splitlines())
                  if row["event"] == "call_supervision_request")
    inputs = record["compiler_input"]
    restored = executor._call_question(node, "call-1", [],
                                      NodeRecord(decision_facts=inputs["decision_context"]))
    assert encoded(restored) == encoded(client.sent[0])
    assert encoded(record["request"]) == encoded(client.sent[0])
