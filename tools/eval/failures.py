"""Extract evidence-backed failure and handback records without replaying traces.

Failures are observations, not automatic blame: protection followed by death is
only a temporal association, and a plan rewrite can be a sensible response.

Detectable: tool_result success=false; native *Done events explicitly reporting
success/ok=false (deduplicated by native epoch/seq/action); call_agent choices
with their matched question; protective choices within 60s before reflex_death;
unknown answers; structural rewrites of successful durable update_plan results;
explicit replaced=true queued plans; and reflex_death.

Not inferred: prose-only abandonment, queue cancellation as abandonment, or
same-args failures 'after unchanged Body'. args_hash identifies arguments, not
physical state; sampled positions/inventory cannot establish unchanged body or
world between calls. These are intentionally omitted, not approximated.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.eval.common import compact, trace_events
from tools.eval.report import table

PROTECTIVE = {"surface", "engage", "ranged", "move_away", "retreat_safe"}
FAILURE_EVENTS = {"tool_result", "body_events", "call_supervision_request", "call_supervision_result",
                  "work_decision_request", "work_decision_result", "reflex_death", "reflex_plan_queued"}
OMITTED = {
    "plan_abandoned": "No explicit Agent abandonment event; prose, preemption and cancellation do not establish abandonment.",
    "repeated_failure_unchanged_body": "args_hash is present, but no per-call complete Body/world identity proves unchanged state; sparse snapshots are insufficient.",
}
START_MARKER = "<!-- eval-failures:start -->"
END_MARKER = "<!-- eval-failures:end -->"


def record(run: str, row: dict, layer: str, kind: str, tool: str | None, reason: str,
           context, pointers=(), **extra) -> dict:
    seqs = list(dict.fromkeys(seq for seq in (row.get("seq"), *pointers) if seq is not None))
    return {"run": run, "ts": row["ts"], "seq": row.get("seq"), "layer": layer, "kind": kind,
            "tool": tool, "reason": reason, "context": compact(context), "trace_seqs": seqs, **extra}


def question_options(request: dict | None):
    if request is None:
        return None
    questions = (request.get("request") or {}).get("questions") or {}
    return {key: value.get("criteria", value.get("options")) for key, value in questions.items() if isinstance(value, dict)}


def extract_failures(run_dir: Path):
    requests, plans, terminals = {}, {}, set()
    recent = deque()
    run = str(run_dir.resolve())
    for row in trace_events(run_dir / "session" / "trace.jsonl", FAILURE_EVENTS):
        event, ts = row["event"], row["ts"]
        while recent and ts - recent[0][0]["ts"] > 60:
            recent.popleft()
        if event in {"call_supervision_request", "work_decision_request"}:
            family = "work" if event.startswith("work_") else "call"
            key = (family, row.get("tool_call_id", row.get("node_id")), row.get("purpose"))
            requests[key] = row
        elif event in {"call_supervision_result", "work_decision_result"}:
            family = "work" if event.startswith("work_") else "call"
            key = (family, row.get("tool_call_id", row.get("node_id")), row.get("purpose"))
            request = requests.pop(key, None)
            pointers = [request["seq"]] if request is not None else []
            purpose, choice = row.get("purpose") or "unknown", row.get("choice") or "unknown"
            tool = row.get("tool")
            extra = {"purpose": purpose, "question_options": question_options(request), "choice": choice,
                     "request_seq": request.get("seq") if request is not None else None}
            if choice == "call_agent":
                yield record(run, row, "decision", "call_agent", tool, purpose,
                             {"purpose": purpose, "choice": choice, "question_options": extra["question_options"]}, pointers, **extra)
            if choice == "unknown" or row.get("unknown") is True:
                yield record(run, row, "decision", "unknown_answer", tool, purpose,
                             {"purpose": purpose, "choice": choice}, pointers, **extra)
            if choice in PROTECTIVE:
                # Retain only normalized context, not megabytes of raw frame input.
                recent.append((row, pointers, extra))
        elif event == "reflex_death":
            facts = row.get("facts") or {}
            yield record(run, row, "agent", "death", None, "death",
                         {"position": facts.get("position", facts.get("pos")), "dimension": facts.get("dimension")})
            for choice_row, pointers, extra in recent:
                yield record(run, choice_row, "decision", "protection_followed_by_death", choice_row.get("tool"),
                             choice_row["choice"], {"choice": choice_row["choice"], "death_in_s": ts - choice_row["ts"]},
                             [*pointers, row.get("seq")], death_seq=row.get("seq"), death_in_s=ts - choice_row["ts"], **extra)
            recent.clear()
        elif event == "tool_result":
            if row.get("success") is False:
                yield record(run, row, "body", "tool_failure", row.get("tool"), row.get("reason") or "unknown",
                             {"tool_call_id": row.get("tool_call_id"), "args_hash": row.get("args_hash"), "reason": row.get("reason")})
            if row.get("tool") == "update_plan" and row.get("success") is True:
                metrics = (row.get("full_result") or {}).get("metrics") or {}
                plan = metrics.get("plan") or (metrics.get("current") or {}).get("plan") or {}
                pid, revision = plan.get("plan_id"), plan.get("revision")
                titles = [step.get("title") for step in plan.get("steps") or []]
                before = plans.get(pid)
                if pid and revision is not None and before and revision > before["revision"] and titles != before["titles"]:
                    yield record(run, row, "agent", "plan_rewritten", "update_plan", "plan_steps_changed",
                                 plan.get("summary") or titles, [before["seq"]],
                                 plan_id=pid, from_revision=before["revision"], to_revision=revision)
                if pid and revision is not None:
                    plans[pid] = {"titles": titles, "revision": revision, "seq": row.get("seq")}
        elif event == "reflex_plan_queued" and row.get("replaced") is True:
            yield record(run, row, "agent", "plan_rewritten", "delegate_work", "queued_plan_replaced", row.get("goal"))
        elif event == "body_events":
            for native in row.get("events") or []:
                data = native.get("data") or {}
                key = (native.get("bot"), data.get("native_epoch"), native.get("seq"), data.get("action_id"))
                if key in terminals or not native.get("name", "").endswith("Done"):
                    continue
                terminals.add(key)
                if data.get("success") is False or data.get("ok") is False:
                    tool = data.get("action_kind") or native["name"]
                    yield record(run, row, "body", "action_terminal_failure", tool, data.get("reason") or "unknown",
                                 {"name": native["name"], "action_id": data.get("action_id"), "status": data.get("status")},
                                 body_seq=native.get("seq"), action_id=data.get("action_id"), event_name=native["name"])


def write_failures(run_dir: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts, examples = Counter(), {}
    total = 0
    # Output is streamed as well: do not collect an entire failure corpus.
    with (out_dir / "failures.jsonl").open("w", encoding="utf-8") as stream:
        for row in extract_failures(run_dir):
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            key = (row["layer"], row["kind"], row["tool"], row["reason"])
            counts[key] += 1
            examples.setdefault(key, row["seq"])
            total += 1
    groups = [{"layer": k[0], "kind": k[1], "tool": k[2], "reason": k[3], "count": n, "example_seq": examples[k]}
              for k,n in counts.most_common()]
    lines = [START_MARKER, "## Failure corpus", "", f"Records: **{total}**. Source: `{run_dir.resolve()}`.", "",
             "Records preserve trace seq pointers. Body terminal seq is separate from outer trace seq.",
             "Protection followed by death is temporal association; plan rewrites can be appropriate adaptations.", ""]
    lines += table(["Layer", "Kind", "Tool", "Reason", "Count", "Example seq"],
                   [(r["layer"],r["kind"],r["tool"],r["reason"],r["count"],r["example_seq"]) for r in groups])
    lines += ["Omitted kinds:", "", *[f"- {kind}: {why}" for kind,why in OMITTED.items()], "", END_MARKER, ""]
    path = out_dir / "report.md"
    existing = path.read_text() if path.exists() else f"# Evaluation failures: {run_dir.name}\n\n"
    if START_MARKER in existing:
        before, rest = existing.split(START_MARKER, 1)
        _, after = rest.split(END_MARKER, 1)
        existing = before.rstrip() + "\n\n" + "\n".join(lines) + after.lstrip("\n")
    else:
        existing = existing.rstrip() + "\n\n" + "\n".join(lines)
    path.write_text(existing, encoding="utf-8")
    return {"count": total, "groups": groups, "omitted": OMITTED}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    result = write_failures(args.run_dir, args.out_dir or args.run_dir)
    print(f"{result['count']} records; {len(result['groups'])} groups")


if __name__ == "__main__":
    main()
