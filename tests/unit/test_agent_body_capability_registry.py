import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from minebot.app.body_capability_tools import (
    BODY_CAPABILITY_DEBT,
    BODY_PRIMITIVE_CLOSURE,
    BODY_TRANSACTION_CLOSURE,
    _read_nearby_blocks_tool,
)
from minebot.app.body_tool_catalog import (
    BODY_OBJECTIVE_PROVIDER_TABLE,
    CanonicalBodyToolCatalog,
    canonical_body_tool_names,
)
from minebot.app.phase1_runtime import (
    Phase1RuntimeConfig,
    _java_break_collect,
    build_phase1_agent_runtime,
    build_phase1_registry,
    tool_manifest,
)
from minebot.app.runner import tool_is_enabled, _model_tool_payload
from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.app.tasks import TaskWorkspace
from minebot.body import (
    BlockApproachTransactions,
    BlockWork,
    CombatTransactions,
    ContainerTransactions,
    ExplorationTransactions,
    FurnaceTransactions,
    InteractionTransactions,
    InventoryTransactions,
    LifecycleTransactions,
    NavigationTransactions,
    PickupTransactions,
    ResourceCollectionTransactions,
    UseTransactions,
)
from minebot.brain.lifecycle import LifecycleState
from minebot.brain.modes import ModeRuntime
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import Region, StructureRiskAssessment, StructureRiskLevel, ToolResult, PerceptionResult
from minebot.game import ScarpetBody
from minebot.game.governance import GovernancePolicy
from minebot.game.composite_body import CompositeBody
from minebot.game.java_body import JavaBody
from minebot.game.java_body_adapter import JavaBodyClient
from tests.unit.test_java_body_adapter import FakeBodyServer


TRANSACTION_CLASSES = (
    BlockApproachTransactions,
    BlockWork,
    CombatTransactions,
    ContainerTransactions,
    ExplorationTransactions,
    FurnaceTransactions,
    InteractionTransactions,
    InventoryTransactions,
    LifecycleTransactions,
    NavigationTransactions,
    PickupTransactions,
    ResourceCollectionTransactions,
    UseTransactions,
)


def _body():
    body = Mock()
    body.bot_name = "Bot1"
    return body


def _registry():
    return build_phase1_registry(
        _body(),
        Phase1RuntimeConfig(
            natural_region=Region("test", (-64, -64, -64), (64, 320, 64))
        ),
    )


