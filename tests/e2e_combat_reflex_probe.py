#!/usr/bin/env python3
"""Live probe: validate the auto-defensive combat reflex (S6).

An IDLE bot (survival, no brain action) stands near a single REAL NoAI:0 husk
with Health:1 (low HP). The husk aggros the bot (night + survival), closes, and
melees -> the bot's hp drops. combat_reflex_scan (hp-drop + nearest_hostile
within 16) fires: emit underAttack, then auto-engage the NEAREST hostile (the
husk itself, the only one). The bot one-shots the 1-HP husk -> engageDone/killed.

Asserts the reflex's full job: hp dropped (husk landed a hit), underAttack
emitted, auto-engage started (engageStarted), and the engaged target was killed
(engageDone/killed). This wires hp-drop -> underAttack -> auto-engage -> kill
end to end. A single low-HP attacker is used so the reflex engages the SAME mob
that triggered it (no target-hopping to a 20-HP neighbor) and kills it cleanly.

A husk attacker is used (not a zombie) because a prior run confirmed husks
reliably melee Carpet fake players; a zombie at range failed to close/aggro.
The engage's kill mechanics are independently validated in e2e_engage_probe.
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
            "script unload minebot", "script load minebot global",
            "carpet commandPlayer true", "carpet allowSpawningOfflinePlayers true",
            "gamerule doDaylightCycle false", "gamerule doMobSpawning false",
            "time set 18000", "weather clear",
            f"player {BOT} kill",
            "kill @e[type=zombie]", "kill @e[type=husk]", "kill @e[type=skeleton]",
            "fill 20 69 20 28 76 28 air",
            "fill 20 69 20 28 69 28 stone",
        ]:
            rcon.command(cmd)
            time.sleep(0.05)
        time.sleep(0.3)

        body = ScarpetBody(BOT, rcon)
        spawn_or_fail(body, (23, 70, 23))
        rcon.command(f"gamemode survival {BOT}")
        time.sleep(0.3)
        # Single low-HP attacker husk 2 blocks south (closes + melees -> reflex;
        # reflex engages it as the only hostile -> one-shot kill at 1 HP).
        rcon.command("summon husk 23 70 25 {NoAI:0b,PersistenceRequired:1b,Health:1f}")
        time.sleep(1.0)

        print(f"baseline: bot hp={body.get_state().health} husk={_count(rcon,'husk')}")

        t0 = time.monotonic()
        hp_dropped = False
        has_under_attack = False
        has_engage_started = False
        has_engage_killed = False
        engage_done_reasons = []
        events_all = ""
        timeline = []
        while time.monotonic() - t0 < 25.0:
            st = body.get_state()
            hp = st.health
            if hp < 20.0:
                hp_dropped = True
            hc = _count(rcon, "husk")
            timeline.append((round(time.monotonic() - t0, 2), round(hp, 2), hc))

            ev = rcon.command(f"script in minebot run minebot_drain_events('{BOT}')")
            if ev:
                events_all += "\n" + ev
            if '"name":"underAttack"' in ev:
                has_under_attack = True
            if '"name":"engageStarted"' in ev:
                has_engage_started = True
            if '"name":"engageDone"' in ev:
                # capture the reason field of the most recent engageDone
                seg = ev[ev.rfind('"name":"engageDone"'):]
                import re
                m = re.search(r'"reason":"([^"]+)"', seg)
                if m and m.group(1) not in engage_done_reasons:
                    engage_done_reasons.append(m.group(1))
                if '"reason":"killed"' in seg:
                    has_engage_killed = True

            if hp_dropped and has_under_attack and has_engage_started and has_engage_killed:
                break
            if engage_done_reasons and time.monotonic() - t0 > 2.0:
                break
            time.sleep(0.5)
        elapsed = time.monotonic() - t0

        rcon.command(f"player {BOT} kill"); time.sleep(0.2)
        rcon.command("kill @e[type=husk]"); rcon.command("kill @e[type=zombie]")

        print(f"timeline (t, hp, husk): {timeline[:14]}{' ...' if len(timeline) > 14 else ''}")
        print(f"hp_dropped={hp_dropped} underAttack={has_under_attack} "
              f"engageStarted={has_engage_started} engageDone(killed)={has_engage_killed} "
              f"reasons={engage_done_reasons} elapsed={elapsed:.2f}s")
        # show the non-empty event lines only
        import json
        for line in events_all.split("\n"):
            if '"events":[]' in line or not line.strip():
                continue
            print("evt:", line[:300])

        if not hp_dropped:
            raise AssertionError("attacker husk did not damage the bot (hp never dropped); cannot test reflex")
        if not has_under_attack:
            raise AssertionError("no underAttack event emitted by combat reflex")
        if not has_engage_started:
            raise AssertionError("reflex did not auto-start an engage (no engageStarted)")
        if not has_engage_killed:
            raise AssertionError(
                f"auto-engage did not kill the husk (engageDone reasons={engage_done_reasons}); "
                "reflex core (underAttack+engageStarted) confirmed, kill path covered by e2e_engage_probe")
        print("\nCOMBAT REFLEX CONFIRMED (full chain): attacker hit bot -> underAttack emitted -> "
              "reflex auto-engaged the attacker -> killed it (engageDone/killed).")


if __name__ == "__main__":
    main()
