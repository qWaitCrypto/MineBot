#!/usr/bin/env python3
"""Replay decision-corpus packs against a configured provider; print drift.

brain-cognitive-framework.md §10.1 (F7). Usage:

    MINEBOT_LLM_* env configured (same variables as minebot-local), then:
    python tools/replay_decision_corpus.py tests/fixtures/decision_corpus/*.jsonl \
        --model primary --limit 20 -o drift-report.json

Compares each fixture's recorded tool batch against what the configured
model chooses on the same recorded context, aggregated per decision
context. Use it to compare candidate providers/models on the same packs
before spending any live run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.config import provider_registry_from_env  # noqa: E402
from minebot.app.decision_replay import ReplayEngine  # noqa: E402
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry  # noqa: E402
from minebot.brain.metacognition import DecisionFixture, drift_report  # noqa: E402
from minebot.contract import Region  # noqa: E402


def _load_fixtures(paths: list[Path], *, limit: int | None, contexts: set[str] | None):
    fixtures: list[DecisionFixture] = []
    for path in paths:
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            fixture = DecisionFixture.from_payload(json.loads(line))
            if contexts and fixture.decision_context not in contexts:
                continue
            fixtures.append(fixture)
            count += 1
            if limit is not None and count >= limit:
                break
    return fixtures


def _current_registry():
    body = Mock()
    body.bot_name = "ReplayBot"
    return build_phase1_registry(
        body,
        Phase1RuntimeConfig(natural_region=Region("replay", (-64, -64, -64), (64, 320, 64))),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packs", nargs="+", type=Path)
    parser.add_argument("--model", default="primary", help="logical provider name")
    parser.add_argument("--limit", type=int, default=None, help="max fixtures per pack")
    parser.add_argument(
        "--contexts",
        default=None,
        help="comma-separated decision contexts to include (default: all)",
    )
    parser.add_argument("-o", "--output", type=Path, default=None, help="write full JSON report")
    args = parser.parse_args(argv)

    try:
        provider = provider_registry_from_env()
    except Exception as exc:
        print(f"provider configuration unavailable: {exc}")
        print("configure MINEBOT_LLM_* (same variables as minebot-local) and retry")
        return 2

    contexts = (
        {item.strip() for item in args.contexts.split(",") if item.strip()}
        if args.contexts
        else None
    )
    fixtures = _load_fixtures(list(args.packs), limit=args.limit, contexts=contexts)
    if not fixtures:
        print("no fixtures selected")
        return 1
    print(f"replaying {len(fixtures)} fixtures against logical model {args.model!r} ...")

    engine = ReplayEngine(
        registry=_current_registry(),
        model_provider=provider,
        logical_model=args.model,
    )
    replays = asyncio.run(engine.replay_all(fixtures))
    report = drift_report(fixtures, replays)

    print(f"total fixtures: {report.total}")
    for context, bucket in sorted(report.by_context.items()):
        print(
            f"  {context:12s} identical={bucket['identical']:4d} "
            f"same_tools={bucket['same_tools']:4d} divergent={bucket['divergent']:4d} "
            f"unreplayed={bucket['unreplayed']:4d} drift_rate={bucket['drift_rate']}"
        )
    if report.substitutions:
        print("top tool substitutions:")
        for pair, count in report.substitutions[:8]:
            print(f"  {pair}: {count}")
    if args.output is not None:
        args.output.write_text(
            json.dumps(report.to_payload(), ensure_ascii=True, sort_keys=True, indent=1),
            encoding="utf-8",
        )
        print(f"full report -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
