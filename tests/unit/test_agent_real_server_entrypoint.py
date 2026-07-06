import asyncio
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from minebot.app.phase1_runtime import Phase1RuntimeConfig, _phase1_recovery_handler, build_phase1_registry, tool_manifest
from minebot.app.runner import AgentRuntime
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleController
from minebot.brain.modes import AgentSignal, ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import ToolRegistry
from minebot.app.real_server_session import (
    RealServerConfigError,
    _collect_goal_driver,
    _ensure_scarpet_global_app,
    _poll_chat_commands,
    _run_interactive_loop,
    evaluate_terminal_truth,
    main,
    parse_collect_target,
    parse_session_command,
    real_server_config_from_env,
)
from minebot.app.resource_runtime import ResourceRuntimeConfig
from minebot.app.session import SessionCommandKind
from minebot.app.session import SessionStep
from minebot.brain.lifecycle import LifecycleState
from minebot.contract import BodyState, InventorySlot, PerceptionResult, Region, Result
from minebot.contract import Event


class AgentRealServerEntrypointTests(unittest.TestCase):
    def test_config_requires_explicit_real_server_env(self):
        with self.assertRaises(RealServerConfigError) as ctx:
            real_server_config_from_env({})

        self.assertIn("MINEBOT_REAL_RCON_HOST", str(ctx.exception))

    def test_config_parses_region_and_log_path_without_exposing_secret(self):
        cfg = real_server_config_from_env(
            {
                "MINEBOT_REAL_RCON_HOST": "example.invalid",
                "MINEBOT_REAL_RCON_PORT": "25576",
                "MINEBOT_REAL_RCON_PASSWORD": "secret",
                "MINEBOT_REAL_BOT": "MineBot",
                "MINEBOT_REAL_RCON_TIMEOUT": "7",
                "MINEBOT_REAL_NATURAL_REGION": "-1,2,-3,4,5,6",
                "MINEBOT_REAL_RECOVERY_RESPAWN_POS": "7,80,9",
                "MINEBOT_AGENT_LOG_PATH": "logs/custom.jsonl",
                "MINEBOT_AGENT_LANGUAGE": "Chinese",
            }
        )

        self.assertEqual(cfg.rcon.host, "example.invalid")
        self.assertEqual(cfg.rcon.port, 25576)
        self.assertEqual(cfg.rcon.timeout_s, 7)
        self.assertEqual(cfg.bot_name, "MineBot")
        self.assertEqual(cfg.natural_region.min_pos, (-1, 2, -3))
        self.assertEqual(cfg.natural_region.max_pos, (4, 5, 6))
        self.assertEqual(cfg.recovery_respawn_pos, (7, 80, 9))
        self.assertEqual(cfg.log_path, Path("logs/custom.jsonl"))
        self.assertEqual(cfg.language, "Chinese")

    def test_config_rejects_bad_recovery_respawn_pos(self):
        with self.assertRaises(RealServerConfigError) as ctx:
            real_server_config_from_env(
                {
                    "MINEBOT_REAL_RCON_HOST": "example.invalid",
                    "MINEBOT_REAL_RCON_PORT": "25576",
                    "MINEBOT_REAL_RCON_PASSWORD": "secret",
                    "MINEBOT_REAL_BOT": "MineBot",
                    "MINEBOT_REAL_NATURAL_REGION": "-1,2,-3,4,5,6",
                    "MINEBOT_REAL_RECOVERY_RESPAWN_POS": "1,2",
                }
            )

        self.assertIn("MINEBOT_REAL_RECOVERY_RESPAWN_POS", str(ctx.exception))

    def test_provider_manifest_is_written_to_real_server_log(self):
        from minebot.app.model_provider import ModelProviderRegistry
        from minebot.brain.provider import ProviderConfig
        from minebot.app.observability import JsonlObservationSink
        from minebot.app.runner import RuntimeTrace
        import json
        import tempfile

        provider = ModelProviderRegistry(
            [
                ProviderConfig(
                    name="primary",
                    kind="openai_chat",
                    model="glm-5.2",
                    base_url="https://maas-openapi.wanjiedata.com/api/v1/chat/completions",
                    api_key_env="ANTHROPIC_AUTH_TOKEN",
                )
            ],
            env={"ANTHROPIC_AUTH_TOKEN": "secret"},
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            trace = RuntimeTrace(session_id="Bot", sink=JsonlObservationSink(path))
            trace.emit(
                "provider_manifest",
                default_route=provider.default,
                language="Chinese",
                providers=provider.trace_configs(),
            )
            trace.close()
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(rows[0]["event"], "provider_manifest")
        self.assertEqual(rows[0]["default_route"], "primary")
        self.assertEqual(rows[0]["language"], "Chinese")
        self.assertEqual(rows[0]["providers"][0]["base_url_host"], "https://maas-openapi.wanjiedata.com")
        self.assertEqual(rows[0]["providers"][0]["api_key_env"], "ANTHROPIC_AUTH_TOKEN")

    def test_main_exits_before_connecting_when_real_env_missing(self):
        clean_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("MINEBOT_REAL_")
        }
        with patch.dict(os.environ, clean_env, clear=True):
            code = main(["collect 64 logs", "--max-steps", "1"])

        self.assertEqual(code, 2)

    def test_real_server_entrypoint_loads_scarpet_app_global_before_state_probe(self):
        rcon = ScriptLoadRcon(
            [
                "minebot app reloaded",
                _state_envelope("MineBotReal"),
            ]
        )

        _ensure_scarpet_global_app(rcon, "MineBotReal")

        self.assertEqual(
            rcon.commands,
            [
                "script load minebot global",
                "script in minebot run minebot_state('MineBotReal')",
            ],
        )

    def test_real_server_entrypoint_global_load_does_not_reset_or_seed_world(self):
        rcon = ScriptLoadRcon(
            [
                "minebot app reloaded",
                _state_envelope("MineBotReal"),
            ]
        )

        _ensure_scarpet_global_app(rcon, "MineBotReal")

        self.assertEqual(
            rcon.commands,
            [
                "script load minebot global",
                "script in minebot run minebot_state('MineBotReal')",
            ],
        )
        self.assertFalse(any("minebot_reset" in command for command in rcon.commands))
        self.assertFalse(any(command.startswith("setblock ") for command in rcon.commands))

    def test_interactive_flag_still_requires_explicit_real_env_before_connecting(self):
        clean_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("MINEBOT_REAL_")
        }
        with patch.dict(os.environ, clean_env, clear=True):
            code = main(["collect 64 logs", "--interactive", "--max-steps", "1"])

        self.assertEqual(code, 2)

    def test_parse_interactive_session_commands(self):
        cases = [
            ("/pause wait there", SessionCommandKind.PAUSE, "", "wait there"),
            ("/continue now go left", SessionCommandKind.CONTINUE, "now go left", "user_continue"),
            ("/goal collect 64 sand", SessionCommandKind.REPLACE_GOAL, "collect 64 sand", "goal_replaced"),
            ("/cancel done", SessionCommandKind.CANCEL, "", "done"),
            ("你现在先看一下背包", SessionCommandKind.MESSAGE, "你现在先看一下背包", "user_message"),
        ]

        for raw, kind, text, reason in cases:
            with self.subTest(raw=raw):
                command = parse_session_command(raw)
                self.assertIsNotNone(command)
                self.assertEqual(command.kind, kind)
                self.assertEqual(command.text, text)
                self.assertEqual(command.reason, reason)

        self.assertIsNone(parse_session_command("   "))

    def test_parse_collect_target_uses_shared_resource_equivalence(self):
        target = parse_collect_target("collect 64 logs")

        self.assertIsNotNone(target)
        self.assertEqual(target.count, 64)
        self.assertIn("spruce_log", target.inventory_items)
        self.assertIn("birch_log", target.inventory_items)

    def test_collect_goal_driver_routes_collect_goal_through_canonical_transaction(self):
        body = HarnessBody()
        context = AgentContext(system_prompt="sys", goal_text="collect 64 logs")
        lifecycle = LifecycleController()
        modes = ModeRuntime()
        authority = ProgressAuthority()
        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=context,
            lifecycle=lifecycle,
            mode_runtime=modes,
            authority=authority,
        )
        calls = []

        def drive_tool_once(tool_name, params, *, reason, extra_signals=None):
            calls.append((tool_name, params, reason, extra_signals))
            lifecycle.ready()
            lifecycle.start()
            return type(
                "Outcome",
                (),
                {"status": "completed_turn", "lifecycle": lifecycle.state, "message": "driven"},
            )()

        runtime.drive_tool_once = drive_tool_once  # type: ignore[method-assign]
        parts = type(
            "Parts",
            (),
            {
                "runtime": runtime,
                "registry": ToolRegistry(),
                "context": context,
                "lifecycle": lifecycle,
                "modes": modes,
                "authority": authority,
            },
        )()

        signal = AgentSignal.goal_started("collect 64 logs")
        step = _collect_goal_driver(parts, [signal])

        self.assertIsNotNone(step)
        self.assertEqual(step.status, "completed_turn")
        self.assertEqual(
            calls,
            [("collect_resource", {"item": "logs", "count": 64}, "canonical_collect_goal", [signal])],
        )

    def test_collect_goal_driver_ignores_non_collect_goal(self):
        body = HarnessBody()
        context = AgentContext(system_prompt="sys", goal_text="come here")
        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=context,
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )
        parts = type(
            "Parts",
            (),
            {
                "runtime": runtime,
                "registry": ToolRegistry(),
                "context": context,
                "lifecycle": runtime.lifecycle,
                "modes": runtime.mode_runtime,
                "authority": runtime.authority,
            },
        )()

        self.assertIsNone(_collect_goal_driver(parts, []))
        self.assertTrue(any(event["event"] == "goal_driver_skipped" for event in runtime.trace.snapshot()))

    def test_terminal_truth_succeeds_only_on_authoritative_inventory(self):
        body = InventoryBody({"spruce_log": 32, "birch_log": 32})
        final = SessionStep("completed_turn", LifecycleState.ACTIVE)

        truth = evaluate_terminal_truth(body, "collect 64 logs", final)

        self.assertTrue(truth.satisfied)
        self.assertEqual(truth.inventory_count, 64)
        self.assertEqual(truth.exit_code, 0)

    def test_terminal_truth_stays_successful_after_completion_stand_down(self):
        body = InventoryBody({"spruce_log": 32, "birch_log": 32})
        final = SessionStep("completed", LifecycleState.IDLE, "terminal_truth_satisfied")

        truth = evaluate_terminal_truth(body, "collect 64 logs", final)

        self.assertTrue(truth.satisfied)
        self.assertEqual(truth.lifecycle, "idle")
        self.assertEqual(truth.exit_code, 0)

    def test_terminal_truth_fails_when_session_runs_but_inventory_short(self):
        body = InventoryBody({"oak_log": 12})
        final = SessionStep("completed_turn", LifecycleState.ACTIVE)

        truth = evaluate_terminal_truth(body, "collect 64 logs", final)

        self.assertFalse(truth.satisfied)
        self.assertEqual(truth.inventory_count, 12)
        self.assertEqual(truth.exit_code, 6)

    def test_terminal_truth_keeps_yield_nonzero(self):
        body = InventoryBody({"oak_log": 12})
        final = SessionStep("yielded", LifecycleState.YIELDED, "How should I continue?")

        truth = evaluate_terminal_truth(body, "collect 64 logs", final)

        self.assertFalse(truth.satisfied)
        self.assertEqual(truth.exit_code, 5)

    def test_terminal_truth_keeps_runtime_failure_nonzero(self):
        body = InventoryBody({"oak_log": 12})
        final = SessionStep("failed", LifecycleState.IDLE, "runtime_error:RuntimeError")

        truth = evaluate_terminal_truth(body, "collect 64 logs", final)

        self.assertFalse(truth.satisfied)
        self.assertEqual(truth.exit_code, 8)

    def test_default_resource_runtime_budget_can_attempt_sixty_four_collection(self):
        cfg = ResourceRuntimeConfig(natural_region=Region("test", (0, 0, 0), (1, 1, 1)))

        self.assertGreaterEqual(cfg.budget.max_candidates, 64)
        self.assertGreaterEqual(cfg.budget.max_mutating_calls, 64)

    def test_phase1_registry_exposes_formal_manifest_not_resource_only_subset(self):
        body = HarnessBody()
        registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=Region("test", (0, 0, 0), (16, 128, 16))))

        self.assertIn("read_state", registry.names())
        self.assertIn("read_inventory", registry.names())
        self.assertIn("move_to", registry.names())
        self.assertIn("go_to_surface", registry.names())
        self.assertIn("search_for_block", registry.names())
        self.assertIn("mine_block_collect", registry.names())
        self.assertIn("craft_item", registry.names())
        self.assertIn("equip_item", registry.names())

        manifest = tool_manifest(registry)
        by_name = {row["name"]: row for row in manifest}
        self.assertEqual(by_name["move_to"]["source"], "body.navigation")
        self.assertEqual(by_name["move_to"]["tool_type"], "navigation")
        self.assertEqual(by_name["go_to_surface"]["source"], "body.block_work")
        self.assertEqual(by_name["go_to_surface"]["tool_type"], "navigation")
        self.assertEqual(by_name["read_state"]["source"], "body.perception")
        self.assertEqual(by_name["search_for_block"]["tool_type"], "perception")
        self.assertEqual(by_name["mine_block_collect"]["tool_type"], "work")
        self.assertEqual(by_name["craft_item"]["source"], "body.inventory")
        self.assertEqual(by_name["craft_item"]["tool_type"], "inventory")
        self.assertEqual(by_name["craft_item"]["permission"], "craft")
        self.assertTrue(by_name["craft_item"]["mutating"])
        self.assertEqual(by_name["craft_item"]["body_scope"], ["inventory", "blocks"])
        self.assertEqual(by_name["craft_item"]["terminal_truth"], ["inventory", "ToolResult"])
        self.assertEqual(by_name["equip_item"]["source"], "body.inventory")
        self.assertEqual(by_name["equip_item"]["tool_type"], "inventory")
        self.assertEqual(by_name["equip_item"]["permission"], "equip")
        self.assertTrue(by_name["equip_item"]["mutating"])
        self.assertEqual(by_name["equip_item"]["body_scope"], ["inventory"])
        self.assertEqual(by_name["equip_item"]["terminal_truth"], ["inventory", "ToolResult"])

    def test_phase1_craft_item_reports_missing_materials_honestly(self):
        body = CraftToolBody(
            inventory_pages=[
                _inventory_page([_slot(0), _slot(41), _slot(42), _slot(43), _slot(44)]),
                _inventory_page([_slot(0), _slot(41), _slot(42), _slot(43), _slot(44)]),
            ],
            recipe_data={
                "minecraft:oak_planks": '[[[[oak_planks, 4, {count:4,id:"minecraft:oak_planks"}]], [[oak_log, oak_wood]], [shapeless]]]'
            },
        )
        registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=Region("test", (0, 0, 0), (16, 128, 16))))

        result = registry.get("craft_item").callable({"item": "minecraft:oak_planks", "count": 4})

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "craft_plan_not_available")
        self.assertIn("variant_failures", result.metrics)
        self.assertEqual(body.actions, [])

    def test_phase1_craft_item_invokes_inventory_transaction_for_non_table_recipe(self):
        body = CraftToolBody(
            inventory_pages=[
                _inventory_page([_slot(0), _slot(1), _slot(41), _slot(42), _slot(43), _slot(44)]),
                _inventory_page([_slot(0, "minecraft:oak_log", 1), _slot(1), _slot(41), _slot(42), _slot(43), _slot(44)]),
                _inventory_page([_slot(0, "minecraft:oak_log", 1), _slot(1)]),
                _inventory_page([_slot(0), _slot(1, "minecraft:oak_planks", 4)]),
                _inventory_page([_slot(0), _slot(1, "minecraft:oak_planks", 4), _slot(41), _slot(42), _slot(43), _slot(44)]),
            ],
            recipe_data={
                "minecraft:oak_planks": '[[[[oak_planks, 4, {count:4,id:"minecraft:oak_planks"}]], [[oak_log, oak_wood]], [shapeless]]]'
            },
        )
        registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=Region("test", (0, 0, 0), (16, 128, 16))))

        result = registry.get("craft_item").callable({"item": "minecraft:oak_planks", "count": 4})

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "completed")
        self.assertEqual([action.name for action in body.actions], ["craftItem"])
        self.assertEqual(body.actions[0].params["inputs"], [{"slot": 0, "item": "minecraft:oak_log", "count": 1}])
        self.assertEqual(body.actions[0].params["output"], {"slot": 1, "item": "minecraft:oak_planks", "count": 4})

    def test_phase1_equip_item_reports_missing_item_honestly(self):
        body = CraftToolBody(
            inventory_pages=[_inventory_page([_slot(0), _slot(36), _slot(37), _slot(38), _slot(39), _slot(40)])],
            recipe_data={},
        )
        registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=Region("test", (0, 0, 0), (16, 128, 16))))

        result = registry.get("equip_item").callable({"item": "minecraft:iron_pickaxe"})

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "item_not_available")
        self.assertEqual([action.name for action in body.actions], ["selectItem"])

    def test_phase1_equip_item_invokes_inventory_transaction(self):
        body = CraftToolBody(
            inventory_pages=[
                _inventory_page([_slot(0, "minecraft:iron_pickaxe", 1), _slot(36)]),
                _inventory_page([_slot(0), _slot(36, "minecraft:iron_pickaxe", 1)]),
            ],
            recipe_data={},
        )
        registry = build_phase1_registry(body, Phase1RuntimeConfig(natural_region=Region("test", (0, 0, 0), (16, 128, 16))))

        result = registry.get("equip_item").callable({"item": "minecraft:iron_pickaxe", "target": "mainhand"})

        self.assertTrue(result.success, result.to_payload())
        self.assertEqual(result.reason, "completed")
        self.assertEqual([action.name for action in body.actions], ["selectItem"])
        self.assertEqual(body.actions[0].params["item"], "minecraft:iron_pickaxe")

    def test_phase1_registry_uses_server_side_navigation_factory(self):
        from minebot.body.navigation import NavigationTransactions

        body = HarnessBody()
        cfg = Phase1RuntimeConfig(natural_region=Region("test", (0, 0, 0), (16, 128, 16)))
        original = NavigationTransactions.server_side

        with patch.object(NavigationTransactions, "server_side", wraps=original) as server_side:
            build_phase1_registry(body, cfg)

        server_side.assert_called_once()

    def test_phase1_registry_wires_navigation_and_combat_to_shared_authority(self):
        from minebot.body.combat import CombatTransactions
        from minebot.body.navigation import NavigationTransactions

        body = HarnessBody()
        cfg = Phase1RuntimeConfig(natural_region=Region("test", (0, 0, 0), (16, 128, 16)))
        authority = ProgressAuthority()

        original_combat_init = CombatTransactions.__init__
        combat_progress: list[ProgressAuthority] = []

        def capture_combat_init(self, body, *, progress=None):
            combat_progress.append(progress)
            original_combat_init(self, body, progress=progress)

        with (
            patch.object(NavigationTransactions, "server_side", wraps=NavigationTransactions.server_side) as server_side,
            patch.object(CombatTransactions, "__init__", capture_combat_init),
        ):
            build_phase1_registry(body, cfg, authority=authority)

        self.assertIs(server_side.call_args.kwargs["progress"], authority)
        self.assertEqual(combat_progress, [authority])

    def test_interactive_loop_terminal_truth_uses_replaced_current_goal(self):
        session = ReplacedGoalSession(
            steps=[
                SessionStep("completed_turn", LifecycleState.ACTIVE),
                SessionStep("completed_turn", LifecycleState.ACTIVE),
            ],
            goals=["collect 64 logs", "collect 64 sand"],
        )
        body = InventoryBody({"sand": 64})

        final = asyncio.run(
            _run_interactive_loop(
                session,
                fallback_goal="collect 64 logs",
                body=body,
                max_steps=5,
            )
        )

        self.assertEqual(final.status, "completed_turn")
        self.assertEqual(session.step_count, 2)

    def test_chat_events_submit_existing_session_commands(self):
        session = RecordingSession()
        chat = ChatSource(
            [
                Event(
                    seq=1,
                    tick=10,
                    bot="Bot1",
                    name="agentChat",
                    data={"sender": "Steve", "message": "/pause wait"},
                ),
                Event(
                    seq=2,
                    tick=11,
                    bot="Bot1",
                    name="agentChat",
                    data={"sender": "Alex", "message": "继续收集木头"},
                ),
            ]
        )

        count = _poll_chat_commands(session, chat)

        self.assertEqual(count, 2)
        self.assertEqual([command.kind for command in session.submitted], [SessionCommandKind.PAUSE, SessionCommandKind.MESSAGE])
        self.assertEqual(session.submitted[0].reason, "wait")
        self.assertEqual(session.submitted[1].text, "继续收集木头")
        self.assertTrue(any(event["event"] == "chat_message" for event in session.parts.runtime.trace.snapshot()))

    def test_interactive_loop_polls_chat_before_each_session_step(self):
        session = ReplacedGoalSession(
            steps=[
                SessionStep("completed_turn", LifecycleState.ACTIVE),
                SessionStep("completed_turn", LifecycleState.ACTIVE),
            ],
            goals=["collect 64 logs", "collect 64 sand"],
        )
        chat = ChatSource(
            [
                Event(
                    seq=1,
                    tick=10,
                    bot="Bot1",
                    name="agentChat",
                    data={"sender": "Steve", "message": "/goal collect 64 sand"},
                )
            ]
        )
        body = InventoryBody({"sand": 64})

        final = asyncio.run(
            _run_interactive_loop(
                session,
                fallback_goal="collect 64 logs",
                body=body,
                chat_source=chat,
                max_steps=5,
            )
        )

        self.assertEqual(final.status, "completed_turn")
        self.assertEqual([command.kind for command in session.submitted], [SessionCommandKind.REPLACE_GOAL])

    def test_interactive_loop_terminal_truth_failure_does_not_crash(self):
        session = ReplacedGoalSession(
            steps=[
                SessionStep("completed_turn", LifecycleState.ACTIVE),
                SessionStep("waiting", LifecycleState.YIELDED),
            ],
            goals=["collect 64 logs"],
        )
        body = BrokenInventoryBody()

        final = asyncio.run(
            _run_interactive_loop(
                session,
                fallback_goal="collect 64 logs",
                body=body,
                max_steps=2,
            )
        )

        self.assertEqual(final.status, "waiting")
        self.assertEqual(session.step_count, 2)

    def test_interactive_loop_keeps_driving_recovering_session(self):
        session = ReplacedGoalSession(
            steps=[
                SessionStep("stopped", LifecycleState.RECOVERING, "death"),
                SessionStep("completed_turn", LifecycleState.ACTIVE),
                SessionStep("waiting", LifecycleState.YIELDED),
            ],
            goals=["collect 64 logs"],
        )
        body = InventoryBody({"oak_log": 0})

        final = asyncio.run(
            _run_interactive_loop(
                session,
                fallback_goal="collect 64 logs",
                body=body,
                max_steps=3,
            )
        )

        self.assertEqual(final.lifecycle, LifecycleState.YIELDED)
        self.assertEqual(session.step_count, 3)

    def test_phase1_recovery_facts_include_inventory_recount_delta(self):
        body = RecoveringInventoryBody(
            before_counts={"oak_log": 8, "bread": 2},
            after_counts={},
        )
        cfg = Phase1RuntimeConfig(
            natural_region=Region("test", (0, 0, 0), (16, 128, 16)),
            recovery_gamemode="survival",
        )
        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect 64 logs"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )
        runtime.last_known_body_state = {"pos": [0.5, 64.0, 0.5], "yaw": 90.0, "pitch": 0.0, "dimension": "overworld"}
        runtime.lifecycle.ready()
        runtime.lifecycle.start()
        runtime.mode_runtime.reduce(
            [
                AgentSignal.death_detected("death", inventory_counts_before={"minecraft:oak_log": 8, "minecraft:bread": 2})
            ],
            runtime.lifecycle.state,
            goal_text=runtime.agent_context.goal_text,
        )

        outcome = _phase1_recovery_handler(body, cfg)(runtime)

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.reason, "respawned")
        self.assertEqual(outcome.facts["inventory_before_recovery"], {"ok": True, "source": "death_event", "counts": {"oak_log": 8, "bread": 2}})
        self.assertEqual(outcome.facts["inventory_after_recovery"], {"ok": True, "source": "body_recount", "counts": {}})
        self.assertEqual(outcome.facts["inventory_recovery_delta"]["lost"], {"bread": 2, "oak_log": 8})
        self.assertEqual(body.recover_calls[0]["respawn_pos"], (0, 64, 0))
        self.assertEqual(body.recover_calls[0]["gamemode"], "survival")

    def test_phase1_recovery_without_last_position_uses_server_safe_spawn(self):
        body = RecoveringInventoryBody(before_counts={}, after_counts={})
        cfg = Phase1RuntimeConfig(
            natural_region=Region("test", (0, 0, 0), (16, 128, 16)),
            recovery_gamemode="survival",
        )
        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect 64 logs"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )

        outcome = _phase1_recovery_handler(body, cfg)(runtime)

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.reason, "respawned")
        self.assertIsNone(body.recover_calls[0]["respawn_pos"])
        self.assertIsNone(outcome.facts["respawn_pos"])

    def test_phase1_recovery_ignores_low_oxygen_last_position_for_respawn(self):
        body = RecoveringInventoryBody(before_counts={}, after_counts={})
        cfg = Phase1RuntimeConfig(
            natural_region=Region("test", (0, 0, 0), (16, 128, 16)),
            recovery_gamemode="survival",
        )
        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect 64 logs"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )
        runtime.last_known_body_state = {
            "pos": [30.5, 49.0, -43.5],
            "yaw": 90.0,
            "pitch": 0.0,
            "dimension": "overworld",
            "oxygen": -1,
        }

        outcome = _phase1_recovery_handler(body, cfg)(runtime)

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.reason, "respawned")
        self.assertIsNone(body.recover_calls[0]["respawn_pos"])
        self.assertIsNone(outcome.facts["respawn_pos"])
        self.assertEqual(outcome.facts["last_known_body_state"]["pos"], [30.5, 49.0, -43.5])

    def test_phase1_recovery_retries_after_state_transport_error(self):
        body = RecoveringInventoryBody(before_counts={}, after_counts={})
        cfg = Phase1RuntimeConfig(
            natural_region=Region("test", (0, 0, 0), (16, 128, 16)),
            recovery_gamemode="survival",
        )
        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect 64 logs"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )
        body.after_recovery_state_errors.append(RuntimeError("RCON socket closed"))

        outcome = _phase1_recovery_handler(body, cfg)(runtime)

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.reason, "respawned")
        self.assertEqual(outcome.facts["recovery_metrics"]["state_after_recheck_errors"][0]["message"], "RCON socket closed")

    def test_phase1_recovery_relocates_unsafe_default_spawn_to_nearby_dry_stand(self):
        body = RecoveringInventoryBody(before_counts={}, after_counts={})
        body.default_spawn_pos = (0.0, 79.922, 0.0)
        body.default_spawn_oxygen = 8
        body.blocks[(2, 80, 0)] = ("air", "CLEAR")
        body.blocks[(2, 81, 0)] = ("air", "CLEAR")
        body.blocks[(2, 79, 0)] = ("stone", "SOLID")
        cfg = Phase1RuntimeConfig(
            natural_region=Region("test", (0, 0, 0), (16, 128, 16)),
            recovery_gamemode="survival",
        )
        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect 64 logs"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )

        outcome = _phase1_recovery_handler(body, cfg)(runtime)

        self.assertTrue(outcome.success, outcome)
        self.assertEqual(outcome.reason, "respawned")
        self.assertEqual([call["respawn_pos"] for call in body.recover_calls], [None, (2, 80, 0)])
        self.assertEqual(outcome.facts["safe_respawn"]["reason"], "safe_completed")
        self.assertEqual(outcome.facts["safe_respawn"]["metrics"]["safe_respawn_pos"], [2, 80, 0])
        self.assertEqual(outcome.facts["state_after_pos"], [2.5, 80.0, 0.5])

    def test_phase1_recovery_rejects_authoritative_position_mismatch(self):
        body = AdjustedSpawnRecoveryBody()
        cfg = Phase1RuntimeConfig(
            natural_region=Region("test", (0, 0, 0), (16, 128, 16)),
            recovery_respawn_pos=(0, 64, 0),
            recovery_gamemode="survival",
        )
        runtime = AgentRuntime(
            body=body,
            registry=ToolRegistry(),
            agent_context=AgentContext(system_prompt="sys", goal_text="collect 64 logs"),
            lifecycle=LifecycleController(),
            mode_runtime=ModeRuntime(),
            authority=ProgressAuthority(),
        )

        outcome = _phase1_recovery_handler(body, cfg)(runtime)

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, "respawn_position_mismatch")
        self.assertEqual(outcome.facts["recovery_reason"], "respawn_position_mismatch")
        self.assertEqual(outcome.facts["recovery_metrics"]["state_after"]["pos"], [1.13, 59.0, 0.5])


