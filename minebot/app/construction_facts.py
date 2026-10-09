"""Bounded world evidence for one direct cuboid construction call."""
from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

MAX_CELLS = 4096


def cuboid_cells(params: dict[str, Any]) -> list[list[int]]:
    low, size = params["anchor"], params["size"]
    cells = []
    for x in range(low[0], low[0] + size[0]):
        for y in range(low[1], low[1] + size[1]):
            for z in range(low[2], low[2] + size[2]):
                cells.append([x, y, z])
                if len(cells) >= MAX_CELLS:
                    return cells
    return cells


def read_construction(body, params: dict[str, Any], *, budget_s: float = 3.0) -> dict[str, Any]:
    cells = cuboid_cells(params)
    wanted = params["block"] if ":" in params["block"] else "minecraft:" + params["block"]
    block, _, properties = wanted.partition("[")
    expected = dict(entry.split("=", 1) for entry in properties.rstrip("]").split(",") if entry)
    matched, remaining, unknown = [], [], []
    started = time.monotonic()
    for start in range(0, len(cells), 128):
        batch = cells[start:start + 128]
        if time.monotonic() - started >= budget_s:
            unknown.extend(cells[start:])
            break
        try:
            read = body.perceive("blockCells", {"cells": batch})
            facts = read.data.get("cells", []) if read.ok else []
        except Exception:
            facts = []
        if not isinstance(facts, list):
            facts = []
        by_pos = {(f.get("x"), f.get("y"), f.get("z")): f for f in facts if isinstance(f, Mapping)}
        for pos in batch:
            fact = by_pos.get(tuple(pos))
            if fact is None or fact.get("state") == "UNKNOWN" or not isinstance(fact.get("type"), str):
                unknown.append(pos)
            elif fact.get("type") == block and all(
                str(fact.get("properties", {}).get(key)) == value for key, value in expected.items()
            ):
                matched.append(pos)
            else:
                remaining.append({"pos": pos, "actual": fact.get("type")})
    expected = params["size"][0] * params["size"][1] * params["size"][2]
    return {"anchor": list(params["anchor"]), "size": list(params["size"]), "block": wanted,
            "expected_cells": expected, "matched_cells": len(matched),
            "matched_positions": matched, "remaining_cells": remaining,
            "unknown_cells": unknown, "unread_cells": max(0, expected - len(cells)),
            "complete": not unknown and expected == len(cells),
            "observed_at_ms": int(time.time() * 1000),
            "read_elapsed_s": round(time.monotonic() - started, 3)}


def construction_delta(before, after):
    old = {tuple(pos) for pos in before.get("matched_positions", [])}
    new = [pos for pos in after["matched_positions"] if tuple(pos) not in old]
    return {**after, "previously_matching_cells": len(old), "newly_matching_cells": len(new),
            "attributable_placed_cells": None,
            "attribution": "World difference only; matching blocks are not a native placement counter.",
            "resume_rule": "Keep matching cells. Reread unknown cells before writing; fill only confirmed remaining cells."}
