#!/usr/bin/env python3
"""Harvest answered wire requests using the trace's order-preserved originals.

    python tools/decision_replay/harvest.py TRACE... --out SET.jsonl
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.decision_replay.common import (
    FORMAT, LIVE_TIMEOUT_S, complete_answers, digest, dumps, frame_choice,
    payload_key, write_jsonl,
)

EVENTS = (b'"call_supervision_request"', b'"decision_request"', b'"decision_response"')


def ordered_source(payload: dict) -> bool:
    # JsonlObservationSink preserves this event; older all-sorted traces
    # cannot supply original order. Reject them instead of imposing an order.
    return list(payload)[:2] == ["state", "questions"] and all(
        list(question)[:3] == ["type", "instructions", "criteria"]
        for question in payload.get("questions", {}).values()
    )


def condition_source(payload: dict, event: dict) -> dict | None:
    """Recover the fixed plan-condition constructor, never arbitrary fact maps.

    work_runtime.py:1207-1234 inserts condition/about, body_condition_facts,
    then evidence_scope. reflex_question.py:899-913 defines the prose-fact
    order. Only this string-only schema is recoverable from a sorted copy;
    other_objects/unknown_fields or new fields must retain an ordered source.
    """
    if set(payload) != {"state", "questions"} or set(payload.get("questions", {})) != {"q"}:
        return None
    state = payload.get("state")
    question = payload["questions"]["q"]
    state_order = ("condition", "about", "body", "daylight", "players", "threats",
                   "inventory", "main_hand", "position", "evidence_scope")
    if not isinstance(state, dict) or not {"condition", "body", "evidence_scope"} <= set(state):
        return None
    if not set(state) <= set(state_order) or not all(isinstance(value, str) for value in state.values()):
        return None
    if state["evidence_scope"] != "Only supplied facts are known; omitted facts are unknown.":
        return None
    if (not isinstance(question, dict) or set(question) != {"type", "instructions", "criteria"}
            or question["type"] != "choice"
            or question["instructions"] != "Choose satisfied, pending, or unknown."
            or not isinstance(question["criteria"], dict)
            or set(question["criteria"]) != {"satisfied", "pending", "unknown"}
            or not all(isinstance(v, str) for v in question["criteria"].values())):
        return None
    restored = {"state": {key: state[key] for key in state_order if key in state},
                "questions": {"q": {"type": question["type"], "instructions": question["instructions"],
                                    "criteria": {key: question["criteria"][key] for key in
                                                 ("satisfied", "pending", "unknown")}}}}
    return {"payload": restored, "purpose": "work_condition", "seq": event["seq"],
            "field": "work_condition_constructor", "session_id": event.get("session_id", "default"),
            "tool_call_id": None, "fact_revision": None}


def harvest_trace(path: Path) -> tuple[list[dict], dict]:
    path = path.resolve()
    run = path.parent.parent.name if path.parent.name == "session" else path.parent.name
    candidates: dict[str, list[dict]] = defaultdict(list)
    pending: dict[tuple, dict] = {}
    seen_requests: set[tuple] = set()
    seen_responses: set[tuple] = set()
    records = []
    counts = Counter()
    excluded = []
    models = Counter()
    trace_hash = hashlib.sha256()
    last_seq = -1
    with path.open("rb") as stream:
        for line_number, line in enumerate(stream, 1):
            trace_hash.update(line)
            # The two real traces exceed 1 GB together. Do not decode other events.
            if not any(event in line for event in EVENTS):
                continue
            try:
                event = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_number}") from exc
            kind = event.get("event")
            if kind not in {"call_supervision_request", "decision_request", "decision_response"}:
                continue
            if event["seq"] <= last_seq:
                raise ValueError(f"Trace seq must increase; split restarted traces: {path}:{line_number}")
            last_seq = event["seq"]
            session = event.get("session_id", "default")
            counts[kind] += 1
            if kind == "call_supervision_request":
                for field in ("request", "done_request"):
                    payload = event.get(field)
                    if not isinstance(payload, dict):
                        continue
                    candidates[payload_key(payload)].append({
                        "payload": payload, "purpose": event["purpose"],
                        "seq": event["seq"], "field": field, "session_id": session,
                        "tool_call_id": event.get("tool_call_id"),
                        "fact_revision": event.get("fact_revision"),
                    })
                continue
            key = (session, event["request_id"])
            if kind == "decision_request":
                if key in seen_requests:
                    raise ValueError(f"Duplicate request_id in session {session}: {key[1]}")
                seen_requests.add(key)
                options = [c for c in candidates.get(payload_key(event["payload"]), [])
                           if c["session_id"] == session and c["payload"] == event["payload"]]
                reconstructed = condition_source(event["payload"], event) if not options else None
                if reconstructed is not None:
                    options = [reconstructed]
                # Content joins only recover order and purpose. Answers are
                # exclusively joined by (session_id, request_id), never time.
                signatures = {(dumps(c["payload"]), c["purpose"], c["field"]) for c in options}
                error = ""
                if not options:
                    error = "no_order_preserved_source"
                elif len(signatures) != 1:
                    error = "ambiguous_order_or_purpose"
                elif not ordered_source(options[-1]["payload"]):
                    error = "source_was_sorted"
                elif "<redacted>" in dumps(options[-1]["payload"]):
                    error = "source_was_redacted"
                source = options[-1] if options else None
                if source and reconstructed is None:
                    # Consume equivalent copies, including cancelled frames
                    # which never reached the wire. Their payloads/purposes
                    # are identical; seq is provenance, not an answer join.
                    content_key = payload_key(event["payload"])
                    candidates[content_key] = [c for c in candidates[content_key] if c not in options]
                pending[key] = {"event": event, "ordered": source, "error": error}
                continue
            if key in seen_responses:
                raise ValueError(f"Duplicate response for request_id {key[1]}")
            seen_responses.add(key)
            request = pending.pop(key, None)
            if request is None:
                raise ValueError(f"Response has no exact request: {key}")
            wire = request["event"]
            response = event.get("response")
            if not complete_answers(wire["payload"], response):
                counts["unanswered_or_invalid_response"] += 1
                continue
            if request["error"]:
                # Fail closed on lost order; keep an explicit audit rather
                # than manufacturing a payload from the sorted wire event.
                excluded.append({"seq": wire["seq"], "request_id": key[1],
                                 "reason": request["error"]})
                continue
            original = request["ordered"]
            source = {"run": run, "trace": str(path), "seq": wire["seq"], "ts": wire["ts"],
                      "session_id": session, "request_id": key[1]}
            record = {
                "type": "request", "id": digest([str(path), session, wire["seq"], key[1]]),
                "source": source, "purpose": original["purpose"],
                "payload": original["payload"], "recorded": response,
                "decision_ms": event["elapsed_s"] * 1000,
                "order_source": {k: original[k] for k in
                                 ("seq", "field", "tool_call_id", "fact_revision")},
            }
            records.append(record)
            if original["field"] == "work_condition_constructor":
                counts["reconstructed_work_condition"] += 1
            models[str(response.get("model", "unspecified"))] += 1
    records.sort(key=lambda record: record["source"]["seq"])
    counts["missing_decision_response"] = len(pending)
    counts["answered_recoverable"] = len(records)
    counts["answered_unrecoverable"] = len(excluded)
    return records, {"run": run, "trace": str(path), "sha256": trace_hash.hexdigest(),
                     "counts": dict(counts), "recorded_models": dict(models), "excluded": excluded}


def select(records: list[dict], *, seed: int, idle_stay_sample: int) -> list[dict]:
    if idle_stay_sample < 0:
        raise ValueError("idle stay sample must be nonnegative")
    keep, stays = [], []
    for record in records:
        if record["purpose"] == "idle_frame" and frame_choice(record["payload"], record["recorded"]["answers"]) == "stay":
            stays.append(record)
        else:
            keep.append(record)
    keep.extend(random.Random(seed).sample(stays, min(len(stays), idle_stay_sample)))
    return sorted(keep, key=lambda record: record["source"]["seq"])


def harvest(paths: list[Path], out: Path, *, seed: int = 0, idle_stay_sample: int = 600) -> dict:
    if len({p.resolve() for p in paths}) != len(paths):
        raise ValueError("A trace was supplied more than once")
    selected, runs = [], []
    for path in paths:
        records, audit = harvest_trace(path)
        chosen = select(records, seed=seed, idle_stay_sample=idle_stay_sample)
        audit["answered_per_purpose"] = dict(Counter(r["purpose"] for r in records))
        audit["selected_per_purpose"] = dict(Counter(r["purpose"] for r in chosen))
        audit["idle_stay_available"] = sum(r["purpose"] == "idle_frame" and
            frame_choice(r["payload"], r["recorded"]["answers"]) == "stay" for r in records)
        selected.extend(chosen)
        runs.append(audit)
    header = {
        "type": "header", "format": FORMAT, "kind": "set",
        "selection": {"seed": seed, "idle_stay_sample_per_run": idle_stay_sample,
                      "rule": "All recoverable answered non-idle requests; all idle actions other than stay; seeded random.sample of idle stay per trace run.",
                      "idle_choice": "p if d=in_danger, otherwise q", "sampling": "random.Random(seed).sample in wire seq order, reset per run"},
        "wire": {"client": "minebot.app.decision_client.DecisionClient", "timeout_s": LIVE_TIMEOUT_S,
                 "trust_env": False, "body": "json=payload; no model, temperature, effort, auth, or other parameters added"},
        "pairing": "(trace, session_id, request_id); order from an equal preserved payload or the validated string-only work-condition constructor",
        "runs": runs, "records": len(selected),
        "per_purpose": dict(Counter(r["purpose"] for r in selected)),
    }
    write_jsonl(out, header, selected)
    return header


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", nargs="+", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seed", default=0, type=int)
    parser.add_argument("--idle-stay-sample", default=600, type=int)
    args = parser.parse_args(argv)
    header = harvest(args.traces, args.out, seed=args.seed, idle_stay_sample=args.idle_stay_sample)
    print(dumps({"out": str(args.out), "records": header["records"], "per_purpose": header["per_purpose"],
                 "excluded_answered": sum(r["counts"]["answered_unrecoverable"] for r in header["runs"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
