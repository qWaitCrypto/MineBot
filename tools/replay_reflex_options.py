"""Replay raw trace frames through the production question builders."""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "/tmp/reflexbench")
import bench  # type: ignore
from minebot.app.decision_client import DecisionPriority
from minebot.app.direct_call_supervision import CallSupervisor
from minebot.app.work_runtime import NodeRecord, WorkPlanExecutor


def _load_frame_metadata() -> list[dict]:
    source = Path("/tmp/reflexbench/frames1.py").read_text(encoding="utf-8")
    namespace: dict = {}
    exec(source.split("import os\nRT=")[0], namespace)
    return namespace["F"]


def _orders(keys: list[str], limit: int = 12) -> list[list[str]]:
    result: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for order in itertools.permutations(keys):
        if tuple(order) not in seen:
            seen.add(tuple(order))
            result.append(list(order))
        if len(result) >= limit:
            return result
    return result[:limit]


def _raw_frame(meta: dict, candidates: dict[tuple[str, int], dict]) -> dict:
    source = meta["src"]
    rid = int(meta["rid"])
    trace = bench.trace(source)
    request = trace["req"][rid]
    payload = request["payload"]
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    tool_data = state.get("tool") if isinstance(state.get("tool"), dict) else {}
    tool = str(tool_data.get("name") or meta.get("tool") or "unknown")
    params = dict(tool_data.get("arguments") or meta.get("args") or {})
    timestamp = meta.get("at", request["ts"] - trace["t0"])
    body, previous, entities = bench.snap(source, timestamp)
    facts = dict(body)
    facts["position"] = body.get("pos")
    facts["nearby_entities"] = entities if entities is not None else body.get("entities_near")
    facts["in_water"] = bool(body.get("oxygen", 300) < 300)
    if previous.get("oxygen") is not None:
        facts["oxygen_previous"] = previous["oxygen"]
    if previous.get("health") is not None:
        facts["health_previous"] = previous["health"]
        if body.get("health") is not None and body["health"] < previous["health"]:
            facts["recently_hurt"] = True
    candidate = candidates.get((source, rid), {})
    result = candidate.get("result") if meta.get("kind") == "result" else None
    raw_plan = candidate.get("plan") if isinstance(candidate.get("plan"), dict) else {}
    operation_id = f"{source}:{tool}:{json.dumps(params, ensure_ascii=False, sort_keys=True)}"
    execution = {
        "operation_id": operation_id,
        "tool": tool,
        "state": "running" if result is None else ("succeeded" if result.get("success") is True else "failed"),
        "phase": body.get("phase") or "working",
        "elapsed_s": 0.0,
        "facts": facts,
        "result": result or {},
    }
    plan = raw_plan
    context = {
        "goal": str(plan.get("goal") or ""),
        "intent": str(meta.get("intent") or plan.get("intent") or plan.get("goal") or ""),
        "intent_chain": list(plan.get("intent_chain") or []),
        "is_plan": bool(meta.get("kind") == "result" and _is_plan_result(trace, request["ts"], tool)),
    }
    next_call = str(meta.get("next") or "")
    return {
        "meta": meta, "trace": trace, "request": request, "state": state,
        "tool": tool, "params": params, "facts": facts, "result": result,
        "execution": execution, "context": context, "next_call": next_call,
    }


def _is_plan_result(trace: dict, ts: float, tool: str) -> bool:
    for row in trace.get("work_requests", []):
        if abs(float(row.get("ts", 0)) - ts) <= 3.0:
            req = row.get("request") or {}
            req_tool = ((req.get("state") or {}).get("tool") or {}).get("name")
            if req_tool == tool:
                return True
    return False


def _prepare_traces() -> None:
    for source in list(bench.SRC):
        trace = bench.trace(source)
        rows = [json.loads(line) for line in open(bench.L + bench.SRC[source] + "/trace.jsonl", errors="replace") if line.strip().startswith("{")]
        trace["work_requests"] = [row for row in rows if row.get("event") == "work_decision_request"]


