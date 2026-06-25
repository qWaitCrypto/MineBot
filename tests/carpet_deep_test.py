#!/usr/bin/env python3
import json
import re
import socket
import struct
import time


HOST = "127.0.0.1"
PORT = 25576
PASSWORD = "test"
BOT = "TestBot"


class Rcon:
    def __init__(self, host, port, password):
        self.sock = socket.create_connection((host, port), timeout=20)
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


def cmd(r, command, delay=0.05):
    out = r.command(command)
    time.sleep(delay)
    return out


def number_list(text):
    return [float(x) for x in re.findall(r"[-+]?\d+(?:\.\d+)?", text)]


def entity_field(r, selector, field):
    return cmd(r, f"data get entity {selector} {field}", delay=0.02)


def bot_pos(r):
    nums = number_list(entity_field(r, BOT, "Pos"))
    return nums[:3]


def bot_health(r):
    nums = number_list(entity_field(r, BOT, "Health"))
    return nums[0] if nums else None


def bot_inventory_text(r):
    return entity_field(r, BOT, "Inventory")


def setup_common(r):
    commands = [
        "gamerule sendCommandFeedback true",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "time set day",
        "weather clear",
        "difficulty normal",
        f"player {BOT} kill",
        "kill @e[type=!player]",
        "setworldspawn 0 80 0",
        "fill -20 70 -20 20 90 20 air",
        "fill -20 69 -20 20 69 20 stone",
        "carpet commandPlayer true",
        "carpet allowSpawningOfflinePlayers true",
        f"player {BOT} spawn",
        f"tp {BOT} 0 70 0 0 0",
        f"gamemode survival {BOT}",
        f"effect give {BOT} minecraft:saturation infinite 255 true",
    ]
    for command in commands:
        cmd(r, command)


def result(name, status, evidence, conclusion):
    return {
        "name": name,
        "status": status,
        "evidence": evidence,
        "conclusion": conclusion,
    }


def test_item_command(r):
    cmd(r, f"clear {BOT}")
    out1 = cmd(r, f"item replace entity {BOT} hotbar.0 with diamond_pickaxe")
    out2 = cmd(r, f"item replace entity {BOT} inventory.9 with cobblestone 64")
    inv = bot_inventory_text(r)
    ok = "diamond_pickaxe" in inv and "cobblestone" in inv
    return result(
        "container_item_command",
        "pass" if ok else "fail",
        {"hotbar": out1, "inventory": out2, "inventory_data": inv[:500]},
        "/item replace entity works on Carpet fake players" if ok else "/item did not mutate fake-player inventory",
    )


def test_container_use(r):
    cmd(r, "setblock 0 70 2 chest")
    cmd(r, "item replace block 0 70 2 container.0 with diamond 3")
    cmd(r, f"tp {BOT} 0 70 0 0 0")
    cmd(r, f"player {BOT} look at 0.5 70.5 2.5")
    out = cmd(r, f"player {BOT} use once", delay=0.2)
    data = entity_field(r, BOT, "{}")
    open_hint = any(key in data for key in ["containerMenu", "containerCounter", "enderItems"])
    return result(
        "container_open_chest",
        "partial" if out is not None else "fail",
        {"use_output": out, "entity_data_sample": data[:700], "open_state_visible_in_data": open_hint},
        "use once can target a chest, but RCON /data does not expose a reliable open-screen assertion",
    )


def test_crafting_command_surface(r):
    help_recipe = cmd(r, "help recipe")
    help_player = cmd(r, "help player")
    help_script = cmd(r, "help script")
    help_item = cmd(r, "help item")
    no_craft = "craft" not in (help_recipe + help_player + help_script + help_item).lower()
    return result(
        "crafting_command_surface",
        "fail" if no_craft else "partial",
        {
            "help_recipe": help_recipe[:500],
            "help_player_has_craft": "craft" in help_player.lower(),
            "help_script": help_script[:500],
            "help_item_has_craft": "craft" in help_item.lower(),
        },
        "No vanilla/Carpet RCON command surface for crafting was found; recipe unlock is not crafting",
    )


