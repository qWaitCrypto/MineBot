# Resource Progression

Use this methodology when a goal requires materials, tools, crafting, or
smelting across several physical objectives.

1. Read the durable task and current authoritative inventory before planning.
2. Query live `recipeData` for exact ingredients and station requirements. Do
   not infer recipe counts from general knowledge or Wiki prose.
3. Check whether the target block requires a tool tier before breaking it.
   Prefer `ensure_tool_for` when the missing prerequisite is itself a bounded
   acquisition chain; use finer tools when the situation needs explicit control.
4. Use `collect_resource`, `craft_item`, `smelt_item`, and `equip_item` as
   governed physical transactions. The model chooses the sequence; each Body
   transaction owns only its local HOW.
5. After each material step, trust inventory delta and Body terminal truth, not
   an intention or a fluent tool message.
6. Update the visible plan when prerequisites change. Checkpoint only after
   meaningful world progress or with a concrete wait/yield condition.
7. If a candidate is unreachable, change candidate or approach based on the
   structured reason. Do not repeat an identical failed action and do not widen
   block governance to force a route.

Player-made blocks remain protected throughout the chain.
