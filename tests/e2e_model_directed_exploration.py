"""Developer-directed tool proof on one independent disposable-world client.

No model calls or house-Agent restarts. Fixtures are placed at an isolated test
site. Success here proves tool execution, not autonomous unknown-world search.
"""
import argparse
import json
import math
from pathlib import Path
import threading
import time

from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.contract import ExecutionCancellation, ExecutionCancelled, Region, execution_cancellation_scope
from minebot.game.rcon import RconClient, RconConfig
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientIpc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-local-server", action="store_true", required=True)
    parser.add_argument("--port", type=int, default=8773)
    parser.add_argument("--bot", default="ExploreProbe")
    parser.add_argument("--natural-only", action="store_true", help="Only traverse an observed natural surface, with no terrain fixture")
    parser.add_argument("--output", type=Path, default=Path("logs/exploration-20261008/result.json"))
    args = parser.parse_args()
    report = {"success": False, "model_calls": 0, "cases": [], "conditions":
        "independent role; supplied grass platform, ore and immobile sheep at (4096,180,4096); no house intervention; Baritone 1.18.0 / MC26.1.2"}
    body = RealClientBody(args.bot, RealClientIpc(port=args.port, timeout_s=15))
    site = Region("exploration-fixture", (4088,179,4088), (4300,190,4180))
    registry = build_phase1_registry(body, Phase1RuntimeConfig(site, body_provider="real_client"))
    try:
        assert body.perceive("baritoneState", {}).data.get("disposable_world") is True
        with RconClient(RconConfig(port=25576, password="test")) as rcon:
            def command(text):
                return rcon.command(text)

            def call(name, params):
                started = time.monotonic()
                tool = registry.get(name)
                result = tool.callable(params)
                report["cases"].append({"tool": name, "params": params, "elapsed_s": round(time.monotonic()-started,3),
                    "result": result.to_payload(), "model_projection": tool.projector(result.reason, result.metrics),
                    "server_position": command(f"data get entity {args.bot} Pos"),
                    "idle": body.get_state().pending_action_count == 0})
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                return result

            def reset():
                command(f"tp {args.bot} 4096.5 180 4096.5")
                command(f"gamemode survival {args.bot}")
                time.sleep(1)
                assert abs(body.get_state().pos[1]-180) < 0.2, "Fixture ground was not ready after teleport"

            if args.natural_only:
                report["conditions"] = "independent role teleported onto natural terrain; developer selects one observed stand; no terrain or resources supplied; no model"
                command(f"spreadplayers 4400 4096 0 16 false {args.bot}")
                candidates = []
                for _ in range(20):
                    raw = body.perceive("environment", {"radius": 48}).data
                    candidates = [s["surface_pos"] for s in raw["samples"] if s.get("standable")
                                  and 28 <= math.dist((s["xz"][0],s["xz"][1]), (raw["pos"][0],raw["pos"][2])) <= 60
                                  and abs(s["surface_pos"][1]-raw["pos"][1]) >= 3]
                    if candidates:
                        break
                    time.sleep(0.5)
                call("observe_environment", {"radius": 48})
                assert candidates, "No observed natural elevation-change candidate in the sample"
                target = min(candidates, key=lambda pos: math.dist(pos, raw["pos"]))
                result = call("explore_for", {"destination": [target[0],target[2]], "space": "surface"})
                report["observed_destination"] = target
                assert result.reason == "route_complete" and abs(result.metrics["final_pos"][1]-target[1]) <= 2
                report["success"] = True
                return

            # This site belongs to this opt-in disposable fixture, away from the house.
            report["forceload"] = command("forceload add 4088 4088 4300 4180")
            time.sleep(3)
            command("fill 4088 179 4088 4300 179 4180 minecraft:grass_block")
            for z in range(4088, 4181, 24):
                command(f"fill 4088 180 {z} 4300 184 {min(z+23,4180)} minecraft:air")
            command("kill @e[type=minecraft:sheep,tag=exploration_probe]")
            command("forceload remove 4088 4088 4300 4180")
            reset()
            environment = call("observe_environment", {"radius": 32})
            assert environment.success and environment.metrics["space"] == "surface"
            assert environment.metrics["difficulty"] == "peaceful"
            result = call("explore_for", {"direction": "east", "distance": 96, "max_distance": 256})
            assert result.reason == "route_complete" and result.metrics["net_displacement"] >= 90
            assert not result.metrics["target_found"]

            reset()
            command("setblock 4156 180 4096 minecraft:iron_ore")
            result = call("explore_for", {"destination": [4230,4096], "block_targets": ["iron_ore"], "scan_radius": 8})
            assert result.reason == "found" and result.metrics["final_pos"][0] < 4170
            assert result.metrics["net_displacement"] > 30
            assert report["cases"][-1]["idle"]
            command("setblock 4156 180 4096 minecraft:air")

            reset()
            command('summon minecraft:sheep 4176 180 4096 {NoAI:1b,PersistenceRequired:1b,Tags:["exploration_probe"]}')
            result = call("explore_for", {"destination": [4230,4096], "entity_targets": ["sheep"]})
            assert result.reason == "found" and result.metrics["final_pos"][0] < 4190
            assert result.metrics["entities"] and report["cases"][-1]["idle"]

            reset()
            result = call("explore_for", {"region": {"min": [4096,4104], "max": [4112,4120]},
                                          "return_policy": "region_budget"})
            assert result.reason == "route_complete" and result.metrics["scan_count"] >= 4

            reset()
            token = ExecutionCancellation()
            timer = threading.Timer(2, lambda: token.cancel("probe_cancel"))
            timer.start()
            before = time.monotonic()
            try:
                with execution_cancellation_scope(token):
                    registry.get("explore_for").callable({"direction": "east", "distance": 1000, "max_distance": 1600})
                raise AssertionError("Cancellation did not end the search")
            except ExecutionCancelled:
                pass
            finally:
                timer.cancel()
            stopped = body.get_state()
            time.sleep(0.6)
            after = body.get_state()
            assert stopped.pending_action_count == after.pending_action_count == 0
            assert math.dist(stopped.pos, after.pos) < 0.15
            report["cases"].append({"tool": "explore_for", "case": "cancel", "cancel_settlement_ms":
                round((time.monotonic()-before-2-0.6)*1000), "stopped_pos": stopped.pos, "later_pos": after.pos, "idle": True})
            report["history"] = call("observe_environment", {}).metrics
            command("kill @e[type=minecraft:sheep,tag=exploration_probe]")
            report["success"] = True
    except Exception as error:
        report["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        body.interrupt("probe_exit")
        body.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({"success": report["success"], "cases": len(report["cases"]), "failure": report.get("failure")}))


if __name__ == "__main__":
    main()
