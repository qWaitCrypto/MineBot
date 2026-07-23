#!/usr/bin/env python3
"""Survey authoritative resource and entity distances around golden spawn.

The probe uses the production ``perceive_find_blocks`` path and a temporary
Scarpet Body.  It never navigates or mutates blocks; the temporary probe body
is removed before exit.  A missing body or malformed perception response is a
hard failure rather than a successful-looking empty survey.
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import ScarpetBody  # noqa: E402
from minebot.game.rcon import RconClient, RconConfig  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402

BOT = "GoldSurvey"


def _val(raw: str) -> str:
    if " = " in raw:
        return raw.split(" = ", 1)[1].rsplit(" (", 1)[0].strip()
    return raw.strip()


def main() -> int:
    with connect_or_skip(RconConfig()) as c:
        body = ScarpetBody(BOT, c)
        spawn_or_fail(body, (0, 70, 0))

        def run_raw(expr: str) -> str:
            raw = _val(c.request(f"script in minebot run {expr}"))
            if "Error while evaluating expression" in raw:
                raise AssertionError(raw)
            return raw

        def find_blocks(label: str, expr: str) -> list[dict[str, object]]:
            raw = run_raw(expr)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AssertionError(f"{label} returned non-JSON: {raw[:300]!r}") from exc
            if payload.get("ok") is not True or payload.get("error"):
                raise AssertionError({"label": label, "payload": payload})
            data = payload.get("data") or {}
            if data.get("missing") is True:
                raise AssertionError({"label": label, "error": "missing_body"})
            blocks = data.get("blocks") or []
            if not isinstance(blocks, list):
                raise AssertionError({"label": label, "blocks": blocks})
            return [block for block in blocks if isinstance(block, dict)]

        def nearest(blocks: list[dict[str, object]]) -> tuple[int, list[object] | None, str | None] | None:
            ranked: list[tuple[float, dict[str, object]]] = []
            for block in blocks:
                try:
                    x = float(block["x"])
                    z = float(block["z"])
                except (KeyError, TypeError, ValueError):
                    continue
                ranked.append((math.hypot(x, z), block))
            if not ranked:
                return None
            distance, block = min(ranked, key=lambda item: item[0])
            return (
                round(distance),
                [block.get("x"), block.get("y"), block.get("z")],
                str(block.get("type")) if block.get("type") is not None else None,
            )

        flowers = [
            "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
            "oxeye_daisy", "cornflower", "lily_of_the_valley", "orange_tulip",
        ]
        types_list = "l(" + ",".join(f"'minecraft:{f}'" for f in flowers) + ")"
        try:
            log_blocks = find_blocks(
                "logs",
                f"perceive_find_blocks('{BOT}', {{'type'->'log','radius'->128,'y_radius'->40,'limit'->128}})",
            )
            flower_blocks = find_blocks(
                "flowers",
                f"perceive_find_blocks('{BOT}', {{'types'->{types_list},'radius'->128,'y_radius'->40,'limit'->128}})",
            )
            print("nearest log / nearest flower from spawn (dist, pos, type):")
            print("  log:", nearest(log_blocks))
            print("  flower:", nearest(flower_blocks))

            print("\nnearest animals from spawn (entity_selector @e):")
            for kind in ("pig", "cow", "sheep"):
                expr = (
                    f"(es = entity_selector('@e[type=minecraft:{kind}]');"
                    f" best=null; nd=1e9;"
                    f" for(es, p=query(_,'pos'); d=(p:0)*(p:0)+(p:2)*(p:2);"
                    f"   if(d<nd, nd=d; best=l(round(p:0),round(p:1),round(p:2))));"
                    f" l(length(es), best, if(best==null,-1,round(sqrt(nd)))))"
                )
                raw = run_raw(expr)
                if raw.startswith("Error"):
                    raise AssertionError({"entity": kind, "raw": raw})
                print(f"  {kind}: [count, nearest_pos, dist] = {raw}")
        finally:
            c.command(f"player {BOT} kill")
            time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
