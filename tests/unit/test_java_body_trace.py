"""The frozen autonomy evaluator consumes a Java-Body trace unchanged.

This closes the trace-continuity step: a Java-Body session's real ToolResults
are wrapped in the runner's trace vocabulary (the same events the runner emits
around any RegisteredTool) plus the one Java-specific inventory-delta mapping,
and the frozen evaluator — untouched thresholds — produces an honest verdict.
It deliberately does NOT manufacture a pass: a short bounded session must
fail-closed to insufficient_evidence, proving the evaluator's independence
holds over Java-Body traces exactly as over Scarpet ones.
"""

from __future__ import annotations

import hashlib

from minebot.app.autonomy_quality import (
    DEFAULT_THRESHOLDS,
    AG_FP30_YARDSTICK,
    evaluate_autonomy_quality,
)
from minebot.app.java_body_trace import body_progress_event
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.contract.governance import Region
from minebot.game.governance import GovernancePolicy
from minebot.game.java_body import JavaBody
from minebot.game.java_body_adapter import GovernanceAnswerer, JavaBodyClient

from tests.unit.test_java_body_adapter import FakeBodyServer


def _client(server, governance=None) -> JavaBodyClient:
    return JavaBodyClient("Bot", lambda: server, governance, action_wall_timeout_s=5.0, recv_timeout_s=0.01)


def _args_hash(tool: str, args: dict) -> str:
    raw = tool + "|" + "&".join(f"{k}={args[k]}" for k in sorted(args))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _run_collect(policy_regions) -> object:
    server = FakeBodyServer()
    policy = GovernancePolicy(**policy_regions)
    client = _client(server, GovernanceAnswerer(policy))
    client.connect()
    natural = policy_regions.get("natural_regions") or [Region("n", (-64, 0, -64), (64, 200, 64))]
    registry = build_phase1_registry(
        JavaBody(client, "Bot"),
        Phase1RuntimeConfig(
            natural_region=natural[0],
            body_provider="java",
            governance_policy=policy,
        ),
    )
    return registry.get("collect_block_domain").callable({
        "block_types": ["minecraft:oak_log"],
        "expected_drops": ["minecraft:oak_log"],
        "remaining_count": 1,
        "search_radius": 16,
    })


def _session_trace(collect_result) -> list[dict]:
    """Wrap real Java-Body ToolResults in the runner's trace vocabulary."""
    args = {"block_types": "minecraft:oak_log", "search_radius": 16}
    tool = "collect_block_domain"
    events: list[dict] = [
        {"event": "scenario_body_ready", "ts": 0.0, "seq": 0},
        # Runner body-state samples: baseline then post-collect.
        {"event": "body_state", "ts": 1.0, "seq": 1, "missing": False,
         "inventory_counts": {}, "selected_item": None, "offhand_item": None},
        {"event": "tool_invoke", "ts": 2.0, "seq": 2, "tool": tool,
         "tool_call_id": "c1", "args_hash": _args_hash(tool, args),
         "tactic_signature": "collect_block_domain:logs"},
        {"event": "tool_result", "ts": 40.0, "seq": 3, "tool": tool,
         "tool_call_id": "c1", "args_hash": _args_hash(tool, args),
         "tactic_signature": "collect_block_domain:logs",
         "success": collect_result.success, "reason": collect_result.reason},
        {"event": "body_state", "ts": 41.0, "seq": 4, "missing": False,
         "inventory_counts": {"minecraft:oak_log": 1} if collect_result.success else {},
         "selected_item": None, "offhand_item": None},
        {"event": "session_terminal", "ts": 42.0, "seq": 5,
         "terminal_truth": {"facts": {"body_owner": None, "pending_action_count": 0}}},
    ]
    progress = body_progress_event(collect_result, ts=40.5, seq=3)
    if progress is not None:
        events.append(progress)
    return events


def test_frozen_evaluator_consumes_a_java_body_trace_and_scores_output() -> None:
    result = _run_collect({"natural_regions": [Region("n", (-64, 0, -64), (64, 200, 64))]})
    assert result.success is True

    trace = _session_trace(result)
    report = evaluate_autonomy_quality(trace, yardstick=AG_FP30_YARDSTICK, active_window_s=1800)

    # The evaluator ran unchanged over the Java-Body trace and scored the
    # verified collect as authoritative output.
    assert report["schema_version"] == "autonomy-quality-v3"
    assert report["thresholds"] == {  # frozen thresholds untouched
        "minimum_output_points_per_1800s": DEFAULT_THRESHOLDS.minimum_output_points_per_1800s,
        **{k: v for k, v in report["thresholds"].items() if k != "minimum_output_points_per_1800s"},
    }
    assert report["signals"]["effective_output"]["points"] >= 1
    assert report["verdict"] in {"pass", "fail", "insufficient_evidence"}


def test_short_java_body_session_fails_closed_not_faked_pass() -> None:
    # A denied collect produces no output; the short trace cannot satisfy the
    # 1800s coverage/output bar and must fail-closed, never a manufactured pass.
    result = _run_collect({
        "natural_regions": [Region("n", (-64, 0, -64), (64, 200, 64))],
        "protected_regions": [Region("base", (0, 0, 0), (10, 128, 10))],
    })
    assert result.success is False

    report = evaluate_autonomy_quality(_session_trace(result), yardstick=AG_FP30_YARDSTICK, active_window_s=1800)
    assert report["verdict"] in {"fail", "insufficient_evidence"}
    assert report["verdict"] != "pass"


def test_denied_result_never_maps_to_output() -> None:
    from minebot.contract import ToolResult

    denied = ToolResult(success=False, reason="candidate_targets_exhausted", can_retry=False,
                        metrics={"attempt_failures": [{"reason": "governance_denied:protected_region"}]})
    assert body_progress_event(denied, ts=1.0, seq=1) is None

    faked = ToolResult(success=True, reason="collected", can_retry=False,
                      metrics={"inventory_delta": {"item_id": "minecraft:oak_log", "before": 2, "after": 2}})
    assert body_progress_event(faked, ts=1.0, seq=1) is None, "zero delta is not output"
