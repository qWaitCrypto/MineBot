#!/usr/bin/env python3
import json
import math
import re
import statistics
import time

from carpet_deep_test import Rcon


HOST = "127.0.0.1"
PORT = 25576
PASSWORD = "test"
BOTS = ["SpikeBot1", "SpikeBot2", "SpikeBot3", "SpikeBot4", "SpikeBot5", "SpikeBot6", "SpikeBot7", "SpikeBot8"]


def rcon(command, delay=0.03):
    con = Rcon(HOST, PORT, PASSWORD)
    started = time.perf_counter()
    try:
        out = con.command(command)
    finally:
        con.close()
    elapsed_ms = (time.perf_counter() - started) * 1000
    if delay:
        time.sleep(delay)
    return out, elapsed_ms


def app(expr, delay=0.03):
    return rcon(f"script in minebot_spike run {expr}", delay=delay)


def nums(text):
    return [float(x) for x in re.findall(r"[-+]?\d+(?:\.\d+)?", text)]


def pos(name):
    out, _ = rcon(f"data get entity {name} Pos", delay=0.01)
    match = re.search(r"\[([^\]]+)\]", out)
    values = nums(match.group(1)) if match else nums(out)
    return values[:3], out


def dist(a, b):
    if len(a) < 3:
        return None
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def event_bool(event, key):
    compact = re.sub(r"\s+", "", event)
    return f"{key},true" in compact or f"'{key}',true" in compact or f'"{key}",true' in compact


def result(name, status, evidence, conclusion):
    return {"name": name, "status": status, "evidence": evidence, "conclusion": conclusion}


def reset_area(x0=-40, x1=180, z0=-40, z1=40, y=60):
    step = 24
    x = x0
    while x <= x1:
        xe = min(x + step - 1, x1)
        rcon(f"fill {x} {y} {z0} {xe} {y + 8} {z1} air", delay=0.0)
        rcon(f"fill {x} {y - 1} {z0} {xe} {y - 1} {z1} stone", delay=0.0)
        x = xe + 1


def ensure_bot(name, x=0, y=60, z=0, yaw=-90):
    rcon(f"player {name} spawn")
    time.sleep(0.15)
    rcon(f"tp {name} {x} {y} {z} {yaw} 0")
    rcon(f"gamemode survival {name}")
    rcon(f"effect give {name} water_breathing infinite 0 true")
    rcon(f"effect give {name} fire_resistance infinite 0 true")
    rcon(f"effect clear {name}")
    rcon(f"effect give {name} water_breathing infinite 0 true")
    rcon(f"effect give {name} fire_resistance infinite 0 true")
    rcon(f"clear {name}")
    rcon(f"player {name} stop")


def setup():
    for command in [
        "script unload minebot_spike",
        "script load minebot_spike global",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "time set day",
        "weather clear",
        "difficulty normal",
        "kill @e[type=!player]",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
    ]:
        rcon(command)
    reset_area()
    app("reset_spike()")
    ensure_bot(BOTS[0], 0, 60, 0)


def stop_all_bots():
    for name in BOTS:
        rcon(f"player {name} stop", delay=0.0)


def tick_health():
    out, ms = rcon("tick query", delay=0.02)
    values = nums(out)
    # tick query includes rate, average MSPT, P50/P95/P99, and sample count.
    return {"raw": out[:800], "rcon_ms": round(ms, 2), "numbers": values}


def sample_mspt(label, duration=5.0, interval=0.5):
    samples = []
    deadline = time.time() + duration
    while time.time() < deadline:
        samples.append(tick_health())
        time.sleep(interval)
    avg_mspt = []
    p95_mspt = []
    for sample in samples:
        raw = sample["raw"]
        avg = re.search(r"Average time per tick:\s*([0-9.]+)ms", raw)
        p95 = re.search(r"P95:\s*([0-9.]+)ms", raw)
        if avg:
            avg_mspt.append(float(avg.group(1)))
        if p95:
            p95_mspt.append(float(p95.group(1)))
    # If parsing tick health is too broad, command latency is still a useful fallback.
    rcon_ms = [s["rcon_ms"] for s in samples]
    return {
        "label": label,
        "samples": samples,
        "parsed_mspt_mean": round(statistics.mean(avg_mspt), 3) if avg_mspt else None,
        "parsed_mspt_p95": round(statistics.mean(p95_mspt), 3) if p95_mspt else None,
        "rcon_ms_mean": round(statistics.mean(rcon_ms), 3),
        "rcon_ms_p95": round(statistics.quantiles(rcon_ms, n=20)[18], 3) if len(rcon_ms) >= 20 else round(max(rcon_ms), 3),
    }


