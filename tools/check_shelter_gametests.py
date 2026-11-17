#!/usr/bin/env python3
"""Run the shelter client scenes, then remove one production statement per required scene.

Only assertion failures in the selected scene count as killing a mutation.
Compilation/startup/IPC failures never count. Production source is restored in finally.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "minecraft/client/body/src/main/java/dev/minebot/client/body/MineBotClientBody.java"
MUTATIONS = {
    "shelterSealsAgainstZombie": "                current.shelterPlan.add(current.anchor.relative(side));\n",
    "shelterRefusesInsufficientBlocks": '                requestStop("failed", "not_enough_blocks");\n',
    "shelterHandlesOccupiedWallTruthfully": "            client.gameMode.attack(client.player, mob); // at most ONE normal melee hit for this cell\n",
    "shelterFillsOnlyTunnelOpenings": (
        "            while (current.shelterIndex < current.shelterPlan.size()\n"
        "                && shelterSolid(client, current.shelterPlan.get(current.shelterIndex))) current.shelterIndex++;\n"
    ),
    "shelterLookOutClosesHeadPort": "            client.gameMode.continueDestroyBlock(current.shelterTarget, Direction.UP);\n",
}


def run(case: str, log: Path) -> tuple[int, str]:
    env = {**os.environ, "LIBGL_ALWAYS_SOFTWARE": "1", "MINEBOT_GAMETEST_CASE": case}
    with log.open("w") as output:
        result = subprocess.run([
            "timeout", "1800", "xvfb-run", "-a", "-s", "-screen 0 1280x720x24",
            "./minecraft/gradlew", "-p", "minecraft", "--offline",
            ":client:body:runClientGameTest", "-x", "buildPatchedBaritone",
        ], cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
    return result.returncode, log.read_text()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--log-dir", type=Path)
    args = parser.parse_args()
    logs = args.log_dir or Path(tempfile.mkdtemp(prefix="minebot-shelter-proof-"))
    logs.mkdir(parents=True, exist_ok=True)
    original = SOURCE.read_text()
    for statement in MUTATIONS.values():
        if original.count(statement) != 1:
            raise SystemExit("Mutation statement is missing or ambiguous; production source was not changed.")
    shutil.copytree(Path.home() / "MineBot/minecraft/client/body/build/baritone",
                    ROOT / "minecraft/client/body/build/baritone", dirs_exist_ok=True)
    code, output = run("shelter", logs / "baseline.log")
    if code != 0 or not all(f"MINEBOT_SHELTER PASS {case} " in output for case in MUTATIONS):
        print(f"Baseline did not pass; no mutations were attempted. See {logs / 'baseline.log'}")
        return 1
    print(f"Baseline: all shelter scenes passed. Logs: {logs}")
    if args.baseline_only:
        return 0
    try:
        for case, statement in MUTATIONS.items():
            # Always mutate the baseline, never the preceding mutant.
            SOURCE.write_text(original.replace(statement, "", 1))
            code, output = run(case, logs / f"{case}-removed.log")
            if code == 0 or "AssertionError" not in output or case not in output:
                print(f"No verified assertion failure for {case}; inspect {logs / (case + '-removed.log')}")
                return 1
            print(f"MINEBOT_SHELTER MUTATION_KILLED {case}")
    finally:
        SOURCE.write_text(original)
    # Rebuild and run restored source: build outputs must not retain the last mutant.
    code, output = run("shelter", logs / "restored.log")
    if code != 0 or not all(f"MINEBOT_SHELTER PASS {case} " in output for case in MUTATIONS):
        print(f"Restored baseline did not pass; inspect {logs / 'restored.log'}")
        return 1
    print("MINEBOT_SHELTER RESTORED all_required_scenes_passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
