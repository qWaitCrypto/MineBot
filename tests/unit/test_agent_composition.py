import unittest

from minebot.brain.composition import (
    CompositionBudget,
    CompositionContext,
    collect_resource,
    register_collect_resource_tool,
    register_inventory_tools,
    resource_plan_for,
)
from minebot.brain.modes import ModeRuntime
from minebot.brain.progress import FAILURE_STORM_LIMIT, ProgressAbort, ProgressAuthority
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, WeldContext, execute_tool
from minebot.brain.lifecycle import LifecycleState
from minebot.contract import BodyState, PerceptionResult, ToolResult


def state(inventory_hash="inv"):
    return BodyState(
        bot="Bot",
        pos=(0.5, 59.0, 0.5),
        yaw=None,
        pitch=None,
        health=20.0,
        food=20,
        oxygen=300,
        inventory_raw="[]",
        inventory_hash=inventory_hash,
        effects=None,
        time=1000,
        weather=None,
        dimension="overworld",
        complete=True,
    )


def slot(index, item=None, count=0):
    return {"slot": index, "empty": item is None or count <= 0, "item": item, "count": count}


class FakeBody:
    bot_name = "Bot"

    def __init__(self):
        self.inventory_counts = {"dirt": 0}
        self.state_reads = 0
        self.inventory_reads = 0

    @property
    def inventory_count(self):
        return self.inventory_counts.get("dirt", 0)

    @inventory_count.setter
    def inventory_count(self, value):
        self.inventory_counts["dirt"] = value

    def get_state(self):
        self.state_reads += 1
        return state(f"inv-{self.inventory_counts}-{self.state_reads}")

    def perceive(self, scope, params):
        if scope != "inventory":
            raise AssertionError(f"unexpected scope {scope}")
        self.inventory_reads += 1
        slots = [
            slot(index, f"minecraft:{item}", count)
            for index, (item, count) in enumerate(sorted(self.inventory_counts.items()))
            if count
        ]
        return PerceptionResult("Bot", "inventory", "perception", True, True, {"slots": slots})


class PagedInventoryBody(FakeBody):
    def perceive(self, scope, params):
        if scope != "inventory":
            raise AssertionError(f"unexpected scope {scope}")
        self.inventory_reads += 1
        start = int(params.get("start") or 0)
        limit = int(params.get("limit") or 12)
        all_slots = [slot(i) for i in range(46)]
        all_slots[13] = slot(13, "minecraft:dirt", 2)
        all_slots[37] = slot(37, "minecraft:dirt", 5)
        end = min(len(all_slots), start + limit)
        data = {"slots": all_slots[start:end]}
        if end < len(all_slots):
            data["nextStart"] = end
        return PerceptionResult("Bot", "inventory", "perception", True, True, data)

    def poll_events(self):
        return []


def search_tool(targets):
    calls = []

    def callable_(params):
        calls.append(dict(params))
        if not targets:
            return ToolResult(False, "search_block_not_found", True, metrics={"block_types": params.get("block_types")})
        target = targets[0]
        block_type = params.get("block_types", ["dirt"])[0]
        return ToolResult(True, "block_in_range", False, metrics={"target": {"pos": target, "type": block_type}})

    return RegisteredTool(
        "search_for_block",
        "search",
        {"type": "object"},
        callable_,
        ToolSidecar("search_for_block", mutating=False, permission="read_world"),
    ), calls


def candidate_search_tool(targets, *, distances=None):
    calls = []

    def callable_(params):
        calls.append(dict(params))
        candidates = [
            {
                "pos": list(target),
                "type": params.get("block_types", ["dirt"])[0],
                "distance": (distances[index] if distances is not None else index),
            }
            for index, target in enumerate(targets)
        ]
        if not candidates:
            return ToolResult(False, "search_block_not_found", True, metrics={"candidates": []})
        return ToolResult(True, "block_in_range", False, metrics={"target": candidates[0], "candidates": candidates})

    return RegisteredTool(
        "search_for_block",
        "search",
        {"type": "object"},
        callable_,
        ToolSidecar("search_for_block", mutating=False, permission="read_world"),
    ), calls


def skip_failing_search_tool(targets, *, reason="search_block_navigation_failed:search_block_no_stand_point"):
    """Search that fails on its own nearest pick (a candidate-skip reason) but
    still returns the full, real candidate list — mirrors the live behavior where
    the nearest block was underground with no stand point."""
    calls = []

    def callable_(params):
        calls.append(dict(params))
        candidates = [
            {"pos": list(target), "type": params.get("block_types", ["dirt"])[0], "distance": index}
            for index, target in enumerate(targets)
        ]
        if not candidates:
            return ToolResult(False, "search_block_not_found", True, metrics={"candidates": []})
        # success=False (its internal approach to candidates[0] failed) but the
        # candidate list is intact and trustworthy.
        return ToolResult(False, reason, True, metrics={"target": candidates[0], "candidates": candidates})

    return RegisteredTool(
        "search_for_block",
        "search",
        {"type": "object"},
        callable_,
        ToolSidecar("search_for_block", mutating=False, permission="read_world"),
    ), calls


