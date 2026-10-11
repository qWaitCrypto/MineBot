"""Ask the real Decision service about the semantic replay frames.

Each frame is sent with six permutations of three relevant concrete options;
the report exposes whether reordering changes the answer.  No Body or game
process is started.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import time
from pathlib import Path
from types import SimpleNamespace

from minebot.app.decision_client import DecisionClient, DecisionPriority
from minebot.app.direct_call_supervision import CallSupervisor


def _request(supervisor: CallSupervisor, row: dict, options: dict[str, str]) -> dict:
    tool = SimpleNamespace(name=row["tool"], supervision_explanation=(
        "The Body approaches the work context, performs the requested operation, and verifies the terminal result."
    ))
    frame = row["frame"]
    return supervisor._question(
        row["id"], tool, row["params"], {
            "operation_id": row["id"], "tool": row["tool"], "state": "running",
            "phase": frame.get("phase"), "elapsed_s": frame.get("elapsed_s"),
            "fact_revision": 1, "facts": frame.get("facts", {}),
        }, idle_s=0.0, purpose="replay", options=options,
        supervision_context={"goal": row["intent"], "current_step": row["intent"],
                             "node_description": row["intent"], "intent": row["intent"],
                             "is_plan": False},
    )


async def main(path: Path, output: Path, url: str, only: set[str] | None = None) -> None:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if only:
        rows = [row for row in rows if row["id"] in only]
    runtime = SimpleNamespace(
        agent_context=None, native_task_workspace=None, native_plan=None,
        trace=SimpleNamespace(emit=lambda *args, **kwargs: None), decision_client=None,
    )
    supervisor = CallSupervisor(runtime, decision_url=url)
    results = []
    try:
        client = supervisor._client
        for row in rows:
            expected = row["expected"]
            pool = [expected, "continue", "call_agent"]
            if expected == "continue":
                pool = ["continue", "step_done", "call_agent"]
            elif expected == "step_done":
                pool = ["step_done", "continue", "call_agent"]
            else:
                pool = [expected, "continue", "call_agent"]
            descriptions = supervisor._criteria({}, {
                key: supervisor._criteria({}).get(key, f"Choose {key}.") for key in pool
            })
            answers = []
            for order in itertools.permutations(pool):
                options = {key: descriptions[key] for key in order}
                started = time.monotonic()
                answer = await client.ask(
                    _request(supervisor, row, options), priority=DecisionPriority.SUPERVISION
                )
                answers.append({"order": list(order), "choice": answer.choice,
                                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                                "correct": answer.choice == expected})
            results.append({"id": row["id"], "expected": expected, "answers": answers,
                            "consistent": len({item["choice"] for item in answers}) == 1,
                            "correct_count": sum(item["correct"] for item in answers)})
    finally:
        await supervisor.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"url": url, "frames": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = sum(len(item["answers"]) for item in results)
    correct = sum(item["correct_count"] for item in results)
    inconsistent = [item["id"] for item in results if not item["consistent"]]
    print(json.dumps({"frames": len(results), "requests": total, "correct": correct,
                      "inconsistent_frames": inconsistent, "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default="tests/fixtures/reflex_r2_semantic_replay.json")
    parser.add_argument("--output", default="logs/reflex-options-replay.json")
    parser.add_argument("--url", default="http://117.176.14.165:10004/v1/systemone")
    parser.add_argument("--only", nargs="*", default=[])
    args = parser.parse_args()
    asyncio.run(main(Path(args.fixture), Path(args.output), args.url, set(args.only) or None))
