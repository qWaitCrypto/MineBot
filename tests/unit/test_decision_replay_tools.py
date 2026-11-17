import asyncio
from collections import Counter
import copy
import json

import httpx
import pytest

from tools.decision_replay.common import dumps, read_jsonl
from tools.decision_replay.compare import load_results, markdown, summarize
from tools.decision_replay.harvest import harvest, harvest_trace
from tools.decision_replay.replay import replay_set


def payload(index=0, *, options=None, danger=False):
    questions = {"q": {"type": "choice", "instructions": "Choose what to do. 中文",
                       "criteria": options or {"stay": "Wait.", "look": "Look around."}}}
    if danger:
        questions["d"] = {"type": "choice", "instructions": "Danger?",
                          "criteria": {"in_danger": "Yes.", "safe": "No."}}
        questions["p"] = {"type": "choice", "instructions": "Protect?",
                          "criteria": {"flee": "Flee.", "fight": "Fight."}}
    return {"state": {"body": f"Health 20, sample {index}.", "agent_intent": "Build a hut.",
                      "daylight": "Day.", "threats": "No threat.",
                      "inventory": {"z_log": 2, "a_log": 1},
                      "main_hand": "z_log", "position": "[1, 2, 3]"}, "questions": questions}


def response(p, choice="stay", *, in_danger=False):
    answers = {}
    for key, question in p["questions"].items():
        options = list(question["criteria"])
        chosen = ("in_danger" if in_danger else "safe") if key == "d" else "flee" if key == "p" else choice
        answers[key] = {"type": "choice", "choice": chosen,
                        "probabilities": {k: .9 if k == chosen else .1 / (len(options) - 1)
                                          for k in options}}
    return {"answers": answers, "model": "recorded-4b", "effort_used": {"passes_per_decision": 1}}


def event(kind, seq, **fields):
    # All events share the same timestamp: any temporal join will be wrong.
    return {"seq": seq, "ts": 100.00001, "session_id": "test", "event": kind, **fields}


def supervision(seq, p, purpose="running_frame", **extra):
    return event("call_supervision_request", seq, tool_call_id="same-call", fact_revision=3,
                 purpose=purpose, request=p, **extra)


def wire(seq, request_id, p):
    return event("decision_request", seq, request_id=request_id, payload=p, priority=100)


def answered(seq, request_id, raw):
    return event("decision_response", seq, request_id=request_id, response=raw, elapsed_s=.123)


def trace(tmp_path, events, run="track-s"):
    path = tmp_path / run / "session" / "trace.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as stream:
        # An invalid irrelevant line proves pre-filtering precedes json.loads.
        stream.write("irrelevant non-JSON line\n")
        for e in events:
            stream.write(json.dumps(e, sort_keys=e["event"] != "call_supervision_request") + "\n")
    return path


def simple_set(tmp_path, n=8):
    events = []
    purposes = ("running_frame", "idle_frame", "result_frame", "plan_result_frame", "default_result_frame")
    for index in range(n):
        p = payload(index, danger=index % 2 == 0)
        seq = index * 3 + 1
        events.extend([supervision(seq, p, purposes[index % len(purposes)]),
                       wire(seq + 1, index + 1, p), answered(seq + 2, index + 1, response(p))])
    out = tmp_path / "set.jsonl"
    harvest([trace(tmp_path, events)], out)
    return out


