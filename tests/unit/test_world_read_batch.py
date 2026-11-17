import unittest

from minebot.contract import PerceptionResult
from minebot.body.world_read import read_block_facts, read_surface_columns


def _fact(pos, block_type="air", state="CLEAR", properties=None):
    return {
        "x": pos[0],
        "y": pos[1],
        "z": pos[2],
        "type": block_type,
        "state": state,
        "properties": properties or {},
    }


class BatchFakeBody:
    bot_name = "Bot1"

    def __init__(
        self,
        blocks=None,
        *,
        fail=False,
        page_limit=None,
        omit_positions=None,
        unexpected_fact=None,
        force_next=None,
        force_complete=None,
        envelope_only_next=False,
    ):
        self.blocks = dict(blocks or {})
        self.fail = fail
        self.page_limit = page_limit
        self.omit_positions = set(omit_positions or ())
        self.unexpected_fact = unexpected_fact
        self.force_next = force_next
        self.force_complete = force_complete
        self.envelope_only_next = envelope_only_next
        self.calls = 0
        self.request_log = []

    def perceive(self, scope, params):
        if scope != "blockCells":
            raise AssertionError(f"unexpected scope {scope}")
        self.calls += 1
        self.request_log.append(dict(params))
        if self.fail:
            return PerceptionResult(
                bot="Bot1", scope="blockCells", type="perception",
                ok=False, complete=False, data={}, uncertainty=None, next=None,
                error="transport",
            )
        cells = params["cells"]
        start = int(params.get("start") or 0)
        limit = self.page_limit if self.page_limit is not None else int(params.get("limit") or 64)
        page = cells[start : start + limit]
        facts = []
        for c in page:
            pos = (int(c[0]), int(c[1]), int(c[2]))
            if pos in self.omit_positions:
                continue
            raw = self.blocks.get(pos, ("air", "CLEAR"))
            if len(raw) == 2:
                bt, st = raw
                props = {}
            else:
                bt, st, props = raw
            facts.append(_fact(pos, bt, st, props))
        if self.unexpected_fact is not None:
            facts.append(_fact(self.unexpected_fact, "stone", "SOLID"))
        next_idx = start + len(page)
        nxt = None if next_idx >= len(cells) else next_idx
        if self.force_next is not None:
            nxt = self.force_next
        complete = nxt is None
        if self.force_complete is not None:
            complete = self.force_complete
        data = {"count": len(page), "total": len(cells), "cells": facts}
        if not self.envelope_only_next:
            data["nextStart"] = nxt
        return PerceptionResult(
            bot="Bot1", scope="blockCells", type="perception",
            ok=True, complete=complete,
            data=data,
            uncertainty=[] if nxt is None else [{"reason": "limit_exceeded"}],
            next=str(nxt) if self.envelope_only_next and nxt is not None else None,
            error=None,
        )


class SurfaceColumnFakeBody:
    bot_name = "Bot1"

    def __init__(self, facts, *, page_limit=64, fail=False):
        self.facts = dict(facts)
        self.page_limit = page_limit
        self.fail = fail
        self.calls = []

    def perceive(self, scope, params):
        if scope != "surfaceColumns":
            raise AssertionError(f"unexpected scope {scope}")
        self.calls.append(dict(params))
        if self.fail:
            return PerceptionResult(
                bot="Bot1",
                scope=scope,
                type="perception",
                ok=False,
                complete=True,
                data={},
                error="heightmap_unavailable",
            )
        columns = params["columns"]
        start = int(params.get("start") or 0)
        limit = min(self.page_limit, int(params.get("limit") or 64))
        page = columns[start : start + limit]
        rows = [self.facts[(int(column[0]), int(column[1]))] for column in page]
        next_start = start + len(page)
        nxt = None if next_start >= len(columns) else next_start
        return PerceptionResult(
            bot="Bot1",
            scope=scope,
            type="perception",
            ok=True,
            complete=nxt is None,
            data={"columns": rows, "nextStart": nxt},
            uncertainty=[] if nxt is None else [{"reason": "limit_exceeded"}],
        )


