"""The execution snapshot reports evidence, never a guess."""

from minebot.app.execution_snapshot import ExecutionSnapshots


def _sample(**fields):
    return {"action_id": "a1", "position": [0.0, 64.0, 0.0], "inventory": {"oak_log": 1},
            "inventory_complete": True, **fields}


def test_first_sample_and_heartbeats_are_not_progress():
    snapshots = ExecutionSnapshots()
    snapshots.begin("call-1", "collect_resource", {"item": "oak_log"})
    assert snapshots.current()["state"] == "accepted"
    snapshots.observe("call-1", _sample(mine_active=True))
    snapshots.observe("call-1", _sample(mine_active=True))
    current = snapshots.current()
    assert current["state"] == "running" and current["phase"] == "mining"
    assert current["action_id"] == "a1" and current["observed_at"] is not None
    assert current["last_progress_at"] is None and "since_progress_s" not in current
    assert current["control"] == "held"


def test_real_advances_are_named():
    snapshots = ExecutionSnapshots()
    snapshots.begin("call-1", "collect_resource")
    snapshots.observe("call-1", _sample())
    snapshots.observe("call-1", _sample(inventory={"oak_log": 3}))
    current = snapshots.current()
    assert current["progress_signal"] == "inventory"
    assert current["facts"]["inventory_delta"] == {"oak_log": 2}

    snapshots.observe("call-1", _sample(inventory={"oak_log": 3}, position=[3.0, 64.0, 0.0]))
    assert snapshots.current()["progress_signal"] == "inventory"  # moved, but no path was followed
    snapshots.observe("call-1", _sample(inventory={"oak_log": 3}, position=[6.0, 64.0, 0.0], pathing=True))
    assert snapshots.current()["progress_signal"] == "movement"
    assert snapshots.current()["phase"] == "moving"

    snapshots.observe("call-1", {"action_id": "a1", "lite": True, "world_edits": [{"index": 0}]})
    assert snapshots.current()["progress_signal"] == "world_edit"
    assert snapshots.current()["phase"] == "moving"  # a lite frame cannot change the phase


def test_construction_and_path_position_use_high_water_marks():
    snapshots = ExecutionSnapshots()
    snapshots.begin("call-1", "build_structure")
    nav = {"src": [0, 64, 0], "dest": [9, 64, 0], "path_movements": 9}
    snapshots.observe("call-1", {"construction_progress": {"matching_cells": 4, "expected_cells": 100},
                                 "builder_active": True, "navigation": {**nav, "path_position": 2}})
    assert snapshots.current()["last_progress_at"] is None
    snapshots.observe("call-1", {"construction_progress": {"matching_cells": 3}, "navigation": {**nav, "path_position": 1}})
    assert snapshots.current()["last_progress_at"] is None  # world may regress; that is not progress
    snapshots.observe("call-1", {"construction_progress": {"matching_cells": 4}, "navigation": {**nav, "path_position": 3}})
    assert snapshots.current()["progress_signal"] == "path"
    snapshots.observe("call-1", {"construction_progress": {"matching_cells": 9, "expected_cells": 100},
                                 "builder_paused": True})
    current = snapshots.current()
    assert current["progress_signal"] == "construction" and current["phase"] == "builder_paused"
    assert current["facts"]["construction"] == {"matching_cells": 9, "expected_cells": 100}


def test_stop_and_finish_keep_the_last_snapshot_and_ignore_late_samples():
    snapshots = ExecutionSnapshots()
    snapshots.begin("call-1", "move_to")
    snapshots.observe("call-1", _sample(pathing=True))
    snapshots.stopping()
    assert snapshots.current()["state"] == "stopping" and snapshots.current()["control"] == "held"
    snapshots.finish("call-1", state="cancelled", reason="cancelled", control="released")
    assert snapshots.current() is None
    last = snapshots.last()
    assert last["state"] == "cancelled" and last["control"] == "released" and last["action_id"] is None

    snapshots.begin("call-2", "move_to")
    snapshots.observe("call-1", _sample(position=[50.0, 64.0, 0.0], pathing=True))  # late, other call
    snapshots.finish("call-1", state="succeeded", reason=None, control="released")
    current = snapshots.current()
    assert current["operation_id"] == "call-2" and current["observed_at"] is None
    assert snapshots.last()["operation_id"] == "call-1" and snapshots.last()["state"] == "cancelled"


def test_incomplete_inventory_is_not_a_baseline():
    snapshots = ExecutionSnapshots()
    snapshots.begin("call-1", "collect_resource")
    snapshots.observe("call-1", _sample(inventory={}, inventory_complete=False))
    snapshots.observe("call-1", _sample(inventory={"oak_log": 5}, inventory_complete=True))
    current = snapshots.current()
    assert current["last_progress_at"] is None and "inventory_delta" in current["facts"]
    assert current["facts"]["inventory_delta"] == {}
