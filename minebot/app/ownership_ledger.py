"""The Bot's own blocks, kept across restarts from confirmed world edits (pro plan P4).

The only source of new ownership is an action's ``world_edit_receipt``: block changes the client
saw the server confirm, numbered per action. A block counts as the Bot's when
the action placed it inside its own declared target. Blocks a route-finding
step put down on the way (``target`` false) are not recorded here; that is the
transit set of the ownership-ledger draft, which is not agreed yet. A block
that is newly matching a blueprint is not proof the Bot placed it, so build
results are never read for ownership.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any
from uuid import uuid4

from minebot.app.runtime_state import PlacementEdit
from minebot.game.governance import DEFAULT_DIMENSION, GovernancePolicy
from minebot.game.real_client_body import WORLD_EDIT_RECEIPT

_PURPOSE_BY_ACTION = {"build": "build", "placeBlock": "place"}
_NOT_A_BLOCK = frozenset({"air", "cave_air", "void_air"})


class OwnershipLedger:
    """Turns receipts into ledger edits and mirrors stored rows into governance."""

    def __init__(
        self,
        governance: GovernancePolicy,
        *,
        protected_positions_sink: Callable[[tuple[tuple[int, int, int], ...]], None] | None = None,
    ) -> None:
        self.governance = governance
        self._protected_positions_sink = protected_positions_sink
        self._applied_receipts: set[tuple[str, str, tuple[int, int, int], int | None]] = set()

    def edits_from(self, result: Mapping[str, Any]) -> list[PlacementEdit]:
        edits: list[PlacementEdit] = []
        for receipt, around in _receipts(result.get("metrics")):
            edits.extend(edit for edit in _receipt_edits(receipt, around)
                         if (edit.action_id, edit.dimension, edit.pos, edit.edit_index) not in self._applied_receipts)
        return edits

    def removals_from(self, result: Mapping[str, Any], *, after: Iterable[PlacementEdit] = ()) -> list[PlacementEdit]:
        """Revoke ownership when an explicit Body observation contradicts it.

        Missing cells and unknown types say nothing. Observations can remove
        ownership, but only a placement receipt can establish it.
        """
        observed = {}
        for dimension, pos, block in _block_observations(result, self.governance._current_dimension()):
            observed[dimension, pos] = block
        placements = {(dimension, pos): entry.block_type
                      for dimension, entries in self.governance.all_bot_placements().items()
                      for pos, entry in entries.items()}
        for edit in after:
            key = (edit.dimension, edit.pos)
            if edit.op == "place":
                placements[key] = edit.block_type
            else:
                placements.pop(key, None)
        action_id = f"observation-{uuid4()}"
        return [PlacementEdit(op="remove", dimension=dimension, pos=pos,
                              block_type=block_type, purpose="", action_id=action_id,
                              instance=None, edit_index=None)
                for (dimension, pos), block in observed.items()
                if (block_type := placements.get((dimension, pos))) is not None
                and block_type.removeprefix("minecraft:") != block]

    def applied(self, edits: Iterable[PlacementEdit]) -> None:
        for edit in edits:
            self._applied_receipts.add((edit.action_id, edit.dimension, edit.pos, edit.edit_index))
            if edit.op == "place":
                self.governance.record_bot_placement(
                    edit.pos, edit.block_type, edit.purpose, self._bot(), dimension=edit.dimension)
            else:
                self.governance.forget_bot_placement(edit.pos, dimension=edit.dimension)
        self._sync_protected_positions()

    def load(self, rows: Iterable[Mapping[str, Any]]) -> int:
        loaded = 0
        for row in rows:
            pos = tuple(int(v) for v in row["pos"])
            self.governance.record_bot_placement(
                pos, str(row["block_type"]), str(row["purpose"]), self._bot(), dimension=str(row["dimension"]))
            loaded += 1
        self._sync_protected_positions()
        return loaded

    def _sync_protected_positions(self) -> None:
        if self._protected_positions_sink is None:
            return
        # Travel protection is position-specific. Keep the wire bounded while
        # retaining a deterministic prefix for unusually large ledgers.
        protected = tuple(sorted(
            pos for pos, entry in self.governance.bot_placements.items()
            if not self.governance.can_break(pos, entry.block_type, "travel").allowed
        )[:4096])
        self._protected_positions_sink(protected)

    def _bot(self) -> str:
        return str(getattr(self.governance, "bot_name", None) or "")


def _receipts(value: object, around: Mapping[str, Any] | None = None):
    """Every receipt in a result, with the metrics dict that holds it.

    Composite results keep an inner action's metrics nested (a supervised
    call's ``original_result``), so the receipt can sit below the top level.
    """
    if isinstance(value, Mapping):
        receipt = value.get(WORLD_EDIT_RECEIPT)
        if isinstance(receipt, Mapping):
            yield receipt, value
        for key, item in value.items():
            if key != WORLD_EDIT_RECEIPT:
                yield from _receipts(item, value)
    elif isinstance(value, list):
        for item in value:
            yield from _receipts(item, around)


def _cell(value: object) -> tuple[int, int, int] | None:
    if isinstance(value, Mapping):
        value = value.get("pos", [value.get(axis) for axis in ("x", "y", "z")])
    if isinstance(value, (list, tuple)) and len(value) == 3 and all(type(v) is int for v in value):
        return tuple(value)
    return None


def _block_observations(value: object, dimension: str):
    """Read existing block-observation shapes, never intent or requested blocks."""
    if not isinstance(value, Mapping) or value.get("ok") is False:
        return
    dimension = str(value.get("dimension") or dimension)
    # Nested action results precede the enclosing call's final observation.
    # Do not interpret historical attempts, parameters or Agent projections.
    for key, child in value.items():
        if key in {"metrics", "data"} or key.endswith("_result"):
            yield from _block_observations(child, dimension)
    def cell_block(pos, block):
        cell = _cell(pos)
        if isinstance(block, Mapping):
            if block.get("state") == "UNKNOWN":
                return
            block = block.get("type")
        if cell is not None and isinstance(block, str):
            block = block.removeprefix("minecraft:")
            if block and block.lower() != "unknown":
                yield dimension, cell, block
    if value.get("scope") == "blockAt" and isinstance(value.get("data"), Mapping):
        data = value["data"]
        yield from cell_block(data, data)
    for key in ("cells", "blocks"):
        for cell in value.get(key) or ():
            if isinstance(cell, Mapping):
                yield from cell_block(cell, cell)
    target = value.get("target")
    if "target_after" in value or "target_before" in value:
        yield from cell_block(target, value.get("target_after", value.get("target_before")))
    if "block_after" in value:
        yield from cell_block(target, value["block_after"])
    if "observed_after" in value:
        yield from cell_block(value.get("observe_pos"), value["observed_after"])


def _receipt_edits(receipt: Mapping[str, Any], around: Mapping[str, Any]) -> list[PlacementEdit]:
    action_id = receipt.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        return []
    dimension = receipt.get("dimension")
    dimension = dimension if isinstance(dimension, str) and dimension else DEFAULT_DIMENSION
    stated = around.get("purpose")
    out: list[PlacementEdit] = []
    for edit in receipt.get("edits") or ():
        if not isinstance(edit, Mapping):
            continue
        pos = edit.get("pos")
        index = edit.get("index")
        before, after = edit.get("before"), edit.get("after")
        if (not isinstance(pos, list) or len(pos) != 3 or not all(type(v) is int for v in pos)
                or not isinstance(before, str) or not isinstance(after, str) or before == after):
            continue
        common = {"dimension": dimension, "pos": (pos[0], pos[1], pos[2]), "action_id": action_id,
                  "instance": None, "edit_index": index if type(index) is int else None}
        kind = str(edit.get("action_kind") or "")
        if edit.get("op") == "place" and edit.get("target") is True and after not in _NOT_A_BLOCK:
            purpose = stated if isinstance(stated, str) and stated else _PURPOSE_BY_ACTION.get(kind, kind or "place")
            out.append(PlacementEdit(op="place", block_type=after, purpose=purpose, **common))
        elif before not in _NOT_A_BLOCK:
            # Whatever was there is gone; the store ignores cells it never owned.
            out.append(PlacementEdit(op="remove", block_type=before, purpose="", **common))
    return out