def _direct_request(supervisor: CallSupervisor, frame: dict, options: dict[str, str]) -> dict:
    tool = SimpleNamespace(
        name=frame["tool"], supervision_reader=None,
        supervision_explanation=frame["state"].get("tool", {}).get("normal_work")
        or "The Body approaches the work context, performs the requested operation, and verifies the terminal result.",
    )
    record = {
        "supervision_context": frame["context"],
        "params": frame["params"],
        "next_call": frame["next_call"],
        "reader_new_fact": False,
    }
    compiled = supervisor._options_for(tool, record, execution=frame["execution"])
    ordered = {key: options[key] for key in options if key in compiled}
    if frame["result"] is None:
        return supervisor._question(
            frame["execution"]["operation_id"], tool, frame["params"], frame["execution"],
            idle_s=0.0, purpose="replay_running", options=ordered,
            supervision_context=frame["context"],
        )
    return supervisor._question(
        frame["execution"]["operation_id"], tool, frame["params"], frame["execution"],
        idle_s=0.0, purpose="replay_result", options=ordered,
        supervision_context=frame["context"],
        decision_context={
            "tool_arguments": frame["params"], "tool_result": frame["result"],
            "body_state": frame["facts"], "history": {"recent_results": []}, "reads": [],
        },
    )


def _plan_request(frame: dict, options: dict[str, str]) -> dict:
    node = {
        "kind": "call", "id": frame["execution"]["operation_id"],
        "tool": frame["tool"], "arguments": frame["params"],
        "intent": frame["context"]["intent"],
    }
    executor = WorkPlanExecutor.__new__(WorkPlanExecutor)
    executor.plan = {"goal": frame["context"]["goal"]}
    record = NodeRecord()
    record.decision_facts = {
        "tool_result": frame["result"] or {},
        "tool_arguments": frame["params"],
        "body_state": frame["facts"],
        "execution": {"tool": frame["tool"], "normal_work": frame["state"].get("tool", {}).get("normal_work", "")},
        "history": [], "history_earlier": 0,
    }
    payload = executor._call_question(node, frame["execution"]["operation_id"], [frame["context"]["intent"]], record)
    criteria = payload["questions"]["q"]["criteria"]
    payload["questions"]["q"]["criteria"] = {key: criteria[key] for key in options if key in criteria}
    return payload


async def main(path: Path, output: Path, url: str, only: set[str] | None = None) -> None:
    _prepare_traces()
    metadata = _load_frame_metadata()
    if only:
        metadata = [item for item in metadata if item["id"] in only]
    candidates = {(item["src"], int(item["rid"])): item for item in json.loads(Path("/tmp/reflexbench/cands.json").read_text())}
    runtime = SimpleNamespace(
        agent_context=None, native_task_workspace=None, native_plan=None,
        trace=SimpleNamespace(emit=lambda *args, **kwargs: None), decision_client=None,
    )
    supervisor = CallSupervisor(runtime, decision_url=url)
    results = []
    try:
        for meta in metadata:
            frame = _raw_frame(meta, candidates)
            tool = SimpleNamespace(name=frame["tool"])
            record = {"supervision_context": frame["context"], "params": frame["params"], "next_call": frame["next_call"], "reader_new_fact": False}
            compiled = supervisor._options_for(tool, record, execution=frame["execution"])
            answers = []
            for order in _orders(list(compiled)):
                ordered = {key: compiled[key] for key in order}
                started = time.monotonic()
                if frame["context"]["is_plan"]:
                    request = _plan_request(frame, ordered)
                else:
                    request = _direct_request(supervisor, frame, ordered)
                answer = await supervisor._client.ask(request, priority=DecisionPriority.SUPERVISION)
                choice = supervisor._answer_for_questions(answer.raw, answer.choice, request.get("questions", {}))
                expected = meta.get("expect", ["keep_going"])
                answers.append({
                    "order": order, "choice": choice,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    "correct": choice in expected,
                })
            results.append({
                "id": meta["id"], "expected": meta.get("expect", ["keep_going"]),
                "options": list(compiled), "answers": answers,
                "correct_count": sum(item["correct"] for item in answers),
                "consistent": len({item["choice"] for item in answers}) == 1,
                "question": _direct_request(supervisor, frame, compiled)
                if not frame["context"]["is_plan"] else _plan_request(frame, compiled),
            })
    finally:
        await supervisor.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"url": url, "frames": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = sum(len(item["answers"]) for item in results)
    correct = sum(item["correct_count"] for item in results)
    print(json.dumps({
        "frames": len(results), "requests": total, "correct": correct,
        "plan_result_frames": sum(1 for item in results if item["question"]["state"].get("normal_work") is not None),
        "inconsistent_frames": [item["id"] for item in results if not item["consistent"]],
        "output": str(output),
    }, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default="/tmp/reflexbench/frames1.py")
    parser.add_argument("--output", default="logs/reflex-options-replay-raw.json")
    parser.add_argument("--url", default="http://117.176.14.165:10004/v1/systemone")
    parser.add_argument("--only", nargs="*", default=[])
    args = parser.parse_args()
    asyncio.run(main(Path(args.fixture), Path(args.output), args.url, set(args.only) or None))
