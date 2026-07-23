#!/usr/bin/env python3
"""Authoritative nearest-resource survey via the production findBlocks perception.

Spawns a temporary probe fake player at spawn, uses the real perception path the
bot uses, records nearest flower/log/ore distances, then removes the probe.
Read-only w.r.t. the world (spawns/despawns only a probe entity, no block
mutation).
"""

from __future__ import annotations

import json
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

        def run(expr: str) -> str:
            raw = _val(c.request(f"script in minebot run {expr}"))
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AssertionError(f"survey expression returned non-JSON: {raw[:300]!r}") from exc
            if payload.get("ok") is not True or payload.get("error"):
                raise AssertionError({"survey_error": payload})
            if payload.get("data", {}).get("missing") is True:
                raise AssertionError({"survey_error": "missing_body", "payload": payload})
            return raw

        flowers = [
            "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
            "oxeye_daisy", "cornflower", "lily_of_the_valley", "orange_tulip",
        ]
        types_list = "l(" + ",".join(f"'minecraft:{f}'" for f in flowers) + ")"
        try:
            for label, expr in (
                (
                    "flowers(r128)",
                    f"perceive_find_blocks('{BOT}', {{'types'->{types_list},'radius'->128,'y_radius'->40,'limit'->5}})",
                ),
                (
                    "logs(r128)",
                    f"perceive_find_blocks('{BOT}', {{'type'->'log','radius'->128,'y_radius'->40,'limit'->5}})",
                ),
                (
                    "iron_ore(r128)",
                    f"perceive_find_blocks('{BOT}', {{'type'->'iron_ore','radius'->128,'y_radius'->48,'limit'->3}})",
                ),
            ):
                print(f"--- {label} ---")
                print("  ", run(expr)[:1200])
        finally:
            c.command(f"player {BOT} kill")
            time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
