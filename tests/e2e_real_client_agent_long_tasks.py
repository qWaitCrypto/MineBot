"""Three opt-in, visible real-model tasks; no scripted Agent tool sequence."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import nbtlib

from minebot.app.local_launcher import load_runtime_environment
from minebot.game.rcon import RconClient, RconConfig
from minebot.game.real_client_ipc import RealClientIpc


BOT = "MineBotCombat"
LIMITS = {"resources": 1800, "camp": 2700, "logistics": 3600}
TUPLE = "MC 26.1.2 / Fabric 0.19.3 / API 0.152.1 / Java 25 / Baritone 1.18.0"


def position(rcon):
    raw = rcon.command(f"data get entity {BOT} Pos")
    values = re.search(r"\[([^]]+)\]", raw)
    if values is None:
        raise RuntimeError(f"server position unavailable: {raw}")
    return [float(v.strip().rstrip("dDfF")) for v in values[1].split(",")]


def contents(rcon, target):
    raw = rcon.command("data get " + target)
    try:
        entries = nbtlib.parse_nbt(raw.split(": ", 1)[1])
        counts = {}
        for entry in entries:
            item = str(entry["id"]).removeprefix("minecraft:")
            counts[item] = counts.get(item, 0) + int(entry.get("count", entry.get("Count", 0)))
        return counts
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise RuntimeError(f"cannot read server inventory: {raw}") from error


def prepare(case, rcon, report):
    commands = report["fixture_commands"] = []

    def cmd(text):
        reply = rcon.command(text)
        commands.append({"command": text, "reply": reply})
        if any(term in reply for term in ("Incorrect argument", "Unknown or incomplete", "not loaded")):
            raise RuntimeError(f"fixture command failed: {text}: {reply}")
        return reply

    def ground(x, z):
        cmd(f"forceload add {x-16} {z-16} {x+16} {z+16}")
        report.setdefault("forced_regions", []).append([x-16, z-16, x+16, z+16])
        time.sleep(0.5)
        cmd(f"execute positioned {x} 200 {z} positioned over motion_blocking_no_leaves run tp {BOT} ~ ~1 ~")
        time.sleep(1)
        return [x, math.ceil(position(rcon)[1]), z]

    def give(item, count):
        cmd(f"give {BOT} minecraft:{item} {count}")

    report["seed"] = rcon.command("seed")
    report["previous_difficulty"] = rcon.command("difficulty")
    cmd("difficulty peaceful")
    cmd(f"gamemode survival {BOT}")
    cmd(f"clear {BOT}")
    cells, inventory = [], {}
    if case == "resources":
        home = ground(640, 1440)
        give("bread", 24)
        inventory = {"oak_log": 32, "cobblestone": 128, "iron_ingot": 16,
                     "iron_pickaxe": 1, "torch": 32, "crafting_table": 1, "furnace": 1}
        goal = (f"在这片自然森林从零建立资源生产能力，起点是 {home}。"
                "只有24个面包，没有工具或预置矿石。最终背包中必须同时保留："
                "32橡木原木、128圆石、16铁锭、一把铁镐、32火把、一个工作台、一个熔炉。"
                "工具和合成消耗不计入这些最终余量。全部准备好后带回起点8格以内并汇报实际数量。"
                "可以自由采集自然资源、合成、熔炼，自己选择地点和方法。遇到失败先考虑可执行的替代办法；"
                "不要原样重复已经失败的操作。不得拆毁玩家或旧实验建筑。30分钟是上限，完成就结束。")
        conditions = "natural forest; only 24 bread; no supplied tools/ores; peaceful; known starting coordinate"
    elif case == "camp":
        home = [1204, 180, 1444]
        ground(1216, 1456)
        cmd("fill 1200 178 1440 1239 179 1471 minecraft:dirt")
        cmd("fill 1200 180 1440 1239 187 1471 minecraft:air")
        for item, count in {"bread": 32, "oak_log": 96, "cobblestone": 64, "glass": 16,
                            "wheat_seeds": 64, "water_bucket": 2, "iron_axe": 1,
                            "iron_pickaxe": 1, "iron_shovel": 1, "iron_hoe": 1}.items():
            give(item, count)
        for x in range(1210, 1219):
            for z in range(1450, 1457):
                cells.extend([([x, 180, z], "oak_planks"), ([x, 184, z], "oak_planks")])
                for y in range(181, 184):
                    wall = x in (1210, 1218) or z in (1450, 1456)
                    block = "oak_planks" if wall else "air"
                    if x == 1214 and z == 1450 and y in (181, 182):
                        block = "air"
                    if x in (1210, 1218) and z in (1453, 1454) and y == 182:
                        block = "glass"
                    if y == 181 and z == 1452 and x in (1212, 1213, 1214):
                        block = {1212: "crafting_table", 1213: "furnace", 1214: "chest"}[x]
                    cells.append(([x, y, z], block))
        for x in range(1224, 1232):
            for z in range(1452, 1456):
                cells.extend([([x, 179, z], "farmland[moisture=7]"), ([x, 180, z], "wheat")])
        goal = ("在这块供料实验地建营地，45分钟上限。木屋范围 x=1210..1218,z=1450..1456，"
                "y=180整层橡木木板地板，y=184整层木板平屋顶；四周y=181..183木板墙，内部保持空气。"
                "入口在(1214,181,1450)和其上方，两格留空；玻璃窗在x=1210和1218、z=1453和1454、y=182，共四块。"
                "屋内y=181,z=1452，x=1212放工作台、1213放熔炉、1214放箱子。"
                "相邻田地x=1224..1231,z=1452..1455，y=179耕地、y=180种小麦，共32株；"
                "自行布置灌溉，结束时每块耕地必须湿润。已经给了原材料、工具、种子和两个水桶。"
                "自己决定施工顺序和工具用法，建成后检查屋内能进入、结构和田地实际完成。不要只宣称命令已接受。")
        conditions = "supplied materials/tools; empty dirt plot above terrain; exact house dimensions; 32 planted hydrated crops, not mature harvest"
    else:
        home = ground(1600, 1440)
        stations = []
        cargo = [{"coal": 128}, {"iron_ingot": 64}, {"wheat": 64, "cobblestone": 128}]
        for (x, z), goods in zip(((2400, 1440), (2600, 2240), (1800, 2600)), cargo):
            at = ground(x, z)
            y = at[1]
            cmd(f"fill {x-3} {y-1} {z-3} {x+3} {y-1} {z+3} minecraft:stone")
            cmd(f"fill {x-3} {y} {z-3} {x+3} {y+4} {z+3} minecraft:air")
            cmd(f"setblock {x} {y} {z} minecraft:chest")
            items = []
            for item, count in goods.items():
                while count:
                    n = min(64, count)
                    items.append('{Slot:%db,id:"minecraft:%s",count:%d}' % (len(items), item, n))
                    count -= n
            cmd(f"data modify block {x} {y} {z} Items set value [" + ",".join(items) + "]")
            stations.append({"position": at, "cargo": goods})
            for dy in range(4):
                cells.append(([x+2, y+dy, z+2], "oak_planks"))
        x, y, z = home
        cmd(f"fill {x-3} {y-1} {z-3} {x+3} {y-1} {z+3} minecraft:stone")
        cmd(f"fill {x-3} {y} {z-3} {x+3} {y+3} {z+3} minecraft:air")
        cmd(f"setblock {x} {y} {z} minecraft:chest")
        for item, count in {"bread": 64, "iron_pickaxe": 1, "iron_axe": 1, "oak_planks": 64}.items():
            give(item, count)
        report["stations"] = stations
        inventory = {"coal": 128, "iron_ingot": 64, "wheat": 64, "cobblestone": 128}
        goal = (f"完成跨区运输，60分钟上限。基地空箱坐标 {home}，补给点和箱内货物为 {stations}。"
                "把三点所有这些货物运回基地箱中，自己选择跨自然地形的路线和取货次序。"
                "每个补给点在箱子x+2,z+2处，从箱子同一y开始立四格高橡木木板路标。"
                "不要破坏补给箱。已给口粮、铁工具和路标木板。货物必须实际进入基地箱，不是只在你的背包里。"
                "最终本人回到基地箱8格内，检查数量后汇报。")
        conditions = "known supply coordinates; supplied cargo/tools/food; natural terrain between four small prepared pads; no prescribed route"
    cmd(f"tp {BOT} {home[0]+0.5} {home[1]} {home[2]+0.5}")
    cmd(f"spawnpoint {BOT} {home[0]} {home[1]} {home[2]}")
    time.sleep(1)
    report.update(home=home, goal=goal, conditions=conditions, expected_inventory=inventory, expected_cells=cells)
    return goal


def verify(rcon, report):
    case, home = report["case"], report["home"]
    target = f"block {' '.join(map(str, home))} Items" if case == "logistics" else f"entity {BOT} Inventory"
    observed = contents(rcon, target)
    missing = {k: {"required": n, "actual": observed.get(k, 0)}
               for k, n in report["expected_inventory"].items() if observed.get(k, 0) < n}
    wrong = []
    for xyz, block in report["expected_cells"]:
        reply = rcon.command(f"execute if block {' '.join(map(str, xyz))} minecraft:{block}")
        if "Test passed" not in reply:
            wrong.append({"position": xyz, "expected": block, "reply": reply})
    pos = position(rcon)
    distance = math.dist(pos, home)
    result = {"inventory": observed, "missing_inventory": missing, "wrong_cells": wrong,
              "position": pos, "distance_from_home": round(distance, 3)}
    return not missing and not wrong and (case == "camp" or distance <= 8), result


def check_cancel(process, rcon, out, home, send):
    trace = out / "trace.jsonl"
    cursor = trace.stat().st_size
    start = position(rcon)
    send(f"/goal 走到 ({home[0]+200}, {home[1]}, {home[2]})，现在开始。")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Agent exited before cancellation test")
        if math.dist(position(rcon), start) > 0.5:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("no physical movement within 120s for the separate cancellation task")
    send("/cancel 明确取消这次远行，停止移动。")
    receipt = None
    while time.monotonic() < deadline:
        with trace.open() as stream:
            stream.seek(cursor)
            for line in stream:
                if not line.endswith("\n"):
                    continue
                event = json.loads(line)
                if event.get("event") == "execution_cancelled" and event.get("settled"):
                    receipt = event
        if receipt:
            break
        time.sleep(0.2)
    if receipt is None:
        raise RuntimeError("explicit cancel did not produce a settled receipt")
    first = position(rcon)
    time.sleep(1)
    second = position(rcon)
    if math.dist(first, second) > 0.2:
        raise RuntimeError(f"position continued changing after cancellation: {first} -> {second}")
    return {"success": True, "receipt": receipt, "position_samples": [first, second]}


def run_case(args, case):
    out = args.output_dir / case
    out.mkdir(parents=True, exist_ok=True)
    report = {"case": case, "success": False, "evidence": "real_model_agent_task", "tuple": TUPLE,
              "model": args.model, "messages": [], "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    rcon = RconClient(RconConfig(password=os.environ.get("MINEBOT_TEST_RCON_PASSWORD", "test"),
                                 response_drain_timeout_s=0.01))
    process = None
    started = time.monotonic()
    log = (out / "agent.log").open("w")

    def save():
        (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def send(message):
        process.stdin.write(message + "\n")
        process.stdin.flush()
        report["messages"].append({"elapsed_s": round(time.monotonic()-started, 3), "text": message})

    try:
        with RealClientIpc(timeout_s=10) as ipc:
            initial = ipc.state()
            if not initial.get("ready") or not initial.get("disposable_world") or initial.get("any_process_active"):
                raise RuntimeError("client must be ready, idle and explicitly disposable before setup")
        goal = prepare(case, rcon, report)
        with RealClientIpc(timeout_s=10) as ipc:
            report["initial_body_state"] = ipc.state()
        environment = load_runtime_environment(args.env_file)
        environment = {k: v for k, v in environment.items() if "RCON" not in k.upper()}
        region = "384,-64,1184,896,320,1696" if case == "resources" else (
            "1190,170,1430,1248,200,1480" if case == "camp" else "1400,-64,1200,2800,320,2800")
        environment.update(MINEBOT_BODY_PROVIDER="real_client", MINEBOT_REAL_BOT=BOT,
                           MINEBOT_REAL_CLIENT_HOST="127.0.0.1", MINEBOT_REAL_CLIENT_PORT="8768",
                           MINEBOT_AGENT_KERNEL="responses", MINEBOT_LLM_KIND="openai_responses",
                           MINEBOT_RESPONSES_TRANSPORT="http", MINEBOT_LLM_MODEL=args.model,
                           MINEBOT_REAL_NATURAL_REGION=region, MINEBOT_AGENT_LANGUAGE="zh",
                           MINEBOT_AGENT_LOG_PATH=str(out / "trace.jsonl"),
                           MINEBOT_AGENT_STATE_DB=str(out / "state.sqlite3"))
        started = time.monotonic()
        limit = args.timeout or LIMITS[case]
        report.update(status="running", limit_s=limit)
        process = subprocess.Popen([sys.executable, "-m", "minebot.app.real_server_session", goal,
                                    "--interactive", "--max-steps", "1000"], env=environment,
                                   stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True)
        report["agent_pid"] = process.pid
        save()
        print(f"START {case} pid={process.pid} limit={limit}s report={out / 'result.json'}", flush=True)
        cursor, outcome, latest, last_sample = 0, None, [], -60
        fatal = None
        sent_chat = sent_append = False
        while time.monotonic()-started < limit:
            elapsed = time.monotonic()-started
            trace = out / "trace.jsonl"
            if trace.exists():
                with trace.open() as stream:
                    stream.seek(cursor)
                    while True:
                        before = stream.tell()
                        line = stream.readline()
                        if not line or not line.endswith("\n"):
                            cursor = before
                            break
                        event = json.loads(line)
                        if event.get("event") in {"tool_invoke", "tool_result", "responses_failed", "session_step_failed"}:
                            latest.append({k: event[k] for k in (
                                "seq", "ts", "event", "tool", "tool_call_id", "arguments_summary",
                                "success", "reason", "error_type", "error_message", "message") if k in event})
                            latest = latest[-8:]
                        if event.get("event") == "task_outcome":
                            outcome = event
                        if event.get("event") == "model_transport_recovery_exhausted":
                            fatal = event
            if outcome is not None or fatal is not None or process.poll() is not None:
                break
            if elapsed >= 180 and not sent_chat:
                send("现在做到哪了？简单说一下就好，继续做原来的任务。")
                sent_chat = True
            if elapsed >= 300 and not sent_append:
                if case == "resources":
                    send("追加一个要求：结束时背包里的熔炉要保留4个，其他要求都保留。")
                    report["expected_inventory"]["furnace"] = 4
                elif case == "logistics":
                    send("追加：结束汇报时分别说清三个补给点实际取走了什么，别只报总数。")
                sent_append = True
            if elapsed-last_sample >= 60:
                snapshot = {"elapsed_s": round(elapsed, 1), "position": position(rcon),
                            "inventory": contents(rcon, f"entity {BOT} Inventory"), "recent_events": latest}
                with (out / "observations.jsonl").open("a") as stream:
                    stream.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
                report.update(elapsed_s=snapshot["elapsed_s"], latest=snapshot)
                save()
                print(f"PROGRESS {case} {elapsed:.0f}s pos={snapshot['position']} inventory={snapshot['inventory']}", flush=True)
                last_sample = elapsed
            time.sleep(1)
        report["outcome"] = outcome
        actual, report["server_checks"] = verify(rcon, report)
        report["success"] = bool(outcome and outcome.get("status") == "complete" and actual)
        if report["success"]:
            report["reason"] = "model completion and actual task results agree"
        elif fatal:
            report["failure_event"] = fatal
            report["reason"] = f"model connection recovery exhausted: {fatal}"
        elif outcome:
            report["reason"] = f"model outcome={outcome.get('status')}: {outcome.get('summary')}; actual checks={actual}"
        elif process.poll() is not None:
            report["reason"] = f"Agent exited {process.returncode} before task completion; inspect recorded recent events and agent.log"
        else:
            report["reason"] = f"time limit {limit}s reached without completing the task"
        if case == "logistics" and report["success"]:
            report["transport_success"] = True
            report["cancellation"] = check_cancel(process, rcon, out, report["home"], send)
    except (Exception, KeyboardInterrupt) as error:
        report.update(success=False, reason=f"{type(error).__name__}: {error}")
        if isinstance(error, KeyboardInterrupt):
            args.operator_stopped = True
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    send("/cancel long-task experiment ended")
                    time.sleep(2)
                    send("/quit long-task experiment ended")
                    process.wait(timeout=20)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            process.stdin.close()
            report["agent_exit_code"] = process.returncode
        try:
            with RealClientIpc(timeout_s=10) as ipc:
                final = ipc.state()
                if final.get("any_process_active"):
                    ipc.control("primitive", action_name="stop", params={})
                    final = ipc.state()
                report["final_body_state"] = final
                if final.get("any_process_active"):
                    report.update(success=False, reason="physical action still active after experiment cleanup")
            for low_x, low_z, high_x, high_z in report.get("forced_regions", []):
                rcon.command(f"forceload remove {low_x} {low_z} {high_x} {high_z}")
        except Exception as error:
            report.update(success=False, cleanup_error=f"{type(error).__name__}: {error}")
        report.update(status="finished", elapsed_s=round(time.monotonic()-started, 3))
        save()
        log.close()
        rcon.close()
        print(f"{'PASS' if report['success'] else 'FAIL'} {case}: {report.get('reason')}", flush=True)
    return report["success"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-local-server", required=True, action="store_true")
    parser.add_argument("--case", choices=("all", *LIMITS), action="append", help="Repeat to select a sequence; default: all three")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--timeout", type=float, help="Override a case time limit, for an explicitly directed replay")
    parser.add_argument("--output-dir", type=Path, default=Path("logs") / f"visible-agent-{time.time_ns()}")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt("operator stop")))
    cases = list(LIMITS) if not args.case or "all" in args.case else args.case
    results = []
    for case in cases:
        results.append(run_case(args, case))
        if getattr(args, "operator_stopped", False):
            break
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
