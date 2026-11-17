from pathlib import Path
from types import SimpleNamespace

import pytest

from minebot.app.observation_artifacts import PersistentToolObservationArchive
from minebot.app.ownership_ledger import OwnershipLedger
from minebot.app.runner import AgentRuntime, _model_tool_payload
from minebot.app.body_loop import BodyLoop
from minebot.app.runtime_state import PlacementEdit, RuntimeScope, RuntimeStateStore
from minebot.contract.governance import BreakContext
from minebot.game.governance import GovernancePolicy
from minebot.game.real_client_body import WORLD_EDIT_RECEIPT

OVERWORLD = "minecraft:overworld"


def _edit(index, pos, before, after, op, *, target=True, kind="build"):
    return {"index": index, "pos": list(pos), "before": before, "after": after, "op": op,
            "action_kind": kind, "target": target, "tick": index}


def _result(*edits, action_id="action-1", dimension=OVERWORLD, **metrics):
    return {"success": True, "reason": "structure_verified", "metrics": {
        **metrics, WORLD_EDIT_RECEIPT: {"action_id": action_id, "dimension": dimension,
                                        "edits": list(edits), "complete": True}}}


class _World:
    """One run of the session: a governance, a ledger loaded from the store, an archive."""

    def __init__(self, path: Path, world_id="house", dimension=OVERWORLD):
        self.store = RuntimeStateStore(path)
        self.scope = RuntimeScope("real-client://127.0.0.1:8771", world_id, "HouseBot")
        self.dimension = dimension
        self.governance = GovernancePolicy(bot_name="HouseBot", dimension=lambda: self.dimension)
        self.ledger = OwnershipLedger(self.governance)
        self.loaded = self.ledger.load(self.store.list_bot_placements(self.scope))
        self.archive = PersistentToolObservationArchive(self.store, self.scope, ledger=self.ledger)

    def store_result(self, result, call_id="call-1"):
        return self.archive.store(tool_name="build_structure", tool_call_id=call_id, result=result, complete=True)

    def can_break_own(self, pos, block="cobblestone"):
        return self.governance.can_break(pos, block, BreakContext.BOT_CLEANUP).allowed


def test_a_wall_built_before_a_restart_can_be_opened_after_it(tmp_path):
    path = tmp_path / "state.sqlite3"
    first = _World(path)
    first.store_result(_result(_edit(0, (0, 64, 0), "air", "cobblestone", "place"),
                               _edit(1, (0, 65, 0), "air", "cobblestone", "place")))
    assert first.can_break_own((0, 64, 0))
    first.store.close()

    second = _World(path)
    assert second.loaded == 2
    assert second.can_break_own((0, 64, 0)) and second.can_break_own((0, 65, 0))
    second.store.close()


def test_only_blocks_placed_inside_the_target_are_owned(tmp_path):
    world = _World(tmp_path / "state.sqlite3")
    world.store_result(_result(
        _edit(0, (0, 64, 0), "air", "cobblestone", "place"),
        _edit(1, (5, 63, 5), "air", "dirt", "place", target=False),  # a step on the way
        _edit(2, (0, 64, 1), "water", "water", "change"),
    ))
    rows = world.store.list_bot_placements(world.scope)
    assert [row["pos"] for row in rows] == [[0, 64, 0]]
    assert rows[0]["purpose"] == "build"
    world.store.close()


def test_breaking_an_own_block_removes_it_from_the_ledger(tmp_path):
    path = tmp_path / "state.sqlite3"
    world = _World(path)
    world.store_result(_result(_edit(0, (0, 64, 0), "air", "cobblestone", "place")))
    world.store_result(_result(_edit(0, (0, 64, 0), "cobblestone", "air", "break", kind="clear"),
                               action_id="action-2"), call_id="call-2")
    assert (0, 64, 0) not in world.governance.bot_placements
    world.store.close()
    assert _World(path).loaded == 0


def test_the_place_block_purpose_is_kept(tmp_path):
    world = _World(tmp_path / "state.sqlite3")
    world.store_result(_result(_edit(0, (1, 64, 1), "air", "dirt", "place", kind="placeBlock"), purpose="scaffold"))
    assert world.governance.bot_placements[(1, 64, 1)].purpose == "scaffold"
    world.store.close()


def test_other_worlds_and_dimensions_do_not_share_records(tmp_path):
    path = tmp_path / "state.sqlite3"
    house = _World(path, world_id="house")
    house.store_result(_result(_edit(0, (0, 64, 0), "air", "cobblestone", "place")))
    house.store.close()

    other = _World(path, world_id="other-save")
    assert other.loaded == 0 and not other.can_break_own((0, 64, 0))
    other.store.close()

    nether = _World(path, world_id="house", dimension="minecraft:the_nether")
    assert nether.loaded == 1 and not nether.can_break_own((0, 64, 0))
    nether.dimension = OVERWORLD
    assert nether.can_break_own((0, 64, 0))
    nether.store.close()


