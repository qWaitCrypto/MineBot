import unittest

from minebot.brain.composition import (
    CompositionBudget,
    CompositionContext,
    collect_resource,
    ensure_item,
    ensure_tool_for,
    register_collect_resource_tool,
    register_ensure_tool_for_tool,
    register_inventory_tools,
    resource_plan_for,
)
from minebot.brain.acquisition import RecipeVariant
from minebot.brain.modes import ModeRuntime
from minebot.brain.progress import FAILURE_STORM_LIMIT, ProgressAbort, ProgressAuthority
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, WeldContext, execute_tool
from minebot.brain.lifecycle import LifecycleState
from minebot.contract import BodyState, PerceptionResult, Result, ToolResult


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
        self.interrupts = []

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

    def interrupt(self, reason=None):
        self.interrupts.append(reason)
        return Result(None, "Bot", "result", True, True, True, {"action": "interrupt"}, None)


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


def mine_tool_with_reason_sequence(body, outcomes):
    calls = []
    planned = list(outcomes)

    def callable_(params):
        calls.append(dict(params))
        outcome = planned.pop(0) if planned else "collect_no_inventory_delta"
        target = params.get("pos")
        if outcome == "success":
            expected = params.get("expected_drops") or ["dirt"]
            item = str(expected[0]).removeprefix("minecraft:")
            body.inventory_counts[item] = body.inventory_counts.get(item, 0) + 1
            return ToolResult(True, "collected", False, metrics={"target": target, "collected_total": 1})
        return ToolResult(False, str(outcome), True, metrics={"target": target, "collected_total": 0})

    return RegisteredTool(
        "mine_block_collect",
        "mine",
        {"type": "object"},
        callable_,
        ToolSidecar("mine_block_collect", mutating=True, permission="break", body_scope=("mine",)),
    ), calls