class InventoryBody:
    def __init__(self, counts):
        self.counts = counts
        self.inventory_reads = []

    def get_inventory(self):
        raise AssertionError("terminal truth must use paged perception, not default get_inventory")

    def perceive(self, scope, params):
        if scope != "inventory":
            return PerceptionResult("Bot", scope, "perception", False, False, {}, error="unsupported")
        self.inventory_reads.append(dict(params))
        slots = []
        for index, (item, count) in enumerate(self.counts.items()):
            slots.append({"slot": index, "item": f"minecraft:{item}", "count": count, "empty": False})
        start = int(params.get("start") or 0)
        limit = int(params.get("limit") or 12)
        page = slots[start : start + limit]
        next_start = start + limit if start + limit < len(slots) else None
        data = {"slots": page}
        if next_start is not None:
            data["nextStart"] = next_start
        return PerceptionResult("Bot", scope, "perception", True, next_start is None, data)


class BrokenInventoryBody:
    def perceive(self, scope, params):
        if scope == "inventory":
            raise ValueError("inventory perception failed: missing_body")
        return PerceptionResult("Bot", scope, "perception", False, False, {}, error="unsupported")


class RecoveringInventoryBody:
    bot_name = "Bot"

    def __init__(self, *, before_counts, after_counts):
        self.before_counts = dict(before_counts)
        self.after_counts = dict(after_counts)
        self.recovered = False
        self.recovered_pos = (0.5, 64.0, 0.5)
        self.recovered_oxygen = 300
        self.default_spawn_pos = (0.5, 64.0, 0.5)
        self.default_spawn_oxygen = 300
        self.recover_calls = []
        self.events = []
        self.blocks = {}
        self.state_errors = []
        self.after_recovery_state_errors = []

    def get_state(self):
        if self.state_errors:
            raise self.state_errors.pop(0)
        if self.recovered and self.after_recovery_state_errors:
            raise self.after_recovery_state_errors.pop(0)
        return BodyState(
            bot=self.bot_name,
            pos=(0.0, 64.0, 0.0) if not self.recovered else self.recovered_pos,
            yaw=90.0,
            pitch=0.0,
            health=0.0 if not self.recovered else 20.0,
            food=0 if not self.recovered else 20,
            oxygen=None if not self.recovered else self.recovered_oxygen,
            inventory_raw="[]",
            inventory_hash="before" if not self.recovered else "after",
            effects=None,
            time=1000,
            weather=None,
            dimension="overworld",
            complete=True,
            missing=not self.recovered,
        )

    def perceive(self, scope, params):
        if scope == "blockCells":
            cells = params.get("cells") or []
            start = int(params.get("start") or 0)
            limit = int(params.get("limit") or 64)
            page = cells[start : start + limit]
            facts = []
            for cell in page:
                pos = (int(cell[0]), int(cell[1]), int(cell[2]))
                block_type, state = self.blocks.get(pos, ("water", "LIQUID"))
                facts.append({"x": pos[0], "y": pos[1], "z": pos[2], "type": block_type, "state": state, "properties": {}})
            next_start = start + len(page)
            nxt = next_start if next_start < len(cells) else None
            return PerceptionResult(
                self.bot_name,
                "blockCells",
                "perception",
                True,
                nxt is None,
                {"cells": facts, "next": nxt},
            )
        if scope != "inventory":
            return PerceptionResult(self.bot_name, scope, "perception", False, False, {}, error="unsupported")
        counts = self.after_counts if self.recovered else self.before_counts
        slots = [
            {"slot": index, "item": f"minecraft:{item}", "count": count, "empty": False}
            for index, (item, count) in enumerate(counts.items())
        ]
        return PerceptionResult(self.bot_name, "inventory", "perception", True, True, {"slots": slots})

    def spawn(self, pos=None, *, yaw=None, pitch=None, dimension=None, gamemode=None, emit_respawned=False, timeout_s=10.0):
        self.recover_calls.append(
            {
                "respawn_pos": None if pos is None else tuple(pos),
                "yaw": yaw,
                "pitch": pitch,
                "dimension": dimension,
                "gamemode": gamemode,
                "emit_respawned": emit_respawned,
            }
        )
        self.recovered = True
        if pos is None:
            self.recovered_pos = self.default_spawn_pos
            self.recovered_oxygen = self.default_spawn_oxygen
            final_pos = list(self.recovered_pos)
        else:
            self.recovered_pos = (float(pos[0]) + 0.5, float(pos[1]), float(pos[2]) + 0.5)
            self.recovered_oxygen = 300
            final_pos = list(self.recovered_pos)
        if emit_respawned:
            self.events.append(Event(seq=1, tick=1, bot=self.bot_name, name="respawned", data={"final_pos": final_pos}))
        return Result(None, self.bot_name, "result", True, True, True)

    def despawn(self):
        self.recovered = False
        return Result(None, self.bot_name, "result", True, True, True)

    def await_action_terminal(self, action_id, timeout_s=15.0, terminal_events=None):
        return Event(seq=1, tick=1, bot=self.bot_name, name="respawned", data={"final_pos": [0.5, 64.0, 0.5]})

    def poll_events(self):
        events = list(self.events)
        self.events.clear()
        return events

    def interrupt(self, reason=None):
        return Result(None, self.bot_name, "result", True, True, True)


