"""Shared agent-tool adapters for existing Body capability transactions."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Literal

from minebot.body import (
    BlockApproachTransactions,
    BlockWork,
    ContainerTransactions,
    ExplorationTransactions,
    FurnaceTransactions,
    GetToBlockConfig,
    InteractionTransactions,
    InventoryTransactions,
    NavigationTransactions,
    PickupConfig,
    PickupTransactions,
    ResourceCollectionConfig,
    ResourceCollectionTransactions,
    UseTransactions,
)
from minebot.app.projection import (
    answer_value,
    blocks_by_kind,
    bounded_summary_value,
    encoded_chars,
    top_reasons,
)
from minebot.body.world_read import read_block_facts
from minebot.body.shelter import ShelterTransactions, shelter_summary
from minebot.body.storage import StorageTransactions
from minebot.body.protection import ProtectionTransactions
from minebot.brain.registry import ObservationProjector, RegisteredTool, ToolSidecar
from minebot.contract import (
    Action,
    Body,
    BreakContext,
    JsonObject,
    PerceptionResult,
    PlaceContext,
    Position,
    ToolResult,
    perception_next_cursor,
    terminal_event_to_tool_result,
)


CapabilityDisposition = Literal["tool", "owned", "internal", "debt"]


@dataclass(frozen=True)
class CapabilityClosure:
    disposition: CapabilityDisposition
    owners: tuple[str, ...]
    note: str


def _closure(
    disposition: CapabilityDisposition,
    *owners: str,
    note: str,
) -> CapabilityClosure:
    return CapabilityClosure(disposition, tuple(owners), note)


BODY_TRANSACTION_CLOSURE: dict[str, CapabilityClosure] = {
    "BlockApproachTransactions.get_to_block": _closure(
        "tool",
        "get_to_block",
        note="Body-owned block candidate, stand-domain, planner selection, and terminal verification.",
    ),
    "BlockWork.mine_block": _closure(
        "owned",
        "mine_block_collect",
        "place_here",
        "smelt_item",
        note="Raw governed breaking is an internal step of verified collect, placement recovery, and bot-owned cleanup.",
    ),
    "BlockWork.mine_block_dry": _closure(
        "owned",
        "mine_block_collect",
        note="Dry sealing is selected by mine_block_collect(dry=true).",
    ),
    "BlockWork.mine_block_collect": _closure(
        "tool", "mine_block_collect", note="Verified single-block collect capability."
    ),
    "BlockWork.dig_down_one": _closure(
        "owned", "dig_down", note="One guarded descent step is owned by the bounded shaft transaction."
    ),
    "BlockWork.dig_down_to_y": _closure(
        "tool", "dig_down", note="Guarded bounded downward shaft capability."
    ),
    "BlockWork.dig_up_one": _closure(
        "owned", "dig_up", "go_to_surface", note="One pillar step is owned by bounded ascent transactions."
    ),
    "BlockWork.dig_up_to_y": _closure(
        "tool", "dig_up", note="Guarded bounded pillar-ascent capability."
    ),
    "BlockWork.go_to_surface": _closure(
        "tool", "go_to_surface", note="Verified natural-surface escape capability."
    ),
    "BlockWork.egress_to_dry": _closure(
        "owned",
        "go_to_surface",
        "collect_block_domain",
        note="Bounded pure-movement dry egress is an internal precondition for surface and dry-land resource transactions.",
    ),
    "BlockWork.search_for_block": _closure(
        "tool", "search_for_block", note="Read-only bounded resource/block candidate perception."
    ),
    "BlockWork.sky_exposed": _closure(
        "owned", "go_to_surface", note="Batched sky probe is internal evidence for surface search."
    ),
    "BlockWork.place_block": _closure(
        "tool", "place_block", note="Exact governed placement with fixed WORK context."
    ),
    "BlockWork.place_here": _closure(
        "tool", "place_here", note="Body-owned nearby supported placement."
    ),
    "CombatTransactions.engage_entity": _closure(
        "tool", "engage_entity", note="Bounded target-locked melee engagement."
    ),
    "CombatTransactions.ranged_attack": _closure(
        "tool", "ranged_attack", note="Bounded bow/crossbow action at one resolved target, with damage/kill terminal."
    ),
    "ExplorationTransactions.explore_for": _closure(
        "tool", "explore_for", note="Bounded multi-target frontier exploration with persisted coverage."
    ),
    "ContainerTransactions.transfer_item": _closure(
        "tool", "transfer_container_item", note="Exact-position branch of the shared transfer tool."
    ),
    "ContainerTransactions.transfer_nearest_container": _closure(
        "tool", "transfer_container_item", note="Nearest-container branch of the shared transfer tool."
    ),
    "StorageTransactions.store_items": _closure(
        "tool", "store_items", note="The Agent's storage plan over the verified container transfer."
    ),
    "StorageTransactions.take_items": _closure(
        "tool", "take_items", note="The Body's own container record over the verified container transfer."
    ),
    "FurnaceTransactions.clear_furnace": _closure(
        "tool", "clear_furnace", note="Exact-position branch of the shared clear tool."
    ),
    "FurnaceTransactions.transfer_slot": _closure(
        "owned", "clear_furnace", "smelt_item", note="Named-slot movement is internal to clear and smelt lifecycles."
    ),
    "FurnaceTransactions.clear_nearest_furnace": _closure(
        "tool", "clear_furnace", note="Nearest-furnace branch of the shared clear tool."
    ),
    "FurnaceTransactions.collect_finished_nearby": _closure(
        "tool", "clear_furnace", note="take='output' without pos: every furnace within search_radius."
    ),
    "FurnaceTransactions.smelt_once": _closure(
        "owned", "smelt_item", note="Direct furnace lifecycle is selected by smelt_item."
    ),
    "FurnaceTransactions.smelt_nearest_furnace": _closure(
        "owned", "smelt_item", note="Nearest furnace lifecycle is selected by smelt_item."
    ),
    "FurnaceTransactions.smelt_with_temporary_furnace": _closure(
        "owned", "smelt_item", note="Explicit temporary furnace lifecycle is selected by smelt_item."
    ),
    "FurnaceTransactions.smelt_with_nearby_temporary_furnace": _closure(
        "owned", "smelt_item", note="Auto-site temporary furnace lifecycle is selected by smelt_item."
    ),
    "InteractionTransactions.give_player": _closure(
        "tool", "give_player", note="Pickup-receipt-verified player handoff."
    ),
    "InteractionTransactions.follow_player": _closure(
        "owned", "search_for_entity", "follow_entity",
        note="Player distance band; the model reaches it through entity approach or native following.",
    ),
    "InteractionTransactions.go_to_player": _closure(
        "owned", "search_for_entity", "give_player",
        note="Named-player approach; a player is an entity, so search_for_entity is the model entry.",
    ),
    "InteractionTransactions.search_for_entity": _closure(
        "tool", "search_for_entity", note="Entity search plus authoritative approach."
    ),
    "InteractionTransactions.go_to_bed": _closure(
        "tool", "go_to_bed", note="Bed search, approach, and sleep truth."
    ),
    "InteractionTransactions.set_openable_state": _closure(
        "tool", "set_openable_state", note="Shared verified open/close state setter."
    ),
    "InteractionTransactions.open_openable": _closure(
        "owned", "set_openable_state", note="Convenience branch owned by the state-setting tool."
    ),
    "InteractionTransactions.close_openable": _closure(
        "owned", "set_openable_state", note="Convenience branch owned by the state-setting tool."
    ),
    "InteractionTransactions.till_farmland": _closure(
        "tool", "till_farmland", note="Single-target governed till transaction."
    ),
    "InteractionTransactions.sow_crop": _closure(
        "tool", "sow_crop", note="Single-target verified sow transaction."
    ),
    "InteractionTransactions.harvest_and_resow": _closure(
        "tool", "harvest_and_resow", note="Mature crop collect plus immediate verified resow."
    ),
    "InteractionTransactions.activate_switch": _closure(
        "owned", "set_switch_state", note="Convenience branch owned by the state-setting tool."
    ),
    "InteractionTransactions.deactivate_switch": _closure(
        "owned", "set_switch_state", note="Convenience branch owned by the state-setting tool."
    ),
    "InteractionTransactions.set_switch_state": _closure(
        "tool", "set_switch_state", note="Verified lever/button powered-state setter."
    ),
    "InventoryTransactions.discard_item": _closure(
        "tool", "discard_item", note="Count-aware verified physical discard."
    ),
    "InventoryTransactions.equip_item": _closure(
        "tool", "equip_item", note="Existing shared equip tool."
    ),
    "InventoryTransactions.craft_exact": _closure(
        "owned", "craft_item", note="Exact recipe primitive is internal to runtime recipe crafting."
    ),
    "InventoryTransactions.cleanup_crafting_residue": _closure(
        "owned", "craft_item", note="Residue cleanup is welded into craft_item."
    ),
    "InventoryTransactions.craft_recipe": _closure(
        "tool", "craft_item", note="Existing runtime-recipe craft tool."
    ),
    "LifecycleTransactions.recover_after_death": _closure(
        "internal",
        note="AgentSession recovery owns respawn coordinates and lifecycle reconciliation; model invocation would create a second recovery authority.",
    ),
    "NavigationTransactions.navigate_to": _closure(
        "tool", "move_to", note="Existing shared coordinate navigation tool."
    ),
    "NavigationTransactions.follow_entity": _closure(
        "tool", "follow_entity", note="Existing generic moving-entity follow tool."
    ),
    "NavigationTransactions.move_away": _closure(
        "tool", "move_away", note="Shared avoid-goal hazard-spacing transaction."
    ),
    "ShelterTransactions.shelter_in_place": _closure(
        "tool", "shelter_in_place", note="Explicit stationary shell and owned head-port transactions with world verification."
    ),
    "ProtectionTransactions.pillar_up": _closure(
        "tool", "pillar_up", note="Bounded protection ascent composed from guarded dig_up steps and verified feet position."
    ),
    "ProtectionTransactions.raise_shield": _closure(
        "tool", "raise_shield", note="Timed offhand shield action with observed hit and health terminal facts."
    ),
    "ProtectionTransactions.place_bed_and_sleep": _closure(
        "tool", "place_bed_and_sleep", note="Carried bed sleep, wake, break and inventory recovery transaction."
    ),
    "PickupTransactions.pickup_items": _closure(
        "tool",
        "pickup_items",
        note="Body-owned dropped-item domain planning with authoritative inventory-delta completion.",
    ),
    "ResourceCollectionTransactions.collect_block_domain": _closure(
        "tool",
        "collect_block_domain",
        note="Body-owned bounded resource candidate, route, exact mine, and pickup process.",
    ),
    "UseTransactions.consume_item": _closure(
        "tool", "consume_item", note="Verified food/potion/milk consumption."
    ),
    "UseTransactions.use_item": _closure(
        "tool", "use_item", note="Untargeted verified item use."
    ),
    "UseTransactions.use_on_entity": _closure(
        "tool", "use_on_entity", note="Entity-targeted verified item use."
    ),
    "UseTransactions.use_on_block": _closure(
        "tool", "use_on_block", note="Block-targeted verified item use."
    ),
}


BODY_CAPABILITY_DEBT: dict[str, str] = {
    "villager_trade": "Offer reading exists, but full-fidelity execution requires the explicitly deferred Thin Java Merchant Primitive.",
    "named_places": "Remembered-place storage belongs to deferred A6 memory; navigation itself is already registered.",
}


BODY_PRIMITIVE_CLOSURE: dict[str, CapabilityClosure] = {
    "ScarpetBody.transport_latency_snapshot": _closure(
        "internal", note="Operator observability owns transport latency diagnostics."
    ),
    "ScarpetBody.observability_snapshot": _closure(
        "internal", note="Operator observability owns event/action/latency snapshots."
    ),
    "ScarpetBody.spawn": _closure(
        "internal", note="Lifecycle recovery and explicit operations own fake-player spawning."
    ),
    "ScarpetBody.despawn": _closure(
        "internal", note="Lifecycle recovery and explicit operations own fake-player despawning."
    ),
    "ScarpetBody.say": _closure(
        "internal", note="The interactive assistant speech sink owns outbound public chat."
    ),
    "ScarpetBody.get_state": _closure(
        "owned", "read_state", note="State reads are exposed through read_state and reused by all terminal verification."
    ),
    "ScarpetBody.perceive": _closure(
        "internal", note="Typed perception tools and Body transactions own scope selection, paging, and result semantics."
    ),
    "ScarpetBody.get_inventory": _closure(
        "owned", "read_inventory", note="Paged inventory is exposed through read_inventory."
    ),
    "ScarpetBody.get_container": _closure(
        "owned", "read_container", note="Paged container truth is exposed through read_container."
    ),
    "ScarpetBody.execute": _closure(
        "internal", note="Capability transactions own action construction, governance, and terminal waits."
    ),
    "ScarpetBody.jump": _closure(
        "owned", "dig_up", "go_to_surface", note="Jump is an internal step of guarded ascent."
    ),
    "ScarpetBody.select_item": _closure(
        "owned", "equip_item", "consume_item", "use_item", note="Inventory/use transactions own item staging."
    ),
    "ScarpetBody.use_item": _closure(
        "owned", "consume_item", "use_item", "use_on_entity", "use_on_block", note="Use transactions own targeting and verification."
    ),
    "ScarpetBody.ignite_block": _closure(
        "owned", "use_on_block", note="Ignition is a verified branch of targeted block use."
    ),
    "ScarpetBody.sow_crop": _closure(
        "owned", "sow_crop", "harvest_and_resow", note="Farm transactions own crop and seed verification."
    ),
    "ScarpetBody.attack_entity": _closure(
        "owned", "engage_entity", note="The engagement transaction owns acquisition, pursuit, cooldown, and kill truth."
    ),
    "ScarpetBody.ranged_attack": _closure(
        "owned", "ranged_attack", note="The combat transaction owns target acquisition, weapon choice and terminal handling."
    ),
    "ScarpetBody.container_transfer": _closure(
        "owned", "transfer_container_item", note="ContainerTransactions owns slot planning and delta verification."
    ),
    "ScarpetBody.drop_item": _closure(
        "owned", "discard_item", "give_player", note="Discard and handoff transactions own count and pickup truth."
    ),
    "ScarpetBody.move_item": _closure(
        "owned", "equip_item", "craft_item", "discard_item", note="Inventory transactions own safe slot planning."
    ),
    "ScarpetBody.craft_item": _closure(
        "owned", "craft_item", note="Runtime-recipe crafting owns exact primitive inputs and output truth."
    ),
    "ScarpetBody.furnace_transfer": _closure(
        "owned", "smelt_item", "clear_furnace", note="Furnace transactions own named-slot planning and cleanup."
    ),
    "ScarpetBody.poll_events": _closure(
        "internal", note="BodyEventPump and terminal waiters own the shared durable event cursor."
    ),
    "ScarpetBody.event_head": _closure(
        "internal", note="Startup reconciliation owns epoch/head discovery."
    ),
    "ScarpetBody.poll_chat_events": _closure(
        "internal", note="The interactive chat reader owns inbound chat admission."
    ),
    "ScarpetBody.await_action_terminal": _closure(
        "internal", note="Capability transactions own terminal-event selection and timeout semantics."
    ),
    "ScarpetBody.interrupt": _closure(
        "internal", note="AgentSession cancellation, recovery, and startup reconciliation own interruption."
    ),
}


def body_capability_tools(
    *,
    body: Body,
    block_approach: BlockApproachTransactions,
    navigator: NavigationTransactions,
    work: BlockWork,
    inventory: InventoryTransactions,
    furnace: FurnaceTransactions,
    container: ContainerTransactions,
    storage: StorageTransactions,
    interaction: InteractionTransactions,
    pickup: PickupTransactions,
    resource_collection: ResourceCollectionTransactions,
    use: UseTransactions,
    dig_down_objective_body: Body | None = None,
    dig_up_objective_body: Body | None = None,
) -> tuple[RegisteredTool, ...]:
    tools = (
        _move_away_tool(navigator),
        _shelter_tool(ShelterTransactions(body, work.governance)),
        _pillar_up_tool(ProtectionTransactions(work, inventory, dig_up_objective_body)),
        _raise_shield_tool(ProtectionTransactions(work, inventory)),
        _place_bed_and_sleep_tool(ProtectionTransactions(work, inventory)),
        _get_to_block_tool(block_approach),
        _search_for_entity_tool(interaction),
        _give_player_tool(interaction),
        _consume_item_tool(use),
        _discard_item_tool(inventory),
        _transfer_container_tool(container),
        _read_container_tool(body),
        _store_items_tool(storage),
        _take_items_tool(storage),
        _clear_furnace_tool(furnace),
        _go_to_bed_tool(interaction),
        _set_openable_state_tool(interaction),
        _till_farmland_tool(interaction),
        _sow_crop_tool(interaction),
        _harvest_and_resow_tool(interaction),
        _set_switch_state_tool(interaction),
        _use_item_tool(use),
        _use_on_entity_tool(use),
        _use_on_block_tool(use),
        _place_block_tool(work),
        _place_here_tool(work),
        _dig_down_tool(work, dig_down_objective_body),
        _dig_up_tool(work, dig_up_objective_body),
        _pickup_items_tool(pickup),
        _collect_block_domain_tool(resource_collection),
        _read_block_tool(body),
        _read_nearby_blocks_tool(body),
        _read_area_tool(body),
        _read_nearby_entities_tool(body),
        _read_recipe_tool(body),
    )
    return tools


def _reflex_look_tool(interaction: InteractionTransactions) -> RegisteredTool:
    """Internal adapter to existing attention, using normal claim/stop lanes."""
    return _tool(
        "__reflex_look_at", "Look toward an observed player's position.",
        _object_schema({"pos": VECTOR_SCHEMA, "timeout_s": {"type": "number"}}, required=("pos",)),
        lambda params: interaction._look_at_pos(tuple(params["pos"]), timeout_s=float(params.get("timeout_s") or 5)),
        mutating=True, source="body.interaction", tool_type="attention", permission="move",
        body_scope=("attention",), terminal_truth=("lookDone", "ToolResult"), timeout_s=10.0,
        model_visible=False,
    )


def _reflex_leave_lava_tool(body: Body) -> RegisteredTool:
    """Internal reflex action: out of lava onto the dry cell the Body named.
    Not a model tool; a Baritone walk plans no path that starts in lava."""
    def run(params: JsonObject) -> ToolResult:
        timeout_s = float(params.get("timeout_s") or 5.0)
        action = Action.create("leaveLava", {
            "target": [int(value) for value in params["pos"]],
            "timeout_ticks": max(20, min(400, int(timeout_s * 20))),
        })
        try:
            result = body.execute(action)
            if result.ok and result.accepted and not result.complete:
                return terminal_event_to_tool_result(body.await_action_terminal(action.id, timeout_s=timeout_s + 5.0))
        except Exception as error:
            return ToolResult(False, "java_body_unavailable", True, metrics={"error": type(error).__name__})
        success = bool(result.ok and result.accepted and result.complete)
        return ToolResult(success, "out_of_lava" if success else str(result.error or "body_rejected"),
                          not success, metrics=dict(result.data))

    return _tool(
        "__reflex_leave_lava", "Get out of the lava onto an observed dry cell.",
        _object_schema({"pos": VECTOR_SCHEMA, "timeout_s": {"type": "number"}}, required=("pos",)),
        run, mutating=True, source="real_client.leaveLava", tool_type="navigation", permission="move",
        body_scope=("navigation",), terminal_truth=("leaveLavaDone", "ToolResult"), timeout_s=10.0,
        model_visible=False,
    )


def _reflex_step_tool(body: Body) -> RegisteredTool:
    """Internal reflex action: one quick step to a nearby cell the Body named
    in its ``steps`` fact. Stepping on is Decision's choice in its next frame
    (dragon-unit-tests-20261030 §6, B)."""
    def run(params: JsonObject) -> ToolResult:
        timeout_s = float(params.get("timeout_s") or 3.0)
        action = Action.create("stepTo", {
            "target": [int(value) for value in params["pos"]],
            "timeout_ticks": max(20, min(200, int(timeout_s * 20))),
        })
        try:
            result = body.execute(action)
            if result.ok and result.accepted and not result.complete:
                return terminal_event_to_tool_result(body.await_action_terminal(action.id, timeout_s=timeout_s + 5.0))
        except Exception as error:
            return ToolResult(False, "java_body_unavailable", True, metrics={"error": type(error).__name__})
        success = bool(result.ok and result.accepted and result.complete)
        return ToolResult(success, "stepped" if success else str(result.error or "body_rejected"),
                          not success, metrics=dict(result.data))

    return _tool(
        "__reflex_step", "Step quickly to a nearby cell the Body named.",
        _object_schema({"pos": VECTOR_SCHEMA, "timeout_s": {"type": "number"}}, required=("pos",)),
        run, mutating=True, source="real_client.stepTo", tool_type="navigation", permission="move",
        body_scope=("navigation",), terminal_truth=("stepDone", "ToolResult"), timeout_s=8.0,
        model_visible=False,
    )


def _reflex_use_item_tool(body: Body) -> RegisteredTool:
    """Internal reflex action: use one item once, aimed at a point. Says what
    the use changed in the inventory."""
    def run(params: JsonObject) -> ToolResult:
        item = str(params.get("item") or "")
        before = dict(body.get_state().inventory_counts or {})
        action_params: JsonObject = {"item": item, "mode": "once", "timeout_ticks": 60}
        if params.get("look_at") is not None:
            action_params["look_at"] = [float(value) for value in params["look_at"]]
        action = Action.create("useItem", action_params)
        try:
            result = body.execute(action)
            if result.ok and result.accepted and not result.complete:
                ended = terminal_event_to_tool_result(body.await_action_terminal(action.id, timeout_s=8.0))
            else:
                ended = ToolResult(bool(result.ok and result.accepted and result.complete),
                                   "interaction_applied" if result.ok and result.accepted else str(result.error or "body_rejected"),
                                   not (result.ok and result.accepted), metrics=dict(result.data))
        except Exception as error:
            return ToolResult(False, "java_body_unavailable", True, metrics={"error": type(error).__name__})
        after = dict(body.get_state().inventory_counts or {})
        changed = {name: after.get(name, 0) - before.get(name, 0)
                   for name in set(before) | set(after) if after.get(name, 0) != before.get(name, 0)}
        if ended.success and not changed:
            return ToolResult(False, "nothing_changed", True, metrics={**dict(ended.metrics or {}), "item": item})
        return ToolResult(ended.success, ended.reason, ended.can_retry,
                          metrics={**dict(ended.metrics or {}), "item": item, "inventory_changed": changed})

    return _tool(
        "__reflex_use_item", "Use one item once, aimed at a point.",
        _object_schema({"item": {"type": "string"}, "look_at": VECTOR_SCHEMA}, required=("item",)),
        run, mutating=True, source="real_client.useItem", tool_type="interaction", permission="use",
        body_scope=("inventory",), terminal_truth=("useDone", "inventory", "ToolResult"), timeout_s=12.0,
        model_visible=False,
    )


def _reflex_cushion_fall_tool(body: Body) -> RegisteredTool:
    """Internal reflex action: land a fall in water from a bucket. The Body
    times the pour; using it is Decision's choice from the ``fall`` fact
    (dragon-unit-tests-20261030 §6.y, form B)."""
    def run(params: JsonObject) -> ToolResult:
        timeout_s = float(params.get("timeout_s") or 10.0)
        action = Action.create("cushionFall", {
            "item": str(params.get("item") or "water_bucket"),
            "timeout_ticks": max(20, min(400, int(timeout_s * 20))),
        })
        try:
            result = body.execute(action)
            if result.ok and result.accepted and not result.complete:
                return terminal_event_to_tool_result(body.await_action_terminal(action.id, timeout_s=timeout_s + 5.0))
        except Exception as error:
            return ToolResult(False, "java_body_unavailable", True, metrics={"error": type(error).__name__})
        success = bool(result.ok and result.accepted and result.complete)
        return ToolResult(success, "cushioned" if success else str(result.error or "body_rejected"),
                          not success, metrics=dict(result.data))

    return _tool(
        "__reflex_cushion_fall", "Land the fall in water poured from a bucket.",
        _object_schema({"item": {"type": "string"}, "timeout_s": {"type": "number"}}),
        run, mutating=True, source="real_client.cushionFall", tool_type="interaction", permission="move",
        body_scope=("navigation",), terminal_truth=("cushionDone", "ToolResult"), timeout_s=15.0,
        model_visible=False,
    )


def _tool(
    name: str,
    description: str,
    schema: JsonObject,
    callable_,
    *,
    mutating: bool,
    source: str,
    tool_type: str,
    permission: str,
    body_scope: tuple[str, ...],
    terminal_truth: tuple[str, ...],
    timeout_s: float,
    projector: ObservationProjector | None = None,
    model_visible: bool = True,
) -> RegisteredTool:
    # This is process context for the semantic supervisor, not a detector:
    # it says what a healthy Body call normally does before its terminal fact.
    explanation = {
        "move_to": "The Body normally plans a route, walks toward the target, and returns an authoritative arrival or failure result.",
        "collect_block_domain": "The Body normally searches candidates, approaches a reachable target, performs the collection, and verifies the inventory change.",
        "mine_block_collect": "The Body normally approaches the selected block, performs the break, and verifies the collected result.",
        "craft_item": "The Body normally approaches or selects the crafting context, performs the recipe transaction, and verifies the inventory result.",
        "smelt_item": "The Body normally approaches or selects a furnace, supplies the declared input and fuel, waits for the furnace transaction, and verifies the output.",
        "place_block": "The Body normally approaches the support position, selects the declared block, places it, and verifies the world result.",
        "clear_area": "The Body normally navigates through the declared area, removes the selected blocks, and verifies the cleared result.",
        "dig_down": "The Body normally finds a safe shaft step, removes the next block, moves through it, and verifies the new position.",
        "dig_up": "The Body normally selects a safe ascent step, places or removes the needed support, moves upward, and verifies the new position.",
        "pickup_items": "The Body normally approaches the item targets, transfers the drops, and verifies the inventory change.",
        "transfer_container_item": "The Body normally approaches the container, opens the requested side, transfers the declared item, and verifies slot truth.",
        "store_items": "The Body normally walks to each planned container in turn, deposits one kind of item at a time, and verifies slot truth.",
        "take_items": "The Body normally walks to each container that holds the items, largest holder first, withdraws them, and verifies slot truth.",
        "clear_furnace": "The Body normally approaches the furnace, withdraws the requested slots, and verifies the final furnace and inventory state.",
        "set_openable_state": "The Body normally approaches the block, applies the requested open or close action, and verifies its state.",
        "set_switch_state": "The Body normally approaches the switch, applies the requested state, and verifies the powered result.",
        "farm_area": "The Body normally navigates the crop area, harvests and replants each eligible crop, and verifies the resulting state.",
        "build_structure": "The Body normally navigates between target cells, selects available materials, places them, and returns a verified world result.",
    }.get(name, "The Body normally approaches the target or work context, performs the requested physical action, and returns an authoritative terminal result.")
    return RegisteredTool(
        name,
        description,
        schema,
        callable_,
        ToolSidecar(
            name,
            mutating=mutating,
            source=source,
            tool_type=tool_type,
            permission=permission,
            body_scope=body_scope,
            terminal_truth=terminal_truth,
            timeout_s=timeout_s,
            model_visible=model_visible,
        ),
        projector=projector,
        supervision_explanation=explanation,
    )


def _object_schema(
    properties: dict[str, object],
    *,
    required: tuple[str, ...] = (),
) -> JsonObject:
    schema: JsonObject = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


POSITION_SCHEMA = {
    "type": "array",
    "items": {"type": "integer"},
    "minItems": 3,
    "maxItems": 3,
}
VECTOR_SCHEMA = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 3,
    "maxItems": 3,
}
STRING_LIST_SCHEMA = {"type": "array", "items": {"type": "string"}}
POSITION_LIST_SCHEMA = {"type": "array", "items": POSITION_SCHEMA}
ITEM_DELTA_SCHEMA = {"type": "object", "additionalProperties": {"type": "integer"}}


def _pos(value: object) -> Position:
    values = tuple(int(item) for item in value)  # type: ignore[arg-type]
    if len(values) != 3:
        raise ValueError("position must contain exactly three coordinates")
    return values


def _optional_pos(value: object) -> Position | None:
    return None if value is None else _pos(value)


def _float_vector(value: object) -> tuple[float, float, float] | None:
    if value is None:
        return None
    values = tuple(float(item) for item in value)  # type: ignore[arg-type]
    if len(values) != 3:
        raise ValueError("vector must contain exactly three coordinates")
    return values


def _strings(value: object) -> tuple[str, ...]:
    return tuple(str(item) for item in (value or ()))  # type: ignore[arg-type]


def _positions(value: object) -> list[Position] | None:
    if value is None:
        return None
    return [_pos(item) for item in value]  # type: ignore[arg-type]


def _item_deltas(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("required_watched_item_deltas must be an object")
    return {str(key): int(amount) for key, amount in value.items()}


def _param(params: JsonObject, key: str, default: object) -> object:
    value = params.get(key)
    return default if value is None else value


def _move_away_tool(navigator: NavigationTransactions) -> RegisteredTool:
    return _tool(
        "move_away",
        "Move away from a hazard position until authoritative distance truth satisfies the requested safety band. "
        "With a named threat, take one short dodge on observed safe ground and report position and threat distance.",
        _object_schema(
            {
                "danger_pos": VECTOR_SCHEMA,
                "min_distance": {"type": "number", "exclusiveMinimum": 0},
                "hazard_radius": {"type": "number", "minimum": 0},
                "maintenance_checks": {"type": "integer", "minimum": 1, "maximum": 8},
                "from_hostiles": {"type": "boolean"},
                "threat_id": {"type": ["string", "null"]},
                "threat_kind": {"type": "string"},
            },
            required=("danger_pos",),
        ),
        lambda params: navigator.move_away(
            _float_vector(params["danger_pos"]),  # type: ignore[arg-type]
            min_distance=float(_param(params, "min_distance", 6.0)),
            hazard_radius=float(_param(params, "hazard_radius", 0.0)),
            maintenance_checks=int(_param(params, "maintenance_checks", 1)),
            from_hostiles=bool(_param(params, "from_hostiles", False)),
            threat_id=params.get("threat_id"),
            threat_kind=params.get("threat_kind"),
        ),
        mutating=True,
        source="body.navigation",
        tool_type="navigation",
        permission="move",
        body_scope=("navigation",),
        terminal_truth=("position", "ToolResult"),
        timeout_s=120.0,
    )


def _shelter_tool(shelter: ShelterTransactions) -> RegisteredTool:
    return _tool(
        "shelter_in_place",
        "Seal the current cell with inventory blocks and verify every wall and roof. "
        "look_out opens one owned head-height port, reports visible hostiles and distances, "
        "and closes it with the same block type unless keep_open is true. When the Body should stay sealed, "
        "write your stance in the same turn: in danger the reflex layer offers staying put only under a stance.",
        _object_schema({
            "action": {"type": "string", "enum": ["seal", "look_out"]},
            "side": {"type": "string", "enum": ["north", "east", "south", "west"]},
            "keep_open": {"type": "boolean"},
            "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 120},
        }),
        lambda params: shelter.shelter_in_place(**params),
        mutating=True, source="body.shelter", tool_type="work", permission="place",
        body_scope=("blocks", "inventory", "combat"),
        terminal_truth=("shelterDone", "blockCells", "world_edit_receipt"), timeout_s=130,
        projector=shelter_summary,
    )


def _pillar_up_tool(protection: ProtectionTransactions) -> RegisteredTool:
    return _tool(
        "pillar_up",
        "Pillar up in the Body's current column with inventory blocks using the timed jump-and-place controller.",
        _object_schema({
            "blocks": {"type": "integer", "minimum": 1, "maximum": 16},
            "scaffold_blocks": STRING_LIST_SCHEMA,
            "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 120},
        }),
        lambda params: protection.pillar_up(
            blocks=params.get("blocks", 3),  # type: ignore[arg-type]
            scaffold_blocks=_strings(params.get("scaffold_blocks")) or (
                "cobblestone", "cobbled_deepslate", "deepslate", "stone", "dirt", "netherrack",
            ),
            timeout_s=float(params.get("timeout_s", 30.0)),
        ),
        mutating=True, source="body.protection", tool_type="work", permission="place",
        body_scope=("blocks", "inventory", "navigation"),
        terminal_truth=("blockAt", "position", "ToolResult"), timeout_s=120,
        projector=lambda reason, metrics: {key: metrics[key] for key in (
            "origin", "position", "blocks_requested", "blocks_raised", "target_y", "steps_completed",
        ) if key in metrics},
    )


def _raise_shield_tool(protection: ProtectionTransactions) -> RegisteredTool:
    return _tool(
        "raise_shield",
        "Hold the shield toward an observed entity for a duration or until cancelled; report observed blocked and taken hits.",
        _object_schema({
            "target": {"type": "string", "minLength": 1},
            "duration_ticks": {"type": "integer", "minimum": 1, "maximum": 1200},
        }, required=("target",)),
        lambda params: protection.raise_shield(
            target=params["target"], duration_ticks=params.get("duration_ticks", 100),  # type: ignore[arg-type]
        ),
        mutating=True, source="body.protection", tool_type="combat", permission="combat",
        body_scope=("combat", "inventory", "attention"),
        terminal_truth=("hits_blocked", "hits_taken", "health_before", "health_after", "equipped_offhand"), timeout_s=80,
        projector=lambda reason, metrics: {key: metrics[key] for key in (
            "hits_blocked", "hits_taken", "health_before", "health_after", "equipped_offhand",
        ) if key in metrics},
    )


def _place_bed_and_sleep_tool(protection: ProtectionTransactions) -> RegisteredTool:
    return _tool(
        "place_bed_and_sleep",
        "Place an inventory bed beside the Body, sleep until awake, break the bed, and verify it returned to inventory. "
        "Sleeping sets your respawn point at that bed; once the bed is broken again, a death wakes you at the "
        "world spawn until you set another.",
        _object_schema({"timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 600}}),
        lambda params: protection.place_bed_and_sleep(timeout_s=float(params.get("timeout_s", 300.0))),
        mutating=True, source="body.protection", tool_type="work", permission="place",
        body_scope=("blocks", "inventory", "state"),
        terminal_truth=("sleeping", "bed_placed", "bed_recovered", "bed_game_message", "ToolResult"),
        timeout_s=610,
        projector=lambda reason, metrics: {key: metrics[key] for key in (
            "bed_position", "bed_placed", "bed_recovered", "bed_inventory_before",
            "bed_inventory_after", "bed_game_message", "sleeping",
        ) if key in metrics},
    )


def _get_to_block_tool(approach: BlockApproachTransactions) -> RegisteredTool:
    return _tool(
        "get_to_block",
        "Approach one usable block from the requested block types. The Body selects bounded candidates and "
        "stand points, replans, then verifies block identity and interaction range. This does not mine or "
        "collect a requested count; use collect_resource for count-based acquisition.",
        _object_schema(
            {
                "block_types": STRING_LIST_SCHEMA,
                "search_radius": {"type": "integer", "minimum": 1, "maximum": 64},
                "interaction_radius": {"type": "number", "exclusiveMinimum": 0, "maximum": 6},
                "candidate_budget": {"type": "integer", "minimum": 1, "maximum": 32},
                "candidate_batch_size": {"type": "integer", "minimum": 1, "maximum": 16},
                "find_limit": {"type": "integer", "minimum": 1, "maximum": 64},
                "max_pages": {"type": "integer", "minimum": 1, "maximum": 8},
                "max_segments": {"type": "integer", "minimum": 1, "maximum": 16},
                "segment_timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
            },
            required=("block_types",),
        ),
        lambda params: approach.get_to_block(
            block_types=_strings(params.get("block_types")),
            config=GetToBlockConfig(
                search_radius=int(_param(params, "search_radius", 16)),
                interaction_radius=float(_param(params, "interaction_radius", 4.5)),
                candidate_budget=int(_param(params, "candidate_budget", 8)),
                candidate_batch_size=int(_param(params, "candidate_batch_size", 8)),
                find_limit=int(_param(params, "find_limit", 16)),
                max_pages=int(_param(params, "max_pages", 1)),
                max_segments=int(_param(params, "max_segments", 5)),
                segment_timeout_s=float(_param(params, "segment_timeout_s", 15.0)),
            ),
        ),
        mutating=True,
        source="body.block_approach",
        tool_type="navigation",
        permission="move",
        body_scope=("navigation", "blocks"),
        terminal_truth=("position", "blockAt", "ToolResult"),
        timeout_s=180.0,
    )


def _player_band_properties() -> dict[str, object]:
    return {
        "player_name": {"type": "string", "minLength": 1},
        "search_radius": {"type": "integer", "minimum": 1, "maximum": 64},
        "min_distance": {"type": "number", "minimum": 0},
        "max_distance": {"type": "number", "exclusiveMinimum": 0},
        "vertical_tolerance": {"type": "number", "minimum": 0},
        "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
        "maintenance_checks": {"type": "integer", "minimum": 1, "maximum": 8},
    }




def _search_for_entity_tool(interaction: InteractionTransactions) -> RegisteredTool:
    return _tool(
        "search_for_entity",
        "Find one entity and approach it: a player by entity_name, a named mob, or the nearest of entity_types. "
        "Verifies stable identity and the requested distance band. This is the one tool for walking up to "
        "someone or something; use follow_entity to keep following a moving target.",
        _object_schema(
            {
                "entity_types": STRING_LIST_SCHEMA,
                "entity_name": {"type": "string"},
                "search_radius": {"type": "integer", "minimum": 1, "maximum": 64},
                "min_distance": {"type": "number", "minimum": 0},
                "max_distance": {"type": "number", "exclusiveMinimum": 0},
                "vertical_tolerance": {"type": "number", "minimum": 0},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
            }
        ),
        lambda params: interaction.search_for_entity(
            entity_types=_strings(params.get("entity_types")),
            entity_name=str(params["entity_name"]) if params.get("entity_name") is not None else None,
            search_radius=int(params.get("search_radius") or 24),
            min_distance=float(_param(params, "min_distance", 0.0)),
            max_distance=float(params.get("max_distance") or 4.5),
            vertical_tolerance=float(params.get("vertical_tolerance") or 1.5),
            timeout_s=float(params.get("timeout_s") or 15.0),
        ),
        mutating=True,
        source="body.interaction",
        tool_type="navigation",
        permission="move",
        body_scope=("navigation", "nearby_entities"),
        terminal_truth=("position", "nearbyEntities", "ToolResult"),
        timeout_s=120.0,
    )


def _give_player_tool(interaction: InteractionTransactions) -> RegisteredTool:
    return _tool(
        "give_player",
        "Give an owned item to a named player and report success only after that receiver's pickup receipt.",
        _object_schema(
            {
                "player_name": {"type": "string", "minLength": 1},
                "item": {"type": "string", "minLength": 1},
                "count": {"type": "integer", "minimum": 1, "maximum": 64},
                "search_radius": {"type": "integer", "minimum": 1, "maximum": 64},
            },
            required=("player_name", "item", "count"),
        ),
        lambda params: interaction.give_player(
            receiver_name=str(params["player_name"]),
            item=str(params["item"]),
            count=int(params["count"]),
            search_radius=int(params.get("search_radius") or 12),
        ),
        mutating=True,
        source="body.interaction",
        tool_type="inventory",
        permission="give_item",
        body_scope=("inventory", "navigation", "nearby_entities"),
        terminal_truth=("handoffDone", "itemPickup", "inventory"),
        timeout_s=60.0,
    )


def _consume_item_tool(use: UseTransactions) -> RegisteredTool:
    return _tool(
        "consume_item",
        "Eat or drink an owned item and verify inventory, hunger, or effect delta; full hunger is reported truthfully.",
        _object_schema(
            {
                "item": {"type": "string", "minLength": 1},
                "use_ticks": {"type": "integer", "minimum": 1, "maximum": 200},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
            },
            required=("item",),
        ),
        lambda params: use.consume_item(
            item=str(params["item"]),
            use_ticks=int(params.get("use_ticks") or 80),
            timeout_s=float(params.get("timeout_s") or 8.0),
        ),
        mutating=True,
        source="body.use",
        tool_type="survival",
        permission="use_item",
        body_scope=("inventory", "state"),
        terminal_truth=("inventory", "BodyState", "ToolResult"),
        timeout_s=45.0,
    )


def _discard_item_tool(inventory: InventoryTransactions) -> RegisteredTool:
    return _tool(
        "discard_item",
        "Physically discard a requested count of an owned item and verify authoritative inventory delta.",
        _object_schema(
            {
                "item": {"type": "string", "minLength": 1},
                "count": {"type": "integer", "minimum": 1, "maximum": 64},
                "toward_entity_id": {"type": "string", "minLength": 1},
            },
            required=("item", "count"),
        ),
        lambda params: inventory.discard_item(
            item=str(params["item"]), count=int(params["count"]),
            target_entity_id=str(params["toward_entity_id"]) if params.get("toward_entity_id") else None,
        ),
        mutating=True,
        source="body.inventory",
        tool_type="inventory",
        permission="discard_item",
        body_scope=("inventory", "entities"),
        terminal_truth=("dropDone", "inventory", "nearbyEntities", "ToolResult"),
        projector=lambda reason, metrics: {key: metrics[key] for key in (
            "dropped_count", "target_entity_id", "target_distance", "target_seen_after",
        ) if key in metrics},
        timeout_s=30.0,
    )


def _transfer_container_tool(container: ContainerTransactions) -> RegisteredTool:
    def run(params: JsonObject) -> ToolResult:
        common = {
            "item": str(params["item"]),
            "count": int(params["count"]),
            "direction": str(params["direction"]),
            "total_slots": int(params.get("total_slots") or 27),
            "page_size": int(params.get("page_size") or 27),
            "timeout_s": float(params.get("timeout_s") or 2.0),
        }
        pos = _optional_pos(params.get("pos"))
        if pos is not None:
            return container.transfer_item(pos, **common)
        return container.transfer_nearest_container(
            **common,
            search_radius=int(params.get("search_radius") or 8),
            container_types=_strings(params.get("container_types")) or ("chest", "trapped_chest", "barrel"),
            approach_timeout_s=float(params.get("approach_timeout_s") or 15.0),
        )

    return _tool(
        "transfer_container_item",
        "Deposit into or withdraw from an exact or nearest allowed container, with count and inventory/container delta verification.",
        _object_schema(
            {
                "item": {"type": "string", "minLength": 1},
                "count": {"type": "integer", "minimum": 1},
                "direction": {"type": "string", "enum": ["container_to_bot", "bot_to_container"]},
                "pos": POSITION_SCHEMA,
                "search_radius": {"type": "integer", "minimum": 1, "maximum": 64},
                "container_types": STRING_LIST_SCHEMA,
                "total_slots": {"type": "integer", "minimum": 1, "maximum": 54},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 54},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                "approach_timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
            },
            required=("item", "count", "direction"),
        ),
        run,
        mutating=True,
        source="body.container",
        tool_type="inventory",
        permission="container_transfer",
        body_scope=("inventory", "containers", "navigation"),
        terminal_truth=("container", "inventory", "ToolResult"),
        timeout_s=120.0,
    )


def _store_items_tool(storage: StorageTransactions) -> RegisteredTool:
    return _tool(
        "store_items",
        "Store what the Body carries into the containers of your storage plan (home.storage, or "
        "outposts[].storage with place): every item in the 36 inventory slots except those matching keep, "
        "or only those matching only (ids or patterns such as *_log). Each kind goes to the first planned "
        "container whose holds matches it; the Body walks to each container and checks every moved slot. "
        "What no planned container holds stays with the Body and is reported. Chest, trapped_chest and barrel "
        "only; a player's containers are refused.",
        _object_schema(
            {
                "keep": STRING_LIST_SCHEMA,
                "only": STRING_LIST_SCHEMA,
                "place": {"type": "string", "minLength": 1},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                "approach_timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
            },
            required=("keep",),
        ),
        lambda params: storage.store_items(
            keep=_strings(params.get("keep")),
            only=_strings(params.get("only")),
            place=params.get("place"),
            timeout_s=float(params.get("timeout_s") or 2.0),
            approach_timeout_s=float(params.get("approach_timeout_s") or 30.0),
        ),
        mutating=True,
        source="body.storage",
        tool_type="inventory",
        permission="container_transfer",
        body_scope=("inventory", "containers", "navigation"),
        terminal_truth=("container", "inventory", "ToolResult"),
        timeout_s=300.0,
    )


def _take_items_tool(storage: StorageTransactions) -> RegisteredTool:
    return _tool(
        "take_items",
        "Take items from the containers at home, or at the outpost named by place, that the Body's own "
        "record says hold them: items {id: count}. The Body walks to each container, the largest holder "
        "first, and checks every moved slot. What the record knows no container for, or what the "
        "containers gave less of, is reported as short. Chest, trapped_chest and barrel only.",
        _object_schema(
            {
                "items": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 1},
                          "minProperties": 1},
                "place": {"type": "string", "minLength": 1},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                "approach_timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
            },
            required=("items",),
        ),
        lambda params: storage.take_items(
            items={str(item): int(count) for item, count in dict(params["items"]).items()},
            place=params.get("place"),
            timeout_s=float(params.get("timeout_s") or 2.0),
            approach_timeout_s=float(params.get("approach_timeout_s") or 30.0),
        ),
        mutating=True,
        source="body.storage",
        tool_type="inventory",
        permission="container_transfer",
        body_scope=("inventory", "containers", "navigation"),
        terminal_truth=("container", "inventory", "ToolResult"),
        timeout_s=300.0,
    )


def _read_container_tool(body: Body) -> RegisteredTool:
    return _tool(
        "read_container",
        "Read every slot of a container at an exact position through bounded paged Body perception.",
        _object_schema(
            {
                "pos": POSITION_SCHEMA,
                "total_slots": {"type": "integer", "minimum": 1, "maximum": 54},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 27},
            },
            required=("pos",),
        ),
        lambda params: _read_container(
            body,
            _pos(params["pos"]),
            total_slots=int(params.get("total_slots") or 27),
            page_size=int(params.get("page_size") or 12),
        ),
        mutating=False,
        source="body.perception",
        tool_type="perception",
        permission="read_container",
        body_scope=("containers",),
        terminal_truth=("container",),
        timeout_s=30.0,
    )


def _clear_furnace_tool(furnace: FurnaceTransactions) -> RegisteredTool:
    def run(params: JsonObject) -> ToolResult:
        pos = _optional_pos(params.get("pos"))
        timeout_s = float(params.get("timeout_s") or 2.0)
        take = str(params.get("take") or "all")
        approach_timeout_s = float(params.get("approach_timeout_s") or 15.0)
        if pos is not None:
            return furnace.clear_furnace(pos, timeout_s=timeout_s, take=take,
                                         approach_timeout_s=max(approach_timeout_s, 60.0))
        if take == "output":
            return furnace.collect_finished_nearby(
                search_radius=int(params.get("search_radius") or 8),
                furnace_types=_strings(params.get("furnace_types")) or ("furnace", "blast_furnace", "smoker"),
                timeout_s=timeout_s,
                approach_timeout_s=approach_timeout_s,
            )
        return furnace.clear_nearest_furnace(
            search_radius=int(params.get("search_radius") or 8),
            furnace_types=_strings(params.get("furnace_types")) or ("furnace", "blast_furnace", "smoker"),
            timeout_s=timeout_s,
            approach_timeout_s=float(params.get("approach_timeout_s") or 15.0),
        )

    return _tool(
        "clear_furnace",
        "Take items out of a furnace and verify what is left in it. take='output' takes only what has "
        "finished and leaves the rest smelting: from the furnace at pos (the Body walks there first), or "
        "without pos from every furnace within search_radius. take='all' (default) empties the furnace at pos "
        "or the nearest one: input, fuel and output.",
        _object_schema(
            {
                "pos": POSITION_SCHEMA,
                "take": {"type": "string", "enum": ["all", "output"], "default": "all"},
                "search_radius": {"type": "integer", "minimum": 1, "maximum": 64},
                "furnace_types": STRING_LIST_SCHEMA,
                "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                "approach_timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
            }
        ),
        run,
        mutating=True,
        source="body.furnace",
        tool_type="inventory",
        permission="furnace_transfer",
        body_scope=("inventory", "furnace", "navigation"),
        terminal_truth=("furnace", "inventory", "ToolResult"),
        timeout_s=120.0,
    )


def _go_to_bed_tool(interaction: InteractionTransactions) -> RegisteredTool:
    return _tool(
        "go_to_bed",
        "Find an allowed nearby bed, approach it, and report authoritative sleep, daytime, or occupied truth. "
        "Sleeping in it sets your respawn point there.",
        _object_schema(
            {"search_radius": {"type": "integer", "minimum": 1, "maximum": 64}}
        ),
        lambda params: interaction.go_to_bed(search_radius=int(params.get("search_radius") or 24)),
        mutating=True,
        source="body.interaction",
        tool_type="survival",
        permission="use_bed",
        body_scope=("navigation", "blocks", "state"),
        terminal_truth=("BodyState", "blockAt", "ToolResult"),
        timeout_s=120.0,
    )


def _set_openable_state_tool(interaction: InteractionTransactions) -> RegisteredTool:
    return _tool(
        "set_openable_state",
        "Open or close a wooden door, gate, or trapdoor and verify its authoritative open property.",
        _object_schema(
            {
                "open": {"type": "boolean"},
                "pos": POSITION_SCHEMA,
                "search_radius": {"type": "integer", "minimum": 1, "maximum": 64},
                "block_types": STRING_LIST_SCHEMA,
            },
            required=("open",),
        ),
        lambda params: interaction.set_openable_state(
            desired_open=bool(params["open"]),
            pos=_optional_pos(params.get("pos")),
            search_radius=int(params.get("search_radius") or 12),
            block_types=_strings(params.get("block_types")) or None,
        ),
        mutating=True,
        source="body.interaction",
        tool_type="interaction",
        permission="use_block",
        body_scope=("navigation", "blocks"),
        terminal_truth=("blockAt", "ToolResult"),
        timeout_s=90.0,
    )


def _till_farmland_tool(interaction: InteractionTransactions) -> RegisteredTool:
    return _tool(
        "till_farmland",
        "Till one allowed dirt-like block with an owned hoe and verify farmland truth.",
        _object_schema(
            {
                "hoe_item": {"type": "string", "minLength": 1},
                "pos": POSITION_SCHEMA,
                "search_radius": {"type": "integer", "minimum": 1, "maximum": 64},
            },
            required=("hoe_item",),
        ),
        lambda params: interaction.till_farmland(
            hoe_item=str(params["hoe_item"]),
            pos=_optional_pos(params.get("pos")),
            search_radius=int(params.get("search_radius") or 8),
        ),
        mutating=True,
        source="body.interaction",
        tool_type="farming",
        permission="farm",
        body_scope=("navigation", "inventory", "blocks"),
        terminal_truth=("blockAt", "inventory", "ToolResult"),
        timeout_s=90.0,
    )


def _sow_crop_tool(interaction: InteractionTransactions) -> RegisteredTool:
    return _tool(
        "sow_crop",
        "Sow an owned seed on an exact farmland block and verify crop appearance plus seed delta.",
        _object_schema(
            {
                "seed_item": {"type": "string", "minLength": 1},
                "farmland_pos": POSITION_SCHEMA,
                "expected_crop_block": {"type": "string"},
            },
            required=("seed_item", "farmland_pos"),
        ),
        lambda params: interaction.sow_crop(
            seed_item=str(params["seed_item"]),
            farmland_pos=_pos(params["farmland_pos"]),
            expected_crop_block=(
                str(params["expected_crop_block"])
                if params.get("expected_crop_block") is not None
                else None
            ),
        ),
        mutating=True,
        source="body.interaction",
        tool_type="farming",
        permission="farm",
        body_scope=("navigation", "inventory", "blocks"),
        terminal_truth=("blockAt", "inventory", "ToolResult"),
        timeout_s=90.0,
    )


def _harvest_and_resow_tool(interaction: InteractionTransactions) -> RegisteredTool:
    return _tool(
        "harvest_and_resow",
        "Harvest one mature crop at an exact farmland position, verify pickup, and immediately resow it. "
        "For a whole field use farm_area where it is available; this is the precise single-crop tool.",
        _object_schema(
            {
                "farmland_pos": POSITION_SCHEMA,
                "crop_block": {"type": "string"},
                "seed_item": {"type": "string"},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
            },
            required=("farmland_pos",),
        ),
        lambda params: interaction.harvest_and_resow(
            farmland_pos=_pos(params["farmland_pos"]),
            crop_block=str(params["crop_block"]) if params.get("crop_block") is not None else None,
            seed_item=str(params["seed_item"]) if params.get("seed_item") is not None else None,
            timeout_s=float(params.get("timeout_s") or 15.0),
        ),
        mutating=True,
        source="body.interaction",
        tool_type="farming",
        permission="farm",
        body_scope=("navigation", "inventory", "mine", "blocks"),
        terminal_truth=("mineDone", "inventory", "blockAt", "ToolResult"),
        timeout_s=120.0,
    )


def _set_switch_state_tool(interaction: InteractionTransactions) -> RegisteredTool:
    return _tool(
        "set_switch_state",
        "Set a lever or button to the requested powered state and verify stable or released block truth.",
        _object_schema(
            {
                "powered": {"type": "boolean"},
                "pos": POSITION_SCHEMA,
                "search_radius": {"type": "integer", "minimum": 1, "maximum": 64},
                "block_types": STRING_LIST_SCHEMA,
            },
            required=("powered",),
        ),
        lambda params: interaction.set_switch_state(
            desired_powered=bool(params["powered"]),
            pos=_optional_pos(params.get("pos")),
            search_radius=int(params.get("search_radius") or 8),
            block_types=_strings(params.get("block_types")) or None,
        ),
        mutating=True,
        source="body.interaction",
        tool_type="interaction",
        permission="use_block",
        body_scope=("navigation", "blocks"),
        terminal_truth=("blockAt", "ToolResult"),
        timeout_s=90.0,
    )


def _use_common_properties() -> dict[str, object]:
    return {
        "item": {"type": ["string", "null"]},
        "use_mode": {"type": "string", "enum": ["once", "continuous"]},
        "use_ticks": {"type": "integer", "minimum": 1, "maximum": 200},
        "watched_items": STRING_LIST_SCHEMA,
        "required_watched_item_deltas": ITEM_DELTA_SCHEMA,
        "min_effect_delta": {"type": "integer", "minimum": 0},
        "min_position_delta": {"type": "number", "minimum": 0},
        "follow_timeout_s": {"type": "number", "minimum": 0, "maximum": 5},
        "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
    }


def _use_item_tool(use: UseTransactions) -> RegisteredTool:
    properties = _use_common_properties()
    properties["look_target"] = VECTOR_SCHEMA
    return _tool(
        "use_item",
        "Use an owned item or empty hand without a block/entity target and verify observable deltas; when use spawns a nearby thrown entity, report its observed path and ending.",
        _object_schema(properties),
        lambda params: use.use_item(
            item=str(params["item"]) if params.get("item") is not None else None,
            look_target=_float_vector(params.get("look_target")),
            use_mode=str(params.get("use_mode") or "once"),
            use_ticks=int(params.get("use_ticks") or 1),
            watched_items=_strings(params.get("watched_items")),
            required_watched_item_deltas=_item_deltas(params.get("required_watched_item_deltas")),
            min_effect_delta=int(_param(params, "min_effect_delta", 0)),
            min_position_delta=float(_param(params, "min_position_delta", 0.0)),
            timeout_s=float(params.get("timeout_s") or 8.0),
            follow_timeout_s=float(_param(params, "follow_timeout_s", 3.0)),
        ),
        mutating=True,
        source="body.use",
        tool_type="interaction",
        permission="use_item",
        body_scope=("inventory", "state", "nearby_entities"),
        terminal_truth=("useDone", "inventory", "BodyState", "nearbyEntities", "ToolResult"),
        timeout_s=90.0,
    )


def _use_on_entity_tool(use: UseTransactions) -> RegisteredTool:
    properties = _use_common_properties()
    properties.update(
        {
            "entity_types": STRING_LIST_SCHEMA,
            "entity_name": {"type": "string"},
            "search_radius": {"type": "integer", "minimum": 1, "maximum": 64},
            "min_distance": {"type": "number", "minimum": 0},
            "max_distance": {"type": "number", "exclusiveMinimum": 0},
            "vertical_tolerance": {"type": "number", "minimum": 0},
        }
    )
    return _tool(
        "use_on_entity",
        "Approach a named or typed entity, use an owned item or empty hand on it, and verify observable deltas.",
        _object_schema(properties),
        lambda params: use.use_on_entity(
            item=str(params["item"]) if params.get("item") is not None else None,
            entity_types=_strings(params.get("entity_types")),
            entity_name=str(params["entity_name"]) if params.get("entity_name") is not None else None,
            search_radius=int(params.get("search_radius") or 24),
            min_distance=float(_param(params, "min_distance", 0.0)),
            max_distance=float(params.get("max_distance") or 4.5),
            vertical_tolerance=float(params.get("vertical_tolerance") or 1.5),
            watched_items=_strings(params.get("watched_items")),
            required_watched_item_deltas=_item_deltas(params.get("required_watched_item_deltas")),
            min_effect_delta=int(_param(params, "min_effect_delta", 0)),
            min_position_delta=float(_param(params, "min_position_delta", 0.0)),
            use_mode=str(params.get("use_mode") or "once"),
            use_ticks=int(params.get("use_ticks") or 1),
            timeout_s=float(params.get("timeout_s") or 8.0),
        ),
        mutating=True,
        source="body.use",
        tool_type="interaction",
        permission="use_entity",
        body_scope=("navigation", "nearby_entities", "inventory", "state"),
        terminal_truth=("useDone", "nearbyEntities", "inventory", "BodyState", "ToolResult"),
        timeout_s=120.0,
    )


def _use_on_block_tool(use: UseTransactions) -> RegisteredTool:
    properties = _use_common_properties()
    properties.update(
        {
            "pos": POSITION_SCHEMA,
            "observe_pos": POSITION_SCHEMA,
            "expected_block_types": STRING_LIST_SCHEMA,
            "expected_properties": {"type": "object", "additionalProperties": {"type": "string"}},
            "allow_unchanged": {"type": "boolean"},
            "look_target": VECTOR_SCHEMA,
            "navigation_arrival_radius": {"type": "number", "minimum": 0},
            "center_after_navigation": {"type": "boolean"},
            "stand_points": POSITION_LIST_SCHEMA,
            "line_of_sight_retries": {"type": "integer", "minimum": 0, "maximum": 4},
        }
    )
    return _tool(
        "use_on_block",
        "Approach and use an owned item or empty hand on a block, then verify explicit block/property or item/effect truth. With a water or lava bucket, pos may be the open cell the liquid should fill: the Body clicks a solid neighbour's face into it and watches that cell. Water poured into a lava source cell replaces the lava; water in the cell above a lava source turns that source into obsidian. With flint and steel on a block, the cell above it is watched for fire or a portal.",
        _object_schema(properties, required=("pos",)),
        lambda params: use.use_on_block(
            pos=_pos(params["pos"]),
            item=str(params["item"]) if params.get("item") is not None else None,
            observe_pos=_optional_pos(params.get("observe_pos")),
            expected_block_types=_strings(params.get("expected_block_types")) or None,
            expected_properties=(
                {str(key): str(value) for key, value in params["expected_properties"].items()}
                if isinstance(params.get("expected_properties"), dict)
                else None
            ),
            allow_unchanged=bool(params.get("allow_unchanged", False)),
            look_target=_float_vector(params.get("look_target")),
            use_mode=str(params.get("use_mode") or "once"),
            use_ticks=int(params.get("use_ticks") or 1),
            navigation_arrival_radius=(
                float(params["navigation_arrival_radius"])
                if params.get("navigation_arrival_radius") is not None
                else None
            ),
            center_after_navigation=bool(params.get("center_after_navigation", True)),
            stand_points=_positions(params.get("stand_points")),
            timeout_s=float(params.get("timeout_s") or 8.0),
            line_of_sight_retries=int(_param(params, "line_of_sight_retries", 1)),
            watched_items=_strings(params.get("watched_items")),
            required_watched_item_deltas=_item_deltas(params.get("required_watched_item_deltas")),
            min_effect_delta=int(_param(params, "min_effect_delta", 0)),
        ),
        mutating=True,
        source="body.use",
        tool_type="interaction",
        permission="use_block",
        body_scope=("navigation", "blocks", "inventory", "state"),
        terminal_truth=("useDone", "blockAt", "inventory", "BodyState", "ToolResult"),
        timeout_s=120.0,
    )


def _place_block_tool(work: BlockWork) -> RegisteredTool:
    return _tool(
        "place_block",
        "Place one owned block at an exact empty position; Body selects a valid support face and verifies block truth.",
        _object_schema(
            {
                "pos": POSITION_SCHEMA,
                "block_type": {"type": "string", "minLength": 1},
                "face": {"type": "string", "enum": ["up", "down", "north", "south", "east", "west"]},
                "purpose": {"type": "string", "maxLength": 64},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
            },
            required=("pos", "block_type"),
        ),
        lambda params: work.place_block(
            _pos(params["pos"]),
            str(params["block_type"]),
            face=str(params["face"]) if params.get("face") is not None else None,
            context=PlaceContext.WORK,
            purpose=str(params.get("purpose") or "agent_requested"),
            allow_replace_liquid=False,
            timeout_s=float(params.get("timeout_s") or 30.0),
        ),
        mutating=True,
        source="body.block_work",
        tool_type="work",
        permission="place",
        body_scope=("blocks", "inventory"),
        terminal_truth=("placeDone", "blockAt", "inventory"),
        timeout_s=90.0,
    )


def _place_here_tool(work: BlockWork) -> RegisteredTool:
    return _tool(
        "place_here",
        "Find a nearby supported allowed position, approach it, place one owned block, and verify terminal block truth.",
        _object_schema(
            {
                "block_type": {"type": "string", "minLength": 1},
                "radius": {"type": "integer", "minimum": 1, "maximum": 6},
                "purpose": {"type": "string", "maxLength": 64},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
            },
            required=("block_type",),
        ),
        lambda params: work.place_here(
            str(params["block_type"]),
            radius=int(params.get("radius") or 1),
            context=PlaceContext.WORK,
            purpose=str(params.get("purpose") or "agent_requested"),
            timeout_s=float(params.get("timeout_s") or 30.0),
        ),
        mutating=True,
        source="body.block_work",
        tool_type="work",
        permission="place",
        body_scope=("navigation", "blocks", "inventory"),
        terminal_truth=("placeDone", "blockAt", "inventory", "position"),
        timeout_s=120.0,
    )


def _dig_down_tool(work: BlockWork, java_objective_body: Body | None = None) -> RegisteredTool:
    def run(params: JsonObject) -> ToolResult:
        if java_objective_body is not None:
            return _java_dig_down(java_objective_body, params)
        return work.dig_down_to_y(
            int(params["target_y"]),
            context=BreakContext.DIRECT,
            max_clear_fall=int(_param(params, "max_clear_fall", 2)),
            max_steps=int(params["max_steps"]) if params.get("max_steps") is not None else None,
        )

    return _tool(
        "dig_down",
        "Dig a guarded vertical shaft down to target Y with liquid, fall, and player-build protection checks.",
        _object_schema(
            {
                "target_y": {"type": "integer"},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 64},
                "max_clear_fall": {"type": "integer", "minimum": 1, "maximum": 4},
            },
            required=("target_y",),
        ),
        run,
        mutating=True,
        source=("java_body" if java_objective_body is not None else "body.block_work"),
        tool_type="navigation",
        permission="dig_vertical",
        body_scope=("mine", "navigation", "blocks"),
        terminal_truth=("mineDone", "position", "ToolResult"),
        timeout_s=240.0,
    )


def _java_dig_down(body: Body, params: JsonObject) -> ToolResult:
    target_y = int(params["target_y"])
    max_steps = int(params["max_steps"]) if params.get("max_steps") is not None else None
    max_clear_fall = int(_param(params, "max_clear_fall", 2))
    timeout_s = float(_param(params, "timeout_s", 240.0))
    action = Action.create("digDown", {
        "target_y": target_y,
        "max_steps": max_steps,
        "max_clear_fall": max_clear_fall,
        "timeout_ticks": max(20, min(4_800, int(timeout_s * 20.0 + 0.999))),
        **({"max_mutations": int(params["max_mutations"])} if "max_mutations" in params else {}),
    })
    try:
        result = body.execute(action)
        if result.ok and result.accepted and not result.complete:
            return terminal_event_to_tool_result(body.await_action_terminal(action.id, timeout_s=timeout_s + 5.0))
    except Exception as error:
        return ToolResult(
            False,
            "java_body_unavailable",
            True,
            metrics={"error": type(error).__name__, "target_y": target_y},
        )
    success = bool(result.ok and result.accepted and result.complete)
    reason = result.error or ("dig_down_target_reached" if success else "body_rejected")
    return ToolResult(
        success,
        reason,
        not success and reason not in {"canceled", "dig_down_start_liquid", "dig_down_target_liquid"},
        metrics=dict(result.data),
    )


def _dig_up_tool(work: BlockWork, objective_body: Body | None = None) -> RegisteredTool:
    def run(params: JsonObject) -> ToolResult:
        scaffold_blocks = _strings(params.get("scaffold_blocks")) or (
            "cobblestone", "cobbled_deepslate", "deepslate", "stone", "dirt", "netherrack",
        )
        if objective_body is not None:
            from minebot.body.inventory import _dispatch

            return _dispatch(objective_body, "digUp", {
                "target_y": int(params["target_y"]),
                "max_steps": int(params["max_steps"]) if params.get("max_steps") is not None else None,
                "scaffold_blocks": list(scaffold_blocks),
                "timeout_ticks": 4_800,
            }, timeout_s=245.0)
        return work.dig_up_to_y(
            int(params["target_y"]), context=BreakContext.DIRECT,
            scaffold_blocks=scaffold_blocks,
            max_steps=int(params["max_steps"]) if params.get("max_steps") is not None else None,
        )

    return _tool(
        "dig_up",
        "Clear and pillar through a guarded vertical shaft to target Y using only owned safe scaffold blocks.",
        _object_schema(
            {
                "target_y": {"type": "integer"},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 64},
                "scaffold_blocks": STRING_LIST_SCHEMA,
            },
            required=("target_y",),
        ),
        run,
        mutating=True,
        source="body.objective" if objective_body is not None else "body.block_work",
        tool_type="navigation",
        permission="dig_vertical",
        body_scope=("mine", "place", "navigation", "blocks", "inventory"),
        terminal_truth=("mineDone", "placeDone", "position", "ToolResult"),
        timeout_s=240.0,
    )


def resource_domain_observation_projector(reason: str, metrics: dict[str, object]) -> JsonObject:
    """Bounded projection used when the resource leaf is wrapped by composition."""
    attempts = metrics.get("attempts")
    searches = metrics.get("searches")
    blocker_counts: dict[str, int] = {}
    governance_counts: dict[str, int] = {}
    movement_counts: dict[str, int] = {}
    final_pos: object | None = None
    selected_goal: object | None = None
    capability_snapshot: dict[str, object] | None = None

    def count_reason(counts: dict[str, int], value: object) -> None:
        normalized = str(value or "").strip()
        if normalized:
            counts[normalized] = counts.get(normalized, 0) + 1

    if isinstance(attempts, list):
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            for phase in ("navigation", "mine"):
                phase_result = attempt.get(phase)
                if not isinstance(phase_result, dict) or phase_result.get("success") is True:
                    continue
                count_reason(blocker_counts, phase_result.get("reason"))
            navigation = attempt.get("navigation")
            if not isinstance(navigation, dict):
                continue
            navigation_metrics = navigation.get("metrics")
            if not isinstance(navigation_metrics, dict):
                continue
            if navigation_metrics.get("selected_goal") is not None:
                selected_goal = navigation_metrics["selected_goal"]
            if navigation_metrics.get("final_pos") is not None:
                final_pos = navigation_metrics["final_pos"]
            segments = navigation_metrics.get("segments")
            if not isinstance(segments, list):
                continue
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                diagnostics = segment.get("diagnostics")
                if not isinstance(diagnostics, dict):
                    continue
                event_data = diagnostics.get("event_data")
                if isinstance(event_data, dict) and event_data.get("final_pos") is not None:
                    final_pos = event_data["final_pos"]
                segment_counts = diagnostics.get("movement_counts")
                if isinstance(segment_counts, dict):
                    for movement, value in segment_counts.items():
                        if isinstance(value, (int, float)) and int(value) > 0:
                            key = str(movement)
                            movement_counts[key] = movement_counts.get(key, 0) + int(value)
                snapshot = diagnostics.get("capability_snapshot")
                if isinstance(snapshot, dict):
                    capability_snapshot = snapshot
                mutation_events = diagnostics.get("mutation_events")
                if not isinstance(mutation_events, list):
                    continue
                for event in mutation_events:
                    if not isinstance(event, dict):
                        continue
                    data = event.get("data")
                    if not isinstance(data, dict) or data.get("success") is not False:
                        continue
                    count_reason(governance_counts, data.get("decision_reason") or data.get("reason"))

    summary: JsonObject = {}
    for key in (
        "collected_total",
        "remaining_delta",
        "candidate_attempts_consumed",
        "candidate_budget_remaining",
        "navigation_fallback_attempts",
        "elapsed_ms",
    ):
        if key in metrics:
            summary[key] = bounded_summary_value(metrics[key])
    budget = metrics.get("budget")
    if isinstance(budget, dict):
        summary["budget"] = {
            key: bounded_summary_value(budget[key])
            for key in (
                "candidate_budget",
                "mutation_budget",
                "max_wall_s",
                "find_limit",
                "max_pages",
                "max_goals",
            )
            if key in budget
        }
    last_failure = metrics.get("last_failure")
    if isinstance(last_failure, dict):
        summary["last_failure"] = {
            key: bounded_summary_value(last_failure[key])
            for key in ("success", "reason", "canRetry", "nextSuggestion")
            if key in last_failure
        }
        failure_metrics = last_failure.get("metrics")
        if isinstance(failure_metrics, dict):
            summary["last_failure"]["evidence"] = {
                key: bounded_summary_value(failure_metrics[key])
                for key in (
                    "target",
                    "goal",
                    "attempts_consumed",
                    "budget_remaining",
                    "closest_approach_distance",
                    "unloaded_chunk_count",
                    "governance_reason",
                    "failure_detail",
                )
                if key in failure_metrics
            }
    if blocker_counts:
        summary["process_blockers"] = _bounded_reason_counts(blocker_counts)
    if governance_counts:
        summary["governance_blockers"] = _bounded_reason_counts(governance_counts)
    if movement_counts:
        summary["movement_counts"] = {
            key: movement_counts[key]
            for key in sorted(movement_counts)
        }
    if final_pos is not None:
        summary["final_pos"] = bounded_summary_value(final_pos)
    if selected_goal is not None:
        summary["selected_goal"] = bounded_summary_value(selected_goal)
    if capability_snapshot is not None:
        summary["capability_snapshot"] = {
            key: bounded_summary_value(capability_snapshot[key])
            for key in (
                "allow_break",
                "allow_place",
                "allow_pillar",
                "allow_downward",
                "allow_swim",
                "break_budget",
                "place_budget",
                "pillar_budget",
                "downward_budget",
                "scaffold_item",
                "scaffold_count",
            )
            if key in capability_snapshot
        }
    if isinstance(searches, list):
        summary["search_count"] = len(searches)
        summary["search_truncated"] = any(
            isinstance(search, dict) and search.get("truncated") is True
            for search in searches
        )
        search_uncertainty = [
            uncertainty
            for search in searches
            if isinstance(search, dict)
            for uncertainty in (search.get("uncertainty") or [])
            if isinstance(uncertainty, dict)
        ]
        if search_uncertainty:
            summary["search_uncertainty"] = top_reasons(search_uncertainty)
    if reason == "resource_domain_budget_exhausted" and final_pos is not None:
        summary["resume_hint"] = (
            "bounded resource work ended after physical progress; retry the same Body domain from final_pos "
            "or choose a different domain or prerequisite from the blocker facts"
        )
    return summary


def _bounded_reason_counts(counts: dict[str, int]) -> list[str]:
    return [
        f"{reason}:{count}"
        for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:6]
    ]


def _collect_block_domain_tool(
    resource: ResourceCollectionTransactions,
) -> RegisteredTool:
    def run(params: JsonObject) -> ToolResult:
        return resource.collect_block_domain(
            block_types=_strings(params["block_types"]),
            expected_drops=_strings(params["expected_drops"]),
            remaining_count=int(params["remaining_count"]),
            dry=bool(params.get("dry", False)),
            config=ResourceCollectionConfig(
                search_radius=int(params.get("search_radius") or 16),
                candidate_budget=int(params.get("candidate_budget") or 8),
                mutation_budget=int(params.get("mutation_budget") or 8),
                max_wall_s=float(params.get("max_wall_s") or 60.0),
                find_limit=int(params.get("find_limit") or 12),
                max_pages=int(params.get("max_pages") or 1),
                segment_timeout_s=float(params.get("segment_timeout_s") or 15.0),
                avoid=tuple(tuple(int(value) for value in pos) for pos in params.get("avoid") or ()),
                retry_unreachable=bool(params.get("retry_unreachable", False)),
            ),
        )

    return _tool(
        "collect_block_domain",
        "Run one bounded deterministic resource process over explicit block types. Python owns candidate ordering, blacklists, count and retry budgets; each selected coordinate is executed by one Body-designated break-collect objective.",
        _object_schema(
            {
                "block_types": STRING_LIST_SCHEMA,
                "expected_drops": STRING_LIST_SCHEMA,
                "remaining_count": {"type": "integer", "minimum": 1},
                "search_radius": {"type": "integer", "minimum": 1, "maximum": 64},
                "candidate_budget": {"type": "integer", "minimum": 1, "maximum": 128},
                "mutation_budget": {"type": "integer", "minimum": 1, "maximum": 128},
                "max_wall_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 900},
                "find_limit": {"type": "integer", "minimum": 1, "maximum": 64},
                "max_pages": {"type": "integer", "minimum": 1, "maximum": 8},
                "segment_timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
                "dry": {"type": "boolean"},
                "avoid": {"type": "array", "maxItems": 64, "items": {"type": "array", "items": {"type": "integer"}, "minItems": 3, "maxItems": 3}},
                "retry_unreachable": {"type": "boolean"},
            },
            required=("block_types", "expected_drops", "remaining_count"),
        ),
        run,
        mutating=True,
        source="body.resource_collection",
        tool_type="resource",
        permission="collect_natural_resource",
        body_scope=("search", "navigation", "mine", "pickup", "inventory"),
        terminal_truth=("findBlocks", "navigateDone", "mineDone", "blockAt", "inventory"),
        timeout_s=960.0,
        projector=resource_domain_observation_projector,
        model_visible=False,
    )


def _pickup_items_tool(pickup: PickupTransactions) -> RegisteredTool:
    return _tool(
        "pickup_items",
        "Pick up nearby dropped items through one bounded planner-owned candidate domain and verify inventory gain.",
        _object_schema(
            {
                "expected_items": STRING_LIST_SCHEMA,
                "minimum_count": {"type": "integer", "minimum": 1, "maximum": 2304},
                "radius": {"type": "integer", "minimum": 1, "maximum": 32},
                "entity_limit": {"type": "integer", "minimum": 1, "maximum": 128},
                "max_scan_rounds": {"type": "integer", "minimum": 1, "maximum": 8},
                "candidate_budget": {"type": "integer", "minimum": 1, "maximum": 32},
                "max_wall_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 120},
            }
        ),
        lambda params: pickup.pickup_items(
            expected_items=_strings(params.get("expected_items")),
            minimum_count=int(params.get("minimum_count") or 1),
            config=PickupConfig(
                radius=int(params.get("radius") or 8),
                entity_limit=int(params.get("entity_limit") or 16),
                max_scan_rounds=int(params.get("max_scan_rounds") or 2),
                candidate_budget=int(params.get("candidate_budget") or 5),
                max_wall_s=float(params.get("max_wall_s") or 20.0),
            ),
        ),
        mutating=True,
        source="body.pickup",
        tool_type="resource",
        permission="pickup_items",
        body_scope=("navigation", "pickup", "inventory", "entities"),
        terminal_truth=("nearbyEntities", "navigateDone", "inventory", "ToolResult"),
        timeout_s=125.0,
    )


def _read_block_tool(body: Body) -> RegisteredTool:
    return _tool(
        "read_block",
        "Read authoritative block type, state, and properties at one exact position.",
        _object_schema({"pos": POSITION_SCHEMA}, required=("pos",)),
        lambda params: _perception_tool_result(
            body.perceive("blockAt", _block_params(_pos(params["pos"]))),
            success_reason="block_read",
        ),
        mutating=False,
        source="body.perception",
        tool_type="perception",
        permission="read_world",
        body_scope=("blocks",),
        terminal_truth=("blockAt",),
        timeout_s=15.0,
    )


# A read tool's answer is shown whole up to this size; past it the result says
# how much was left out and the handle recovers the rest.
READ_ANSWER_CHARS = 12_000


def nearby_blocks_observation_projector(reason: str, metrics: dict[str, object]) -> JsonObject:
    data = metrics.get("data")
    if not isinstance(data, dict):
        return {}
    summary: JsonObject = {"detail_path": ["metrics", "data", "blocks"]}
    for key in ("center", "radius", "start", "limit", "total", "nextStart"):
        if key in data:
            summary[key] = bounded_summary_value(data[key])
    blocks = data.get("blocks")
    if isinstance(blocks, list):
        # The blocks are what was asked for. Grouped by kind they all fit;
        # only an unusually varied neighbourhood is cut, nearest first as read.
        shown = len(blocks)
        grouped = blocks_by_kind(blocks)
        while shown > 8 and encoded_chars(grouped) > READ_ANSWER_CHARS:
            shown = max(8, shown // 2)
            grouped = blocks_by_kind(blocks[:shown])
        summary["block_count"] = len(blocks)
        summary["blocks"] = grouped
        summary["blocks_shown"] = shown
        summary["blocks_complete"] = shown == len(blocks) and metrics.get("complete") is True
    return summary


def recipe_observation_projector(reason: str, metrics: dict[str, object]) -> JsonObject:
    data = metrics.get("data")
    if data is None:
        return {}
    shown, whole = answer_value(data, budget=READ_ANSWER_CHARS)
    return {"data": shown, "data_complete": whole and metrics.get("complete") is not False,
            "detail_path": ["metrics", "data"]}


def _read_nearby_blocks_tool(body: Body) -> RegisteredTool:
    return _tool(
        "read_nearby_blocks",
        "Read a bounded sample of nearby authoritative block facts; completeness and continuation truth are preserved.",
        _object_schema(
            {
                "radius": {"type": "integer", "minimum": 1, "maximum": 16},
                "limit": {"type": "integer", "minimum": 1, "maximum": 128},
            }
        ),
        lambda params: _perception_tool_result(
            body.perceive(
                "nearbyBlocks",
                {"radius": int(params.get("radius") or 4), "limit": int(params.get("limit") or 64)},
            ),
            success_reason="nearby_blocks_read",
        ),
        mutating=False,
        source="body.perception",
        tool_type="perception",
        permission="read_world",
        body_scope=("blocks",),
        terminal_truth=("nearbyBlocks",),
        timeout_s=20.0,
        projector=nearby_blocks_observation_projector,
    )


# One answer the model can read whole in most cases: an 8x8x8 cube.
AREA_READ_MAX_CELLS = 512


def area_observation_projector(reason: str, metrics: dict[str, object]) -> JsonObject:
    blocks = metrics.get("blocks")
    if not isinstance(blocks, list):
        return {}
    summary: JsonObject = {key: metrics[key] for key in ("start", "end", "cell_count", "complete", "read_at", "expect")
                           if key in metrics}
    grouped = blocks_by_kind(blocks)
    if encoded_chars(grouped) <= READ_ANSWER_CHARS:
        summary["blocks"] = grouped
        summary["positions_complete"] = True
    else:
        # Every kind and its count stay; long position lists are sampled and
        # the stored original keeps them all.
        summary["blocks"] = [
            {**{key: value for key, value in group.items() if key != "positions"},
             "count": len(group.get("positions") or []), "positions_sample": (group.get("positions") or [])[:6]}
            for group in grouped
        ]
        summary["positions_complete"] = False
        summary["detail_path"] = ["metrics", "blocks"]
    unread = metrics.get("unread")
    if unread:
        summary["unread"] = bounded_summary_value(unread)
    return summary


def _read_area(body: Body, params: JsonObject) -> ToolResult:
    first, second = _pos(params["start"]), _pos(params["end"])
    low = tuple(min(a, b) for a, b in zip(first, second))
    high = tuple(max(a, b) for a, b in zip(first, second))
    size = [high[axis] - low[axis] + 1 for axis in range(3)]
    cells = size[0] * size[1] * size[2]
    if cells > AREA_READ_MAX_CELLS:
        return ToolResult(
            False, "area_too_large", False,
            next_suggestion=f"read at most {AREA_READ_MAX_CELLS} cells per call; split the area",
            metrics={"size": size, "requested_cells": cells, "max_cells": AREA_READ_MAX_CELLS},
        )
    positions = tuple((x, y, z) for y in range(low[1], high[1] + 1)
                      for z in range(low[2], high[2] + 1) for x in range(low[0], high[0] + 1))
    read_at = time.strftime("%H:%M:%S")
    try:
        facts = read_block_facts(body, positions, failure_label="read_area")
    except ValueError as exc:
        return ToolResult(False, "perception_failed", True, metrics={"error": str(exc)[:300], "start": list(low),
                                                                       "end": list(high)})
    blocks = [dict(facts[pos].data) for pos in positions if pos in facts]
    unread = [list(pos) for pos in positions if pos not in facts]
    metrics: JsonObject = {"start": list(low), "end": list(high), "cell_count": cells, "blocks": blocks,
                           "unread": unread, "complete": not unread, "read_at": read_at}
    wanted = str(params.get("expect") or "").strip()
    if wanted:
        wanted = wanted if ":" in wanted else f"minecraft:{wanted}"
        matched = sum(1 for block in blocks if block.get("type") == wanted)
        metrics["expect"] = {"block": wanted, "matched": matched, "not_matched": len(blocks) - matched,
                             "unread": len(unread)}
    return ToolResult(True, "area_read", False, metrics=metrics)


def _read_area_tool(body: Body) -> RegisteredTool:
    return _tool(
        "read_area",
        "Read every block in the inclusive cuboid from start to end (at most 512 cells) in one call: the blocks "
        "grouped by kind with their positions, any cell that could not be read, and when it was read. With expect "
        "(a block id), also how many cells already are that block and how many are not. Use it instead of many "
        "read_block calls to see what an area holds or what a build left.",
        _object_schema({"start": POSITION_SCHEMA, "end": POSITION_SCHEMA, "expect": {"type": "string"}},
                       required=("start", "end")),
        lambda params: _read_area(body, params),
        mutating=False,
        source="body.perception",
        tool_type="perception",
        permission="read_world",
        body_scope=("blocks",),
        terminal_truth=("blockCells",),
        timeout_s=30.0,
        projector=area_observation_projector,
    )


def _read_nearby_entities_tool(body: Body) -> RegisteredTool:
    return _tool(
        "read_nearby_entities",
        "Read a bounded nearest-first list of nearby players, mobs, and item entities with stable IDs where available. "
        "Set hostile_only=true to list only hostile mobs (radius is then capped at 32).",
        _object_schema(
            {
                "radius": {"type": "integer", "minimum": 1, "maximum": 64},
                "limit": {"type": "integer", "minimum": 1, "maximum": 128},
                "hostile_only": {"type": "boolean"},
            }
        ),
        lambda params: _perception_tool_result(
            body.perceive(
                "nearbyHostiles",
                {"radius": min(32, int(params.get("radius") or 16)), "limit": int(params.get("limit") or 32)},
            )
            if params.get("hostile_only")
            else body.perceive(
                "nearbyEntities",
                {"radius": int(params.get("radius") or 16), "limit": int(params.get("limit") or 32)},
            ),
            success_reason="nearby_entities_read",
        ),
        mutating=False,
        source="body.perception",
        tool_type="perception",
        permission="read_world",
        body_scope=("nearby_entities",),
        terminal_truth=("nearbyEntities",),
        timeout_s=20.0,
        projector=nearby_entities_observation_projector,
    )


def nearby_entities_observation_projector(reason: str, metrics: dict[str, object]) -> JsonObject:
    data = metrics.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("entities"), list):
        return {}
    entities = data["entities"]
    # The bounded read itself already selects nearest entities. Preserve all
    # selected IDs, positions and interaction properties; do not replace them
    # with only a count or a generic three-item sample.
    return {"entities": [bounded_summary_value(entity) for entity in entities],
            "entity_count": len(entities), "entities_complete": metrics.get("complete") is True,
            "detail_path": ["metrics", "data", "entities"]}


def _read_recipe_tool(body: Body) -> RegisteredTool:
    return _tool(
        "read_recipe",
        "Read native runtime recipe variants for an item so the model can reason about prerequisites before crafting. "
        "When a recipe ingredient group lists interchangeable options, do not collapse it to the first option; "
        "collect a matching material family such as logs, then craft the concrete output matching the owned ingredient.",
        _object_schema(
            {
                "item": {"type": "string", "minLength": 1},
                "recipe_type": {"type": "string"},
            },
            required=("item",),
        ),
        lambda params: _perception_tool_result(
            body.perceive(
                "recipeData",
                {
                    "item": str(params["item"]),
                    **({"type": str(params["recipe_type"])} if params.get("recipe_type") else {}),
                },
            ),
            success_reason="recipe_read",
        ),
        mutating=False,
        source="body.perception",
        tool_type="knowledge",
        permission="read_recipe",
        body_scope=("inventory", "recipes"),
        terminal_truth=("recipeData",),
        timeout_s=20.0,
        projector=recipe_observation_projector,
    )


def _read_container(
    body: Body,
    pos: Position,
    *,
    total_slots: int,
    page_size: int,
) -> ToolResult:
    slots: list[object] = []
    start: int | None = 0
    seen: set[int] = set()
    pages = 0
    while start is not None:
        if start in seen:
            return ToolResult(
                False,
                "container_read_repeated_cursor",
                True,
                metrics={"pos": list(pos), "cursor": start, "pages": pages},
            )
        seen.add(start)
        perception = body.perceive(
            "container",
            {
                "pos": list(pos),
                "start": start,
                "limit": min(page_size, total_slots - start),
                "total_slots": total_slots,
            },
        )
        pages += 1
        if not perception.ok:
            return _perception_tool_result(perception, success_reason="container_read")
        slots.extend(perception.data.get("slots") or [])
        next_cursor = perception_next_cursor(perception)
        if not perception.complete and next_cursor is None:
            return ToolResult(
                False,
                "container_read_incomplete",
                True,
                metrics={
                    "pos": list(pos),
                    "pages": pages,
                    "slots": slots,
                    "uncertainty": list(perception.uncertainty or ()),
                },
            )
        start = int(next_cursor) if next_cursor is not None else None
    return ToolResult(
        True,
        "container_read",
        False,
        metrics={
            "pos": list(pos),
            "total_slots": total_slots,
            "page_size": page_size,
            "pages": pages,
            "slots": slots,
            "complete": True,
        },
    )


def _perception_tool_result(
    perception: PerceptionResult,
    *,
    success_reason: str,
) -> ToolResult:
    metrics: JsonObject = {
        "scope": perception.scope,
        "complete": perception.complete,
        "data": perception.data,
        "uncertainty": list(perception.uncertainty or ()),
        "next": perception.next,
    }
    if not perception.ok:
        metrics["error"] = perception.error
        return ToolResult(False, "perception_failed", True, metrics=metrics)
    return ToolResult(True, success_reason, False, metrics=metrics)


def _block_params(pos: Position) -> JsonObject:
    return {"x": pos[0], "y": pos[1], "z": pos[2]}


__all__ = [
    "BODY_CAPABILITY_DEBT",
    "BODY_PRIMITIVE_CLOSURE",
    "BODY_TRANSACTION_CLOSURE",
    "CapabilityClosure",
    "body_capability_tools",
]
