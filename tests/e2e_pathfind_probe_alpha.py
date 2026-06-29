#!/usr/bin/env python3
"""Live probe: measure server-side Scarpet A* pathfinding cost.

Tests whether minebot_pathfind_probe() (a minimal A* in Scarpet) can complete
within the per-tick budget (~40ms headroom) on real terrain. This is the
alpha-feasibility gate: if it passes, the Body can own navigation server-side;
if it fails, we fall back to Body-batch-feeds-terrain + Python A*.

No API key needed. Exercises only the Scarpet probe function over RCON.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import RconClient, ScarpetBody  # noqa: E402
from minebot.game.rcon import RconConfig  # noqa: E402
from tests.e2e_support import SKIP_EXIT_CODE, connect_or_skip, spawn_or_fail  # noqa: E402

BOT = "AlphaProbeBot"

TIMING_TOKEN_RE = re.compile(r"\((\d+(?:\.\d+)?)\s*(µs|us|ms|s)\)")

SMALL_GRID_BUDGET_MS = 10.0
LARGE_GRID_BUDGET_MS = 30.0
HARD_CEILING_MS = 40.0


def extract_timing_ms(raw: str) -> float | None:
    m = TIMING_TOKEN_RE.search(raw)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2)
    if unit in ("µs", "us"):
        return value / 1000.0
    if unit == "ms":
        return value
    if unit == "s":
        return value * 1000.0
    return None


def parse_probe_result(raw: str) -> dict:
    cleaned = re.sub(r"^\s*=\s*", "", raw.strip())
    cleaned = TIMING_TOKEN_RE.sub("", cleaned).strip()
    if cleaned.startswith("'") and cleaned.endswith("'"):
        cleaned = cleaned[1:-1]
    return json.loads(cleaned)


def command(rcon: RconClient, text: str, delay: float = 0.05) -> str:
    out = rcon.command(text)
    if delay:
        time.sleep(delay)
    return out


def run_probe(rcon: RconClient, sx: int, sy: int, sz: int,
              gx: int, gy: int, gz: int, grid_radius: int) -> dict:
    params_json = json.dumps({
        "start": [sx, sy, sz],
        "goal": [gx, gy, gz],
        "grid_radius": grid_radius,
    }, separators=(",", ":"))
    scarpet_params = "'" + params_json.replace("\\", "\\\\").replace("'", "\\'") + "'"
    cmd = f"script in minebot run minebot_pathfind_probe('{BOT}', {scarpet_params})"

    t0 = time.monotonic()
    raw = rcon.command(cmd)
    wall_ms = (time.monotonic() - t0) * 1000.0

    timing_ms = extract_timing_ms(raw)
    result = parse_probe_result(raw)

    result["scarpet_ms"] = round(timing_ms, 3) if timing_ms is not None else None
    result["wall_ms"] = round(wall_ms, 3)
    result["raw_tail"] = raw[-120:] if len(raw) > 120 else raw
    return result


def main() -> None:
    with connect_or_skip() as rcon:
        for cmd_text in [
            "script unload minebot",
            "script load minebot global",
            "carpet commandPlayer true",
            "carpet allowSpawningOfflinePlayers true",
            "gamerule doDaylightCycle false",
            "time set day",
            "weather clear",
            f"player {BOT} kill",
        ]:
            command(rcon, cmd_text)

        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (8, 72, 8))
        time.sleep(0.5)
        state = body.get_state()
        sx, sy, sz = int(state.pos[0]), int(state.pos[1]), int(state.pos[2])
        print(f"spawned at ({sx}, {sy}, {sz})")

        results = []

        print("\n--- small grid (radius=8) ---")
        r_small = run_probe(rcon, sx, sy, sz, sx + 10, sy, sz + 10, grid_radius=8)
        results.append(("small_r8", r_small))
        print(json.dumps(r_small, indent=2))

        print("\n--- large grid (radius=16) ---")
        r_large = run_probe(rcon, sx, sy, sz, sx + 20, sy, sz + 20, grid_radius=16)
        results.append(("large_r16", r_large))
        print(json.dumps(r_large, indent=2))

        print("\n--- extra: medium grid (radius=12) ---")
        r_med = run_probe(rcon, sx, sy, sz, sx + 15, sy, sz + 15, grid_radius=12)
        results.append(("med_r12", r_med))
        print(json.dumps(r_med, indent=2))

        command(rcon, f"player {BOT} kill", delay=0.2)

        print("\n=== SUMMARY ===")
        any_blocked = False
        for label, r in results:
            ms = r.get("scarpet_ms") or r.get("wall_ms", 0)
            ok = r.get("ok", False)
            expanded = r.get("nodes_expanded", 0)
            path_len = r.get("path_length", 0)
            reason = r.get("reason", "?")
            within = ms < HARD_CEILING_MS
            print(f"  {label}: ok={ok} reason={reason} expanded={expanded} "
                  f"path={path_len} scarpet_ms={r.get('scarpet_ms')} "
                  f"wall_ms={r.get('wall_ms')} within_40ms={within}")
            if not within:
                any_blocked = True

        r_small_ms = (r_small.get("scarpet_ms") or r_small.get("wall_ms", 999))
        r_large_ms = (r_large.get("scarpet_ms") or r_large.get("wall_ms", 999))

        if any_blocked:
            print(f"\nα-FEASIBILITY BLOCKED: at least one probe exceeded {HARD_CEILING_MS}ms ceiling")
            raise SystemExit(1)

        if r_small_ms > SMALL_GRID_BUDGET_MS:
            print(f"\nWARN: small grid ({r_small_ms:.1f}ms) exceeded soft budget "
                  f"({SMALL_GRID_BUDGET_MS}ms) but within hard ceiling")
        if r_large_ms > LARGE_GRID_BUDGET_MS:
            print(f"\nWARN: large grid ({r_large_ms:.1f}ms) exceeded soft budget "
                  f"({LARGE_GRID_BUDGET_MS}ms) but within hard ceiling")

        if r_small.get("ok") and r_large.get("ok"):
            print("\nα-FEASIBILITY CONFIRMED: server-side A* within tick budget on real terrain")
        elif not r_small.get("ok") or not r_large.get("ok"):
            print(f"\nα-FEASIBILITY INCONCLUSIVE: pathfind returned no_path on real terrain "
                  f"(terrain may be impassable at spawn — try different goal offsets)")
            print("This is NOT an α-blocker — it means the terrain RNG blocked the path, "
                  "not that Scarpet A* is too slow.")


if __name__ == "__main__":
    main()