class AdjustedSpawnRecoveryBody:
    bot_name = "Bot"

    def __init__(self):
        self.recovered = False
        self.events = []

    def get_state(self):
        return BodyState(
            bot=self.bot_name,
            pos=(0.0, 0.0, 0.0) if not self.recovered else (1.13, 59.0, 0.5),
            yaw=None,
            pitch=None,
            health=0.0 if not self.recovered else 20.0,
            food=0 if not self.recovered else 20,
            oxygen=None,
            inventory_raw="[]",
            inventory_hash="before" if not self.recovered else "after",
            effects=None,
            time=1000,
            weather=None,
            dimension="overworld",
            complete=True,
            missing=not self.recovered,
        )

    def perceive(self, scope, params):
        if scope != "inventory":
            return PerceptionResult(self.bot_name, scope, "perception", False, False, {}, error="unsupported")
        if not self.recovered:
            return PerceptionResult(self.bot_name, "inventory", "perception", False, True, {}, error="missing_body")
        return PerceptionResult(self.bot_name, "inventory", "perception", True, True, {"slots": []})

    def spawn(self, pos, *, yaw=None, pitch=None, dimension=None, gamemode=None, emit_respawned=False, timeout_s=10.0):
        self.recovered = True
        if emit_respawned:
            self.events.append(Event(seq=1, tick=1, bot=self.bot_name, name="respawned", data={"final_pos": [1.13, 59.0, 0.5]}))
        return Result(None, self.bot_name, "result", True, True, True, data={"action": "spawn"})

    def poll_events(self):
        events = list(self.events)
        self.events.clear()
        return events