class BodyCapabilityRegistryClosureTests(unittest.TestCase):
    def test_nearby_blocks_exposes_decision_sample_and_exact_detail_path(self):
        body = _body()
        blocks = [{"x": i, "y": 64, "z": 0, "type": "minecraft:stone"} for i in range(12)]
        body.perceive.return_value = PerceptionResult(
            bot="Bot1", scope="nearbyBlocks", type="perception", ok=True, complete=False,
            data={"blocks": blocks, "center": [0, 64, 0], "nextStart": 12},
        )
        tool = _read_nearby_blocks_tool(body)
        result = tool.callable({})
        payload = _model_tool_payload(tool.name, result.to_payload(), trace_ref="nearby", projector=tool.projector)
        summary = payload["summary"]
        self.assertEqual(summary["blocks"], blocks[:8])
        self.assertEqual(summary["block_count"], 12)
        self.assertFalse(summary["blocks_complete"])
        self.assertEqual(summary["detail_path"], ["metrics", "data", "blocks"])
        self.assertEqual(result.metrics["data"]["blocks"], blocks)

    def test_java_resource_scanner_is_read_only_and_never_referenced_by_action_executors(self):
        java_root = Path("minecraft/server/body/src/main/java/dev/minebot/body")
        action_sources = list((java_root / "action").glob("*.java"))
        scanner_tokens = ("dev.minebot.body.search", "LoadedBlockScanner", "scanner.scan(")
        offenders = [
            source.as_posix()
            for source in action_sources
            if any(token in source.read_text(encoding="utf-8") for token in scanner_tokens)
        ]
        self.assertEqual(offenders, [])

        channel = (java_root / "protocol" / "FakePlayerBodyChannel.java").read_text(
            encoding="utf-8"
        )
        find_start = channel.index("private void handleFindBlocks(")
        find_end = channel.index("\n    private ", find_start + 1)
        self.assertEqual(channel.count("scanner.scan("), 1)
        self.assertIn("scanner.scan(", channel[find_start:find_end])

    def test_java_entity_actions_accept_only_agent_resolved_uuid(self):
        java_root = Path("minecraft/server/body/src/main/java/dev/minebot/body")
        channel = (java_root / "protocol" / "FakePlayerBodyChannel.java").read_text(
            encoding="utf-8"
        )
        target_source_start = channel.index("private EngageExecutor.TargetSource entityTargetSource(")
        target_source_end = channel.index("\n    private ", target_source_start + 1)
        target_source = channel[target_source_start:target_source_end]
        self.assertNotIn("nearest_hostile", target_source)
        self.assertNotIn("closestLookup", target_source)
        self.assertIn("targetUuid.equals(entity.getUUID().toString())", target_source)

        for handler_name in ("handleFollowEntity", "handleEngageEntity"):
            start = channel.index(f"private void {handler_name}(")
            end = channel.index("\n    private ", start + 1)
            handler = channel[start:end]
            self.assertIn('requiredUuidString(request, "target_uuid")', handler)
            self.assertIn("rejectLegacyEntitySelector(request)", handler)

        for executor_name in ("EngageExecutor.java", "FollowExecutor.java"):
            executor = (java_root / "action" / executor_name).read_text(encoding="utf-8")
            self.assertNotIn("targetSpec", executor)
            self.assertIn("targetUuid", executor)

    def test_java_designated_collect_projects_missing_tool_as_strategy_terminal(self):
        body = Mock()
        body.execute.return_value = Mock(
            ok=False,
            accepted=True,
            complete=True,
            error="no_path",
            data={
                "phase_reason": "missing_required_tool",
                "attempts_consumed": 1,
                "budget_remaining": 0,
            },
        )

        result = _java_break_collect(
            body,
            (5, 63, 5),
            expected_block_id="deepslate_diamond_ore",
            expected_item_ids=("diamond",),
            excavation_budget=0,
            timeout_s=10.0,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "missing_required_tool")
        self.assertFalse(result.can_retry)
        self.assertEqual(result.metrics["phase_reason"], "missing_required_tool")

    def test_every_base_player_intent_is_registered_through_the_canonical_catalog(self):
        registry = _registry()

        self.assertEqual(tuple(registry.names()), canonical_body_tool_names(stage="base"))
        bindings = registry.canonical_bindings()
        self.assertEqual(len(bindings), 43)
        self.assertEqual(
            {binding.intent for binding in bindings},
            {f"body:{name}" for name in canonical_body_tool_names(stage="base")},
        )
        self.assertTrue(all(row["intent"] for row in tool_manifest(registry)))
        self.assertTrue(all(row["implementation"] for row in tool_manifest(registry)))

    def test_production_runtime_completes_all_45_player_intents(self):
        parts = build_phase1_agent_runtime(
            body=_body(),
            goal_text="collect wood",
            model_provider=None,
            config=Phase1RuntimeConfig(
                natural_region=Region("test", (-64, -64, -64), (64, 320, 64))
            ),
        )
        try:
            body_bindings = [
                binding
                for binding in parts.registry.canonical_bindings()
                if binding.intent.startswith("body:")
            ]
            self.assertEqual(len(body_bindings), 45)
            self.assertEqual(
                {binding.tool_name for binding in body_bindings},
                set(canonical_body_tool_names()),
            )
        finally:
            parts.runtime.close()

    def test_governance_confirmation_is_support_tool_not_a_duplicate_player_intent(self):
        class AmbiguousRisk:
            def assess(self, pos, block_type, context, *, bot_placements=None):
                return StructureRiskAssessment(
                    pos=pos,
                    block_type=block_type,
                    level=StructureRiskLevel.AMBIGUOUS,
                    score=0.4,
                    complete=True,
                    sampled_cells=63,
                    signals=("local_axis_symmetry",),
                )

        body = _body()
        policy = GovernancePolicy(
            structure_risk_assessor=AmbiguousRisk(),
            require_structure_assessment=True,
            bot_name="Bot1",
        )
        parts = build_phase1_agent_runtime(
            body=body,
            goal_text="collect stone",
            model_provider=None,
            config=Phase1RuntimeConfig(
                natural_region=Region("test", (-64, -64, -64), (64, 320, 64)),
                governance_policy=policy,
            ),
        )
        try:
            decision = policy.can_break(
                (0, 64, 0),
                "stone",
                "direct",
                bot="Bot1",
            )
            challenge_id = decision.details["governance_confirmation"]["challenge_id"]
            for _ in range(3):
                parts.authority.next_generation()
            confirmation = parts.registry.get("confirm_governance_scope").callable(
                {
                    "challenge_id": challenge_id,
                    "purpose": "collect natural stone for the active goal",
                }
            )
            allowed = policy.can_break((1, 64, 0), "stone", "direct", bot="Bot1")

            self.assertTrue(confirmation.success)
            self.assertEqual(confirmation.reason, "governance_scope_confirmed")
            self.assertTrue(allowed.allowed)
            self.assertEqual(allowed.reason, "allowed_governance_scope")
            self.assertNotIn(
                "body:confirm_governance_scope",
                {binding.intent for binding in parts.registry.canonical_bindings()},
            )
            manifest = {row["name"]: row for row in tool_manifest(parts.registry)}
            self.assertEqual(manifest["confirm_governance_scope"]["source"], "governance.policy")
            self.assertFalse(manifest["confirm_governance_scope"]["body_mutating"])
        finally:
            parts.runtime.close()

    def test_governance_confirmation_is_invalidated_when_durable_task_changes(self):
        class AmbiguousRisk:
            def assess(self, pos, block_type, context, *, bot_placements=None):
                return StructureRiskAssessment(
                    pos=pos,
                    block_type=block_type,
                    level=StructureRiskLevel.AMBIGUOUS,
                    score=0.4,
                    complete=True,
                    sampled_cells=63,
                    signals=("local_axis_symmetry",),
                )

        store = RuntimeStateStore(":memory:")
        workspace = TaskWorkspace(store, RuntimeScope("server", "world", "Bot1"))
        workspace.start("collect stone", source="test")
        body = _body()
        policy = GovernancePolicy(
            structure_risk_assessor=AmbiguousRisk(),
            require_structure_assessment=True,
            bot_name="Bot1",
        )
        parts = build_phase1_agent_runtime(
            body=body,
            goal_text="collect stone",
            model_provider=None,
            config=Phase1RuntimeConfig(
                natural_region=Region("test", (-64, -64, -64), (64, 320, 64)),
                governance_policy=policy,
                task_workspace=workspace,
            ),
        )
        try:
            first = policy.can_break((0, 64, 0), "stone", "direct", bot="Bot1")
            confirmed = policy.confirm_scope(
                first.details["governance_confirmation"]["challenge_id"],
                "collect natural stone for the active task",
            )
            self.assertTrue(confirmed["confirmed"])
            self.assertTrue(policy.can_break((1, 64, 0), "stone", "direct", bot="Bot1").allowed)

            workspace.replace("collect sand", source="test")
            after_replacement = policy.can_break((1, 64, 0), "stone", "direct", bot="Bot1")

            self.assertFalse(after_replacement.allowed)
            self.assertEqual(after_replacement.reason, "governance_confirmation_required")
        finally:
            parts.runtime.close()
            store.close()

    def test_uncatalogued_player_tool_is_rejected_before_registration(self):
        registry = ToolRegistry()
        catalog = CanonicalBodyToolCatalog(registry, "scarpet")
        tool = RegisteredTool(
            "scarpet_special_move",
            "provider-specific sibling",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda _params: ToolResult(True, "done", False),
            ToolSidecar("scarpet_special_move", mutating=True),
        )

        with self.assertRaisesRegex(ValueError, "uncatalogued"):
            catalog.register(tool)

    def test_registry_rejects_two_tools_for_one_canonical_intent(self):
        registry = ToolRegistry()
        first = RegisteredTool(
            "first_move",
            "first",
            {"type": "object", "properties": {}},
            lambda _params: ToolResult(True, "done", False),
            ToolSidecar("first_move", mutating=True),
        )
        second = RegisteredTool(
            "second_move",
            "second",
            {"type": "object", "properties": {}},
            lambda _params: ToolResult(True, "done", False),
            ToolSidecar("second_move", mutating=True),
        )
        registry.register(first, intent="body:move", implementation="first")

        with self.assertRaisesRegex(ValueError, "duplicate canonical intent"):
            registry.register(second, intent="body:move", implementation="second")

    def test_registry_keeps_internal_tools_callable_but_out_of_model_view(self):
        registry = ToolRegistry()
        visible = RegisteredTool(
            "visible_tool",
            "visible",
            {"type": "object", "properties": {}},
            lambda _params: ToolResult(True, "visible", False),
            ToolSidecar("visible_tool", mutating=False),
        )
        internal = RegisteredTool(
            "internal_leaf",
            "internal",
            {"type": "object", "properties": {}},
            lambda _params: ToolResult(True, "internal", False),
            ToolSidecar("internal_leaf", mutating=False, model_visible=False),
        )
        registry.register(visible)
        registry.register(internal)

        self.assertEqual(registry.names(), ["visible_tool", "internal_leaf"])
        self.assertEqual(registry.model_names(), ["visible_tool"])
        self.assertEqual(
            [tool["name"] for tool in registry.framework_tools()],
            ["visible_tool"],
        )
        self.assertEqual(registry.get("internal_leaf").callable({}).reason, "internal")
        manifest = {row["name"]: row for row in tool_manifest(registry)}
        self.assertTrue(manifest["visible_tool"]["model_visible"])
        self.assertFalse(manifest["internal_leaf"]["model_visible"])

    def test_every_public_body_transaction_has_an_explicit_closure_disposition(self):
        public_transactions = {
            f"{cls.__name__}.{name}"
            for cls in TRANSACTION_CLASSES
            for name, value in cls.__dict__.items()
            if not name.startswith("_") and callable(value)
        }

        self.assertEqual(public_transactions, set(BODY_TRANSACTION_CLOSURE))

    def test_every_tool_owned_transaction_resolves_to_the_shared_registry(self):
        registry = _registry()
        registered = set(registry.names())

        for capability, closure in BODY_TRANSACTION_CLOSURE.items():
            with self.subTest(capability=capability):
                self.assertIn(closure.disposition, {"tool", "owned", "internal", "debt"})
                self.assertTrue(closure.note)
                if closure.disposition in {"tool", "owned"}:
                    self.assertTrue(closure.owners)
                    self.assertTrue(set(closure.owners).issubset(registered))
                elif closure.disposition == "debt":
                    self.assertFalse(closure.owners)

        self.assertTrue(BODY_CAPABILITY_DEBT)
        self.assertTrue(all(reason.strip() for reason in BODY_CAPABILITY_DEBT.values()))

    def test_every_public_scarpet_body_primitive_has_an_explicit_owner_or_debt(self):
        public_primitives = {
            f"ScarpetBody.{name}"
            for name, value in ScarpetBody.__dict__.items()
            if not name.startswith("_") and callable(value)
        }
        registry = _registry()
        registered = set(registry.names())

        self.assertEqual(public_primitives, set(BODY_PRIMITIVE_CLOSURE))
        for primitive, closure in BODY_PRIMITIVE_CLOSURE.items():
            with self.subTest(primitive=primitive):
                self.assertTrue(closure.note)
                if closure.disposition == "owned":
                    self.assertTrue(set(closure.owners).issubset(registered))
                if closure.disposition == "debt":
                    self.assertFalse(closure.owners)

    def test_registry_exposes_the_complete_safe_capability_surface(self):
        registry = _registry()
        expected = {
            "explore_for",
            "move_away",
            "go_to_player",
            "follow_player",
            "search_for_entity",
            "give_player",
            "consume_item",
            "discard_item",
            "transfer_container_item",
            "read_container",
            "clear_furnace",
            "go_to_bed",
            "set_openable_state",
            "till_farmland",
            "sow_crop",
            "harvest_and_resow",
            "set_switch_state",
            "use_item",
            "use_on_entity",
            "use_on_block",
            "place_block",
            "place_here",
            "dig_down",
            "dig_up",
            "pickup_items",
            "read_block",
            "read_nearby_blocks",
            "read_nearby_entities",
            "read_recipe",
        }

        self.assertTrue(expected.issubset(set(registry.names())))
        manifest = {row["name"]: row for row in tool_manifest(registry)}
        for name in expected:
            with self.subTest(tool=name):
                if name == "dig_down":
                    self.assertEqual(manifest[name]["source"], "java_body")
                else:
                    self.assertTrue(manifest[name]["source"].startswith("body."))
                self.assertTrue(manifest[name]["permission"])
                self.assertTrue(manifest[name]["body_scope"])
                self.assertTrue(manifest[name]["terminal_truth"])

    def test_exploration_cursor_contract_requires_the_same_target_descriptor(self):
        tool = _registry().get("explore_for")

        self.assertIn("target descriptor must remain unchanged", tool.description)
        cursor_schema = tool.input_schema["properties"]["resume_cursor"]
        self.assertIn("exact target descriptor", cursor_schema["description"])
        self.assertEqual(
            cursor_schema["required"],
            ["query_signature", "dimension", "coverage_revision"],
        )

    def test_java_recipe_tool_accepts_no_uncertainty_as_complete_truth(self):
        server = FakeBodyServer()
        body = JavaBody(JavaBodyClient("Bot", lambda: server), "Bot")
        registry = build_phase1_registry(
            body,
            Phase1RuntimeConfig(
                natural_region=Region("test", (-64, -64, -64), (64, 320, 64)),
                body_provider="java",
            ),
        )

        result = registry.get("read_recipe").callable({"item": "oak_planks"})

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "recipe_read")
        self.assertEqual(result.metrics["uncertainty"], [])
        self.assertEqual(result.metrics["data"]["variantCount"], 1)

    def test_resource_tool_descriptions_expose_the_capability_hierarchy(self):
        registry = _registry()

        search = registry.get("search_for_block").description
        approach = registry.get("get_to_block").description
        move = registry.get("move_to").description
        mine = registry.get("mine_block_collect").description

        self.assertIn("without moving", search)
        self.assertIn("does not verify line of sight", search)
        self.assertIn("Approach one usable block", approach)
        self.assertIn("does not mine", approach)
        self.assertIn("generic spatial travel", move)
        self.assertIn("never invent a coordinate", move)
        self.assertIn("go_to_surface", move)
        self.assertIn("does not prove line of sight", move)
        self.assertIn("one deliberately selected exact coordinate", mine)
        self.assertIn("Never use it to acquire a resource quantity", mine)
        self.assertIn("collect_resource owns discovery, alternative targets", mine)

        self.assertEqual(
            {"search_for_block", "get_to_block", "move_to", "mine_block_collect"}
            - set(registry.names()),
            set(),
        )

    def test_body_epoch_tools_project_decision_grade_evidence(self):
        registry = _registry()
        resource = registry.get("collect_block_domain")
        explore = registry.get("explore_for")

        self.assertIsNotNone(resource.projector)
        resource_summary = resource.projector(
            "new_physics_failure",
            {
                "candidate_attempts_consumed": 4,
                "candidate_budget_remaining": 8,
                "remaining_delta": 2,
                "last_failure": {
                    "reason": "new_physics_failure",
                    "metrics": {"failure_detail": "replan_budget_exhausted:arrival_unstable"},
                },
            },
        )
        self.assertEqual(resource_summary["candidate_attempts_consumed"], 4)
        self.assertEqual(resource_summary["candidate_budget_remaining"], 8)
        self.assertEqual(resource_summary["remaining_delta"], 2)
        self.assertEqual(resource_summary["last_failure"]["reason"], "new_physics_failure")
        self.assertEqual(resource_summary["last_failure"]["evidence"]["failure_detail"],
                         "replan_budget_exhausted:arrival_unstable")

        self.assertIsNotNone(explore.projector)
        explore_summary = explore.projector(
            "unloaded_boundary",
            {
                "budget": {"max_regions": 4, "regions_consumed": 2},
                "blocks": [{"pos": [1, 64, 1]}],
                "entities": [],
                "candidate_failures": [{"reason": "unloaded_boundary"}],
                "continuation": {"tool": "explore_for"},
            },
        )
        self.assertEqual(explore_summary["budget"]["regions_consumed"], 2)
        self.assertEqual(explore_summary["candidate_count"], 1)
        self.assertTrue(explore_summary["unloaded_boundary"])
        self.assertEqual(explore_summary["continuation"]["tool"], "explore_for")

    def test_all_modes_receive_the_same_shared_tool_pool(self):
        registry = _registry()
        expected = set(registry.names())

        for situational in ("normal", "mobility", "engage", "survival", "death"):
            mode = ModeRuntime()
            mode.situational = situational
            profile = mode.profile_for(LifecycleState.ACTIVE)
            visible = {
                name
                for name in registry.names()
                if tool_is_enabled(registry.sidecar(name), profile, {})
            }
            with self.subTest(situational=situational):
                self.assertEqual(visible, expected)

    def test_body_effect_metadata_covers_leaf_led_physical_tools(self):
        registry = _registry()

        self.assertFalse(registry.sidecar("search_for_block").can_mutate_body)
        self.assertTrue(registry.sidecar("get_to_block").can_mutate_body)
        self.assertTrue(registry.sidecar("explore_for").can_mutate_body)
        self.assertTrue(registry.sidecar("move_to").can_mutate_body)
        self.assertFalse(registry.sidecar("read_block").can_mutate_body)
        self.assertFalse(registry.sidecar("read_inventory").can_mutate_body)

    def test_registered_adapter_calls_the_existing_body_transaction(self):
        expected = ToolResult(True, "completed", False, metrics={"item_delta": 1})
        with patch.object(UseTransactions, "consume_item", return_value=expected) as consume:
            registry = _registry()
            result = registry.get("consume_item").callable(
                {"item": "minecraft:bread", "use_ticks": 40, "timeout_s": 5.0}
            )

        self.assertIs(result, expected)
        consume.assert_called_once_with(
            item="minecraft:bread",
            use_ticks=40,
            timeout_s=5.0,
        )


