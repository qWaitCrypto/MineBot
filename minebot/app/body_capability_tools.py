"""Shared agent-tool adapters for existing Body capability transactions."""

from __future__ import annotations

from dataclasses import dataclass
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
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import (
    Body,
    BreakContext,
    JsonObject,
    PerceptionResult,
    PlaceContext,
    Position,
    ToolResult,
    perception_next_cursor,
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
    "ExplorationTransactions.explore_for": _closure(
        "tool", "explore_for", note="Bounded multi-target frontier exploration with persisted coverage."
    ),
    "ContainerTransactions.transfer_item": _closure(
        "tool", "transfer_container_item", note="Exact-position branch of the shared transfer tool."
    ),
    "ContainerTransactions.transfer_nearest_container": _closure(
        "tool", "transfer_container_item", note="Nearest-container branch of the shared transfer tool."
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
        "tool", "follow_player", note="Bounded player distance-band maintenance."
    ),
    "InteractionTransactions.go_to_player": _closure(
        "tool", "go_to_player", note="One-shot named-player approach."
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
    "ranged_attack": "Scarpet has a live-proven rangedAttack primitive, but no capability-level Python transaction yet owns weapon selection, target acquisition, and terminal verification for the Brain.",
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
        "debt", note=BODY_CAPABILITY_DEBT["ranged_attack"]
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


def register_body_capability_tools(
    registry: ToolRegistry,
    *,
    body: Body,
    block_approach: BlockApproachTransactions,
    navigator: NavigationTransactions,
    work: BlockWork,
    inventory: InventoryTransactions,
    furnace: FurnaceTransactions,
    container: ContainerTransactions,
    interaction: InteractionTransactions,
    pickup: PickupTransactions,
    resource_collection: ResourceCollectionTransactions,
    use: UseTransactions,
) -> None:
    tools = (
        _move_away_tool(navigator),
        _get_to_block_tool(block_approach),
        _go_to_player_tool(interaction),
        _follow_player_tool(interaction),
        _search_for_entity_tool(interaction),
        _give_player_tool(interaction),
        _consume_item_tool(use),
        _discard_item_tool(inventory),
        _transfer_container_tool(container),
        _read_container_tool(body),
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
        _dig_down_tool(work),
        _dig_up_tool(work),
        _pickup_items_tool(pickup),
        _collect_block_domain_tool(resource_collection),
        _read_block_tool(body),
        _read_nearby_blocks_tool(body),
        _read_nearby_entities_tool(body),
        _read_recipe_tool(body),
    )
    for tool in tools:
        registry.register(tool)


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
) -> RegisteredTool:
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
        ),
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
        "Move away from a hazard position until authoritative distance truth satisfies the requested safety band.",
        _object_schema(
            {
                "danger_pos": VECTOR_SCHEMA,
                "min_distance": {"type": "number", "exclusiveMinimum": 0},
                "hazard_radius": {"type": "number", "minimum": 0},
                "maintenance_checks": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            required=("danger_pos",),
        ),
        lambda params: navigator.move_away(
            _float_vector(params["danger_pos"]),  # type: ignore[arg-type]
            min_distance=float(_param(params, "min_distance", 6.0)),
            hazard_radius=float(_param(params, "hazard_radius", 0.0)),
            maintenance_checks=int(_param(params, "maintenance_checks", 1)),
        ),
        mutating=True,
        source="body.navigation",
        tool_type="navigation",
        permission="move",
        body_scope=("navigation",),
        terminal_truth=("position", "ToolResult"),
        timeout_s=120.0,
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


def _go_to_player_tool(interaction: InteractionTransactions) -> RegisteredTool:
    return _tool(
        "go_to_player",
        "Approach a named player and verify the requested distance band from fresh entity facts.",
        _object_schema(_player_band_properties(), required=("player_name",)),
        lambda params: interaction.go_to_player(
            player_name=str(params["player_name"]),
            search_radius=int(params.get("search_radius") or 24),
            min_distance=float(_param(params, "min_distance", 1.0)),
            max_distance=float(params.get("max_distance") or 4.5),
            vertical_tolerance=float(params.get("vertical_tolerance") or 1.5),
            timeout_s=float(params.get("timeout_s") or 15.0),
            maintenance_checks=int(params.get("maintenance_checks") or 1),
        ),
        mutating=True,
        source="body.interaction",
        tool_type="navigation",
        permission="move",
        body_scope=("navigation", "nearby_entities"),
        terminal_truth=("position", "nearbyEntities", "ToolResult"),
        timeout_s=120.0,
    )


def _follow_player_tool(interaction: InteractionTransactions) -> RegisteredTool:
    return _tool(
        "follow_player",
        "Follow a named player for bounded maintenance checks while preserving a min/max distance band.",
        _object_schema(_player_band_properties(), required=("player_name",)),
        lambda params: interaction.follow_player(
            player_name=str(params["player_name"]),
            search_radius=int(params.get("search_radius") or 24),
            min_distance=float(_param(params, "min_distance", 2.0)),
            max_distance=float(params.get("max_distance") or 4.5),
            vertical_tolerance=float(params.get("vertical_tolerance") or 1.5),
            timeout_s=float(params.get("timeout_s") or 15.0),
            maintenance_checks=int(params.get("maintenance_checks") or 1),
        ),
        mutating=True,
        source="body.interaction",
        tool_type="navigation",
        permission="move",
        body_scope=("navigation", "nearby_entities"),
        terminal_truth=("position", "nearbyEntities", "ToolResult"),
        timeout_s=120.0,
    )


def _search_for_entity_tool(interaction: InteractionTransactions) -> RegisteredTool:
    return _tool(
        "search_for_entity",
        "Find a named or typed entity, approach it, and verify stable identity and interaction range.",
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
            },
            required=("item", "count"),
        ),
        lambda params: inventory.discard_item(item=str(params["item"]), count=int(params["count"])),
        mutating=True,
        source="body.inventory",
        tool_type="inventory",
        permission="discard_item",
        body_scope=("inventory",),
        terminal_truth=("dropDone", "inventory", "ToolResult"),
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
        if pos is not None:
            return furnace.clear_furnace(pos, timeout_s=timeout_s)
        return furnace.clear_nearest_furnace(
            search_radius=int(params.get("search_radius") or 8),
            furnace_types=_strings(params.get("furnace_types")) or ("furnace", "blast_furnace", "smoker"),
            timeout_s=timeout_s,
            approach_timeout_s=float(params.get("approach_timeout_s") or 15.0),
        )

    return _tool(
        "clear_furnace",
        "Withdraw input, fuel, and output from an exact or nearest allowed furnace and verify final slot truth.",
        _object_schema(
            {
                "pos": POSITION_SCHEMA,
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
        "Find an allowed nearby bed, approach it, and report authoritative sleep, daytime, or occupied truth.",
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
        "Harvest one mature crop, verify pickup, and immediately resow the same farmland position.",
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
        "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
    }


def _use_item_tool(use: UseTransactions) -> RegisteredTool:
    properties = _use_common_properties()
    properties["look_target"] = VECTOR_SCHEMA
    return _tool(
        "use_item",
        "Use an owned item or empty hand without a block/entity target and verify inventory, effect, or position delta.",
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
        ),
        mutating=True,
        source="body.use",
        tool_type="interaction",
        permission="use_item",
        body_scope=("inventory", "state"),
        terminal_truth=("useDone", "inventory", "BodyState", "ToolResult"),
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
        "Approach and use an owned item or empty hand on a block, then verify explicit block/property or item/effect truth.",
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
        "Place one owned block at an exact empty position through fixed WORK governance and verify block truth.",
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


def _dig_down_tool(work: BlockWork) -> RegisteredTool:
    return _tool(
        "dig_down",
        "Dig a guarded vertical shaft down to target Y with liquid, fall, and player-build protection checks.",
        _object_schema(
            {
                "target_y": {"type": "integer"},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 64},
                "max_clear_fall": {"type": "integer", "minimum": 0, "maximum": 4},
            },
            required=("target_y",),
        ),
        lambda params: work.dig_down_to_y(
            int(params["target_y"]),
            context=BreakContext.DIRECT,
            max_clear_fall=int(_param(params, "max_clear_fall", 2)),
            max_steps=int(params["max_steps"]) if params.get("max_steps") is not None else None,
        ),
        mutating=True,
        source="body.block_work",
        tool_type="navigation",
        permission="dig_vertical",
        body_scope=("mine", "navigation", "blocks"),
        terminal_truth=("mineDone", "position", "ToolResult"),
        timeout_s=240.0,
    )


def _dig_up_tool(work: BlockWork) -> RegisteredTool:
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
        lambda params: work.dig_up_to_y(
            int(params["target_y"]),
            context=BreakContext.DIRECT,
            scaffold_blocks=_strings(params.get("scaffold_blocks")) or (
                "cobblestone",
                "cobbled_deepslate",
                "deepslate",
                "stone",
                "dirt",
                "netherrack",
            ),
            max_steps=int(params["max_steps"]) if params.get("max_steps") is not None else None,
        ),
        mutating=True,
        source="body.block_work",
        tool_type="navigation",
        permission="dig_vertical",
        body_scope=("mine", "place", "navigation", "blocks", "inventory"),
        terminal_truth=("mineDone", "placeDone", "position", "ToolResult"),
        timeout_s=240.0,
    )


