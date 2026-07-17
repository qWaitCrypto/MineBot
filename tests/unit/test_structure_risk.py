import unittest

from minebot.body.structure_risk import VoxelStructureRiskAssessor
from minebot.contract import BreakContext, PerceptionResult, StructureRiskLevel


class VoxelBody:
    bot_name = "Bot1"

    def __init__(self, blocks=None, *, fail=False):
        self.blocks = dict(blocks or {})
        self.fail = fail
        self.calls = []

    def perceive(self, scope, params):
        self.calls.append((scope, dict(params)))
        if self.fail:
            raise ValueError("world unavailable")
        if scope != "blockCells":
            raise AssertionError(scope)
        cells = []
        for raw in params["cells"]:
            pos = tuple(int(value) for value in raw)
            block_type = self.blocks.get(pos, "air")
            cells.append(
                {
                    "x": pos[0],
                    "y": pos[1],
                    "z": pos[2],
                    "type": block_type,
                    "state": "CLEAR" if block_type == "air" else "SOLID",
                    "properties": {},
                }
            )
        return PerceptionResult(
            bot=self.bot_name,
            scope=scope,
            type="perception",
            ok=True,
            complete=True,
            data={"cells": cells, "next": None},
            uncertainty=[],
            next=None,
            error=None,
        )


class StructureRiskTests(unittest.TestCase):
    def test_rooted_vertical_log_is_low_risk_natural_tree_evidence(self):
        blocks = {
            (0, 61, 0): "dirt",
            (0, 62, 0): "oak_log",
            (0, 63, 0): "oak_log",
            (0, 64, 0): "oak_log",
            (0, 65, 0): "oak_log",
            (0, 66, 0): "oak_log",
            (1, 66, 0): "oak_leaves",
        }
        body = VoxelBody(blocks)

        assessment = VoxelStructureRiskAssessor(body).assess(
            (0, 64, 0),
            "oak_log",
            BreakContext.COLLECT,
        )

        self.assertEqual(assessment.level, StructureRiskLevel.LOW)
        self.assertTrue(assessment.complete)
        self.assertIn("rooted_vertical_log", assessment.signals)
        self.assertTrue(all(scope == "blockCells" for scope, _params in body.calls))

    def test_manufactured_neighbor_makes_raw_stone_high_risk(self):
        body = VoxelBody(
            {
                (0, 64, 0): "stone",
                (1, 64, 0): "oak_planks",
            }
        )

        assessment = VoxelStructureRiskAssessor(body).assess(
            (0, 64, 0),
            "stone",
            BreakContext.DIRECT,
        )

        self.assertEqual(assessment.level, StructureRiskLevel.HIGH)
        self.assertIn("manufactured_blocks:1", assessment.signals)

    def test_exposed_regular_raw_stone_plane_is_ambiguous(self):
        blocks = {
            (0, y, z): "stone"
            for y in range(61, 68)
            for z in range(-1, 2)
        }
        body = VoxelBody(blocks)

        assessment = VoxelStructureRiskAssessor(body).assess(
            (0, 64, 0),
            "stone",
            BreakContext.DIRECT,
        )

        self.assertEqual(assessment.level, StructureRiskLevel.AMBIGUOUS)
        self.assertIn("exposed_regular_plane", assessment.signals)

    def test_isolated_cobblestone_column_is_not_low_risk(self):
        body = VoxelBody(
            {
                (0, y, 0): "cobblestone"
                for y in range(64, 69)
            }
        )

        assessment = VoxelStructureRiskAssessor(body).assess(
            (0, 65, 0),
            "cobblestone",
            BreakContext.RECOVERY,
        )

        self.assertEqual(assessment.level, StructureRiskLevel.HIGH)
        self.assertIn("isolated_vertical_column", assessment.signals)

    def test_incomplete_world_read_is_ambiguous_not_natural(self):
        assessment = VoxelStructureRiskAssessor(VoxelBody(fail=True)).assess(
            (0, 64, 0),
            "stone",
            BreakContext.DIRECT,
        )

        self.assertEqual(assessment.level, StructureRiskLevel.AMBIGUOUS)
        self.assertFalse(assessment.complete)
        self.assertEqual(assessment.sampled_cells, 0)


if __name__ == "__main__":
    unittest.main()
