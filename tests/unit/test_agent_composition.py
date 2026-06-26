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
from minebot.brain.progress import ProgressAuthority
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


def candidate_search_tool(targets):
    calls = []

    def callable_(params):
        calls.append(dict(params))
        candidates = [
            {"pos": list(target), "type": params.get("block_types", ["dirt"])[0], "distance": index}
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


def composition_context(body, registry, *, max_candidates=4):
    return CompositionContext(
        registry=registry,
        weld_context=WeldContext(body=body, authority=ProgressAuthority(), goal_text="collect 2 dirt"),
        runtime_profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
        budget=CompositionBudget(max_candidates=max_candidates, max_mutating_calls=max_candidates, max_wall_s=10),
    )


class AgentCompositionTests(unittest.TestCase):
    def test_read_inventory_tool_pages_to_avoid_truncated_payloads(self):
        body = PagedInventoryBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        ctx = composition_context(body, registry)

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
        ctx = composition_context(body, registry)
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
        self.assertEqual(ctx.weld_context.authority.last_action[0], "mine_block_collect")
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
        ctx = composition_context(body, registry)
        register_collect_resource_tool(registry, ctx)

        result = execute_tool(
            registry.get("collect_resource"),
            {"item": "dirt", "count": 1},
            ctx.weld_context,
        )

        self.assertTrue(result["success"], result)
        self.assertFalse(registry.get("collect_resource").sidecar.mutating)
        self.assertEqual(ctx.weld_context.authority.last_action[0], "mine_block_collect")
        self.assertNotEqual(ctx.weld_context.authority.last_action[0], "collect_resource")
        self.assertIsNone(ctx.weld_context.writer.holder)

    def test_mutating_leaf_owner_busy_is_honest_retryable_failure(self):
        body = FakeBody()
        registry = ToolRegistry()
        miner, _mine_calls = mine_tool(body)
        registry.register(miner)
        ctx = composition_context(body, registry)
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
        ctx = composition_context(body, registry)

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
        ctx = composition_context(body, registry)

        result = collect_resource({"item": "dirt", "count": 1}, ctx)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "protected_or_illegal_target")
        self.assertTrue(result.can_retry)
        self.assertEqual(result.metrics["skipped"][0]["reason"], "break_denied:protected_region")

    def test_collect_resource_maps_resource_to_blocks_and_inventory_item(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, search_calls = search_tool([[1, 59, 0]])
        registry.register(search)
        miner, mine_calls = mine_tool(body)
        registry.register(miner)
        ctx = composition_context(body, registry)
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

    def test_collect_resource_caps_search_limit_for_rcon_payload_safety(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, search_calls = search_tool([[1, 59, 0]])
        registry.register(search)
        miner, _mine_calls = mine_tool(body)
        registry.register(miner)
        ctx = composition_context(body, registry, max_candidates=96)

        result = collect_resource({"item": "logs", "count": 1}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(search_calls[0]["find_limit"], 32)

    def test_collect_resource_counts_equivalent_log_inventory_items(self):
        body = FakeBody()
        body.inventory_counts = {"spruce_log": 32, "birch_log": 32}
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        search, _search_calls = search_tool([[1, 59, 0]])
        registry.register(search)
        miner, mine_calls = mine_tool(body)
        registry.register(miner)
        ctx = composition_context(body, registry)

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
        ctx = composition_context(body, registry, max_candidates=3)

        result = collect_resource({"item": "dirt", "count": 1}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual([call["pos"] for call in mine_calls], [[1, 59, 0], [2, 59, 0]])
        self.assertEqual(result.metrics["candidates_tried"], 2)


if __name__ == "__main__":
    unittest.main()
