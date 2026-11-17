"""The reflex frame question: Body facts as plain sentences, options as concrete things to do.

This is the only place that writes question text for Decision.  The wording
was settled offline against recorded frames (see
``docs/design-docs/reflex-frame-question-20261012.md``); change it there first.
The module is pure: callers hand in raw facts and get back a request body.
"""

from __future__ import annotations

import copy
import math
import json
import re
import time
from collections.abc import Mapping
from typing import Any, Iterable

from minebot.app.world_record import dimension_id, dimension_words
from minebot.body.threat_reach import DODGE_STEP_REACH, attack_reach

# Options that protect the Body.  They are asked in a separate question and
# only used when Decision says the Body is in danger.
# One quick step in a direction the Body reports it can walk (Body fact
# ``steps``); a dodge goes on as Decision chooses step after step.
STEP_DIRECTIONS = ("north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest")
STEP_OPTIONS = tuple(f"step_{direction}" for direction in STEP_DIRECTIONS)
PROTECTIVE = ("leave_lava", "put_out_fire", "surface", "cushion_fall", "engage", "ranged", "move_away", *STEP_OPTIONS)
# A fall of this many blocks or fewer does not hurt.
SAFE_FALL_BLOCKS = 3.0
# Best first; engageEntity draws the first one owned (MineBotClientBody.MELEE_WEAPONS).
MELEE_WEAPONS = ("netherite_sword", "diamond_sword", "iron_sword", "copper_sword", "stone_sword", "golden_sword",
                 "wooden_sword", "netherite_axe", "diamond_axe", "iron_axe", "copper_axe", "stone_axe", "golden_axe",
                 "wooden_axe")
# What a shot does to a kind that projectiles do not hurt, said in the ranged
# option: the protective option aimed at the nearest hostile, an angry
# enderman, and Decision chose it again and again; the arrows never hurt it
# and endermen nearby turned on the Body after the shots (End run-12,
# dragon-unit-tests-20261030 §6.y.21). Options carry consequences; the option
# stays. Add a kind only with evidence.
PROJECTILE_CONSEQUENCES = {
    "enderman": ("An enderman teleports away from arrows: arrows do not hurt it, and aiming at it means looking at "
                 "endermen, which angers the ones nearby."),
}
# Mobs that keep up with a running player: fleeing does not lose them. Said
# in the move_away option as what moving away does; the program's verdict
# "The Body can deal with it" that once carried it was removed at the user's
# word (dragon-unit-tests-20261030 §6.y.22).
FAST_CHASERS = frozenset({"magma_cube", "spider", "cave_spider"})
# Running off (body-tool-boundaries-20261110 §7, user 2026-11-10: 奔跑的情况下，
# 僵尸肯定是追不上的). move_away is one whole act: the Body runs from the
# hostile mobs it sees until the nearest is this far beyond its reach, or
# no farther ground is left. A one-step dodge chosen again every second was
# executed 96 times in easy run-13 and moved a median of 0 blocks: 81 of 95
# times the mob was already 16 or more blocks away. So a mob already that
# far away is not run from (nothing to do). The dragon keeps the dodge step
# (End fight, dragon-unit-tests-20261030 §6).
FLEE_MARGIN = 8.0
FLEE_CAP = 24.0
# The run option names the hostile mobs it runs from: those this near, nearest first.
RUN_NAMES_WITHIN = 16.0
RUN_NAMES_MAX = 4
DODGE_ONLY = frozenset({"ender_dragon"})


def _wound_words(target: Mapping[str, Any]) -> str:
    """A mob's health once it is hurt, as a player sees a wounded mob: how
    far the fight has gone (body-tool-boundaries-20261110 §6, A)."""
    health, most = target.get("health"), target.get("max_health")
    if isinstance(health, (int, float)) and isinstance(most, (int, float)) and 0 < health < most:
        return f", health {health:.0f} of {most:.0f}"
    return ""


def mob_kind(target: Mapping[str, Any]) -> str:
    """A mob's kind as attack_reach reads it: its type, else its name."""
    return str(target.get("type") or target.get("name") or "").lower().removeprefix("minecraft:").replace(" ", "_")


def flee_distance(kind: str, now: float | None = None) -> float:
    """How far from a mob of this kind the Body runs to: out of its reach
    with a margin, and at least FLEE_MARGIN farther than it is now, so a run
    is never a standstill. run-14 02:15: a creeper 15 blocks off had no run
    (its old end was 14) and engage was the only protection left; the
    engage dug through stone to it (body-tool-boundaries-20261110 §12)."""
    end = attack_reach(kind) + FLEE_MARGIN
    if now is not None:
        end = max(end, now + FLEE_MARGIN)
    return min(FLEE_CAP + FLEE_MARGIN, end)
AIR_FULL = 300
DEFAULT_ACTION_INTENTS = {
    "leave_lava": "Get out of the lava onto dry ground.",
    "put_out_fire": "Pour water at the Body's feet to put out the fire.",
    "surface": "Swim up to air.",
    "cushion_fall": "Pour the water bucket just before landing to land in water.",
    "scoop_water": "Fill an empty bucket from a water source within reach.",
    "eat": "Eat food from the inventory.",
    "move_away": "Move away from the nearby hostile mob.",
    **{key: f"Step a few blocks {key.removeprefix('step_')}." for key in STEP_OPTIONS},
    "engage": "Fight back against the selected hostile mob.",
    "ranged": "Shoot the selected hostile mob.",
    "go_home": "Walk to the home position.",
    "go_home_portal": "Walk into the portal that leads to the dimension where home is.",
    "approach_player": "Walk to the nearby player.",
    "follow_player": "Follow the nearby player.",
    "look_player": "Look toward the nearby player.",
    "seek_player": "Find the player named in the life profile.",
    "mine_ore": "Mine the exposed ore block nearby.",
}
# A health change smaller than this is regeneration noise, not an injury.
HURT_MARGIN = 0.4
# Within this many blocks of a place the Body is at it (the task book's
# "beside the player" / "at home"): an option to go there is not offered.
AT_PLACE_BLOCKS = 2
VITAL_BASELINE_S = (3.0, 8.0)
# The same protective action started this many times within the window, and
# the Body hurt again during at least REPEAT_HURT of them, is said as a fact.
REPEAT_WINDOW_S = 90.0
REPEAT_MIN = 3
REPEAT_HURT = 2
_REPEAT_WORDS = {
    "leave_lava": ("Getting out of the lava", "tried to get out of the lava"),
    "put_out_fire": ("Pouring water", "poured water"),
    "surface": ("Swimming up", "swum up"),
    "engage": ("Fighting back", "fought back"),
    "ranged": ("Shooting", "shot"),
    "move_away": ("Moving away", "moved away"),
}

_ARGS_NOT_SHOWN = {"timeout_s", "segment_timeout_s", "max_segments", "intent"}
_DANGER = {
    "safe": "Nothing is harming the Body and nothing nearby is about to harm it.",
    "in_danger": "The Body is being harmed right now, or something nearby is about to harm it.",
}
# The Agent's own reading of where the Body is, what keeps it there and what
# would end it (closed-loop-architecture-20261107.md §3 A). Said first in every
# frame; with it the danger branch may keep the Body where it is. Replay of the
# run-3 night (325 frames, live order): protective choices inside sealed
# places 49 -> 2, where protection was due 42 -> 41; the three engages that
# took the Body out of its hole turned to staying.
STAY_PUT = "Stay where the Body is now."
# Said while the Agent holds the Body (control_reflex take_control): "the Body
# acts on its own" was false and Decision read the night as the Body's to run
# (run-3, death at 13:20:19).
HELD_WORDS = ("The Agent has taken the Body into its own hands and holds its plan: "
              "no plan work starts until the Agent says so.")
# Asked once a second while a protective action runs under a stance (§3 B):
# whether the Agent's reading still holds. Hurt, a close mob or the target
# gone still ask the protective choice again on their own.
STANCE_CHECK = {
    "type": "choice",
    "instructions": ("Compare the facts above with the Agent's reading of the situation. "
                     "Does that reading still hold for the Body now?"),
    "criteria": {
        "holds": ("Yes: the Body is still in the situation the Agent's reading describes, "
                  "and nothing it says would end it has happened."),
        "broken": ("No: the Body has left the situation the Agent's reading describes, "
                   "or something it says would end it has happened."),
    },
}


def call_text(tool: str, params: Mapping[str, Any] | None = None) -> str:
    """Write a call the way the Agent would read it, without transport arguments."""
    shown = ", ".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}"
        for key, value in (params or {}).items() if key not in _ARGS_NOT_SHOWN
    )
    return f"{tool}({shown})"


class VitalWindow:
    """Recent health and air samples for the whole Body, not for one call."""

    def __init__(self) -> None:
        self._samples: list[tuple[float, dict[str, float]]] = []
        # Protective actions started, each with whether the Body was hurt
        # while it was the latest one.
        self._protections: list[dict[str, Any]] = []

    def note_protection(self, now: float, choice: str) -> None:
        """Record that a protective action started."""
        self._protections = [entry for entry in self._protections if now - entry["at"] <= REPEAT_WINDOW_S]
        self._protections.append({"at": now, "choice": choice, "hurt": False, "failed": False})

    def note_protection_result(self, choice: str, success: bool) -> None:
        """Record that the latest protective action of this kind reported failure.

        A failed attempt has not helped either: go_to_surface failed 33 times
        in 10 minutes with no route up and was chosen again each time, health
        full, so the hurt-only count never said it (H6 14:00, plan-ladder §9.z).
        """
        if not success and self._protections and self._protections[-1]["choice"] == choice:
            self._protections[-1]["failed"] = True

    def _repeat(self, now: float) -> dict[str, Any] | None:
        """The latest protective action, started again and again while the
        Body kept being hurt.  Each move away ended normally while magma
        cubes kept reaching the Body, and it moved away about 25 times until
        it died (eval-20261020-m2 run-1, 23:51)."""
        streak: list[dict[str, Any]] = []
        for entry in reversed(self._protections):
            if now - entry["at"] > REPEAT_WINDOW_S or (streak and entry["choice"] != streak[0]["choice"]):
                break
            streak.append(entry)
        hurt = sum(1 for entry in streak if entry["hurt"])
        failed = sum(1 for entry in streak if entry.get("failed"))
        if len(streak) < REPEAT_MIN or sum(1 for entry in streak if entry["hurt"] or entry.get("failed")) < REPEAT_HURT:
            return None
        return {"choice": streak[0]["choice"], "count": len(streak), "hurt": hurt, "failed": failed,
                "age_s": now - streak[-1]["at"]}

    def add(self, now: float, facts: Mapping[str, Any]) -> dict[str, Any]:
        """Record this frame and return the values from a few seconds ago."""
        sample: dict[str, Any] = {key: float(facts[key]) for key in ("health", "oxygen")
                                  if isinstance(facts.get(key), (int, float))}
        near = hostiles(facts)
        if near and isinstance(near[0].get("id"), str) and near[0]["id"]:
            # Comes back as hostile_id_before / hostile_dist_before.
            sample["hostile_id"] = near[0]["id"]
            sample["hostile_dist"] = near[0]["dist"]
        before: dict[str, Any] = {}
        oldest, newest = VITAL_BASELINE_S[1], VITAL_BASELINE_S[0]
        for stamp, values in self._samples:
            if newest <= now - stamp <= oldest:
                before = {f"{key}_before": value for key, value in values.items()}
                before["vital_age_s"] = now - stamp
                break
        last_health = next((values["health"] for _, values in reversed(self._samples) if "health" in values), None)
        if ("health" in sample and last_health is not None and sample["health"] < last_health - HURT_MARGIN
                and self._protections and now - self._protections[-1]["at"] <= REPEAT_WINDOW_S):
            self._protections[-1]["hurt"] = True
        repeat = self._repeat(now)
        if repeat:
            before["protection_repeat"] = repeat
        if sample:
            self._samples.append((now, sample))
        self._samples = [(stamp, values) for stamp, values in self._samples if now - stamp <= oldest]
        if "oxygen" in sample:
            # The current uninterrupted direction of air change, rather than
            # a net change across an earlier breath followed by another dive.
            air = sample["oxygen"]
            direction = 0
            baseline = None
            for stamp, values in reversed(self._samples[:-1]):
                if "oxygen" not in values:
                    continue
                previous = values["oxygen"]
                change = (air > previous) - (air < previous)
                if change and direction and change != direction:
                    break
                if change:
                    direction = change
                baseline = (stamp, previous)
                air = previous
            before.pop("oxygen_before", None)
            if baseline is not None:
                before["oxygen_before"] = baseline[1]
                before["oxygen_age_s"] = now - baseline[0]
        return before