class HarnessBody:
    bot_name = "Bot"

    def spawn(self, *args, **kwargs):
        return Result(None, self.bot_name, "result", True, True, True)

    def despawn(self):
        return Result(None, self.bot_name, "result", True, True, True)

    def get_state(self):
        return BodyState(
            bot=self.bot_name,
            pos=(0.5, 64.0, 0.5),
            yaw=None,
            pitch=None,
            health=20.0,
            food=20,
            oxygen=300,
            inventory_raw="[]",
            inventory_hash="0",
            effects=None,
            time=1000,
            weather=None,
            dimension="overworld",
            complete=True,
        )

    def perceive(self, scope, params):
        return PerceptionResult(self.bot_name, scope, "perception", True, True, {"slots": []})

    def execute(self, action):
        return Result(action.id, self.bot_name, "result", True, True, False)

    def await_action_terminal(self, action_id, timeout_s=15.0):
        return Event(seq=1, tick=1, bot=self.bot_name, name="moveDone", data={"actionId": action_id})

    def poll_events(self):
        return []

    def ignite_block(self, pos, *, item=None, allow_server_substitute=False, timeout_s=8.0):
        return Event(seq=1, tick=1, bot=self.bot_name, name="igniteDone", data={})

    def sow_crop(self, pos, *, crop_block, seed_item=None, allow_server_substitute=False, timeout_s=8.0):
        return Event(seq=1, tick=1, bot=self.bot_name, name="sowDone", data={})

    def interrupt(self, reason=None):
        return Result(None, self.bot_name, "result", True, True, True)

    def get_inventory(self):
        return []


