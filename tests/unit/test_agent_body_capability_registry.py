import unittest
from unittest.mock import Mock, patch

from minebot.app.body_capability_tools import (
    BODY_CAPABILITY_DEBT,
    BODY_PRIMITIVE_CLOSURE,
    BODY_TRANSACTION_CLOSURE,
)
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry, tool_manifest
from minebot.app.runner import tool_is_enabled
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
from minebot.contract import Region, ToolResult
from minebot.game import ScarpetBody


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
        self.assertIn("does not prove line of sight", move)
        self.assertIn("one already selected exact block", mine)
        self.assertIn("does not discover or choose alternative", mine)

        self.assertEqual(
            {"search_for_block", "get_to_block", "move_to", "mine_block_collect"}
            - set(registry.names()),
            set(),
        )

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
