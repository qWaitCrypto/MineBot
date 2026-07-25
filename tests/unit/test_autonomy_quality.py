import json
import unittest
from dataclasses import asdict
from pathlib import Path

from minebot.app.autonomy_quality import (
    AG_FP30_YARDSTICK,
    AG_FP30_X_YARDSTICK,
    AUTONOMY_QUALITY_SCHEMA_VERSION,
    DEFAULT_THRESHOLDS,
    evaluate_autonomy_quality,
)


ROOT = Path(__file__).resolve().parents[2]
THRESHOLD_FREEZE = ROOT / "tests" / "fixtures" / "autonomy_quality_thresholds.json"


def _body_state(seq, ts, *, x=0.0, counts=None, selected_item=None, offhand_item=None, owner=None, pending=0):
    return {
        "event": "body_state",
        "seq": seq,
        "ts": float(ts),
        "bot": "Bot",
        "pos": [float(x), 64.0, 0.0],
        "health": 20.0,
        "food": 20,
        "oxygen": 300,
        "inventory_hash": f"inv-{seq}",
        "inventory_counts": dict(counts or {}),
        "selected_item": selected_item,
        "offhand_item": offhand_item,
        "body_owner": owner,
        "pending_action_count": pending,
        "dimension": "overworld",
        "complete": True,
        "missing": False,
    }


def _body_events(seq, ts, events=None):
    payloads = list(events or [])
    return {
        "event": "body_events",
        "seq": seq,
        "ts": float(ts),
        "count": len(payloads),
        "names": [event["name"] for event in payloads],
        "seqs": [event["seq"] for event in payloads],
        "events": payloads,
    }


def _invoke(seq, ts, tool, args_hash, tactic, *, mutating=True):
    return {
        "event": "tool_invoke",
        "seq": seq,
        "ts": float(ts),
        "tool": tool,
        "args_hash": args_hash,
        "tactic_signature": tactic,
        "mutating": mutating,
    }


def _result(seq, ts, tool, args_hash, tactic, *, success, reason):
    return {
        "event": "tool_result",
        "seq": seq,
        "ts": float(ts),
        "tool": tool,
        "args_hash": args_hash,
        "tactic_signature": tactic,
        "success": success,
        "reason": reason,
    }


def _coverage_events(*, states):
    events = []
    for index, state in enumerate(states):
        events.append(state)
        events.append(_body_events(10_000 + index, state["ts"]))
        if int(state["ts"]) % 240 == 0:
            events.append({"event": "llm_start", "seq": 20_000 + index, "ts": state["ts"] + 1.0})
    events.append({"event": "session_terminal", "seq": 30_000, "ts": 1800.0})
    return events


def _healthy_material_incomplete_trace(include_obstacle=True):
    counts_by_ts = {
        0: {},
        120: {"oak_log": 1},
        240: {"oak_log": 1},
        360: {"oak_log": 1},
        480: {"oak_log": 2},
        600: {"oak_log": 2},
        720: {"oak_log": 2},
        840: {"oak_log": 2},
        960: {"oak_log": 3},
        1080: {"oak_log": 3},
        1200: {"oak_log": 3},
        1320: {"oak_log": 3, "crafting_table": 1},
        1440: {"oak_log": 3, "crafting_table": 1},
        1560: {"oak_log": 3, "crafting_table": 1},
        1680: {"oak_log": 3, "crafting_table": 1},
        1800: {"oak_log": 3, "crafting_table": 1},
    }
    states = [
        _body_state(index + 1, ts, x=index * 2.0, counts=counts)
        for index, (ts, counts) in enumerate(counts_by_ts.items())
    ]
    events = _coverage_events(states=states)
    if include_obstacle:
        events.extend(
            [
                _invoke(100, 250, "explore_for", "args-a", "explore:targets", mutating=True),
                _result(
                    101,
                    300,
                    "explore_for",
                    "args-a",
                    "explore:targets",
                    success=False,
                    reason="mobility_blocked:no_path",
                ),
                _invoke(102, 360, "collect_resource", "args-b", "collect:logs", mutating=True),
                _result(103, 430, "collect_resource", "args-b", "collect:logs", success=True, reason="collected"),
            ]
        )
    return sorted(events, key=lambda event: (event["ts"], event["seq"]))


