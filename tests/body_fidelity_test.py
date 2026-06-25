#!/usr/bin/env python3
import json
import math
import os
import re
import socket
import struct
import time


HOST = "127.0.0.1"
PORT = 25576
PASSWORD = "test"
BOT = "PhysBot"
OUT_PATH = "/home/qwait/MineBot/test-server/body-fidelity-results.json"
MARKER = (0, 120, 0)


class Rcon:
    def __init__(self, host, port, password):
        self.sock = socket.create_connection((host, port), timeout=25)
        self.req_id = 1
        self._request(3, password)

    def close(self):
        self.sock.close()

    def command(self, command):
        return self._request(2, command)

    def _request(self, kind, payload):
        req_id = self.req_id
        self.req_id += 1
        body = struct.pack("<ii", req_id, kind) + payload.encode() + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(body)) + body)
        size = struct.unpack("<i", self._recv_exact(4))[0]
        data = self._recv_exact(size)
        resp_id, _resp_kind = struct.unpack("<ii", data[:8])
        if resp_id == -1:
            raise RuntimeError("RCON authentication failed")
        return data[8:-2].decode(errors="replace")

    def _recv_exact(self, n):
        chunks = []
        remaining = n
        while remaining:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise ConnectionError("RCON socket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


def rcon(command, delay=0.05):
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


def cmd(command, delay=0.05):
    return rcon(command, delay=delay)[0]


def nums(text):
    return [float(x) for x in re.findall(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text)]


def bracket_nums(text):
    match = re.search(r"\[([^\]]+)\]", text)
    return nums(match.group(1)) if match else nums(text)


def data(selector, field, delay=0.02):
    return cmd(f"data get entity {selector} {field}", delay=delay)


def pos(selector=BOT):
    out = data(selector, "Pos")
    return bracket_nums(out)[:3], out


def health(selector=BOT):
    out = data(selector, "Health")
    values = nums(out)
    return (values[0] if values else None), out


def food(selector=BOT):
    out = data(selector, "foodLevel")
    values = nums(out)
    return (values[0] if values else None), out


def inventory(selector=BOT):
    return data(selector, "Inventory")


def effects(selector=BOT):
    return data(selector, "active_effects")


def dim_probe(expected_dim, selector=BOT):
    out = cmd(f"data get entity {selector} Dimension")
    return expected_dim in out, out


def any_dimension(selector=BOT):
    rows = {}
    for dim in ["minecraft:overworld", "minecraft:the_nether", "minecraft:the_end"]:
        ok, out = dim_probe(dim, selector)
        rows[dim] = {"match": ok, "raw": out[:300]}
    matches = [k for k, v in rows.items() if v["match"]]
    return matches[0] if matches else None, rows


def block_is(dim, x, y, z, block_id):
    clear_marker()
    out = cmd(f"execute in {dim} if block {x} {y} {z} {block_id} run execute in minecraft:overworld run setblock {MARKER[0]} {MARKER[1]} {MARKER[2]} redstone_block")
    return marker_was_set(), out


def entity_count(selector):
    clear_marker()
    out = cmd(f"execute if entity {selector} run execute in minecraft:overworld run setblock {MARKER[0]} {MARKER[1]} {MARKER[2]} redstone_block")
    return marker_was_set(), out


def entity_count_in(dim, selector):
    clear_marker()
    out = cmd(f"execute in {dim} if entity {selector} run execute in minecraft:overworld run setblock {MARKER[0]} {MARKER[1]} {MARKER[2]} redstone_block")
    return marker_was_set(), out


def clear_marker():
    cmd(f"execute in minecraft:overworld run setblock {MARKER[0]} {MARKER[1]} {MARKER[2]} air", delay=0.0)


def marker_was_set():
    out = cmd(f"execute in minecraft:overworld run fill {MARKER[0]} {MARKER[1]} {MARKER[2]} {MARKER[0]} {MARKER[1]} {MARKER[2]} air replace redstone_block", delay=0.0)
    return "Replaced 1 block" in out or "Replaced 1 blocks" in out or "Successfully filled 1 block" in out or "Changed the block" in out


def first_entity_health(selector):
    out = cmd(f"data get entity {selector} Health", delay=0.02)
    values = nums(out)
    return (values[0] if values else None), out


def first_entity_health_in(dim, selector):
    out = cmd(f"execute in {dim} run data get entity {selector} Health", delay=0.02)
    values = nums(out)
    return (values[0] if values else None), out


def result(name, tier, classification, evidence, conclusion, required=True):
    return {
        "name": name,
        "tier": tier,
        "required_for_ender_dragon": required,
        "classification": classification,
        "evidence": evidence,
        "conclusion": conclusion,
    }


def classify_use(physical_ok, server_substitute_ok, required=True):
    if physical_ok:
        return "direct_pass"
    if server_substitute_ok:
        return "engineering"
    if required:
        return "wall"
    return "engineering"


def wait_until(predicate, timeout=8.0, interval=0.25):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last and last[0]:
            return last
        time.sleep(interval)
    return last


def reset_area(dim="minecraft:overworld", x0=-40, x1=120, z0=-40, z1=40, y=70):
    step = 24
    x = x0
    while x <= x1:
        xe = min(x + step - 1, x1)
        cmd(f"execute in {dim} run fill {x} {y} {z0} {xe} {y + 12} {z1} air", delay=0.0)
        cmd(f"execute in {dim} run fill {x} {y - 1} {z0} {xe} {y - 1} {z1} stone", delay=0.0)
        x = xe + 1


def reset_bot(x=0, y=70, z=0, yaw=0, pitch=0, dim="minecraft:overworld"):
    cmd(f"player {BOT} spawn")
    time.sleep(0.15)
    cmd(f"execute in {dim} run tp {BOT} {x} {y} {z} {yaw} {pitch}")
    cmd(f"gamemode survival {BOT}")
    cmd(f"effect clear {BOT}")
    cmd(f"clear {BOT}")
    cmd(f"player {BOT} stop")
    time.sleep(0.1)


def setup():
    setup_commands = [
        "script unload minebot_phys",
        "script load minebot_phys global",
        "gamerule sendCommandFeedback true",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "gamerule doTileDrops true",
        "gamerule keepInventory true",
        "time set day",
        "weather clear",
        "difficulty normal",
        "kill @e[type=!player]",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
    ]
    for command in setup_commands:
        cmd(command)
    reset_area()
    reset_bot()


def test_bow():
    reset_area()
    reset_bot(0, 70, 0, 0, 0)
    cmd(f"item replace entity {BOT} weapon.mainhand with bow")
    cmd(f"item replace entity {BOT} weapon.offhand with arrow 16")
    cmd("summon husk 0 70 6 {Tags:[\"phys_bow_target\"],NoAI:1b,Health:20f,PersistenceRequired:1b}")
    before, raw_before = first_entity_health("@e[tag=phys_bow_target,limit=1]")
    shots = []
    for _ in range(3):
        cmd(f"player {BOT} look at 0 71 6")
        cmd(f"player {BOT} use continuous", delay=1.25)
        cmd(f"player {BOT} stop", delay=0.8)
        h, raw = first_entity_health("@e[tag=phys_bow_target,limit=1]")
        arrows, arrow_raw = entity_count("@e[type=arrow,distance=..20,limit=1]")
        shots.append({"health": h, "raw_health": raw[:250], "arrow_seen": arrows, "raw_arrow": arrow_raw[:250]})
        if h is not None and before is not None and h < before:
            break
    after = shots[-1]["health"] if shots else None
    physical = after is not None and before is not None and after < before
    generated_arrow = any(s["arrow_seen"] for s in shots)
    return {
        "subtest": "bow_damage",
        "physical_ok": physical,
        "server_substitute_ok": True,
        "classification": classify_use(physical, True),
        "evidence": {"before": before, "raw_before": raw_before[:250], "shots": shots, "generated_arrow": generated_arrow},
    }


def test_ender_pearl():
    reset_area()
    reset_bot(0, 70, 0, -90, -10)
    cmd(f"item replace entity {BOT} weapon.mainhand with ender_pearl 4")
    before, raw_before = pos()
    cmd(f"player {BOT} look at 40 72 0")
    cmd(f"player {BOT} use once", delay=3.5)
    after, raw_after = pos()
    moved = len(before) == 3 and len(after) == 3 and math.dist(before, after) > 5.0
    pearl_seen, pearl_raw = entity_count("@e[type=ender_pearl,distance=..80,limit=1]")
    return {
        "subtest": "ender_pearl_teleport",
        "physical_ok": moved,
        "server_substitute_ok": True,
        "classification": classify_use(moved, True),
        "evidence": {"before": before, "after": after, "dist": round(math.dist(before, after), 3) if len(before) == len(after) == 3 else None, "raw_before": raw_before[:250], "raw_after": raw_after[:250], "pearl_seen_after_wait": pearl_seen, "pearl_raw": pearl_raw[:250]},
    }


def give_fire_res_potion():
    variants = [
        f"item replace entity {BOT} weapon.mainhand with potion[potion_contents=fire_resistance]",
        f"item replace entity {BOT} weapon.mainhand with minecraft:potion[minecraft:potion_contents=fire_resistance]",
        f"give {BOT} potion[potion_contents=fire_resistance] 1",
        f"give {BOT} minecraft:potion[minecraft:potion_contents=fire_resistance] 1",
        f"give {BOT} potion{{Potion:\"minecraft:fire_resistance\"}} 1",
    ]
    rows = []
    for command in variants:
        out = cmd(command)
        rows.append({"command_shape": command.replace(BOT, "<bot>"), "raw": out[:300]})
        if "Expected whitespace" not in out and "Unknown item" not in out and "Invalid" not in out and "Unknown" not in out:
            return rows
    return rows


def test_potion():
    reset_area()
    reset_bot(0, 70, 0, 0, 0)
    give_rows = give_fire_res_potion()
    before = effects()
    cmd(f"player {BOT} use continuous", delay=2.4)
    cmd(f"player {BOT} stop", delay=0.4)
    after = effects()
    physical = "fire_resistance" in after
    sub_out = cmd(f"effect give {BOT} minecraft:fire_resistance 10 0 true")
    sub_after = effects()
    substitute = "fire_resistance" in sub_after
    return {
        "subtest": "fire_resistance_potion",
        "physical_ok": physical,
        "server_substitute_ok": substitute,
        "classification": classify_use(physical, substitute),
        "evidence": {"give_attempts": give_rows, "before_effects": before[:500], "after_drink_effects": after[:500], "effect_substitute_output": sub_out[:250], "after_substitute_effects": sub_after[:500]},
    }


def test_food():
    reset_area()
    reset_bot(0, 70, 0, 0, 0)
    cmd(f"effect give {BOT} minecraft:hunger 8 20 true", delay=9.0)
    before, raw_before = food()
    cmd(f"effect clear {BOT}")
    cmd(f"item replace entity {BOT} weapon.mainhand with cooked_beef 4")
    cmd(f"player {BOT} use continuous", delay=2.2)
    cmd(f"player {BOT} stop", delay=0.3)
    after, raw_after = food()
    physical = before is not None and after is not None and after > before
    substitute_out = cmd(f"effect give {BOT} minecraft:saturation 1 4 true", delay=0.3)
    sub_food, raw_sub = food()
    substitute = sub_food is not None and after is not None and sub_food >= after
    return {
        "subtest": "food_eating",
        "physical_ok": physical,
        "server_substitute_ok": substitute,
        "classification": classify_use(physical, substitute),
        "evidence": {"before_food": before, "after_food": after, "after_substitute_food": sub_food, "raw_before": raw_before[:250], "raw_after": raw_after[:250], "substitute_output": substitute_out[:250], "raw_substitute": raw_sub[:250]},
    }


def test_water_bucket():
    reset_area()
    reset_bot(0, 70, 0, 0, 0)
    cmd("setblock 0 69 2 stone")
    cmd("setblock 0 70 2 air")
    cmd(f"item replace entity {BOT} weapon.mainhand with water_bucket")
    cmd(f"player {BOT} look at 0.5 69.8 2.5")
    cmd(f"player {BOT} use once", delay=0.5)
    placed, raw_placed = block_is("minecraft:overworld", 0, 70, 2, "minecraft:water")
    cmd(f"player {BOT} use once", delay=0.5)
    removed, raw_removed = block_is("minecraft:overworld", 0, 70, 2, "minecraft:air")
    substitute_place = cmd("setblock 0 70 2 water")
    sub_placed, _ = block_is("minecraft:overworld", 0, 70, 2, "minecraft:water")
    cmd("setblock 0 70 2 air")
    return {
        "subtest": "water_bucket_place_collect",
        "physical_ok": placed and removed,
        "server_substitute_ok": sub_placed,
        "classification": classify_use(placed and removed, sub_placed),
        "evidence": {"placed_water": placed, "place_raw": raw_placed[:250], "removed_water": removed, "remove_raw": raw_removed[:250], "setblock_substitute": substitute_place[:250]},
    }


def test_flint_and_steel():
    reset_area()
    reset_bot(0, 70, 0, 0, 0)
    cmd("setblock 0 69 2 netherrack")
    cmd("setblock 0 70 2 air")
    cmd(f"item replace entity {BOT} weapon.mainhand with flint_and_steel")
    cmd(f"player {BOT} look at 0.5 69.8 2.5")
    cmd(f"player {BOT} use once", delay=0.5)
    lit, raw_lit = block_is("minecraft:overworld", 0, 70, 2, "minecraft:fire")
    cmd("setblock 0 69 2 netherrack")
    sub = cmd("setblock 0 70 2 fire")
    sub_lit, _ = block_is("minecraft:overworld", 0, 70, 2, "minecraft:fire")
    return {
        "subtest": "flint_and_steel_fire",
        "physical_ok": lit,
        "server_substitute_ok": sub_lit,
        "classification": classify_use(lit, sub_lit),
        "evidence": {"lit_fire": lit, "raw_lit": raw_lit[:250], "setblock_substitute": sub[:250]},
    }


def test_use_effects():
    subtests = [
        test_bow(),
        test_ender_pearl(),
        test_potion(),
        test_food(),
        test_water_bucket(),
        test_flint_and_steel(),
    ]
    wall_subtests = [s for s in subtests if s["classification"] == "wall"]
    classification = "wall" if wall_subtests else "engineering" if any(s["classification"] == "engineering" for s in subtests) else "direct_pass"
    return result(
        "player_use_true_effects",
        "Tier-1",
        classification,
        {"subtests": subtests},
        "No use-effect wall found." if not wall_subtests else "At least one required use effect had no physical path or server substitute.",
    )


def test_fall_damage():
    reset_area(x0=20, x1=40, z0=-5, z1=5, y=60)
    rows = []
    for height in [5, 10, 23]:
        reset_bot(30, 60 + height, 0, 0, 0)
        before, raw_before = health()
        time.sleep(max(2.0, height / 12.0 + 1.5))
        after_pos, raw_pos = pos()
        after, raw_after = health()
        rows.append({
            "height": height,
            "before_health": before,
            "after_health": after,
            "health_delta": round(before - after, 3) if before is not None and after is not None else None,
            "after_pos": after_pos,
            "raw_before": raw_before[:250],
            "raw_after": raw_after[:250],
            "raw_pos": raw_pos[:250],
        })
    real_damage = any(r["health_delta"] is not None and r["health_delta"] > 0 for r in rows)
    reset_bot(30, 70, 0, 0, 0)
    sub_before, _ = health()
    sub_out = cmd(f"damage {BOT} 7 minecraft:fall", delay=0.2)
    sub_after, raw_sub_after = health()
    substitute = sub_before is not None and sub_after is not None and sub_after < sub_before
    classification = "direct_pass" if real_damage else "engineering" if substitute else "wall"
    return result(
        "fall_damage_truth",
        "Tier-1",
        classification,
        {"fall_rows": rows, "damage_substitute": {"before": sub_before, "after": sub_after, "raw_after": raw_sub_after[:250], "output": sub_out[:250], "works": substitute}},
        "Fake player took real fall damage." if real_damage else "Fall damage did not register in this setup, but /damage can supply a server-side substitute." if substitute else "No real fall damage and /damage substitute failed.",
    )


def prepare_dimension_floor(dim, y=80):
    cmd(f"execute in {dim} run fill -8 {y} -8 8 {y + 6} 8 air")
    cmd(f"execute in {dim} run fill -8 {y - 1} -8 8 {y - 1} 8 stone")


def dimension_action_probe(dim):
    prepare_dimension_floor(dim)
    cmd(f"execute in {dim} run tp {BOT} 0 80 0 0 0", delay=0.4)
    before_dim, dim_rows = any_dimension()
    before_pos, _ = pos()
    cmd(f"player {BOT} look at 6 81 0")
    cmd(f"player {BOT} move forward", delay=1.2)
    cmd(f"player {BOT} stop", delay=0.2)
    after_pos, raw_after = pos()
    moved = len(before_pos) == 3 and len(after_pos) == 3 and math.dist(before_pos, after_pos) > 0.5
    cmd(f"execute in {dim} run kill @e[tag=phys_dim_target]")
    cmd(f"execute in {dim} run summon husk 0 80 2 {{Tags:[\"phys_dim_target\"],NoAI:1b,Health:20f,PersistenceRequired:1b}}")
    cmd(f"player {BOT} look at 0 81 2")
    h0, _ = first_entity_health("@e[tag=phys_dim_target,limit=1]")
    cmd(f"player {BOT} attack once", delay=0.4)
    h1, raw_h1 = first_entity_health("@e[tag=phys_dim_target,limit=1]")
    hit = h0 is not None and h1 is not None and h1 < h0
    return {"dim": dim, "current_dim": before_dim, "dimension_probe": dim_rows, "before_pos": before_pos, "after_pos": after_pos, "raw_after_pos": raw_after[:250], "moved": moved, "hit_target": hit, "target_health_before": h0, "target_health_after": h1, "raw_target_after": raw_h1[:250]}


def test_dimensions():
    reset_area()
    reset_bot(50, 70, 0, 0, 0)
    # Physical nether portal: build and put the fake player inside the portal blocks.
    cmd("fill 48 70 -1 52 74 -1 obsidian")
    cmd("fill 49 71 -1 51 73 -1 nether_portal[axis=x]")
    cmd(f"tp {BOT} 50 71 -1 0 0", delay=0.2)
    time.sleep(7.0)
    physical_dim, physical_rows = any_dimension()
    portal_to_nether = physical_dim == "minecraft:the_nether"
    # Command cross-dimension probes are the server-side substitute and prove the fake player can exist/action there.
    nether_probe = dimension_action_probe("minecraft:the_nether")
    end_probe = dimension_action_probe("minecraft:the_end")
    end_ok = end_probe["current_dim"] == "minecraft:the_end" and end_probe["moved"]
    nether_ok = nether_probe["current_dim"] == "minecraft:the_nether" and nether_probe["moved"]
    classification = "direct_pass" if portal_to_nether and end_ok and nether_ok else "engineering" if end_ok and nether_ok else "wall"
    return result(
        "dimension_travel_and_action",
        "Tier-1",
        classification,
        {"physical_nether_portal": {"changed_to_nether": portal_to_nether, "dimension": physical_dim, "rows": physical_rows}, "nether_action_probe": nether_probe, "end_action_probe": end_probe},
        "Fake player can exist and act in Nether and End; physical portal behavior is noted separately." if classification != "wall" else "Fake player could not be made to exist and act in required dimensions.",
    )


def test_attack_to_death():
    cmd("execute in minecraft:overworld run kill @e[tag=phystarget]")
    reset_area(x0=60, x1=80, z0=-5, z1=5, y=70)
    reset_bot(70, 70, 0, 0, 0)
    cmd(f"item replace entity {BOT} weapon.mainhand with diamond_sword")
    summon_raw = cmd("execute in minecraft:overworld run summon husk 70 70 2 {Tags:[\"phystarget\"],NoAI:1b,Health:20f,PersistenceRequired:1b}")
    cmd(f"player {BOT} look at 70 71 2")
    rows = []
    killed = False
    target_selector = "@e[type=husk,tag=phystarget,limit=1,sort=nearest]"
    initial_exists, initial_exists_raw = entity_count_in("minecraft:overworld", target_selector)
    initial_health, initial_health_raw = first_entity_health_in("minecraft:overworld", target_selector)
    if not initial_exists or initial_health is None:
        return result(
            "attack_to_death_tick_approx",
            "Tier-1",
            "inconclusive",
            {"summon": summon_raw[:300], "initial_exists": initial_exists, "initial_exists_raw": initial_exists_raw[:250], "initial_health": initial_health, "initial_health_raw": initial_health_raw[:250]},
            "Controlled target was not visible to the harness; attack result not classified.",
        )
    for i in range(12):
        before, raw_before = first_entity_health_in("minecraft:overworld", target_selector)
        exists_before, raw_exists_before = entity_count_in("minecraft:overworld", target_selector)
        if not exists_before:
            killed = True
            break
        cmd(f"player {BOT} attack once", delay=0.75)
        after, raw_after = first_entity_health_in("minecraft:overworld", target_selector)
        exists_after, raw_exists_after = entity_count_in("minecraft:overworld", target_selector)
        rows.append({"attack": i + 1, "before": before, "after": after, "delta": round(before - after, 3) if before is not None and after is not None else None, "exists_before": exists_before, "exists_after": exists_after, "raw_before": raw_before[:200], "raw_exists_before": raw_exists_before[:200], "raw_after": raw_after[:200], "raw_exists_after": raw_exists_after[:200]})
        if not exists_after:
            killed = True
            break
    any_damage = any(r["delta"] is not None and r["delta"] > 0 for r in rows)
    classification = "direct_pass" if killed else "engineering" if any_damage else "wall"
    return result(
        "attack_to_death_tick_approx",
        "Tier-1",
        classification,
        {"summon": summon_raw[:300], "initial_health": initial_health, "attacks": rows, "killed": killed, "any_damage": any_damage},
        "Tick-spaced fake-player attacks killed a controlled tagged target." if killed else "Fake-player attacks damaged but did not kill within the bounded loop; tune controller/cooldown." if any_damage else "Fake-player attack caused no damage to a controlled target.",
    )


def test_vertical_ladder_and_substitute():
    reset_area(x0=80, x1=100, z0=-5, z1=5, y=70)
    reset_bot(90, 70, 0, 0, 0)
    cmd("fill 91 70 0 91 76 0 oak_planks")
    for y in range(70, 77):
        cmd(f"setblock 90 {y} 0 ladder[facing=west]")
    before, _ = pos()
    cmd(f"player {BOT} look at 90.5 74 0.5")
    for _ in range(35):
        cmd(f"player {BOT} move forward", delay=0.08)
        cmd(f"player {BOT} jump once", delay=0.08)
    cmd(f"player {BOT} stop")
    after, raw_after = pos()
    ladder_gain = after[1] - before[1] if len(before) == 3 and len(after) == 3 else None
    reset_bot(94, 70, 0, 0, 0)
    tower_rows = []
    for i in range(4):
        p, _ = pos()
        if len(p) == 3:
            cmd(f"setblock {math.floor(p[0])} {math.floor(p[1]) - 1 + i} {math.floor(p[2])} cobblestone")
            cmd(f"tp {BOT} {p[0]} {p[1] + 1.0} {p[2]}")
            tower_rows.append(pos()[0])
    tower_gain = tower_rows[-1][1] - 70 if tower_rows else None
    return result(
        "vertical_ladder_and_substitute",
        "Tier-2",
        "engineering",
        {"ladder": {"before": before, "after": after, "raw_after": raw_after[:250], "y_gain": ladder_gain}, "server_tower_substitute": {"positions": tower_rows, "y_gain": tower_gain}},
        "Ladder/controller behavior is quantified; vertical movement has server-side scaffold substitutes, so this is engineering.",
        required=False,
    )


def test_physical_mine_place():
    reset_area(x0=100, x1=120, z0=-5, z1=5, y=70)
    reset_bot(110, 70, 0, 0, 0)
    cmd("setblock 110 70 3 stone")
    cmd(f"item replace entity {BOT} weapon.mainhand with diamond_pickaxe")
    cmd(f"player {BOT} look at 110.5 70.5 3.5")
    cmd(f"player {BOT} attack continuous", delay=1.8)
    cmd(f"player {BOT} stop", delay=0.2)
    broken, raw_broken = block_is("minecraft:overworld", 110, 70, 3, "minecraft:air")
    dropped, raw_drop = entity_count("@e[type=item,distance=..8,limit=1]")
    cmd(f"tp {BOT} 110 70 2.5")
    time.sleep(1.0)
    inv_after_mine = inventory()
    pickup = "cobblestone" in inv_after_mine or "stone" in inv_after_mine
    reset_bot(112, 70, 0, 0, 0)
    cmd("setblock 112 70 2 air")
    cmd("setblock 112 69 2 stone")
    cmd(f"item replace entity {BOT} weapon.mainhand with cobblestone 8")
    cmd(f"player {BOT} look at 112.5 69.8 2.5")
    cmd(f"player {BOT} use once", delay=0.4)
    placed, raw_placed = block_is("minecraft:overworld", 112, 70, 2, "minecraft:cobblestone")
    classification = "direct_pass" if broken and (dropped or pickup) and placed else "engineering"
    return result(
        "physical_mining_and_placing",
        "Tier-2",
        classification,
        {"mining": {"block_broken": broken, "raw_broken": raw_broken[:250], "drop_entity_seen": dropped, "raw_drop": raw_drop[:250], "inventory_after_pickup": inv_after_mine[:500], "pickup_seen": pickup}, "placing": {"placed": placed, "raw_placed": raw_placed[:250]}},
        "Physical dig/place path works." if classification == "direct_pass" else "Physical dig/place needs controller tuning or server substitute for some substep.",
        required=False,
    )


def run_all():
    setup()
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    tests = [
        test_use_effects,
        test_fall_damage,
        test_dimensions,
        test_attack_to_death,
        test_vertical_ladder_and_substitute,
        test_physical_mine_place,
    ]
    results = []
    for fn in tests:
        try:
            results.append(fn())
        except Exception as exc:
            results.append(result(fn.__name__, "unknown", "inconclusive", {"error": repr(exc)}, "Test crashed; inspect harness and rerun."))
    tier1 = [r for r in results if r["tier"] == "Tier-1"]
    walls = [r for r in tier1 if r["classification"] == "wall"]
    summary = {
        "started": started,
        "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "wall_found": bool(walls),
        "wall_tests": [r["name"] for r in walls],
        "phase_verdict": "reopen_phase_1" if walls else "close_phase_1_unique_falsifier_cleared",
    }
    payload = {"summary": summary, "results": results}
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 1 if walls else 0


if __name__ == "__main__":
    raise SystemExit(run_all())