def mine_tool(body, *, fail_reason=None):
    calls = []

    def callable_(params):
        calls.append(dict(params))
        if fail_reason is not None:
            return ToolResult(False, fail_reason, True, metrics={"target": params.get("pos")})
        expected = params.get("expected_drops") or ["dirt"]
        item = str(expected[0]).removeprefix("minecraft:")
        body.inventory_counts[item] = body.inventory_counts.get(item, 0) + 1
        return ToolResult(True, "collected", False, metrics={"target": params.get("pos"), "collected_total": 1})

    return RegisteredTool(
        "mine_block_collect",
        "mine",
        {"type": "object"},
        callable_,
        ToolSidecar("mine_block_collect", mutating=True, permission="break", body_scope=("mine",)),
    ), calls


def mine_tool_fail_once(body):
    calls = []

    def callable_(params):
        calls.append(dict(params))
        if len(calls) == 1:
            return ToolResult(False, "mine_failed:no_inventory_delta", True, metrics={"target": params.get("pos")})
        expected = params.get("expected_drops") or ["dirt"]
        item = str(expected[0]).removeprefix("minecraft:")
        body.inventory_counts[item] = body.inventory_counts.get(item, 0) + 1
        return ToolResult(True, "collected", False, metrics={"target": params.get("pos"), "collected_total": 1})

    return RegisteredTool(
        "mine_block_collect",
        "mine",
        {"type": "object"},
        callable_,
        ToolSidecar("mine_block_collect", mutating=True, permission="break", body_scope=("mine",)),
    ), calls


def mine_tool_with_outcomes(body, outcomes):
    calls = []
    planned = list(outcomes)

    def callable_(params):
        calls.append(dict(params))
        outcome = planned.pop(0) if planned else "fail"
        target = params.get("pos")
        if outcome == "success":
            expected = params.get("expected_drops") or ["dirt"]
            item = str(expected[0]).removeprefix("minecraft:")
            body.inventory_counts[item] = body.inventory_counts.get(item, 0) + 1
            return ToolResult(True, "collected", False, metrics={"target": target, "collected_total": 1})
        return ToolResult(False, "collect_no_inventory_delta", True, metrics={"target": target, "collected_total": 0})

    return RegisteredTool(
        "mine_block_collect",
        "mine",
        {"type": "object"},
        callable_,
        ToolSidecar("mine_block_collect", mutating=True, permission="break", body_scope=("mine",)),
    ), calls


def mine_tool_body_rejected(body):
    calls = []

    def callable_(params):
        calls.append(dict(params))
        return ToolResult(False, "body_rejected", True, metrics={"target": params.get("pos"), "error": "owner_busy"})

    return RegisteredTool(
        "mine_block_collect",
        "mine",
        {"type": "object"},
        callable_,
        ToolSidecar("mine_block_collect", mutating=True, permission="break", body_scope=("mine",)),
    ), calls


def mine_tool_approach_body_rejected_then_success(body):
    calls = []

    def callable_(params):
        calls.append(dict(params))
        target = params.get("pos")
        if len(calls) == 1:
            stand_block = [target[0], target[1], target[2] + 1]
            move_target = [stand_block[0] + 0.5, float(stand_block[1]), stand_block[2] + 0.5]
            approach = {"target": target, "stand_block": stand_block, "move_target": move_target}
            rejection = {
                "success": False,
                "reason": "body_rejected",
                "canRetry": True,
                "metrics": {
                    "action": "moveTo",
                    "target": stand_block,
                    "ok": True,
                    "accepted": False,
                    "error": None,
                    "data": {"action": "moveTo"},
                    "mine_approach": approach,
                },
            }
            return ToolResult(
                False,
                "body_rejected",
                True,
                metrics={
                    "target": stand_block,
                    "action": "moveTo",
                    "ok": True,
                    "accepted": False,
                    "error": None,
                    "data": {"action": "moveTo"},
                    "mine_approach": approach,
                    "stand_candidate_failures": [
                        {"stand_block": stand_block, "reason": "body_rejected", "result": rejection}
                    ],
                },
            )
        expected = params.get("expected_drops") or ["dirt"]
        item = str(expected[0]).removeprefix("minecraft:")
        body.inventory_counts[item] = body.inventory_counts.get(item, 0) + 1
        return ToolResult(True, "collected", False, metrics={"target": target, "collected_total": 1})

    return RegisteredTool(
        "mine_block_collect",
        "mine",
        {"type": "object"},
        callable_,
        ToolSidecar("mine_block_collect", mutating=True, permission="break", body_scope=("mine",)),
    ), calls


def mine_tool_clearance_denied(body):
    calls = []

    def callable_(params):
        calls.append(dict(params))
        return ToolResult(
            False,
            "mine_approach_failed:dig_through:break_denied:not_natural_breakable",
            True,
            metrics={
                "target": params.get("pos"),
                "stand_block": [10, 64, 11],
                "clearance": {
                    "success": False,
                    "reason": "break_denied:not_natural_breakable",
                    "canRetry": False,
                    "metrics": {
                        "target": [10, 65, 11],
                        "block_type": "spruce_leaves",
                        "legality": {"allowed": False, "reason": "not_natural_breakable"},
                        "collect_approach_clearance": {
                            "stand_block": [10, 64, 11],
                            "target": params.get("pos"),
                            "cleared": [],
                        },
                    },
                },
            },
        )

    return RegisteredTool(
        "mine_block_collect",
        "mine",
        {"type": "object"},
        callable_,
        ToolSidecar("mine_block_collect", mutating=True, permission="break", body_scope=("mine",)),
    ), calls