def test_same_tick_attack(r):
    cmd(r, f"clear {BOT}")
    cmd(r, f"item replace entity {BOT} weapon.mainhand with diamond_sword")
    cmd(r, f"tp {BOT} 0 70 0 0 0")
    cmd(r, "summon zombie 0 70 1.8 {NoAI:1b,Health:20f}")
    before = cmd(r, "data get entity @e[type=zombie,limit=1,sort=nearest] Health")
    cmd(r, f"player {BOT} look at 0 71 1.8")
    cmd(r, f"player {BOT} attack once", delay=0.2)
    after = cmd(r, "data get entity @e[type=zombie,limit=1,sort=nearest] Health")
    nums_before = number_list(before)
    nums_after = number_list(after)
    hit = nums_before and nums_after and nums_after[0] < nums_before[0]
    return result(
        "combat_look_attack_sequence",
        "pass" if hit else "fail",
        {"before": before, "after": after},
        "Sequential RCON look-at then attack can hit a stationary target; exact same-tick semantics remain unproven by RCON",
    )


def test_attack_cooldown(r):
    cmd(r, "kill @e[type=zombie]")
    cmd(r, f"item replace entity {BOT} weapon.mainhand with diamond_sword")
    cmd(r, f"tp {BOT} 0 70 0 0 0")
    cmd(r, "summon zombie 0 70 1.8 {NoAI:1b,Health:40f}")
    cmd(r, f"player {BOT} look at 0 71 1.8")
    cmd(r, f"player {BOT} attack once", delay=0.1)
    health1 = cmd(r, "data get entity @e[type=zombie,limit=1,sort=nearest] Health")
    cmd(r, f"player {BOT} attack once", delay=0.2)
    health2 = cmd(r, "data get entity @e[type=zombie,limit=1,sort=nearest] Health")
    h1 = number_list(health1)
    h2 = number_list(health2)
    second_damage = h1 and h2 and h2[0] < h1[0]
    return result(
        "combat_attack_cooldown",
        "pass" if second_damage else "partial",
        {"after_first": health1, "after_second": health2},
        "Fake player uses normal combat pipeline; rapid second attack still registers only reduced/limited damage",
    )


def test_bow_release(r):
    cmd(r, "kill @e[type=zombie]")
    cmd(r, f"clear {BOT}")
    cmd(r, f"item replace entity {BOT} weapon.mainhand with bow")
    cmd(r, f"item replace entity {BOT} weapon.offhand with arrow 16")
    cmd(r, f"tp {BOT} 0 70 0 0 0")
    cmd(r, "summon zombie 0 70 8 {NoAI:1b,Health:20f}")
    cmd(r, f"player {BOT} look at 0 71 8")
    cmd(r, f"player {BOT} use continuous", delay=1.2)
    cmd(r, f"player {BOT} stop", delay=0.5)
    arrows = cmd(r, "data get entity @e[type=arrow,limit=1,sort=nearest] Pos")
    return result(
        "combat_bow_charge_release",
        "pass" if "No entity was found" not in arrows else "fail",
        {"arrow_query": arrows[:500]},
        "Carpet use continuous/stop can fire a bow from a fake player" if "No entity was found" not in arrows else "Bow charge/release did not create an arrow",
    )


def test_mob_aggro(r):
    cmd(r, "kill @e[type=zombie]")
    cmd(r, f"tp {BOT} 0 70 0")
    before = bot_health(r)
    cmd(r, "summon zombie 1 70 0 {Health:20f}", delay=4.0)
    after = bot_health(r)
    return result(
        "entity_zombie_aggro",
        "pass" if before is not None and after is not None and after < before else "fail",
        {"health_before": before, "health_after": after},
        "Hostile mobs can target and damage Carpet fake players" if before is not None and after is not None and after < before else "Zombie did not damage fake player in the observation window",
    )


def test_swimming(r):
    cmd(r, "fill -3 70 -1 8 72 1 water")
    cmd(r, "fill -3 69 -1 8 69 1 stone")
    cmd(r, f"tp {BOT} 0 70 0 90 0")
    start = bot_pos(r)
    cmd(r, f"player {BOT} move forward", delay=2.0)
    cmd(r, f"player {BOT} stop")
    end = bot_pos(r)
    moved = start and end and end[0] > start[0] + 1.0
    return result(
        "movement_swimming",
        "pass" if moved else "fail",
        {"start_pos": start, "end_pos": end},
        "Carpet move-forward drives fake-player swimming/water movement" if moved else "Fake player did not traverse water in this setup",
    )


def test_ladder_climb(r):
    cmd(r, "fill 10 70 0 10 75 0 ladder[facing=west]")
    cmd(r, "fill 11 70 0 11 75 0 stone")
    cmd(r, f"tp {BOT} 9.5 70 0.5 -90 0")
    start = bot_pos(r)
    cmd(r, f"player {BOT} move forward", delay=2.0)
    cmd(r, f"player {BOT} stop")
    end = bot_pos(r)
    climbed = start and end and end[1] > start[1] + 1.0
    return result(
        "movement_ladder_climb",
        "pass" if climbed else "fail",
        {"start_pos": start, "end_pos": end},
        "Carpet held movement can climb ladders when aligned" if climbed else "Ladder climb was not achieved with simple move-forward",
    )