if __name__ == "__main__":
    unittest.main()


class JavaBodyProviderRegistryTests(unittest.TestCase):
    def test_all_providers_expose_identical_canonical_names_descriptions_and_schemas(self):
        scarpet = _registry()
        composite_body = CompositeBody(
            _body(),
            JavaBody(JavaBodyClient("Bot1", lambda: FakeBodyServer()), "Bot1"),
        )
        composite = build_phase1_registry(
            composite_body,
            Phase1RuntimeConfig(
                natural_region=Region("test", (-64, -64, -64), (64, 320, 64)),
                body_provider="composite",
            ),
        )
        java_body = JavaBody(JavaBodyClient("Bot1", lambda: FakeBodyServer()), "Bot1")
        java = build_phase1_registry(
            java_body,
            Phase1RuntimeConfig(
                natural_region=Region("test", (-64, -64, -64), (64, 320, 64)),
                body_provider="java",
            ),
        )

        self.assertEqual(scarpet.framework_tools(), composite.framework_tools())
        self.assertEqual(scarpet.framework_tools(), java.framework_tools())
        for registry in (scarpet, composite, java):
            names = set(registry.names())
            self.assertNotIn("navigate_to", names)
            self.assertNotIn("collect_block", names)
            self.assertFalse(any("scarpet" in name or "java" in name for name in names))

    def test_provider_table_is_the_only_direct_objective_owner_choice(self):
        self.assertEqual(
            set(BODY_OBJECTIVE_PROVIDER_TABLE),
            {"scarpet", "java", "composite"},
        )
        self.assertTrue(
            all(
                set(bindings) == {
                    "move_to",
                    "follow_entity",
                    "go_to_surface",
                    "dig_down",
                    "collect_block_domain",
                }
                for bindings in BODY_OBJECTIVE_PROVIDER_TABLE.values()
            )
        )
        self.assertEqual(set(BODY_OBJECTIVE_PROVIDER_TABLE["scarpet"].values()), {"scarpet"})
        self.assertEqual(set(BODY_OBJECTIVE_PROVIDER_TABLE["java"].values()), {"java"})
        self.assertEqual(set(BODY_OBJECTIVE_PROVIDER_TABLE["composite"].values()), {"java"})

    def test_composite_has_one_canonical_navigation_and_collection_surface(self):
        scarpet = _body()
        java = JavaBody(JavaBodyClient("Bot1", lambda: FakeBodyServer()), "Bot1")
        body = CompositeBody(scarpet, java)
        registry = build_phase1_registry(
            body,
            Phase1RuntimeConfig(
                natural_region=Region("test", (-64, -64, -64), (64, 320, 64)),
                body_provider="composite",
            ),
        )
        names = set(registry.names())
        self.assertIn("collect_block_domain", names)
        self.assertIn("mine_block_collect", names)
        self.assertIn("get_to_block", names)
        self.assertIn("move_to", names)
        self.assertNotIn("navigate_to", names)
        self.assertNotIn("collect_block", names)
        self.assertEqual(registry.sidecar("move_to").source, "java_body")
        self.assertEqual(
            registry.sidecar("collect_block_domain").source,
            "body.resource_collection",
        )

    def test_default_registry_uses_java_after_production_retirement(self):
        registry = _registry()
        names = set(registry.names())
        self.assertNotIn("navigate_to", names)
        self.assertNotIn("collect_block", names)
        self.assertEqual(registry.sidecar("move_to").source, "java_body")
        self.assertEqual(
            registry.sidecar("collect_block_domain").source,
            "body.resource_collection",
        )

    def test_resource_process_leaf_is_internal_but_remains_composition_callable(self):
        parts = build_phase1_agent_runtime(
            body=_body(),
            goal_text="collect 3 raw iron",
            model_provider=None,
            config=Phase1RuntimeConfig(
                natural_region=Region("test", (-64, -64, -64), (64, 320, 64))
            ),
        )
        try:
            registered = set(parts.registry.names())
            model_visible = set(parts.registry.model_names())
            sdk_tools = {tool.name for tool in parts.runtime.agent.tools}

            self.assertIn("collect_block_domain", registered)
            self.assertNotIn("collect_block_domain", model_visible)
            self.assertNotIn("collect_block_domain", sdk_tools)
            self.assertIn("collect_resource", model_visible)
            self.assertIn("collect_resource", sdk_tools)
            self.assertEqual(
                set(parts.registry.get("collect_resource").input_schema["properties"]),
                {"item", "count"},
            )
            manifest = {row["name"]: row for row in tool_manifest(parts.registry)}
            self.assertFalse(manifest["collect_block_domain"]["model_visible"])
            self.assertTrue(manifest["collect_resource"]["model_visible"])
        finally:
            parts.runtime.close()