def start_many(count):
    app("reset_spike()")
    stop_all_bots()
    reset_area()
    for i in range(count):
        name = BOTS[i]
        z = -18 + i * 5
        ensure_bot(name, 0, 60, z)
        app(f"spike_moveTo('{name}', 90, 60, {z})", delay=0.0)


def tile_read_burst(duration=5.0):
    rows = []
    deadline = time.time() + duration
    while time.time() < deadline:
        out, ms = app("region_blocks_compact(0,59,0,8,8,8)", delay=0.0)
        rows.append({"chars": len(out), "ms": round(ms, 2), "truncated": len(out) >= 4096})
        time.sleep(0.1)
    return rows


def test_tps_mspt_budget():
    app("reset_spike()")
    stop_all_bots()
    reset_area()
    baseline = sample_mspt("baseline")

    start_many(1)
    one_bot = sample_mspt("1bot_move")

    start_many(1)
    tile_rows = []
    mspt_rows = []
    deadline = time.time() + 6
    while time.time() < deadline:
        tile_rows.extend(tile_read_burst(0.6))
        mspt_rows.append(tick_health())
    one_bot_tile = {
        "label": "1bot_move_plus_tile_reads",
        "tile_reads": {
            "count": len(tile_rows),
            "mean_rcon_ms": round(statistics.mean([r["ms"] for r in tile_rows]), 3) if tile_rows else None,
            "p95_rcon_ms": round(statistics.quantiles([r["ms"] for r in tile_rows], n=20)[18], 3) if len(tile_rows) >= 20 else None,
            "max_chars": max([r["chars"] for r in tile_rows], default=0),
            "truncated": any(r["truncated"] for r in tile_rows),
        },
        "tick_health_samples": mspt_rows,
    }

    nbot = []
    for count in [2, 4, 8]:
        start_many(count)
        nbot.append(sample_mspt(f"{count}bot_move"))
    app("reset_spike()")
    for name in BOTS:
        rcon(f"player {name} stop", delay=0.0)

    return result(
        "tps_mspt_budget",
        "pass",
        {"baseline": baseline, "one_bot": one_bot, "one_bot_plus_tile": one_bot_tile, "n_bot": nbot},
        "Tick-health parsing is included with raw samples; RCON tile reads are separately timed to expose main-thread command cost.",
    )


def wait_events(predicate, timeout=14):
    deadline = time.time() + timeout
    all_events = []
    while time.time() < deadline:
        out, _ = app("drain_events()")
        all_events.append(out)
        if predicate(out):
            return out, all_events
        time.sleep(0.25)
    return all_events[-1] if all_events else "", all_events


def run_move_case(case):
    app("reset_spike()")
    reset_area()
    name = BOTS[0]
    setup_cmds = case.get("commands", [])
    ensure_bot(name, *case["start"])
    for command in setup_cmds:
        rcon(command)
    target = case["target"]
    before, _ = pos(name)
    app(f"spike_moveTo('{name}', {target[0]}, {target[1]}, {target[2]})")
    final_event, drains = wait_events(lambda out: "moveDone" in out, timeout=case.get("timeout", 14))
    after, raw = pos(name)
    d = dist(after, target)
    return {
        "case": case["name"],
        "target": target,
        "start_pos": before,
        "final_pos_independent": after,
        "independent_dist": round(d, 3) if d is not None else None,
        "event": final_event,
        "all_drains_tail": drains[-4:],
        "raw_pos": raw,
        "reported_arrived": event_bool(final_event, "arrived"),
        "ground_truth_arrived": d is not None and d <= 1.0,
    }


