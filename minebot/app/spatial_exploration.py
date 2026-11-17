"""Model-directed search legs over the existing cancellable Body navigation.

The model chooses the destination, heading or rectangle. This module expands
that intention into local waypoints and samples; it does not choose a strategy.
"""

from __future__ import annotations

import math
import time

from minebot.body.exploration import CoverageStatus, ExplorationTargets, ExplorationTransactions
from minebot.brain.registry import RegisteredTool, ToolSidecar
from minebot.contract import Action, ToolResult, action_with_timeout, execution_checkpoint
from minebot.game.errors import BodyActionTimeoutError


DIRECTIONS = {
    "north": (0, -1), "northeast": (1, -1), "east": (1, 0), "southeast": (1, 1),
    "south": (0, 1), "southwest": (-1, 1), "west": (-1, 0), "northwest": (-1, -1),
}
XZ = {"type": "array", "items": {"type": "integer", "minimum": -29999984, "maximum": 29999984},
      "minItems": 2, "maxItems": 2}


def _xz(pos):
    return (float(pos[0]), float(pos[-1]))


def _distance(a, b):
    return math.dist(_xz(a), _xz(b))


def _direction(dx, dz):
    if dx == dz == 0:
        return "here"
    return ("east", "southeast", "south", "southwest", "west", "northwest", "north", "northeast")[
        round(math.atan2(dz, dx) / (math.pi / 4)) % 8]


