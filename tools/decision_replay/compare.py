#!/usr/bin/env python3
"""Render per-question agreement, confusion tables, and probability shifts.

    python tools/decision_replay/compare.py RESULT.jsonl > REPORT.md
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.decision_replay.common import FORMAT


def cell(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>").replace("\r", "")


def load_results(path: Path) -> tuple[dict, list[dict]]:
    # A resumed run can contain failed attempts followed by a successful one.
    # Count each set record once; a later error cannot replace a success.
    latest = {}
    with path.open(encoding="utf-8") as stream:
        header = json.loads(next(stream))
        if header.get("format") != FORMAT or header.get("kind") != "result":
            raise ValueError("Expected a Decision replay result file")
        for line in stream:
            if not line.endswith("\n"):
                break  # An interrupted append; replay.py repairs it on resume.
            record = json.loads(line)
            previous = latest.get(record["id"])
            if previous is None or previous.get("error") is not None:
                latest[record["id"]] = record
    return header, sorted(latest.values(), key=lambda r: (r["source"]["run"], r["source"]["seq"]))


def probability(value: object) -> float | None:
    if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1:
        return float(value)
    return None


def summarize(records: list[dict]) -> dict:
    purposes = defaultdict(Counter)
    questions = defaultdict(Counter)
    by_pair = defaultdict(Counter)
    confusion = defaultdict(Counter)
    shifts = []
    unavailable = 0
    latency = defaultdict(lambda: {"recorded": [], "replayed": []})
    for record in records:
        purpose = record["purpose"]
        latency[purpose]["recorded"].append(record.get("decision_ms"))
        if record.get("error") is None:
            latency[purpose]["replayed"].append(record.get("replay_ms"))
        replayed = record.get("replayed")
        new_answers = replayed.get("answers", {}) if isinstance(replayed, dict) else {}
        if not isinstance(new_answers, dict):
            new_answers = {}
        old_answers = record["recorded"]["answers"]
        frame_matches = True
        for key, question in record["payload"]["questions"].items():
            old = old_answers[key]
            new = new_answers.get(key, {})
            if not isinstance(new, dict):
                new = {}
            before = old["choice"]
            after = new.get("choice")
            if record.get("error") is not None or not isinstance(after, str) or after not in question["criteria"]:
                after = "<missing/error>"
            equal = before == after
            frame_matches &= equal
            for counter in (purposes[purpose], questions[key], by_pair[(purpose, key)]):
                counter["total"] += 1
                counter["agree"] += equal
            confusion[purpose][(key, before, after)] += 1
            old_probs, new_probs = old.get("probabilities"), new.get("probabilities")
            for option in question["criteria"]:
                p0 = probability(old_probs.get(option)) if isinstance(old_probs, dict) else None
                p1 = probability(new_probs.get(option)) if isinstance(new_probs, dict) else None
                if p0 is None or p1 is None:
                    unavailable += 1
                    continue
                shifts.append({"delta": abs(p1 - p0), "before": p0, "after": p1,
                               "question": key, "option": option, "record": record})
        purposes[purpose]["requests"] += 1
        purposes[purpose]["all_questions_agree"] += frame_matches
    shifts.sort(key=lambda s: (-s["delta"], s["record"]["id"], s["question"], s["option"]))
    return {"purposes": dict(purposes), "questions": dict(questions), "by_pair": dict(by_pair),
            "confusion": dict(confusion), "shifts": shifts[:20], "probabilities_unavailable": unavailable,
            "latency": dict(latency)}


def quantile(values: list, q: float) -> str:
    ordered = sorted(v for v in values if isinstance(v, (int, float)))
    return f"{ordered[min(len(ordered) - 1, int(len(ordered) * q))]:.0f}" if ordered else "-"


def agreement_row(label: str, counts: Counter) -> str:
    return f"| {cell(label)} | {counts['agree']}/{counts['total']} | {counts['agree'] / counts['total']:.2%} |"


def markdown(header: dict, records: list[dict]) -> str:
    stats = summarize(records)
    failed = sum(r.get("error") is not None for r in records)
    full_coverage = len(records) == header["records"] and failed == 0
    lines = ["# Decision replay comparison", "",
             f"Coverage: {len(records)}/{header['records']} set records; {failed} failed records. "
             "Failures count as disagreements; unattempted records are outside the denominator. "
             "A baseline is valid only at full coverage with no failures.", "",
             "Agreement counts each question independently, including protective choices when danger is safe. "
             "The baseline target is at least 99% per purpose.", "",
             "## Agreement by purpose", "", "| Purpose | Questions agreeing | Agreement | Requests with every question agreeing | Target |",
             "| --- | ---: | ---: | ---: | --- |"]
    for purpose, counts in sorted(stats["purposes"].items()):
        target = "PASS" if counts["agree"] / counts["total"] >= .99 else "BELOW 99%"
        if target == "PASS" and not full_coverage:
            target = "INCOMPLETE"
        lines.append(agreement_row(purpose, counts) +
                     f" {counts['all_questions_agree']}/{counts['requests']} | {target} |")
    lines.extend(["", "## Latency (ms)", "",
                  "Recorded is the live decision_ms; replayed counts successful requests only.", "",
                  "| Purpose | Recorded p50 | Recorded p90 | Replayed p50 | Replayed p90 |",
                  "| --- | ---: | ---: | ---: | ---: |"])
    for purpose, values in sorted(stats["latency"].items()):
        lines.append(f"| {cell(purpose)} | {quantile(values['recorded'], .5)} | {quantile(values['recorded'], .9)} "
                     f"| {quantile(values['replayed'], .5)} | {quantile(values['replayed'], .9)} |")
    lines.extend(["", "## Agreement by question", "", "| Question | Agreeing | Agreement |", "| --- | ---: | ---: |"])
    for key, counts in sorted(stats["questions"].items()):
        lines.append(agreement_row(key, counts))
    lines.extend(["", "## Agreement by purpose and question", "", "| Purpose / question | Agreeing | Agreement |", "| --- | ---: | ---: |"])
    for (purpose, key), counts in sorted(stats["by_pair"].items()):
        lines.append(agreement_row(f"{purpose} / {key}", counts))
    for purpose, counts in sorted(stats["confusion"].items()):
        lines.extend(["", f"## Confusion: {cell(purpose)}", "", "| Question | Recorded → new | Count |", "| --- | --- | ---: |"])
        for (key, before, after), count in sorted(counts.items()):
            lines.append(f"| {cell(key)} | {cell(before)} → {cell(after)} | {count} |")
    lines.extend(["", "## 20 largest absolute probability shifts", "",
                  "Each entry is one question/option probability; missing probabilities are not assumed to be zero. "
                  f"Unavailable option comparisons: {stats['probabilities_unavailable']}."])
    if not stats["shifts"]:
        lines.extend(["", "No comparable probabilities."])
    for index, shift in enumerate(stats["shifts"], 1):
        record = shift["record"]
        source = record["source"]
        lines.extend(["", f"### {index}. {cell(record['purpose'])} / {cell(shift['question'])} / {cell(shift['option'])}", "",
                      f"{shift['before']:.6f} → {shift['after']:.6f}; |Δ| = {shift['delta']:.6f}. "
                      f"Source: {cell(source['run'])}, seq {source['seq']}, request_id {source['request_id']}.", "",
                      "| State key | State line |", "| --- | --- |"])
        for key, value in record["payload"]["state"].items():
            lines.append(f"| {cell(key)} | {cell(value)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    args = parser.parse_args(argv)
    header, records = load_results(args.result)
    print(markdown(header, records), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
