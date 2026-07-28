"""Canonical model-visible player intents and provider implementation bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from minebot.brain.registry import RegisteredTool, ToolRegistry
from minebot.contract import Body


BodyToolStage = Literal["base", "composition"]


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
    "find_hostiles",
    "search_for_block",
    "mine_block_collect",
    "craft_item",
    "equip_item",
    "smelt_item",
    "move_away",
    "get_to_block",
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
    "collect_block_domain",
    "read_block",
    "read_nearby_blocks",
    "read_nearby_entities",
    "read_recipe",
)

_COMPOSITION_TOOL_NAMES = ("collect_resource", "ensure_tool_for")

CANONICAL_BODY_TOOL_SPECS = tuple(
    CanonicalBodyToolSpec(intent=name, name=name, stage="base")
    for name in _BASE_TOOL_NAMES
) + tuple(
    CanonicalBodyToolSpec(intent=name, name=name, stage="composition")
    for name in _COMPOSITION_TOOL_NAMES
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
        "collect_block_domain": "scarpet",
    },
    "java": {
        "move_to": "java",
        "follow_entity": "java",
        "go_to_surface": "java",
        "collect_block_domain": "java",
    },
    "composite": {
        "move_to": "java",
        "follow_entity": "java",
        "go_to_surface": "java",
        "collect_block_domain": "java",
    },
}


class CanonicalBodyToolCatalog:
    """Registers every player intent once and binds provider implementations."""

    def __init__(self, registry: ToolRegistry, provider: object) -> None:
        self.registry = registry
        self.provider = str(getattr(provider, "value", provider)).strip().lower()
        if self.provider not in BODY_OBJECTIVE_PROVIDER_TABLE:
            choices = ", ".join(sorted(BODY_OBJECTIVE_PROVIDER_TABLE))
            raise ValueError(f"unknown Body tool provider {self.provider!r}; expected one of: {choices}")

    def objective_body(self, tool_name: str, body: Body) -> Body | None:
        return body if BODY_OBJECTIVE_PROVIDER_TABLE[self.provider].get(tool_name) == "java" else None

    def register(self, tool: RegisteredTool) -> RegisteredTool:
        try:
            spec = _SPEC_BY_NAME[tool.name]
        except KeyError:
            raise ValueError(f"uncatalogued model-visible Body tool: {tool.name!r}") from None
        provider = BODY_OBJECTIVE_PROVIDER_TABLE[self.provider].get(tool.name)
        implementation = provider or (
            "agent-composition" if spec.stage == "composition" else "python-body-contract"
        )
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
    "CANONICAL_BODY_TOOL_SPECS",
    "CanonicalBodyToolCatalog",
    "CanonicalBodyToolSpec",
    "canonical_body_tool_names",
]
