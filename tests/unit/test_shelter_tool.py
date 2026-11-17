from __future__ import annotations

import pytest

from minebot.app.body_capability_tools import _shelter_tool
from minebot.app.ownership_ledger import OwnershipLedger
from minebot.app.runner import _model_tool_payload
from minebot.body.shelter import ShelterTransactions
from minebot.contract import Region
from minebot.game.governance import GovernancePolicy
from minebot.game.real_client_body import RealClientBody, WORLD_EDIT_RECEIPT
from minebot.game.real_client_ipc import RealClientTerminal
from tests.unit.test_real_client_body import FakeClientIpc


class ShelterIpc(FakeClientIpc):
    def __init__(self, *, status="completed", reason="shelter_sealed", **facts):
        super().__init__()
        self.status, self.reason, self.facts = status, reason, facts

    def action(self, name, params, *, action_id, timeout_ms, on_progress=None):
        self.actions.append((name, dict(params), action_id, timeout_ms))
        edits = self.facts.get("edits", [])
        if on_progress and edits:
            on_progress({"action_id": action_id, "lite": True, "world_edits": edits})
        return RealClientTerminal(action_id, self.status, self.reason, {
            "position": [4.5, 70.0, 9.5], "dimension": "minecraft:overworld",
            "world_edit_count": len(edits), "world_edits_unconfirmed": 0,
            "world_edits_truncated": False,
            "action_kind": name, **{k: v for k, v in self.facts.items() if k != "edits"},
        })


def tool(ipc=None):
    ipc = ipc or ShelterIpc()
    body = RealClientBody("ShelterBot", ipc)
    governance = GovernancePolicy(bot_name=body.bot_name,
        natural_regions=(Region("arena", (-64, -64, -64), (64, 320, 64)),))
    return _shelter_tool(ShelterTransactions(body, governance)), ipc, governance


@pytest.mark.parametrize("params", [
    {"action": "escape"}, {"side": "up", "action": "look_out"},
    {"side": "north"}, {"keep_open": True}, {"action": "look_out", "keep_open": "false"},
    {"timeout_s": 0}, {"timeout_s": -1}, {"timeout_s": 121},
    {"timeout_s": float("nan")}, {"timeout_s": float("inf")}, {"timeout_s": True},
    {"timeout_s": None}, {"timeout_s": "60"},
])
def test_invalid_shelter_arguments_never_send_an_action(params):
    registered, ipc, _ = tool()
    with pytest.raises(ValueError):
        registered.callable(params)
    assert ipc.actions == []


def test_shelter_is_one_explicit_native_action_with_current_center_and_governance():
    registered, ipc, policy = tool()
    result = registered.callable({})
    assert result.success and result.reason == "shelter_sealed"
    assert result.next_suggestion is None
    name, params, _, timeout_ms = ipc.actions[0]
    assert name == "shelterInPlace" and params["center"] == [4, 70, 9]
    assert params["action"] == "seal" and params["keep_open"] is False
    assert timeout_ms == 60000
    assert len(params["allowed_placements"]) == 15
    assert all(row["items"] == ["oak_log", "wheat_seeds"] for row in params["allowed_placements"])
    assert ipc.commands == [] and ipc.controls == []


def test_protected_region_grants_no_placement_positions():
    registered, ipc, policy = tool()
    policy.protected_regions = (Region("protected", (-64, -64, -64), (64, 320, 64)),)
    registered.callable({})
    assert all(not row["items"] for row in ipc.actions[0][1]["allowed_placements"])


def test_look_out_does_not_authorize_breaking_an_owned_block_in_a_protected_region():
    registered, ipc, policy = tool()
    policy.record_bot_placement((4, 71, 8), "cobblestone", "shelter", "ShelterBot")
    policy.protected_regions = (Region("protected", (-64, -64, -64), (64, 320, 64)),)
    registered.callable({"action": "look_out", "side": "north", "keep_open": True})
    assert ipc.actions[0][1]["owned_blocks"] == []


def test_insufficient_material_result_reports_counts_without_advice():
    registered, _, _ = tool(ShelterIpc(status="failed", reason="not_enough_blocks",
        blocks_needed=10, blocks_held=3,
        message="The shell needs 10 blocks; the inventory holds 3 placeable blocks."))
    result = registered.callable({})
    assert not result.success and result.reason == "not_enough_blocks"
    assert result.next_suggestion is None
    shown = _model_tool_payload(registered.name, result.to_payload(), trace_ref="shelter", projector=registered.projector)
    assert shown["summary"]["blocks_needed"] == 10
    assert shown["summary"]["blocks_held"] == 3
    assert "cobblestone" not in shown["summary"]["message"]


