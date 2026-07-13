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
2. Query live recipe data for exact ingredients and station requirements. Do
   not infer recipe counts from general knowledge or Wiki prose.
3. Check whether the target block requires a tool tier before breaking it.
   Prefer `ensure_tool_for` when the missing prerequisite is itself a bounded
   acquisition chain; use finer tools when the situation needs explicit control.
4. Use `collect_resource`, `craft_item`, `smelt_item`, and `equip_item` as
   governed physical transactions. The model chooses the sequence; each Body
   transaction owns only its local HOW.
5. Update the visible plan when prerequisites change. Checkpoint only after
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