class SpatialExploration:
    def __init__(self, exploration: ExplorationTransactions):
        self.scanner = exploration
        self.body = exploration.body
        self.coverage = exploration.coverage

    def observe(self, radius=48):
        perception = self.body.perceive("environment", {"radius": radius})
        if not perception.ok:
            return ToolResult(False, perception.error or "environment_unavailable", True)
        raw = perception.data
        pos = raw["pos"]
        sectors = {}
        for sample in raw.get("samples", []):
            x, z = sample["xz"]
            heading = _direction(x - pos[0], z - pos[2])
            sector = sectors.setdefault(heading, {"biomes": {}, "surface_blocks": {}, "height_range": [],
                                                  "water_samples": 0, "unloaded_samples": 0, "examples": []})
            if not sample.get("loaded"):
                sector["unloaded_samples"] += 1
                continue
            for field, key in (("biomes", "biome"), ("surface_blocks", "surface_block")):
                name = sample.get(key, "unknown")
                sector[field][name] = sector[field].get(name, 0) + 1
            y = sample["surface_pos"][1]
            heights = sector["height_range"]
            sector["height_range"] = [min([y, *heights]), max([y, *heights])]
            sector["water_samples"] += int(bool(sample.get("water")))
            if len(sector["examples"]) < 3:
                sector["examples"].append({k: sample[k] for k in (
                    "surface_pos", "biome", "surface_block", "standable", "canopy_height") if k in sample})
        history = sorted(self.coverage.list_regions(raw["dimension"]), key=lambda item: item.revision, reverse=True)
        recent = []
        for record in history[:16]:
            search = next((item for item in record.observations if item.get("kind") == "search_sample"), {})
            recent.append({"region": list(record.key), "pos": list(record.center),
                           "visits": record.visit_count, "last_result": record.reason,
                           **{k: search[k] for k in ("targets", "radius", "space", "target_found") if k in search},
                           "age_s": round(time.time()-search["observed_at"]) if "observed_at" in search else None})
        entities = self.body.perceive("nearbyEntities", {"radius": 64, "limit": 32})
        data = {k: v for k, v in raw.items() if k != "samples"}
        data.update(directions=sectors, sampled_bounds_xz=[[pos[0]-radius, pos[2]-radius],
                                                         [pos[0]+radius, pos[2]+radius]],
                    visited_region_count=len(history), recent_searches=recent,
                    history_bounds_xz=([[min(r.center[0] for r in history), min(r.center[2] for r in history)],
                                        [max(r.center[0] for r in history), max(r.center[2] for r in history)]] if history else None),
                    nearby_entities=list(entities.data.get("entities", []))[:16] if entities.ok else [],
                    entity_observation_error=None if entities.ok else entities.error,
                    history_scope="sampled places, not fully cleared regions; missing targets may move or spawn later",
                    coordinates="x east, z south; surface_pos is observed terrain, not a guaranteed reachable destination")
        return ToolResult(True, "environment_observed", False, metrics=data)

    def search(self, params):
        selection = {key: params[key] for key in ("destination", "direction", "region") if params.get(key) is not None}
        if len(selection) != 1:
            return ToolResult(False, "choose_one_exploration_destination", False,
                              metrics={"choices": ["destination [x,z]", "direction", "region {min,max}"],
                                       "hint": "Use observe_environment, then choose where to search. Hypothetical waypoints are allowed."})
        blocks = tuple(params.get("block_targets") or ())
        entities = tuple(params.get("entity_targets") or ())
        try:
            targets = ExplorationTargets.create(blocks=blocks, entities=entities) if blocks or entities else ExplorationTargets((), (), (), (), "survey")
            max_distance = float(params.get("max_distance", 256))
            timeout = float(params.get("timeout_s", 180))
            radius = int(params.get("scan_radius", 12))
            space = params.get("space", "surface")
            policy = params.get("return_policy", "first_match")
            if not 16 <= max_distance <= 2048 or not 1 <= timeout <= 600 or not 4 <= radius <= 32:
                raise ValueError("distance 16..2048, timeout 1..600, scan_radius 4..32 required")
            if space not in {"surface", "underground", "any"} or policy not in {"first_match", "region_budget"}:
                raise ValueError("invalid search space or return policy")
            if space == "underground" and params.get("target_y") is None:
                raise ValueError("underground search requires target_y")
            state = self.body.get_state()
            route_origin, route_index = list(state.pos), 0
            previous_search = None
            if "region" in selection and not params.get("restart_survey", False):
                previous_regions = sorted(self.coverage.list_regions(state.dimension, targets.query_signature),
                                          key=lambda item: item.revision, reverse=True)
                for record in previous_regions:
                    previous_search = next((item for item in record.observations
                        if item.get("kind") == "search_sample" and item.get("selection") == selection
                        and item.get("space") == space and item.get("radius") == radius
                        and item.get("target_y") == params.get("target_y")), None)
                    if previous_search:
                        route_origin = previous_search["route_origin"]
                        route_index = previous_search["route_index"]
                        break
            heading_distance = float(params.get("distance", max_distance))
            if not 16 <= heading_distance <= 2048:
                raise ValueError("direction distance must be 16..2048")
            waypoints = self._waypoints(selection, route_origin, heading_distance, radius)
            if previous_search and route_index < len(waypoints) and _distance(previous_search["pos"], waypoints[route_index]) <= 2:
                # The scan is written before the route advances; do not revisit
                # that already reached point after a later leg is interrupted.
                route_index += 1
        except (TypeError, ValueError, KeyError) as error:
            return ToolResult(False, "exploration_request_invalid", False, metrics={"error": str(error)})
        origin, dimension = list(state.pos), state.dimension
        started = time.monotonic()
        observations, matches, uncertainty, legs = [], {"blocks": [], "entities": []}, [], []
        distance = 0.0
        previous = state.pos
        active = None

        def result(reason, success=False, **extra):
            current = self.body.get_state()
            return ToolResult(success, reason, reason not in {"found", "route_complete", "death", "dimension_changed"}, metrics={
                "selection": selection, "space": space, "targets": targets.payload(), "origin": origin,
                "final_pos": list(current.pos), "dimension": dimension, "target_found": bool(matches["blocks"] or matches["entities"]),
                "complete": reason == "route_complete", "elapsed_s": round(time.monotonic()-started, 2),
                "sampled_distance": round(distance, 2), "distance_measurement": "sum of sampled horizontal displacements; not exact path length",
                "net_displacement": round(_distance(origin, current.pos), 2), "max_distance": max_distance,
                "scan_count": len(observations), "sampled_positions": observations[-16:], **matches,
                "uncertainty": uncertainty[-8:], "navigation_legs": legs[-8:],
                "next_destination": list(waypoints[route_index]) if route_index < len(waypoints) else None,
                "remaining_waypoints": len(waypoints)-route_index, "survey_origin": route_origin,
                "search_window": {"radius": radius, "entity_radius": max(radius, 16),
                                  "block_vertical_radius": min(20, radius*2)}, **extra})

        def scan():
            nonlocal state, distance, previous
            execution_checkpoint()
            state = self.body.get_state()
            distance += _distance(previous, state.pos)
            previous = state.pos
            if state.missing or not state.complete:
                return "body_missing"
            if state.health <= 0:
                return "death"
            if state.dimension != dimension:
                return "dimension_changed"
            found = self.scanner._scan_targets(targets, scan_radius=radius, stop_on_match=policy == "first_match")
            uncertainty.extend(found.uncertainty)
            for field in ("blocks", "entities"):
                for item in getattr(found, field):
                    if item not in matches[field]:
                        matches[field].append(item)
            if found.terminal_reason and found.terminal_reason != "scan_page_limit":
                return found.terminal_reason
            pos = [math.floor(value) for value in state.pos]
            sample = {"kind": "search_sample", "targets": targets.payload()["requested"],
                      "observed_at": time.time(), "radius": radius, "space": space,
                      "target_found": found.found, "pos": pos,
                      "selection": selection, "route_origin": route_origin, "route_index": route_index,
                      "target_y": params.get("target_y")}
            self.coverage.record_region(
                dimension=dimension, query_signature=targets.query_signature, region=(pos[0]//16, pos[2]//16),
                status=CoverageStatus.FOUND if found.found else CoverageStatus.COVERED,
                center=tuple(pos), reason="target_found" if found.found else "point_sample_no_match",
                observations=(sample, *found.blocks, *found.entities), uncertainty=found.uncertainty,
                negative_evidence=() if found.found or not (blocks or entities) else ("no_match_in_this_sample",))
            observations.append(pos)
            return "found" if found.found and policy == "first_match" else None

        def stop(reason):
            nonlocal active
            if active is not None:
                self.body.interrupt(reason)
                terminal = self.body.await_action_terminal(active.id, timeout_s=5)
                active = None
                # A discovery result is only returned once physical control has stopped.
                return terminal

        try:
            reason = scan()
            if reason:
                return result(reason, reason == "found")
            while route_index < len(waypoints):
                execution_checkpoint()
                if _distance(state.pos, waypoints[route_index]) <= 2:
                    route_index += 1
                    continue
                if time.monotonic()-started >= timeout or distance >= max_distance:
                    return result("search_budget_exhausted")
                goal = self._local_goal(state.pos, waypoints[route_index], space, params.get("target_y"))
                if isinstance(goal, str):
                    return result(goal)
                active = action_with_timeout(Action.create("navigate", {"goal": goal}),
                                             min(60, timeout-(time.monotonic()-started)))
                accepted = self.body.execute(active)
                if not accepted.ok or not accepted.accepted:
                    active = None
                    return result(accepted.error or "navigation_rejected", source=dict(accepted.data))
                legs.append({"goal": goal, "action_id": active.id})
                last_scan = time.monotonic()
                terminal = None
                while terminal is None:
                    execution_checkpoint()
                    try:
                        terminal = self.body.await_action_terminal(active.id, timeout_s=0.1)
                    except BodyActionTimeoutError:
                        pass  # The same navigation remains active while we observe it.
                    execution_checkpoint()
                    current = self.body.get_state()
                    distance += _distance(previous, current.pos)
                    previous = current.pos
                    if terminal is not None or _distance(observations[-1], current.pos) >= min(8, radius) or time.monotonic()-last_scan >= 2:
                        reason = scan()
                        last_scan = time.monotonic()
                        if reason:
                            stop(reason)
                            return result(reason, reason == "found")
                    if terminal is None and (time.monotonic()-started >= timeout or distance >= max_distance):
                        stop("search_budget_exhausted")
                        return result("search_budget_exhausted")
                active = None
                legs[-1]["reason"] = terminal.data.get("reason")
                if not terminal.data.get("success"):
                    return result(str(terminal.data.get("reason") or "navigation_failed"), source={
                        key: terminal.data[key] for key in ("reason", "status", "elapsed_ms", "last_path_event",
                            "position", "baritone_goal", "pathing", "any_process_active") if key in terminal.data})
            return result("route_complete", True)
        finally:
            # Includes external execution cancellation and perception/transport failures.
            stop("search_exit")

    def _local_goal(self, pos, destination, space, target_y):
        dx, dz = destination[0]-pos[0], destination[1]-pos[2]
        length = math.hypot(dx, dz)
        scale = min(1, 24/length)
        x, z = round(pos[0]+dx*scale), round(pos[2]+dz*scale)
        if space == "any":
            return {"kind": "xz", "x": x, "z": z}
        if space == "underground":
            return {"kind": "near", "x": x, "y": int(target_y), "z": z, "range": 1}
        columns = [[x+ox, z+oz] for ox in (-4, 0, 4) for oz in (-4, 0, 4)]
        terrain = self.body.perceive("environment", {"columns": columns})
        if not terrain.ok:
            return terrain.error or "environment_unavailable"
        if terrain.data.get("space") == "underground":
            return "surface_required"
        stands = [sample["surface_pos"] for sample in terrain.data.get("samples", [])
                  if sample.get("loaded") and sample.get("standable")]
        if not stands:
            return "surface_frontier_unloaded" if any(not s.get("loaded") for s in terrain.data.get("samples", [])) else "no_surface_stand"
        # Prefer the requested column. Adjacent stands must still advance toward it.
        stands = [stand for stand in stands if _distance(stand, destination) < _distance(pos, destination)]
        if not stands:
            return "no_surface_progress"
        stand = min(stands, key=lambda point: _distance(point, (x, z)))
        return {"kind": "near", "x": stand[0], "y": stand[1], "z": stand[2], "range": 1}

    @staticmethod
    def _waypoints(selection, pos, distance, radius):
        if "direction" in selection:
            dx, dz = DIRECTIONS[selection["direction"]]
            scale = distance/math.hypot(dx, dz)
            return [(round(pos[0]+dx*scale), round(pos[2]+dz*scale))]
        if "destination" in selection:
            if len(selection["destination"]) != 2:
                raise ValueError("destination is [x,z]; use space/target_y to select height")
            return [tuple(selection["destination"])]
        low, high = selection["region"]["min"], selection["region"]["max"]
        if len(low) != 2 or len(high) != 2 or any(high[i] < low[i] or high[i]-low[i] > 256 for i in (0, 1)):
            raise ValueError("region min/max must be ordered [x,z], at most 256 blocks per side")
        spacing = max(4, min(16, radius))
        def axis(a, b):
            return list(range(a, b, spacing)) + [b]
        xs, zs = axis(low[0], high[0]), axis(low[1], high[1])
        # Choose only the entry corner nearest the player, then cover orderly strips.
        if abs(pos[0]-xs[-1]) < abs(pos[0]-xs[0]):
            xs.reverse()
        if abs(pos[2]-zs[-1]) < abs(pos[2]-zs[0]):
            zs.reverse()
        return [(x, z) for index, z in enumerate(zs) for x in (xs if index % 2 == 0 else list(reversed(xs)))]


def search_observation(_reason, metrics):
    projected = {key: value for key, value in metrics.items() if key not in {"blocks", "entities", "navigation_legs"}}
    for field in ("blocks", "entities"):
        facts = metrics.get(field, [])
        projected[field] = facts[:16]
        projected[field+"_count"] = len(facts)
        projected[field+"_truncated"] = len(facts) > 16
    projected["navigation_legs"] = [{key: leg[key] for key in ("goal", "reason") if key in leg}
                                    for leg in metrics.get("navigation_legs", [])]
    return projected


def spatial_exploration_tools(search: SpatialExploration):
    target = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 128}, "maxItems": 32}
    return (
        RegisteredTool("observe_environment",
            "Read loaded terrain by compass direction: biomes, elevations, water, surface/underground, difficulty, "
            "nearby entities and recent searches. Use observations and Minecraft knowledge to choose promising "
            "places. Unknown terrain may be proposed as a waypoint; do not claim an unobserved target exists there.",
            {"type": "object", "properties": {"radius": {"type": "integer", "minimum": 16, "maximum": 96}},
             "additionalProperties": False},
            lambda p: search.observe(int(p.get("radius", 48))),
            ToolSidecar("observe_environment", False, source="client.environment", tool_type="perception",
                        body_scope=("state", "blocks", "entities"), timeout_s=15),
            projector=lambda reason, metrics: metrics),
        RegisteredTool("explore_for",
            "Choose exactly one destination [x,z], compass direction, or rectangular region to explore. "
            "Search for optional block/entity targets while walking; first_match stops and returns on discovery. "
            "Default space=surface uses real surface heights; choose underground with target_y or any explicitly. "
            "A waypoint is your search hypothesis, not a claim that a target is there. No targets means reconnaissance. "
            "Use region_budget to finish a survey despite discoveries. Read final_pos, net_displacement and "
            "next_destination before continuing or changing strategy. No opaque cursor is required. "
            "A completed route with target_found=false means no target was found, not that the whole area is empty.",
            {"type": "object", "properties": {
                "destination": XZ, "direction": {"type": "string", "enum": list(DIRECTIONS)},
                "region": {"type": "object", "properties": {"min": XZ, "max": XZ},
                           "required": ["min", "max"], "additionalProperties": False},
                "block_targets": target, "entity_targets": target,
                "space": {"type": "string", "enum": ["surface", "underground", "any"]},
                "target_y": {"type": "integer", "minimum": -64, "maximum": 319},
                "restart_survey": {"type": "boolean", "description": "Normally a repeated region resumes its saved route. Set true to deliberately resurvey it."},
                "max_distance": {"type": "integer", "minimum": 16, "maximum": 2048},
                "distance": {"type": "integer", "minimum": 16, "maximum": 2048,
                             "description": "Desired advance for direction mode; defaults to max_distance. max_distance bounds sampled travel including detours."},
                "timeout_s": {"type": "number", "minimum": 1, "maximum": 600},
                "scan_radius": {"type": "integer", "minimum": 4, "maximum": 32},
                "return_policy": {"type": "string", "enum": ["first_match", "region_budget"]},
            }, "additionalProperties": False}, search.search,
            ToolSidecar("explore_for", True, source="agent.spatial_exploration", tool_type="exploration",
                        permission="explore_world", body_scope=("navigation", "state", "blocks", "entities"),
                        terminal_truth=("position", "target_observation", "navigateDone"), timeout_s=610),
            projector=search_observation),
    )
