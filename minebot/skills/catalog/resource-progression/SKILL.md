---
name: resource-progression
description: Acquire resources through live recipe, inventory, tool-tier, crafting, smelting, and equipment truth when a goal has material prerequisites.
tools:
  - collect_resource
  - craft_item
  - ensure_tool_for
  - equip_item
  - read_inventory
  - smelt_item
---

# Resource Progression

## Use When

Use this methodology when a goal requires materials, tools, crafting, or
smelting across several physical objectives.

## Do Not Use When

Do not load it for a single direct action whose prerequisites and terminal
truth are already known, or as a substitute for current inventory and recipe
observations.

## Method

1. Read the durable task and current authoritative inventory before planning.
2. Order a multi-objective plan by capability dependencies, not by the order
   nouns appear in the goal. From empty or minimal inventory, acquire the
   smallest reusable capability that unlocks later work before spending a full
   frontier budget on an independent collectible. Keep independent visible
   objectives opportunistic rather than making one of them a blocking first
   step.
3. For ordinary tool progression, choose the desired outcome and call
   `ensure_tool_for` once. It owns live inventory and recipe reads, equivalent
   material selection, prerequisite collection, crafting-table use, crafting,
   and main-hand equipment. Do not pre-read every recipe or issue one model
   call per plank, stick, table, or pickaxe. Use `read_recipe` and finer tools
   only after a typed composition failure exposes a real strategic choice or
   when the task explicitly requires manual control.
4. Check whether the target block requires a tool tier before breaking it.
   Pass either that resource or the desired pickaxe to `ensure_tool_for`; use
   finer tools only when its returned facts require a different strategy.
5. For count-based acquisition, or when search candidates have not been proven
   interactable, prefer `collect_resource`. It owns bounded candidate discovery,
   stand-point selection, navigation, governed mining, pickup, and authoritative
   inventory completion. Do not replace that transaction with a manual
   `search_for_block` -> `move_to` -> `mine_block_collect` chain.
6. Use `mine_block_collect` only for one deliberately selected exact block.
   Use `get_to_block` for one block-approach objective. Generic `move_to` is
   coordinate travel and does not prove line of sight or block interaction
   readiness.
7. Use `craft_item`, `smelt_item`, and `equip_item` directly for isolated work
   or typed recovery. For a routine prerequisite chain, the model chooses the
   outcome while `ensure_tool_for` executes the deterministic sequence.
8. Update the visible plan when prerequisites change. Checkpoint only after
   meaningful world progress or with a concrete wait or yield condition.

## Evidence Of Success

Trust authoritative inventory delta, recipe output, equipment state, and Body
terminal truth. An intended action or fluent tool message is not evidence.

## Failure And Adaptation

If a candidate is unreachable, change candidate or approach from the structured
reason. Do not repeat identical failed actions. Re-read live prerequisites when
the world or inventory may have changed.

## Boundaries

Player-made blocks remain protected. This methodology never widens governance,
executes commands, grants permissions, or claims success without world truth.