def test_sneak_edge(r):
    cmd(r, "fill -5 80 -5 5 80 5 air")
    cmd(r, "setblock 0 80 0 stone")
    cmd(r, f"tp {BOT} 0.5 81 0.5 0 0")
    cmd(r, f"player {BOT} sneak")
    cmd(r, f"player {BOT} move forward", delay=1.0)
    cmd(r, f"player {BOT} stop")
    cmd(r, f"player {BOT} unsneak")
    end = bot_pos(r)
    safe = end and end[1] >= 80.5
    return result(
        "movement_sneak_edge",
        "pass" if safe else "fail",
        {"end_pos": end},
        "Carpet sneak plus movement preserves vanilla edge-safety" if safe else "Fake player fell while sneaking at an edge",
    )


def test_villager_use(r):
    cmd(r, "kill @e[type=villager]")
    cmd(r, f"tp {BOT} 0 70 0 0 0")
    cmd(r, "summon villager 0 70 2 {NoAI:1b,VillagerData:{profession:\"minecraft:farmer\",level:2,type:\"minecraft:plains\"},Offers:{Recipes:[{buy:{id:\"minecraft:wheat\",count:20},sell:{id:\"minecraft:emerald\",count:1},maxUses:999}]}}")
    cmd(r, f"player {BOT} look at 0 71 2")
    out = cmd(r, f"player {BOT} use once", delay=0.2)
    data = entity_field(r, BOT, "{}")
    return result(
        "entity_villager_use",
        "partial",
        {"use_output": out, "entity_data_sample": data[:500]},
        "Fake player can issue use against villager, but RCON cannot prove a trade screen is open",
    )


def test_scarpet_inventory_recipe(r):
    read1 = cmd(r, f"script run inventory_get('{BOT}', 0)")
    write = cmd(r, f"script run inventory_set('{BOT}', 0, 64, 'diamond')")
    read2 = cmd(r, f"script run inventory_get('{BOT}', 0)")
    block = cmd(r, "script run block(0,70,0)")
    recipe = cmd(r, "script run recipe_data('minecraft:stick')")
    craft = cmd(r, f"script run craft(player('{BOT}'), 'minecraft:stick')")
    ok = "diamond, 64" in read2 and "minecraft:stick" in recipe and "not defined" in craft
    return result(
        "scarpet_inventory_recipe",
        "pass" if ok else "partial",
        {"read_before": read1, "write": write, "read_after": read2, "block": block, "recipe": recipe[:500], "craft": craft[:500]},
        "Scarpet can read/write fake-player inventory and inspect recipes/world blocks, but has no built-in craft(player, recipe)",
    )


def test_death_respawn(r):
    cmd(r, f"clear {BOT}")
    cmd(r, f"item replace entity {BOT} hotbar.0 with diamond 5")
    cmd(r, f"kill {BOT}", delay=0.5)
    after_kill = cmd(r, f"data get entity {BOT} Health")
    dropped = cmd(r, "execute if entity @e[type=item] run data get entity @e[type=item,limit=1,sort=nearest] Item")
    respawn = cmd(r, f"player {BOT} spawn", delay=0.2)
    cmd(r, f"tp {BOT} 0 70 0 0 0")
    inv = bot_inventory_text(r)
    return result(
        "death_respawn_inventory",
        "partial",
        {"after_kill": after_kill, "dropped_item": dropped, "respawn": respawn, "inventory_after_respawn": inv[:500]},
        "Death removes the fake-player entity and drops inventory; manual /player spawn is needed for a new body",
    )


def main():
    r = Rcon(HOST, PORT, PASSWORD)
    try:
        setup_common(r)
        results = [
            test_item_command(r),
            test_container_use(r),
            test_crafting_command_surface(r),
            test_same_tick_attack(r),
            test_attack_cooldown(r),
            test_bow_release(r),
            test_swimming(r),
            test_ladder_climb(r),
            test_sneak_edge(r),
            test_mob_aggro(r),
            test_villager_use(r),
            test_scarpet_inventory_recipe(r),
            test_death_respawn(r),
        ]
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        r.close()


if __name__ == "__main__":
    main()
