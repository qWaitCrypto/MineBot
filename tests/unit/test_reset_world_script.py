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
        self.assertIn('SCRIPT_ASSETS="$ROOT/assets/carpet/scripts"', source)

    def test_reset_load_check_requires_current_event_head_protocol(self):
        source = RESET_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("minebot_event_head('ResetProbe', 'reset-world')", source)
        self.assertIn("'\"type\":\"result\"' not in event_head", source)


if __name__ == "__main__":
    unittest.main()