def test_a_changed_block_is_not_broken_on_an_old_record(tmp_path):
    world = _World(tmp_path / "state.sqlite3")
    world.store_result(_result(_edit(0, (0, 64, 0), "air", "cobblestone", "place")))
    assert not world.can_break_own((0, 64, 0), block="oak_planks")
    world.store.close()


def test_a_failed_ledger_write_keeps_the_observation_and_says_so(tmp_path):
    world = _World(tmp_path / "state.sqlite3")
    result = _result({"index": 0, "pos": [0, 64, 0], "before": "air", "after": "cobblestone",
                      "op": "place", "action_kind": "build", "target": True})
    original = world.store.create_tool_observation

    def failing(*args, placement_edits=(), **kwargs):
        if placement_edits:
            raise RuntimeError("disk full")
        return original(*args, **kwargs)

    world.store.create_tool_observation = failing
    handle = world.store_result(result)
    assert handle.startswith("observation:")
    assert result["metrics"]["provenance_persistence"] == "failed"
    # The block is there; this session still knows it is its own.
    assert world.can_break_own((0, 64, 0))
    shown = _model_tool_payload("build_structure", result, trace_ref="call-1")
    assert shown["provenance_persistence"] == "failed"
    world.store.close()


def test_without_a_ledger_nothing_is_recorded(tmp_path):
    store = RuntimeStateStore(tmp_path / "state.sqlite3")
    scope = RuntimeScope("server", "real-client:minecraft:overworld", "HouseBot")
    archive = PersistentToolObservationArchive(store, scope)
    archive.store(tool_name="build_structure", tool_call_id="c", complete=True,
                  result=_result(_edit(0, (0, 64, 0), "air", "cobblestone", "place")))
    assert store.list_bot_placements(scope) == []
    store.close()


def test_protected_position_sink_tracks_travel_denials():
    # The Body's own blocks are its own to break on the way (run-45, user 2026-11-11):
    # none of them is kept from Baritone.
    policy = GovernancePolicy(bot_name="HouseBot")
    sent = []
    ledger = OwnershipLedger(policy, protected_positions_sink=sent.append)
    ledger.applied([PlacementEdit(
        op="place", pos=(4, 70, 4), block_type="oak_planks", purpose="build",
        dimension=OVERWORLD, action_id="a", instance=None, edit_index=0,
    )])
    assert sent[-1] == ()
    ledger.applied([PlacementEdit(
        op="remove", pos=(4, 70, 4), block_type="oak_planks", purpose="",
        dimension=OVERWORLD, action_id="b", instance=None, edit_index=0,
    )])
    assert sent[-1] == ()


def test_protected_positions_wait_for_and_follow_each_handshake():
    from minebot.game.real_client_body import RealClientBody

    class Ipc:
        def __init__(self):
            self.sent = []
        def connect(self):
            pass
        def wait_ready(self):
            pass
        def settings(self, values):
            self.sent.append(values)
            return {}

    ipc = Ipc()
    body = RealClientBody("HouseBot", ipc)
    body.sync_protected_positions(((4, 70, 4),))
    assert ipc.sent == []  # no client yet: startup is not blocked
    body._ensure_connected()
    assert ipc.sent == [{"protected_positions": [[4, 70, 4]]}]
    body._ready = False  # a lost connection forces a new handshake
    body._ensure_connected()
    assert len(ipc.sent) == 2
    body.sync_protected_positions(())
    assert ipc.sent[-1] == {"protected_positions": []}


@pytest.mark.parametrize("metrics", [
    {"target": [0, 64, 0], "target_before": {"type": "cobblestone"}, "target_after": {"type": "air"}},
    {"target": [0, 64, 0], "target_after": {"type": "oak_planks"}},
    {"target": [0, 64, 0], "block_after": "air"},
    {"observe_pos": [0, 64, 0], "observed_after": {"type": "water"}},
    {"scope": "blockAt", "data": {"x": 0, "y": 64, "z": 0, "type": "minecraft:air"}},
    {"data": {"cells": [{"pos": [0, 64, 0], "type": "air"}]}},
    {"blocks": [{"x": 0, "y": 64, "z": 0, "type": "dirt"}]},
    {"original_result": {"metrics": {"target": [0, 64, 0], "target_after": {"type": "air"}}}},
])
def test_observed_block_changes_revoke_any_own_block_and_survive_restart(tmp_path, metrics):
    path = tmp_path / "state.sqlite3"
    world = _World(path)
    world.store_result(_result(_edit(0, (0, 64, 0), "air", "cobblestone", "place")))
    # A failed/unverified action can still report an authoritative cell read.
    world.archive.reconcile_ownership({"success": False, "reason": "targeted_use_unverified", "metrics": metrics})
    assert not world.governance.bot_placements
    assert not world.store.list_bot_placements(world.scope)
    world.store.close()
    restarted = _World(path)
    assert restarted.loaded == 0
    restarted.store.close()