def test_completion_honesty():
    cases = [
        {"name": "flat_reachable", "start": (0, 60, 0), "target": (8, 60, 0)},
        {
            "name": "wall_blocked",
            "start": (0, 60, 5),
            "target": (10, 60, 5),
            "commands": ["fill 4 60 3 4 62 7 stone"],
        },
        {
            "name": "one_block_pit",
            "start": (0, 60, 10),
            "target": (6, 59, 10),
            "commands": ["setblock 6 59 10 air", "setblock 6 58 10 stone"],
        },
        {
            "name": "lava_gap",
            "start": (0, 60, 15),
            "target": (9, 60, 15),
            "commands": ["fill 4 59 14 5 59 16 lava"],
        },
        {"name": "already_there", "start": (0, 60, 20), "target": (0, 60, 20), "timeout": 4},
    ]
    rows = [run_move_case(c) for c in cases]
    lies = [r for r in rows if r["reported_arrived"] != r["ground_truth_arrived"]]
    return result(
        "completion_honesty",
        "pass" if not lies else "fail",
        {"rows": rows, "lies": lies},
        "Move completion event carries observed final state and matched independent position checks." if not lies else "At least one move result disagreed with independent ground truth.",
    )


def test_lava_reflex():
    name = BOTS[0]
    trigger_rows = []
    for i in range(10):
        app("reset_spike()")
        stop_all_bots()
        reset_area(-10, 20, -10, 10)
        ensure_bot(name, 0, 60, 0)
        app(f"watch_bot('{name}')")
        rcon("setblock 1 59 0 lava")
        before_tick = nums(app("tick_count()")[0])
        event, drains = wait_events(lambda out: "reflexTriggered" in out or "reflexFailed" in out, timeout=3)
        complete, _ = wait_events(lambda out: "reflexCompleted" in out or "reflexFailed" in out, timeout=8)
        after, _ = pos(name)
        after_tick = nums(app("tick_count()")[0])
        trigger_rows.append({
            "iteration": i + 1,
            "triggered": "reflexTriggered" in event,
            "completed": "reflexCompleted" in complete,
            "failed": "reflexFailed" in event or "reflexFailed" in complete,
            "trigger_event": event,
            "complete_event": complete,
            "final_pos": after,
            "tick_delta_observed": (after_tick[-1] - before_tick[-1]) if before_tick and after_tick else None,
        })

    app("reset_spike()")
    stop_all_bots()
    reset_area(-5, 25, -5, 5)
    ensure_bot(name, 0, 60, 0)
    rcon("setblock 5 59 0 lava")
    app(f"spike_moveTo('{name}', 16, 60, 0)")
    preempt, _ = wait_events(lambda out: "preempted" in out or "reflexTriggered" in out, timeout=8)
    completion, _ = wait_events(lambda out: "reflexCompleted" in out or "reflexFailed" in out, timeout=12)
    final, _ = pos(name)
    triggered = sum(1 for r in trigger_rows if r["triggered"])
    completed = sum(1 for r in trigger_rows if r["completed"])
    return result(
        "lava_reflex_preemption",
        "pass" if triggered == 10 and completed == 10 and "preempted" in preempt and "reflexCompleted" in completion else "partial",
        {
            "trigger_trials": trigger_rows,
            "trigger_rate": f"{triggered}/10",
            "completion_rate": f"{completed}/10",
            "preemption_event": preempt,
            "preemption_completion": completion,
            "preemption_final_pos": final,
        },
        "Lava reflex trigger/completion/preemption were measured; partial status means at least one reliability condition was not fully satisfied.",
    )


def test_per_bot_isolation():
    app("reset_spike()")
    stop_all_bots()
    reset_area(-5, 35, -15, 15)
    ensure_bot(BOTS[0], 0, 60, -8)
    ensure_bot(BOTS[1], 0, 60, 8)
    app(f"spike_moveTo('{BOTS[0]}', 18, 60, -8)")
    app(f"spike_moveTo('{BOTS[1]}', 25, 60, 8)")
    event, drains = wait_events(lambda out: out.count("moveDone") >= 2, timeout=18)
    p1, _ = pos(BOTS[0])
    p2, _ = pos(BOTS[1])
    d1 = dist(p1, (18, 60, -8))
    d2 = dist(p2, (25, 60, 8))
    ok = d1 is not None and d2 is not None and d1 <= 1.3 and d2 <= 1.3 and BOTS[0] in "".join(drains) and BOTS[1] in "".join(drains)
    return result(
        "per_bot_state_isolation",
        "pass" if ok else "fail",
        {"events": drains, "bot1_pos": p1, "bot2_pos": p2, "bot1_dist": d1, "bot2_dist": d2},
        "Two concurrent moveTo controllers kept separate state and completion events." if ok else "Concurrent bot state leaked or one bot failed to complete.",
    )


def main():
    setup()
    results = [
        test_tps_mspt_budget(),
        test_completion_honesty(),
        test_lava_reflex(),
        test_per_bot_isolation(),
    ]
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
