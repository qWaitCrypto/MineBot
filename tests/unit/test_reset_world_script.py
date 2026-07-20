import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RESET_SCRIPT = ROOT / "tools" / "reset-world.sh"


class ResetWorldScriptTests(unittest.TestCase):
    def test_current_scarpet_assets_override_golden_world_scripts(self):
        source = RESET_SCRIPT.read_text(encoding="utf-8")

        golden_copy = source.index('cp -a "$GOLDEN" "$WORLD"')
        asset_copy = source.index('cp -a "$SCRIPT_ASSETS/." "$WORLD/scripts/"')
        self.assertGreater(asset_copy, golden_copy)
        self.assertIn('SCRIPT_ASSETS="$ROOT/minecraft/server/scarpet"', source)

    def test_reset_load_check_requires_current_event_head_protocol(self):
        source = RESET_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("minebot_event_head('ResetProbe', 'reset-world')", source)
        self.assertIn("'\"type\":\"result\"' not in event_head", source)

    def test_camera_config_is_forwarded_to_server_bridge_startup(self):
        source = RESET_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("from minebot.camera.config import discover_camera_config_path", source)
        self.assertIn('PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"', source)
        self.assertIn('PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - "$CAMERA_CONFIG"', source)
        self.assertIn("from minebot.camera.config import load_camera_config", source)
        self.assertIn("-Dminebot.bridge.host=", source)
        self.assertIn("-Dminebot.bridge.port=", source)
        self.assertIn("-Dminebot.camera.observerUuid=", source)
        self.assertIn('java "${SERVER_JAVA_ARGS[@]}" -jar fabric-server-launch.jar nogui', source)

    def test_reset_preserves_fixed_world_passive_entities(self):
        source = RESET_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('r.command("kill @e[type=minecraft:item]")', source)
        self.assertNotIn("type=!minecraft:player,type=!minecraft:item", source)
        self.assertIn("for entity_type in hostile_types:", source)


if __name__ == "__main__":
    unittest.main()
