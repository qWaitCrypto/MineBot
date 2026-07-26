"""Lock test for the frozen Java Body performance budgets.

Mirrors the autonomy-quality threshold freeze: the fixture is the contract,
this test is the tamper alarm, and the Java-side BudgetFreezeTest locks the
implementation constants against the same file. A change here without the
full three-place review packet means no budget claim can be trusted.
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURE = Path("tests/fixtures/java_body_budgets.json")


def test_java_body_budgets_match_the_freeze() -> None:
    freeze = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert freeze["status"] == "frozen"
    assert freeze["goal_id"] == "fakeplayer-java-body-baseline-recovery-20260726"
    assert freeze["budgets"] == {
        "find_blocks_p95_ms_radius_32": 100.0,
        "find_blocks_p95_ms_radius_128": 300.0,
        "search_server_cost_ceiling_ms": 40.0,
        "planner_nodes_per_tick": 2000,
        "navigate_default_timeout_ticks": 2400,
        "mutation_verdict_timeout_ticks": 100,
        "mutation_verdict_turnaround_p95_ms": 500.0,
    }
    assert freeze["change_log"], "budget changes carry an explicit change record"
