---
name: evidence-led-exploration
description: Explore beyond known local facts with bounded multi-target coverage when a goal remains active but nearby authoritative searches contain no usable target.
tools:
  - collect_resource
  - engage_entity
  - explore_for
  - mine_block_collect
  - read_state
---

# Evidence Led Exploration

## Use When

Use this methodology when the current objective is still valid, local world
facts contain no usable target, and moving to new information frontiers is the
next justified commitment.

## Do Not Use When

Do not load it when a known reachable target already supports a material action,
when the goal is complete, or when a typed terminal says the approach is
exhausted without a materially different strategy.

## Method

1. Read current task context and authoritative Body state. Choose target classes
   that answer the active objective, grouping compatible block and entity targets
   into one `explore_for` call instead of rewalking the same area.
2. Set finite distance and region budgets. Ask for target predicates, never
   invent reconnaissance coordinates or steer movement step by step.
3. On `found`, use the returned block positions or entity identities to choose a
   governed material operation such as `mine_block_collect`, `collect_resource`,
   or `engage_entity`. The discovery itself is not completion of that material
   objective.
4. On `budget_exhausted`, retain the resume cursor and coverage evidence. Continue
   only when the active durable task, checkpoint budget, and new evidence justify
   another model-authored commitment.
5. On `mobility_blocked`, `unloaded_boundary`, interruption, or death, adapt from
   the typed cause and fresh state. Do not replay the same frontier request as if
   wording changes created new evidence.

## Evidence Of Success

Trust authoritative target facts, covered-region evidence, final position, and
the terminal truth of the later material operation. A longer walk, a successful
tool return without a match, or model prose is not objective completion.

## Failure And Adaptation

Change target class, budget, or physical approach only when accumulated evidence
supports the change. If safe frontiers or the continuation budget are exhausted,
checkpoint or yield with the exact typed blocker rather than extending retries.

## Boundaries

Exploration never weakens player-block governance, creates a second scheduler,
grants continuation by itself, hides tools, or moves Brain decisions into Body.