class CraftToolBody(HarnessBody):
    def __init__(self, *, inventory_pages, recipe_data):
        self.inventory_pages = list(inventory_pages)
        self._all_inventory_pages = list(inventory_pages)
        self.recipe_data = dict(recipe_data)
        self.actions = []
        self.perceptions = []

    def perceive(self, scope, params):
        self.perceptions.append((scope, dict(params)))
        if scope == "inventory":
            if not self.inventory_pages:
                raise AssertionError("inventory page exhausted")
            return self.inventory_pages.pop(0)
        if scope == "recipeData":
            item = str(params.get("item"))
            recipe_raw = self.recipe_data.get(item)
            return PerceptionResult(
                self.bot_name,
                scope,
                "perception",
                recipe_raw is not None,
                True,
                {"item": item, "recipe_raw": recipe_raw} if recipe_raw is not None else {},
                error=None if recipe_raw is not None else "recipe_not_found",
            )
        return super().perceive(scope, params)

    def execute(self, action):
        self.actions.append(action)
        return Result(action.id, self.bot_name, "result", True, True, True, {"action": action.name}, None)

    def await_action_terminal(self, action_id, timeout_s=15.0):
        action = next(action for action in self.actions if action.id == action_id)
        if action.name == "craftItem":
            return Event(
                seq=len(self.actions),
                tick=20,
                bot=self.bot_name,
                name="craftDone",
                data={
                    "action_id": action_id,
                    "success": True,
                    "item": action.params["output"]["item"],
                    "count": action.params["output"]["count"],
                    "output_slot": action.params["output"]["slot"],
                    "stopped_reason": "completed",
                    "inputs_after": [
                        {"slot": entry["slot"], "empty": True, "item": None, "count": 0}
                        for entry in action.params["inputs"]
                    ],
                },
            )
        if action.name == "moveItem":
            return Event(
                seq=len(self.actions),
                tick=20,
                bot=self.bot_name,
                name="moveItemDone",
                data={
                    "action_id": action_id,
                    "success": True,
                    "stopped_reason": "completed",
                    "count": action.params.get("count", 1),
                },
            )
        if action.name == "selectItem":
            item = str(action.params["item"])
            present = any(
                isinstance(row, dict)
                and not row.get("empty")
                and str(row.get("item") or "") == item
                for page in self._all_inventory_pages
                for row in (page.data.get("slots") or [])
            )
            return Event(
                seq=len(self.actions),
                tick=20,
                bot=self.bot_name,
                name="selectItemDone",
                data={
                    "action_id": action_id,
                    "success": present,
                    "item": item,
                    "slot": 0 if present else -1,
                    "count": 1 if present else 0,
                    "stopped_reason": "completed" if present else "not_in_inventory",
                },
            )
        return super().await_action_terminal(action_id, timeout_s=timeout_s)


