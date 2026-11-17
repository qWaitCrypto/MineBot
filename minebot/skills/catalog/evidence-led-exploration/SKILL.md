---
name: evidence-led-exploration
description: Choose useful exploration destinations from environment observations, Minecraft knowledge and search history; search while travelling.
tools:
  - explore_for
  - read_state
  - collect_resource
  - engage_entity
  - mine_block_collect
---

# Exploration

## Use When

When local observations contain no useful target, decide where to search next.
On the real-client backend, `observe_environment` gives compass sectors,
biomes, heights, water, difficulty, surface/underground position, nearby entities
and recent searches. Samples are loaded client facts; gaps remain unknown.

## Do Not Use When

Act on an already known useful target instead of searching again. Stop when
the goal is complete or the user cancels it.

## Method

Choose a destination coordinate, compass direction or rectangular region in
`explore_for`. You may propose a coordinate in unknown terrain. That is a search
hypothesis, not evidence that a sheep, ore or structure exists there. Combine
observed terrain with Minecraft knowledge: search likely habitats, follow a
promising terrain transition, investigate an observed landmark, or continue into
new space. Explain the choice briefly; do not move a few blocks around the same
base repeatedly when a target needs a wider search. Check difficulty and time
before relying on natural mob spawning.

Use `space=surface` for a surface search; it resolves local surface heights.
Use `underground` with a chosen `target_y` for underground searches, or `any`
when altitude is irrelevant. Surface-required and inaccessible-terrain results
are actual constraints: choose a different approach rather than repeat blindly.

Targets are optional for reconnaissance. With targets, `first_match` watches
while walking and returns immediately after discovery and physical stopping.
Use `region_budget` for a deliberate area survey that should continue past a
discovery. A survey follows orderly strips inside your selected rectangle.

## Evidence Of Success

Use returned target identities/positions for the next action. Discovery is not
collection or task completion. A completed route with `target_found=false`
means the sampled route produced no match, not that the entire biome is empty.
Searches retain geographic history across different target lists; a previous
negative sample is specific to its target list, time, radius and height.

## Failure And Adaptation

After a bounded leg, compare final position, net displacement, sample locations,
remaining route and specific failure with the intention. You choose whether to
continue toward `next_destination`, explore a different region, return or change
the objective. The runtime records history without cursor/revision paperwork.
Moving entities can enter an area previously searched, so revisiting can be a
deliberate choice. Ordinary chat does not cancel travel; explicit cancel does.

## Boundaries

All physical movement and pathfinding use the upstream Baritone Body. Respect
player-block protection and the selected search space. Environment sampling
does not claim to observe unloaded terrain.

Legacy FakePlayer tools retain their existing frontier/cursor interface; use
the parameters actually exposed by that backend.