@pytest.mark.parametrize("metrics", [
    {"target": [0, 64, 0], "target_after": {"type": "minecraft:cobblestone", "properties": {"changed": True}}},
    {"target": [0, 64, 0], "target_after": {"type": "unknown"}},
    {"target": [0, 64, 0], "target_after": {"type": "air", "state": "UNKNOWN"}},
    {"scope": "blockAt", "data": {"type": "air"}},
    {"cells": []},
    {"ok": False, "cells": [{"pos": [0, 64, 0], "type": "air"}]},
    {"dimension": "minecraft:the_end", "target": [0, 64, 0], "target_after": {"type": "air"}},
    {"intent": "The block is gone", "params": {"target": [0, 64, 0], "target_after": {"type": "air"}}},
    {"entities": [{"pos": [0, 64, 0], "type": "zombie"}]},
])
def test_same_block_unknown_cells_other_dimensions_and_intent_do_not_revoke_ownership(tmp_path, metrics):
    world = _World(tmp_path / "state.sqlite3")
    world.store_result(_result(_edit(0, (0, 64, 0), "air", "cobblestone", "place")))
    world.archive.reconcile_ownership({"metrics": metrics})
    assert world.can_break_own((0, 64, 0))
    assert len(world.store.list_bot_placements(world.scope)) == 1
    world.store.close()


def test_a_replacement_receipt_keeps_the_new_own_block(tmp_path):
    world = _World(tmp_path / "state.sqlite3")
    world.store_result(_result(_edit(0, (0, 64, 0), "air", "cobblestone", "place")))
    world.store_result(_result(_edit(0, (0, 64, 0), "cobblestone", "oak_planks", "place"), action_id="replace"))
    assert world.can_break_own((0, 64, 0), "oak_planks")
    assert world.store.list_bot_placements(world.scope)[0]["block_type"] == "oak_planks"
    world.store.close()


def test_internal_body_block_reads_remove_ownership_before_a_tool_finishes(tmp_path):
    from minebot.game.real_client_body import RealClientBody

    world = _World(tmp_path / "state.sqlite3")
    world.store_result(_result(_edit(0, (0, 64, 0), "air", "cobblestone", "place")))
    body = RealClientBody("HouseBot", SimpleNamespace(connect=lambda: None,
        world_read=lambda scope, params: {"ok": True, "complete": True, "data": {"type": "air"}}))
    body.set_block_observer(world.archive.reconcile_ownership)
    observed = body.perceive("blockAt", {"x": 0, "y": 64, "z": 0})
    assert observed.ok and observed.data["type"] == "air"
    assert not world.governance.bot_placements
    assert not world.store.list_bot_placements(world.scope)
    world.store.close()


def test_archiving_the_original_result_after_a_protective_use_does_not_restore_ownership(tmp_path):
    world = _World(tmp_path / "state.sqlite3", dimension="minecraft:the_end")
    runtime = SimpleNamespace(observation_archive=world.archive, governance_policy=world.governance,
                              world_observer=None, decision_client=SimpleNamespace(),
                              trace=SimpleNamespace(emit=lambda *args, **kwargs: None))
    runtime.body_loop = BodyLoop(runtime)
    placed = _result(_edit(0, (0, 68, 0), "air", "white_bed", "place"), dimension="minecraft:the_end")
    AgentRuntime._observe_world(runtime, "place_block", {"pos": [0, 68, 0]}, placed)
    assert (0, 68, 0) in world.governance.bot_placements
    used = {"success": True, "reason": "completed", "metrics": {
        "target": [0, 68, 0], "target_before": {"type": "white_bed"}, "target_after": {"type": "air"}}}
    AgentRuntime._observe_world(runtime, "use_on_block", {"pos": [0, 68, 0]}, used)
    world.store_result(placed)
    assert not world.governance.bot_placements
    assert not world.store.list_bot_placements(world.scope)
    assert "use_on_block(" in runtime.body_loop._latest_finished_call
    world.store.close()
