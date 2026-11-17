"""Rewrite a runtime trace losslessly, or expand it for legacy bench scripts.

python tools/rewrite_trace.py INPUT OUTPUT --limit 200000 --report REPORT.json
python tools/rewrite_trace.py INPUT OUTPUT --expand
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.trace_facts import TraceFactsReader, TraceFactsWriter, encode_line  # noqa: E402


def rewrite(source: Path, output: Path, *, limit: int | None = None, expand: bool = False) -> dict:
    if source.resolve() == output.resolve():
        raise ValueError("input and output must differ")
    reader, verifier, writer = TraceFactsReader(), TraceFactsReader(), TraceFactsWriter()
    before, after, counts = Counter(), Counter(), Counter()
    questions: dict[str, str] = {}
    matches = Counter()
    first_ts = last_ts = None
    lines = events = 0
    with source.open("rb") as incoming, output.open("w", encoding="utf-8") as outgoing:
        for line in itertools.islice(incoming, limit):
            lines += 1
            original = json.loads(line)
            before[original["event"]] += len(line)
            record = reader.decode(original)
            if record is None:
                continue
            events += 1
            counts[record["event"]] += 1
            ts = record.get("ts")
            if isinstance(ts, (int, float)):
                first_ts = ts if first_ts is None else min(first_ts, ts)
                last_ts = ts if last_ts is None else max(last_ts, ts)
            if record["event"] == "call_supervision_request":
                for field in ("request", "done_request"):
                    if record.get(field) is not None:
                        questions[_digest(record[field])] = field
            elif record["event"] == "decision_request":
                matches[questions.get(_digest(record.get("payload")), "other")] += 1
            for row in [record] if expand else writer.encode(record):
                restored = verifier.decode(row)
                if restored is not None:
                    if restored != record or list(restored) != list(record):
                        raise AssertionError(f"round trip failed at input line {lines}")
                    if record["event"] == "call_supervision_request" and _ordered(restored) != _ordered(record):
                        raise AssertionError(f"ordered round trip failed at input line {lines}")
                encoded = encode_line(row)
                after[row["event"]] += len(encoded.encode("utf-8"))
                outgoing.write(encoded)
    hours = (last_ts - first_ts) / 3600 if first_ts is not None and last_ts != first_ts else None
    return {"input": str(source), "output": str(output), "input_lines": lines,
            "round_tripped_events": events, "hours": hours, "counts": dict(counts),
            "before_bytes": dict(before), "after_bytes": dict(after),
            "before_mb": sum(before.values()) / 1e6, "after_mb": sum(after.values()) / 1e6,
            "before_mb_per_hour": sum(before.values()) / 1e6 / hours if hours else None,
            "after_mb_per_hour": sum(after.values()) / 1e6 / hours if hours else None,
            "decision_payload_matches": dict(matches)}


def _ordered(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--expand", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = rewrite(args.input, args.output, limit=args.limit, expand=args.expand)
    rendered = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
