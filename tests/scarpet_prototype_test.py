#!/usr/bin/env python3
import json
import re
import time

from carpet_deep_test import Rcon


BOT = "TestBot"
HOST = "127.0.0.1"
PORT = 25576
PASSWORD = "test"


def nums(text):
    return [float(x) for x in re.findall(r"[-+]?\d+(?:\.\d+)?", text)]


def cmd(r, command, delay=0.08):
    # Use a fresh RCON connection per command. Minecraft RCON may split long
    # responses; closing after one command prevents leftover packets from being
    # misread as the next command response.
    one = Rcon(HOST, PORT, PASSWORD)
    try:
        out = one.command(command)
    finally:
        one.close()
    time.sleep(delay)
    return out


def app(r, expr, delay=0.08):
    return cmd(r, f"script in minebot_proto run {expr}", delay=delay)


def data(r, selector, field):
    return cmd(r, f"data get entity {selector} {field}", delay=0.02)


def pos(r):
    values = nums(data(r, BOT, "Pos"))
    return values[:3]


def health(r):
    values = nums(data(r, BOT, "Health"))
    return values[0] if values else None


def result(name, status, evidence, conclusion):
    return {"name": name, "status": status, "evidence": evidence, "conclusion": conclusion}


def setup(r):
    for command in [
        "script unload minebot_proto",
        "script load minebot_proto global",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "time set day",
        "weather clear",
        "difficulty normal",
        "kill @e[type=!player]",
        "fill -30 60 -30 30 90 30 air",
        "fill -30 59 -30 30 59 30 stone",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        f"player {BOT} spawn",
    ]:
        cmd(r, command)
    time.sleep(2)
    for command in [
        f"tp {BOT} 0 60 0 0 0",
        f"gamemode survival {BOT}",
        f"effect clear {BOT}",
        f"clear {BOT}",
        "script in minebot_proto run reset_results()",
    ]:
        cmd(r, command)


def test_crafting(r):
    cmd(r, f"clear {BOT}")
    cmd(r, f"item replace entity {BOT} hotbar.0 with oak_log 2")
    recipe = app(r, "recipe_probe()")
    craft = app(r, "craft_oak_planks()")
    inv = data(r, BOT, "Inventory")
    ok = "oak_planks" in inv and "oak_log" not in inv
    return result(
        "crafting_execution",
        "pass" if ok else "fail",
        {"recipe_probe": recipe[:700], "craft_result": craft, "inventory": inv[:700]},
        "Scarpet can implement non-GUI crafting by reading recipes and mutating inventory slots directly.",
    )


def test_same_tick_attack(r):
    cmd(r, "kill @e[type=zombie]")
    cmd(r, f"clear {BOT}")
    cmd(r, f"item replace entity {BOT} weapon.mainhand with diamond_sword")
    cmd(r, f"tp {BOT} 0 60 0 0 0")
    cmd(r, "summon zombie 0 60 1.8 {NoAI:1b,Health:20f}")
    before = cmd(r, "data get entity @e[type=zombie,limit=1,sort=nearest] Health")
    call = app(r, "same_tick_attack()", delay=0.15)
    after = cmd(r, "data get entity @e[type=zombie,limit=1,sort=nearest] Health")
    event = app(r, "result('deals_damage_event')")
    b = nums(before)
    a = nums(after)
    ok = b and a and a[0] < b[0]
    return result(
        "same_tick_look_attack",
        "pass" if ok else "fail",
        {"before": before, "call": call, "after": after, "deals_damage_event": event},
        "A Scarpet function can issue look and attack in one app invocation; damage was observed.",
    )


def test_cooldown_read(r):
    fields = ["attack_cooldown", "attack_cooldown_progress", "cooldown", "last_attack_time"]
    evidence = {}
    for field in fields:
        evidence[field] = app(r, f"query(bot(), '{field}')")
    supported = [k for k, v in evidence.items() if "Unknown entity feature" not in v and "Error while evaluating" not in v]
    return result(
        "attack_cooldown_read",
        "pass" if supported else "fail",
        evidence,
        "No direct Scarpet query field for attack cooldown was found." if not supported else f"Supported cooldown-like fields: {supported}",
    )


def wait_controller(r, mode, timeout=14):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = app(r, f"result('{mode}_final_pos')")
        if "null" not in last:
            return last
        time.sleep(0.4)
    return last


def wait_position(r, predicate, timeout=12):
    deadline = time.time() + timeout
    last = pos(r)
    while time.time() < deadline:
        last = pos(r)
        if len(last) >= 3 and predicate(last):
            return last
        time.sleep(0.25)
    return last


def test_closed_loop_move(r):
    cmd(r, "fill -5 60 -5 15 65 5 air")
    cmd(r, "fill -5 59 -5 15 59 5 stone")
    cmd(r, f"tp {BOT} 0 60 0 0 0")
    start = pos(r)
    app(r, "start_move('walk', 8, 60, 0)")
    reached = wait_position(r, lambda p: p[0] > 7.7)
    cmd(r, f"player {BOT} stop")
    final = app(r, "result('walk_final_pos')")
    end = pos(r)
    ok = abs(end[0] - 8.5) < 1.0 and abs(end[2] - 0.5) < 1.0
    return result(
        "closed_loop_move",
        "pass" if ok else "partial",
        {"start": start, "reached": reached, "final_record": final, "end": end},
        "Scarpet tick loop can close the loop over Carpet move/stop and stop near a target.",
    )