def _collect_block_domain_tool(resource: ResourceCollectionTransactions) -> RegisteredTool:
    return _tool(
        "collect_block_domain",
        "Collect a bounded physical domain of explicit block types. The Body discovers candidates, submits their stand points as one planner goal set, blacklists failed candidates, replans, mines, and verifies pickup inventory truth.",
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
            },
            required=("block_types", "expected_drops", "remaining_count"),
        ),
        lambda params: resource.collect_block_domain(
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
            ),
        ),
        mutating=True,
        source="body.resource_collection",
        tool_type="resource",
        permission="collect_natural_resource",
        body_scope=("search", "navigation", "mine", "pickup", "inventory"),
        terminal_truth=("findBlocks", "navigateDone", "mineDone", "blockAt", "inventory"),
        timeout_s=960.0,
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
    )


def _read_nearby_entities_tool(body: Body) -> RegisteredTool:
    return _tool(
        "read_nearby_entities",
        "Read a bounded nearest-first list of nearby players, mobs, and item entities with stable IDs where available.",
        _object_schema(
            {
                "radius": {"type": "integer", "minimum": 1, "maximum": 64},
                "limit": {"type": "integer", "minimum": 1, "maximum": 128},
            }
        ),
        lambda params: _perception_tool_result(
            body.perceive(
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
    )


def _read_recipe_tool(body: Body) -> RegisteredTool:
    return _tool(
        "read_recipe",
        "Read native runtime recipe variants for an item so the model can reason about prerequisites before crafting.",
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
                    "uncertainty": list(perception.uncertainty),
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
        "uncertainty": list(perception.uncertainty),
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
    "register_body_capability_tools",
]
