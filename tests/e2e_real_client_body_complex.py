"""Developer-directed single-capability tasks in the disposable client world."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time
import threading
import gzip
import struct

from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.contract import Action, Region, ToolResult
from minebot.game.governance import GovernancePolicy
from minebot.game.rcon import RconClient, RconConfig
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientIpc


CASES = ("navigation", "mine_resource", "perception", "menus", "combat", "vertical", "building", "farming", "interactions", "collection", "controls", "exploration", "flight", "players", "pickup", "placement", "crop_interactions", "native_controls", "spacing", "native_misc", "sleep", "blacklist", "effects", "file_house", "file_cancel")


def write_house_schematic(path: Path) -> list[int]:
    # Sponge v2 NBT: standard-library serialization of a supplied blueprint.
    def tag(kind,name,payload):
        encoded=name.encode();return bytes([kind])+struct.pack('>H',len(encoded))+encoded+payload
    def text_payload(value):
        raw=value.encode();return struct.pack('>H',len(raw))+raw
    cells=[]
    for y in range(4):
        for z in range(5):
            for x in range(5):
                wall=x in (0,4) or z in (0,4)
                solid=y in (0,3) or (wall and not (x==2 and z==0))
                cells.append(1 if solid else 0)
    palette=tag(3,'minecraft:air',struct.pack('>i',0))+tag(3,'minecraft:cobblestone',struct.pack('>i',1))+b'\0'
    payload=tag(3,'Version',struct.pack('>i',2))+tag(3,'DataVersion',struct.pack('>i',3700))
    for key,value in [('Width',5),('Height',4),('Length',5)]:payload+=tag(2,key,struct.pack('>h',value))
    payload+=tag(11,'Offset',struct.pack('>iiii',3,0,0,0))+tag(3,'PaletteMax',struct.pack('>i',2))
    payload+=tag(10,'Palette',palette)+tag(7,'BlockData',struct.pack('>i',len(cells))+bytes(cells))+b'\0'
    path.write_bytes(gzip.compress(tag(10,'Schematic',payload)))
    return cells


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-local-server", action="store_true", required=True)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--through-agent", action="store_true", help="Use production Agent dispatch and model-result projection, without model calls")
    args = parser.parse_args()
    root = Path("logs") / "body-complex-20261007" / f"{args.case}-{time.time_ns()}"
    root.mkdir(parents=True, exist_ok=True)
    body = RealClientBody("MineBotCombat", RealClientIpc(timeout_s=30))
    rcon = RconClient(RconConfig(password="test"))
    region = Region("body-complex-fixtures", (672, 140, -32), (1100, 280, 96))
    registry = build_phase1_registry(body, Phase1RuntimeConfig(region, body_provider="real_client",
        governance_policy=GovernancePolicy(natural_regions=[region], bot_name="MineBotCombat")))
    report = {"case": args.case, "success": False, "records": [], "model_calls": 0,
              "conditions": "disposable local world; developer-supplied terrain/resources; non-player fixture entities cleared before tasks; production Body/tool calls",
              "tuple": "MC 26.1.2 / Fabric 0.19.3 / API 0.152.1 / Java 25 / Baritone 1.18.0"}
    began = time.monotonic()
    runtime = None
    if args.through_agent:
        from minebot.app.phase1_runtime import build_phase1_agent_runtime
        from minebot.app.runner import RuntimeRunContext
        parts = build_phase1_agent_runtime(body=body, goal_text=f"Validate {args.case}", model_provider=None,
            config=Phase1RuntimeConfig(region, body_provider="real_client",
                governance_policy=GovernancePolicy(natural_regions=[region], bot_name="MineBotCombat")))
        runtime = parts.runtime
        runtime.native_responses = True
        runtime._native_context = RuntimeRunContext(agent_context=parts.context, weld_context=runtime.weld_context,
            profile=parts.modes.profile_for(parts.lifecycle.state), trace=runtime.trace, runtime=runtime)
        registry = runtime.registry
    report["through_agent_dispatch"] = args.through_agent

    def command(text):
        return rcon.command(text)

    def check(text):
        observed = command(text)
        report["records"][-1].setdefault("server_checks", []).append({"command": text, "observed": observed})
        assert observed.startswith("Test passed"), (text, observed)

    def call(name, params):
        report["step"] = {"tool": name, "params": params}
        started = time.monotonic()
        registered = registry.get(name)
        assert set(params) <= set(registered.input_schema.get("properties",{})), ("invalid experiment tool parameters", name, params)
        assert set(registered.input_schema.get("required",[])) <= set(params), ("missing experiment parameters",name)
        model_payload = None
        if runtime is None:
            result = registered.callable(params)
        else:
            call_id = f"direct-{len(report['records'])}-{name}"
            model_payload = asyncio.run(runtime._invoke_responses_tool(name, json.dumps(params), call_id))
            full = next(e["full_result"] for e in reversed(runtime.trace.snapshot())
                        if e.get("event") == "tool_result" and e.get("tool_call_id") == call_id)
            result = ToolResult(bool(full["success"]), full["reason"], can_retry=bool(full.get("canRetry")), metrics=full.get("metrics", {}))
        report["records"].append({"tool": name, "params": params, "result": result.to_payload(),
                                  "elapsed_s": round(time.monotonic() - started, 3)})
        if model_payload is not None:
            report["records"][-1]["model_result"] = model_payload
            report["records"][-1]["model_tool"] = next(t for t in runtime._responses_tools() if t["name"] == name)
        print(name, result.success, result.reason, flush=True)
        assert result.success, result.to_payload()
        return result

    def action(name, params, *, cancel_after=None, expect_success=True):
        report["step"] = {"action": name, "params": params}
        request = Action.create(name, params)
        report["before_action"] = body.ipc.state()
        accepted = body.execute(request)
        assert accepted.accepted, accepted
        if cancel_after is not None:
            time.sleep(cancel_after)
            active = body.ipc.state()
            stopped = time.monotonic()
            assert body.interrupt("directed cancellation").ok
            report["cancel_before"] = active
        terminal = body.await_action_terminal(request.id, timeout_s=float(params.get("timeout_s", 60)) + 10)
        data = dict(terminal.data)
        report["records"].append({"action": name, "params": params, "terminal": data})
        print(name, data.get("success"), data.get("reason"), flush=True)
        if cancel_after is not None:
            data["observed_cancel_latency_ms"] = round((time.monotonic()-stopped)*1000)
            assert data.get("status")=="canceled", data
            time.sleep(0.5)
            stable=body.ipc.state()
            assert not stable["any_process_active"] and not stable["pathing"],stable
            data["settled_after_cancel"] = stable["position"]
        elif expect_success:
            assert data.get("success"), data
        return data

    def fixture(x=700, z=0, width=60, depth=32):
        action("stop", {})
        command(f"forceload add {x-16} {z-16} {x+width+16} {z+depth+16}")
        until = time.monotonic()+15
        while True:
            reply=command(f"fill {x-16} 149 {z-16} {x+width+16} 149 {z+depth+16} minecraft:stone")
            if "not loaded" not in reply:break
            assert time.monotonic()<until, reply
            time.sleep(0.2)
        assert "filled" in reply, reply
        command(f"fill {x} 150 {z} {x+width} 165 {z+depth} minecraft:air")
        command(f"fill {x} 166 {z} {x+width} 180 {z+depth} minecraft:air")
        command(f"kill @e[type=!player,x={x},y=149,z={z},dx={width},dy=32,dz={depth}]")
        command("gamemode survival MineBotCombat")
        command("effect clear MineBotCombat")
        command("effect give MineBotCombat minecraft:saturation 1 10 true")
        command("clear MineBotCombat")
        command("time set noon")
        command(f"tp MineBotCombat {x+0.5} 150 {z+0.5}")
        action("baritoneSettings", {"settings":{"allow_break":True,"allow_place":True}})
        until = time.monotonic()+10
        while True:
            s = body.ipc.state()
            if abs(s["position"][0]-x-0.5)<0.1 and abs(s["position"][2]-z-0.5)<0.1 and s["on_ground"] and s.get("feet_chunk_loaded") and s.get("health",0)>0:
                floor=body.perceive("blockAt", {"x":x,"y":149,"z":z})
                if floor.ok and floor.data.get("type")=="minecraft:stone":break
            assert time.monotonic()<until, "fixture teleport not observed"
            time.sleep(0.1)

    try:
        body.get_state()
        if args.through_agent:
            call("read_body_processes", {})
        report["seed"] = command("seed")
        report["difficulty"] = command("difficulty")
        if args.case == "navigation":
            fixture()
            command("fill 700 150 0 760 154 0 minecraft:stone")
            command("fill 700 150 32 760 154 32 minecraft:stone")
            command("fill 700 150 0 700 154 32 minecraft:stone")
            command("fill 760 150 0 760 154 32 minecraft:stone")
            for i,x in enumerate((712,724,736,748)):
                command(f"fill {x} 150 1 {x} 154 31 minecraft:stone")
                a,b=(27,31) if i%2==0 else (1,5)
                command(f"fill {x} 150 {a} {x} 154 {b} minecraft:air")
            command("tp MineBotCombat 702.5 150 3.5")
            time.sleep(0.4)
            action("baritoneSettings", {"settings": {"allow_break": False, "allow_place": False}})
            report["task"] = "Single move_to through four alternating five-block-high maze walls; no terrain mutations"
            call("move_to", {"pos": [757,150,28], "radius": 0, "timeout_s": 120})
            check("execute if entity @a[name=MineBotCombat,x=757.5,y=150,z=28.5,distance=..1.2]")
            for x in (712,724,736,748):check(f"execute if block {x} 150 16 minecraft:stone")
        elif args.case == "mine_resource":
            fixture(depth=16)
            for x in (709,719,729):
                command(f"fill {x} 150 6 {x+1} 152 7 minecraft:oak_log")
            command("give MineBotCombat minecraft:iron_axe")
            command("give MineBotCombat minecraft:oak_log 5")
            time.sleep(0.3)
            action("baritoneSettings", {"settings": {"allow_break": True, "allow_place": False}})
            until=time.monotonic()+10
            while True:
                observed=body.perceive("blockAt", {"x":709,"y":150,"z":6})
                if observed.ok and observed.data.get("type")=="minecraft:oak_log":break
                assert time.monotonic()<until, observed
                time.sleep(0.1)
            report["fixture_ore_observation"] = observed.data
            report["task"] = "One mineResource call must collect 24 logs from three separated three-high clusters"
            data = call("mine_resource", {"block": "oak_log", "quantity": 24, "timeout_s": 120}).metrics
            observed = command("clear MineBotCombat minecraft:oak_log 0")
            report["server_inventory_count"] = observed
            assert body.get_state().inventory_counts.get("oak_log",0)>=29, observed
        elif args.case == "perception":
            fixture(width=24,depth=24)
            for x in range(704,712):
                for z in range(4,12):command(f"setblock {x} 150 {z} minecraft:diamond_ore")
            command("setblock 703 150 14 minecraft:oak_fence_gate[open=true]")
            command("item replace entity MineBotCombat hotbar.4 with minecraft:diamond 7")
            command("item replace entity MineBotCombat inventory.20 with minecraft:iron_ingot 13")
            command('kill @e[name=BodyReadTarget]')
            command('kill @e[name=BodyReadDecoy]')
            command('summon sheep 703 150 3 {NoAI:1b,CustomName:"BodyReadTarget"}')
            command('summon cow 704 150 2 {NoAI:1b,CustomName:"BodyReadDecoy"}')
            until=time.monotonic()+10
            while body.perceive("blockAt", {"x":711,"y":150,"z":11}).data.get("type")!="minecraft:diamond_ore":
                assert time.monotonic()<until, "last fixture ore not synced"
                time.sleep(0.1)
            report["task"] = "Read sparse inventory, distinguish entities, and page 64 exact ore blocks without duplicates"
            call("read_state", {})
            inv=call("read_inventory", {})
            assert inv.metrics["counts"]=={"diamond":7,"iron_ingot":13}, inv.to_payload()
            small=body.perceive("nearbyEntities", {"radius":1,"limit":1})
            assert small.ok and small.data["entities"]==[], small
            nearby=body.perceive("nearbyBlocks", {"radius":4,"limit":128})
            assert nearby.ok and nearby.data["count"]>0, nearby
            call("read_recipe", {"item":"cake"})
            blocks=call("search_for_block", {"block_types": ["diamond_ore"],"search_radius":24,"find_limit":12,"max_pages":8})
            for name,params in [("read_block",{"pos":[703,150,14]}),("read_nearby_blocks",{"radius":16,"limit":128}),
                ("read_nearby_entities",{"radius":24,"limit":64}),("search_for_entity",{"entity_name":"BodyReadTarget","entity_types":["sheep"],"search_radius":24,"max_distance":2})]:call(name,params)
            locations=[c["pos"] for c in blocks.metrics.get("candidates", [])]
            report["block_search_metrics"] = blocks.metrics
            assert len({tuple(p) for p in locations})==64, ("expected 64 unique ore coordinates",len(locations),blocks.to_payload())
            check('execute if data entity MineBotCombat Inventory[{id:"minecraft:diamond",count:7}]')
            check('execute if data entity MineBotCombat Inventory[{id:"minecraft:iron_ingot",count:13}]')
        elif args.case == "menus":
            fixture(width=30,depth=12)
            command("setblock 702 150 2 minecraft:crafting_table")
            command("setblock 702 150 4 minecraft:chest")
            command("setblock 702 150 6 minecraft:blast_furnace")
            command("recipe give MineBotCombat *")
            command("item replace entity MineBotCombat inventory.0 with minecraft:oak_planks 32")
            command("item replace entity MineBotCombat inventory.18 with minecraft:oak_planks 32")
            time.sleep(0.5)
            report["task"] = "Batch recipes from sparse inventory, 96-item chest transfer, and 4-item blast-furnace batch"
            call("craft_item", {"item":"stick","count":64})
            assert body.get_state().inventory_counts.get("stick",0)>=64
            call("craft_item", {"item":"crafting_table","count":8})
            assert body.get_state().inventory_counts.get("crafting_table",0)>=8
            command("clear MineBotCombat")
            for item,n in [("milk_bucket",6),("sugar",4),("wheat",6),("egg",2)]:command(f"give MineBotCombat minecraft:{item} {n}")
            time.sleep(0.4)
            call("craft_item", {"item":"cake","count":2})
            assert body.get_state().inventory_counts.get("cake")==2 and body.get_state().inventory_counts.get("bucket")==6
            command("clear MineBotCombat")
            for slot,n in [(0,32),(13,48),(26,16)]:command(f"item replace block 702 150 4 container.{slot} with minecraft:diamond {n}")
            time.sleep(0.4)
            call("read_container", {"pos":[702,150,4],"page_size":9})
            call("transfer_container_item", {"pos":[702,150,4],"item":"diamond","count":96,"direction":"container_to_bot","page_size":9})
            assert body.get_state().inventory_counts.get("diamond")==96
            call("transfer_container_item", {"pos":[702,150,4],"item":"diamond","count":80,"direction":"bot_to_container","page_size":9})
            assert body.get_state().inventory_counts.get("diamond")==16
            check('execute if data block 702 150 4 Items[{id:"minecraft:diamond"}]')
            command("clear MineBotCombat")
            command("give MineBotCombat minecraft:raw_iron 4")
            command("give MineBotCombat minecraft:coal 2")
            time.sleep(0.3)
            call("smelt_item", {"input_item":"raw_iron","count":4,"fuel_item":"coal"})
            assert body.get_state().inventory_counts.get("iron_ingot")==4
            command("item replace block 702 150 6 container.1 with minecraft:coal 3")
            time.sleep(0.3)
            call("clear_furnace", {"pos":[702,150,6]})
            check('execute unless data block 702 150 6 Items[0]')
        elif args.case == "combat":
            fixture(width=48,depth=24)
            command("fill 700 150 0 748 153 0 minecraft:stone")
            command("fill 700 150 24 748 153 24 minecraft:stone")
            command("time set midnight")
            report["task"]="Named moving armored zombie behind a low obstacle; closer decoy; bow/crossbow multiple shots"
            for weapon in ["diamond_sword","bow","crossbow"]:
                command('kill @e[name=BodyCombatTarget]');command('kill @e[name=BodyCombatDecoy]')
                command("clear MineBotCombat")
                command(f"item replace entity MineBotCombat inventory.18 with minecraft:{weapon}")
                command("give MineBotCombat minecraft:arrow 32")
                command("effect give MineBotCombat minecraft:resistance 120 4 true")
                command("tp MineBotCombat 702.5 150 12.5")
                command('summon zombie 722.5 150 12.5 {Silent:1b,CustomName:"BodyCombatTarget",PersistenceRequired:1b}')
                command('summon zombie 704.5 150 16.5 {NoAI:1b,Silent:1b,CustomName:"BodyCombatDecoy",PersistenceRequired:1b}')
                command('item replace entity @e[name=BodyCombatTarget,limit=1] armor.head with minecraft:iron_helmet')
                time.sleep(0.4)
                call("equip_item", {"item":weapon,"target":"mainhand"})
                call("find_hostiles", {"radius":32})
                if weapon=="diamond_sword":
                    call("engage_entity", {"target":"BodyCombatTarget","timeout_s":60})
                    check('execute unless entity @e[name=BodyCombatTarget]')
                else:
                    result=call("ranged_attack", {"target":"BodyCombatTarget","weapon":weapon,"shots":4,"timeout_s":40})
                    hp=command('data get entity @e[name=BodyCombatTarget,limit=1] Health')
                    report["records"][-1]["server_target_health"]=hp
                    assert not hp.endswith(': 20.0f'),hp
                check('execute if data entity @e[name=BodyCombatDecoy,limit=1] {Health:20.0f}')
        elif args.case == "vertical":
            fixture(width=12,depth=12)
            command("fill 700 130 0 712 170 12 minecraft:stone")
            command("fill 706 158 6 706 159 6 minecraft:air")
            command("fill 700 171 0 712 180 12 minecraft:air")
            command("give MineBotCombat minecraft:diamond_pickaxe")
            command("give MineBotCombat minecraft:dirt 64")
            command("tp MineBotCombat 706.5 158 6.5")
            time.sleep(0.5)
            report["task"]="Dig 12 blocks down in a solid shaft, rise 12 with specified dirt, then reach roof surface"
            call("dig_down", {"target_y":146,"max_steps":16})
            check('execute if entity @a[name=MineBotCombat,x=706.5,y=146,z=6.5,distance=..0.2]')
            call("dig_up", {"target_y":158,"max_steps":16,"scaffold_blocks":["dirt"]})
            for y in range(146,158):check(f'execute if block 706 {y} 6 minecraft:dirt')
            call("go_to_surface", {"timeout_s":45})
            check('execute if entity @a[name=MineBotCombat,x=706.5,y=171,z=6.5,distance=..0.8]')
        elif args.case == "building":
            fixture(width=36,depth=24)
            command("give MineBotCombat minecraft:cobblestone 256")
            command("give MineBotCombat minecraft:diamond_pickaxe")
            action("baritoneSettings", {"settings":{"allow_break":True,"allow_place":True}})
            report["task"]="Build an elevated 6x3x4 solid volume, inspect builder, then clear exactly that volume"
            call("build_structure", {"anchor":[708,150,8],"size":[6,3,4],"block":"cobblestone","timeout_s":120})
            for x in range(708,714):
                for y in range(150,153):
                    for z in range(8,12):check(f"execute if block {x} {y} {z} minecraft:cobblestone")
            call("clear_area", {"start":[708,150,8],"end":[713,152,11],"timeout_s":120})
            for x in range(708,714):
                for y in range(150,153):
                    for z in range(8,12):check(f"execute if block {x} {y} {z} minecraft:air")
            check("execute if block 708 149 8 minecraft:stone")
            call("place_block", {"pos":[704,150,3],"block_type":"cobblestone"})
            check("execute if block 704 150 3 minecraft:cobblestone")
            placed=call("place_here", {"block_type":"cobblestone","radius":3})
            report["records"][-1]["server_placed_block"]=command("data get entity MineBotCombat Pos")
        elif args.case == "placement":
            fixture()
            command("give MineBotCombat minecraft:cobblestone 64")
            command("setblock 728 150 12 minecraft:stone")
            time.sleep(0.4)
            report["task"]="Place exact distant blocks, including a side-supported block, then nearby placement; server confirms coordinates"
            for pos,face in [([720,150,4],"up"),([740,150,24],"up"),([729,150,12],"east")]:
                call("place_block", {"pos":pos,"face":face,"block_type":"cobblestone"})
                check(f"execute if block {pos[0]} {pos[1]} {pos[2]} minecraft:cobblestone")
            placed=call("place_here", {"block_type":"cobblestone","radius":3})
            target=placed.metrics.get("target") or placed.metrics.get("target_pos")
            assert target,placed.to_payload()
            check(f"execute if block {target[0]} {target[1]} {target[2]} minecraft:cobblestone")
        elif args.case == "crop_interactions":
            fixture(width=32,depth=24)
            command("give MineBotCombat minecraft:diamond_hoe")
            command("give MineBotCombat minecraft:wheat_seeds 64")
            command("setblock 720 149 12 minecraft:dirt")
            time.sleep(0.4)
            report["task"]="Approach and till distant floor-level soil, sow it, harvest the ripe crop and replant"
            call("till_farmland", {"hoe_item":"diamond_hoe","pos":[720,149,12]})
            check("execute if block 720 149 12 minecraft:farmland")
            call("sow_crop", {"seed_item":"wheat_seeds","farmland_pos":[720,149,12],"expected_crop_block":"wheat"})
            check("execute if block 720 150 12 minecraft:wheat")
            command("setblock 720 150 12 minecraft:wheat[age=7]")
            time.sleep(0.3)
            call("harvest_and_resow", {"farmland_pos":[720,149,12],"timeout_s":30})
            check("execute if block 720 150 12 minecraft:wheat[age=0]")
        elif args.case == "farming":
            fixture(width=32,depth=24)
            command("fill 704 149 4 712 149 12 minecraft:farmland[moisture=7]")
            command("setblock 708 149 8 minecraft:water")
            for x in range(704,713):
                for z in range(4,13):
                    if (x,z)!=(708,8):command(f"setblock {x} 150 {z} minecraft:wheat[age=7]")
            command("give MineBotCombat minecraft:wheat_seeds 128")
            action("baritoneSettings", {"settings":{"allow_break":True,"allow_place":True,"replant_crops":True}})
            report["task"]="Harvest and replant an 80-crop irrigated wheat field with one farmArea call"
            call("farm_area", {"anchor":[708,150,8],"range":12,"timeout_s":120})
            assert body.get_state().inventory_counts.get("wheat",0)>=80
            for x in range(704,713):
                for z in range(4,13):
                    if (x,z)!=(708,8):check(f"execute if block {x} 150 {z} minecraft:wheat")
            command("give MineBotCombat minecraft:diamond_hoe")
            command("setblock 718 149 8 minecraft:dirt")
            time.sleep(0.3)
            call("till_farmland", {"hoe_item":"diamond_hoe","pos":[718,149,8]})
            check("execute if block 718 149 8 minecraft:farmland")
            call("sow_crop", {"seed_item":"wheat_seeds","farmland_pos":[718,149,8],"expected_crop_block":"wheat"})
            check("execute if block 718 150 8 minecraft:wheat")
            command("setblock 718 150 8 minecraft:wheat[age=7]")
            time.sleep(0.3)
            call("harvest_and_resow", {"farmland_pos":[718,149,8],"timeout_s":30})
            check("execute if block 718 150 8 minecraft:wheat[age=0]")
        elif args.case == "interactions":
            fixture(width=36,depth=24)
            command("give MineBotCombat minecraft:bread 8")
            command("give MineBotCombat minecraft:bucket")
            command("give MineBotCombat minecraft:shears")
            command("give MineBotCombat minecraft:flint_and_steel")
            command('kill @e[name=BodyMilkTarget]')
            command('kill @e[name=BodyShearTarget]')
            command('summon cow 710 150 5 {NoAI:1b,CustomName:"BodyMilkTarget"}')
            command('summon sheep 710 150 8 {NoAI:1b,CustomName:"BodyShearTarget"}')
            command("setblock 710 150 12 minecraft:oak_fence_gate")
            command("setblock 716 150 12 minecraft:lever[face=floor]")
            command("setblock 724 150 8 minecraft:red_bed[part=foot,facing=east]")
            command("setblock 725 150 8 minecraft:red_bed[part=head,facing=east]")
            command("effect give MineBotCombat minecraft:hunger 6 200 true")
            until=time.monotonic()+10
            while body.get_state().food>=15:
                assert time.monotonic()<until, "hunger fixture"
                time.sleep(0.1)
            command("effect clear MineBotCombat")
            report["task"]="Eat under hunger, remotely milk/shear selected entities, operate gate/lever, ignite a block and sleep"
            food=body.get_state().food
            call("consume_item", {"item":"bread"})
            assert body.get_state().food>food
            call("discard_item", {"item":"bread","count":3})
            call("use_on_entity", {"item":"bucket","entity_name":"BodyMilkTarget","entity_types":["cow"],"search_radius":24,"watched_items":["milk_bucket"],"required_watched_item_deltas":{"milk_bucket":1}})
            assert body.get_state().inventory_counts.get("milk_bucket")==1
            call("use_on_entity", {"item":"shears","entity_name":"BodyShearTarget","entity_types":["sheep"],"search_radius":24,"max_distance":2})
            check('execute if data entity @e[name=BodyShearTarget,limit=1] {Sheared:1b}')
            call("set_openable_state", {"pos":[710,150,12],"open":True})
            check("execute if block 710 150 12 minecraft:oak_fence_gate[open=true]")
            call("set_openable_state", {"pos":[710,150,12],"open":False})
            check("execute if block 710 150 12 minecraft:oak_fence_gate[open=false]")
            call("set_switch_state", {"pos":[716,150,12],"powered":True})
            check("execute if block 716 150 12 minecraft:lever[powered=true]")
            call("use_on_block", {"item":"flint_and_steel","pos":[716,149,16],"observe_pos":[716,150,16],"expected_block_types":["fire"]})
            check("execute if block 716 150 16 minecraft:fire")
            call("use_item", {"item":"milk_bucket","use_mode":"continuous","use_ticks":40,"watched_items":["milk_bucket"],"required_watched_item_deltas":{"milk_bucket":-1}})
            command("time set midnight")
            call("go_to_bed", {"search_radius":24})
            report["records"][-1]["server_sleeping"]=command('data get entity MineBotCombat SleepTimer')
        elif args.case == "collection":
            fixture(width=40,depth=24)
            command("give MineBotCombat minecraft:diamond_pickaxe")
            command("setblock 708 150 6 minecraft:diamond_ore")
            command("setblock 710 150 6 minecraft:diamond_ore")
            for x in (717,723,729):command(f"fill {x} 150 8 {x+1} 152 9 minecraft:oak_log")
            command("give MineBotCombat minecraft:iron_axe")
            action("baritoneSettings", {"settings":{"allow_break":True,"allow_place":False}})
            time.sleep(0.3)
            report["task"]="Mine a designated ore with an untouched neighboring decoy, collect 18 logs across clusters, pick up six scattered stacks"
            call("get_to_block", {"block_types":["diamond_ore"],"search_radius":24})
            call("mine_block_collect", {"pos":[708,150,6],"expected_drops":["diamond"],"target_block_types":["diamond_ore"]})
            check("execute if block 708 150 6 minecraft:air")
            check("execute if block 710 150 6 minecraft:diamond_ore")
            call("collect_block_domain", {"block_types":["oak_log"],"expected_drops":["oak_log"],"remaining_count":18,"search_radius":40,"candidate_budget":40,"mutation_budget":40,"max_wall_s":120})
            assert body.get_state().inventory_counts.get("oak_log",0)>=18
            for x in (705,709,714,719,724,729):command(f'summon item {x}.5 150 18.5 {{Item:{{id:"minecraft:emerald",count:12}},PickupDelay:0s}}')
            time.sleep(0.4)
            call("pickup_items", {"expected_items":["emerald"],"minimum_count":72,"radius":32,"max_scan_rounds":8,"candidate_budget":12,"max_wall_s":90})
            assert body.get_state().inventory_counts.get("emerald",0)>=72
        elif args.case == "pickup":
            fixture()
            report["task"]="Collect 72 emeralds from six separated drop stacks with a closer non-target stack"
            for x in (705,709,714,719,724,729):command(f'summon item {x}.5 150 18.5 {{Item:{{id:"minecraft:emerald",count:12}},PickupDelay:0s}}')
            command('summon item 706.5 150 5.5 {Item:{id:"minecraft:dirt",count:32},PickupDelay:0s}')
            time.sleep(0.4)
            call("pickup_items", {"expected_items":["emerald"],"minimum_count":72,"radius":32,"max_scan_rounds":8,"candidate_budget":12,"max_wall_s":90})
            assert body.get_state().inventory_counts.get("emerald",0)>=72
            check('execute if data entity MineBotCombat Inventory[{id:"minecraft:emerald",count:64}]')
        elif args.case == "controls":
            fixture()
            report["task"]="Cancel real navigation, mining, builder and vertical work while moving/mutating; prove stopped controls"
            action("navigate", {"goal":{"kind":"block","pos":[757,150,28]},"timeout_s":60},cancel_after=1)
            command("fill 708 150 6 711 156 9 minecraft:oak_log")
            command("give MineBotCombat minecraft:iron_axe")
            time.sleep(0.3)
            action("mineResource", {"block":"oak_log","quantity":100,"timeout_s":60},cancel_after=1)
            command("give MineBotCombat minecraft:cobblestone 256")
            action("buildStructure", {"anchor":[718,150,16],"size":[8,4,8],"block":"cobblestone","timeout_s":60},cancel_after=1)
            action("digUp", {"target_y":175,"max_distance":32,"max_mutations":96,"timeout_s":60},cancel_after=1)
        elif args.case == "exploration":
            fixture(width=60,depth=32)
            command("setblock 726 150 36 minecraft:emerald_block")
            time.sleep(0.3)
            report["task"]="Search multiple regions for a target outside the initial observation radius; run and cancel native exploration"
            found=call("explore_for", {"block_targets":["emerald_block"],"scan_radius":8,"max_distance":256,"max_regions":20})
            assert found.metrics.get("blocks"),found.to_payload()
            action("explore", {"anchor":[730,150,20],"timeout_s":60},cancel_after=2)
        elif args.case == "flight":
            fixture(x=800,width=40,depth=32)
            command("forceload add 800 0 1100 32")
            command("fill 1038 160 6 1062 160 30 minecraft:stone")
            command("item replace entity MineBotCombat armor.chest with minecraft:elytra")
            command("give MineBotCombat minecraft:firework_rocket 64")
            command("tp MineBotCombat 800.5 250 18.5")
            time.sleep(0.3)
            report["task"]="250-block Elytra flight from a drop start to a raised landing platform"
            data=call("elytra_travel", {"target":[1050,161,18],"timeout_s":90}).metrics
            assert data["flight_observed"] and data["on_ground"]
            check("execute if entity @a[name=MineBotCombat,x=1050.5,y=161,z=18.5,distance=..8]")
        elif args.case in {"native_controls", "file_house"}:
            base_x,base_z=(900,40) if args.case=="file_house" else (700,0)
            fixture(x=base_x,z=base_z)
            origin=[base_x+8,150,base_z+8]
            command("give MineBotCombat minecraft:cobblestone 256")
            command("give MineBotCombat minecraft:diamond_pickaxe")
            command("give MineBotCombat minecraft:dirt 64")
            report["task"]="Build an 80-block hollow doorway house from a local Sponge schematic; " + ("without pause" if args.case=="file_house" else "pause/resume and selection transforms")
            action("builderSelectionClear", {})
            action("builderSelectionAdd", {"pos1":origin,"pos2":[origin[0]+4,153,origin[2]+4]})
            action("builderSelectionExpand", {"direction":"up","amount":2})
            action("builderSelectionContract", {"direction":"down","amount":1})
            action("builderSelectionShift", {"direction":"east","amount":2})
            selection=body.perceive("baritoneState", {})
            report["records"][-1]["selection"]=selection.data
            assert selection.data["builder_selections"]==[{"pos1":[origin[0]+2,150,origin[2]],"pos2":[origin[0]+6,154,origin[2]+4]}],selection
            schematic=(root/'house.schem').resolve()
            cells=write_house_schematic(schematic)
            action("baritoneSettings", {"settings":{"build_in_layers":args.case=="native_controls"}})
            if args.case == "file_house":
                call("build_schematic", {"path": str(schematic), "origin": origin, "timeout_s": 90})
            else:
                action("builderBuildFile", {"path":str(schematic),"origin":origin,"name":"body-house"})
            if args.case == "native_controls":
                time.sleep(0.8)
                action("builderPause", {})
                assert body.ipc.state()["builder_paused"]
                time.sleep(0.5)
                action("builderResume", {})
            until=time.monotonic()+90
            while body.ipc.state()["builder_active"]:
                if time.monotonic() >= until:
                    matching=[];missing=[]
                    for y in range(4):
                        for z in range(5):
                            for x in range(5):
                                wanted='cobblestone' if cells[x+5*z+25*y] else 'air'
                                reply=command(f'execute if block {origin[0]+x} {150+y} {origin[2]+z} minecraft:{wanted}')
                                (matching if reply.startswith('Test passed') else missing).append([origin[0]+x,150+y,origin[2]+z,wanted])
                    report["schematic_outcome"]={"matched_cells":len(matching),"missing_cells":missing,
                        "solid_expected":sum(cells),"solid_matched":sum(p[3]=='cobblestone' for p in matching),
                        "process":body.ipc.state(),"pause_used":args.case=="native_controls"}
                    raise AssertionError(f'schematic build did not finish: {report["schematic_outcome"]["solid_matched"]}/{sum(cells)} blocks placed; missing positions recorded')
                time.sleep(0.3)
            for y in range(4):
                for z in range(5):
                    for x in range(5):
                        wanted='cobblestone' if cells[x+5*z+25*y] else 'air'
                        check(f'execute if block {origin[0]+x} {150+y} {origin[2]+z} minecraft:{wanted}')
            inspected=body.perceive("baritoneState", {})
            report["records"][-1]["builder_inspection"]=inspected.data
            action("baritoneSettings", {"settings":{"build_in_layers":False}})
            action("builderSelectionClear", {})
            if args.case == "file_house":
                report.update(success=True,reason="file house completed with all 100 cells independently checked")
                return 0
            filter_file=(root/'explore-filter.json').resolve();filter_file.write_text(json.dumps([{"x":43,"z":0}]))
            action("exploreFilter", {"path":str(filter_file)})
            action("elytraRepack", {})
            action("elytraReset", {})
            action("getToBlockBlacklistClosest", {},expect_success=False)
            command("item replace entity MineBotCombat hotbar.7 with minecraft:carrot 12")
            time.sleep(0.3)
            action("selectSlot", {"slot":7})
            assert body.ipc.state()["selected_item"]=='carrot'
            action("lookAt", {"target":[710,151,10]})
            action("jump", {})
            time.sleep(0.15)
            jumping=body.ipc.state();report["records"][-1]["jump_position"]=jumping["position"]
            assert not jumping["on_ground"],jumping
            time.sleep(0.6)
            action("stop", {})
        elif args.case == "file_cancel":
            fixture()
            command("give MineBotCombat minecraft:cobblestone 256")
            command("give MineBotCombat minecraft:diamond_pickaxe")
            path=(root/"house.schem").resolve()
            write_house_schematic(path)
            report["task"]="Cancel a file builder running independently of a pending Python action; confirm process stop and stable server inventory"
            action("builderBuildFile", {"path":str(path),"origin":[708,150,8],"name":"cancel-house"})
            time.sleep(1)
            assert body.ipc.state()["builder_active"]
            began_cancel=time.monotonic()
            stopped=body.interrupt("developer cancels file builder")
            assert stopped.ok and stopped.data.get("native_process_stop"),stopped
            until=time.monotonic()+3
            while True:
                settled=body.ipc.state()
                if not settled["any_process_active"] and not settled["pathing"]:break
                assert time.monotonic()<until,settled
                time.sleep(0.05)
            cancel_latency_ms=round((time.monotonic()-began_cancel)*1000)
            time.sleep(0.3)
            stock=command("clear MineBotCombat minecraft:cobblestone 0")
            time.sleep(0.7)
            assert command("clear MineBotCombat minecraft:cobblestone 0")==stock
            report["records"].append({"operation":"interrupt independent builder",
                "result":stopped.data,"settled_state":settled,
                "cancel_latency_ms":cancel_latency_ms,"server_stock_after_stop":stock})
        elif args.case == "blacklist":
            fixture()
            command("setblock 710 150 6 minecraft:chest")
            command("setblock 750 150 24 minecraft:chest")
            time.sleep(0.3)
            report["task"]="Blacklist the nearest chest during one getToBlock process; reach the other chest without a replacement goal"
            request=Action.create("getToBlock", {"block":"chest","timeout_s":60})
            assert body.execute(request).accepted
            time.sleep(0.8)
            assert body.ipc.state()["get_active"]
            control=action("getToBlockBlacklistClosest", {})
            assert control.get("get_to_block_blacklist") is True,control
            terminal=body.await_action_terminal(request.id,timeout_s=70)
            report["records"].append({"action":"getToBlock","params":request.params,"terminal":dict(terminal.data)})
            assert terminal.data.get("success"),terminal
            check("execute if entity @a[name=MineBotCombat,x=750.5,y=150,z=24.5,distance=..4.5]")
        elif args.case == "effects":
            fixture(width=32,depth=24)
            command('give MineBotCombat minecraft:potion[minecraft:potion_contents={potion:"minecraft:swiftness"}]')
            command('give MineBotCombat minecraft:milk_bucket')
            command('item replace entity MineBotCombat inventory.20 with minecraft:diamond 19')
            time.sleep(0.3)
            report["task"]="Discard an exact backpack stack; drink potion with a real speed effect; clear that effect with milk"
            call("discard_item", {"item":"diamond","count":19})
            until=time.monotonic()+3
            while True:
                remaining=command("clear MineBotCombat minecraft:diamond 0")
                if "No items" in remaining:break
                assert time.monotonic()<until,remaining
                time.sleep(0.1)
            report["records"][-1]["server_remaining"]=remaining
            check('execute if entity @e[type=item,nbt={Item:{id:"minecraft:diamond",count:19}},x=700,y=149,z=0,dx=3,dy=4,dz=3]')
            call("consume_item", {"item":"potion"})
            assert any(e["id"]=="minecraft:speed" for e in body.get_state().effects),body.get_state()
            call("consume_item", {"item":"milk_bucket"})
            assert body.get_state().effects==[],body.get_state()
        elif args.case == "sleep":
            fixture(width=36,depth=24)
            command("setblock 724 150 8 minecraft:red_bed[part=foot,facing=east]")
            command("setblock 725 150 8 minecraft:red_bed[part=head,facing=east]")
            command("time set midnight")
            time.sleep(0.3)
            report["task"]="Reach a bed 24 blocks away and actually enter sleep at night"
            call("go_to_bed", {"search_radius":32})
            assert body.get_state().sleeping is True
            observed=command('data get entity MineBotCombat SleepTimer')
            report["records"][-1]["server_sleeping"]=observed
            assert not observed.endswith(' 0s'), observed
        elif args.case == "native_misc":
            fixture()
            report["task"]="Native explore filter, Elytra repack/reset, hotbar/look/jump/stop; concrete missing-editor results"
            filter_file=(root/'explore-filter.json').resolve();filter_file.write_text(json.dumps([{"x":43,"z":0}]))
            action("exploreFilter", {"path":str(filter_file)})
            action("elytraRepack", {})
            action("elytraReset", {})
            refused=action("getToBlockBlacklistClosest", {},expect_success=False)
            assert refused["reason"]=="get_to_block_not_active",refused
            refused=action("builderOpenSchematic", {},expect_success=False)
            assert refused["reason"]=="schematica_not_installed",refused
            refused=action("builderOpenLitematic", {"index":0},expect_success=False)
            assert refused["reason"]=="litematica_not_installed",refused
            command("item replace entity MineBotCombat hotbar.7 with minecraft:carrot 12")
            time.sleep(0.3)
            action("selectSlot", {"slot":7})
            assert body.ipc.state()["selected_item"]=='carrot'
            action("lookAt", {"target":[710,151,10]})
            action("jump", {})
            time.sleep(0.15)
            jumping=body.ipc.state();report["records"][-1]["jump_position"]=jumping["position"]
            assert not jumping["on_ground"],jumping
            import re
            server_jump=command("data get entity MineBotCombat Pos")
            report["records"][-1]["server_jump_position"]=server_jump
            values=[float(v) for v in re.findall(r'(-?\d+(?:\.\d+)?)d',server_jump)]
            assert len(values)==3 and values[1]>150.1,server_jump
            time.sleep(0.6)
            action("stop", {})
        elif args.case == "spacing":
            fixture()
            command("tp MineBotCombat 740.5 150 24.5")
            time.sleep(0.3)
            report["task"]="One move_away call reaches a 16-block safety distance, with a second physical distance check"
            call("move_away", {"danger_pos":[742.5,150,24.5],"min_distance":16,"maintenance_checks":2})
            check("execute if entity @a[name=MineBotCombat,x=742.5,y=150,z=24.5,distance=16..]")
        elif args.case == "players":
            fixture()
            command("gamemode survival Receiver")
            command("clear Receiver")
            command("tp Receiver 740.5 150 24.5")
            command("give MineBotCombat minecraft:diamond 96")
            time.sleep(0.5)
            report["task"]="Find/approach a named real player across the arena, follow their movement, and deliver 80 diamonds across stacks"
            call("search_for_entity", {"entity_types":["player"],"entity_name":"Receiver","search_radius":64,"max_distance":64})
            call("go_to_player", {"player_name":"Receiver","search_radius":64,"timeout_s":60})
            call("give_player", {"player_name":"Receiver","item":"diamond","count":80})
            receipt=command("clear Receiver minecraft:diamond 0")
            report["records"][-1]["server_receipt"]=receipt
            assert 'Found 80 ' in receipt,receipt
            sent=command("clear MineBotCombat minecraft:diamond 0")
            report["records"][-1]["server_sender"]=sent
            assert 'Found 16 ' in sent,sent
            command("tp Receiver 710.5 150 4.5")
            time.sleep(0.3)
            call("follow_player", {"player_name":"Receiver","search_radius":64,"timeout_s":60,"maintenance_checks":3})
            check("execute if entity @a[name=MineBotCombat,x=710.5,y=150,z=4.5,distance=..4]")
            command("tp Receiver 742.5 150 24.5")
            time.sleep(0.3)
            # Move the real target during one follow call, rather than issuing
            # new navigation goals on behalf of the Body.
            def receiver_moves():
                time.sleep(2)
                command("tp Receiver 724.5 150 20.5")
                time.sleep(3)
                command("tp Receiver 742.5 150 24.5")
            mover=threading.Thread(target=receiver_moves)
            mover.start()
            call("follow_entity", {"target":"Receiver","keep_distance":3,"timeout_s":18})
            mover.join()
            check("execute if entity @a[name=MineBotCombat,x=742.5,y=150,z=24.5,distance=..4]")
            call("move_away", {"danger_pos":[742.5,150,24.5],"min_distance":16,"maintenance_checks":2})
            check("execute if entity @a[name=MineBotCombat,x=742.5,y=150,z=24.5,distance=16..]")
        else:
            raise AssertionError(f"case not implemented: {args.case}")
        report.update(success=True, reason="task and independent server checks passed")
    except Exception as exc:
        report["reason"] = f"{type(exc).__name__}: {exc}"
        print(report["reason"][:1500], flush=True)
    finally:
        try:
            report["final_state"] = body.ipc.state()
            if report["final_state"].get("any_process_active"):
                if not body.interrupt("directed task ended").ok:
                    action("stop", {})
        except Exception as exc:report["final_state_error"] = str(exc)
        report["elapsed_s"] = round(time.monotonic()-began,3)
        (root/"result.json").write_text(json.dumps(report,indent=2))
        if runtime is not None:
            asyncio.run(runtime.aclose())
        body.close();rcon.close()
        print("PASS" if report["success"] else "FAIL", root/"result.json",flush=True)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
