"""Canonical model-visible player intents and provider implementation bindings."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from minebot.brain.registry import RegisteredTool, ToolRegistry
from minebot.contract import Body
from minebot.contract.execution import walk_look_down_scope


BodyToolStage = Literal["base", "composition", "client"]


@dataclass(frozen=True)
class CanonicalBodyToolSpec:
    intent: str
    name: str
    stage: BodyToolStage


_BASE_TOOL_NAMES = (
    "read_state",
    "read_inventory",
    "move_to",
    "explore_for",
    "go_to_surface",
    "follow_entity",
    "engage_entity",
    "ranged_attack",
    "search_for_block",
    "mine_block_collect",
    "craft_item",
    "equip_item",
    "smelt_item",
    "move_away",
    "shelter_in_place",
    "pillar_up",
    "raise_shield",
    "place_bed_and_sleep",
    "get_to_block",
    "search_for_entity",
    "give_player",
    "consume_item",
    "discard_item",
    "transfer_container_item",
    "read_container",
    "store_items",
    "take_items",
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
    "collect_block_domain",
    "read_block",
    "read_nearby_blocks",
    "read_area",
    "read_nearby_entities",
    "read_recipe",
)

_COMPOSITION_TOOL_NAMES = ("collect_resource", "ensure_tool_for")

# Native client processes with no counterpart on other providers.
_CLIENT_TOOL_NAMES = (
    "observe_environment",
    "build_structure",
    "clear_area",
    "farm_area",
    "elytra_travel",
    "read_body_processes",
    "build_schematic",
    "set_movement",
    "set_walk_gaze",
)

CANONICAL_BODY_TOOL_SPECS = tuple(
    CanonicalBodyToolSpec(intent=name, name=name, stage="base")
    for name in _BASE_TOOL_NAMES
) + tuple(
    CanonicalBodyToolSpec(intent=name, name=name, stage="composition")
    for name in _COMPOSITION_TOOL_NAMES
) + tuple(
    CanonicalBodyToolSpec(intent=name, name=name, stage="client")
    for name in _CLIENT_TOOL_NAMES
)

_SPEC_BY_NAME = {spec.name: spec for spec in CANONICAL_BODY_TOOL_SPECS}
if len(_SPEC_BY_NAME) != len(CANONICAL_BODY_TOOL_SPECS):
    raise RuntimeError("canonical Body tool catalog contains duplicate names")

# This is the only model-tool-level provider table. Capabilities not listed here
# remain Python transactions over the neutral Body contract; their primitive
# provider is selected below that contract and never by the model.
BODY_OBJECTIVE_PROVIDER_TABLE: dict[str, dict[str, str]] = {
    "scarpet": {
        "move_to": "scarpet",
        "follow_entity": "scarpet",
        "go_to_surface": "scarpet",
        "dig_down": "scarpet",
        "collect_block_domain": "scarpet",
    },
    "java": {
        "move_to": "java",
        "follow_entity": "java",
        "go_to_surface": "java",
        "dig_down": "java",
        "collect_block_domain": "java",
    },
    "composite": {
        "move_to": "java",
        "follow_entity": "java",
        "go_to_surface": "java",
        "dig_down": "java",
        "collect_block_domain": "java",
    },
    "real_client": {
        "move_to": "real_client",
        "follow_entity": "real_client",
        "go_to_surface": "real_client",
        "dig_down": "real_client",
        "dig_up": "real_client",
    },
}


# Tools whose job is to walk somewhere. On the real client they take an
# optional look_down (user 2026-11-05: 默认平视，传参之后就低头): the default
# walk is unchanged; with it the Body walks looking at the ground. Whether to
# use it is the caller's strategy.
WALKING_TOOL_NAMES = frozenset({
    "move_to",
    "explore_for",
    "go_to_surface",
    "get_to_block",
    "move_away",
    "search_for_entity",
    "follow_entity",
})

LOOK_DOWN_PROPERTY = {
    "type": "boolean",
    "description": (
        "Walk looking at the ground; use where looking around is risky, "
        "e.g. endermen in the End. Without it, your set_walk_gaze setting decides."
    ),
}


def with_look_down(tool: RegisteredTool) -> RegisteredTool:
    """Add the optional look_down argument and carry it to the Body actions."""
    schema = dict(tool.input_schema)
    properties = dict(schema.get("properties") or {})
    properties["look_down"] = dict(LOOK_DOWN_PROPERTY)
    schema["properties"] = properties
    inner = tool.callable

    def call(params):
        params = dict(params)
        # Without the argument the Agent's standing walk gaze decides (set_walk_gaze).
        look_down = params.pop("look_down", None)
        with walk_look_down_scope(None if look_down is None else bool(look_down)):
            return inner(params)

    return replace(tool, input_schema=schema, callable=call)


class CanonicalBodyToolCatalog:
    """Registers every player intent once and binds provider implementations."""

    def __init__(self, registry: ToolRegistry, provider: object) -> None:
        self.registry = registry
        self.provider = str(getattr(provider, "value", provider)).strip().lower()
        if self.provider not in BODY_OBJECTIVE_PROVIDER_TABLE:
            choices = ", ".join(sorted(BODY_OBJECTIVE_PROVIDER_TABLE))
            raise ValueError(f"unknown Body tool provider {self.provider!r}; expected one of: {choices}")

    def objective_body(self, tool_name: str, body: Body) -> Body | None:
        implementation = BODY_OBJECTIVE_PROVIDER_TABLE[self.provider].get(tool_name)
        return body if implementation in {"java", "real_client"} else None

    def register(self, tool: RegisteredTool) -> RegisteredTool:
        try:
            spec = _SPEC_BY_NAME[tool.name]
        except KeyError:
            raise ValueError(f"uncatalogued model-visible Body tool: {tool.name!r}") from None
        if spec.stage == "client" and self.provider != "real_client":
            raise ValueError(f"client Body tool {tool.name!r} requires the real_client provider")
        if self.provider == "real_client" and tool.name in WALKING_TOOL_NAMES:
            tool = with_look_down(tool)
        provider = BODY_OBJECTIVE_PROVIDER_TABLE[self.provider].get(tool.name)
        implementation = provider or {
            "composition": "agent-composition",
            "client": "real_client",
        }.get(spec.stage, "python-body-contract")
        return self.registry.register(
            tool,
            intent=f"body:{spec.intent}",
            implementation=implementation,
        )

    def register_many(self, tools: tuple[RegisteredTool, ...]) -> None:
        for tool in tools:
            self.register(tool)

    def assert_stage_complete(self, stage: BodyToolStage) -> None:
        expected = {spec.name for spec in CANONICAL_BODY_TOOL_SPECS if spec.stage == stage}
        registered = {
            binding.tool_name
            for binding in self.registry.canonical_bindings()
            if binding.intent.startswith("body:")
        }
        missing = sorted(expected - registered)
        if missing:
            raise RuntimeError(f"canonical Body tool stage {stage!r} is incomplete: {missing}")


def canonical_body_tool_names(*, stage: BodyToolStage | None = None) -> tuple[str, ...]:
    return tuple(
        spec.name
        for spec in CANONICAL_BODY_TOOL_SPECS
        if stage is None or spec.stage == stage
    )


__all__ = [
    "BODY_OBJECTIVE_PROVIDER_TABLE",
    "LOOK_DOWN_PROPERTY",
    "WALKING_TOOL_NAMES",
    "with_look_down",
    "CANONICAL_BODY_TOOL_SPECS",
    "CanonicalBodyToolCatalog",
    "CanonicalBodyToolSpec",
    "canonical_body_tool_names",
]
