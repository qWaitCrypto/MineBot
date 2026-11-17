"""Replay the semantic supervision frames through the real question builder.

Pass ``--live`` to send the generated questions to the configured Decision
endpoint.  The default mode is offline and only writes the generated prompts.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace

from minebot.app.decision_client import DecisionClient, DecisionPriority
from minebot.app.direct_call_supervision import CallSupervisor


FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "reflex_r2_semantic_replay.json"


def build_question(supervisor: CallSupervisor, row: dict[str, object]) -> dict[str, object]:
    intent = str(row["intent"])
    frame = row["frame"]
    assert isinstance(frame, dict)
    return supervisor._question(
        str(row["id"]),
        SimpleNamespace(
            name=str(row["tool"]),
            supervision_explanation=(
                "The Body approaches the relevant context, performs the requested operation, "
                "and verifies the terminal result."
            ),
        ),
        dict(row["params"]),
        {
            "operation_id": str(row["id"]), "tool": str(row["tool"]), "state": "running",
            "phase": frame["phase"], "elapsed_s": frame["elapsed_s"], "fact_revision": 1,
            "facts": frame["facts"],
        },
        idle_s=0.0, purpose="replay",
        supervision_context={
            "goal": intent, "current_step": intent, "node_description": intent,
            "intent": intent, "is_plan": False,
        },
        options={"continue": "", "step_done": "", "call_agent": ""},
    )


async def main(live: bool, all_option_orders: bool) -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    supervisor = CallSupervisor(SimpleNamespace(
        agent_context=None,
        native_task_workspace=None,
        decision_client=None,
        trace=SimpleNamespace(emit=lambda *args, **kwargs: None),
    ))
    client = DecisionClient(timeout_s=2.0) if live else None
    try:
        for row in rows:
            payload = build_question(supervisor, row)
            if client is None:
                print(json.dumps({
                    "id": row["id"], "request_chars": payload["state"]["request_chars"],
                    "option_orders": 6 if all_option_orders else 1,
                }, ensure_ascii=False))
                continue
            orders = list(permutations(payload["questions"]["q"]["criteria"])) if all_option_orders else [
                tuple(payload["questions"]["q"]["criteria"])
            ]
            answers = []
            for order in orders:
                request = copy.deepcopy(payload)
                criteria = request["questions"]["q"]["criteria"]
                request["questions"]["q"]["criteria"] = {key: criteria[key] for key in order}
                answer = await client.ask(request, priority=DecisionPriority.SUPERVISION)
                answers.append({"order": list(order), "choice": answer.choice,
                                "elapsed_ms": round(answer.elapsed_s * 1000, 1)})
            choices = {item["choice"] for item in answers}
            print(json.dumps({
                "id": row["id"], "answers": answers,
                "consistent": len(choices) <= 1,
                "expected": row["expected"],
                "request_chars": payload["state"]["request_chars"],
            }, ensure_ascii=False))
    finally:
        if client is not None:
            await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="send prompts to the configured Decision endpoint")
    parser.add_argument("--all-option-orders", action="store_true",
                        help="ask every permutation of the three runtime options and report consistency")
    args = parser.parse_args()
    asyncio.run(main(args.live, args.all_option_orders))
