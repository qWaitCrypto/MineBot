#!/usr/bin/env python3
"""Live Body geometry probe: which mismatch first limits the Agent's chosen path?

Step 4 of the agent-native autonomy plan asks which Body geometry mismatch
first limits the *real* Agent's chosen path, and states that static move-set
gaps are not proof of live failure. So this drives the real FakePlayer Body
through the production ``move_to`` registry tool over the local test server,
one finite geometry fixture at a time, and records two independent signals per
fixture:

  * ``standing_y`` from the Body's own ``blockCells`` perception -- the
    continuous standing height the move generator now consumes. This says
    whether the Body *sees* the geometry.
  * the ``move_to`` outcome plus the observed end position -- whether the
    Body can actually *traverse* it.

Those two disagreeing is the interesting result: it separates a perception
gap from a planner gap, which a pass/fail navigation run alone cannot do.

Safety. The probe writes only into cells it has first read as air through
authoritative Body perception, and it reads the whole fixture volume, not just
the cells it writes. If any cell is occupied it refuses to build and exits
non-zero, so no existing block -- player-made or natural -- is ever broken or
overwritten. Teardown restores exactly the cells this run wrote and releases
only the chunk forceloads this run added. Loopback RCON only. No world reset,
no server restart, no external model call.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry  # noqa: E402
from minebot.body.world_read import read_block_facts, read_surface_columns  # noqa: E402
from minebot.contract.governance import Region  # noqa: E402
from minebot.game.governance import GovernancePolicy  # noqa: E402
from minebot.game.java_body import JavaBody  # noqa: E402
from minebot.game.java_body_adapter import (  # noqa: E402
    GovernanceAnswerer,
    JavaBodyClient,
    websocket_transport,
)
from minebot.game.rcon import RconClient, RconConfig  # noqa: E402

EMPTY_TYPES = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air",
               "air", "cave_air", "void_air"}

LANE_LENGTH = 6      # goal is this many blocks east of the start pad
LANE_PITCH = 8       # z spacing between lanes, so no lane can reroute via another
HALF_WIDTH = 1       # corridor is 3 cells wide on z


def fixtures(ox: int, oy: int, oz: int) -> list[dict]:
    """Finite geometry cases: a start pad, a lane expressing one geometry
    class, and a goal. Each case is self-contained; lanes are far enough apart
    on z that a failure cannot be masked by detouring through a neighbour.
    """
    cases: list[dict] = []

    def add(index: int, name: str, geometry: str, lane_blocks, goal_dy: int,
            expect_reachable: bool, standing_probe, *,
            goal_override: tuple[int, int, int] | None = None,
            in_volume: bool = True, probe_in_volume: bool = True,
            interrupt: bool = False) -> None:
        z = oz + index * LANE_PITCH
        placements: dict[tuple[int, int, int], str] = {}
        # Start pad, always at oy-1 so the body stands at oy.
        for dx in range(-2, 1):
            for dz in range(-HALF_WIDTH, HALF_WIDTH + 1):
                placements[(ox + dx, oy - 1, z + dz)] = "minecraft:stone"
        for pos, block in lane_blocks(z):
            placements[pos] = block
        cases.append({
            "name": name,
            "geometry": geometry,
            "placements": placements,
            "start": (ox, oy, z),
            "goal": goal_override if goal_override is not None
            else (ox + LANE_LENGTH, oy + goal_dy, z),
            "expect_reachable": expect_reachable,
            # A far goal must not drag the verified volume out with it.
            "in_volume": in_volume,
            "probe_in_volume": probe_in_volume,
            # Kill the body mid-route, then recover and re-issue this same goal.
            "interrupt": interrupt,
            # Feet cells whose standing height carries the geometry claim,
            # paired with the height that geometry implies (None = no dry
            # standing pose, which is itself the correct answer for water,
            # solid cells, and unloaded chunks).
            "standing_probe": [((int(p[0]), int(p[1]), int(p[2])), expected)
                               for p, expected in standing_probe(z)],
        })

    def strip(dxs, y, z, block):
        """A width-3 run of one block at ``y``, at x offsets east of the origin."""
        return [((ox + dx, y, z + dz), block)
                for dx in dxs for dz in range(-HALF_WIDTH, HALF_WIDTH + 1)]

    def pool(z, depth: int):
        """A watertight pool two cells long and ``depth`` cells deep.

        Containment is the whole point: an open-sided pool drains off the
        platform and its current shoves the body around, which produces a
        navigation failure that is about the fixture rather than the Body.
        """
        levels = [oy - 1 - d for d in range(depth)]
        floor_y = oy - 1 - depth
        blocks = []
        # Sealed floor, wider than the water so nothing leaks at the corners.
        blocks += [((ox + dx, floor_y, z + dz), "minecraft:stone")
                   for dx in (3, 4) for dz in range(-HALF_WIDTH - 1, HALF_WIDTH + 2)]
        # Side walls at every water level.
        blocks += [((ox + dx, y, z + dz), "minecraft:stone")
                   for dx in (3, 4) for y in levels
                   for dz in (-HALF_WIDTH - 1, HALF_WIDTH + 1)]
        # End walls below the walking surface, so the water cannot run out east
        # or west; at oy-1 the approach floor itself is the wall.
        blocks += [((ox + dx, y, z + dz), "minecraft:stone")
                   for dx in (2, 5) for y in levels[1:]
                   for dz in range(-HALF_WIDTH, HALF_WIDTH + 1)]
        # Approach floor either side of the pool.
        blocks += strip([1, 2, 5, 6], oy - 1, z, "minecraft:stone")
        # The water itself.
        blocks += [((ox + dx, y, z + dz), "minecraft:water")
                   for dx in (3, 4) for y in levels
                   for dz in range(-HALF_WIDTH, HALF_WIDTH + 1)]
        return blocks

    # Standing probes below are FEET cells, not the block underneath:
    # WorldView.standingHeight(x,y,z) scans collision tops within [y, y+1) and
    # requires 1.8 blocks of dry clearance above, so it answers "where does a
    # body whose feet cell is this stand?". Each carries the height the
    # geometry implies, so the artifact judges perception instead of an eyeball.

    # 1. Flat walk -- the control. A failure here indicts the probe, not geometry.
    add(0, "flat_walk", "flat stone",
        lambda z: strip(range(1, LANE_LENGTH + 1), oy - 1, z, "minecraft:stone"),
        0, True, lambda z: [((ox + 3, oy, z), float(oy))])

    # 2. Bottom slab -- a +0.5 rise that no voxel-derived endpoint can express.
    add(1, "slab_step_up", "bottom slab (+0.5) then flat at slab height",
        lambda z: strip(range(1, 3), oy - 1, z, "minecraft:stone")
        + strip(range(3, LANE_LENGTH + 1), oy - 1, z, "minecraft:stone_slab[type=bottom]"),
        0, True, lambda z: [((ox + 4, oy - 1, z), oy - 0.5)])

    # 3. Stair ascent -- a full block rise expressed as stair geometry.
    #    The stair's own upper step occupies the cell above its 0.5 landing, so
    #    standingHeight is correctly NaN *inside* the stair cell: a body cannot
    #    stand at 0.5 there with 1.8 of clearance. The walkable feet cell is the
    #    one above the stair, standing on the upper step.
    add(2, "stair_ascent", "east-facing stairs rising one block",
        lambda z: strip(range(1, 3), oy - 1, z, "minecraft:stone")
        + strip([3], oy - 1, z, "minecraft:stone_stairs[facing=east,half=bottom]")
        + strip(range(4, LANE_LENGTH + 1), oy, z, "minecraft:stone"),
        1, True, lambda z: [((ox + 3, oy, z), float(oy)),
                            ((ox + 4, oy + 1, z), float(oy + 1))])

    # 4. Stair descent -- the same geometry downhill, where the voxel graph and
    #    the physical standing pose diverge most.
    add(3, "stair_descent", "west-facing stairs dropping one block",
        lambda z: strip(range(1, 3), oy - 1, z, "minecraft:stone")
        + strip([3], oy - 2, z, "minecraft:stone_stairs[facing=west,half=bottom]")
        + strip(range(4, LANE_LENGTH + 1), oy - 2, z, "minecraft:stone"),
        -1, True, lambda z: [((ox + 3, oy - 1, z), float(oy - 1)),
                             ((ox + 4, oy - 1, z), float(oy - 1))])

    # 5. Carpet -- a 1/16 rise, far below any voxel threshold.
    add(4, "carpet_walk", "carpet (+0.0625) over flat stone",
        lambda z: strip(range(1, LANE_LENGTH + 1), oy - 1, z, "minecraft:stone")
        + strip(range(3, 5), oy, z, "minecraft:white_carpet"),
        0, True, lambda z: [((ox + 3, oy, z), oy + 0.0625)])

    # 6. Wade -- a contained one-deep pool the body can walk through. Dry
    #    standing height is NaN inside water by design: standingHeight demands a
    #    dry volume, so the swim/wade branch, not the dry move set, must carry
    #    this. ``None`` here is the correct answer, not a perception gap.
    add(5, "water_wade", "contained one-deep water, wadeable",
        lambda z: pool(z, 1), 0, True, lambda z: [((ox + 3, oy - 1, z), None)])

    # 7. Swim -- a contained three-deep pool that requires the swim move.
    add(6, "water_swim", "contained three-deep water, requires swimming",
        lambda z: pool(z, 3), 0, True, lambda z: [((ox + 3, oy - 1, z), None)])

    # 8. Sealed goal -- must produce a finite decision-grade refusal, never a
    #    hang and never a claim of arrival. Expected unreachable, and the goal
    #    cell is solid so it has no standing height at all.
    add(7, "sealed_goal", "goal walled off with no opening",
        lambda z: strip(range(1, LANE_LENGTH + 1), oy - 1, z, "minecraft:stone")
        + [((ox + 4, oy + dy, z + dz), "minecraft:stone")
           for dy in (0, 1, 2) for dz in range(-HALF_WIDTH - 1, HALF_WIDTH + 2)]
        + [((ox + LANE_LENGTH, oy + dy, z), "minecraft:stone") for dy in (0, 1)],
        0, False, lambda z: [((ox + 4, oy, z), None)])

    # 9. Loaded-world boundary -- a goal far outside the loaded/forceloaded
    #    world. The plan asks for this explicitly: the requirement is a finite
    #    refusal, not a hang and not a fabricated arrival. Nothing is built
    #    out there, and standingHeight returns NaN for an unloaded
    #    neighbourhood, which is the honest answer.
    add(8, "distant_unloaded", "goal 2000 blocks away, outside loaded chunks",
        lambda z: strip(range(1, LANE_LENGTH + 1), oy - 1, z, "minecraft:stone"),
        0, False, lambda z: [((ox + 2000, oy, z), None)],
        goal_override=(ox + 2000, oy, oz + 8 * 8), in_volume=False,
        probe_in_volume=False)

    # 10. Detour -- a wall across the lane with its only gap offset to one
    #     side. Arrival requires routing around an obstacle rather than
    #     straight-lining, so it tests replanning, not just walking.
    add(9, "detour_wall", "wall across the lane, gap offset to one side",
        lambda z: [((ox + dx, oy - 1, z + dz), "minecraft:stone")
                   for dx in range(1, LANE_LENGTH + 1) for dz in (-2, -1, 0, 1, 2)]
        + [((ox + 3, oy + dy, z + dz), "minecraft:stone")
           for dy in (0, 1) for dz in (-2, -1, 0, 1)],
        0, True, lambda z: [((ox + 3, oy, z + 2), float(oy))])

    # 11. Recovery then resumption of the ORIGINAL objective. The body is
    #     killed mid-route, which must produce an honest failure rather than a
    #     fabricated arrival; then it is respawned, returned to its pad, and
    #     the same objective is re-issued and must complete. Plain flat ground,
    #     so only the interruption is under test.
    add(10, "interrupt_resume", "killed mid-route, then same objective re-issued",
        lambda z: [((ox + dx, oy - 1, z + dz), "minecraft:stone")
                   for dx in range(1, 25) for dz in (-1, 0, 1)],
        0, True, lambda z: [((ox + 12, oy, z), float(oy))],
        goal_override=(ox + 24, oy, oz + 10 * LANE_PITCH),
        interrupt=True)

    return cases


def volume_cells(cases: list[dict]) -> tuple[list[tuple[int, int, int]],
                                             tuple[int, int, int], tuple[int, int, int]]:
    """Every cell the fixtures depend on: what gets written, plus the free
    space around it. The fixtures only mean anything in a void, so the whole
    bounding box is checked, not just the written cells.
    """
    xs, ys, zs = [], [], []
    for case in cases:
        for x, y, z in case["placements"]:
            xs.append(x); ys.append(y); zs.append(z)
        if case["in_volume"]:
            gx, gy, gz = case["goal"]
            xs.append(gx); ys.append(gy); zs.append(gz)
    low = (min(xs) - 1, min(ys) - 1, min(zs) - 2)
    high = (max(xs) + 1, max(ys) + 3, max(zs) + 2)
    cells = [(x, y, z)
             for x in range(low[0], high[0] + 1)
             for y in range(low[1], high[1] + 1)
             for z in range(low[2], high[2] + 1)]
    return cells, low, high


def chunk_span(low, high) -> list[tuple[int, int]]:
    return [(cx, cz)
            for cx in range(low[0] >> 4, (high[0] >> 4) + 1)
            for cz in range(low[2] >> 4, (high[2] >> 4) + 1)]


def forceload_add(rcon: RconClient, chunks) -> list[tuple[int, int]]:
    """Force-load the probe area, returning only the chunks this run added so
    teardown never releases a forceload that was already there."""
    added = []
    for cx, cz in chunks:
        bx, bz = cx * 16, cz * 16
        if "not marked" not in rcon.command(f"forceload query {bx} {bz}").lower():
            continue  # already forceloaded by someone else; leave it alone
        rcon.command(f"forceload add {bx} {bz}")
        added.append((cx, cz))
    return added


def _kill_after(args, delay_s: float) -> None:
    """Kill the FakePlayer after a delay, on its own RCON connection.

    A separate connection because the main thread is blocked inside the
    navigation transaction while this fires; the interruption has to be real
    and concurrent, not staged before or after the action.
    """
    time.sleep(delay_s)
    try:
        killer = RconClient(RconConfig(host="127.0.0.1", port=args.rcon_port,
                                       password=args.rcon_password))
        killer.connect()
        killer.command(f"player {args.bot} kill")
        killer.close()
    except Exception as exc:  # noqa: BLE001 -- reported by the case's evidence
        print(f"[geometry] interrupt kill failed: {type(exc).__name__}", flush=True)


def bot_pos(rcon: RconClient, bot: str) -> list[float] | None:
    nums = re.findall(r"(-?\d+\.\d+)d", rcon.command(f"data get entity {bot} Pos"))
    return [float(n) for n in nums[:3]] if len(nums) >= 3 else None


def bot_alive(rcon: RconClient, bot: str) -> bool:
    return "data" in rcon.command(f"data get entity {bot} Pos")


def ensure_alive(rcon: RconClient, bot: str) -> bool:
    """Keep the FakePlayer present. Returns True if a respawn was needed, which
    is an honest death to record against whatever case caused it."""
    if bot_alive(rcon, bot):
        return False
    rcon.command(f"player {bot} spawn")
    time.sleep(2.5)
    rcon.command(f"gamemode survival {bot}")
    return True


def await_body(body: JavaBody, rcon: RconClient, bot: str,
               *, attempts: int = 12) -> None:
    """Block until the Body answers a trivial perception.

    A FakePlayer that is spawning, dying, or respawning is briefly absent, and
    the bridge answers ``body_missing`` for that window. Treating that as a
    geometry result would be a lie, so wait for a real answer instead.
    """
    for attempt in range(attempts):
        ensure_alive(rcon, bot)
        probe = body.perceive("blockCells", {"cells": [[0, 0, 0]], "start": 0, "limit": 1})
        if probe.ok and probe.complete:
            return
        time.sleep(1.5)
    raise RuntimeError(f"Body for {bot} never became readable after {attempts} attempts")


def survey(body: JavaBody, cases: list[dict], low, high, args_oy: int | None = None) -> dict:
    """Read-only terrain report for the fixture footprint.

    The fixtures need empty space, and a platform far above the ground turns
    every planner mistake into a fatal fall, which buries the geometry signal
    under respawn noise. So report the real surface and let the caller place
    the fixtures just above it.
    """
    columns = tuple((x, z)
                    for x in range(low[0], high[0] + 1)
                    for z in range(low[2], high[2] + 1))
    facts = read_surface_columns(body, columns, page_size=64, failure_label="survey")
    heights = sorted(int(fact.feet_y) for fact in facts.values())
    # The deepest fixture (a three-deep pool) writes down to oy-4 and the
    # verified volume extends one below that, so the platform must clear the
    # highest surface by 6 to keep the whole volume in open air.
    depth_below_oy = int(low[1] - args_oy) if args_oy is not None else -5
    return {
        "columns": len(facts),
        "surface_min": heights[0],
        "surface_max": heights[-1],
        "volume_extends_below_oy": depth_below_oy,
        "suggested_oy": heights[-1] + 1 - depth_below_oy,
    }


def run(args: argparse.Namespace) -> int:
    rcon = RconClient(RconConfig(host="127.0.0.1", port=args.rcon_port,
                                 password=args.rcon_password))
    rcon.connect()

    cases = fixtures(args.ox, args.oy, args.oz)
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        unknown = wanted.difference({case["name"] for case in cases})
        if unknown:
            print(f"[geometry] unknown case(s): {sorted(unknown)}", flush=True)
            return 2
        cases = [case for case in cases if case["name"] in wanted]
    cells, low, high = volume_cells(cases)
    chunks = chunk_span(low, high)
    written: list[tuple[int, int, int]] = []
    forceloaded: list[tuple[int, int]] = []
    client = None
    results: list[dict] = []
    standing: dict[str, dict] = {}
    verdict = "incomplete"

    natural = Region("geometry-probe", (low[0] - 16, 0, low[2] - 16),
                     (high[0] + 16, 320, high[2] + 16))
    policy = GovernancePolicy(natural_regions=[natural])

    try:
        forceloaded = forceload_add(rcon, chunks)
        print(f"[geometry] volume {low}..{high} = {len(cells)} cells, "
              f"{len(chunks)} chunks ({len(forceloaded)} newly forceloaded)", flush=True)

        rcon.command(f"player {args.bot} spawn")
        time.sleep(2.5)
        rcon.command(f"gamemode survival {args.bot}")

        client = JavaBodyClient(args.bot, websocket_transport(args.url),
                                GovernanceAnswerer(policy),
                                action_wall_timeout_s=args.action_timeout_s,
                                recv_timeout_s=2.0)
        client.connect()
        body = JavaBody(client, args.bot)
        # The bot stays wherever it spawned until the fixtures exist. Teleporting
        # it onto a pad that has not been built yet drops it through open air,
        # and the resulting death reads as a Body failure that never happened.
        await_body(body, rcon, args.bot)

        registry = build_phase1_registry(
            body,
            Phase1RuntimeConfig(natural_region=natural, body_provider="java",
                                governance_policy=policy),
        )
        move_to = registry.get("move_to").callable

        if args.survey:
            report = survey(body, cases, low, high, args.oy)
            print(f"[geometry] survey {report}", flush=True)
            verdict = "survey"
            return 0

        # --- Emptiness: authoritative Body perception, not a fill-count trick.
        # /fill ... replace air reports 0 because setBlock returns false when
        # the state is unchanged, so it cannot distinguish "empty" from
        # "occupied". blockCells reads the real block at every cell and raises
        # rather than inventing one.
        t0 = time.time()
        facts = read_block_facts(body, tuple(cells), page_size=256,
                                 failure_label="geometry-probe emptiness")
        occupied = sorted(pos for pos, fact in facts.items()
                          if str(fact.data.get("type") or "") not in EMPTY_TYPES)
        print(f"[geometry] read {len(facts)} cells in {round(time.time()-t0,1)}s; "
              f"occupied={len(occupied)}", flush=True)
        if occupied:
            sample = [list(p) for p in occupied[:6]]
            print(f"[geometry] REFUSING to build: {len(occupied)} of {len(cells)} cells "
                  f"are not air, e.g. {sample}. Run --survey and pick an empty "
                  f"origin with --ox/--oy/--oz.", flush=True)
            verdict = "refused_non_empty_volume"
            return 2

        # --- Build, then let the chunks settle (water flows, stairs re-shape).
        for case in cases:
            for (x, y, z), block in case["placements"].items():
                rcon.command(f"setblock {x} {y} {z} {block} replace")
                written.append((x, y, z))
        time.sleep(2.0)
        print(f"[geometry] placed {len(written)} blocks", flush=True)

        # --- Signal 1: does the Body see the geometry it would stand on?
        probe_cells = tuple({pos for case in cases if case["probe_in_volume"]
                             for pos, _ in case["standing_probe"]})
        heights = read_block_facts(body, probe_cells, page_size=256,
                                   failure_label="standing height",
                                   include_standing=True)
        for case in cases:
            cells_out = []
            for pos, expected in case["standing_probe"]:
                fact = heights.get(pos)
                if fact is None:
                    # Deliberately unread: outside the verified volume, e.g.
                    # the unloaded-boundary lane. Say so rather than guess.
                    cells_out.append({"pos": list(pos), "read": False,
                                      "expected_standing_y": expected})
                    continue
                actual = fact.data.get("standing_y")
                matched = (actual is None) if expected is None else (
                    actual is not None and abs(float(actual) - expected) < 1e-6)
                cells_out.append({
                    "pos": list(pos), "read": True,
                    "type": fact.data.get("type"), "state": fact.data.get("state"),
                    "standing_y": actual, "expected_standing_y": expected,
                    "matched": matched,
                })
            case["standing_matched"] = all(c.get("matched", True) for c in cells_out)
            standing[case["name"]] = {
                "geometry": case["geometry"],
                "matched": case["standing_matched"],
                "cells": cells_out,
            }
            shown = ", ".join(
                f"{str(c.get('type','?')).replace('minecraft:','')}"
                f"@{c.get('standing_y')}(want {c['expected_standing_y']})"
                for c in cells_out
            )
            print(f"[geometry] standing {case['name']:<17} "
                  f"{'ok ' if case['standing_matched'] else 'MISMATCH'} {shown}",
                  flush=True)

        # --- Signal 2: can the Body traverse it, through the production tool?
        for case in cases:
            sx, sy, sz = case["start"]
            respawned = ensure_alive(rcon, args.bot)
            rcon.command(f"tp {args.bot} {sx} {sy} {sz}")
            time.sleep(1.5)
            before = bot_pos(rcon, args.bot)
            # A body that is not actually standing on its start pad cannot
            # produce evidence about the lane, so say so instead of scoring it.
            staged = bool(before is not None and abs(before[1] - sy) <= 1.0
                          and abs(before[0] - sx) <= 1.5 and abs(before[2] - sz) <= 1.5)
            started = time.time()
            interrupted: dict[str, object] | None = None
            if case["interrupt"]:
                # Kill the body a moment into the route. This is a real
                # interruption, so the first attempt must fail honestly; a
                # reported arrival here would be a fabrication.
                killer = threading.Thread(
                    target=_kill_after,
                    args=(args, args.interrupt_after_s),
                    daemon=True,
                )
                killer.start()
                first = move_to({"pos": list(case["goal"]), "radius": 0,
                                 "timeout_s": args.action_timeout_s})
                killer.join(timeout=10.0)
                first_elapsed = round(time.time() - started, 2)
                died_mid_route = not bot_alive(rcon, args.bot)
                pos_at_interrupt = bot_pos(rcon, args.bot)
                # Recovery: bring the body back and return it to its pad.
                ensure_alive(rcon, args.bot)
                await_body(body, rcon, args.bot)
                rcon.command(f"tp {args.bot} {sx} {sy} {sz}")
                time.sleep(2.0)
                interrupted = {
                    "first_attempt_success": bool(first.success),
                    "first_attempt_reason": str(first.reason),
                    "first_attempt_elapsed_s": first_elapsed,
                    "body_died_mid_route": died_mid_route,
                    "pos_at_interrupt": pos_at_interrupt,
                    # The whole point: an interrupted route must not report
                    # arrival. If it did, that is the finding.
                    "honest_interruption": not bool(first.success),
                }
                print(f"[geometry] interrupt {case['name']:<15} first="
                      f"{'ok' if first.success else 'FAIL'}/{first.reason} "
                      f"died={died_mid_route} -> resuming original objective",
                      flush=True)
                started = time.time()
            result = move_to({"pos": list(case["goal"]), "radius": 0,
                              "timeout_s": args.action_timeout_s})
            elapsed = round(time.time() - started, 2)
            died = not bot_alive(rcon, args.bot)
            after = bot_pos(rcon, args.bot)
            gx, gy, gz = case["goal"]
            # radius:0 asks for that block, so judge by block coordinates
            # rather than a loose window -- a wide tolerance let a body that
            # had been shoved a block sideways read as "arrived".
            reached = bool(
                after is not None and not died
                and math.floor(after[0]) == gx and math.floor(after[2]) == gz
                and abs(math.floor(after[1]) - gy) <= 1
            )
            # A case is honest when the outcome matches the geometry: reachable
            # lanes must arrive, an unreachable lane must refuse without
            # claiming arrival.
            honest = (bool(result.success) and reached) if case["expect_reachable"] \
                else (not result.success and not reached)
            # Refusals are not interchangeable. A planner that concluded
            # "no path" made a decision the Agent can act on; one that ran out
            # of wall clock only reports that it stopped, which is honest but
            # much weaker evidence and much more expensive.
            refusal_quality = None
            if not case["expect_reachable"]:
                timed_out = ("timeout" in str(result.reason).lower()
                             or elapsed >= args.action_timeout_s - 2.0)
                refusal_quality = "wall_clock_exhaustion" if timed_out else "decision_grade"
            results.append({
                "case": case["name"], "geometry": case["geometry"],
                "start": list(case["start"]), "goal": list(case["goal"]),
                "expect_reachable": case["expect_reachable"],
                "staged_on_start_pad": staged,
                "standing_matched": bool(case.get("standing_matched", True)),
                "success": bool(result.success), "reason": str(result.reason),
                "reached_goal": reached, "honest": honest and staged,
                "refusal_quality": refusal_quality,
                "interruption": interrupted,
                "died_during_case": died, "respawned_before_case": respawned,
                "elapsed_s": elapsed, "pos_before": before, "pos_after": after,
                "metrics": {k: v for k, v in (result.metrics or {}).items()
                            if k in {"steps", "nodes", "reason", "distance",
                                     "pathLength", "aborted", "stopReason"}},
            })
            print(f"[geometry] move    {case['name']:<15} "
                  f"{'ok' if result.success else 'FAIL'}/{result.reason} "
                  f"reached={reached} staged={staged} died={died} {elapsed}s", flush=True)
        verdict = "complete"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001 -- teardown must still run
                print(f"[geometry] client close failed: {type(exc).__name__}", flush=True)
        rcon.command(f"player {args.bot} kill")
        for x, y, z in written:
            rcon.command(f"setblock {x} {y} {z} minecraft:air replace")
        for cx, cz in forceloaded:
            rcon.command(f"forceload remove {cx * 16} {cz * 16}")
        print(f"[geometry] teardown: cleared {len(written)} blocks, "
              f"released {len(forceloaded)} forceloads", flush=True)
        rcon.close()

    first_gap = next((r for r in results if not r["honest"]), None)
    perception_gap = next((r for r in results if not r["standing_matched"]), None)
    weak_refusals = [r["case"] for r in results
                     if r.get("refusal_quality") == "wall_clock_exhaustion"]
    payload = {
        "verdict": verdict,
        "bot": args.bot,
        "volume": {"low": list(low), "high": list(high), "cells": len(cells)},
        "emptiness_check": "authoritative blockCells perception; refused if any cell non-air",
        "standing_height": standing,
        "cases": results,
        "first_limiting_geometry": None if first_gap is None else {
            "case": first_gap["case"], "geometry": first_gap["geometry"],
            "reason": first_gap["reason"], "reached_goal": first_gap["reached_goal"],
            "expect_reachable": first_gap["expect_reachable"],
        },
        "first_perception_gap": None if perception_gap is None else {
            "case": perception_gap["case"], "geometry": perception_gap["geometry"],
        },
        "wall_clock_refusals": weak_refusals,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"[geometry] artifact -> {out}", flush=True)
    if first_gap is None:
        print("[geometry] traversal: every fixture behaved as its geometry requires",
              flush=True)
    else:
        print(f"[geometry] first limiting geometry: {first_gap['case']} "
              f"({first_gap['geometry']}) -> {first_gap['reason']}", flush=True)
    if perception_gap is not None:
        print(f"[geometry] first perception gap: {perception_gap['case']} "
              f"(standing height disagreed with the built geometry)", flush=True)
    if weak_refusals:
        print(f"[geometry] refused only by running out of wall clock: "
              f"{weak_refusals} -- honest, but not a decision the Agent can act on",
              flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ox", type=int, default=8)
    parser.add_argument("--oy", type=int, default=74)
    parser.add_argument("--oz", type=int, default=8)
    parser.add_argument("--survey", action="store_true",
                        help="Read-only: report the footprint's real surface "
                             "heights and a safe --oy, then exit without building.")
    parser.add_argument("--url", default="ws://127.0.0.1:8767")
    parser.add_argument("--bot", default="GeometryProbe")
    parser.add_argument("--rcon-port", type=int, default=25576, dest="rcon_port")
    parser.add_argument("--rcon-password", dest="rcon_password",
                        default=os.environ.get("MINEBOT_REAL_RCON_PASSWORD", "test"))
    parser.add_argument("--interrupt-after-s", type=float, default=1.5,
                        dest="interrupt_after_s",
                        help="How far into the route the interrupt case kills the body.")
    parser.add_argument("--only", default="",
                        help="Comma-separated case names to run in isolation, "
                             "e.g. --only distant_unloaded")
    parser.add_argument("--action-timeout-s", type=float, default=45.0,
                        dest="action_timeout_s")
    parser.add_argument("--out",
                        default="logs/agentic-runtime/body-geometry-probe.json")
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