def protection_experience(stamp: float, tool: str, params: Mapping[str, Any],
                          result: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    from datetime import datetime, timezone
    when = datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")
    position = _position(facts.get("position", facts.get("pos")))
    return f"At {when}, {result_sentence(call_text(tool, params), result)} Now at {position or 'an unrecorded position'}."


# Where a frame's entity read may sit.
HOSTILE_KEYS = ("nearby_hostiles", "hostiles", "nearby_entities", "entities", "entities_near")
CREEPER_MEMORY_S = 5.0


def hostiles(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Hostile mobs in the frame, nearest first, each with ``name`` and ``dist``."""
    found: list[dict[str, Any]] = []
    for key in HOSTILE_KEYS:
        items = facts.get(key)
        if isinstance(items, Mapping):
            items = items.get("entities", items.get("items"))
        if not isinstance(items, (list, tuple)):
            continue
        for item in items:
            # A boss, or a creature seen in attack mode, counts with the
            # hostile ones (dragon-unit-tests-20261030 §5.2).
            if not isinstance(item, Mapping) or not (item.get("hostile") or item.get("boss") or item.get("aggressive")):
                continue
            # Native entities can remain visible during their death animation.
            # Observed zero health is a terminal game fact, not a live target
            # or a judgement about whether the Agent's intention is complete.
            health = item.get("health")
            if isinstance(health, (int, float)) and health <= 0:
                continue
            # The nearest part, not the centre: a dragon's body is many blocks long.
            dist = item.get("nearest_dist", item.get("dist", item.get("distance")))
            if dist is None and isinstance(item.get("dist2"), (int, float)):
                dist = float(item["dist2"]) ** 0.5
            if not isinstance(dist, (int, float)):
                continue
            found.append({
                "id": item.get("id", item.get("uuid")),
                "name": str(item.get("name") or item.get("type") or "hostile mob"),
                "type": ("charged_creeper" if str(item.get("type") or "").removeprefix("minecraft:") == "creeper"
                         and (item.get("charged") or item.get("powered")) else
                         str(item.get("type") or "").removeprefix("minecraft:")),
                "dist": float(dist),
                "pos": item.get("pos", item.get("position")),
                **({"swelling": item["swelling"]} if isinstance(item.get("swelling"), bool) else {}),
                **({"health": float(health), "max_health": float(item["max_health"])}
                   if isinstance(health, (int, float)) and isinstance(item.get("max_health"), (int, float)) else {}),
                **{key: item[key] for key in SEEN_KEYS if item.get(key) is not None},
            })
        if found:
            break
    return sorted(found, key=lambda entry: entry["dist"])


# What the Body sees of each creature, the same for every kind (Body facts,
# dragon-unit-tests-20261030 §5.2). The mob's own target is not seen.
SEEN_KEYS = ("health", "max_health", "velocity", "approaching", "facing_body", "boss", "aggressive",
             "phase_id", "head_pos", "in_sight")
# EnderDragonPhase ids, said as a player sees them.
DRAGON_PHASES = {
    0: "circling high over the island",
    1: "swooping down to shoot a fireball",
    2: "flying in to land on the exit portal",
    3: "landing on the exit portal",
    4: "taking off from the exit portal",
    5: "perched on the exit portal, breathing fire",
    6: "perched on the exit portal, looking around",
    7: "perched on the exit portal, roaring before it attacks",
    8: "charging",
    9: "dying",
    10: "hovering",
}


def _seen_words(mob: Mapping[str, Any], facts: Mapping[str, Any]) -> str | None:
    """One creature as observed: health, what it does, its head, how it moves."""
    if not any(key in mob for key in ("velocity", "phase_id", "max_health")):
        return None
    parts = [mob["name"] + (" (a boss)" if mob.get("boss") is True else "")]
    health, most = mob.get("health"), mob.get("max_health")
    if isinstance(health, (int, float)) and isinstance(most, (int, float)):
        parts.append(f"health {health:.0f} of {most:.0f}")
    if mob.get("type") == "ender_dragon" and mob.get("phase_id") in DRAGON_PHASES:
        parts.append(DRAGON_PHASES[mob["phase_id"]])
    if mob.get("swelling") is True:
        parts.append("swelling to explode")
    elif mob.get("aggressive") is True:
        parts.append("in attack mode")
    head = _position(mob.get("head_pos"))
    if head:
        away = player_distance({"pos": mob.get("head_pos")}, facts)
        parts.append(f"its head is at {head}" + (f", {away:.0f} blocks from the Body" if away is not None else ""))
    parts.append(f"{mob['dist']:.0f} blocks away")
    if mob.get("in_sight") is False:
        parts.append("out of the Body's sight: blocks stand between them")
    velocity = mob.get("velocity")
    if isinstance(velocity, (list, tuple)) and len(velocity) == 3:
        speed = sum(float(v) ** 2 for v in velocity) ** 0.5
        if mob.get("approaching") is True:
            parts.append(f"moving toward the Body at {speed:.0f} blocks a second")
        elif speed >= 0.5:
            parts.append(f"moving at {speed:.0f} blocks a second, not toward the Body")
        else:
            parts.append("not moving")
    if mob.get("facing_body") is True:
        parts.append("facing the Body")
    return parts[0] + ": " + "; ".join(parts[1:]) + "."


def seen_sentence(facts: Mapping[str, Any], limit: int = 3) -> str | None:
    said = [words for words in (_seen_words(mob, facts) for mob in hostiles(facts)[:limit]) if words]
    return "What the Body sees: " + " ".join(said) if said else None


def update_creeper_memory(
    facts: Mapping[str, Any], memory: dict[str, dict[str, Any]], now: float | None = None,
) -> list[dict[str, Any]]:
    """Keep observed creepers as bounded, time-stamped frame facts.

    The five-second window describes what was observed recently. It does not
    decide whether the Body should act; Decision still sees the sentence and
    chooses among its options.
    """
    stamp = time.monotonic() if now is None else float(now)
    current_ids: set[str] = set()
    for mob in hostiles(facts):
        if mob.get("type") != "creeper" or not mob.get("id"):
            continue
        entity_id = str(mob["id"])
        current_ids.add(entity_id)
        entry = memory.setdefault(entity_id, {})
        entry.update({
            "id": entity_id,
            "name": mob.get("name") or "creeper",
            "last_seen_position": mob.get("pos"),
            "last_seen_distance": mob.get("dist"),
            "last_seen_at": stamp,
        })
        if isinstance(mob.get("swelling"), bool):
            entry["swelling"] = mob["swelling"]
    for entity_id in list(memory):
        if stamp - float(memory[entity_id].get("last_seen_at", stamp)) > CREEPER_MEMORY_S:
            del memory[entity_id]
    result: list[dict[str, Any]] = []
    for entry in memory.values():
        age = max(0.0, stamp - float(entry.get("last_seen_at", stamp)))
        result.append({
            key: value for key, value in {
                "id": entry.get("id"),
                "name": entry.get("name") or "creeper",
                "last_seen_position": entry.get("last_seen_position"),
                "last_seen_distance": entry.get("last_seen_distance"),
                "seconds_since_seen": round(age, 3),
                "in_view": entry.get("id") in current_ids,
                "swelling": entry.get("swelling"),
            }.items() if value is not None
        })
    return sorted(result, key=lambda item: str(item.get("id", "")))


# Two last_shot readings belong to one arrow when their release times are this close.
SHOT_SAME_RELEASE_S = 0.5
# Targets kept in the shot record; the oldest are dropped first.
SHOT_RECORD_TARGETS = 32


def update_shot_record(facts: Mapping[str, Any], record: dict[str, dict[str, Any]],
                       now: float | None = None, log: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Count the arrows the Body has loosed at each target, from last_shot.

    last_shot names only the latest arrow; this keeps how many were shot at
    one target and what its health was at the first, so the shooting option
    can say it. Shots are real and outlive a death; the record does not judge
    whether to shoot (End run-29: 165 arrows at a flying dragon whose health
    never moved, dragon-unit-tests-20261030 §6.y.40).

    With ``log``, each new arrow is also appended there (release time on the
    same clock as ``now``, target, hit) so the Body loop can say who shot it.
    """
    stamp = time.monotonic() if now is None else float(now)
    shot = facts.get("last_shot")
    if isinstance(shot, Mapping) and shot.get("target_id") and isinstance(shot.get("age_s"), (int, float)):
        target_id = str(shot["target_id"])
        released = stamp - float(shot["age_s"])
        entry = record.pop(target_id, None) or {"target_id": target_id, "shots": 0, "hits": 0}
        record[target_id] = entry  # most recent last
        entry["target_type"] = str(shot.get("target_type") or entry.get("target_type") or "")
        last = entry.get("released_at")
        if not isinstance(last, (int, float)) or abs(released - last) > SHOT_SAME_RELEASE_S:
            entry["shots"] += 1
            entry["released_at"] = released
            entry["counted_hit"] = False
            if entry["shots"] == 1 and isinstance(shot.get("health_before"), (int, float)):
                entry["health_at_first_shot"] = float(shot["health_before"])
            if log is not None:
                entry["log_entry"] = {"released": released, "target_id": target_id,
                                      "target_type": entry["target_type"], "hit": False}
                log.append(entry["log_entry"])
        if shot.get("outcome") in ("hit", "killed") and not entry.get("counted_hit"):
            entry["hits"] += 1
            entry["counted_hit"] = True
            if isinstance(entry.get("log_entry"), dict):
                entry["log_entry"]["hit"] = True
        while len(record) > SHOT_RECORD_TARGETS:
            del record[next(iter(record))]
    return [{key: value for key, value in entry.items() if key in (
        "target_id", "target_type", "shots", "hits", "health_at_first_shot")} for entry in record.values()]


def crystals_seen(facts: Mapping[str, Any]) -> int:
    """End crystals in the Body's entity read (64 blocks round it).

    Said on the dragon's shooting option as what a shot does while they
    stand. Replay of 60 frames (dragon-unit-tests §6.y.43): ranged 40 -> 27,
    and with the Agent's no-dragon-shot rule 5 -> 1. "arrows at the dragon are
    wasted" went lower (15) but is a verdict, not a fact.
    """
    seen = set()
    for key in ("entities", "nearby_entities", "entities_near"):
        items = facts.get(key) or []
        if isinstance(items, Mapping):
            items = items.get("entities", items.get("items", []))
        for entity in items if isinstance(items, (list, tuple)) else []:
            if isinstance(entity, Mapping) and str(entity.get("type") or "").removeprefix("minecraft:") == "end_crystal":
                seen.add(str(entity.get("id") or entity.get("uuid") or id(entity)))
    return len(seen)


def shot_record_sentence(facts: Mapping[str, Any], target: Mapping[str, Any]) -> str | None:
    """The arrows already shot at this target and its health then and now."""
    entries = facts.get("shot_record")
    if not isinstance(entries, (list, tuple)) or not target.get("id"):
        return None
    entry = next((item for item in entries if isinstance(item, Mapping)
                  and item.get("target_id") == target.get("id")), None)
    if entry is None or not entry.get("shots"):
        return None
    shots, hits = int(entry["shots"]), int(entry.get("hits") or 0)
    sentence = f"Arrows already shot at this {target['name']}: {shots}, {hits} of them hit"
    health, most = target.get("health"), target.get("max_health")
    first = entry.get("health_at_first_shot")
    if isinstance(health, (int, float)) and isinstance(first, (int, float)):
        of = f" of {most:.0f}" if isinstance(most, (int, float)) else ""
        sentence += f"; its health is {health:.0f}{of} now and was {first:.0f} at the first shot"
    return sentence + "."


def just_hurt(facts: Mapping[str, Any]) -> bool:
    health, before = facts.get("health"), facts.get("health_before")
    return (isinstance(health, (int, float)) and isinstance(before, (int, float))
            and health < before - HURT_MARGIN)


def _ago(facts: Mapping[str, Any], key: str = "vital_age_s") -> str:
    age = facts.get(key, facts.get("vital_age_s"))
    if not isinstance(age, (int, float)):
        return "a few seconds"
    rounded = round(age)
    return f"{rounded} second" + ("" if rounded == 1 else "s")


def _hurt_source(facts: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The client's last damage source, only while the Body was just hurt:
    the client keeps it until respawn, and an old hit is not a fact about now."""
    value = facts.get("hurt_source")
    return value if isinstance(value, Mapping) and just_hurt(facts) else None


def _hurt_source_sentence(facts: Mapping[str, Any]) -> str | None:
    source = _hurt_source(facts)
    if source is None:
        return None
    name = str(source.get("name") or source.get("type") or "an observed source")
    distance = source.get("distance", source.get("dist"))
    if isinstance(distance, (int, float)):
        where = f"at {round(distance)} blocks"
    else:
        where = "at an unrecorded distance"
    age = source.get("seconds_since_hurt", source.get("age_s"))
    ago = f"{round(age)} second" + ("" if round(age) == 1 else "s") + " ago" if isinstance(age, (int, float)) else "recently"
    return f"The {name} {where} hit the Body {ago}."


CREEPER_KINDS = frozenset({"creeper", "charged_creeper"})
MELEE_OFFER = 16.0


def melee_target(near: list[dict[str, Any]], facts: Mapping[str, Any]) -> dict[str, Any] | None:
    """The mob a player fights back against: the one that hurt the Body, else the nearest that is not a creeper.

    A creeper is run from or shot, not met in melee: run-15 03:32:48 offered
    "fight back against the Creeper (11 blocks away)" while two skeletons
    shot at the Body, and the fight took it from 15 to 9; run-14 02:16 an
    engage on a creeper ended in its blast (body-tool-boundaries-20261110 §13).
    """
    source = _hurt_source(facts)
    hit_by = next((mob for mob in near if source is not None and source.get("id") and mob.get("id") == source.get("id")), None)
    if hit_by is not None and hit_by.get("type") not in CREEPER_KINDS:
        return hit_by
    # Fighting back is against a mob at hand: run-16 04:37:41 the fight went
    # after a zombie 37 blocks off behind blocks, the only other mobs being
    # creepers, and walked the Body into an enderman and two creepers.
    # The dragon is fought wherever it is (AR-17, the End fight).
    return next((mob for mob in near if mob.get("type") not in CREEPER_KINDS
                 and (mob["dist"] <= MELEE_OFFER or mob_kind(mob) in DODGE_ONLY)), None)


def _hurt_target(near: list[dict[str, Any]], facts: Mapping[str, Any]) -> dict[str, Any] | None:
    source = _hurt_source(facts)
    source_id = source.get("id") if source else None
    if source_id:
        matched = next((mob for mob in near if mob.get("id") == source_id), None)
        if matched is not None:
            return matched
    return near[0] if near else None


def _creeper_memory_sentence(facts: Mapping[str, Any]) -> str | None:
    entries = facts.get("creeper_memory")
    if not isinstance(entries, (list, tuple)):
        return None
    # A creeper in view is already in the nearest-mob sentence; only what that
    # sentence cannot say is added: one out of view, and swelling.
    sentences: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not entry.get("id"):
            continue
        distance = entry.get("last_seen_distance")
        distance_text = f"{round(distance)} blocks away" if isinstance(distance, (int, float)) else "near"
        if entry.get("in_view"):
            if entry.get("swelling") is True:
                sentences.append(f"The creeper {distance_text} is swelling.")
            continue
        age = entry.get("seconds_since_seen")
        age_text = f"{round(age)} second" + ("" if round(age) == 1 else "s") + " ago" if isinstance(age, (int, float)) else "recently"
        sentence = f"A creeper was {distance_text} {age_text} and is not in view now"
        if entry.get("swelling") is True:
            sentence += "; it was swelling"
        sentences.append(sentence + ".")
    return " ".join(sentences) or None


def _inventory(facts: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("inventory", "inventory_counts"):
        if isinstance(facts.get(key), Mapping):
            return facts[key]
    return None


def inventory_slots_used(facts: Mapping[str, Any]) -> list[int] | None:
    """[used, all] of the 36 hotbar and inventory slots; armour, offhand and the crafting grid are not counted."""
    slots = facts.get("inventory_slots")
    if not isinstance(slots, list):
        return None
    main = [slot for slot in slots if isinstance(slot, Mapping) and slot.get("slotType") in ("hotbar", "inventory")]
    if not main:
        return None
    return [sum(1 for slot in main if slot.get("empty") is False), len(main)]


def inventory_space(facts: Mapping[str, Any]) -> str | None:
    """How full the inventory is, in the frame's words (vision-and-home-20261028.md §7.7, Decision)."""
    used = facts.get("inventory_slots_used")
    if not (isinstance(used, (list, tuple)) and len(used) == 2 and all(isinstance(n, int) for n in used)):
        return None
    return f"{used[0]} of {used[1]} inventory slots are used."


def dead(facts: Mapping[str, Any]) -> bool:
    health = facts.get("health")
    return isinstance(health, (int, float)) and health <= 0


def breathing(facts: Mapping[str, Any]) -> bool:
    """The head is out of liquid, or the air is climbing back: the Body
    breathes, whatever the air supply still is."""
    air, before = facts.get("oxygen"), facts.get("oxygen_before")
    rising = isinstance(air, (int, float)) and isinstance(before, (int, float)) and air > before
    return facts.get("head_in_liquid") is False or (facts.get("head_in_liquid") is not True and rising)


def _body_sentence(facts: Mapping[str, Any]) -> str:
    if dead(facts):
        return "The Body has died."
    parts: list[str] = []
    health, food, air = facts.get("health"), facts.get("food"), facts.get("oxygen")
    if isinstance(health, (int, float)):
        parts.append(f"Health is {health:.0f} of 20.")
    bed_fact = dragon_bed_sentence(facts)
    if bed_fact:
        parts.append(bed_fact)
    # Said as observed: with health alone, a Body burning in lava read as
    # "just hurt" (eval-20261020-m1 run-2, 18:33).
    if facts.get("in_lava") is True:
        parts.append("The Body is in lava and is burning.")
    elif facts.get("on_fire") is True:
        parts.append("The Body is on fire.")
    fall = facts.get("fall")
    if isinstance(fall, Mapping):
        # Thrown by the dragon, the Body fell with frames that said nothing
        # of it (end run-6). Said as seen, with "Body" as the frame names it.
        sentence = fall_sentence(fall).replace("The bot is", "The Body is") + (
            f" It has fallen {float(fall['fallen']):g} blocks so far." if isinstance(fall.get("fallen"), (int, float)) else "")
        # The landing said as its cost: without it Decision called a Body
        # falling 29 blocks "safe" in 22 of 58 run-6 frames; with it, 5 (probe
        # 2026-10-30, ~/mbprobe/fall).
        harm = fall_harm(fall)
        if harm is not None and isinstance(health, (int, float)):
            total = float(fall.get("fallen") or 0) + float(fall["ground_below"])
            sentence += (f" Landing on {str(fall['lands_on']).replace('_', ' ')} from {total:g} blocks takes about"
                         f" {harm} health; the Body has {health:.0f}.")
        parts.append(sentence)
    cloud = facts.get("cloud")
    if isinstance(cloud, Mapping) and cloud.get("inside") is True and isinstance(cloud.get("radius"), (int, float)):
        # Said as what the Body sees: its particles. A cloud of dragon's
        # breath is known to take health; said with its cost, Decision left
        # it in 52 of 80 replayed run-19..26 cloud frames against 13 with
        # "a cloud" alone (body-preemption-20261106.md §8.1). Any other cloud
        # stays seen, not judged: hurt_here says whether it harms (§6.y.15).
        if cloud.get("particle") == "dragon_breath":
            parts.append(f"The Body is standing in a cloud of dragon's breath (radius {cloud['radius']:.2f}): "
                         "a Body inside loses health every second, several points each time.")
        else:
            parts.append(f"The Body is standing in a cloud (radius {cloud['radius']:.2f}).")
    hurt = hurt_here(facts)
    if hurt is not None:
        parts.append(hurt_here_sentence(hurt))
    shot = last_shot_sentence(facts)
    if shot:
        parts.append(shot)
    if isinstance(food, (int, float)):
        parts.append(f"Hunger is {food:.0f} of 20.")
        if food <= 6:
            parts.append(f"Food is {food:.0f}; the Body cannot sprint.")
    if isinstance(air, (int, float)):
        air_before = facts.get("oxygen_before")
        if air >= AIR_FULL:
            parts.append("Air supply is full.")
        elif facts.get("head_in_liquid") is False:
            if isinstance(air_before, (int, float)) and air > air_before:
                parts.append(
                    f"The Body is breathing again: air supply was {air_before:.0f} {_ago(facts, 'oxygen_age_s')} ago "
                    f"and is {air:.0f} of {AIR_FULL} now."
                )
            else:
                parts.append(f"The head is out of liquid; air supply is {air:.0f} of {AIR_FULL} now.")
        elif air <= 0:
            parts.append("Air supply is empty; the Body is under water and is drowning.")
        elif facts.get("head_in_liquid") is not True and isinstance(air_before, (int, float)) and air > air_before:
            parts.append(
                f"The Body is breathing again: air supply was {air_before:.0f} {_ago(facts, 'oxygen_age_s')} ago "
                f"and is {air:.0f} of {AIR_FULL} now."
            )
        elif isinstance(air_before, (int, float)):
            parts.append(
                "The head is under water and the Body cannot breathe: air supply was "
                f"{air_before:.0f} {_ago(facts, 'oxygen_age_s')} ago and is {air:.0f} of {AIR_FULL} now."
            )
        else:
            parts.append(
                "The head is under water and the Body cannot breathe: air supply is "
                f"{air:.0f} of {AIR_FULL} now."
            )
    if just_hurt(facts):
        parts.append(
            f"The Body was just hurt: health fell from {facts['health_before']:.0f} "
            f"to {health:.0f} in the last {_ago(facts)}."
        )
    hurt_source = _hurt_source_sentence(facts)
    if hurt_source:
        parts.append(hurt_source)
    # Said across actions: each frame alone looked like the first one, and
    # moving away was chosen until the Body died, five times (eval-20261020-m2
    # run-1). On the 63 recorded frames that carry this fact it was still
    # chosen 5 times (58 before); "has not stopped the harm" left it 21, and
    # "the mobs kept reaching it" handed 40 to the Agent.
    repeat = facts.get("protection_repeat")
    choice = repeat.get("choice") if isinstance(repeat, Mapping) else None
    words = _REPEAT_WORDS.get(choice) if isinstance(choice, str) else None
    if isinstance(choice, str) and choice.startswith(RULE_PREFIX):
        words = ("Following the Agent's rule", "followed the Agent's rule")
    if words:
        doing, done = words
        outcomes = []
        if repeat.get("failed"):
            outcomes.append(f"{repeat['failed']} of them reported failure")
        if repeat.get("hurt"):
            outcomes.append(f"was hurt again during {repeat['hurt']} of them")
        parts.append(
            f"{doing} has not helped: the Body has {done} {repeat['count']} times "
            f"in the last {_ago(repeat, 'age_s')} and " + " and ".join(outcomes) + "."
        )
    return " ".join(parts)


def _main_hand(facts: Mapping[str, Any]) -> str | None:
    """The observed main-hand item, said beside the inventory.  Said inside
    the body sentence it was overlooked when an intent named both an item and
    holding it.  An unobserved hand is left out, never written as empty."""
    selected = facts.get("selected_item")
    if dead(facts) or facts.get("selected_item_known") is False or not isinstance(selected, str):
        return None
    return selected or "nothing"


def _threat_sentence(facts: Mapping[str, Any]) -> str:
    near = hostiles(facts)
    memory_sentence = _creeper_memory_sentence(facts)
    if near:
        now = round(near[0]["dist"])
        before = facts.get("hostile_dist_before")
        if (near[0].get("id") and near[0].get("id") == facts.get("hostile_id_before")
                and isinstance(before, (int, float)) and round(before) - now >= 1):
            # Said as a change (§6). As a still distance, a zombie walking in
            # from 14 to 6 blocks was "safe" every frame and the Body was
            # killed (villa run 2026-10-15); with this sentence those recorded
            # frames were in danger 72/72, and still or retreating mobs stayed
            # safe. Only "coming closer" is said: "moving away" turned a just
            # hurt, knocked-back frame from in danger to safe.
            age = facts.get("vital_age_s")
            ago = f"{round(age)} seconds ago" if isinstance(age, (int, float)) else "a few seconds ago"
            base = (f"Nearest hostile mob: {near[0]['name']}, {now} blocks away and coming closer: "
                    f"it was {round(before)} blocks away {ago}.")
        else:
            base = f"Nearest hostile mob: {near[0]['name']}, {near[0]['dist']:.0f} blocks away."
    else:
        base = "No hostile mob is in view."
    return " ".join(part for part in (base, seen_sentence(facts), memory_sentence) if part)


def _position(value: Any) -> str | None:
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return "[" + ", ".join(f"{float(part):.1f}" for part in value) + "]"
        except (TypeError, ValueError):
            return None
    return None


def scoop_water_offered(facts: Mapping[str, Any]) -> bool:
    """A water source within reach and an empty bucket carried."""
    if not _cell(facts.get("water_source_in_reach")):
        return False
    inventory = _inventory(facts) or {}
    return any(str(item).removeprefix("minecraft:") == "bucket" and count > 0 for item, count in inventory.items())


def fall_harm(fall: Mapping[str, Any]) -> int | None:
    """Health a plain landing takes, by the game's rule: one per block past
    the first three. Armour does not lessen it."""
    below = fall.get("ground_below")
    if fall.get("void_below") or fall.get("lands_on") in (None, "water") or not isinstance(below, (int, float)):
        return None
    return max(0, math.ceil(float(fall.get("fallen") or 0) + float(below) - SAFE_FALL_BLOCKS))


def cushion_fall_offered(facts: Mapping[str, Any]) -> bool:
    """Physical facts only: falling onto ground (not water, not the void),
    a water bucket carried, water that stays (not the Nether), and a fall long
    enough to hurt at all. Whether it is worth it is Decision's to read."""
    fall = facts.get("fall")
    if not isinstance(fall, Mapping) or fall.get("void_below") or fall.get("lands_on") in (None, "water"):
        return False
    below = fall.get("ground_below")
    if not isinstance(below, (int, float)) or below <= 0:
        return False
    if float(fall.get("fallen") or 0) + float(below) <= SAFE_FALL_BLOCKS:
        return False
    if facts.get("dimension") == "minecraft:the_nether":
        return False
    inventory = _inventory(facts) or {}
    return any(str(item).removeprefix("minecraft:") == "water_bucket" and count > 0 for item, count in inventory.items())


def hurt_here(facts: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The Body's count of hurts from the place it stands on now (not a mob's
    blow, an arrow or an effect it carries), whatever the place holds."""
    hurt = facts.get("hurt_here")
    return hurt if isinstance(hurt, Mapping) and isinstance(hurt.get("times"), int) and hurt["times"] > 0 else None


def hurt_here_sentence(hurt: Mapping[str, Any]) -> str:
    """Said as a place, not as a kind of danger: the breath of run-7 is one of
    many things a place can hold (user, dragon-unit-tests-20261030 §6.y.11)."""
    times = hurt["times"]
    seconds = hurt.get("seconds")
    span = f" in the last {round(seconds)} seconds" if isinstance(seconds, (int, float)) else ""
    return (f"The place where the Body stands is taking its health: hurt {times} "
            f"time{'' if times == 1 else 's'} here{span}, not by a mob's blow.")


# How long after a shot its outcome is still said in the frame.
LAST_SHOT_SAID_S = 30.0


def last_shot_sentence(facts: Mapping[str, Any]) -> str | None:
    """What the last arrow did, as the Body followed it after the shot ended:
    ranged_attack ends when the arrow is loosed (user 2026-11-05, run-10)."""
    shot = facts.get("last_shot")
    if not isinstance(shot, Mapping) or not isinstance(shot.get("age_s"), (int, float)):
        return None
    age = float(shot["age_s"])
    if age > LAST_SHOT_SAID_S:
        return None
    what = str(shot.get("target_type") or "target").replace("_", " ")
    ago = "just now" if age < 1 else f"{age:.0f} seconds ago"
    outcome = shot.get("outcome")
    if outcome == "in_flight":
        return f"The last arrow, shot at the {what} {ago}, is still in flight."
    if outcome == "killed":
        return f"The last arrow, shot {ago}, killed the {what}."
    if outcome == "hit":
        if shot.get("target_gone") is True:
            return f"The last arrow, shot {ago}, hit the {what}; it is gone."
        before, after = shot.get("health_before"), shot.get("health_after")
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            return f"The last arrow, shot {ago}, hit the {what}: its health went from {before:g} to {after:g}."
        return f"The last arrow, shot {ago}, hit the {what}."
    if outcome == "missed":
        landed = _cell(shot.get("arrow_landed"))
        if landed:
            block = str(shot.get("arrow_landed_block") or "block").replace("_", " ")
            return f"The last arrow, shot at the {what} {ago}, missed; it struck the {block} at {landed}."
        return f"The last arrow, shot at the {what} {ago}, missed; it was not seen to land."
    return None


def fall_sentence(fall: Mapping[str, Any]) -> str:
    """The Body's fall said as seen: what lies straight below and how far."""
    if fall.get("void_below"):
        return "The bot is falling with nothing below it down to the bottom of the world."
    below, lands = fall.get("ground_below"), fall.get("lands_on")
    if isinstance(below, (int, float)) and lands:
        what = "water" if lands == "water" else str(lands).replace("_", " ")
        return f"The bot is falling; {what} is {below:g} blocks straight below."
    return "The bot is falling."


_REJECTED_WORDS = {
    "no_floor_beside_path": "a drop or no floor beside the way",
    "cloud_or_hurting_place": "a breath cloud or a place that takes health",
    "toward_another_mob": "toward another hostile mob",
    "nearer_the_threat": "nearer the {what}",
}


def _no_safe_point_words(result: Mapping[str, Any]) -> str:
    """Say a dodge that found no step in words: the code alone was chosen
    again up to 15 times in a row (End run-29, dragon-unit-tests §6.y.41)."""
    if result.get("reason") != "dodge_no_safe_point":
        return ""
    for field in ("summary", "metrics"):
        details = result.get(field)
        if not isinstance(details, Mapping) or "rejected_cells" not in details:
            continue
        what = str(details.get("threat_kind") or "threat").removeprefix("minecraft:").replace("_", " ")
        reach = details.get("step_reach") or 4
        cells = int(details.get("level_step_cells") or 0)
        rejected = details.get("rejected_cells") if isinstance(details.get("rejected_cells"), Mapping) else {}
        reasons = [f"{count} {_REJECTED_WORDS.get(key, key.replace('_', ' ')).format(what=what)}"
                   for key, count in rejected.items() if isinstance(count, int) and count > 0]
        if cells == 0:
            found = f"no level ground to step onto within {reach} blocks"
        else:
            found = (f"none of the {cells} level cells within {reach} blocks was a safe step away from the "
                     f"{what} ({', '.join(reasons)})")
        return f" The Body did not move: {found}."
    return ""


def _found_words(result: Mapping[str, Any]) -> str:
    """What a search found and where, nearest first. Said only "ended normally",
    an explore_for that saw a cow 32 blocks off at once was run again 106 times
    in 90 s, the Body never moving, for the intent "cows approached" (easy
    run-19, 07:11-07:13; body-tool-boundaries-20261110 §13)."""
    for field in ("summary", "metrics"):
        details = result.get(field)
        if not isinstance(details, Mapping) or details.get("target_found") is not True:
            continue
        found = []
        for item in list(details.get("entities") or []) + list(details.get("blocks") or []):
            if not isinstance(item, Mapping) or not isinstance(item.get("dist2"), (int, float)):
                continue
            name = str(item.get("name") or item.get("block") or item.get("type") or "target").replace("_", " ")
            pos = _cell(item.get("pos"))
            found.append((float(item["dist2"]) ** 0.5, name, pos))
        if not found:
            return ""
        words = ", ".join(f"{name} {dist:.0f} blocks away" + (f" at {pos}" if pos else "")
                          for dist, name, pos in sorted(found)[:3])
        moved = details.get("net_displacement")
        stayed = " The Body did not move." if isinstance(moved, (int, float)) and moved < 1.0 else ""
        return f" It found {words}.{stayed}"
    return ""


def result_sentence(call: str, result: Mapping[str, Any] | None) -> str:
    """Say how the call ended.  Success is only "ended normally": whether the
    intent is met is for Decision to read from the inventory and the intent."""
    result = result if isinstance(result, Mapping) else {}
    dodge_words = ""
    for field in ("summary", "metrics"):
        details = result.get(field)
        if not isinstance(details, Mapping) or not details.get("threat_kind"):
            continue
        pos, distance = _point(details.get("final_pos")), details.get("final_distance")
        if pos is not None and isinstance(distance, (int, float)):
            what = str(details["threat_kind"]).removeprefix("minecraft:").replace("_", " ")
            where = "" if details.get("threat_observed") else "last observed position of the "
            dodge_words = f" The Body is at {list(pos)}; the {where}{what} is {distance:.2f} blocks away."
            if details.get("outside_reach_margin") is False:
                dodge_words += " It is still within the threat's attack reach plus margin."
        break
    if result.get("success") is True:
        if result.get("reason") == "entered_portal":
            return f"{call} has ended normally: the Body went into the portal."
        if result.get("reason") == "standing_in_portal":
            return f"{call} has ended normally: the Body is standing in the portal."
        return f"{call} has ended normally." + dodge_words + _found_words(result)
    sentence = f"{call} has ended. It reported failure: {result.get('reason') or 'no reason given'}."
    sentence += _no_safe_point_words(result)
    sentence += _route_words(result)
    sentence += _range_words(result)
    sentence += dodge_words
    for field in ("summary", "metrics"):
        details = result.get(field)
        landed = _cell(details.get("arrow_landed")) if isinstance(details, Mapping) else None
        if landed:
            block = details.get("arrow_landed_block")
            what = str(block).replace("_", " ") if isinstance(block, str) and block else "block"
            predicted = _predicted_point(details.get("predicted_target_pos"))
            if result.get("reason") == "missed" and predicted is not None:
                sentence += f" The target was predicted to be at {predicted}; the arrow struck the {what} at {landed}."
            else:
                sentence += f" The arrow struck the {what} at {landed}."
            break
        fall = details.get("fall") if isinstance(details, Mapping) else None
        if isinstance(fall, Mapping):
            sentence += " " + fall_sentence(fall)
            break
        hurt = hurt_here(details) if isinstance(details, Mapping) else None
        if hurt is not None:
            sentence += " The Body is still losing health where it stands: " + hurt_here_sentence(hurt)
            break
    # The native perception result already records its scope and observation
    # count. Preserve that fact after the Agent projector moves it to summary;
    # it does not establish that the target is absent from the whole world.
    for field in ("summary", "metrics"):
        details = result.get(field)
        if not isinstance(details, Mapping):
            continue
        count, radius = details.get("entities_observed"), details.get("search_radius")
        if type(count) is int and type(radius) in (int, float):
            return sentence + f" The tool observed {count} entities within {radius:g} blocks."
        near = details.get("nearest_hostile")
        if isinstance(near, Mapping) and near.get("name") and isinstance(near.get("dist"), (int, float)):
            return sentence + f" The nearest hostile mob, {near['name']}, is {round(near['dist'])} blocks away."
    return sentence


def _range_words(result: Mapping[str, Any]) -> str:
    """A shot refused for range says how far the target was: the Body no
    longer walks toward it, so closing in is the caller's choice.  A fight
    whose walk ended short of the target says the same, and what blocks the
    view when it is close (easy run-5, 17:51: 20 s beside a spider)."""
    if result.get("reason") not in {"out_of_range", "no_line_of_sight", "target_out_of_reach", "no_route_found",
                                    "none_within"}:
        return ""
    for field in ("summary", "metrics"):
        details = result.get(field)
        if not isinstance(details, Mapping):
            continue
        distance, reach = details.get("target_distance"), details.get("max_range")
        if isinstance(distance, (int, float)) and not isinstance(distance, bool):
            words = f" The target is {distance:.0f} blocks away"
            if isinstance(reach, (int, float)) and not isinstance(reach, bool):
                words += f"; the call allowed {reach:g}"
            words += "."
            blocked = details.get("sight_blocked_by")
            cell = _cell(blocked.get("pos")) if isinstance(blocked, Mapping) else None
            if cell:
                what = str(blocked.get("block") or "block").replace("_", " ")
                words += f" The {what} at {cell} is between the Body's eyes and the target."
            return words
    return ""


def _route_words(result: Mapping[str, Any]) -> str:
    """A walk whose pathfinder found no route says so, with what it was allowed
    to change: in the End arrival cell five such walks read only "stopped"
    (end baseline run-3, 15:19)."""
    for field in ("summary", "metrics"):
        details = result.get(field)
        route = details.get("route") if isinstance(details, Mapping) else None
        if isinstance(route, Mapping) and route.get("route_found") is False:
            allowed = [word for key, word in (("may_break", "break blocks"), ("may_place", "place blocks"))
                       if route.get(key) is True]
            may = (" It was allowed to " + " and ".join(allowed) + ".") if allowed else \
                " It was not allowed to break or place blocks."
            blocks = route.get("placeable_blocks")
            held = f" It carries {blocks} blocks it can place." if type(blocks) is int and "place blocks" in allowed else ""
            return " The pathfinder found no route to the target." + may + held
    return ""


def attempt_lines(history: list[Mapping[str, Any]], earlier: int = 0) -> list[str]:
    """Earlier attempts of the same call, oldest first, the current one left out.

    ``history`` holds the recent attempts including the current one; each has
    ``result`` and the ``body`` (position, inventory) seen when it ended.
    """
    if len(history) < 2 and not earlier:
        return []
    now = history[-1].get("body") if history else None
    lines = [f"There were {earlier} earlier attempts before these."] if earlier else []
    for index, attempt in enumerate(history[:-1], start=earlier + 1):
        result = attempt.get("result") if isinstance(attempt.get("result"), Mapping) else {}
        interruption = _interruption(result)
        if interruption:
            lines.extend(interruption_lines([attempt], earlier=index - 1, include_count=False))
            continue
        if result.get("success") is True:
            lines.append(f"Attempt {index}: ended normally.")
            continue
        same = attempt.get("body") == now
        lines.append(
            f"Attempt {index}: failure: {result.get('reason') or 'no reason given'}; "
            + ("inventory and position unchanged." if same else "inventory or position changed since.")
        )
    return lines


def _interruption(result: Mapping[str, Any]) -> Mapping[str, Any]:
    metrics = result.get("metrics")
    handoff = metrics.get("direct_call_handoff") if isinstance(metrics, Mapping) else None
    if not isinstance(handoff, Mapping):
        handoff = result.get("supervision")
    facts = handoff.get("facts") if isinstance(handoff, Mapping) else None
    return facts if isinstance(facts, Mapping) and facts.get("default_action") else {}


def interruption_lines(history: list[Mapping[str, Any]], earlier: int = 0, *,
                       style: str = "attempts", include_count: bool = True) -> list[str]:
    """All attempts of the suspended work, including the one just stopped.

    Its current attempt is distinct from the protective call's current result.
    No judgement about whether another attempt is worthwhile is added.
    The alternate styles are for the required offline wording comparison.
    """
    lines = [f"There were {earlier} earlier attempts before these."] if earlier and include_count else []
    for index, attempt in enumerate(history, start=earlier + 1):
        result = attempt.get("result") if isinstance(attempt.get("result"), Mapping) else {}
        facts = _interruption(result)
        default = facts.get("default_action") or {}
        tool = default.get("tool")
        if not tool:
            lines.append(f"Attempt {index}: {result_sentence('the original call', result)}")
            continue
        body = attempt.get("body") or {}
        position = _position(body.get("position", body.get("pos")))
        inventory = _inventory(body)
        values = (", ".join(f"{k} {v}" for k, v in inventory.items()) or "empty") if inventory is not None else "not recorded"
        protective_result = default.get("result")
        protective_result = protective_result if isinstance(protective_result, Mapping) else {}
        reason = result.get("reason") or "no reason given"
        if style == "events":
            prefix = f"On attempt {index}, Decision stopped the original work ({reason}) and the Body ran {tool}()."
        elif style == "outcomes":
            prefix = f"Attempt {index} of the original work returned {reason}; the Body switched to {tool}()."
        else:
            prefix = f"Attempt {index}: the original work was interrupted for {tool}() ({reason})."
        if protective_result:
            prefix += " " + result_sentence(f"{tool}()", protective_result)
        lines.append(prefix + f" Inventory at interruption: {values}. Position: {position or 'not recorded'}.")
    return lines


_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")

def frame_state(
    *, goal: str, intent: str, tool: str, params: Mapping[str, Any] | None,
    facts: Mapping[str, Any], running: bool, phase: str | None = None,
    elapsed_s: float | None = None, progress_signal: str | None = None,
    since_progress_s: float | None = None, result: Mapping[str, Any] | None = None,
    earlier_attempts: list[str] | None = None,
    interrupted_attempts: list[str] | None = None,
) -> dict[str, Any]:
    """Describe one frame.  Every fact is said once, as a sentence."""
    call = call_text(tool, params)
    state: dict[str, Any] = {
        # Only a task Decision can read: a Chinese goal (the user's words in a
        # direct call, the Agent's in a plan) carries nothing for it and reads
        # as work at odds with the step. Replay 2026-10-18: a place_block
        # 0.7 s in, call_agent 0.75 with the Chinese task, 0.15 without it
        # and 0.22 with it in English; 60 sampled frames, 20 lower, 1 higher.
        "agent_stance": stance_sentence(facts),
        **({"task": goal} if not _CJK.search(goal or "") else {}),
        # Outcome-shaped Agent words such as "there is a pickaxe in the
        # inventory" are an intention, not an observed inventory fact.
        # This clarification was verified on recorded running frames; keep
        # the separately verified terminal-frame wording unchanged.
        "agent_intent": f"The Agent wants the following result: “{intent}”." if running and intent else intent,
        "agent_rules": agent_rules_sentence(facts),
    }
    inventory = _inventory(facts)
    if inventory is not None:
        state["inventory"] = ", ".join(f"{item} {count}" for item, count in inventory.items()) or "empty"
    state["inventory_space"] = inventory_space(facts)
    state["main_hand"] = _main_hand(facts)
    state["body"] = _body_sentence(facts)
    state["threats"] = _threat_sentence(facts)
    if running:
        work = f"{call} is running."
        if isinstance(elapsed_s, (int, float)):
            work += f" elapsed {elapsed_s:.1f}s."
        if phase:
            work += f" Phase: {phase}."
        state["current_work"] = work
        if progress_signal and isinstance(since_progress_s, (int, float)):
            progress = f"last progress event: {progress_signal}, {since_progress_s:.1f}s ago."
        else:
            progress = "no real progress has been observed yet."
        delta = facts.get("inventory_delta")
        if isinstance(delta, Mapping) and delta:
            progress += " inventory changed: " + ", ".join(
                f"{item} {amount:+d}" for item, amount in delta.items() if isinstance(amount, int)
            ) + "."
        state["progress"] = progress
    else:
        state["finished_call"] = result_sentence(call, result)
        if earlier_attempts:
            state["earlier_attempts_of_this_call"] = list(earlier_attempts)
        if interrupted_attempts:
            state["interruptions_of_the_original_work"] = list(interrupted_attempts)
    start = _position(facts.get("start_position"))
    # A result carries the Body's place as the call ended; the sample stream
    # may be one step behind it. End run-12: a pearl carried the Body into the
    # End, the frame still said the overworld place and no dimension, and
    # Decision chose to throw again from the End platform, into the void.
    ended = {} if running else _ended_state(result)
    now = _position(ended.get("position")) or _position(facts.get("position", facts.get("pos")))
    if start and now:
        state["position"] = f"started at {start}; now at {now}."
    elif now:
        state["position"] = f"now at {now}."
    if movement_sentence(facts):
        state["movement"] = movement_sentence(facts)
    was, here = facts.get("start_dimension"), ended.get("dimension") or facts.get("dimension")
    if isinstance(was, str) and isinstance(here, str) and was and here and was != here:
        state["position"] = (state.get("position", "") + f" The Body is now in {dimension_words(here)}; "
                             f"it was in {dimension_words(was)} when the call started.").strip()
    return {key: value for key, value in state.items() if value not in (None, "")}


def _ended_state(result: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """The Body state a result reports: its position and dimension as the call ended."""
    if not isinstance(result, Mapping):
        return {}
    for field in ("metrics", "summary"):
        details = result.get(field)
        if isinstance(details, Mapping) and ("position" in details or "dimension" in details):
            return details
    return {}


# A result ended from outside (the user, Decision, protection, a preempting
# owner) says nothing about whether the same call can work: it was cut short.
_STOPPED_FROM_OUTSIDE = {"cancel_requested", "cancelled", "preempted", "stopped_by_decision", "turn_cancelled"}


def _block(value: Any) -> tuple[int, int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return tuple(int(float(v) // 1) for v in value)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    return None


def body_unchanged_by_call(after: Mapping[str, Any] | None, before: Mapping[str, Any] | None = None) -> bool:
    """True only when the facts show the call left the Body as it found it.

    Same block position, same inventory, same item in hand. With no ``before``
    the call's own execution facts are used (start position and inventory
    change). Missing evidence is never read as "unchanged".
    """
    after = after if isinstance(after, Mapping) else {}
    now = _block(after.get("position", after.get("pos")))
    if now is None:
        return False
    if isinstance(before, Mapping):
        if _block(before.get("position", before.get("pos"))) != now:
            return False
        inventories = [_inventory(state) for state in (before, after)]
        if inventories[0] is None or inventories[1] is None or dict(inventories[0]) != dict(inventories[1]):
            return False
        hands = [state.get("selected_item") for state in (before, after)]
        return hands[0] == hands[1]
    delta = after.get("inventory_delta")
    return _block(after.get("start_position")) == now and isinstance(delta, Mapping) and not delta


def same_item_run_again(end: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    """A ladder item's last run completed by itself and changed nothing, and
    the Body is still as that run found it: starting the item again is the
    same attempt (frame-question §10 for ladder items, s7-later-20261029.md
    §8.14).  In T7 a standing ``wood`` item completed with nothing changed 98
    times in four minutes and ``night-spot`` re-sealed a sealed cell every
    second; each run counted as Body work, so neither the heartbeat nor
    ``none_started`` reached the Agent.  Missing evidence is never "same".
    """
    if end.get("outcome") != "completed" or end.get("inventory_change") != {}:
        return False
    there = _block(end.get("position"))
    counts = end.get("inventory_counts")
    if there is None or _block(end.get("start_position")) != there or not isinstance(counts, Mapping):
        return False
    now = _inventory(facts)
    if now is None or _block(facts.get("position", facts.get("pos"))) != there:
        return False
    try:
        held = {str(name): int(count) for name, count in now.items() if int(count)}
        then = {str(name): int(count) for name, count in counts.items() if int(count)}
    except (TypeError, ValueError):
        return False
    return held == then


def _same_attempt_again(result: Mapping[str, Any], unchanged: bool) -> bool:
    """The call ended by itself and nothing about the Body moved: running it
    again is the same attempt. 4294 such repeats after failures in the
    2026-10-14/15 runs, 44 then succeeded; up to 873 in a row (go_to_surface,
    life-stage b2). A success counts too: a move_to onto the cell the Body
    already stood in ended normally 94 times in a row (house run 2026-10-18)."""
    if not unchanged:
        return False
    metrics = result.get("metrics") if isinstance(result.get("metrics"), Mapping) else {}
    if str(result.get("reason") or "") in _STOPPED_FROM_OUTSIDE or "direct_call_handoff" in metrics:
        return False
    return True


def frame_options(
    *, tool: str, params: Mapping[str, Any] | None, intent: str,
    facts: Mapping[str, Any], running: bool, result: Mapping[str, Any] | None = None,
    next_call: str = "", tree: Mapping[str, str] | None = None, repeat_note: str = "",
    intent_of_plan: bool = False,
    interrupt: Mapping[str, Any] | None = None,
    unchanged: bool = False,
) -> dict[str, str]:
    """The concrete things the Body may do next in this frame.

    ``intent_of_plan`` says the step wrote no intent of its own and ``intent``
    is the one of the plan around it. ``unchanged`` says the finished call left
    the Body as it found it (``body_unchanged_by_call``).
    """
    if dead(facts):
        return {}
    call = call_text(tool, params)
    result = result if isinstance(result, Mapping) else {}
    failed = not running and result.get("success") is not True
    stop = "Stop the current action" if running else "Do not repeat it"
    options: dict[str, str] = {}
    if running:
        if intent and intent_of_plan:
            # Said as the plan's words: read as this step's own purpose, a
            # plan that ends in the next step makes the next step look due.
            options["keep_going"] = f"Keep doing the current step, {call}. It is one step of the plan: “{intent}”"
        elif intent:
            options["keep_going"] = f"Keep doing the current work, {call}, for: “{intent}”"
        else:
            options["keep_going"] = f"Keep doing the current work, {call}."
    elif not (failed and result.get("canRetry") is False) and not _same_attempt_again(result, unchanged):
        options["repeat"] = f"Run the same call again: {call}." + (f" {repeat_note}" if repeat_note else "")
    # After a failed step the plan cannot simply move on: later steps depend on it.
    # While a call runs for an intent, whether the facts already show it is
    # asked as its own question (frame_questions); the next step follows from
    # that answer, not from an option beside "keep going".
    if not failed and not (running and intent):
        options["next_step"] = (
            f"{stop} and start the next step of the plan: {next_call}." if next_call
            else f"{stop} and report to the Agent that this work is finished."
        )
    for key, text in (tree or {}).items():
        options[str(key)] = str(text)
    if running and isinstance(interrupt, Mapping):
        condition = interrupt.get("condition")
        handler = interrupt.get("handler")
        if isinstance(condition, Mapping) and isinstance(handler, Mapping):
            options["interrupt"] = (
                f"Stop the current action because “{condition.get('text', '')}” "
                f"and do: {handler.get('intent', '')}."
            )
    if failed:
        options.update(equipment_options(facts, result, call=call))
    options.update(protective_options(facts, stop=stop))
    if running:
        # On the way means while work runs. Offered in idle and result frames
        # too (not on the bench), H7 mined lapis, redstone and gold while the
        # Agent thought, the after-frame offered only stay or the Agent, and
        # each ore woke the Agent (7 of 31 hand-backs in the first hour).
        options.update(wayside_options(facts, stop=stop))
    if failed and next_call:
        # The Agent hears of the failure and may revise or take over at any
        # time; the Body does not stand still waiting for it (user
        # 2026-10-18: Agent fully asynchronous, Body busy at least 90%).
        # Replay of 22 live failure frames: chosen where later steps stand
        # on their own (glass on an occupied cell 0.81, the next search
        # direction 0.79), repeat or call_agent kept where they did.
        options["report_and_go_on"] = f"Tell the Agent what went wrong and go on with the next step of the plan: {next_call}."
    options["call_agent"] = f"{stop} and {HANDBACK_CALL}"
    return options


def equipment_candidates(facts: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, str]:
    """Other owned tools of the observed held/broken tool's game category."""
    held = str(facts.get("selected_item") or "").removeprefix("minecraft:")
    source = held
    for details in (result.get("summary"), result.get("metrics")):
        if isinstance(details, Mapping) and isinstance(details.get("broken_item"), str):
            source = details["broken_item"].removeprefix("minecraft:")
            break
    category = next((kind for kind in ("pickaxe", "axe", "shovel", "sword")
                     if source.endswith("_" + kind)), None)
    if category is None:
        return {}
    return {"equip:" + str(item).removeprefix("minecraft:"): str(item)
            for item, count in sorted((_inventory(facts) or {}).items())
            if isinstance(count, (int, float)) and count > 0
            and str(item).endswith("_" + category)
            and str(item).removeprefix("minecraft:") not in {held, source}}


# Result reasons in which the Body reports that its tool was the problem.
TOOL_FAILURE_REASONS = ("tool_broke", "missing_required_tool")


def tool_failed(result: Mapping[str, Any]) -> bool:
    """The result itself says the tool broke or was not the one needed."""
    details = [result] + [d for d in (result.get("summary"), result.get("metrics")) if isinstance(d, Mapping)]
    if any(isinstance(d.get("broken_item"), str) for d in details):
        return True
    reasons = [d.get("reason") for d in details if isinstance(d.get("reason"), str)]
    return any(r in TOOL_FAILURE_REASONS or r.startswith("tool_equip_failed") for r in reasons)


def equipment_options(facts: Mapping[str, Any], result: Mapping[str, Any], *, call: str) -> dict[str, str]:
    """Owned tools of the same category, when a call failed because of its tool.

    Offered for any failure, "equip the stone sword and run the same call
    again" was chosen after a fight ended with its target gone and the sword
    intact, 2 of 2 times (live step8-extra-claude3), and after a failed move
    away (step8-extra-claude1). The user chose tool failures only (2026-10-15).
    """
    if result.get("success") is True or not tool_failed(result):
        return {}
    return {key: f"Equip {item} from the inventory and run the same call again: {call}."
            for key, item in equipment_candidates(facts, result).items()}


def equipment_intent(item: str) -> str:
    return f"Hold {item} in the main hand."


def repeat_note(again_when: str, until: str) -> str:
    """The Agent's own repeat conditions, said beside the same-call option."""
    return f"Repeat when: {again_when}; stop when: {until}."


def protective_options(facts: Mapping[str, Any], *, stop: str = "") -> dict[str, str]:
    """One eligibility compiler for work and idle protective tools."""
    options: dict[str, str] = {}
    prefix = f"{stop} and " if stop else ""
    def text(action: str) -> str:
        return prefix + action if prefix else action[0].upper() + action[1:]
    # In lava the Body loses health every half second. Its frames offered
    # only staying and it burned from 20 to death in 4 s: the place last
    # stood safely on was the cell the lava had just flowed into
    # (eval-20261020-m1 run-2, 18:33).
    exit_cell = _cell(facts.get("lava_exit"))
    if facts.get("in_lava") is True and exit_cell:
        options["leave_lava"] = text(f"get out of the lava now, onto the dry block at {exit_cell}.")
    # Leaving a cloud is one of the steps: the ones that end outside it say so.
    options.update(step_options(facts, text))
    owned = {str(item).removeprefix("minecraft:"): count for item, count in (_inventory(facts) or {}).items()}
    # Water cannot be poured in the Nether.
    if (facts.get("on_fire") is True and facts.get("in_lava") is not True and owned.get("water_bucket", 0) > 0
            and _cell(facts.get("standing_on"))
            and facts.get("dimension") != "minecraft:the_nether"):
        options["put_out_fire"] = text("pour the water bucket at the Body's feet to put out the fire.")
    air = facts.get("oxygen")
    # Breathing again, swimming up is no move at all: right after
    # go_to_surface ended normally, the 9B Instant Decision chose it again
    # 117 of 240 times (replay of track S 2026-10-20; H2O stayed 233).
    if isinstance(air, (int, float)) and air < AIR_FULL and not breathing(facts):
        options["surface"] = text("swim up to air now.")
    if cushion_fall_offered(facts):
        fall = facts["fall"]
        what = str(fall["lands_on"]).replace("_", " ")
        options["cushion_fall"] = text(
            f"pour the water bucket just before landing, to land in water: fallen {float(fall.get('fallen') or 0):g} blocks "
            f"so far, {what} {float(fall['ground_below']):g} blocks straight below; landing in water takes no health.")
    if scoop_water_offered(facts):
        # Another fall can be landed only with the bucket full again; whether
        # to take it back is the next choice (form B, §6.y).
        options["scoop_water"] = text(f"fill the empty bucket from the water source at {_cell(facts.get('water_source_in_reach'))}.")
    food_item, food = facts.get("food_item"), facts.get("food")
    if isinstance(food_item, str) and food_item and isinstance(food, (int, float)) and food < 20:
        options["eat"] = text(f"eat {food_item} from the inventory.")
    # The timing is Decision's: the Body only reports an owned bed it can click
    # now and the dragon's head distance in the frame facts.
    options.update(end_bed_options(facts, stop=stop))
    near = hostiles(facts)
    target = _hurt_target(near, facts)
    fight = melee_target(near, facts)
    if fight is not None and isinstance(fight.get("id"), str) and fight["id"]:
        fight_words = f"{fight['name']} ({fight['dist']:.0f} blocks away{_wound_words(fight)})"
        hand = _main_hand(facts)
        held = {str(item).removeprefix("minecraft:"): count for item, count in (_inventory(facts) or {}).items()}
        weapon = next((item for item in MELEE_WEAPONS if held.get(item, 0) > 0), None)
        if weapon and weapon != hand:
            # The Body draws it before the first swing; "with the cobblestone
            # in the main hand" was said while an iron sword was in the bag (M2 run-1).
            held_with = f" with the {weapon} from the inventory"
        else:
            held_with = f" with the {hand} in the main hand" if hand and hand != "nothing" else " with bare hands"
        # Said in the option: with only "in melee", a zombie at one block
        # and an iron sword in hand still lost to fleeing (probe 24/36 vs 36/36).
        options["engage"] = text(f"fight back against the {fight_words} in melee{held_with}.")
        if fight.get("in_sight") is False:
            # A fact and what the call does with it: engage_entity paths to
            # the mob, through or around what stands between (easy run-12).
            options["engage"] += (f" The {fight['name']} is out of the Body's sight behind blocks: to reach "
                                  "it, the Body digs through them or goes around.")
        source = _hurt_source(facts)
        if source is not None and source.get("id") == fight.get("id"):
            source_sentence = _hurt_source_sentence(facts)
            if source_sentence:
                options["engage"] += " " + source_sentence
    if target is not None:
        target_words = f"{target['name']} ({target['dist']:.0f} blocks away{_wound_words(target)})"
        if isinstance(target.get("id"), str) and target["id"]:
            held = {str(item).removeprefix("minecraft:"): count for item, count in (_inventory(facts) or {}).items()}
            bow = next((item for item in ("bow", "crossbow") if held.get(item, 0) > 0), None)
            if bow and held.get("arrow", 0) > 0:
                options["ranged"] = text(f"shoot the {target_words} with the {bow} from the inventory.")
                consequence = PROJECTILE_CONSEQUENCES.get(str(target.get("type") or ""))
                if consequence:
                    options["ranged"] += " " + consequence
                record = shot_record_sentence(facts, target)
                if record:
                    options["ranged"] += " " + record
                options["ranged"] += f" Arrows left: {held['arrow']}."
                if str(target.get("type") or "") == "ender_dragon":
                    crystals = crystals_seen(facts)
                    if crystals == 1:
                        options["ranged"] += (" 1 end crystal still stands within 64 blocks; while it stands, it heals"
                                              " the dragon back to full.")
                    elif crystals:
                        options["ranged"] += (f" {crystals} end crystals still stand within 64 blocks; while any"
                                              " stands, they heal the dragon back to full.")
        kind = mob_kind(target)
        if kind in DODGE_ONLY:
            options["move_away"] = text(f"move away from the {target_words}.")
        elif target["dist"] < FLEE_CAP:
            # The run is away from every hostile seen (from_hostiles), so it is
            # said so, each one named. Named after one mob only, a creeper coming
            # in was in no option (run-44 2.71-2.92: "run from the Skeleton" while
            # a creeper walked up and blew); replay of 40 such frames: run chosen
            # 0 -> 7 with every mob named (project-review-20261110 §9.16).
            around = [mob for mob in near if mob["dist"] <= RUN_NAMES_WITHIN][:RUN_NAMES_MAX]
            if len(around) > 1:
                named = "; ".join(f"{mob['name']} {mob['dist']:.0f} blocks away{_wound_words(mob)}"
                                  + (", coming closer" if mob.get("approaching") is True else "")
                                  for mob in around)
                options["move_away"] = text(
                    f"run from the hostile mobs around the Body ({named}) until the nearest is "
                    f"{flee_distance(kind, target['dist']):.0f} blocks away.")
            else:
                options["move_away"] = text(
                    f"run from the {target_words} until it is {flee_distance(kind, target['dist']):.0f} blocks away.")
        if "move_away" in options and target.get("in_sight") is False:
            options["move_away"] += f" The {target['name']} is out of the Body's sight behind blocks."
        if "move_away" in options and target.get("type") in FAST_CHASERS:
            options["move_away"] += f" A {target['name']} is faster than the Body; running does not lose it."
        # What the step can do: it walks at most DODGE_STEP_REACH cells on
        # level ground, so a shooter or the dragon is not left by it, nor by
        # two of them. Not said, the dragon's dodge was chosen 15 times in a
        # row standing still (End run-29, dragon-unit-tests §6.y.41). Replay
        # of 80 such frames: move_away 40 -> 25 with this sentence, 24 with
        # the no-safe-point words too; adding "only off the line it is on
        # now" undid it (40).
        reach = attack_reach(str(target.get("type") or ""))
        if kind in DODGE_ONLY and reach > 2 * DODGE_STEP_REACH:
            options["move_away"] += (
                f" It is one step of at most {DODGE_STEP_REACH} blocks on level ground; the {target['name']} "
                f"reaches {reach:.0f} blocks, so the step does not take the Body out of its reach.")
    # No "go back to the last safe place": the program judged which place was
    # safe, and the place it kept was where the dragon's breath had just
    # landed; Decision chose leaving the cloud and going back to it in turn
    # until the Body died (end baseline run-3, 15:21:27-35). Removed at the
    # user's word: leaving the danger is itself the safe move.
    # The Agent's rules in every frame where the Body can carry them out, not
    # only beside other protective options: whether a rule's "when" holds is
    # Decision's to judge from the frame (dragon-unit-tests-20261030
    # §6.y.22). frame_questions asks them with the work choice when no other
    # protective option is offered, so a rule is not behind a danger question.
    options.update(rule_options(facts, text))
    return options


def _point(value: Any) -> tuple[float, float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 3 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return (float(value[0]), float(value[1]), float(value[2]))
    return None


def step_options(facts: Mapping[str, Any], text: Any) -> dict[str, str]:
    """One quick step in each direction the Body reports it can walk, offered
    while a hostile mob is seen or a cloud is near: no distance gate and no
    need to be hurt first. The End baseline Body stood with only "stay" while
    the dragon came on from 74 blocks, and a single long walk out of the
    breath cloud went back into it (dragon-unit-tests-20261030 §6, B). Each
    option says where it ends and how far that is from the threat; a cell
    inside the cloud is not offered. Going on dodging is Decision choosing
    the next step in the next frame."""
    steps = facts.get("steps")
    if not isinstance(steps, (list, tuple)):
        return {}
    cloud = facts.get("cloud") if isinstance(facts.get("cloud"), Mapping) else None
    centre = _point(cloud.get("pos")) if cloud else None
    radius = cloud.get("radius") if cloud and isinstance(cloud.get("radius"), (int, float)) else None
    # Steps are for exact footing: out of a cloud, off a place that hurts, or
    # out of the dragon's line. Against other mobs a step chosen again every
    # second is a dodge that goes nowhere: they get a whole run (move_away) or
    # a fight (body-tool-boundaries-20261110 §7 #5).
    near = [mob for mob in hostiles(facts) if mob_kind(mob) in DODGE_ONLY]
    hurt = hurt_here(facts)
    if not near and (centre is None or radius is None) and hurt is None:
        return {}
    here = _point(facts.get("position", facts.get("pos")))
    threat = near[0] if near else None
    there = _point(threat.get("pos")) if threat else None
    options: dict[str, str] = {}
    for step in steps:
        if not isinstance(step, Mapping) or step.get("direction") not in STEP_DIRECTIONS:
            continue
        cell = _cell(step.get("to"))
        if cell is None:
            continue
        end = (cell[0] + 0.5, float(cell[1]), cell[2] + 0.5)
        if centre is not None and radius is not None and abs(end[1] - centre[1]) <= 2 and math.hypot(
                end[0] - centre[0], end[2] - centre[2]) <= radius + 0.3:
            continue
        words = f"step {step['direction']} to {cell}"
        if (cloud and cloud.get("inside") is True) or (hurt is not None and here and math.dist(end, here) >= 2):
            words += ", away from the place that is taking the Body's health"
        # Said as a direction, the count beside it: on the run-4 frames the
        # 9B Decision picked a step away 93% (stay 13 of 509) against 89%
        # (stay 23) with the count alone (probe 2026-10-30).
        if threat and there and here:
            change = math.dist(end, there) - math.dist(here, there)
            blocks = max(1, round(abs(change)))
            unit = "block" if blocks == 1 else "blocks"
            if change >= 0.5:
                words += f", away from the {threat['name']} ({blocks} {unit} farther)"
            elif change <= -0.5:
                words += f", toward the {threat['name']} ({blocks} {unit} nearer)"
            else:
                words += f", neither toward nor away from the {threat['name']}"
        options[f"step_{step['direction']}"] = text(words + ".")
    return options


# The Agent's standing rules (life profile ``rules``): its own "when ..., ..."
# offered beside the protective actions under neutral keys, in list order.
# Bench of 86 recorded protection frames, 12 orders each
# (decide-like-a-player-20261023 §8): the flee rules held on every frame; a
# fight rule whose condition plainly held was followed on 43 of 44.
RULE_PREFIX = "rule_"
# A rule argument written as this string is the nearest hostile mob of the frame.
NEAREST_HOSTILE = "nearest_hostile"


# Ores open to the air near the Body (Body fact ``exposed_ores``), mined on the
# way when the Agent's plan wants them (mechanism-map §11.6, D5). Only the
# physical limit is applied: the pickaxe the ore needs to drop anything.
PICKAXE_TIER = {"wooden_pickaxe": 0, "golden_pickaxe": 0, "stone_pickaxe": 1, "copper_pickaxe": 1,
                "iron_pickaxe": 2, "diamond_pickaxe": 3, "netherite_pickaxe": 4}
ORE_TIER = {"coal_ore": 0, "nether_gold_ore": 0, "nether_quartz_ore": 0, "copper_ore": 1, "iron_ore": 1,
            "lapis_ore": 1, "gold_ore": 2, "redstone_ore": 2, "diamond_ore": 2, "emerald_ore": 2,
            "ancient_debris": 3}
ORE_DROP = {"coal_ore": "coal", "nether_gold_ore": "gold_nugget", "nether_quartz_ore": "quartz",
            "copper_ore": "raw_copper", "iron_ore": "raw_iron", "lapis_ore": "lapis_lazuli", "gold_ore": "raw_gold",
            "redstone_ore": "redstone", "diamond_ore": "diamond", "emerald_ore": "emerald",
            "ancient_debris": "ancient_debris"}
# Not protective, yet run the same way: the current work stops, the action
# runs, and Decision is asked whether to go back to the work.
# Chosen, these run though they protect nothing. scoop_water chosen 4 times in
# the frame after a cushioned landing ran nothing; the next throw found the
# bucket empty and no cushion to offer (end run-34 22:18-22:20, §6.y.47).
BODY_DEFAULTS = frozenset({"eat", "mine_ore", "scoop_water"})


def _end_bed_choice(choice: Any) -> bool:
    return isinstance(choice, str) and choice.startswith("use_bed_")


def _bed_cells(facts: Mapping[str, Any]) -> list[list[int]]:
    """Cells of beds the Body owns; ledger rows win over life-local fallback."""
    rows: list[Any] = []
    own = facts.get("own_beds")
    if isinstance(own, (list, tuple)):
        rows.extend(own)
    if not isinstance(own, (list, tuple)) and isinstance(facts.get("placed_beds"), (list, tuple)):
        rows.extend(facts["placed_beds"])
    cells: list[list[int]] = []
    for row in rows:
        if isinstance(row, Mapping):
            block = str(row.get("block", "")).removeprefix("minecraft:")
            pos = row.get("pos")
        else:
            block, pos = "bed", row
        if block and block != "bed" and not block.endswith("_bed"):
            continue
        cell = _cell(pos)
        if cell is not None and cell not in cells:
            cells.append(cell)
    return cells


def dragon_bed_distances(facts: Mapping[str, Any]) -> list[tuple[list[int], float]]:
    """Observed distances from the visible dragon head to each owned bed."""
    dragon = next((mob for mob in hostiles(facts) if mob.get("type") == "ender_dragon"
                   and _point(mob.get("head_pos")) is not None), None)
    if dragon is None:
        return []
    head = _point(dragon["head_pos"])
    assert head is not None
    return [(cell, math.dist(head, [cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5]))
            for cell in _bed_cells(facts)]


def dragon_bed_sentence(facts: Mapping[str, Any]) -> str | None:
    distances = dragon_bed_distances(facts)
    if not distances:
        return None
    return " ".join(f"The dragon's head is {distance:.1f} blocks from the bed at {cell}."
                    for cell, distance in distances)


def end_bed_options(facts: Mapping[str, Any], *, stop: str = "") -> dict[str, str]:
    """Offer generic block use for an owned bed already in interaction reach."""
    if not any(mob.get("type") == "ender_dragon" for mob in hostiles(facts)):
        return {}
    dimension = str(facts.get("dimension") or "").removeprefix("minecraft:")
    if dimension and dimension != "the_end":
        return {}
    here = _point(facts.get("position", facts.get("pos")))
    if here is None:
        return {}
    prefix = f"{stop} and " if stop else ""
    options: dict[str, str] = {}
    for cell in _bed_cells(facts):
        centre = [cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5]
        if math.dist(here, centre) > 4.5:
            continue
        key = "use_bed_" + "_".join(str(value) for value in cell)
        options[key] = (f"{prefix}use the bed at {cell} now. Using a bed in the End explodes it."
                        if prefix else f"Use the bed at {cell} now. Using a bed in the End explodes it.")
    return options


def runs_default(choice: Any) -> bool:
    return is_protective(choice) or choice in BODY_DEFAULTS or _end_bed_choice(choice)


def wayside_ore(facts: Mapping[str, Any]) -> dict[str, Any] | None:
    """The nearest exposed ore the Body has a pickaxe for, while the Agent's
    plan is there to judge it against (the life profile)."""
    if not isinstance(facts.get("life_profile"), Mapping):
        return None
    owned = {str(item).removeprefix("minecraft:"): count for item, count in (_inventory(facts) or {}).items()}
    tier = max((PICKAXE_TIER[item] for item, count in owned.items() if item in PICKAXE_TIER and count > 0), default=-1)
    for ore in facts.get("exposed_ores") or ():
        if not isinstance(ore, Mapping):
            continue
        block = str(ore.get("block") or "").removeprefix("minecraft:")
        need = ORE_TIER.get(block.removeprefix("deepslate_"))
        cell = _cell(ore.get("pos"))
        if need is not None and tier >= need and cell:
            return {"block": block, "pos": cell, "dist": ore.get("dist"),
                    "drop": ORE_DROP[block.removeprefix("deepslate_")]}
    return None


def _ore_words(ore: Mapping[str, Any]) -> str:
    away = f", {ore['dist']:.0f} blocks away" if isinstance(ore.get("dist"), (int, float)) else ""
    return f"{ore['block']} at {ore['pos']}{away}"


def wayside_options(facts: Mapping[str, Any], *, stop: str = "") -> dict[str, str]:
    ore = wayside_ore(facts)
    if ore is None:
        return {}
    if stop:
        return {"mine_ore": f"{stop} and mine the exposed {_ore_words(ore)}; then go back to the current work."}
    return {"mine_ore": f"Mine the exposed {_ore_words(ore)}."}


def wayside_facts(facts: Mapping[str, Any]) -> dict[str, str]:
    """What a wayside choice is judged by, said beside it (D5 bench, H6 running
    frames with an ore added: plan without the item 0/20 mined; plan with it
    and room left 10/20; inventory full or another ore 1/20 each). Without the
    ore said as a fact, or with the whole plan unsaid, 0-1/20."""
    ore = wayside_ore(facts)
    if ore is None:
        return {}
    said = {}
    plan = agent_plan_whole(facts)
    if plan:
        said["agent_plan"] = ("The Agent's plan, in its order: " + plan + " An item whose condition holds now may be"
                              " done on the way: the current work waits and goes on after it.")
    said["exposed_ores"] = f"Open to the air nearby: {_ore_words(ore)}."
    space = inventory_space(facts)
    if space:
        said["inventory_space"] = space
    return said


def agent_plan_whole(facts: Mapping[str, Any], *, leave_out: Iterable[str] = ()) -> str | None:
    """The Agent's active plan items, in its order, whether the Body starts
    them as work or not: what a wayside choice is judged against (D5)."""
    profile = facts.get("life_profile") if isinstance(facts.get("life_profile"), Mapping) else {}
    skip = set(leave_out)
    parts = []
    for item in profile.get("projects") or ():
        if not (isinstance(item, Mapping) and item.get("id") and item.get("status") == "active"):
            continue
        if f"project_{item['id']}" in skip:
            continue
        intent = str(item.get("intent") or "").strip().rstrip(".")
        when = str(item.get("when") or "").strip().rstrip(".")
        parts.append(f"{len(parts) + 1}) “{item['id']}”: {intent}" + (f" (it fits when {when})" if when else ""))
    return "; ".join(parts) + "." if parts else None


def is_protective(choice: Any) -> bool:
    """A protective action or one of the Agent's rules."""
    return choice in PROTECTIVE or (isinstance(choice, str) and choice.startswith(RULE_PREFIX))


def agent_rules(facts: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The running ladder item's own rules when it has them ([] for none),
    else the profile's (ladder-item-consolidation-20261108 §3, step 2)."""
    rules = facts.get("item_rules")
    if not isinstance(rules, list):
        profile = facts.get("life_profile") if isinstance(facts.get("life_profile"), Mapping) else {}
        rules = profile.get("rules") if isinstance(profile, Mapping) else None
    return [rule for rule in rules or [] if isinstance(rule, Mapping)]


def bind_rule_arguments(arguments: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any] | None:
    """The rule's arguments for this frame, or None when the Body cannot do it
    now: no hostile mob to bind, or an item it does not own."""
    near = hostiles(facts)
    owned = {str(item).removeprefix("minecraft:"): count for item, count in (_inventory(facts) or {}).items()}
    bound: dict[str, Any] = {}
    for key, value in arguments.items():
        if value == NEAREST_HOSTILE:
            if not near:
                return None
            if key == "pos" or key.endswith("_pos"):
                if not (isinstance(near[0].get("pos"), (list, tuple)) and len(near[0]["pos"]) == 3):
                    return None
                value = [float(part) for part in near[0]["pos"]]
        elif key == "item" and isinstance(value, str) and owned.get(value.removeprefix("minecraft:"), 0) <= 0:
            return None
        bound[key] = value
    return bound


def rule_action(choice: str, facts: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The call a chosen rule runs, bound to the frame it was offered in."""
    try:
        rule = agent_rules(facts)[int(choice.removeprefix(RULE_PREFIX)) - 1]
    except (ValueError, IndexError):
        return None
    call = rule.get("call") if isinstance(rule.get("call"), Mapping) else {}
    arguments = call.get("arguments") if isinstance(call.get("arguments"), Mapping) else {}
    bound = bind_rule_arguments(arguments, facts)
    return None if bound is None or not call.get("tool") else (str(call["tool"]), bound)


def agent_rules_sentence(facts: Mapping[str, Any]) -> str | None:
    """Rules without calls are standing instructions, in the Agent's words."""
    words = []
    for rule in agent_rules(facts):
        if rule.get("call") is None:
            when = str(rule.get("when") or "").strip().removeprefix("when ").removeprefix("When ")
            then = str(rule.get("then") or "").strip().rstrip(".")
            words.append(f"when {when}, {then}")
    return "The Agent's standing rules, in its words: " + "; ".join(words) + "." if words else None


def rule_options(facts: Mapping[str, Any], text: Any = None) -> dict[str, str]:
    """Each call-rule the Body can carry out now, in the Agent's words."""
    write = text or (lambda action: action[0].upper() + action[1:])
    options: dict[str, str] = {}
    for index, rule in enumerate(agent_rules(facts), start=1):
        key = f"{RULE_PREFIX}{index}"
        if rule_action(key, facts) is not None:
            when = str(rule.get("when") or "").strip().removeprefix("when ").removeprefix("When ")
            then = str(rule.get("then") or "").strip().rstrip(".")
            options[key] = write(f"the Agent said: “when {when}, {then}”. Do that now.")
            # The rule said with what it does to health now: "stand and shoot"
            # was chosen 9 times while the dragon's breath took the Body from
            # 16 to 5 (end run-7, 22:27:26-39).
            action = rule_action(key, facts)
            if hurt_here(facts) is not None and action is not None and action[0] in STAND_STILL_TOOLS:
                options[key] += " Doing it, the Body stays where it is losing health."
    return options


# Calls a rule may name that leave the Body where it stands.
STAND_STILL_TOOLS = frozenset({"ranged_attack", "consume_item", "use_item", "equip_item", "use_on_entity", "use_on_block"})


# What a running protective action is judged on.
PROTECTION_FACTS = ("body", "threats", "inventory", "main_hand", "position")


def running_fight(facts: Mapping[str, Any], current: str, target_id: Any) -> tuple[str, str] | None:
    """The fight a running protective action is in, bound to its own target:
    the sentence for the frame and the option that carries it on.

    The options name the nearest hostile; a Body fighting the perched dragon
    with an enderman nearer had no way to go on fighting the dragon, and in
    run-21..25 ten sword attacks on the perched dragon were each replaced
    within seconds, none swung. Replay of the 20 perched-dragon frames
    (~/mbprobe/engage): fighting went on 3 -> 13, fleeing 7 -> 1 with this
    sentence and option (body-preemption-20261106.md §8.3). Fights only: a
    flight said as running kept fleeing while health fell (step5-threat-claude2).
    """
    if current not in {"engage", "ranged"} or not target_id:
        return None
    target = next((item for item in hostiles(facts) if str(item.get("id")) == str(target_id)), None)
    if target is None:
        return None
    words = f"{target['name']} ({target['dist']:.0f} blocks away{_wound_words(target)})"
    if current == "engage":
        hand = _main_hand(facts)
        held = f" with the {hand}" if hand and hand != "nothing" else ""
        return (f"The Body is fighting the {words} in melee{held}.",
                f"Keep fighting the {words} in melee{held}, as the Body is doing now.")
    return (f"The Body is shooting at the {words}.", f"Keep shooting at the {words}, as the Body is doing now.")


def protecting_options(facts: Mapping[str, Any], current: str, target_id: Any = None) -> dict[str, str]:
    """While a protective action runs, every frame asks the protective choice
    again; the running action is one of the options, not a "keep going".
    Offered "keep going", Decision kept fleeing at one block while health fell
    from 18 to 2 (live step5-threat-claude2). A fight is carried on under its
    own target (running_fight)."""
    options = protective_options(facts)
    if current not in options:
        options = {current: DEFAULT_ACTION_INTENTS.get(current, current), **options}
    fight = running_fight(facts, current, target_id)
    if fight is not None:
        options = {"keep_fighting": fight[1], **options}
    # Under a stance staying is one of the protective choices here too: a
    # flight under a stance that still held went on for 22 s, the Body stepping
    # back and forth 4 blocks with the mobs 30 away (easy run-5, 17:44:51).
    hold = hold_option(facts)
    if hold:
        options["stay_put"] = hold
    options["call_agent"] = "Hand control back to the Agent for a new plan."
    return options


# After a protective action taken while idle there is no work to go back to;
# staying means the idle loop picks the next step. Said as "wait for the next
# step", the 9B Instant Decision handed back to the Agent 107 of 361 times
# once the stale surface option was gone; said this way it stayed 321
# (replay of track S 2026-10-20; H2O stayed 336).
IDLE_AGAIN = "Go back to being idle where the Body is; the idle Body chooses its next step itself."


def after_default_options(
    *, original_call: str, intent: str, default_call: str, result: Mapping[str, Any] | None,
    facts: Mapping[str, Any] | None = None, protective_choice: str = "",
) -> dict[str, str]:
    """What the Body may do once a protective action has ended: go back (or
    stay), any protective action the facts allow now, owned spare equipment,
    or the Agent.

    Offered as "Run the same call again", a fight refused at low health was
    repeated twelve times under a skeleton's arrows (step5-threat-claude4);
    named beside the others, the replay chose to move away 18/18.  The one
    that just ended follows the same eligibility as the others: offered
    anyway, going back to the safe place where the Body already stood was
    chosen 12 times in a row (step5-threat-claude9)."""
    if dead(facts or {}):
        return {}
    result = result if isinstance(result, Mapping) else {}
    options = {
        "resume": f"Go back to the earlier work, {original_call}" + (f", for: “{intent}”" if intent else "."),
    } if original_call else {"stay": IDLE_AGAIN + behind_blocks_sentence(facts or {})}
    options.update(protective_options(facts or {}))
    options.update(equipment_options(facts or {}, result, call=default_call))
    options["call_agent"] = "Do not go back and hand control back to the Agent for a new plan."
    return options


# When to hand back, said as the logic an Agent prompt would give
# (mechanism-map-20261027.md §11.6, user 2026-10-30: "decision不笨 ... 是能继续干，
# 还是临时卡住，还是计划根本上出错了"). Replay of H6 frames (~/mbprobe/handback,
# baseline 53/53 as live, two runs alike): wrongly handed back 0/17 -> 15/17 go
# on; rightly handed back kept 5/6 (2/6 without the call_agent wording);
# controls kept 29/30.
HANDBACK_CALL = "hand control back to the Agent because the plan itself cannot work as it is; the Agent makes a new plan."
_HANDBACK_LOGIC = {
    # Said as "keep going" it was read as an order and the Body never mined an
    # ore on the way (D5 bench v4 1/20 vs v6 9/20); said about handing back
    # only, the A4 replay is unchanged (15/17, 5/6, 29/30).
    "running": (
        " While the work runs, do not hand it back to the Agent because it is slow, has only just started or is"
        " still finding its way (for example a path still being calculated); if it is stuck for now, set it aside"
        " when that option is offered. Hand control back to the Agent only when the facts show the Agent's plan"
        " itself is wrong: what it needs is not here, or the work is going somewhere the Agent did not mean."
        " Whether the intent is already reached is asked separately; do not stop the work for that."),
    "result": (
        " Read the result against the Agent's intent. If the work can still go on (this step is done and the next"
        " follows, the same call can still make progress, or the next step does not need this one), go on."
        " If it is stuck for now for a reason that may pass or that a retry can get past, try again or report it"
        " and go on; the Body does not stand waiting for the Agent. Hand control back to the Agent only when the"
        " facts show the plan itself cannot work: what it needs is missing, or the next steps depend on a result"
        " that did not happen."),
}


# The Agent's own ways of getting out of reach. While one runs, carrying it
# on is one of the protective choices: without it the danger branch offered
# only a fight, a run or "stay where the Body is", and the Agent's seal (run-15
# 03:33:04) and its dig away from a skeleton (03:36:59) were each stopped by
# an engage within 2 s; the second ended in a creeper blast
# (body-tool-boundaries-20261110 §13).
AGENT_PROTECTION_TOOLS = frozenset({"shelter_in_place", "dig_down", "dig_up", "pillar_up", "raise_shield"})


def frame_questions(
    options: Mapping[str, str], *, protecting: bool = False, acting_for_intent: bool = False,
    interrupt: Mapping[str, Any] | None = None, running_protection: bool = False,
    handback: str = "", hold: str = "", keep_protecting: str = "",
) -> dict[str, Any]:
    """One request.  The danger question is asked only when a protective
    action is on offer in this frame.  Whether running work has reached its
    intent is asked in its own request (done_question).  ``handback`` ("running"
    or "result") adds when to go on and when to hand back to the Agent.
    ``hold`` (hold_option) is offered last among the protective choices."""
    # The Agent's call-rules go with the protective options when those are asked
    # (in danger); alone, they are part of the work choice: a rule whose
    # condition is not danger must not wait for a danger answer.
    # Only built-in protective actions trigger d/p, independently of rules.
    guarding = not protecting and any(is_protective(key) and not str(key).startswith(RULE_PREFIX) for key in options)
    protective = {key: text for key, text in options.items() if is_protective(key) and guarding}
    normal = dict(options) if protecting else {key: text for key, text in options.items()
                                               if not is_protective(key) or not guarding}
    questions: dict[str, Any] = {
        "q": {"type": "choice", "instructions": (
            "Choose how the Body protects itself right now." if running_protection
            else "Choose what the Body does right now for the Agent: its current work, or an item of its plan"
            " whose condition holds now." if acting_for_intent and "mine_ore" in normal
            else "Choose what the Body does right now to achieve the Agent's intent."
            if acting_for_intent else "Choose what the Body does next."
        ), "criteria": normal},
    }
    if handback in _HANDBACK_LOGIC and "call_agent" in normal and not protecting:
        questions["q"]["instructions"] += _HANDBACK_LOGIC[handback]
    if "interrupt" in normal and isinstance(interrupt, Mapping):
        condition, handler = interrupt.get("condition"), interrupt.get("handler")
        if isinstance(condition, Mapping) and isinstance(handler, Mapping):
            questions["q"]["instructions"] += (
                f" The Agent also says: when “{condition.get('text', '')}”, "
                f"interrupt the current work and do “{handler.get('intent', '')}”.")
    if protective:
        questions["d"] = {"type": "choice", "instructions": "Is the Body in danger?", "criteria": dict(_DANGER)}
        questions["p"] = {
            "type": "choice", "instructions": "Choose how the Body protects itself.",
            "criteria": {**({"keep_going": keep_protecting} if keep_protecting else {}), **protective, **({"call_agent": options["call_agent"]} if "call_agent" in options else {}),
                         **({"stay_put": hold} if hold else {})},
        }
    return questions


def interrupt_result_question(*, goal: str, intent: str, original_call: str,
                              handler_intent: str, handler_call: str,
                              facts: Mapping[str, Any]) -> dict[str, Any]:
    state = frame_state(goal=goal, intent=intent, tool="interrupt_handler", params={},
                        facts=facts, running=False,
                        result={"success": True, "reason": "handler_completed"})
    state["finished_call"] = f"The interruption handler for “{handler_intent}” has ended normally."
    options = after_default_options(
        original_call=original_call, default_call=handler_call, intent=intent,
        result={"success": True}, facts=facts)
    options["repeat"] = f"Do the interruption handler again: “{handler_intent}”, starting with {handler_call}."
    return {"state": state, "questions": frame_questions(options)}


def nearby_players(facts: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    # Native action frames and perception reads use different names for the
    # same bounded entity sample. An explicit empty sample is authoritative.
    entities = next((facts[key] for key in ("nearby_entities", "entities", "entities_near")
                     if isinstance(facts.get(key), (list, tuple))), [])
    return [item for item in entities
            if isinstance(item, Mapping)
            and str(item.get("entity_type", item.get("type", ""))).removeprefix("minecraft:") == "player"]


def player_distance(player: Mapping[str, Any], facts: Mapping[str, Any]) -> float | None:
    if isinstance(player.get("dist"), (int, float)):
        return float(player["dist"])
    if isinstance(player.get("dist2"), (int, float)):
        return max(0, player["dist2"]) ** 0.5
    here, there = facts.get("position", facts.get("pos")), player.get("pos")
    if isinstance(here, (list, tuple)) and isinstance(there, (list, tuple)) and len(here) == len(there) == 3:
        return sum((float(a) - float(b)) ** 2 for a, b in zip(here, there)) ** 0.5
    return None


def life_running_options(facts: Mapping[str, Any], running: str) -> dict[str, str]:
    """A running life action: the same idle range with the running action under
    its own name, first.  Offered only as "Keep doing the current life activity:
    __reflex_look_at(...)", it was never chosen: following a walking player
    switched between follow, approach and look every frame (step8-extra-claude2;
    offline 64/64 switches, named 60/64 carried on)."""
    options = idle_options(facts)
    if running in options:
        return {running: options[running], **{key: text for key, text in options.items() if key != running}}
    return options


def _cell(value: Any) -> list[int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 3 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return [math.floor(v) for v in value]
    return None


def _predicted_point(value: Any) -> str | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        parts = [float(part) for part in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(part) for part in parts):
        return None
    return "[" + ", ".join(f"{part:.3f}".rstrip("0").rstrip(".") for part in parts) + "]"


def home_candidates(facts: Mapping[str, Any]) -> list[list[int]]:
    """Where going home walks to: the Agent's arrival cells, else the home anchor."""
    profile = facts.get("life_profile") if isinstance(facts.get("life_profile"), Mapping) else {}
    home = profile.get("home") if isinstance(profile, Mapping) else None
    if not isinstance(home, Mapping):
        return []
    points = home.get("arrival_positions") or [home.get("pos")]
    return [cell for cell in (_cell(point) for point in points) if cell is not None]


def home_arrival(candidates: list[list[int]], cells: list[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Whether the Body fits in each candidate: feet and head clear, solid below.

    A cell the read did not cover, or reported UNKNOWN, leaves that candidate
    unknown; only a read that shows a block in the way says it cannot be used.
    """
    by_pos = {}
    for fact in cells or ():
        if isinstance(fact, Mapping) and all(isinstance(fact.get(axis), int) for axis in "xyz"):
            by_pos[(fact["x"], fact["y"], fact["z"])] = fact
    out = []
    for x, y, z in candidates:
        feet, head, below = (by_pos.get((x, y + dy, z)) for dy in (0, 1, -1))
        known = [fact for fact in (feet, head, below) if fact is not None and fact.get("state") != "UNKNOWN"]
        blocked = [f"{fact.get('type', '').removeprefix('minecraft:')} {name}"
                   for name, fact in (("at feet height", feet), ("at head height", head))
                   if fact is not None and fact.get("state") not in {"CLEAR", "UNKNOWN", None}]
        if below is not None and below.get("state") == "CLEAR":
            blocked.append(f"nothing solid below ({below.get('type', '').removeprefix('minecraft:')})")
        standable = False if blocked else (True if len(known) == 3 else None)
        out.append({"pos": [x, y, z], "standable": standable, **({"blocked": blocked} if blocked else {})})
    return out


def home_target(facts: Mapping[str, Any]) -> list[int] | None:
    """The first home cell not shown to be blocked; None when every one is."""
    arrival = facts.get("home_arrival")
    if isinstance(arrival, list) and arrival:
        return next((list(item["pos"]) for item in arrival
                     if isinstance(item, Mapping) and item.get("standable") is not False), None)
    candidates = home_candidates(facts)
    return candidates[0] if candidates else None


def home_here(facts: Mapping[str, Any]) -> bool:
    """The Body is in the home's dimension; a home written without one is in the overworld.

    A frame that does not say its dimension is taken as the home's.
    """
    profile = facts.get("life_profile") if isinstance(facts.get("life_profile"), Mapping) else {}
    home = profile.get("home") if isinstance(profile, Mapping) else None
    here = dimension_id(facts.get("dimension"))
    if not isinstance(home, Mapping) or here is None:
        return True
    return here == (dimension_id(home.get("dimension")) or "minecraft:overworld")


def home_portal(facts: Mapping[str, Any]) -> list[int] | None:
    """The nearest known portal on this side that the Body came through from the home's dimension, or went into."""
    profile = facts.get("life_profile") if isinstance(facts.get("life_profile"), Mapping) else {}
    home = profile.get("home") if isinstance(profile, Mapping) else None
    here, position = dimension_id(facts.get("dimension")), facts.get("position", facts.get("pos"))
    if not isinstance(home, Mapping) or here is None or _cell(position) is None:
        return None
    there = dimension_id(home.get("dimension")) or "minecraft:overworld"
    ends = [_cell(link[a].get("pos")) for link in facts.get("known_portals") or () if isinstance(link, Mapping)
            for a, b in (("from", "to"), ("to", "from"))
            if isinstance(link.get(a), Mapping) and isinstance(link.get(b), Mapping)
            and link[a].get("dimension") == here and link[b].get("dimension") == there]
    ends = [cell for cell in ends if cell is not None]
    return min(ends, key=lambda cell: math.dist(cell, [float(value) for value in position])) if ends else None


def action_intent(choice: str, facts: Mapping[str, Any]) -> str:
    """The intent a reflex action runs under; going home from another dimension walks into a portal."""
    if _end_bed_choice(choice):
        return end_bed_options(facts).get(choice, choice)
    if choice == "go_home" and not home_here(facts):
        return DEFAULT_ACTION_INTENTS["go_home_portal"]
    return DEFAULT_ACTION_INTENTS.get(choice, choice)


def at_home(facts: Mapping[str, Any]) -> bool:
    """In the home's dimension and within the at-place band of the home anchor or of any arrival cell."""
    profile = facts.get("life_profile") if isinstance(facts.get("life_profile"), Mapping) else {}
    home = profile.get("home") if isinstance(profile, Mapping) else None
    if not isinstance(home, Mapping) or _position(facts.get("position", facts.get("pos"))) is None:
        return False
    if not home_here(facts):
        return False
    places = [home.get("pos"), *(home.get("arrival_positions") or [])]
    return any(_position(place) is not None and player_distance({"pos": place}, facts) <= AT_PLACE_BLOCKS
               for place in places)


def home_blocked_sentence(arrival: list[Mapping[str, Any]]) -> str:
    """What a fully blocked home looks like, for the Agent."""
    parts = [f"{item['pos']} has {' and '.join(item.get('blocked') or ['something in the way'])}"
             for item in arrival]
    return "The Body cannot stand at any home arrival position now: " + "; ".join(parts) + "."


def idle_options(facts: Mapping[str, Any]) -> dict[str, str]:
    """Idle range compiled from generic player/profile facts and protection."""
    if dead(facts):
        return {}
    options: dict[str, str] = {}
    profile = facts.get("life_profile") if isinstance(facts.get("life_profile"), Mapping) else {}
    players = nearby_players(facts) if profile else []
    thinking = facts.get("agent_thinking") is True
    if players:
        player = players[0]
        name = str(player.get("name") or player.get("id") or "the nearby player")
        options["look_player"] = f"Look toward {name} at the observed position."
        distance = player_distance(player, facts)
        # This is the existing approach tool's arrival band, not a judgement
        # about when the life intention has been achieved.
        if distance is not None and distance > 2:
            options["approach_player"] = f"Walk to {name} at the observed position."
        if not thinking:
            options["follow_player"] = f"Follow {name} as a moving player."
    position = _position(facts.get("position", facts.get("pos")))
    # A home cell a block now fills is not offered: walking there failed
    # 163 times in a row once a plank sat at its head height (villa run
    # 2026-10-16). The cells are read every idle frame, so a cleared cell
    # or a new arrival position comes back by itself (pro plan P5).
    if position and not at_home(facts) and not thinking:
        if home_here(facts):
            target = home_target(facts)
            if target:
                options["go_home"] = f"Walk to the home position {_position(target)}."
        else:
            # From another dimension home is reached only through a portal the Body
            # has walked through; with none known the option is not offered.
            portal = home_portal(facts)
            if portal:
                home = profile.get("home") if isinstance(profile.get("home"), Mapping) else {}
                options["go_home"] = (f"Walk into the portal at {_position(portal)}; it leads to "
                                      f"{dimension_words(home.get('dimension'))}, where home is.")
    if not thinking:
        relationships = profile.get("relationships") or []
        visible = {str(p.get("name")) for p in players}
        for relationship in relationships:
            if isinstance(relationship, Mapping) and relationship.get("player") not in visible:
                name = str(relationship.get("player") or "")
                if name:
                    options.setdefault("seek_player", f"Search nearby for {name} and walk up to them.")
    # The Agent's plan ladder, in its order, also while it thinks (decision 8).
    options.update(ladder_options(facts, profile))
    options["stay"] = ("Go on as the Body is: it keeps moving (the Agent's setting); wait for the next step."
                       if movement_sentence(facts) else "Stay where the Body is and wait for the next step."
                       + behind_blocks_sentence(facts))
    options.update(protective_options(facts))
    # While the Agent is thinking it is already awake; calling it adds nothing.
    if profile and not thinking:
        options["call_agent"] = "Hand control back to the Agent for a new direction."
    return options


def ladder_options(facts: Mapping[str, Any], profile: Mapping[str, Any]) -> dict[str, str]:
    """Ladder items the Body may start now (plan-ladder-20261028.md §7.3).

    An item is offered when the Agent marked it active, it has work (walk),
    its run state is waiting, the Agent has not taken the Body, and running it
    now would not be the same attempt as its last run (``ladder_same_run``,
    §8.14).  Whether
    its ``when`` holds is Decision's to judge, never the program's.  Wording
    from bench-project2 (step7-life-claude2 frames, two routine wordings, 12
    orders): "Leave what I am doing ... in my area" was chosen 72/72 by day
    and 0/72 at dusk at home; "Work on my project" 26/72 and "Go to my area"
    62/72 at dusk.  The sentence without an area and the "It fits when"
    sentence are checked on the replay bench (§7.14).
    """
    if facts.get("ladder_held") is True:
        return {}
    states = facts.get("ladder_states") if isinstance(facts.get("ladder_states"), Mapping) else {}
    same_again = facts.get("ladder_same_run") or ()
    options: dict[str, str] = {}
    for project in profile.get("projects") or []:
        if not (isinstance(project, Mapping) and project.get("status") == "active" and project.get("id")
                and isinstance(project.get("work"), Mapping)):
            continue
        if states.get(project["id"], "waiting") != "waiting" or project["id"] in same_again:
            continue
        where = f" in my area “{project['area']}”" if project.get("area") else ""
        text = f"Leave what I am doing and work on my project “{project['id']}”{where}: {project.get('intent', '')}"
        when = str(project.get("when") or "").strip().rstrip(".")
        if when:
            text = f"{text.rstrip()}{'' if text.rstrip().endswith(('.', '!', '?')) else '.'} It fits when {when}."
        options[f"project_{project['id']}"] = text
    return options


def set_aside_option(stop: str) -> str:
    """An Agent ladder item may be set aside instead of handed back (§7.4)."""
    return f"{stop} and set this work aside for now; tell the Agent."


def daylight_phase(clock: Any) -> str | None:
    """"day", "dusk" or "night" from the Overworld clock (None when unknown)."""
    if not isinstance(clock, (int, float)) or isinstance(clock, bool):
        return None
    tick = clock % 24000
    return "day" if tick < 12000 or tick >= 23000 else "dusk" if tick < 13000 else "night"


def daylight_sentence(clock: Any) -> str | None:
    """Day, dusk or night from the Overworld clock, the same words for the Agent and Decision."""
    phase = daylight_phase(clock)
    return None if phase is None else {
        "day": "It is day.", "dusk": "It is dusk; night falls soon.", "night": "It is night."}[phase]


def dusk_notice(clock: Any) -> str | None:
    """What a player sees when the sun goes down (easy-run-20261106.md): the clock, said as seen."""
    phase = daylight_phase(clock)
    if phase not in {"dusk", "night"}:
        return None
    tick = int(clock % 24000)
    if phase == "dusk":
        return (f"The sun is setting (time {tick}). Night falls at 13000, in about {round((13000 - tick) / 20)} "
                "seconds, and lasts until about 23000; hostile mobs spawn in the dark until then.")
    return (f"Night has fallen (time {tick}); it lasts until about 23000, and hostile mobs spawn "
            "in the dark until then.")


def dawn_notice(clock: Any) -> str | None:
    """What a player sees when the sun comes up: the clock, said as seen."""
    if daylight_phase(clock) != "day":
        return None
    tick = int(clock % 24000)
    return f"The sun is up (time {tick}). Sunset is at 12000, in about {round(((12000 - tick) % 24000) / 20)} seconds."


FURNACE_FACT_BLOCKS = 32.0


def known_furnaces(world: Mapping[str, Any], dimension: Any, position: Any, now: float) -> list[dict[str, Any]]:
    """Furnaces within FURNACE_FACT_BLOCKS that the Body has loaded or looked into, nearest first; empty ones too.

    Times are taken here, as seconds before ``now``, so a replayed frame says what the live one said."""
    here = _cell(position)
    where = dimension_id(dimension)
    if here is None or where is None:
        return []
    found = []
    for place in world.get("places") or ():
        furnace = place.get("furnace") if isinstance(place, Mapping) else None
        slots = furnace.get("slots") if isinstance(furnace, Mapping) else None
        if not isinstance(slots, Mapping) or place.get("gone"):
            continue
        if place.get("dimension") != where or _cell(place.get("pos")) is None:
            continue
        distance = math.dist([float(v) for v in place["pos"]], [float(v) for v in here])
        if distance > FURNACE_FACT_BLOCKS:
            continue
        loaded = furnace.get("loaded") if isinstance(furnace.get("loaded"), Mapping) else {}
        found.append({
            "pos": list(place["pos"]), "block": place.get("block") or "furnace", "distance": round(distance),
            "slots": dict(slots), "put_in": dict(loaded.get("slots") or {}),
            "put_in_s_ago": round(now - loaded["t"]) if isinstance(loaded.get("t"), (int, float)) else None,
            "looked_s_ago": round(now - furnace["t"]) if isinstance(furnace.get("t"), (int, float)) else None,
            "not_looked_since": bool(furnace.get("last_was_load")),
            "empty": not (slots.get("input") or slots.get("output")),
        })
    return sorted(found, key=lambda item: item["distance"])[:4]


def _minutes(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "at a time not recorded"
    return "less than a minute ago" if seconds < 60 else f"{int(seconds // 60)} minutes ago"


def furnace_sentence(facts: Mapping[str, Any]) -> str | None:
    """What the furnaces near the Body hold, from the Body's own record (user 2026-11-10).

    An emptied furnace is said too: with only the ones that hold something, a
    ladder item waiting for a furnace still fired 2 of 12 recorded run-33
    frames after the furnace was emptied; with "empty" said, 0 of 12, while it
    fired 7-8 of 12 with things put in 4 minutes ago and 10-11 of 12 with
    finished items (replay 2026-11-10)."""
    furnaces = [item for item in facts.get("known_furnaces") or () if isinstance(item, Mapping)]
    if not furnaces:
        return None
    parts = []
    for item in furnaces:
        slot = lambda value: f"{value['count']} {value['item']}" if isinstance(value, Mapping) else ""
        cell = item.get("pos") or ["?", "?", "?"]
        head = f"{item.get('block')} at ({cell[0]}, {cell[1]}, {cell[2]}), {item.get('distance')} blocks away: "
        put = item.get("put_in") or {}
        text = head
        if item.get("empty"):
            parts.append(head + f"empty, looked into {_minutes(item.get('looked_s_ago'))}")
            continue
        if put.get("input"):
            fuel = f" with {slot(put['fuel'])}" if put.get("fuel") else ""
            text += f"{slot(put['input'])}{fuel} put in {_minutes(item.get('put_in_s_ago'))}, "
        if item.get("not_looked_since"):
            text += "not looked into since"
        else:
            slots = item.get("slots") or {}
            seen = [part for part in (f"{slot(slots.get('output'))} finished" if slots.get("output") else "",
                                      f"{slot(slots.get('input'))} not yet" if slots.get("input") else "") if part]
            text += f"looked into {_minutes(item.get('looked_s_ago'))}: " + ", ".join(seen)
        parts.append(text)
    return ("Furnaces near the Body, from the Body's own record (a furnace turns one "
            "item every 10 seconds): " + "; ".join(parts) + ".")


def life_surroundings(facts: Mapping[str, Any]) -> dict[str, str]:
    """Daylight, nearby players and furnaces holding something, said the same way in idle and project frames."""
    state: dict[str, str] = {}
    daylight = daylight_sentence(facts.get("time"))
    if daylight:
        state["daylight"] = daylight
    players = nearby_players(facts)
    if players:
        lines = []
        for player in players:
            distance = player_distance(player, facts)
            name = player.get("name") or player.get("id") or "a nearby player"
            lines.append(f"Player {name} is {distance:.0f} blocks away." if distance is not None
                         else f"Player {name} is in view.")
        state["players"] = " ".join(lines)
    furnaces = furnace_sentence(facts)
    if furnaces:
        state["furnaces"] = furnaces
    return state


def body_condition_facts(facts: Mapping[str, Any]) -> dict[str, str]:
    """The Body facts a plan condition is judged on, as sentences.

    A plan's wait condition used to get the raw Body state (20k characters,
    the clock as a tick number). "Minecraft daytime has begun" was answered
    unknown 49 times in a row in full daylight (villa run 2026-10-15); the
    frames of the same run said "It is day." and were read correctly.

    The clock is said beside it, because the Agent writes conditions in its
    numbers too: "body time is between 0 and 12000, i.e. it is daytime again"
    with only "It is night." was unknown 64/64 for the 9B Instant Decision;
    with the clock, pending 64/64, and satisfied 20/20 by day (replay of
    track S 2026-10-20). "Minecraft daytime has begun" stayed satisfied by
    day and pending by night.
    """
    state: dict[str, str] = {"body": _body_sentence(facts)}
    state.update(life_surroundings(facts))
    clock = facts.get("time")
    if "daylight" in state and isinstance(clock, (int, float)):
        state["daylight"] += f" The game clock reads {clock % 24000:.0f} of 24000 ticks."
    threats = _threat_sentence(facts)
    if threats:
        state["threats"] = threats
    inventory = _inventory(facts)
    if inventory is not None:
        state["inventory"] = ", ".join(f"{k} {v}" for k, v in inventory.items()) or "empty"
    hand = _main_hand(facts)
    if hand:
        state["main_hand"] = hand
    position = _position(facts.get("position", facts.get("pos")))
    if position:
        state["position"] = f"now at {position}."
    if movement_sentence(facts):
        state["movement"] = movement_sentence(facts)
    return state


def movement_sentence(facts: Mapping[str, Any]) -> str | None:
    """The legs as the Body holds them (body-channels-20261105 §6.1): Decision
    chooses hands and dodges knowing the legs keep going."""
    movement = facts.get("movement")
    if not isinstance(movement, Mapping) or movement.get("mode") != "keep_moving":
        return None
    centre = _position(movement.get("center"))
    radius = movement.get("radius")
    within = f" within {float(radius):g} blocks" if isinstance(radius, (int, float)) else ""
    words = f"The Body keeps moving around {centre}{within} (the Agent's setting); shots, eating and the shield do not stop it."
    if movement.get("held_by_action"):
        words += " The current action holds the legs; they go on when it ends."
    return words


def project_frame(state: Mapping[str, Any], options: Mapping[str, str],
                  facts: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """A running frame of the Agent's own project: the work frame, plus the
    life intention, daylight and players, and the option to put it down."""
    shown = dict(state)
    if facts.get("life_intent"):
        shown["life_intent"] = f"This is the idle-life intention: “{facts['life_intent']}”."
    shown.update(life_surroundings(facts))
    put_down = "Stop the current action and put the project down for now; go back to everyday life."
    if "call_agent" not in options:
        return shown, {**options, "put_down": put_down}
    ordered = {key: text for key, text in options.items() if key != "call_agent"}
    return shown, {**ordered, "put_down": put_down, "call_agent": options["call_agent"]}


def idle_question(facts: Mapping[str, Any], options: Mapping[str, str]) -> dict[str, Any]:
    """Compile idle prose here, alongside the work-frame prose."""
    state: dict[str, Any] = {}
    stance = stance_sentence(facts)
    if stance:
        state["agent_stance"] = stance
    state["body"] = _body_sentence(facts)
    if facts.get("life_intent"):
        state["agent_intent"] = f"This is the idle-life intention: “{facts['life_intent']}”."
    rules = agent_rules_sentence(facts)
    if rules:
        state["agent_rules"] = rules
    state.update(life_surroundings(facts))
    threats = _threat_sentence(facts)
    if threats:
        state["threats"] = threats
    inventory = _inventory(facts)
    if inventory is not None:
        state["inventory"] = ", ".join(f"{k} {v}" for k, v in inventory.items()) or "empty"
    space = inventory_space(facts)
    if space:
        state["inventory_space"] = space
    hand = _main_hand(facts)
    if hand:
        state["main_hand"] = hand
    position = _position(facts.get("position", facts.get("pos")))
    if position:
        state["position"] = f"now at {position}."
    if movement_sentence(facts):
        state["movement"] = movement_sentence(facts)
    plan = agent_plan_sentence(facts, options)
    if plan:
        state["agent_plan"] = plan
    rest = agent_plan_whole(facts, leave_out=options) if "mine_ore" in options else None
    if rest:
        state["agent_plan_rest"] = "Also on the Agent's plan, not started as work: " + rest
    if "mine_ore" in options:
        state.update({key: text for key, text in wayside_facts(facts).items() if key != "agent_plan"})
    state["agent"] = agent_state_sentence(facts)
    return {"state": state, "questions": frame_questions(options, hold=hold_option(facts))}


def agent_plan_sentence(facts: Mapping[str, Any], options: Mapping[str, str]) -> str | None:
    """The offered ladder items as the Agent's plan, in its order (plan-ladder §9.y).

    Offered only as options, a fitting item was chosen 0/40 on H6 frames:
    nothing said they were the Agent's plan. Said as the plan, with how it is
    read (§7.3: Agent's order, an item waits while its condition does not
    hold), 28/40 and no unfitting item. Whether a condition holds stays
    Decision's judgement.
    """
    profile = facts.get("life_profile") if isinstance(facts.get("life_profile"), Mapping) else {}
    by_key = {f"project_{p['id']}": p for p in profile.get("projects") or ()
              if isinstance(p, Mapping) and p.get("id")}
    items = [by_key[key] for key in options if key in by_key]
    if not items:
        return None
    parts = []
    for number, item in enumerate(items, 1):
        intent = str(item.get("intent") or "").strip().rstrip(".")
        when = str(item.get("when") or "").strip().rstrip(".")
        parts.append(f"{number}) “{item['id']}”: {intent}" + (f" (it fits when {when})" if when else ""))
    return ("The Agent's plan for the Body, in its order: " + "; ".join(parts) + ". The Body works on the "
            "first item whose condition holds now; an item whose condition does not hold waits.")


def agent_state_sentence(facts: Mapping[str, Any]) -> str:
    """What the Agent is doing, so waiting for it is not read as the next step (§9.y)."""
    if facts.get("ladder_held") is True:
        return HELD_WORDS
    if facts.get("agent_thinking") is True:
        return "The Agent is thinking about its next step and does not need the Body to wait for it."
    return "The Agent is not directing the Body right now; the Body acts on its own."


def _seconds_words(seconds: float) -> str:
    whole = max(0, int(seconds))
    if whole < 120:
        return f"{whole} second{'' if whole == 1 else 's'}"
    return f"{whole // 60} minutes"


def stance_sentence(facts: Mapping[str, Any]) -> str | None:
    """The Agent's stance (life profile ``stance``) as the frame says it, with its age."""
    stance = facts.get("agent_stance")
    if not isinstance(stance, Mapping) or not isinstance(stance.get("text"), str) or not stance["text"].strip():
        return None
    age = stance.get("age_s")
    written = f"written {_seconds_words(age)} ago" if isinstance(age, (int, float)) else "written earlier"
    return f"The Agent's reading of the situation, {written}: “{stance['text'].strip()}”"


def behind_blocks_sentence(facts: Mapping[str, Any]) -> str:
    """Said with staying where the Body is, when the nearest hostile mob is out of
    its sight: a fact about the place. Easy run-12 18:23: sealed in a pit, the
    replayed frame chose engage 10/10 and dug the Body out; with this sentence on
    the stay option it chose stay 10/10 (engage's own wording changed nothing)."""
    near = hostiles(facts)
    if near and near[0].get("in_sight") is False:
        return f" Blocks stand between the Body and the {near[0]['name']} here."
    return ""


def hold_option(facts: Mapping[str, Any]) -> str:
    """Staying put, offered in the danger branch only under a stance: without
    one, a Body just woken at the spawn stayed in open danger (run-3 replay,
    12:59:23). Said with what it costs where the place itself takes health."""
    if not stance_sentence(facts):
        return ""
    # A falling Body cannot stay where it is: once running frames carried the
    # fall (8ab4cea), staying would have stood beside cushion_fall.
    if isinstance(facts.get("fall"), Mapping):
        return ""
    return (STAY_PUT + (" Doing it, the Body stays where it is losing health." if hurt_here(facts) is not None else "")
            + behind_blocks_sentence(facts))


def chosen_option_text(choice: str, facts: Mapping[str, Any]) -> str:
    """The words a protective choice was offered with, read from these facts."""
    return protective_options(facts).get(choice) or DEFAULT_ACTION_INTENTS.get(choice, choice)


def nearest_hostile_words(facts: Mapping[str, Any]) -> str:
    near = hostiles(facts)
    if not near:
        return "no hostile mob in view"
    return f"the nearest hostile mob, {near[0]['name']}, {near[0]['dist']:.0f} blocks away"


def moved_since_sentence(chosen_at: Any, now: Any, seconds: float | None) -> str | None:
    """How far the Body has gone since its current action was chosen: measured, not judged."""
    start, here = _point(chosen_at), _point(now)
    if start is None or here is None:
        return None
    since = "Since the current action was chosen" + (
        f" {_seconds_words(seconds)} ago" if isinstance(seconds, (int, float)) else "")
    dx, dy, dz = (here[0] - start[0]), (here[1] - start[1]), (here[2] - start[2])
    total = math.sqrt(dx * dx + dy * dy + dz * dz)
    if total < 1.0:
        return f"{since}, the Body has not moved."
    blocks = lambda value: f"{value:.0f} block{'' if round(value) == 1 else 's'}"
    parts = []
    if abs(dy) >= 1.0:
        parts.append(f"{blocks(abs(dy))} {'up' if dy > 0 else 'down'}")
    across = math.hypot(dx, dz)
    if across >= 1.0:
        parts.append(f"{blocks(across)} across")
    return f"{since}, the Body has moved {blocks(total)}" + (f": {' and '.join(parts)}" if parts else "") + "."


def stance_check_question(facts: Mapping[str, Any], *, chosen_at: Any = None,
                          chosen_s_ago: float | None = None) -> dict[str, Any] | None:
    """Whether the Agent's stance still holds while a protective action runs.

    The stance, the body, the nearest threat, the place and how far the Body
    has moved since the action was chosen. Not the action's own words: beside
    "fight the creeper 19 blocks away" a Body still in its sealed pocket was
    read as having left it 516/520 times; without them and with the distance
    said, the run-3 replay read still-in-place seconds as holding 7/8 and the
    climb out of the shaft as broken (closed-loop-architecture-20261107.md §8)."""
    stance = stance_sentence(facts)
    if not stance or dead(facts):
        return None
    state: dict[str, Any] = {
        "agent_stance": stance,
        "body": _body_sentence(facts),
        "threats": _threat_sentence(facts),
    }
    now = facts.get("position", facts.get("pos"))
    position = _position(now)
    if position:
        state["position"] = f"now at {position}."
    moved = moved_since_sentence(chosen_at, now, chosen_s_ago)
    if moved:
        state["movement_since_chosen"] = moved
    return {"state": state, "questions": {"k": copy.deepcopy(STANCE_CHECK)}}


def stance_check_choice(raw: Mapping[str, Any] | None) -> str | None:
    answers = raw.get("answers") if isinstance(raw, Mapping) else None
    answer = answers.get("k") if isinstance(answers, Mapping) else None
    choice = answer.get("choice") if isinstance(answer, Mapping) else None
    return choice if choice in STANCE_CHECK["criteria"] else None


def only_option(questions: Mapping[str, Any]) -> str | None:
    """When a frame leaves a single thing to do there is nothing to ask."""
    if set(questions) == {"q"} and len(questions["q"]["criteria"]) == 1:
        return next(iter(questions["q"]["criteria"]))
    return None


def frame_choice(raw: Mapping[str, Any] | None, questions: Mapping[str, Any]) -> str:
    """Read the offered choice: protective action if in danger, else normal work."""
    answers = raw.get("answers") if isinstance(raw, Mapping) else None
    if not isinstance(answers, Mapping):
        return "unknown"

    def chosen(name: str) -> str | None:
        answer = answers.get(name)
        choice = answer.get("choice") if isinstance(answer, Mapping) else None
        allowed = (questions.get(name) or {}).get("criteria") or {}
        return choice if isinstance(choice, str) and choice in allowed else None

    name = "q"
    if "d" in questions:
        danger = chosen("d")
        if danger not in {"safe", "in_danger"}:
            return "unknown"
        if danger == "in_danger":
            name = "p"
            if chosen("p") == "stay_put":
                # Staying is no protective action: the work choice of the same answer stands.
                return chosen("q") or "unknown"
    return chosen(name) or "unknown"


def in_danger(raw: Mapping[str, Any] | None, questions: Mapping[str, Any]) -> bool:
    """Whether this answer took the protective branch."""
    answers = raw.get("answers") if isinstance(raw, Mapping) else None
    answer = answers.get("d") if isinstance(answers, Mapping) else None
    return "d" in questions and isinstance(answer, Mapping) and answer.get("choice") == "in_danger"


# Facts that can show an intent.  The 4B single-pass Decision compares an
# intent with them correctly only when nothing else is in view: the progress
# line ("inventory changed: oak_log +3") reads as "done" to it, and so do the
# task and the call text (docs/references/reflex-always-on-20261012/
# decision-capability-20261014.md).
_DONE_FACTS = ("inventory", "main_hand", "position")


def done_question(state: Mapping[str, Any], intent: str) -> dict[str, Any]:
    """Whether running work has reached its intent, asked in its own request
    with only the facts the intent can be checked against."""
    small: dict[str, Any] = {"agent_intent": f"The Agent wants the following result: “{intent}”."}
    small.update({key: state[key] for key in _DONE_FACTS if state.get(key)})
    return {"state": small, "questions": {"a": {"type": "choice", "instructions": (
        "Compare each part of the result the Agent wants with the inventory and Body facts above. "
        "Do the facts already show every part of it?"
    ), "criteria": {
        "shown": f"Yes: the facts above already show “{intent}”.",
        "not_shown": f"No: the facts above do not show all of “{intent}” yet.",
    }}}}


def return_place_question(*, work_call: str, intent: str, before: Any, now: Any) -> dict[str, Any] | None:
    """Whether danger stopped the work again where it stopped it before the
    Body last went back.  The 4B Decision compares two places well but not
    a count of interruptions; whether to risk going back again is then the
    Agent's (decision-capability-20261014.md)."""
    before_at, now_at = _position(before), _position(now)
    if not before_at or not now_at:
        return None
    return {"state": {
        "agent_intent": f"The earlier work was for: “{intent}”." if intent else f"The earlier work was {work_call}.",
        "last_return": f"Before the Body last went back to {work_call}, danger stopped it at {before_at}.",
        "this_interruption": f"Now danger has stopped it again at {now_at}.",
    }, "questions": {"r": {"type": "choice", "instructions": (
        "Compare the two places. Did danger stop the work again at about the same place?"
    ), "criteria": {
        "same": "Yes: about the same place as before the last return.",
        "different": "No: somewhere else.",
    }}}}


def return_place_choice(raw: Mapping[str, Any] | None) -> str | None:
    answers = raw.get("answers") if isinstance(raw, Mapping) else None
    answer = answers.get("r") if isinstance(answers, Mapping) else None
    choice = answer.get("choice") if isinstance(answer, Mapping) else None
    return choice if choice in {"same", "different"} else None


def done_choice(raw: Mapping[str, Any] | None) -> str | None:
    """"shown" / "not_shown", or None when there is no valid answer."""
    answers = raw.get("answers") if isinstance(raw, Mapping) else None
    answer = answers.get("a") if isinstance(answers, Mapping) else None
    choice = answer.get("choice") if isinstance(answer, Mapping) else None
    return choice if choice in {"shown", "not_shown"} else None


__all__ = [
    "PROTECTIVE", "VitalWindow", "after_default_options", "attempt_lines", "call_text", "frame_choice",
    "protecting_options", "PROTECTION_FACTS",
    "RULE_PREFIX", "NEAREST_HOSTILE", "is_protective", "agent_rules", "bind_rule_arguments", "rule_action",
    "rule_options", "agent_rules_sentence",
    "frame_options", "frame_questions", "frame_state", "hostiles", "just_hurt",
    "idle_options", "idle_question", "ladder_options", "set_aside_option", "done_question", "done_choice", "in_danger",
    "return_place_question", "return_place_choice",
    "only_option", "result_sentence",
    "STAY_PUT", "HELD_WORDS", "STANCE_CHECK", "stance_sentence", "hold_option", "chosen_option_text",
    "nearest_hostile_words", "moved_since_sentence", "stance_check_question", "stance_check_choice",
]