def test_order_reconstruction_and_exact_request_id_pairing(tmp_path):
    first = payload(1, options={"keep_going": "Continue.", "next_step": "Next.", "call_agent": "Ask."}, danger=True)
    second = payload(2)
    first_raw = response(first, "next_step")
    second_raw = response(second, "look")
    path = trace(tmp_path, [
        supervision(1, first), wire(2, 91, first),
        supervision(3, second), wire(4, 92, second),
        answered(5, 92, second_raw), answered(6, 91, first_raw),
        event("call_supervision_result", 7, tool_call_id="same-call", fact_revision=3,
              purpose="running_frame", choice="deliberately_wrong_body_choice"),
    ])
    records, audit = harvest_trace(path)
    assert [r["source"]["request_id"] for r in records] == [91, 92]
    assert records[0]["recorded"] == first_raw
    assert records[1]["recorded"] == second_raw
    # Dict equality alone misses the very regression this task addresses.
    assert dumps(records[0]["payload"]) == dumps(first)
    assert list(records[0]["payload"]["questions"]) == ["q", "d", "p"]
    assert records[0]["decision_ms"] == 123
    assert audit["counts"]["answered_unrecoverable"] == 0


def test_separate_done_request_gets_its_own_answer(tmp_path):
    p = payload()
    done = {"state": {"agent_intent": "Logs.", "inventory": "3 logs."},
            "questions": {"a": {"type": "choice", "instructions": "Done?",
                                "criteria": {"shown": "Yes.", "not_shown": "No."}}}}
    path = trace(tmp_path, [supervision(1, p, done_request=done), wire(2, 1, p),
                            answered(3, 1, response(p)), wire(4, 2, done),
                            answered(5, 2, response(done, "not_shown"))])
    records, _ = harvest_trace(path)
    assert len(records) == 2
    assert [r["order_source"]["field"] for r in records] == ["request", "done_request"]
    assert records[1]["recorded"]["answers"]["a"]["choice"] == "not_shown"
    assert dumps(records[1]["payload"]) == dumps(done)


def test_fixed_plan_condition_order_is_reconstructed_but_unknown_fields_are_rejected(tmp_path):
    p = {"state": {"condition": "Enough wood?", "body": "Healthy.", "daylight": "Day.",
                   "threats": "None.", "inventory": "oak_log 3", "main_hand": "oak_log",
                   "position": "now at [1,2,3].",
                   "evidence_scope": "Only supplied facts are known; omitted facts are unknown."},
         "questions": {"q": {"type": "choice", "instructions": "Choose satisfied, pending, or unknown.",
                             "criteria": {"satisfied": "Yes.", "pending": "Not yet.", "unknown": "Unknown."}}}}
    unknown = copy.deepcopy(p)
    unknown["state"]["other_objects"] = {"nested": {"b": 1, "a": 2}}
    path = trace(tmp_path, [wire(1, 1, p), answered(2, 1, response(p, "pending")),
                            wire(3, 2, unknown), answered(4, 2, response(unknown, "unknown"))])
    records, audit = harvest_trace(path)
    assert len(records) == 1
    assert dumps(records[0]["payload"]) == dumps(p)
    assert records[0]["purpose"] == "work_condition"
    assert audit["counts"]["reconstructed_work_condition"] == 1
    assert audit["counts"]["answered_unrecoverable"] == 1


@pytest.mark.parametrize("failure", ["missing", "sorted", "ambiguous", "redacted"])
def test_unrecoverable_order_is_audited_not_guessed(tmp_path, failure):
    p = payload()
    source = p
    if failure == "sorted":
        source = json.loads(json.dumps(p, sort_keys=True))
    if failure == "redacted":
        p["state"]["body"] = "<redacted>"
    events = [] if failure == "missing" else [supervision(1, source)]
    if failure == "ambiguous":
        other = copy.deepcopy(p)
        other["questions"]["q"]["criteria"] = dict(reversed(list(other["questions"]["q"]["criteria"].items())))
        events.append(supervision(2, other))
    events.extend([wire(3, 1, p), answered(4, 1, response(p))])
    records, audit = harvest_trace(trace(tmp_path, events))
    assert records == []
    assert audit["counts"]["answered_unrecoverable"] == 1
    assert audit["excluded"][0]["request_id"] == 1