def _inventory_page(slots):
    return PerceptionResult(
        "Bot",
        "inventory",
        "perception",
        True,
        True,
        {"slots": slots},
    )


def _slot(index, item=None, count=0):
    return {"slot": index, "empty": item is None or count <= 0, "item": item, "count": count}


class ReplacedGoalSession:
    pending = []

    def __init__(self, *, steps, goals):
        self.steps = list(steps)
        self.goals = list(goals)
        self.step_count = 0
        self.current_goal = self.goals[0] if self.goals else None
        self.submitted = []

    async def step(self):
        index = min(self.step_count, len(self.steps) - 1)
        self.current_goal = self.goals[min(self.step_count, len(self.goals) - 1)]
        self.step_count += 1
        return self.steps[index]

    def submit(self, command):
        self.submitted.append(command)


class RecordingSession:
    pending = []

    def __init__(self):
        self.submitted = []
        self.parts = TraceParts()

    def submit(self, command):
        self.submitted.append(command)


class TraceParts:
    def __init__(self):
        from minebot.app.runner import RuntimeTrace

        self.runtime = type("Runtime", (), {"trace": RuntimeTrace()})()


class ChatSource:
    def __init__(self, events):
        self.events = list(events)

    def poll_chat_events(self):
        events = self.events
        self.events = []
        return events


class ScriptLoadRcon:
    def __init__(self, responses):
        self.responses = list(responses)
        self.commands = []

    def request(self, command):
        self.commands.append(command)
        if not self.responses:
            raise AssertionError(f"unexpected RCON request {command}")
        return self.responses.pop(0)


def _state_envelope(bot):
    return (
        f' = {{"type":"state","bot":"{bot}","ok":true,"complete":true,'
        '"data":{"pos":[0,0,0],"yaw":null,"pitch":null,"health":20,'
        '"food":20,"oxygen":300,"inventory_raw":"","inventory_hash":"",'
        '"effects":null,"time":1000,"weather":null,"dimension":"overworld",'
        '"sleeping":false,"missing":false},"error":null} (1ms)'
    )


if __name__ == "__main__":
    unittest.main()
