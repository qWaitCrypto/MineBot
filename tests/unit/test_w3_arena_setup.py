import unittest

from tools.setup_w3_arena import setup_w3_arena


class FakeRcon:
    def __init__(self):
        self.commands: list[str] = []

    def command(self, command_text: str) -> str:
        self.commands.append(command_text)
        if command_text == "script in minebot run minebot_reset()":
            return "true"
        return ""


class W3ArenaSetupTests(unittest.TestCase):
    def test_resource_blocks_are_single_layer_bands_for_deterministic_g2_sequence(self):
        rcon = FakeRcon()

        arena = setup_w3_arena(rcon)

        setblocks = [command for command in rcon.commands if command.startswith("setblock ")]
        stone = _positions_for(setblocks, "stone")
        iron = _positions_for(setblocks, "iron_ore")
        diamond = _positions_for(setblocks, "deepslate_diamond_ore")
        self.assertEqual(len(stone), 16)
        self.assertEqual(len(iron), 6)
        self.assertEqual(len(diamond), 6)
        self.assertEqual({pos[1] for pos in stone}, {70})
        self.assertEqual({pos[1] for pos in iron}, {70})
        self.assertEqual({pos[1] for pos in diamond}, {70})
        self.assertEqual(arena["stone_count"], 16)


def _positions_for(commands: list[str], block: str) -> list[tuple[int, int, int]]:
    positions: list[tuple[int, int, int]] = []
    for command in commands:
        parts = command.split()
        if parts[-1] != block:
            continue
        positions.append((int(parts[1]), int(parts[2]), int(parts[3])))
    return positions


if __name__ == "__main__":
    unittest.main()