def test_missing_and_failed_answers_are_not_fabricated(tmp_path):
    p, p2 = payload(), payload(2)
    records, audit = harvest_trace(trace(tmp_path, [supervision(1, p), wire(2, 1, p),
        answered(3, 1, None), supervision(4, p2), wire(5, 2, p2)]))
    assert records == []
    assert audit["counts"]["unanswered_or_invalid_response"] == 1
    assert audit["counts"]["missing_decision_response"] == 1


def test_duplicate_pairing_key_is_rejected(tmp_path):
    p = payload()
    with pytest.raises(ValueError, match="Duplicate request_id"):
        harvest_trace(trace(tmp_path, [supervision(1, p), wire(2, 1, p), wire(3, 1, p)]))


def test_stratification_is_reproducible_per_run_and_uses_danger_gate(tmp_path):
    events = []
    for i in range(24):
        p = payload(i, danger=i == 21)
        purpose = "running_frame" if i == 23 else "idle_frame"
        raw = response(p, "look" if i == 22 else "stay", in_danger=i == 21)
        seq = i * 3 + 1
        events.extend([supervision(seq, p, purpose), wire(seq + 1, i, p), answered(seq + 2, i, raw)])
    paths = [trace(tmp_path, events, run=run) for run in ("track-s", "track-p")]
    out = tmp_path / "set.jsonl"
    h = harvest(paths, out, seed=42, idle_stay_sample=4)
    first = out.read_bytes()
    harvest(paths, out, seed=42, idle_stay_sample=4)
    assert out.read_bytes() == first
    _, records = read_jsonl(out)
    assert Counter(r["source"]["run"] for r in records) == {"track-s": 7, "track-p": 7}
    assert h["selection"]["seed"] == 42
    for run in ("track-s", "track-p"):
        chosen = {r["source"]["request_id"] for r in records if r["source"]["run"] == run}
        assert {21, 22, 23} <= chosen
    harvest(paths, out, seed=43, idle_stay_sample=4)
    assert out.read_bytes() != first


def test_recorded_set_replayed_through_live_client_stub_has_100_percent_agreement(tmp_path):
    set_path = simple_set(tmp_path)
    _, records = read_jsonl(set_path)
    expected = {dumps(r["payload"]).encode(): r["recorded"] for r in records}
    active = peak = 0
    calls = []

    async def stub(request):
        nonlocal active, peak
        assert request.method == "POST"
        assert str(request.url) == "http://decision.invalid/v1/systemone"
        assert request.headers["content-type"] == "application/json"
        assert "authorization" not in request.headers
        # Byte equality verifies every object and option order, plus no
        # extra model/temperature/effort fields or URL parameters.
        assert request.content in expected
        calls.append(request.content)
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(.001)
        active -= 1
        return httpx.Response(200, json=expected[request.content])

    out = tmp_path / "result.jsonl"
    counts = asyncio.run(replay_set(set_path, out, endpoint="http://decision.invalid/v1/systemone",
                                   transport=httpx.MockTransport(stub)))
    assert counts["ok"] == len(records)
    assert peak == 2
    assert len(calls) == len(records)
    header, results = load_results(out)
    stats = summarize(results)
    assert set(stats["questions"]) == {"q", "d", "p"}
    for counters in (stats["purposes"], stats["questions"], stats["by_pair"]):
        assert all(c["agree"] == c["total"] for c in counters.values())
    assert all(s["delta"] == 0 for s in stats["shifts"])
    assert "100.00%" in markdown(header, results)