def test_unsealable_result_keeps_every_hole_and_the_blocking_mob():
    cells = [[4, 70, 8], [4, 71, 8]]
    blocked = [{"pos": cells[0], "mob": "zombie", "name": "Zombie"}]
    registered, _, _ = tool(ShelterIpc(status="failed", reason="shelter_unsealable",
        open_cells=cells, blocked_cells=blocked, attacks=1, message="The shelter has open cells."))
    result = registered.callable({})
    assert not result.success and result.next_suggestion is None
    shown = registered.projector(result.reason, result.metrics)
    assert shown["open_cells"] == cells and shown["blocked_cells"] == blocked and shown["attacks"] == 1


def test_displaced_result_reports_actual_position_without_return_advice():
    registered, _, _ = tool(ShelterIpc(status="failed", reason="displaced",
        message="The Body left the shelter center cell."))
    result = registered.callable({})
    assert not result.success and result.reason == "displaced"
    assert result.metrics["position"] == [4.5, 70, 9.5] and result.next_suggestion is None


@pytest.mark.parametrize("keep_open", [False, True])
def test_look_out_forwards_port_choices_and_preserves_visible_distances(keep_open):
    seen = [{"type": "zombie", "name": "Zombie", "distance": 5.0}]
    registered, ipc, policy = tool(ShelterIpc(reason="looked_out", seen=seen, port=[4, 71, 8], port_closed=not keep_open))
    policy.record_bot_placement((4, 71, 8), "cobblestone", "shelter", "ShelterBot")
    result = registered.callable({"action": "look_out", "side": "north", "keep_open": keep_open})
    params = ipc.actions[0][1]
    assert params["side"] == "north" and params["keep_open"] is keep_open
    assert params["owned_blocks"] == [{"pos": [4, 71, 8], "block_type": "cobblestone"}]
    summary = registered.projector(result.reason, result.metrics)
    assert summary["seen"] == seen and summary["port_closed"] is not keep_open


def test_confirmed_shelter_edits_reach_the_own_block_ledger_even_on_partial_failure():
    edits = [{"index": 0, "pos": [4, 70, 8], "before": "air", "after": "cobblestone",
              "op": "place", "target": True, "action_kind": "shelterInPlace"}]
    registered, _, policy = tool(ShelterIpc(status="failed", reason="shelter_unsealable", edits=edits))
    result = registered.callable({})
    ledger = OwnershipLedger(policy)
    changes = ledger.edits_from(result.to_payload())
    assert len(changes) == 1 and changes[0].pos == (4, 70, 8) and changes[0].purpose == "shelter"
    ledger.applied(changes)
    assert policy.bot_placements[(4, 70, 8)].block_type == "cobblestone"
    assert result.metrics[WORLD_EDIT_RECEIPT]["complete"]


def test_look_out_receipt_removes_then_restores_the_head_block_in_order():
    edits = [
        {"index": 0, "pos": [4, 71, 8], "before": "cobblestone", "after": "air", "op": "break", "target": True, "action_kind": "shelterInPlace"},
        {"index": 1, "pos": [4, 71, 8], "before": "air", "after": "cobblestone", "op": "place", "target": True, "action_kind": "shelterInPlace"},
    ]
    registered, _, policy = tool(ShelterIpc(reason="looked_out", edits=edits))
    result = registered.callable({"action": "look_out"})
    ledger = OwnershipLedger(policy)
    changes = ledger.edits_from(result.to_payload())
    assert [edit.op for edit in changes] == ["remove", "place"]
    ledger.applied(changes)
    assert policy.bot_placements[(4, 71, 8)].purpose == "shelter"


def test_shelter_ownership_survives_the_observation_archive_and_reload(tmp_path):
    from minebot.app.observation_artifacts import PersistentToolObservationArchive
    from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore

    edits = [{"index": 0, "pos": [4, 70, 8], "before": "air", "after": "cobblestone",
              "op": "place", "target": True, "action_kind": "shelterInPlace"}]
    registered, _, policy = tool(ShelterIpc(edits=edits))
    result = registered.callable({})
    scope = RuntimeScope("client", "shelter-arena", "ShelterBot")
    store = RuntimeStateStore(tmp_path / "ownership.sqlite3")
    try:
        archive = PersistentToolObservationArchive(store, scope, ledger=OwnershipLedger(policy))
        archive.store(tool_name=registered.name, tool_call_id="seal-1", result=result.to_payload(), complete=True)
        reloaded = GovernancePolicy(bot_name="ShelterBot")
        assert OwnershipLedger(reloaded).load(store.list_bot_placements(scope)) == 1
        assert reloaded.bot_placements[(4, 70, 8)].purpose == "shelter"
    finally:
        store.close()
