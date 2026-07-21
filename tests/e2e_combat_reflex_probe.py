#!/usr/bin/env python3
"""Live probe: validate the defensive combat reflex (S6).

An IDLE bot (survival, no brain action) stands near a real husk. The husk
damages the bot, which emits ``underAttack`` and starts the Body-owned
``combat_flee`` reflex. The reflex reaches its local escape target, emits its
terminal event, and releases ownership.

The reflex deliberately does not choose whether to fight or whom to attack:
that is an Agent decision through the ordinary combat tools. This probe only
verifies the Body's immediate defensive interruption chain.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.game import ScarpetBody  # noqa: E402
from tests.e2e_support import connect_or_skip, spawn_or_fail  # noqa: E402

BOT = "ReflexProbe"


def _count(rcon, kind: str) -> int:
    out = rcon.command(f"script run length(entity_selector('@e[type={kind}]'))")
    try:
        return int(out.split("=")[-1].split("(")[0].strip())
    except Exception:
        return -1


def main():
    with connect_or_skip() as rcon:
        for cmd in [
            "script unload minebot", "script resume", "script load minebot global",
            "carpet commandPlayer true", "carpet allowSpawningOfflinePlayers true",
            "gamerule doDaylightCycle false", "gamerule doMobSpawning false",
            "difficulty normal", "time set 18000", "weather clear",
            f"player {BOT} kill",
            "kill @e[type=zombie]", "kill @e[type=husk]", "kill @e[type=skeleton]",
            "fill 10 69 10 36 76 36 air",
            "fill 10 69 10 36 69 36 stone",
        ]:
            rcon.command(cmd)
            time.sleep(0.05)
        time.sleep(0.3)

        try:
            body = ScarpetBody(BOT, rcon)
            spawn_or_fail(body, (23, 70, 23))
            rcon.command(f"gamemode survival {BOT}")
            time.sleep(0.3)
            # A single attacker leaves a clear, eight-block flee lane inside the
            # controlled platform. The reflex must flee, not select an engage goal.
            rcon.command("summon husk 23 70 25 {NoAI:0b,PersistenceRequired:1b}")
            time.sleep(1.0)

            print(f"baseline: bot hp={body.get_state().health} husk={_count(rcon,'husk')}")

            t0 = time.monotonic()
            hp_dropped = False
            has_under_attack = False
            combat_flee_triggered = None
            combat_flee_completed = None
            timeline = []
            while time.monotonic() - t0 < 12.0:
                st = body.get_state()
                hp = st.health
                if hp < 20.0:
                    hp_dropped = True
                hc = _count(rcon, "husk")
                timeline.append((round(time.monotonic() - t0, 2), round(hp, 2), hc))

                for event in body.poll_events():
                    if event.name == "underAttack":
                        has_under_attack = True
                    elif event.name == "reflexTriggered" and event.data.get("kind") == "combat_flee":
                        combat_flee_triggered = event
                    elif event.name == "reflexCompleted" and event.data.get("kind") == "combat_flee":
                        combat_flee_completed = event

                if hp_dropped and has_under_attack and combat_flee_triggered and combat_flee_completed:
                    break
                time.sleep(0.1)
            elapsed = time.monotonic() - t0
            final = body.get_state()
            owner = rcon.command(
                f"script in minebot run minebot_event_head('{BOT}', 'combat-reflex-probe')"
            )

            print(f"timeline (t, hp, husk): {timeline[:14]}{' ...' if len(timeline) > 14 else ''}")
            print(f"hp_dropped={hp_dropped} underAttack={has_under_attack} "
                  f"combat_flee_triggered={combat_flee_triggered is not None} "
                  f"combat_flee_completed={combat_flee_completed is not None} elapsed={elapsed:.2f}s")
            print(f"start={timeline[0] if timeline else None} final_pos={final.pos} owner={owner[:180]}")

            if not hp_dropped:
                raise AssertionError("attacker husk did not damage the bot (hp never dropped); cannot test reflex")
            if not has_under_attack:
                raise AssertionError("no underAttack event emitted by combat reflex")
            if combat_flee_triggered is None:
                raise AssertionError("combat reflex did not start a combat_flee action")
            if combat_flee_completed is None or combat_flee_completed.data.get("escaped_hazard") is not True:
                raise AssertionError(f"combat flee did not complete cleanly: {combat_flee_completed}")
            if '"owner":null' not in owner:
                raise AssertionError(f"combat reflex did not release the Body owner: {owner}")
            if final.pos[2] >= 20.0:
                raise AssertionError(f"combat flee did not gain distance from the southern attacker: {final.pos}")
            print("\nCOMBAT REFLEX CONFIRMED: attacker hit bot -> underAttack emitted -> "
                  "Body fled to a local safe position -> reflex settled and released ownership.")
        finally:
            rcon.command(f"player {BOT} kill")
            rcon.command("kill @e[type=husk]")
            rcon.command("kill @e[type=zombie]")
            rcon.command("difficulty peaceful")


if __name__ == "__main__":
    main()