def test_resume_skips_success_repairs_tail_retries_failure_and_rejects_changes(tmp_path):
    set_path = simple_set(tmp_path, n=4)
    _, records = read_jsonl(set_path)
    expected = {dumps(r["payload"]).encode(): r["recorded"] for r in records}
    failing = dumps(records[0]["payload"]).encode()
    calls = []

    def stub(request):
        calls.append(request.content)
        return httpx.Response(503) if request.content == failing else httpx.Response(200, json=expected[request.content])

    out = tmp_path / "result.jsonl"
    kwargs = {"endpoint": "http://decision.invalid", "concurrency": 1}
    first = asyncio.run(replay_set(set_path, out, transport=httpx.MockTransport(stub), **kwargs))
    assert first["failed"] == 1
    with out.open("ab") as stream:
        stream.write(b'{"id":"partial\xe4')
    calls.clear()

    def success(request):
        calls.append(request.content)
        return httpx.Response(200, json=expected[request.content])

    second = asyncio.run(replay_set(set_path, out, transport=httpx.MockTransport(success), **kwargs))
    assert second == {"skipped": 3, "ok": 1}
    assert calls == [failing]
    header, results = load_results(out)
    assert len(results) == 4
    assert all(c["agree"] == c["total"] for c in summarize(results)["purposes"].values())
    for override in ({"model": "new"}, {"endpoint": "http://other.invalid"}, {"timeout_s": 3}):
        with pytest.raises(ValueError, match="configuration differs"):
            asyncio.run(replay_set(set_path, out, transport=httpx.MockTransport(success), **(kwargs | override)))
    header, originals = read_jsonl(set_path)
    originals[0]["payload"]["state"]["body"] = "Changed."
    set_path.write_text("\n".join(dumps(x) for x in [header, *originals]) + "\n")
    with pytest.raises(ValueError, match="configuration differs"):
        asyncio.run(replay_set(set_path, out, transport=httpx.MockTransport(success), **kwargs))


def test_compare_reports_independent_choices_errors_and_largest_shifts(tmp_path):
    set_path = simple_set(tmp_path, n=4)
    _, originals = read_jsonl(set_path)
    records = [{**r, "replayed": copy.deepcopy(r["recorded"]), "error": None} for r in originals]
    changed = records[0]["replayed"]["answers"]["p"]
    changed["choice"] = "fight"
    changed["probabilities"] = {"flee": .02, "fight": .98}
    records[0]["payload"]["state"]["threats"] = "Zombie | nearby\nHealth fell."
    records[1]["replayed"] = {"answers": []}
    records[1]["error"] = "no_complete_answer"
    records[2]["replayed"]["answers"]["q"].pop("probabilities")
    for index, record in enumerate(records):
        record["replay_ms"] = 10.0 * (index + 1)
    stats = summarize(records)
    assert sorted(ms for values in stats["latency"].values() for ms in values["replayed"]) == [10.0, 30.0, 40.0]
    assert stats["by_pair"][("running_frame", "q")]["agree"] == 1
    assert stats["by_pair"][("running_frame", "p")]["agree"] == 0
    assert stats["shifts"][0]["delta"] == pytest.approx(.88)
    assert stats["probabilities_unavailable"] > 0
    report = markdown({"records": 5}, records)
    assert "Coverage: 4/5" in report
    assert "## Latency (ms)" in report
    assert "flee → fight" in report
    assert "BELOW 99%" in report
    assert "<missing/error>" in report
    assert "Zombie \\| nearby<br>Health fell." in report
    assert report.count("### ") <= 20
    # All Markdown table rows must have consistent columns in each table.
    assert "| running_frame | 2/3 | 66.67% | 0/1 | BELOW 99% |" in report


def test_model_override_is_explicit_and_appended_without_reordering(tmp_path):
    set_path = simple_set(tmp_path, n=1)
    _, records = read_jsonl(set_path)
    calls = []

    def stub(request):
        body = json.loads(request.content)
        assert list(body) == ["state", "questions", "model"]
        assert body.pop("model") == "replacement-4b"
        assert dumps(body) == dumps(records[0]["payload"])
        calls.append(body)
        return httpx.Response(200, json=records[0]["recorded"])

    asyncio.run(replay_set(set_path, tmp_path / "result.jsonl", endpoint="http://decision.invalid",
                           model="replacement-4b", transport=httpx.MockTransport(stub)))
    assert len(calls) == 1