def mine_tool_no_path_sequence(body):
    calls = []

    def callable_(params):
        calls.append(dict(params))
        target = params.get("pos")
        return ToolResult(
            False,
            "mine_approach_failed:dig_through:no_path",
            True,
            metrics={
                "target": target,
                "dig_through_result": {
                    "success": False,
                    "reason": "no_path",
                    "canRetry": False,
                    "metrics": {"goal": target},
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


def mine_tool_body_action_timeout_then_success(body):
    calls = []

    def callable_(params):
        calls.append(dict(params))
        target = params.get("pos")
        if len(calls) == 1:
            exc = TimeoutError("timed out waiting for terminal event for action a1")
            exc.diagnostics = {
                "action_id": "a1",
                "terminal_events": ["mineDone"],
                "poll_count": 88,
                "wait_ms": 12043.456,
                "observed_events": 0,
            }
            raise exc
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
        recipe_lookup=acquisition_recipe_lookup,
        trace=lambda event, payload: trace_events.append({"event": event, **payload}),
    ), trace_events


def resource_domain_tool(body, *outcomes):
    calls = []
    planned = list(outcomes)

    def callable_(params):
        calls.append(dict(params))
        outcome = dict(planned.pop(0)) if planned else {
            "success": True,
            "reason": "resource_domain_collected",
            "can_retry": False,
            "delta": int(params.get("remaining_count") or 0),
        }
        delta = int(outcome.pop("delta", 0) or 0)
        expected = [str(item).removeprefix("minecraft:") for item in params.get("expected_drops") or []]
        if delta > 0 and expected:
            body.inventory_counts[expected[0]] = body.inventory_counts.get(expected[0], 0) + delta
        metrics = {
            "collected_total": delta,
            "attempts": outcome.pop("attempts", []),
            "candidate_blacklist": outcome.pop("candidate_blacklist", []),
            "complete": bool(outcome.get("success")),
        }
        metrics.update(dict(outcome.pop("metrics", {})))
        return ToolResult(
            bool(outcome.pop("success")),
            str(outcome.pop("reason")),
            bool(outcome.pop("can_retry", False)),
            metrics=metrics,
        )

    return RegisteredTool(
        "collect_block_domain",
        "collect a physical block domain",
        {"type": "object"},
        callable_,
        ToolSidecar(
            "collect_block_domain",
            mutating=True,
            source="body.resource_collection",
            permission="collect_natural_resource",
            body_scope=("search", "navigation", "mine", "pickup", "inventory"),
        ),
    ), calls


def acquisition_recipe_lookup(item):
    recipes = {
        "oak_planks": [RecipeVariant("oak_planks", 4, (("oak_log",),))],
        "stick": [RecipeVariant("stick", 4, (("oak_planks",), ("oak_planks",)))],
        "crafting_table": [RecipeVariant("crafting_table", 1, (("oak_planks",),) * 4)],
        "wooden_pickaxe": [
            RecipeVariant("wooden_pickaxe", 1, (("oak_planks",), ("oak_planks",), ("oak_planks",), ("stick",), ("stick",)), requires_table=True)
        ],
        "stone_pickaxe": [
            RecipeVariant("stone_pickaxe", 1, (("cobblestone",), ("cobblestone",), ("cobblestone",), ("stick",), ("stick",)), requires_table=True)
        ],
        "iron_pickaxe": [
            RecipeVariant("iron_pickaxe", 1, (("iron_ingot",), ("iron_ingot",), ("iron_ingot",), ("stick",), ("stick",)), requires_table=True)
        ],
        "furnace": [RecipeVariant("furnace", 1, tuple(("cobblestone",) for _ in range(8)), requires_table=True)],
    }
    return recipes.get(item)


def register_fake_acquisition_leaf_tools(registry, body, *, fail_tool=None):
    calls = []

    def record(name, params):
        calls.append((name, dict(params)))
        if fail_tool == name:
            return ToolResult(False, f"{name}_failed", True, metrics={"params": dict(params)})
        if name == "collect_resource":
            item = str(params["item"]).removeprefix("minecraft:")
            count = int(params.get("count") or 1)
            body.inventory_counts[item] = body.inventory_counts.get(item, 0) + count
        elif name == "craft_item":
            item = str(params["item"]).removeprefix("minecraft:")
            count = int(params.get("count") or 1)
            body.inventory_counts[item] = body.inventory_counts.get(item, 0) + count
        elif name == "smelt_item":
            input_item = str(params["input_item"]).removeprefix("minecraft:")
            count = int(params.get("count") or 1)
            output = {"raw_iron": "iron_ingot", "raw_gold": "gold_ingot", "raw_copper": "copper_ingot"}.get(input_item, input_item)
            body.inventory_counts[output] = body.inventory_counts.get(output, 0) + count
        elif name == "equip_item":
            pass
        return ToolResult(True, "completed", False, metrics={"params": dict(params)})

    for name, sidecar in (
        ("collect_resource", ToolSidecar("collect_resource", mutating=False, source="agent.composition", permission="compose_collect")),
        ("craft_item", ToolSidecar("craft_item", mutating=True, source="body.inventory", permission="craft")),
        ("smelt_item", ToolSidecar("smelt_item", mutating=True, source="body.furnace", permission="smelt")),
        ("equip_item", ToolSidecar("equip_item", mutating=True, source="body.inventory", permission="equip")),
    ):
        registry.register(RegisteredTool(name, name, {"type": "object"}, lambda params, tool_name=name: record(tool_name, params), sidecar))
    return calls


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




    def test_register_ensure_tool_for_is_leaf_led_composition_tool(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        register_fake_acquisition_leaf_tools(registry, body)
        ctx, _trace_events = composition_context(body, registry)
        register_ensure_tool_for_tool(registry, ctx, acquisition_recipe_lookup)

        tool = registry.get("ensure_tool_for")

        self.assertFalse(tool.sidecar.mutating)
        self.assertTrue(tool.sidecar.can_mutate_body)
        self.assertEqual(tool.sidecar.source, "agent.composition")
        self.assertEqual(tool.sidecar.permission, "compose_ensure")
        self.assertEqual(tool.sidecar.terminal_truth, ("inventory", "ToolResult"))

    def test_ensure_item_executes_resolver_plan_through_leaf_tools(self):
        body = FakeBody()
        body.inventory_counts = {}
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        calls = register_fake_acquisition_leaf_tools(registry, body)
        ctx, _trace_events = composition_context(body, registry, max_candidates=64)

        result = ensure_item({"item": "iron_pickaxe", "count": 1}, ctx, acquisition_recipe_lookup)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "ensured")
        self.assertEqual(result.metrics["current_count"], 1)
        self.assertEqual(calls[0], ("collect_resource", {"item": "oak_log", "count": 4, "constraints": {"auto_prerequisites": False}}))
        self.assertIn(("smelt_item", {"input_item": "raw_iron", "count": 3}), calls)
        self.assertEqual(calls[-1], ("craft_item", {"item": "iron_pickaxe", "count": 1, "search_radius": 64, "cleanup_existing_bot_table": True}))
        self.assertFalse(registry.get("collect_resource").sidecar.mutating)
        self.assertNotEqual(ctx.weld_context.authority.last_action[0], "ensure_tool_for")

    def test_ensure_item_keeps_workstation_until_final_table_recipe(self):
        body = FakeBody()
        body.inventory_counts = {}
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        calls = []

        def record(name, params):
            calls.append((name, dict(params)))
            if name == "collect_resource":
                item = str(params["item"]).removeprefix("minecraft:")
                count = int(params.get("count") or 1)
                body.inventory_counts[item] = max(body.inventory_counts.get(item, 0), count)
            elif name == "craft_item":
                item = str(params["item"]).removeprefix("minecraft:")
                count = int(params.get("count") or 1)
                if item == "crafting_table":
                    body.inventory_counts[item] = body.inventory_counts.get(item, 0) + count
                elif params.get("keep_temporary_table"):
                    body.inventory_counts["crafting_table"] = 0
                elif params.get("cleanup_existing_bot_table"):
                    body.inventory_counts["crafting_table"] = body.inventory_counts.get("crafting_table", 0) + 1
                body.inventory_counts[item] = max(body.inventory_counts.get(item, 0), count)
            elif name == "smelt_item":
                body.inventory_counts["iron_ingot"] = body.inventory_counts.get("iron_ingot", 0) + int(params.get("count") or 1)
            return ToolResult(True, "completed", False, metrics={"params": dict(params)})

        for name, sidecar in (
            ("collect_resource", ToolSidecar("collect_resource", mutating=False, source="agent.composition", permission="compose_collect")),
            ("craft_item", ToolSidecar("craft_item", mutating=True, source="body.inventory", permission="craft")),
            ("smelt_item", ToolSidecar("smelt_item", mutating=True, source="body.furnace", permission="smelt")),
            ("equip_item", ToolSidecar("equip_item", mutating=True, source="body.inventory", permission="equip")),
        ):
            registry.register(RegisteredTool(name, name, {"type": "object"}, lambda params, tool_name=name: record(tool_name, params), sidecar))
        ctx, _trace_events = composition_context(body, registry, max_candidates=64)

        result = ensure_item({"item": "iron_pickaxe", "count": 1}, ctx, acquisition_recipe_lookup)

        self.assertTrue(result.success, result.to_payload())
        craft_calls = [params for tool, params in calls if tool == "craft_item"]
        table_recipe_calls = [params for params in craft_calls if params["item"] in {"wooden_pickaxe", "stone_pickaxe", "furnace", "iron_pickaxe"}]
        self.assertEqual(
            table_recipe_calls,
            [
                {"item": "wooden_pickaxe", "count": 1, "search_radius": 64, "keep_temporary_table": True},
                {"item": "stone_pickaxe", "count": 1, "search_radius": 64, "keep_temporary_table": True},
                {"item": "furnace", "count": 1, "search_radius": 64, "keep_temporary_table": True},
                {"item": "iron_pickaxe", "count": 1, "search_radius": 64, "cleanup_existing_bot_table": True},
            ],
        )
        self.assertEqual([params for params in craft_calls if params["item"] == "crafting_table"], [{"item": "crafting_table", "count": 1}])

    def test_ensure_tool_for_resource_targets_required_pickaxe(self):
        body = FakeBody()
        body.inventory_counts = {}
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        calls = register_fake_acquisition_leaf_tools(registry, body)
        ctx, _trace_events = composition_context(body, registry, max_candidates=64)

        result = ensure_tool_for({"resource": "diamond"}, ctx, acquisition_recipe_lookup)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.metrics["item"], "iron_pickaxe")
        self.assertEqual(calls[-1], ("craft_item", {"item": "iron_pickaxe", "count": 1, "search_radius": 64, "cleanup_existing_bot_table": True}))

    def test_ensure_item_failure_reports_failed_step_and_plan(self):
        body = FakeBody()
        body.inventory_counts = {}
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        register_fake_acquisition_leaf_tools(registry, body, fail_tool="smelt_item")
        ctx, _trace_events = composition_context(body, registry, max_candidates=64)

        result = ensure_item({"item": "iron_pickaxe", "count": 1}, ctx, acquisition_recipe_lookup)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "ensure_step_failed:smelt_item_failed")
        self.assertEqual(result.metrics["resume_hint"], "reinvoke_ensure")
        self.assertGreater(len(result.metrics["plan"]), 1)
        self.assertEqual(result.metrics["failed_step"]["step"]["kind"], "smelt")

    def test_composition_tool_result_trace_summarizes_leaf_result_metrics(self):
        body = FakeBody()
        body.inventory_counts = {
            "cobblestone": 8,
            "crafting_table": 1,
            "oak_planks": 2,
            "raw_iron": 3,
            "stick": 2,
            "stone_pickaxe": 1,
            "wooden_pickaxe": 1,
        }
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        calls = []

        def record(name, params):
            calls.append((name, dict(params)))
            if name == "smelt_item":
                body.inventory_counts["iron_ingot"] = 3
                return ToolResult(
                    True,
                    "completed",
                    False,
                    metrics={
                        "input_item": "raw_iron",
                        "count": 3,
                        "temporary_furnace_site": [2, 70, 0],
                        "nearest_furnace_result": ToolResult(
                            False,
                            "furnace_no_stand_point",
                            True,
                            metrics={"furnace_pos": [1, 70, 0]},
                        ).to_payload(),
                    },
                )
            if name == "craft_item":
                body.inventory_counts[str(params["item"])] = int(params.get("count") or 1)
            return ToolResult(True, "completed", False, metrics={"params": dict(params)})

        for name, sidecar in (
            ("collect_resource", ToolSidecar("collect_resource", mutating=False, source="agent.composition", permission="compose_collect")),
            ("craft_item", ToolSidecar("craft_item", mutating=True, source="body.inventory", permission="craft")),
            ("smelt_item", ToolSidecar("smelt_item", mutating=True, source="body.furnace", permission="smelt")),
            ("equip_item", ToolSidecar("equip_item", mutating=True, source="body.inventory", permission="equip")),
        ):
            registry.register(RegisteredTool(name, name, {"type": "object"}, lambda params, tool_name=name: record(tool_name, params), sidecar))
        ctx, trace_events = composition_context(body, registry, max_candidates=64)

        result = ensure_item({"item": "iron_pickaxe", "count": 1}, ctx, acquisition_recipe_lookup)

        self.assertTrue(result.success, result.to_payload())
        smelt_results = [
            event
            for event in trace_events
            if event["event"] == "composition_tool_result" and event["tool"] == "smelt_item"
        ]
        self.assertEqual(len(smelt_results), 1)
        self.assertTrue(smelt_results[0]["success"])
        self.assertEqual(smelt_results[0]["reason"], "completed")
        metrics = smelt_results[0]["summary"]["metrics"]
        self.assertEqual(metrics["input_item"], "raw_iron")
        self.assertEqual(metrics["count"], 3)
        self.assertEqual(metrics["temporary_furnace_site"], [2, 70, 0])
        self.assertEqual(metrics["nearest_furnace_result"]["reason"], "furnace_no_stand_point")
        self.assertEqual(metrics["nearest_furnace_result"]["metrics"]["furnace_pos"], [1, 70, 0])

    def test_ensure_item_requires_inventory_delta_before_completing_collect_step(self):
        body = FakeBody()
        body.inventory_counts = {}
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        calls = []

        def record(name, params):
            calls.append((name, dict(params)))
            if name == "collect_resource":
                body.inventory_counts["oak_log"] = 3
                return ToolResult(
                    True,
                    "partial_candidate_targets_exhausted",
                    True,
                    metrics={
                        "item": "oak_log",
                        "target_count": 4,
                        "after_count": 3,
                        "remaining_count": 1,
                        "resume_hint": "reselect_candidates",
                    },
                )
            return ToolResult(True, "completed", False, metrics={"params": dict(params)})

        for name, sidecar in (
            ("collect_resource", ToolSidecar("collect_resource", mutating=False, source="agent.composition", permission="compose_collect")),
            ("craft_item", ToolSidecar("craft_item", mutating=True, source="body.inventory", permission="craft")),
            ("smelt_item", ToolSidecar("smelt_item", mutating=True, source="body.furnace", permission="smelt")),
            ("equip_item", ToolSidecar("equip_item", mutating=True, source="body.inventory", permission="equip")),
        ):
            registry.register(RegisteredTool(name, name, {"type": "object"}, lambda params, tool_name=name: record(tool_name, params), sidecar))
        ctx, _trace_events = composition_context(body, registry, max_candidates=64)

        result = ensure_item({"item": "iron_pickaxe", "count": 1}, ctx, acquisition_recipe_lookup)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "ensure_step_incomplete")
        self.assertTrue(result.can_retry)
        self.assertEqual(result.metrics["resume_hint"], "reinvoke_ensure")
        self.assertEqual(result.metrics["failed_step"]["step"]["kind"], "collect")
        self.assertEqual(result.metrics["failed_step"]["current_count"], 3)
        self.assertEqual(result.metrics["failed_step"]["remaining_count"], 1)
        self.assertEqual(calls, [("collect_resource", {"item": "oak_log", "count": 4, "constraints": {"auto_prerequisites": False}})])

    def test_ensure_item_requires_step_delta_not_total_count_after_resume(self):
        body = FakeBody()
        body.inventory_counts = {
            "cobblestone": 5,
            "crafting_table": 1,
            "oak_planks": 5,
            "raw_iron": 3,
            "stick": 4,
            "stone_pickaxe": 1,
            "wooden_pickaxe": 1,
        }
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        calls = []

        def record(name, params):
            calls.append((name, dict(params)))
            if name == "collect_resource":
                body.inventory_counts["cobblestone"] += 2
                return ToolResult(
                    True,
                    "collected",
                    False,
                    metrics={"item": "cobblestone", "target_count": 3, "collected_delta": 2},
                )
            if name == "smelt_item":
                body.inventory_counts["iron_ingot"] = int(body.inventory_counts.get("iron_ingot", 0)) + int(
                    params.get("count") or 1
                )
            if name == "craft_item":
                body.inventory_counts[str(params["item"])] = max(
                    int(body.inventory_counts.get(str(params["item"]), 0)),
                    int(params.get("count") or 1),
                )
            return ToolResult(True, "completed", False, metrics={"params": dict(params)})

        for name, sidecar in (
            ("collect_resource", ToolSidecar("collect_resource", mutating=False, source="agent.composition", permission="compose_collect")),
            ("craft_item", ToolSidecar("craft_item", mutating=True, source="body.inventory", permission="craft")),
            ("smelt_item", ToolSidecar("smelt_item", mutating=True, source="body.furnace", permission="smelt")),
            ("equip_item", ToolSidecar("equip_item", mutating=True, source="body.inventory", permission="equip")),
        ):
            registry.register(RegisteredTool(name, name, {"type": "object"}, lambda params, tool_name=name: record(tool_name, params), sidecar))
        ctx, _trace_events = composition_context(body, registry, max_candidates=64)

        result = ensure_item({"item": "iron_pickaxe", "count": 1}, ctx, acquisition_recipe_lookup)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "ensure_step_incomplete")
        self.assertEqual(result.metrics["failed_step"]["step"]["item"], "cobblestone")
        self.assertEqual(result.metrics["failed_step"]["before_count"], 5)
        self.assertEqual(result.metrics["failed_step"]["current_count"], 7)
        self.assertEqual(result.metrics["failed_step"]["required_count"], 8)
        self.assertEqual(result.metrics["failed_step"]["remaining_count"], 1)
        self.assertEqual(calls, [("collect_resource", {"item": "cobblestone", "count": 8, "constraints": {"auto_prerequisites": False}})])

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


    def test_resource_plan_maps_stone_cobblestone_and_raw_gold_aliases(self):
        stone = resource_plan_for("stone")
        self.assertEqual(stone.inventory_item, "cobblestone")
        self.assertEqual(stone.inventory_items, ("cobblestone",))
        self.assertEqual(stone.expected_drops, ("cobblestone",))
        self.assertEqual(stone.block_types, ("stone", "cobblestone"))

        cobblestone = resource_plan_for("cobblestone")
        self.assertEqual(cobblestone.inventory_item, "cobblestone")
        self.assertEqual(cobblestone.block_types, ("stone", "cobblestone"))

        raw_gold = resource_plan_for("raw_gold")
        self.assertEqual(raw_gold.inventory_item, "raw_gold")
        self.assertEqual(raw_gold.expected_drops, ("raw_gold",))
        self.assertEqual(raw_gold.block_types, ("gold_ore", "deepslate_gold_ore"))































    def test_collect_resource_delegates_one_physical_domain_through_weld(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        domain, calls = resource_domain_tool(body)
        registry.register(domain)
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
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["block_types"], ["dirt", "grass_block", "coarse_dirt", "rooted_dirt"])
        self.assertEqual(calls[0]["expected_drops"], ["dirt"])
        self.assertEqual(calls[0]["remaining_count"], 2)
        self.assertFalse(registry.get("collect_resource").sidecar.mutating)
        self.assertTrue(registry.get("collect_resource").sidecar.can_mutate_body)
        self.assertIsNone(ctx.weld_context.writer.holder)

    def test_collect_resource_passes_bounded_domain_budgets(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        domain, calls = resource_domain_tool(body)
        registry.register(domain)
        ctx, _trace_events = composition_context(body, registry, max_candidates=96)

        result = collect_resource({"item": "logs", "count": 11}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(calls[0]["find_limit"], 12)
        self.assertEqual(calls[0]["max_pages"], 8)
        self.assertEqual(calls[0]["candidate_budget"], 96)
        self.assertEqual(calls[0]["mutation_budget"], 96)
        self.assertEqual(calls[0]["search_radius"], 48)

    def test_collect_resource_returns_not_found_as_honest_retryable_failure(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        domain, _calls = resource_domain_tool(
            body,
            {"success": False, "reason": "resource_candidates_not_found", "can_retry": True},
        )
        registry.register(domain)
        ctx, _trace_events = composition_context(body, registry)

        result = collect_resource({"item": "dirt", "count": 1}, ctx)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "target_not_found")
        self.assertTrue(result.can_retry)
        self.assertEqual(result.metrics["after_count"], 0)
        self.assertEqual(result.metrics["resume_hint"], "reselect_candidates")

    def test_collect_resource_keeps_body_candidate_exhaustion_truth(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        domain, _calls = resource_domain_tool(
            body,
            {
                "success": False,
                "reason": "resource_candidate_domain_exhausted",
                "can_retry": True,
                "candidate_blacklist": [[1, 59, 0]],
            },
        )
        registry.register(domain)
        ctx, _trace_events = composition_context(body, registry)

        result = collect_resource({"item": "dirt", "count": 1}, ctx)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "candidate_targets_exhausted")
        self.assertEqual(result.metrics["skipped"], [
            {"pos": [1, 59, 0], "reason": "body_candidate_blacklist", "skip": True}
        ])
        self.assertEqual(
            result.metrics["last_failure"]["result"]["metrics"]["candidate_blacklist"],
            [[1, 59, 0]],
        )

    def test_collect_resource_surfaces_missing_required_tool(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        domain, calls = resource_domain_tool(
            body,
            {
                "success": False,
                "reason": "missing_required_tool",
                "can_retry": False,
                "metrics": {"required_tier": "iron", "best_owned": {"item": "stone_pickaxe", "tier": "stone"}},
            },
        )
        registry.register(domain)
        ctx, trace_events = composition_context(body, registry, max_candidates=2)

        result = collect_resource(
            {"item": "diamond", "count": 1, "constraints": {"auto_prerequisites": False}},
            ctx,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "missing_required_tool")
        self.assertFalse(result.can_retry)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.metrics["last_failure"]["result"]["metrics"]["required_tier"], "iron")
        self.assertEqual(
            [event for event in trace_events if event["event"] == "composition_summary"][-1]["reason"],
            "missing_required_tool",
        )

    def test_collect_resource_auto_prerequisites_finish_before_body_domain(self):
        body = FakeBody()
        body.inventory_counts = {}
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        register_fake_acquisition_leaf_tools(registry, body)
        domain, calls = resource_domain_tool(body)
        registry.register(domain)
        ctx, _trace_events = composition_context(body, registry, max_candidates=64)

        result = collect_resource({"item": "diamond", "count": 1}, ctx)

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(body.inventory_counts["iron_pickaxe"], 1)
        self.assertEqual(calls[0]["block_types"], ["diamond_ore", "deepslate_diamond_ore"])
        self.assertEqual(calls[0]["expected_drops"], ["diamond"])

    def test_collect_resource_maps_resource_objective_before_body_domain(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        domain, calls = resource_domain_tool(body)
        registry.register(domain)
        ctx, _trace_events = composition_context(body, registry)

        result = collect_resource(
            {"item": "iron", "count": 1, "constraints": {"max_candidates": 1, "auto_prerequisites": False}},
            ctx,
        )

        self.assertTrue(result.success, result)
        self.assertEqual(calls[0]["block_types"], ["iron_ore", "deepslate_iron_ore"])
        self.assertEqual(calls[0]["expected_drops"], ["raw_iron"])
        self.assertEqual(result.metrics["requested_item"], "iron")
        self.assertEqual(result.metrics["item"], "raw_iron")

    def test_resource_plan_maps_surface_and_mined_aliases(self):
        dirt = resource_plan_for("dirt")
        self.assertEqual(dirt.inventory_items, ("dirt",))
        self.assertEqual(dirt.expected_drops, ("dirt",))
        self.assertIn("grass_block", dirt.block_types)

        stone = resource_plan_for("stone")
        self.assertEqual(stone.inventory_item, "cobblestone")
        self.assertEqual(stone.block_types, ("stone", "cobblestone"))

        raw_gold = resource_plan_for("raw_gold")
        self.assertEqual(raw_gold.block_types, ("gold_ore", "deepslate_gold_ore"))

    def test_collect_resource_caps_radius_and_uses_bounded_pages(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        domain, calls = resource_domain_tool(body)
        registry.register(domain)
        ctx, _trace_events = composition_context(body, registry, max_candidates=24)

        result = collect_resource({"item": "logs", "count": 1, "constraints": {"radius": 96}}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(calls[0]["search_radius"], 64)
        self.assertEqual(calls[0]["find_limit"], 12)
        self.assertEqual(calls[0]["max_pages"], 2)

    def test_collect_resource_counts_equivalent_log_inventory_items(self):
        body = FakeBody()
        body.inventory_counts = {"spruce_log": 32, "birch_log": 32}
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        ctx, _trace_events = composition_context(body, registry)

        result = collect_resource({"item": "logs", "count": 64}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(result.reason, "already_satisfied")
        self.assertEqual(result.metrics["after_count"], 64)

    def test_collect_resource_reports_authoritative_partial_progress(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        domain, _calls = resource_domain_tool(
            body,
            {
                "success": False,
                "reason": "resource_domain_budget_exhausted",
                "can_retry": True,
                "delta": 1,
            },
        )
        registry.register(domain)
        ctx, _trace_events = composition_context(body, registry, max_candidates=3)

        result = collect_resource({"item": "dirt", "count": 2}, ctx)

        self.assertTrue(result.success, result)
        self.assertEqual(result.reason, "partial_budget_exhausted")
        self.assertEqual(result.metrics["after_count"], 1)
        self.assertEqual(result.metrics["collected_delta"], 1)
        self.assertEqual(result.metrics["remaining_count"], 1)
        self.assertFalse(result.metrics["complete"])

    def test_collect_resource_keeps_budget_failure_without_inventory_progress(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        domain, _calls = resource_domain_tool(
            body,
            {"success": False, "reason": "resource_domain_budget_exhausted", "can_retry": True},
        )
        registry.register(domain)
        ctx, _trace_events = composition_context(body, registry, max_candidates=3)

        result = collect_resource({"item": "dirt", "count": 2}, ctx)

        self.assertFalse(result.success, result)
        self.assertEqual(result.reason, "partial_budget_exhausted")
        self.assertEqual(result.metrics["collected_delta"], 0)

    def test_collect_resource_preserves_goal_total_and_log_family(self):
        body = FakeBody()
        body.inventory_counts = {"oak_log": 18}
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        domain, calls = resource_domain_tool(
            body,
            {"success": False, "reason": "resource_candidate_domain_exhausted", "can_retry": True},
        )
        registry.register(domain)
        ctx, _trace_events = composition_context(body, registry, max_candidates=1)
        ctx.weld_context.goal_text = "collect 64 logs"

        result = collect_resource({"item": "oak_log", "count": 46}, ctx)

        self.assertFalse(result.success, result)
        self.assertEqual(result.metrics["requested_count"], 46)
        self.assertEqual(result.metrics["goal_target_count"], 64)
        self.assertEqual(result.metrics["target_count"], 64)
        self.assertEqual(result.metrics["remaining_count"], 46)
        self.assertEqual(result.metrics["requested_item"], "logs")
        self.assertEqual(
            calls[0]["block_types"],
            ["oak_log", "spruce_log", "birch_log", "jungle_log", "acacia_log", "dark_oak_log"],
        )

    def test_collect_resource_does_not_widen_specific_log_goal(self):
        body = FakeBody()
        registry = ToolRegistry()
        register_inventory_tools(registry, body)
        domain, calls = resource_domain_tool(
            body,
            {"success": False, "reason": "resource_candidate_domain_exhausted", "can_retry": True},
        )
        registry.register(domain)
        ctx, _trace_events = composition_context(body, registry, max_candidates=1)
        ctx.weld_context.goal_text = "collect 64 oak_log"

        result = collect_resource({"item": "logs", "count": 64}, ctx)

        self.assertFalse(result.success, result)
        self.assertEqual(result.metrics["requested_item"], "oak_log")
        self.assertEqual(calls[0]["block_types"], ["oak_log"])

if __name__ == "__main__":
    unittest.main()