class AutonomyQualityTests(unittest.TestCase):
    def test_default_thresholds_match_q0_freeze_fixture(self):
        freeze = json.loads(THRESHOLD_FREEZE.read_text(encoding="utf-8"))

        self.assertEqual(freeze["status"], "frozen")
        self.assertEqual(freeze["evaluator_schema_version"], AUTONOMY_QUALITY_SCHEMA_VERSION)
        self.assertEqual(freeze["source_symbol"], "minebot.app.autonomy_quality.DEFAULT_THRESHOLDS")
        self.assertEqual(len(freeze["thresholds"]), 9)
        self.assertEqual(freeze["thresholds"], asdict(DEFAULT_THRESHOLDS))

    def test_report_binds_authoritative_progress_ledger_schema(self):
        report = evaluate_autonomy_quality(
            _healthy_material_incomplete_trace(),
            yardstick=AG_FP30_X_YARDSTICK,
            active_window_s=1800,
        )

        self.assertEqual(report["schema_version"], AUTONOMY_QUALITY_SCHEMA_VERSION)
        self.assertEqual(report["schema_version"], "autonomy-quality-v3")

    def test_material_incomplete_but_healthy_output_can_pass(self):
        report = evaluate_autonomy_quality(
            _healthy_material_incomplete_trace(),
            yardstick=AG_FP30_X_YARDSTICK,
            active_window_s=1800,
        )

        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["signals"]["effective_output"]["points"], 4)
        self.assertEqual(report["signals"]["recovery"]["verdict"], "pass")

    def test_recovery_attempt_limit_ignores_read_only_observation(self):
        events = _healthy_material_incomplete_trace()
        # Recovery commonly re-reads state, inventory, and nearby facts before
        # selecting a different physical tactic.  Those observations must not
        # consume the semantic physical-attempt budget A.
        for index in range(6):
            ts = 305 + index * 5
            events.extend(
                [
                    _invoke(
                        10_000 + index * 2,
                        ts,
                        "read_state",
                        f"read-{index}",
                        "read_state:refresh",
                        mutating=False,
                    ),
                    _result(
                        10_001 + index * 2,
                        ts + 1,
                        "read_state",
                        f"read-{index}",
                        "read_state:refresh",
                        success=True,
                        reason="state_read",
                    ),
                ]
            )

        report = evaluate_autonomy_quality(
            sorted(events, key=lambda event: (event["ts"], event["seq"])),
            yardstick=AG_FP30_X_YARDSTICK,
            active_window_s=1800,
        )

        self.assertEqual(report["signals"]["recovery"]["verdict"], "pass")
        episode = report["signals"]["recovery"]["episodes"][0]
        self.assertEqual(episode["attempts"], 1)
        self.assertEqual(len(episode["attempt_refs"]), 1)

    def test_ag_fp30_counts_prerequisite_chain_as_effective_output(self):
        early = {"oak_log": 2, "oak_planks": 5, "stick": 4}
        tools = {**early, "crafting_table": 1, "wooden_pickaxe": 1, "cobblestone": 4}
        later = {**tools, "coal": 1, "raw_iron": 2}
        counts_by_ts = {
            0: {},
            120: {"oak_log": 2},
            240: early,
            360: {**early, "crafting_table": 1},
            480: {**early, "crafting_table": 1, "wooden_pickaxe": 1},
            600: tools,
            720: tools,
            840: tools,
            960: tools,
            1080: tools,
            1200: tools,
            1320: later,
            1440: later,
            1560: later,
            1680: later,
            1800: later,
        }
        states = [
            _body_state(index + 1, ts, x=index * 2.0, counts=counts)
            for index, (ts, counts) in enumerate(counts_by_ts.items())
        ]
        events = _coverage_events(states=states)
        events.extend(
            [
                _invoke(100, 250, "explore_for", "args-a", "explore:targets", mutating=True),
                _result(101, 300, "explore_for", "args-a", "explore:targets", success=False, reason="no_path"),
                _invoke(102, 360, "mine_block_collect", "args-b", "mine:stone", mutating=True),
                _result(103, 430, "mine_block_collect", "args-b", "mine:stone", success=True, reason="collected"),
            ]
        )

        report = evaluate_autonomy_quality(sorted(events, key=lambda event: (event["ts"], event["seq"])), active_window_s=1800)

        output = report["signals"]["effective_output"]
        self.assertEqual(report["verdict"], "pass")
        self.assertGreaterEqual(output["points"], output["required_points"])
        self.assertGreater(output["yardstick"]["families"]["logs"]["points"], 0)
        self.assertGreater(output["yardstick"]["families"]["cobblestone"]["points"], 0)
        self.assertGreater(output["yardstick"]["families"]["coal_or_charcoal"]["points"], 0)
        self.assertFalse(output["yardstick"]["families"]["flowers"]["observed"])

    def test_authoritative_body_events_count_when_state_sample_lags(self):
        states = [_body_state(index + 1, ts, x=index * 2.0, counts={}) for index, ts in enumerate(range(0, 1801, 120))]
        events = _coverage_events(states=states)
        sample = next(event for event in events if event.get("event") == "body_events" and event.get("ts") == 120.0)
        sample["events"] = [
            {
                "seq": 77,
                "name": "itemPickup",
                "data": {"player": "Bot", "item": "minecraft:oak_log", "count": 3},
            }
        ]
        sample["count"] = 1
        sample["names"] = ["itemPickup"]
        sample["seqs"] = [77]
        events.extend(
            [
                _invoke(100, 250, "explore_for", "args-a", "explore:targets", mutating=True),
                _result(101, 300, "explore_for", "args-a", "explore:targets", success=False, reason="no_path"),
                _invoke(102, 360, "craft_item", "args-b", "craft:table", mutating=True),
                _result(103, 430, "craft_item", "args-b", "craft:table", success=True, reason="crafted"),
            ]
        )

        report = evaluate_autonomy_quality(events, active_window_s=1800)

        output = report["signals"]["effective_output"]
        self.assertGreaterEqual(output["points"], 3)
        self.assertEqual(output["authoritative_progress_events"], 1)
        self.assertIn("seq:77", output["authoritative_progress_refs"])

    def test_initial_inventory_and_equipment_are_not_run_output(self):
        events = _healthy_material_incomplete_trace()
        for event in events:
            if event.get("event") == "body_state":
                event["inventory_counts"] = {"minecraft:oak_log": 3}
                event["selected_item"] = "minecraft:wooden_pickaxe"
                event["offhand_item"] = "minecraft:shield"

        report = evaluate_autonomy_quality(
            events,
            yardstick=AG_FP30_X_YARDSTICK,
            active_window_s=1800,
        )

        output = report["signals"]["effective_output"]
        self.assertEqual(output["points"], 0)
        self.assertEqual(output["yardstick"]["families"]["logs"]["points"], 0)
        self.assertFalse(output["yardstick"]["equipment"]["stone_pickaxe_equipped"]["satisfied"])

    def test_position_is_required_for_fail_closed_process_health(self):
        events = _healthy_material_incomplete_trace()
        for event in events:
            if event.get("event") == "body_state":
                event.pop("pos", None)

        report = evaluate_autonomy_quality(
            events,
            yardstick=AG_FP30_X_YARDSTICK,
            active_window_s=1800,
        )

        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertIn("body_state.pos", report["coverage"]["missing"])

    def test_terminal_event_is_required_for_evidence(self):
        events = [event for event in _healthy_material_incomplete_trace() if event.get("event") != "session_terminal"]

        report = evaluate_autonomy_quality(
            events,
            yardstick=AG_FP30_X_YARDSTICK,
            active_window_s=1800,
        )

        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertIn("session_terminal", report["coverage"]["missing"])

    def test_terminal_owner_or_pending_action_is_a_hard_failure(self):
        events = _healthy_material_incomplete_trace()
        final_state = next(
            event
            for event in reversed(events)
            if event.get("event") == "body_state"
        )
        final_state["body_owner"] = "Bot"
        final_state["pending_action_count"] = 1

        report = evaluate_autonomy_quality(
            events,
            yardstick=AG_FP30_X_YARDSTICK,
            active_window_s=1800,
        )

        cleanup = report["hard_invariants"]["terminal_cleanup"]
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(cleanup["verdict"], "fail")
        self.assertEqual(
            cleanup["failures"],
            ["body_owner_not_released", "pending_actions_not_empty"],
        )

    def test_session_terminal_truth_takes_precedence_over_late_body_state(self):
        events = _healthy_material_incomplete_trace()
        final_state = next(
            event
            for event in reversed(events)
            if event.get("event") == "body_state"
        )
        final_state["body_owner"] = "Bot"
        final_state["pending_action_count"] = 2
        terminal = next(event for event in events if event.get("event") == "session_terminal")
        terminal["terminal_truth"] = {
            "exit_code": 0,
            "facts": {"body_owner": None, "pending_action_count": 0},
            "goal": "goal",
            "inventory_count": None,
            "lifecycle": "idle",
            "satisfied": False,
            "status": "quit",
            "target": {"goal_id": "AG-FP30", "kind": "production_terminal"},
        }

        report = evaluate_autonomy_quality(
            events,
            yardstick=AG_FP30_X_YARDSTICK,
            active_window_s=1800,
        )

        cleanup = report["hard_invariants"]["terminal_cleanup"]
        self.assertEqual(cleanup["verdict"], "pass")
        self.assertEqual(cleanup["source"], "session_terminal")
        self.assertEqual(cleanup["body_owner"], None)
        self.assertEqual(cleanup["pending_action_count"], 0)

    def test_repeated_body_event_observation_does_not_mint_duplicate_output(self):
        states = [_body_state(index + 1, ts, x=index * 2.0, counts={}) for index, ts in enumerate(range(0, 1801, 120))]
        events = _coverage_events(states=states)
        pickup = {
            "seq": 88,
            "name": "itemPickup",
            "data": {"player": "Bot", "item": "minecraft:oak_log", "count": 3},
        }
        for index, sample in enumerate(
            event for event in events if event.get("event") == "body_events" and event.get("ts") in {120.0, 240.0, 360.0}
        ):
            sample["events"] = [pickup]
            sample["count"] = 1
            sample["names"] = ["itemPickup"]
            sample["seqs"] = [88]
        report = evaluate_autonomy_quality(events, active_window_s=1800)

        output = report["signals"]["effective_output"]
        self.assertEqual(output["authoritative_progress_events"], 1)
        self.assertEqual(output["points"], 3)

    def test_outbound_container_or_furnace_transfer_is_not_positive_output(self):
        states = [_body_state(index + 1, ts, x=index * 2.0, counts={}) for index, ts in enumerate(range(0, 1801, 120))]
        events = _coverage_events(states=states)
        sample = next(event for event in events if event.get("event") == "body_events" and event.get("ts") == 120.0)
        sample["events"] = [
            {
                "seq": 89,
                "name": "containerDone",
                "data": {"success": True, "direction": "bot_to_container", "item": "minecraft:oak_log", "count": 3},
            },
            {
                "seq": 90,
                "name": "furnaceDone",
                "data": {"success": True, "direction": "bot_to_furnace", "item": "minecraft:coal", "count": 1},
            },
        ]
        sample["count"] = 2
        sample["names"] = ["containerDone", "furnaceDone"]
        sample["seqs"] = [89, 90]

        report = evaluate_autonomy_quality(events, active_window_s=1800)

        output = report["signals"]["effective_output"]
        self.assertEqual(output["authoritative_progress_events"], 0)
        self.assertEqual(output["points"], 0)

    def test_malformed_authoritative_event_payload_fails_closed(self):
        states = [_body_state(index + 1, ts, x=index * 2.0, counts={}) for index, ts in enumerate(range(0, 1801, 120))]
        events = _coverage_events(states=states)
        sample = next(event for event in events if event.get("event") == "body_events" and event.get("ts") == 120.0)
        sample["events"] = [{"name": "itemPickup", "data": {"item": "oak_log", "count": 3}}]

        report = evaluate_autonomy_quality(events, active_window_s=1800)

        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertIn("body_events.event_payload", report["coverage"]["missing"])

    def test_material_complete_does_not_mask_repeated_failure_loop(self):
        events = _healthy_material_incomplete_trace()
        for state in events:
            if state.get("event") == "body_state":
                state["inventory_counts"] = {
                    "dandelion": 1,
                    "poppy": 1,
                    "blue_orchid": 1,
                    "porkchop": 1,
                    "beef": 1,
                    "mutton": 1,
                    "torch": 16,
                    "iron_ingot": 3,
                }
                state["selected_item"] = "iron_pickaxe"
                state["offhand_item"] = "shield"
        events.extend(
            [
                _result(900, 500, "move_to", "same", "move:point", success=False, reason="no_path"),
                _result(901, 520, "move_to", "same", "move:point", success=False, reason="no_path"),
                _result(902, 540, "move_to", "same", "move:point", success=False, reason="no_path"),
                _result(903, 560, "move_to", "same", "move:point", success=False, reason="no_path"),
            ]
        )

        report = evaluate_autonomy_quality(events, yardstick=AG_FP30_YARDSTICK, active_window_s=1800)

        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["signals"]["process_health"]["repeated_failure"]["streak"], 4)

    def test_read_only_failure_does_not_reset_physical_failure_streak(self):
        events = _healthy_material_incomplete_trace()
        events.extend(
            [
                _invoke(900, 500, "move_to", "same", "move:point", mutating=True),
                _result(901, 510, "move_to", "same", "move:point", success=False, reason="no_path"),
                _invoke(902, 520, "read_state", "read", "read_state:refresh", mutating=False),
                {
                    "event": "tool_result",
                    "seq": 903,
                    "ts": 530.0,
                    "tool": "read_state",
                    "tool_call_id": "read-call",
                    "args_hash": "read",
                    "tactic_signature": "read_state:refresh",
                    "success": False,
                    "reason": "state_unavailable",
                    "mutating": False,
                },
                _invoke(904, 540, "move_to", "same", "move:point", mutating=True),
                _result(905, 550, "move_to", "same", "move:point", success=False, reason="no_path"),
                _invoke(906, 560, "move_to", "same", "move:point", mutating=True),
                _result(907, 570, "move_to", "same", "move:point", success=False, reason="no_path"),
                _invoke(908, 580, "move_to", "same", "move:point", mutating=True),
                _result(909, 590, "move_to", "same", "move:point", success=False, reason="no_path"),
            ]
        )

        report = evaluate_autonomy_quality(
            sorted(events, key=lambda event: (event["ts"], event["seq"])),
            yardstick=AG_FP30_X_YARDSTICK,
            active_window_s=1800,
        )

        repeated = report["signals"]["process_health"]["repeated_failure"]
        self.assertEqual(report["verdict"], "fail")
        self.assertIsNotNone(repeated)
        self.assertEqual(repeated["streak"], 4)

    def test_zero_output_is_failure_not_honest_success(self):
        states = [_body_state(index + 1, ts, x=index * 3.0) for index, ts in enumerate(range(0, 1801, 120))]
        events = _coverage_events(states=states)
        events.extend(
            [
                _invoke(100, 250, "explore_for", "args-a", "explore:targets", mutating=True),
                _result(101, 300, "explore_for", "args-a", "explore:targets", success=False, reason="no_path"),
                _invoke(102, 360, "go_to_surface", "args-b", "surface:up", mutating=True),
                _result(103, 420, "go_to_surface", "args-b", "surface:up", success=True, reason="surface_reached"),
            ]
        )

        report = evaluate_autonomy_quality(events, yardstick=AG_FP30_X_YARDSTICK, active_window_s=1800)

        self.assertEqual(report["verdict"], "fail")
        self.assertIn("output_points_below_threshold", report["signals"]["effective_output"]["failures"])

    def test_no_natural_obstacle_episode_is_insufficient_evidence(self):
        report = evaluate_autonomy_quality(
            _healthy_material_incomplete_trace(include_obstacle=False),
            yardstick=AG_FP30_X_YARDSTICK,
            active_window_s=1800,
        )

        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertEqual(report["signals"]["recovery"]["reason"], "no_natural_obstacle_episode")

    def test_runtime_contract_failure_does_not_count_as_natural_recovery_episode(self):
        events = _healthy_material_incomplete_trace(include_obstacle=False)
        events.extend(
            [
                _invoke(100, 250, "load_skill", "skill-a", "load_skill:water", mutating=True),
                _result(
                    101,
                    300,
                    "load_skill",
                    "skill-a",
                    "load_skill:water",
                    success=False,
                    reason="skill_not_found",
                ),
            ]
        )

        report = evaluate_autonomy_quality(
            sorted(events, key=lambda event: (event["ts"], event["seq"])),
            yardstick=AG_FP30_X_YARDSTICK,
            active_window_s=1800,
        )

        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertEqual(report["signals"]["recovery"]["reason"], "no_natural_obstacle_episode")

    def test_sparse_body_state_trace_is_insufficient_evidence(self):
        states = [
            _body_state(1, 0, x=0, counts={}),
            _body_state(2, 600, x=10, counts={"oak_log": 3, "crafting_table": 1}),
            _body_state(3, 1800, x=20, counts={"oak_log": 3, "crafting_table": 1}),
        ]
        events = _coverage_events(states=states)
        events.extend(
            [
                _result(100, 300, "explore_for", "args-a", "explore:targets", success=False, reason="no_path"),
                _invoke(101, 360, "collect_resource", "args-b", "collect:logs", mutating=True),
            ]
        )

        report = evaluate_autonomy_quality(events, yardstick=AG_FP30_X_YARDSTICK, active_window_s=1800)

        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertIn("body_state_sample_gap", report["coverage"]["missing"])

    def test_legacy_trace_shape_without_required_fields_is_insufficient(self):
        events = [
            {
                "event": "body_state",
                "seq": 1,
                "ts": 0.0,
                "pos": [0, 64, 0],
                "inventory_hash": "old",
                "missing": False,
            },
            {
                "event": "body_state",
                "seq": 2,
                "ts": 120.0,
                "pos": [1, 64, 0],
                "inventory_hash": "old2",
                "missing": False,
            },
            {"event": "body_events", "seq": 3, "ts": 0.0, "count": 0, "names": [], "seqs": []},
            {"event": "tool_invoke", "seq": 4, "ts": 10.0, "tool": "move_to"},
            {"event": "tool_result", "seq": 5, "ts": 20.0, "tool": "move_to", "success": False, "reason": "no_path"},
            {"event": "session_terminal", "seq": 6, "ts": 1800.0},
        ]

        report = evaluate_autonomy_quality(events, active_window_s=1800)

        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertIn("body_events.payload", report["coverage"]["missing"])
        self.assertIn("tool_invoke.args_hash", report["coverage"]["missing"])

    def test_060009_style_choke_stall_is_negative_sample(self):
        states = [_body_state(index + 1, ts, x=0.1, counts={}) for index, ts in enumerate(range(0, 1801, 120))]
        events = _coverage_events(states=states)
        events.extend(
            [
                _invoke(100, 240, "explore_for", "same", "explore:primary", mutating=True),
                _result(101, 300, "explore_for", "same", "explore:primary", success=False, reason="mobility_blocked:no_path"),
                _invoke(102, 420, "explore_for", "same", "explore:primary", mutating=True),
                _result(103, 480, "explore_for", "same", "explore:primary", success=False, reason="mobility_blocked:no_path"),
                _invoke(104, 600, "explore_for", "same", "explore:primary", mutating=True),
                _result(105, 660, "explore_for", "same", "explore:primary", success=False, reason="mobility_blocked:no_path"),
                _invoke(106, 780, "explore_for", "same", "explore:primary", mutating=True),
                _result(107, 840, "explore_for", "same", "explore:primary", success=False, reason="mobility_blocked:no_path"),
            ]
        )

        report = evaluate_autonomy_quality(events, yardstick=AG_FP30_YARDSTICK, active_window_s=1800)

        self.assertEqual(report["verdict"], "fail")
        self.assertIn("deadlock_window", report["signals"]["process_health"]["failures"])
        self.assertEqual(report["signals"]["effective_output"]["points"], 0)


if __name__ == "__main__":
    unittest.main()
