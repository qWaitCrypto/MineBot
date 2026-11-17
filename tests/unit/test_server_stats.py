import json

from tools.eval.server_stats import read_stats


def test_reads_completion_and_progress_counters_from_stats_file(tmp_path):
    path = tmp_path / "player.json"
    path.write_text(json.dumps({"stats": {
        "minecraft:killed": {"minecraft:ender_dragon": 1},
        "minecraft:custom": {"minecraft:deaths": 2, "minecraft:play_time": 900, "minecraft:mob_kills": 8},
        "minecraft:picked_up": {"minecraft:ender_eye": 3},
        "minecraft:crafted": {"minecraft:ender_eye": 4},
        "minecraft:used": {"minecraft:ender_eye": 1},
    }}))
    result = read_stats(path)
    assert result["minecraft:killed/minecraft:ender_dragon"] == 1
    assert result["minecraft:custom/minecraft:deaths"] == 2
    assert result["minecraft:used/minecraft:ender_eye"] == 1