def move_tool(*, success=True, reason="arrived"):
    calls = []

    def callable_(params):
        calls.append(dict(params))
        return ToolResult(
            success,
            reason,
            not success,
            metrics={"goal": params.get("pos"), "navigation_goal": {"kind": "near", "pos": params.get("pos"), "radius": params.get("radius")}},
        )

    return RegisteredTool(
        "move_to",
        "move",
        {"type": "object"},
        callable_,
        ToolSidecar("move_to", mutating=True, permission="move"),
    ), calls


def move_tool_with_metrics(*, success=True, reason="arrived", metrics=None):
    calls = []

    def callable_(params):
        calls.append(dict(params))
        payload = {"goal": params.get("pos"), "navigation_goal": {"kind": "near", "pos": params.get("pos"), "radius": params.get("radius")}}
        payload.update(dict(metrics or {}))
        return ToolResult(success, reason, not success, metrics=payload)

    return RegisteredTool(
        "move_to",
        "move",
        {"type": "object"},
        callable_,
        ToolSidecar("move_to", mutating=True, permission="move"),
    ), calls


def mine_tool_that_progress_yields(body):
    calls = []

    def callable_(params):
        calls.append(dict(params))
        raise ProgressAbort(
            "simulated mine yield",
            facts=ProgressAuthority(stagnant_steps=1, stalled_steps=8, failure_steps=5).facts("collect logs"),
        )

    return RegisteredTool(
        "mine_block_collect",
        "mine",
        {"type": "object"},
        callable_,
        ToolSidecar("mine_block_collect", mutating=True, permission="break", body_scope=("mine",)),
    ), calls


def mine_tool_candidate_navigation_yields_then_success(body):
    calls = []

    def callable_(params):
        calls.append(dict(params))
        target = params.get("pos")
        if len(calls) == 1:
            raise ProgressAbort(
                "simulated candidate navigation yield",
                facts=ProgressAuthority(
                    stagnant_steps=0,
                    stalled_steps=8,
                    failure_steps=1,
                    last_action=("navigate.segment", [0, 59, 0], {"kind": "block", "pos": target}, "no_path", target),
                ).facts("collect logs"),
            )
        expected = params.get("expected_drops") or ["dirt"]
        item = str(expected[0]).removeprefix("minecraft:")
        body.inventory_counts[item] = body.inventory_counts.get(item, 0) + 1
        return ToolResult(True, "collected", False, metrics={"target": target, "collected_total": 1})

    return RegisteredTool(
        "mine_block_collect",
        "mine",
        {"type": "object"},
        callable_,
        ToolSidecar("mine_block_collect", mutating=True, permission="break", body_scope=("mine",)),
    ), calls


def composition_context(body, registry, *, max_candidates=4):
    trace_events = []
    return CompositionContext(
        registry=registry,
        weld_context=WeldContext(body=body, authority=ProgressAuthority(), goal_text="collect dirt"),
        runtime_profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
        budget=CompositionBudget(max_candidates=max_candidates, max_mutating_calls=max_candidates, max_wall_s=10),
        trace=lambda event, payload: trace_events.append({"event": event, **payload}),
    ), trace_events


