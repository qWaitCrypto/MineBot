#!/usr/bin/env python3
"""Replay a set with the production DecisionClient transport and serialization.

    python tools/decision_replay/replay.py SET.jsonl --endpoint URL --out RESULT.jsonl

Successful records are flushed immediately and skipped on resume. Failed or
incomplete answers are retried on the next invocation. Only a truncated final
JSONL line is discarded. Changing the set, endpoint, model, or timeout requires
a new result file. No credentials are loaded or sent.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from minebot.app.decision_client import DecisionClient
from tools.decision_replay.common import (
    FORMAT, LIVE_TIMEOUT_S, complete_answers, digest, dumps, read_jsonl,
)


def resume_results(path: Path, expected: dict, records: list[dict]) -> set[str]:
    if not path.exists() or path.stat().st_size == 0:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumps(expected) + "\n", encoding="utf-8")
        return set()
    valid = {record["id"]: record for record in records}
    completed = set()
    # Repair only an interrupted append, not corrupt earlier lines. Binary
    # offsets also handle interruption in the middle of a UTF-8 character.
    with path.open("rb+") as stream:
        header = json.loads(stream.readline())
        if header != expected:
            raise ValueError("Result configuration differs; choose a new --out file")
        while True:
            start = stream.tell()
            line = stream.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                # The writer always commits a row with its newline. Even a
                # parseable final fragment is uncommitted and may be retried.
                stream.truncate(start)
                break
            record = json.loads(line)
            original = valid.get(record["id"])
            if original is None or digest(record["payload"]) != digest(original["payload"]):
                raise ValueError("Result contains an unknown or changed set record")
            if record.get("error") is None and complete_answers(record["payload"], record.get("replayed")):
                completed.add(record["id"])
    return completed


async def replay_set(
    set_path: Path, out: Path, *, endpoint: str, model: str | None = None,
    concurrency: int = 2, timeout_s: float = LIVE_TIMEOUT_S,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """transport injects an offline stub through the same live client path."""
    if concurrency < 1 or timeout_s <= 0:
        raise ValueError("concurrency and timeout must be positive")
    if set_path.resolve() == out.resolve():
        raise ValueError("Set and result paths must differ")
    header, records = read_jsonl(set_path)
    if header.get("kind") != "set":
        raise ValueError("Expected a harvested set")
    if header.get("records") != len(records):
        raise ValueError("Set is incomplete")
    config = {"type": "header", "format": FORMAT, "kind": "result",
              "set_sha256": digest([header, records]), "records": len(records),
              "endpoint": endpoint, "model_override": model, "timeout_s": timeout_s,
              "transport": "DecisionClient.ask -> httpx.AsyncClient.post(json=payload), trust_env=False"}
    completed = resume_results(out, config, records)
    todo = iter(record for record in records if record["id"] not in completed)
    counts = Counter(skipped=len(completed))
    async with httpx.AsyncClient(trust_env=False, timeout=timeout_s, transport=transport) as http:
        with out.open("a", encoding="utf-8") as stream:
            async def worker() -> None:
                client = DecisionClient(url=endpoint, timeout_s=timeout_s, client=http)
                try:
                    for record in todo:
                        payload = dict(record["payload"])
                        if model is not None:
                            payload["model"] = model
                        answer = await client.ask(payload)
                        ok = complete_answers(payload, answer.raw)
                        result = {**record, "type": "result", "replayed": answer.raw,
                                  "replay_ms": answer.elapsed_s * 1000,
                                  "sent_payload_sha256": digest(payload),
                                  "error": None if ok else "no_complete_answer"}
                        # There is no await between write and flush, so workers
                        # cannot interleave lines. Do not keep unfinished work
                        # in a batch that would be lost on Ctrl-C.
                        stream.write(dumps(result) + "\n")
                        stream.flush()
                        counts["ok" if ok else "failed"] += 1
                        total = counts["ok"] + counts["failed"] + counts["skipped"]
                        if total % 100 == 0:
                            print(f"{total}/{len(records)}; failed this pass: {counts['failed']}", file=sys.stderr, flush=True)
                finally:
                    await client.close()

            workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
            try:
                await asyncio.gather(*workers)
            finally:
                for task in workers:
                    task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
    return dict(counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("set", type=Path)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", help="Explicit model override; omit for the unchanged live payload")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=LIVE_TIMEOUT_S, help="Live client default: 2 seconds")
    args = parser.parse_args(argv)
    counts = asyncio.run(replay_set(args.set, args.out, endpoint=args.endpoint, model=args.model,
                                   concurrency=args.concurrency, timeout_s=args.timeout))
    print(dumps(counts))
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
