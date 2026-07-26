#!/usr/bin/env python3
"""Harvest decision-replay fixture packs from persisted RuntimeTrace JSONL.

brain-cognitive-framework.md §10.1 (F7). One pack per input trace file; one
fixture per settled progress epoch (= one model-response batch). The pack is
plain JSONL of ``DecisionFixture.to_payload()`` records, suitable for CI
micro-evals and model-swap drift reports.

Usage:
    python tools/harvest_decision_corpus.py logs/ag-natural-*.jsonl \
        -o tests/fixtures/decision_corpus/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.brain.metacognition import fixtures_from_trace  # noqa: E402


def _iter_events(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def harvest(trace_path: Path, output_dir: Path) -> tuple[int, Path | None]:
    source_run = trace_path.stem
    fixtures = fixtures_from_trace(_iter_events(trace_path), source_run=source_run)
    if not fixtures:
        return 0, None
    output_dir.mkdir(parents=True, exist_ok=True)
    pack_path = output_dir / f"{source_run}.jsonl"
    with pack_path.open("w", encoding="utf-8") as handle:
        for fixture in fixtures:
            handle.write(
                json.dumps(fixture.to_payload(), ensure_ascii=True, sort_keys=True, default=str)
                + "\n"
            )
    return len(fixtures), pack_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", nargs="+", type=Path, help="RuntimeTrace JSONL files")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("tests/fixtures/decision_corpus"),
        help="directory for harvested fixture packs (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    total = 0
    for trace_path in args.traces:
        if not trace_path.is_file():
            print(f"skip (not a file): {trace_path}")
            continue
        count, pack_path = harvest(trace_path, args.output_dir)
        total += count
        if pack_path is None:
            print(f"{trace_path.name}: 0 fixtures (no settled epochs) — no pack written")
        else:
            size_kb = pack_path.stat().st_size / 1024
            print(f"{trace_path.name}: {count} fixtures -> {pack_path} ({size_kb:.0f} KiB)")
    print(f"total fixtures: {total}")
    return 0 if total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