class ReadBlockFactsTests(unittest.TestCase):
    def test_returns_facts_for_all_positions_in_one_call(self):
        blocks = {
            (8, 70, 8): ("air", "CLEAR"),
            (8, 69, 8): ("stone", "SOLID"),
            (8, 71, 8): ("water", "LIQUID"),
        }
        body = BatchFakeBody(blocks)
        facts = read_block_facts(body, ((8, 70, 8), (8, 69, 8), (8, 71, 8)))
        self.assertEqual(body.calls, 1)
        self.assertEqual(facts[(8, 70, 8)].data["state"], "CLEAR")
        self.assertEqual(facts[(8, 69, 8)].data["type"], "stone")
        self.assertEqual(facts[(8, 69, 8)].data["state"], "SOLID")
        self.assertEqual(facts[(8, 71, 8)].data["state"], "LIQUID")

    def test_facts_are_blockat_shaped_for_predicates(self):
        body = BatchFakeBody({(0, 64, 0): ("oak_slab", "SOLID", {"type": "bottom"})})
        facts = read_block_facts(body, ((0, 64, 0),))
        pr = facts[(0, 64, 0)]
        self.assertEqual(pr.scope, "blockAt")
        self.assertTrue(pr.ok and pr.complete)
        self.assertEqual(pr.data["state"], "SOLID")
        self.assertEqual(pr.data["properties"], {"type": "bottom"})

    def test_paginates_across_pages(self):
        blocks = {(x, 64, 0): ("stone", "SOLID") for x in range(5)}
        body = BatchFakeBody(blocks, page_limit=2)
        facts = read_block_facts(
            body, tuple((x, 64, 0) for x in range(5)), page_size=2
        )
        self.assertEqual(body.calls, 3)  # 2 + 2 + 1
        self.assertEqual(len(facts), 5)
        for x in range(5):
            self.assertEqual(facts[(x, 64, 0)].data["state"], "SOLID")

    def test_pagination_chunks_outbound_cells_to_bound_rcon_command_size(self):
        blocks = {(x, 64, 0): ("stone", "SOLID") for x in range(5)}
        body = BatchFakeBody(blocks)

        facts = read_block_facts(body, tuple((x, 64, 0) for x in range(5)), page_size=2)

        self.assertEqual(len(facts), 5)
        self.assertEqual(
            [call["cells"] for call in body.request_log],
            [
                [[0, 64, 0], [1, 64, 0]],
                [[2, 64, 0], [3, 64, 0]],
                [[4, 64, 0]],
            ],
        )
        self.assertTrue(all(call["start"] == 0 for call in body.request_log))

    def test_paginates_with_envelope_next_when_data_cursor_is_absent(self):
        blocks = {(x, 64, 0): ("stone", "SOLID") for x in range(3)}
        body = BatchFakeBody(blocks, page_limit=1, envelope_only_next=True)

        facts = read_block_facts(body, tuple((x, 64, 0) for x in range(3)), page_size=1)

        self.assertEqual(body.calls, 3)
        self.assertEqual(len(facts), 3)

    def test_unknown_positions_default_to_air_clear(self):
        body = BatchFakeBody({})
        facts = read_block_facts(body, ((100, 100, 100),))
        self.assertEqual(facts[(100, 100, 100)].data["state"], "CLEAR")
        self.assertEqual(facts[(100, 100, 100)].data["type"], "air")

    def test_raises_on_perception_failure(self):
        body = BatchFakeBody(fail=True)
        with self.assertRaises(ValueError):
            read_block_facts(body, ((0, 64, 0),))

    def test_rejects_non_positive_page_size(self):
        body = BatchFakeBody({})
        with self.assertRaises(ValueError):
            read_block_facts(body, ((0, 64, 0),), page_size=0)

    def test_duplicate_positions_are_allowed_but_return_once(self):
        body = BatchFakeBody({(0, 64, 0): ("stone", "SOLID")})
        facts = read_block_facts(body, ((0, 64, 0), (0, 64, 0)))
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[(0, 64, 0)].data["state"], "SOLID")

    def test_raises_when_incomplete_page_has_no_next_cursor(self):
        body = BatchFakeBody({(0, 64, 0): ("stone", "SOLID")}, force_complete=False)
        with self.assertRaisesRegex(ValueError, "incomplete without next"):
            read_block_facts(body, ((0, 64, 0),))

    def test_raises_when_next_cursor_does_not_advance(self):
        body = BatchFakeBody({(0, 64, 0): ("stone", "SOLID")}, force_next=0, force_complete=False)
        with self.assertRaisesRegex(ValueError, "did not advance"):
            read_block_facts(body, ((0, 64, 0),))

    def test_raises_when_next_cursor_exceeds_request_length(self):
        body = BatchFakeBody({(0, 64, 0): ("stone", "SOLID")}, force_next=3, force_complete=False)
        with self.assertRaisesRegex(ValueError, "exceeds request length"):
            read_block_facts(body, ((0, 64, 0),))

    def test_raises_when_requested_cell_is_missing_from_completed_response(self):
        body = BatchFakeBody({(0, 64, 0): ("stone", "SOLID")}, omit_positions={(0, 64, 0)})
        with self.assertRaisesRegex(ValueError, "missing"):
            read_block_facts(body, ((0, 64, 0),))

    def test_raises_when_response_contains_unrequested_cell(self):
        body = BatchFakeBody({(0, 64, 0): ("stone", "SOLID")}, unexpected_fact=(99, 64, 99))
        with self.assertRaisesRegex(ValueError, "unexpected cell"):
            read_block_facts(body, ((0, 64, 0),))


class ReadSurfaceColumnsTests(unittest.TestCase):
    def test_reads_paginated_compact_heightmap_facts(self):
        facts = {
            (0, 0): {
                "x": 0,
                "z": 0,
                "feetY": 64,
                "feetType": "air",
                "feetState": "CLEAR",
                "headType": "air",
                "headState": "CLEAR",
                "supportType": "grass_block",
                "supportState": "SOLID",
            },
            (16, 0): {
                "x": 16,
                "z": 0,
                "feetY": 70,
                "feetType": "air",
                "feetState": "CLEAR",
                "headType": "air",
                "headState": "CLEAR",
                "supportType": "stone",
                "supportState": "SOLID",
            },
            (32, 0): {
                "x": 32,
                "z": 0,
                "feetY": 63,
                "feetType": "air",
                "feetState": "CLEAR",
                "headType": "air",
                "headState": "CLEAR",
                "supportType": "stone",
                "supportState": "SOLID",
            },
        }
        body = SurfaceColumnFakeBody(facts, page_limit=2)

        observed = read_surface_columns(body, tuple(facts), page_size=2)

        self.assertEqual(len(body.calls), 2)
        self.assertEqual(observed[(32, 0)].feet_pos, (32, 63, 0))
        self.assertEqual(observed[(0, 0)].support_type, "grass_block")

    def test_rejects_failed_surface_envelope(self):
        body = SurfaceColumnFakeBody({}, fail=True)
        with self.assertRaisesRegex(ValueError, "heightmap_unavailable"):
            read_surface_columns(body, ((0, 0),))


if __name__ == "__main__":
    unittest.main()