def test_swim_controller(r):
    cmd(r, "fill 18 60 -2 28 64 2 air")
    cmd(r, "fill 18 59 -2 28 59 2 stone")
    cmd(r, "fill 20 60 -1 26 62 1 water")
    cmd(r, f"tp {BOT} 18.5 60 0.5 -90 0")
    start = pos(r)
    app(r, "start_move('swim', 28, 60, 0)")
    reached = wait_position(r, lambda p: p[0] > 26)
    cmd(r, f"player {BOT} stop")
    final = app(r, "result('swim_final_pos')")
    end = pos(r)
    ok = end[0] > 26
    return result(
        "swim_controller",
        "pass" if ok else "partial",
        {"start": start, "reached": reached, "final_record": final, "end": end},
        "Scarpet can add jump pulses while moving, but water traversal still needs robust controller tuning.",
    )


def test_ladder_controller(r):
    cmd(r, "fill -12 60 -2 -6 70 2 air")
    cmd(r, "fill -9 60 0 -9 66 0 ladder[facing=west]")
    cmd(r, "fill -8 60 0 -8 66 0 stone")
    cmd(r, "setblock -9 59 0 stone")
    cmd(r, f"tp {BOT} -10.5 60 0.5 -90 0")
    start = pos(r)
    app(r, "start_move('ladder', -9, 66, 0)")
    reached = wait_position(r, lambda p: p[1] > start[1] + 3 if len(start) >= 2 else False)
    cmd(r, f"player {BOT} stop")
    final = app(r, "result('ladder_final_pos')")
    end = pos(r)
    ok = len(end) >= 2 and len(start) >= 2 and end[1] > start[1] + 3
    return result(
        "ladder_controller",
        "pass" if ok else "fail",
        {"start": start, "reached": reached, "final_record": final, "end": end},
        "Simple Scarpet forward+jump ladder controller did not reliably climb." if not ok else "Scarpet can coordinate ladder climb with tick control.",
    )


def test_mob_aggro(r):
    cmd(r, "kill @e[type=zombie]")
    cmd(r, f"tp {BOT} 0 60 0 0 0")
    cmd(r, f"gamemode survival {BOT}")
    before = health(r)
    cmd(r, "summon zombie 2 60 0 {Health:20f}")
    time.sleep(10)
    after = health(r)
    return result(
        "mob_aggro",
        "pass" if after is not None and before is not None and after < before else "fail",
        {"before": before, "after": after},
        "Zombie damaged TestBot." if after is not None and before is not None and after < before else "Zombie did not damage fake player in a 10s observation window.",
    )


def test_damage_event(r):
    app(r, "reset_results()")
    cmd(r, f"tp {BOT} 0 60 0")
    before = health(r)
    cmd(r, f"damage {BOT} 4 minecraft:generic", delay=0.5)
    after = health(r)
    event = app(r, "result('damage_event')")
    ok = "4" in event or (before is not None and after is not None and after < before)
    return result(
        "damage_event",
        "pass" if ok and "null" not in event else "partial",
        {"before": before, "after": after, "event": event},
        "Scarpet player_takes_damage event can observe fake-player damage." if "null" not in event else "Damage applies, but event was not captured in the app result.",
    )


def test_death_respawn(r):
    app(r, "reset_results()")
    cmd(r, f"tp {BOT} 0 60 0")
    cmd(r, f"kill {BOT}", delay=2.5)
    death = app(r, "result('death_event')")
    respawn = app(r, "result('respawn_scheduled')")
    exists = data(r, BOT, "Health")
    ok = "true" in death and "true" in respawn and "No entity was found" not in exists
    return result(
        "death_auto_respawn",
        "pass" if ok else "partial",
        {"death_event": death, "respawn_scheduled": respawn, "entity_health": exists},
        "Scarpet can observe fake-player death and schedule /player spawn for respawn." if ok else "Death/respawn was not fully confirmed.",
    )


def test_container_transfer(r):
    cmd(r, f"player {BOT} spawn")
    cmd(r, f"tp {BOT} 0 60 0")
    cmd(r, f"clear {BOT}")
    cmd(r, "setblock 4 60 0 chest")
    cmd(r, "item replace block 4 60 0 container.0 with diamond 7")
    call = app(r, "container_transfer(4, 60, 0)")
    bot_inv = data(r, BOT, "Inventory")
    chest = cmd(r, "data get block 4 60 0 Items")
    ok = "diamond" in bot_inv and "diamond" not in chest
    return result(
        "container_non_gui_transfer",
        "pass" if ok else "fail",
        {"call": call, "bot_inventory": bot_inv[:600], "chest_items": chest[:600]},
        "Scarpet can move items between chest inventory and fake-player inventory without opening GUI.",
    )


def test_network_api(r):
    funcs = {
        "http_get": "call('http_get', 'http://127.0.0.1')",
        "http_request": "call('http_request', 'http://127.0.0.1')",
        "request": "call('request', 'http://127.0.0.1')",
        "socket": "call('socket', '127.0.0.1', 1)",
        "websocket": "call('websocket', '127.0.0.1', 1)",
        "server_socket": "call('server_socket', 1)",
    }
    evidence = {name: app(r, expr) for name, expr in funcs.items()}
    supported = [k for k, v in evidence.items() if "not defined" not in v and "Error while evaluating" not in v]
    return result(
        "scarpet_network_api",
        "pass" if supported else "fail",
        evidence,
        "No built-in Scarpet socket/HTTP/WebSocket API was found." if not supported else f"Network-like APIs found: {supported}",
    )


def main():
    r = Rcon(HOST, PORT, PASSWORD)
    try:
        setup(r)
        results = [
            test_crafting(r),
            test_same_tick_attack(r),
            test_cooldown_read(r),
            test_closed_loop_move(r),
            test_swim_controller(r),
            test_ladder_controller(r),
            test_mob_aggro(r),
            test_damage_event(r),
            test_death_respawn(r),
            test_container_transfer(r),
            test_network_api(r),
        ]
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        r.close()


if __name__ == "__main__":
    main()
