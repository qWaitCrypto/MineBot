"""Trace-only autonomy quality evaluator for the FakePlayer AG gates."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Literal


JsonObject = dict[str, Any]
Verdict = Literal["pass", "fail", "insufficient_evidence"]

AUTONOMY_QUALITY_SCHEMA_VERSION = "autonomy-quality-v2"


@dataclass(frozen=True)
class AutonomyQualityThresholds:
    minimum_output_points_per_1800s: float = 4.0
    max_no_output_window_s: float = 900.0
    max_repeated_failure_streak: int = 3
    deadlock_window_s: float = 300.0
    idle_window_s: float = 300.0
    recovery_attempt_limit: int = 4
    recovery_window_s: float = 900.0
    position_epsilon: float = 1.0
    max_body_state_sample_gap_s: float = 240.0


DEFAULT_THRESHOLDS = AutonomyQualityThresholds()

LOG_ITEMS = (
    "oak_log",
    "spruce_log",
    "birch_log",
    "jungle_log",
    "acacia_log",
    "dark_oak_log",
    "mangrove_log",
    "cherry_log",
    "pale_oak_log",
    "crimson_stem",
    "warped_stem",
)

PLANK_ITEMS = (
    "oak_planks",
    "spruce_planks",
    "birch_planks",
    "jungle_planks",
    "acacia_planks",
    "dark_oak_planks",
    "mangrove_planks",
    "cherry_planks",
    "pale_oak_planks",
    "crimson_planks",
    "warped_planks",
)


@dataclass(frozen=True)
class InventoryProgressFamily:
    key: str
    accepted_items: tuple[str, ...]
    minimum: int = 1
    distinct: bool = False
    score_unit: int = 1
    max_points: int | None = None


@dataclass(frozen=True)
class EquipmentProgressRequirement:
    key: str
    item: str
    slot: Literal["mainhand", "offhand"]
    points: int = 1


@dataclass(frozen=True)
class MaterialYardstick:
    goal_id: str
    inventory_families: tuple[InventoryProgressFamily, ...]
    equipment: tuple[EquipmentProgressRequirement, ...] = ()


AG_FP30_YARDSTICK = MaterialYardstick(
    goal_id="AG-FP30",
    inventory_families=(
        # Target-adjacent prerequisite chain.  These do not make the material
        # checklist a hard gate; they let the quality evaluator count genuine
        # server-authoritative progress such as wood -> tools -> stone -> fuel
        # while the final flowers/drops/iron/torch checklist remains only the
        # stronger output yardstick.
        InventoryProgressFamily("logs", LOG_ITEMS, minimum=3, max_points=3),
        InventoryProgressFamily("planks", PLANK_ITEMS, minimum=4, score_unit=4, max_points=2),
        InventoryProgressFamily("sticks", ("stick",), minimum=4, score_unit=4, max_points=2),
        InventoryProgressFamily("crafting_table", ("crafting_table",)),
        InventoryProgressFamily("wooden_pickaxe", ("wooden_pickaxe",)),
        InventoryProgressFamily("cobblestone", ("cobblestone",), minimum=3, max_points=3),
        InventoryProgressFamily("coal_or_charcoal", ("coal", "charcoal"), minimum=1, max_points=4),
        InventoryProgressFamily("raw_iron", ("raw_iron",), minimum=7, max_points=7),
        InventoryProgressFamily(
            "flowers",
            (
                "dandelion",
                "poppy",
                "blue_orchid",
                "allium",
                "azure_bluet",
                "red_tulip",
                "orange_tulip",
                "white_tulip",
                "pink_tulip",
                "oxeye_daisy",
                "cornflower",
                "lily_of_the_valley",
                "wither_rose",
                "sunflower",
                "lilac",
                "rose_bush",
                "peony",
                "torchflower",
                "pitcher_plant",
                "open_eyeblossom",
                "closed_eyeblossom",
            ),
            minimum=3,
            distinct=True,
        ),
        InventoryProgressFamily("pig_drop", ("porkchop",)),
        InventoryProgressFamily("cow_drop", ("beef", "leather")),
        InventoryProgressFamily("sheep_drop", ("mutton", "white_wool", "wool")),
        InventoryProgressFamily("torches", ("torch",), minimum=16, score_unit=8, max_points=2),
        InventoryProgressFamily("iron_ingots", ("iron_ingot",), minimum=3, max_points=3),
    ),
    equipment=(
        EquipmentProgressRequirement("stone_pickaxe_equipped", "stone_pickaxe", "mainhand"),
        EquipmentProgressRequirement("shield_equipped", "shield", "offhand"),
        EquipmentProgressRequirement("iron_pickaxe_equipped", "iron_pickaxe", "mainhand"),
    ),
)

AG_FP30_X_YARDSTICK = MaterialYardstick(
    goal_id="AG-FP30-X",
    inventory_families=(
        InventoryProgressFamily(
            "logs",
            LOG_ITEMS,
            minimum=3,
            max_points=3,
        ),
        InventoryProgressFamily("crafting_table", ("crafting_table",)),
        InventoryProgressFamily("cobblestone", ("cobblestone",), minimum=3, max_points=3),
        InventoryProgressFamily("iron_ingots", ("iron_ingot",), minimum=3, max_points=3),
    ),
    equipment=(EquipmentProgressRequirement("stone_pickaxe_equipped", "stone_pickaxe", "mainhand"),),
)


def evaluate_autonomy_quality(
    events: list[JsonObject],
    *,
    yardstick: MaterialYardstick = AG_FP30_YARDSTICK,
    thresholds: AutonomyQualityThresholds = DEFAULT_THRESHOLDS,
    active_window_s: float | None = None,
) -> JsonObject:
    ordered = sorted((dict(event) for event in events), key=lambda event: (float(event.get("ts", 0.0)), int(event.get("seq", 0))))
    start_ts, end_ts, active_s = _active_window(ordered, active_window_s=active_window_s)
    coverage = _coverage(ordered, start_ts=start_ts, end_ts=end_ts, thresholds=thresholds)
    output = _effective_output_signal(
        ordered,
        yardstick=yardstick,
        thresholds=thresholds,
        start_ts=start_ts,
        end_ts=end_ts,
        active_s=active_s,
    )
    health = _process_health_signal(
        ordered,
        thresholds=thresholds,
        start_ts=start_ts,
        end_ts=end_ts,
        output_events=output.get("output_events") if isinstance(output.get("output_events"), list) else [],
    )
    recovery = _recovery_signal(
        ordered,
        thresholds=thresholds,
        start_ts=start_ts,
        end_ts=end_ts,
        output_events=output.get("output_events") if isinstance(output.get("output_events"), list) else [],
    )
    verdict = _overall_verdict(coverage, output, health, recovery)
    return {
        "schema_version": AUTONOMY_QUALITY_SCHEMA_VERSION,
        "goal_id": yardstick.goal_id,
        "verdict": verdict,
        "active_window": {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "duration_s": round(active_s, 3),
        },
        "thresholds": asdict(thresholds),
        "coverage": coverage,
        "signals": {
            "effective_output": output,
            "process_health": health,
            "recovery": recovery,
        },
    }


def _active_window(events: list[JsonObject], *, active_window_s: float | None) -> tuple[float, float, float]:
    timestamps = [float(event["ts"]) for event in events if isinstance(event.get("ts"), int | float)]
    if not timestamps:
        return 0.0, float(active_window_s or 0.0), float(active_window_s or 0.0)
    ready = _first_ts(events, "scenario_body_ready")
    start = ready if ready is not None else timestamps[0]
    terminal = _last_ts(events, "session_terminal")
    if active_window_s is not None:
        return start, start + float(active_window_s), float(active_window_s)
    end = terminal if terminal is not None else timestamps[-1]
    return start, end, max(0.0, end - start)


def _coverage(
    events: list[JsonObject],
    *,
    start_ts: float,
    end_ts: float,
    thresholds: AutonomyQualityThresholds,
) -> JsonObject:
    missing: list[str] = []
    states = [
        event
        for event in events
        if event.get("event") == "body_state"
        and _in_window(event, start_ts=start_ts, end_ts=end_ts)
        and event.get("missing") is not True
    ]
    if len(states) < 2:
        missing.append("body_state_time_series")
    required_state_fields = ("inventory_counts", "selected_item", "offhand_item", "body_owner", "pending_action_count")
    for state in states:
        for field in required_state_fields:
            if field not in state:
                missing.append(f"body_state.{field}")
                break
        if not isinstance(state.get("inventory_counts"), dict):
            missing.append("body_state.inventory_counts_dict")
    if states:
        sample_times = [start_ts, *(float(state["ts"]) for state in states), end_ts]
        max_gap = max(b - a for a, b in zip(sample_times, sample_times[1:], strict=False))
        if max_gap > thresholds.max_body_state_sample_gap_s:
            missing.append("body_state_sample_gap")
    else:
        max_gap = None

    body_events = [
        event
        for event in events
        if event.get("event") == "body_events" and _in_window(event, start_ts=start_ts, end_ts=end_ts)
    ]
    if not body_events:
        missing.append("body_events_time_series")
    for event in body_events:
        if not isinstance(event.get("events"), list):
            missing.append("body_events.payload")
            break
        for nested in event["events"]:
            if (
                not isinstance(nested, dict)
                or _int(nested.get("seq")) <= 0
                or not isinstance(nested.get("name"), str)
                or not isinstance(nested.get("data"), dict)
            ):
                missing.append("body_events.event_payload")
                break
        if "body_events.event_payload" in missing:
            break

    for event in events:
        if event.get("event") in {"tool_invoke", "tool_result"} and _in_window(event, start_ts=start_ts, end_ts=end_ts):
            if not isinstance(event.get("args_hash"), str) or not event.get("args_hash"):
                missing.append(f"{event.get('event')}.args_hash")
            if not isinstance(event.get("tactic_signature"), str) or not event.get("tactic_signature"):
                missing.append(f"{event.get('event')}.tactic_signature")

    unique_missing = sorted(set(missing))
    return {
        "verdict": "pass" if not unique_missing else "insufficient_evidence",
        "missing": unique_missing,
        "body_state_samples": len(states),
        "body_events_samples": len(body_events),
        "max_body_state_gap_s": None if max_gap is None else round(float(max_gap), 3),
        "evidence_refs": [_ref(state) for state in states[:3]],
    }


def _effective_output_signal(
    events: list[JsonObject],
    *,
    yardstick: MaterialYardstick,
    thresholds: AutonomyQualityThresholds,
    start_ts: float,
    end_ts: float,
    active_s: float,
) -> JsonObject:
    states = [
        event
        for event in events
        if event.get("event") == "body_state"
        and _in_window(event, start_ts=start_ts, end_ts=end_ts)
        and event.get("missing") is not True
        and isinstance(event.get("inventory_counts"), dict)
    ]
    highwater: dict[str, int] = {}
    event_totals: dict[str, int] = {}
    equipped: set[tuple[str, str]] = set()
    previous_score = 0
    output_events: list[JsonObject] = []
    authoritative_events = _authoritative_progress_events(
        events,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    final_detail: JsonObject = {"total_points": 0, "families": {}, "equipment": {}}
    observations: list[tuple[float, int, str, JsonObject]] = [
        (float(state["ts"]), _int(state.get("seq")), "body_state", state)
        for state in states
    ]
    observations.extend(
        (float(item["ts"]), _int(item.get("seq")), "body_event", item)
        for item in authoritative_events
    )
    observations.sort(key=lambda item: (item[0], item[1]))
    for timestamp, _sequence, kind, observation in observations:
        if kind == "body_state":
            for item, count in dict(observation.get("inventory_counts") or {}).items():
                clean_item = _item_name(item)
                clean_count = _int(count)
                if clean_item and clean_count > highwater.get(clean_item, 0):
                    highwater[clean_item] = clean_count
            selected = _item_name(observation.get("selected_item"))
            offhand = _item_name(observation.get("offhand_item"))
            if selected:
                equipped.add(("mainhand", selected))
            if offhand:
                equipped.add(("offhand", offhand))
        else:
            item = _item_name(observation.get("item"))
            count = _int(observation.get("count"))
            if item and count > 0:
                # Body events are authoritative deltas, but a later state
                # sample may contain the same inventory change.  Merge them
                # through an item high-water mark and cap at the yardstick's
                # family limits, so duplicate trace observations cannot mint
                # unbounded output.
                event_totals[item] = event_totals.get(item, 0) + count
                highwater[item] = max(highwater.get(item, 0), event_totals[item])
        score, detail = _yardstick_score(highwater, equipped, yardstick)
        final_detail = detail
        if score > previous_score:
            output_events.append(
                {
                    "ts": timestamp,
                    "seq": observation.get("seq"),
                    "delta_points": score - previous_score,
                    "total_points": score,
                    "evidence_ref": _ref(observation),
                }
            )
            previous_score = score

    required_points = max(1, math.ceil(thresholds.minimum_output_points_per_1800s * max(active_s, 1.0) / 1800.0))
    timestamps = [float(event["ts"]) for event in output_events]
    gaps = _gaps(start_ts, end_ts, timestamps)
    max_gap = max(gaps) if gaps else max(0.0, end_ts - start_ts)
    failures: list[str] = []
    if previous_score < required_points:
        failures.append("output_points_below_threshold")
    if max_gap > thresholds.max_no_output_window_s:
        failures.append("no_output_window_exceeded")
    return {
        "verdict": "pass" if not failures else "fail",
        "points": previous_score,
        "required_points": required_points,
        "max_no_output_window_s": round(float(max_gap), 3),
        "failures": failures,
        "yardstick": final_detail,
        "output_events": output_events,
        "authoritative_progress_events": len(authoritative_events),
        "authoritative_progress_refs": [_ref(event) for event in authoritative_events[:16]],
        "evidence_refs": [event["evidence_ref"] for event in output_events[:8]],
    }


def _authoritative_progress_events(
    events: list[JsonObject],
    *,
    start_ts: float,
    end_ts: float,
) -> list[JsonObject]:
    """Flatten positive inventory-producing Body terminals into a deduped ledger.

    A Body event is only useful here when the server emitted a successful
    terminal with an item/count delta.  Movement, reads, and mutation success
    without an inventory result are intentionally excluded.  Event sequence
    numbers are authoritative and may appear in several non-consuming trace
    observations, so they are the deduplication key.
    """

    progress: list[JsonObject] = []
    seen: set[tuple[int, str, str]] = set()
    for sample in events:
        if sample.get("event") != "body_events" or not _in_window(sample, start_ts=start_ts, end_ts=end_ts):
            continue
        for nested in sample.get("events") or ():
            if not isinstance(nested, dict):
                continue
            name = str(nested.get("name") or "")
            data = nested.get("data")
            if not isinstance(data, dict):
                continue
            sequence = _int(nested.get("seq"))
            action_id = str(data.get("action_id") or "")
            identity = (sequence, name, action_id)
            if sequence > 0 and identity in seen:
                continue
            if sequence > 0:
                seen.add(identity)
            item: object = None
            count = 0
            if name == "itemPickup":
                item = data.get("item")
                count = _int(data.get("count"))
            elif name == "craftDone" and data.get("success") is True:
                item = data.get("item")
                count = _int(data.get("count"))
            elif (
                name in {"furnaceDone", "containerDone"}
                and data.get("success") is True
                and str(data.get("direction") or "") in {"furnace_to_bot", "container_to_bot"}
            ):
                item = data.get("item")
                count = _int(data.get("count"))
            if count <= 0 or _item_name(item) is None:
                continue
            progress.append(
                {
                    "event": name,
                    "seq": sequence if sequence > 0 else sample.get("seq"),
                    "ts": sample.get("ts"),
                    "item": item,
                    "count": count,
                    "action_id": action_id or None,
                }
            )
    return sorted(progress, key=lambda event: (float(event.get("ts", 0.0)), _int(event.get("seq"))))


def _process_health_signal(
    events: list[JsonObject],
    *,
    thresholds: AutonomyQualityThresholds,
    start_ts: float,
    end_ts: float,
    output_events: list[Any],
) -> JsonObject:
    repeated = _repeated_failure(events, thresholds=thresholds, start_ts=start_ts, end_ts=end_ts)
    deadlock = _deadlock_window(
        events,
        thresholds=thresholds,
        start_ts=start_ts,
        end_ts=end_ts,
        output_ts=[float(event["ts"]) for event in output_events if isinstance(event, dict) and isinstance(event.get("ts"), int | float)],
    )
    idle = _idle_window(events, thresholds=thresholds, start_ts=start_ts, end_ts=end_ts)
    failures = []
    if repeated is not None:
        failures.append("repeated_failure_loop")
    if deadlock is not None:
        failures.append("deadlock_window")
    if idle is not None:
        failures.append("idle_window")
    return {
        "verdict": "pass" if not failures else "fail",
        "failures": failures,
        "repeated_failure": repeated,
        "deadlock_window": deadlock,
        "idle_window": idle,
    }


def _recovery_signal(
    events: list[JsonObject],
    *,
    thresholds: AutonomyQualityThresholds,
    start_ts: float,
    end_ts: float,
    output_events: list[Any],
) -> JsonObject:
    output_ts = [
        float(event["ts"])
        for event in output_events
        if isinstance(event, dict) and isinstance(event.get("ts"), int | float)
    ]
    invokes = [
        event
        for event in events
        if event.get("event") == "tool_invoke" and _in_window(event, start_ts=start_ts, end_ts=end_ts)
    ]
    results = [
        event
        for event in events
        if event.get("event") == "tool_result" and _in_window(event, start_ts=start_ts, end_ts=end_ts)
    ]
    obstacles = [
        event
        for event in results
        if event.get("success") is False
        and _is_natural_obstacle(event.get("reason"), tool=event.get("tool"))
    ]
    if not obstacles:
        return {
            "verdict": "insufficient_evidence",
            "reason": "no_natural_obstacle_episode",
            "episodes": [],
        }

    episodes: list[JsonObject] = []
    incomplete = 0
    failed = 0
    passed = 0
    for obstacle in obstacles:
        obstacle_ts = float(obstacle.get("ts", 0.0))
        if obstacle_ts + thresholds.recovery_window_s > end_ts:
            incomplete += 1
            episodes.append(
                {
                    "verdict": "insufficient_evidence",
                    "reason": "obstacle_too_late_for_recovery_window",
                    "obstacle": _episode_obstacle(obstacle),
                }
            )
            continue
        obstacle_tactic = str(obstacle.get("tactic_signature") or "")
        switched = next(
            (
                event
                for event in invokes
                if float(event.get("ts", 0.0)) > obstacle_ts
                and float(event.get("ts", 0.0)) <= obstacle_ts + thresholds.recovery_window_s
                and event.get("mutating") is True
                and str(event.get("tactic_signature") or "") != obstacle_tactic
            ),
            None,
        )
        if switched is None:
            failed += 1
            episodes.append(
                {
                    "verdict": "fail",
                    "reason": "no_semantic_switch",
                    "obstacle": _episode_obstacle(obstacle),
                }
            )
            continue
        switch_ts = float(switched.get("ts", 0.0))
        recovered_at = next((ts for ts in output_ts if switch_ts <= ts <= obstacle_ts + thresholds.recovery_window_s), None)
        attempts = sum(
            1
            for result in results
            if obstacle_ts < float(result.get("ts", 0.0)) <= (recovered_at or obstacle_ts + thresholds.recovery_window_s)
        )
        if recovered_at is None:
            failed += 1
            episodes.append(
                {
                    "verdict": "fail",
                    "reason": "no_authoritative_output_after_switch",
                    "obstacle": _episode_obstacle(obstacle),
                    "switch": _episode_switch(switched),
                    "attempts": attempts,
                }
            )
            continue
        if attempts > thresholds.recovery_attempt_limit:
            failed += 1
            episodes.append(
                {
                    "verdict": "fail",
                    "reason": "recovery_attempt_limit_exceeded",
                    "obstacle": _episode_obstacle(obstacle),
                    "switch": _episode_switch(switched),
                    "attempts": attempts,
                    "recovered_at": recovered_at,
                }
            )
            continue
        passed += 1
        episodes.append(
            {
                "verdict": "pass",
                "obstacle": _episode_obstacle(obstacle),
                "switch": _episode_switch(switched),
                "attempts": attempts,
                "recovered_at": recovered_at,
            }
        )
    verdict: Verdict
    if failed:
        verdict = "fail"
    elif passed:
        verdict = "pass"
    else:
        verdict = "insufficient_evidence"
    return {
        "verdict": verdict,
        "episode_counts": {"pass": passed, "fail": failed, "insufficient_evidence": incomplete},
        "episodes": episodes,
    }


def _overall_verdict(coverage: JsonObject, *signals: JsonObject) -> Verdict:
    if coverage.get("verdict") == "insufficient_evidence":
        return "insufficient_evidence"
    if any(signal.get("verdict") == "fail" for signal in signals):
        return "fail"
    if any(signal.get("verdict") == "insufficient_evidence" for signal in signals):
        return "insufficient_evidence"
    return "pass"


def _yardstick_score(
    counts: dict[str, int],
    equipped: set[tuple[str, str]],
    yardstick: MaterialYardstick,
) -> tuple[int, JsonObject]:
    total = 0
    families: dict[str, object] = {}
    for family in yardstick.inventory_families:
        accepted = tuple(_item_name(item) or item for item in family.accepted_items)
        if family.distinct:
            observed = sorted(item for item in accepted if counts.get(item, 0) > 0)
            points = min(len(observed), family.max_points or family.minimum)
            families[family.key] = {
                "observed": observed,
                "minimum": family.minimum,
                "points": points,
            }
        else:
            observed_count = sum(counts.get(item, 0) for item in accepted)
            point_cap = family.max_points or max(1, math.ceil(family.minimum / max(family.score_unit, 1)))
            points = min(point_cap, observed_count // max(family.score_unit, 1))
            families[family.key] = {
                "observed_count": observed_count,
                "minimum": family.minimum,
                "score_unit": family.score_unit,
                "points": points,
            }
        total += points
    equipment: dict[str, object] = {}
    for requirement in yardstick.equipment:
        item = _item_name(requirement.item) or requirement.item
        satisfied = (requirement.slot, item) in equipped
        points = requirement.points if satisfied else 0
        equipment[requirement.key] = {
            "item": item,
            "slot": requirement.slot,
            "satisfied": satisfied,
            "points": points,
        }
        total += points
    return total, {"total_points": total, "families": families, "equipment": equipment}


def _repeated_failure(
    events: list[JsonObject],
    *,
    thresholds: AutonomyQualityThresholds,
    start_ts: float,
    end_ts: float,
) -> JsonObject | None:
    current: tuple[str, str, str] | None = None
    streak = 0
    refs: list[str] = []
    for event in events:
        if event.get("event") != "tool_result" or not _in_window(event, start_ts=start_ts, end_ts=end_ts):
            continue
        if event.get("success") is not False:
            continue
        signature = (
            str(event.get("tool") or ""),
            str(event.get("args_hash") or ""),
            _reason_family(event.get("reason")),
        )
        if signature == current:
            streak += 1
            refs.append(_ref(event))
        else:
            current = signature
            streak = 1
            refs = [_ref(event)]
        if streak > thresholds.max_repeated_failure_streak:
            return {
                "signature": {
                    "tool": signature[0],
                    "args_hash": signature[1],
                    "reason_family": signature[2],
                },
                "streak": streak,
                "threshold": thresholds.max_repeated_failure_streak,
                "evidence_refs": refs,
            }
    return None


def _deadlock_window(
    events: list[JsonObject],
    *,
    thresholds: AutonomyQualityThresholds,
    start_ts: float,
    end_ts: float,
    output_ts: list[float],
) -> JsonObject | None:
    states = [
        event
        for event in events
        if event.get("event") == "body_state"
        and _in_window(event, start_ts=start_ts, end_ts=end_ts)
        and event.get("missing") is not True
        and _pos(event) is not None
    ]
    for start_index, start_state in enumerate(states):
        window: list[JsonObject] = []
        for end_state in states[start_index:]:
            duration = float(end_state.get("ts", 0.0)) - float(start_state.get("ts", 0.0))
            window.append(end_state)
            if duration < thresholds.deadlock_window_s:
                continue
            if any(float(start_state.get("ts", 0.0)) < ts <= float(end_state.get("ts", 0.0)) for ts in output_ts):
                break
            span = _position_span(window)
            if span <= thresholds.position_epsilon:
                return {
                    "start_ts": float(start_state.get("ts", 0.0)),
                    "end_ts": float(end_state.get("ts", 0.0)),
                    "duration_s": round(duration, 3),
                    "position_span": round(span, 3),
                    "threshold_s": thresholds.deadlock_window_s,
                    "epsilon": thresholds.position_epsilon,
                    "evidence_refs": [_ref(start_state), _ref(end_state)],
                }
            break
    return None


def _idle_window(
    events: list[JsonObject],
    *,
    thresholds: AutonomyQualityThresholds,
    start_ts: float,
    end_ts: float,
) -> JsonObject | None:
    activity = [start_ts]
    for event in events:
        if not _in_window(event, start_ts=start_ts, end_ts=end_ts):
            continue
        name = str(event.get("event") or "")
        ts = float(event.get("ts", 0.0))
        if name in {"llm_start", "model_request", "tool_invoke", "tool_result", "tool_start", "tool_end", "body_event_wake"}:
            activity.append(ts)
            continue
        if name == "body_state" and (event.get("body_owner") is not None or _int(event.get("pending_action_count")) > 0):
            activity.append(ts)
            continue
        if name == "body_events" and _has_material_body_event(event):
            activity.append(ts)
    activity.append(end_ts)
    activity = sorted(set(activity))
    for before, after in zip(activity, activity[1:], strict=False):
        gap = after - before
        if gap > thresholds.idle_window_s:
            return {
                "start_ts": round(before, 3),
                "end_ts": round(after, 3),
                "duration_s": round(gap, 3),
                "threshold_s": thresholds.idle_window_s,
            }
    return None


def _has_material_body_event(event: JsonObject) -> bool:
    for item in event.get("events") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name.endswith("Done") or name in {"itemPickup", "death", "respawned", "underAttack"}:
            return True
    return False


def _is_natural_obstacle(reason: object, *, tool: object = None) -> bool:
    text = str(reason or "").casefold()
    if not text:
        return False
    tool_name = str(tool or "").casefold()
    # These are agent/runtime contract failures, not world episodes.  Counting
    # them would let a failed skill lookup or local planning call manufacture
    # recovery evidence without the Body ever encountering an obstacle.
    if tool_name in {"load_skill", "update_plan", "read_state", "read_inventory", "read_block", "read_recipe"}:
        return False
    if any(
        text == marker or text.startswith(f"{marker}:")
        for marker in (
            "skill_not_found",
            "task_plan_update_rejected",
            "missing_required_tool",
            "tool_runtime_error",
            "invalid_tool",
            "invalid_input",
            "body_batch_conflict",
            "body_rejected",
            "transport_error",
        )
    ):
        return False
    if any(
        marker in text
        for marker in (
            "no_path",
            "not_found",
            "unavailable",
            "unreachable",
            "blocked",
            "denied",
            "exhausted",
            "budget",
            "death",
            "drown",
            "hazard",
            "lava",
            "water",
            "under_attack",
            "structure_risk",
        )
    ):
        return not any(marker in text for marker in ("invalid_tool", "invalid_input", "body_batch_conflict"))
    return False


def _episode_obstacle(event: JsonObject) -> JsonObject:
    return {
        "tool": event.get("tool"),
        "reason": event.get("reason"),
        "args_hash": event.get("args_hash"),
        "tactic_signature": event.get("tactic_signature"),
        "ts": event.get("ts"),
        "evidence_ref": _ref(event),
    }


def _episode_switch(event: JsonObject) -> JsonObject:
    return {
        "tool": event.get("tool"),
        "args_hash": event.get("args_hash"),
        "tactic_signature": event.get("tactic_signature"),
        "ts": event.get("ts"),
        "evidence_ref": _ref(event),
    }


def _reason_family(reason: object) -> str:
    text = str(reason or "unknown").casefold()
    for separator in (":", "|", "/"):
        if separator in text:
            return text.split(separator, 1)[0]
    return text


def _gaps(start_ts: float, end_ts: float, timestamps: list[float]) -> list[float]:
    points = [start_ts, *sorted(ts for ts in timestamps if start_ts <= ts <= end_ts), end_ts]
    return [after - before for before, after in zip(points, points[1:], strict=False)]


def _position_span(states: list[JsonObject]) -> float:
    positions = [_pos(state) for state in states]
    clean = [pos for pos in positions if pos is not None]
    if len(clean) < 2:
        return 0.0
    xs = [pos[0] for pos in clean]
    ys = [pos[1] for pos in clean]
    zs = [pos[2] for pos in clean]
    return math.sqrt((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2 + (max(zs) - min(zs)) ** 2)


def _pos(event: JsonObject) -> tuple[float, float, float] | None:
    pos = event.get("pos")
    if not isinstance(pos, list | tuple) or len(pos) != 3:
        return None
    try:
        return (float(pos[0]), float(pos[1]), float(pos[2]))
    except (TypeError, ValueError):
        return None


def _first_ts(events: list[JsonObject], name: str) -> float | None:
    for event in events:
        if event.get("event") == name and isinstance(event.get("ts"), int | float):
            return float(event["ts"])
    return None


def _last_ts(events: list[JsonObject], name: str) -> float | None:
    for event in reversed(events):
        if event.get("event") == name and isinstance(event.get("ts"), int | float):
            return float(event["ts"])
    return None


def _in_window(event: JsonObject, *, start_ts: float, end_ts: float) -> bool:
    ts = event.get("ts")
    return isinstance(ts, int | float) and start_ts <= float(ts) <= end_ts


def _item_name(value: object) -> str | None:
    if value is None:
        return None
    return str(value).removeprefix("minecraft:").casefold()


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _ref(event: JsonObject) -> str:
    if event.get("seq") is not None:
        return f"seq:{event['seq']}"
    return f"ts:{event.get('ts')}"


__all__ = [
    "AG_FP30_X_YARDSTICK",
    "AG_FP30_YARDSTICK",
    "AUTONOMY_QUALITY_SCHEMA_VERSION",
    "AutonomyQualityThresholds",
    "DEFAULT_THRESHOLDS",
    "EquipmentProgressRequirement",
    "InventoryProgressFamily",
    "MaterialYardstick",
    "evaluate_autonomy_quality",
]