class AgentCompositionTests(unittest.TestCase):
    def test_read_inventory_tool_pages_to_avoid_truncated_payloads(self):
        body = PagedInventoryBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        ctx, _trace_events = composition_context(body, registry)

        result = execute_tool(registry.get("read_inventory"), {}, ctx.weld_context)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["metrics"]["counts"]["dirt"], 7)
        self.assertGreater(body.inventory_reads, 1)

    def test_collect_resource_composes_leaf_tools_through_weld(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool([[1, 59, 0], [2, 59, 0]])
        registry.register(search)
        miner, mine_calls = mine_tool(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry)
        register_collect_resource_tool(registry, ctx)

        result = execute_tool(
            registry.get("collect_resource"),
            {"item": "minecraft:dirt", "count": 2, "constraints": {"max_candidates": 3}},
            ctx.weld_context,
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["reason"], "collected")
        self.assertEqual(result["metrics"]["after_count"], 2)
        self.assertEqual(result["metrics"]["candidates_tried"], 2)
        self.assertEqual([call["pos"] for call in mine_calls], [[1, 59, 0], [2, 59, 0]])
        self.assertEqual(len(mine_calls), 2)
        self.assertIsNotNone(ctx.weld_context.authority.last_action)
        self.assertIn(ctx.weld_context.authority.last_action[0], {"mine_block_collect", "read_inventory"})
        self.assertNotEqual(ctx.weld_context.authority.last_action[0], "collect_resource")
        self.assertFalse(registry.get("collect_resource").sidecar.mutating)
        self.assertIsNone(ctx.weld_context.writer.holder)

    def test_collect_resource_outer_tool_does_not_own_writer_or_progress_key(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = search_tool([[1, 59, 0]])
        registry.register(search)
        miner, _mine_calls = mine_tool(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry)
        register_collect_resource_tool(registry, ctx)

        result = execute_tool(
            registry.get("collect_resource"),
            {"item": "dirt", "count": 1},
            ctx.weld_context,
        )

        self.assertTrue(result["success"], result)
        self.assertFalse(registry.get("collect_resource").sidecar.mutating)
        self.assertIn(ctx.weld_context.authority.last_action[0], {"mine_block_collect", "read_inventory"})
        self.assertNotEqual(ctx.weld_context.authority.last_action[0], "collect_resource")
        self.assertIsNone(ctx.weld_context.writer.holder)

    def test_mutating_leaf_owner_busy_is_honest_retryable_failure(self):
        body = FakeBody()
        registry = ToolRegistry()
        miner, _mine_calls = mine_tool(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry)
        self.assertTrue(ctx.weld_context.writer.try_acquire("other_mutation"))

        result = execute_tool(
            registry.get("mine_block_collect"),
            {"pos": [1, 59, 0], "expected_drops": ["dirt"]},
            ctx.weld_context,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "owner_busy")
        self.assertTrue(result["canRetry"])
        self.assertEqual(result["metrics"]["holder"], "other_mutation")
        ctx.weld_context.writer.release("other_mutation")

    def test_collect_resource_returns_not_found_as_honest_retryable_failure(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = search_tool([])
        registry.register(search)
        miner, _mine_calls = mine_tool(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry)

        result = collect_resource({"item": "dirt", "count": 1}, ctx)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "target_not_found")
        self.assertTrue(result.can_retry)
        self.assertEqual(result.metrics["after_count"], 0)
        self.assertEqual(result.metrics["resume_hint"], "reselect_candidates")

    def test_collect_resource_reports_illegal_leaf_target_without_greenwashing(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = search_tool([[1, 59, 0]])
        registry.register(search)
        miner, _mine_calls = mine_tool(body, fail_reason="break_denied:protected_region")
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry)

        result = collect_resource({"item": "dirt", "count": 1}, ctx)

        # A break_denied candidate is now a skip (try another), not an immediate
        # task failure. With only the one illegal candidate, the collect honestly
        # exhausts and still surfaces the break_denied in skipped/last_failure — no
        # greenwashing into success.
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "candidate_targets_exhausted")
        self.assertTrue(result.can_retry)
        self.assertEqual(result.metrics["skipped"][0]["reason"], "break_denied:protected_region")
        self.assertTrue(result.metrics["skipped"][0]["skip"])

    def test_weld_treats_candidate_skip_as_neutral_not_failure(self):
        # A mutating tool returning a candidate-skip reason must NOT accrue the
        # failure-storm counter, even repeated past the limit.
        body = FakeBody()
        authority = ProgressAuthority()
        weld = WeldContext(body=body, authority=authority, goal_text="collect 1 dirt")
        miner, _ = mine_tool(body, fail_reason="break_denied:protected_region")

        for _ in range(FAILURE_STORM_LIMIT + 2):
            payload = execute_tool(miner, {"pos": [1, 59, 0]}, weld)
            self.assertFalse(payload["success"])

        self.assertEqual(authority.failure_steps, 0)
        authority.require_can_continue("collect 1 dirt")  # does not raise

    def test_weld_counts_repeated_read_only_observation_loops(self):
        # Read-only tools do not acquire the Body writer and do not feed the
        # failure-storm sensor, but repeated identical observations with no world
        # fingerprint change still have to trip stagnation. Otherwise a model can
        # spin forever on tool-only read_inventory calls.
        body = FakeBody()
        body.get_state = lambda: state("stable")
        authority = ProgressAuthority()
        weld = WeldContext(body=body, authority=authority, goal_text="collect 64 logs")

        def callable_(_params):
            return ToolResult(True, "inventory_counted", False, metrics={"counts": {}})

        tool = RegisteredTool(
            "read_inventory",
            "read",
            {"type": "object"},
            callable_,
            ToolSidecar("read_inventory", mutating=False, source="body.perception", permission="read_state"),
        )

        with self.assertRaises(ProgressAbort) as cm:
            for _ in range(10):
                execute_tool(tool, {}, weld)

        self.assertGreaterEqual(cm.exception.facts.stagnant_steps, 3)
        self.assertEqual(cm.exception.facts.failure_steps, 0)

    def test_collect_resource_internal_inventory_reads_do_not_trip_observation_stall(self):
        body = FakeBody()
        body.get_state = lambda: state("stable")
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool([[index, 59, 0] for index in range(8)])
        registry.register(search)
        miner, mine_calls = mine_tool(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=8)

        result = collect_resource({"item": "dirt", "count": 5}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(result.reason, "collected")
        self.assertEqual(result.metrics["after_count"], 5)
        self.assertEqual(len(mine_calls), 5)
        self.assertEqual(ctx.weld_context.authority.stagnant_steps, 0)
        self.assertNotEqual(ctx.weld_context.authority.last_action[0], "read_inventory")

    def test_weld_does_not_count_outer_agent_composition_observation(self):
        # collect_resource is mutating=False by design because its leaf Body
        # calls own progress accounting. The outer composition wrapper must not
        # become a second progress engine just because read-only body tools are
        # now observed for anti-spin protection.
        body = FakeBody()
        body.get_state = lambda: state("stable")
        authority = ProgressAuthority()
        weld = WeldContext(body=body, authority=authority, goal_text="collect 64 logs")

        def callable_(_params):
            return ToolResult(False, "candidate_targets_exhausted", True)

        tool = RegisteredTool(
            "collect_resource",
            "collect",
            {"type": "object"},
            callable_,
            ToolSidecar("collect_resource", mutating=False, source="agent.composition", permission="compose_collect"),
        )

        for _ in range(10):
            execute_tool(tool, {"item": "logs", "count": 64}, weld)

        self.assertEqual(authority.stagnant_steps, 0)
        self.assertEqual(authority.stalled_steps, 0)
        self.assertEqual(authority.failure_steps, 0)

    def test_weld_counts_genuine_failure_toward_storm(self):
        # A non-skip failure (a real error) is still counted and still trips the
        # storm via the weld itself — the skip set must not swallow genuine failures.
        body = FakeBody()
        authority = ProgressAuthority()
        weld = WeldContext(body=body, authority=authority, goal_text="g")
        miner, _ = mine_tool(body, fail_reason="mine_failed:real_error")

        for _ in range(FAILURE_STORM_LIMIT - 1):
            execute_tool(miner, {"pos": [1, 59, 0]}, weld)
        self.assertEqual(authority.failure_steps, FAILURE_STORM_LIMIT - 1)

        with self.assertRaises(ProgressAbort):
            execute_tool(miner, {"pos": [1, 59, 0]}, weld)

    def test_weld_surfaces_inner_progress_yield_without_double_counting_failure(self):
        # Long Body transactions (navigation/combat) feed intermediate progress
        # into the same authority. If they return progress_yielded, the wrapper
        # must surface that yield instead of recording one more failed tool call.
        body = FakeBody()
        authority = ProgressAuthority()
        authority.note_step(
            ("navigate.segment", (0, 59, 0), (64, 64, 64), "stuck"),
            success=False,
            fingerprint=authority.fingerprint(body.get_state()),
        )
        weld = WeldContext(body=body, authority=authority, goal_text="collect 64 logs")

        def callable_(_params):
            return ToolResult(False, "progress_yielded", True, metrics={"error": "inner yield"})

        tool = RegisteredTool(
            "move_to",
            "move",
            {"type": "object"},
            callable_,
            ToolSidecar("move_to", mutating=True, permission="move"),
        )

        before = authority.failure_steps
        with self.assertRaises(ProgressAbort) as cm:
            execute_tool(tool, {"pos": [64, 64, 64]}, weld)

        self.assertEqual(authority.failure_steps, before)
        self.assertEqual(cm.exception.facts.failure_steps, before)

    def test_collect_resource_maps_resource_to_blocks_and_inventory_item(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, search_calls = search_tool([[1, 59, 0]])
        registry.register(search)
        miner, mine_calls = mine_tool(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry)
        register_collect_resource_tool(registry, ctx)

        result = execute_tool(
            registry.get("collect_resource"),
            {"item": "iron", "count": 1, "constraints": {"max_candidates": 1}},
            ctx.weld_context,
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(search_calls[0]["block_types"], ["iron_ore", "deepslate_iron_ore"])
        self.assertEqual(mine_calls[0]["expected_drops"], ["raw_iron"])
        self.assertEqual(result["metrics"]["requested_item"], "iron")
        self.assertEqual(result["metrics"]["item"], "raw_iron")

    def test_collect_resource_maps_dirt_to_dirt_dropping_surface_blocks(self):
        plan = resource_plan_for("dirt")

        self.assertEqual(plan.inventory_items, ("dirt",))
        self.assertEqual(plan.expected_drops, ("dirt",))
        self.assertIn("dirt", plan.block_types)
        self.assertIn("grass_block", plan.block_types)
        self.assertIn("coarse_dirt", plan.block_types)

    def test_collect_resource_caps_search_limit_for_rcon_payload_safety(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, search_calls = search_tool([[1, 59, 0]])
        registry.register(search)
        miner, _mine_calls = mine_tool(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=96)

        result = collect_resource({"item": "logs", "count": 1}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(search_calls[0]["find_limit"], 12)
        self.assertEqual(search_calls[0]["search_radius"], 48)
        self.assertEqual(search_calls[0]["max_pages"], 8)

    def test_collect_resource_caps_requested_log_radius_for_server_tick_safety(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, search_calls = search_tool([[1, 59, 0]])
        registry.register(search)
        miner, _mine_calls = mine_tool(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=96)

        result = collect_resource({"item": "logs", "count": 1, "constraints": {"radius": 96}}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(search_calls[0]["search_radius"], 64)

    def test_collect_resource_uses_small_paged_searches_for_large_candidate_budget(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, search_calls = search_tool([[1, 59, 0]])
        registry.register(search)
        miner, _mine_calls = mine_tool(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=24)

        result = collect_resource({"item": "logs", "count": 1}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(search_calls[0]["find_limit"], 12)
        self.assertEqual(search_calls[0]["max_pages"], 2)

    def test_collect_resource_counts_equivalent_log_inventory_items(self):
        body = FakeBody()
        body.inventory_counts = {"spruce_log": 32, "birch_log": 32}
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = search_tool([[1, 59, 0]])
        registry.register(search)
        miner, mine_calls = mine_tool(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry)

        result = collect_resource({"item": "logs", "count": 64}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(result.reason, "already_satisfied")
        self.assertEqual(result.metrics["after_count"], 64)
        self.assertEqual(mine_calls, [])
        self.assertIn("spruce_log", resource_plan_for("logs").inventory_items)
        self.assertIn("birch_log", resource_plan_for("logs").inventory_items)

    def test_collect_resource_tries_next_candidate_after_failed_target(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool([[1, 59, 0], [2, 59, 0]])
        registry.register(search)
        miner, mine_calls = mine_tool_fail_once(body)
        registry.register(miner)
        ctx, trace_events = composition_context(body, registry, max_candidates=3)

        result = collect_resource({"item": "dirt", "count": 1}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual([call["pos"] for call in mine_calls], [[1, 59, 0], [2, 59, 0]])
        self.assertEqual(result.metrics["candidates_tried"], 2)
        self.assertTrue(any(event["event"] == "composition_search" for event in trace_events))
        self.assertTrue(any(event["event"] == "composition_mine_attempt" for event in trace_events))
        summaries = [event for event in trace_events if event["event"] == "composition_summary"]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["reason"], "collected")
        self.assertEqual(summaries[0]["collected_delta"], 1)
        self.assertEqual(summaries[0]["attempt_count"], 2)
        self.assertEqual(summaries[0]["mine_result_reasons"][0]["reason"], "collected")

    def test_collect_resource_stops_candidate_loop_on_body_rejected(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool([[1, 59, 0], [2, 59, 0], [3, 59, 0], [4, 59, 0]])
        registry.register(search)
        miner, mine_calls = mine_tool_body_rejected(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=4)

        result = collect_resource({"item": "logs", "count": 1}, ctx)

        self.assertFalse(result.success, result)
        self.assertEqual(result.reason, "body_rejected")
        self.assertEqual(result.metrics["resume_hint"], "resume_after_body_control")
        self.assertEqual(len(mine_calls), 1)
        self.assertEqual(ctx.weld_context.authority.failure_steps, 1)
        self.assertFalse(result.metrics["skipped"][0]["skip"])

    def test_collect_resource_skips_body_rejected_mining_stand_candidate(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool([[1, 59, 0], [2, 59, 0]])
        registry.register(search)
        miner, mine_calls = mine_tool_approach_body_rejected_then_success(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=2)

        result = collect_resource({"item": "logs", "count": 1}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(result.reason, "collected")
        self.assertEqual([call["pos"] for call in mine_calls], [[1, 59, 0], [2, 59, 0]])
        self.assertEqual(result.metrics["skipped"][0]["reason"], "body_rejected")
        self.assertTrue(result.metrics["skipped"][0]["skip"])
        self.assertEqual(ctx.weld_context.authority.failure_steps, 0)

    def test_collect_resource_trace_keeps_clearance_denial_block_fact(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool([[9, 64, 11]])
        registry.register(search)
        miner, _mine_calls = mine_tool_clearance_denied(body)
        registry.register(miner)
        ctx, trace_events = composition_context(body, registry, max_candidates=1)

        result = collect_resource({"item": "logs", "count": 1}, ctx)

        self.assertFalse(result.success)
        mine_events = [event for event in trace_events if event["event"] == "composition_mine_attempt"]
        self.assertEqual(len(mine_events), 1)
        clearance = mine_events[0]["diagnostics"]["clearance"]
        self.assertEqual(clearance["metrics"]["target"], [10, 65, 11])
        self.assertEqual(clearance["metrics"]["block_type"], "spruce_leaves")
        self.assertEqual(clearance["legality"]["reason"], "not_natural_breakable")
        summaries = [event for event in trace_events if event["event"] == "composition_summary"]
        self.assertEqual(summaries[0]["skip_reasons"][0]["reason"], "mine_approach_failed:dig_through:break_denied:not_natural_breakable")
        self.assertEqual(summaries[0]["last_failure"]["phase"], "mine")
        self.assertEqual(
            summaries[0]["blocked_clearance"],
            [
                {
                    "block_type": "spruce_leaves",
                    "legality_reason": "not_natural_breakable",
                    "count": 1,
                    "sample_targets": [[10, 65, 11]],
                    "sample_stand_blocks": [[10, 64, 11]],
                }
            ],
        )

    def test_collect_resource_prefers_verified_search_target_over_raw_candidates(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)

        def callable_(params):
            return ToolResult(
                True,
                "block_in_range",
                False,
                metrics={
                    "target": {"pos": [5, 70, 0], "type": "dirt"},
                    "candidates": [
                        {"pos": [3, 59, 0], "type": "dirt", "distance": 1},
                        {"pos": [5, 70, 0], "type": "dirt", "distance": 2},
                    ],
                },
            )

        registry.register(
            RegisteredTool(
                "search_for_block",
                "search",
                {"type": "object"},
                callable_,
                ToolSidecar("search_for_block", mutating=False, permission="read_world"),
            )
        )
        miner, mine_calls = mine_tool(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=3)

        result = collect_resource({"item": "dirt", "count": 1}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual([call["pos"] for call in mine_calls], [[5, 70, 0]])

    def test_collect_resource_diversifies_same_tree_candidates_before_budget_exhaustion(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        # Search commonly returns several blocks from the same trunk/canopy before
        # any other tree. The orchestrator should spread attempts across clusters
        # instead of spending the whole candidate budget on one blocked tree.
        search, _search_calls = candidate_search_tool(
            [
                [10, 66, 10],
                [10, 67, 10],
                [11, 68, 10],
                [30, 66, 30],
            ]
        )
        registry.register(search)
        miner, mine_calls = mine_tool(body, fail_reason="mine_failed:no_inventory_delta")
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=3)

        result = collect_resource({"item": "logs", "count": 1}, ctx)

        self.assertFalse(result.success, result)
        self.assertEqual(result.reason, "partial_budget_exhausted")
        # Logs are tree structures: prefer low trunk candidates before jumping
        # into higher canopy blocks that are often leaf-blocked in real terrain.
        self.assertEqual([call["pos"] for call in mine_calls], [[10, 66, 10], [30, 66, 30], [10, 67, 10]])

    def test_collect_resource_orders_log_candidates_by_low_trunk_before_canopy(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool(
            [
                [-66, 79, -59],
                [-64, 80, -63],
                [-81, 72, -47],
                [-81, 73, -47],
            ]
        )
        registry.register(search)
        miner, mine_calls = mine_tool(body, fail_reason="mine_failed:no_inventory_delta")
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=4)

        result = collect_resource({"item": "logs", "count": 1}, ctx)

        self.assertFalse(result.success, result)
        self.assertEqual(
            [call["pos"] for call in mine_calls],
            [[-81, 72, -47], [-66, 79, -59], [-64, 80, -63], [-81, 73, -47]],
        )

    def test_collect_resource_round_robins_log_columns_before_high_canopy(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool(
            [
                [-79, 72, 75],
                [-79, 73, 75],
                [-79, 74, 75],
                [-79, 75, 75],
                [-82, 64, 75],
                [-75, 63, 62],
            ]
        )
        registry.register(search)
        miner, mine_calls = mine_tool(body, fail_reason="mine_failed:no_inventory_delta")
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=4)

        result = collect_resource({"item": "logs", "count": 1}, ctx)

        self.assertFalse(result.success, result)
        self.assertEqual(
            [call["pos"] for call in mine_calls],
            [[-75, 63, 62], [-82, 64, 75], [-79, 72, 75], [-79, 73, 75]],
        )

    def test_collect_resource_stops_candidate_loop_on_inner_progress_yield(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool([[-79, 65, 75]], distances=[2.0])
        registry.register(search)
        miner, mine_calls = mine_tool_that_progress_yields(body)
        registry.register(miner)
        ctx, trace_events = composition_context(body, registry, max_candidates=1)

        result = collect_resource({"item": "logs", "count": 1}, ctx)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "mine_progress_yielded")
        self.assertEqual(result.metrics["resume_hint"], "resume_after_body_control")
        self.assertEqual(len(mine_calls), 1)
        self.assertEqual(ctx.weld_context.authority.failure_steps, 0)
        mine_events = [event for event in trace_events if event["event"] == "composition_mine_attempt"]
        self.assertEqual(len(mine_events), 1)
        self.assertEqual(mine_events[0]["reason"], "mine_progress_yielded")
        self.assertEqual(result.metrics["skipped"][0]["reason"], "mine_progress_yielded")
        self.assertEqual(result.metrics["attempts"][0]["mine"]["metrics"]["target"], [-79, 65, 75])
        self.assertEqual(result.metrics["attempts"][0]["mine"]["metrics"]["progress_facts"]["failure_steps"], 5)

    def test_collect_resource_skips_candidate_navigation_progress_yield(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool([[1, 59, 0], [2, 59, 0]])
        registry.register(search)
        miner, mine_calls = mine_tool_candidate_navigation_yields_then_success(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=2)

        result = collect_resource({"item": "logs", "count": 1}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(result.reason, "collected")
        self.assertEqual([call["pos"] for call in mine_calls], [[1, 59, 0], [2, 59, 0]])
        self.assertEqual(result.metrics["skipped"][0]["reason"], "mine_approach_failed:dig_through:no_path")
        self.assertTrue(result.metrics["skipped"][0]["skip"])
        self.assertEqual(ctx.weld_context.authority.stalled_steps, 0)
        self.assertEqual(ctx.weld_context.authority.failure_steps, 0)

    def test_collect_resource_keeps_probing_after_candidate_navigation_no_path_abort(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool([[10, 59, 0], [2, 59, 0]])
        registry.register(search)
        miner, mine_calls = mine_tool_candidate_navigation_yields_then_success(body)
        registry.register(miner)
        ctx, trace_events = composition_context(body, registry, max_candidates=2)

        result = collect_resource({"item": "logs", "count": 1}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual([call["pos"] for call in mine_calls], [[10, 59, 0], [2, 59, 0]])
        self.assertEqual(result.metrics["skipped"][0]["reason"], "mine_approach_failed:dig_through:no_path")
        self.assertTrue(result.metrics["skipped"][0]["skip"])
        self.assertEqual(ctx.weld_context.authority.failure_steps, 0)
        mine_events = [event for event in trace_events if event["event"] == "composition_mine_attempt"]
        self.assertEqual(mine_events[0]["reason"], "mine_approach_failed:dig_through:no_path")

    def test_collect_resource_keeps_non_log_candidate_diversification(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool(
            [
                [10, 59, 10],
                [10, 60, 10],
                [11, 61, 10],
                [30, 59, 30],
            ]
        )
        registry.register(search)
        miner, mine_calls = mine_tool(body, fail_reason="mine_failed:no_inventory_delta")
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=3)

        result = collect_resource({"item": "dirt", "count": 1}, ctx)

        self.assertFalse(result.success, result)
        self.assertEqual([call["pos"] for call in mine_calls], [[10, 59, 10], [30, 59, 30], [10, 60, 10]])

    def test_collect_resource_tries_candidates_when_search_skip_fails_on_top_pick(self):
        # The live bug: search navigated to its own nearest candidate (underground,
        # no stand point) and returned success=False with a candidate-skip reason,
        # which made collect_resource abort -- even though the candidate list still
        # held a reachable block. The orchestrator must fall through and try the
        # next untried candidate via mine's own approach, not abort.
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = skip_failing_search_tool([[1, 59, 0], [2, 70, 0]])
        registry.register(search)
        miner, mine_calls = mine_tool(body)
        registry.register(miner)
        ctx, trace_events = composition_context(body, registry, max_candidates=3)

        result = collect_resource({"item": "dirt", "count": 1}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(result.reason, "collected")
        # Both candidates were tried (mine approaches each), not aborted on the first.
        self.assertEqual([call["pos"] for call in mine_calls][0], [1, 59, 0])
        self.assertTrue(any(event["event"] == "composition_search_skip" for event in trace_events))
        # The search-skip is recorded as a neutral skip, not a task failure.
        self.assertTrue(any(entry.get("phase") == "search" and entry.get("skip") for entry in result.metrics["skipped"]))

    def test_collect_resource_reports_successful_partial_when_progress_made_then_local_candidates_exhaust(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool([[1, 59, 0], [2, 59, 0], [3, 59, 0]])
        registry.register(search)
        miner, _mine_calls = mine_tool_with_outcomes(body, ["fail", "success", "fail"])
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=6)

        result = collect_resource({"item": "dirt", "count": 2}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(result.reason, "partial_candidate_targets_exhausted")
        self.assertEqual(result.metrics["before_count"], 0)
        self.assertEqual(result.metrics["after_count"], 1)
        self.assertEqual(result.metrics["collected_delta"], 1)
        self.assertFalse(result.metrics["complete"])
        self.assertEqual(result.metrics["last_failure"]["reason"], "candidate_targets_exhausted")

    def test_collect_resource_reports_successful_partial_when_progress_made_then_budget_exhausts(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool([[1, 59, 0], [2, 59, 0], [3, 59, 0]])
        registry.register(search)
        miner, _mine_calls = mine_tool_with_outcomes(body, ["success", "fail", "fail"])
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=3)

        result = collect_resource({"item": "dirt", "count": 2}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(result.reason, "partial_budget_exhausted")
        self.assertEqual(result.metrics["before_count"], 0)
        self.assertEqual(result.metrics["after_count"], 1)
        self.assertEqual(result.metrics["collected_delta"], 1)
        self.assertEqual(result.metrics["remaining_count"], 1)
        self.assertFalse(result.metrics["complete"])

    def test_collect_resource_keeps_budget_exhaustion_failure_when_no_progress_made(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool([[1, 59, 0], [2, 59, 0], [3, 59, 0]])
        registry.register(search)
        miner, _mine_calls = mine_tool(body, fail_reason="mine_failed:no_inventory_delta")
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=3)

        result = collect_resource({"item": "dirt", "count": 2}, ctx)

        self.assertFalse(result.success, result)
        self.assertEqual(result.reason, "partial_budget_exhausted")
        self.assertEqual(result.metrics["collected_delta"], 0)
        self.assertFalse(result.metrics["complete"])

    def test_collect_resource_keeps_goal_total_when_model_passes_remaining_count(self):
        body = FakeBody()
        body.inventory_counts = {"oak_log": 18}
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = candidate_search_tool([[1, 59, 0]])
        registry.register(search)
        miner, _mine_calls = mine_tool(body, fail_reason="break_denied:protected_region")
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=1)
        ctx.weld_context.goal_text = "collect 64 logs"

        result = collect_resource({"item": "oak_log", "count": 46}, ctx)

        self.assertFalse(result.success, result)
        self.assertEqual(result.metrics["requested_count"], 46)
        self.assertEqual(result.metrics["goal_target_count"], 64)
        self.assertEqual(result.metrics["target_count"], 64)
        self.assertEqual(result.metrics["after_count"], 18)
        self.assertEqual(result.metrics["remaining_count"], 46)

    def test_collect_resource_aborts_when_search_fails_for_non_skip_reason(self):
        # A non-skip search failure (perception_failed: the candidate list itself is
        # untrustworthy) must still abort honestly -- do NOT swallow real failures.
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = skip_failing_search_tool([[1, 59, 0], [2, 70, 0]], reason="perception_failed")
        registry.register(search)
        miner, mine_calls = mine_tool(body)
        registry.register(miner)
        ctx, _trace_events = composition_context(body, registry, max_candidates=3)

        result = collect_resource({"item": "dirt", "count": 1}, ctx)

        self.assertFalse(result.success, result)
        self.assertEqual(mine_calls, [])  # never mined on an untrustworthy candidate list
        self.assertEqual(result.metrics["after_count"], 0)


if __name__ == "__main__":
    unittest.main()
