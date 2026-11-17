"""The Body's own record of its places in this world.

Program-written facts, never the Agent's (vision-and-home-20261028.md §7.7):
what each container held when the Body last looked into it, the workstations
it used, where it wakes after dying, the world spawn, and both ends of every
portal it walked through.  One append-only JSONL file beside the life
profile; reading folds the rows by place.  The Agent's own plan for these
places (home, outposts, storage, whose things are whose) is in the profile.
"""
from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from minebot.game.real_client_body import WORLD_EDIT_RECEIPT

KEPT_PLACES = 128
KEPT_PORTALS = 16
# The file is rewritten as its folded rows once it grows past this.
COMPACT_AFTER_ROWS = 4000
# Two portal ends this close are the same portal.
SAME_PORTAL_BLOCKS = 4.0
CONTAINER_BLOCKS = frozenset({"chest", "trapped_chest", "barrel", "ender_chest", "shulker_box"})
WORKSTATION_BLOCKS = frozenset({
    "crafting_table", "furnace", "blast_furnace", "smoker", "respawn_anchor", "anvil", "chipped_anvil",
    "damaged_anvil", "enchanting_table", "brewing_stand", "smithing_table", "stonecutter", "loom",
    "cartography_table", "grindstone", "lectern", "composter", "cauldron", "beacon",
})
_DIMENSIONS = {"overworld": "minecraft:overworld", "nether": "minecraft:the_nether",
               "the_nether": "minecraft:the_nether", "end": "minecraft:the_end", "the_end": "minecraft:the_end"}
_DIMENSION_WORDS = {"minecraft:overworld": "the overworld", "minecraft:the_nether": "the Nether",
                    "minecraft:the_end": "the End"}


def block_name(value: Any) -> str:
    return str(value or "").strip().lower().removeprefix("minecraft:")


def is_container(block: Any) -> bool:
    name = block_name(block)
    return name in CONTAINER_BLOCKS or name.endswith("_shulker_box")


def is_place_block(block: Any) -> bool:
    """Blocks a home is made of: containers, workstations and beds."""
    name = block_name(block)
    return is_container(name) or name in WORKSTATION_BLOCKS or name == "bed" or name.endswith("_bed")


def dimension_id(value: Any) -> str | None:
    """``overworld`` and ``minecraft:overworld`` are one dimension; None stays unknown."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    return _DIMENSIONS.get(text.removeprefix("minecraft:"), text if ":" in text else f"minecraft:{text}")


def dimension_words(value: Any) -> str:
    """``the Nether`` for ``minecraft:the_nether``; an unknown dimension is the overworld."""
    dimension = dimension_id(value) or "minecraft:overworld"
    return _DIMENSION_WORDS.get(dimension, dimension)


def block_pos(value: Any) -> list[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        numbers = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in numbers):
        return None
    return [math.floor(item) for item in numbers]


def slot_counts(slots: Iterable[Any]) -> dict[str, int]:
    """Item ids and counts of the filled slots, as the read showed them."""
    counts: dict[str, int] = {}
    for slot in slots:
        if not isinstance(slot, Mapping) or slot.get("empty") or not slot.get("item"):
            continue
        item = block_name(slot["item"])
        counts[item] = counts.get(item, 0) + max(0, int(slot.get("count") or 0))
    return {item: count for item, count in counts.items() if count > 0}


class WorldRecord:
    """Append-only place record for one world and bot; every write is one row.

    Places are kept most recently touched first.  The respawn point is not a
    place of its own: whether its bed is still there is read from the place.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        # Oldest touched first; a touched place moves to the end.
        self._places: dict[tuple[str, int, int, int], dict[str, Any]] = {}
        self._respawn: dict[str, Any] | None = None
        self._world_spawn: dict[str, Any] | None = None
        self._portals: list[dict[str, Any]] = []
        self._rows = 0
        # Bumped on every change, so a reader can tell cheaply that nothing moved.
        self.revision = 0
        self._load()

    # -- writes ------------------------------------------------------------

    def container_seen(self, dimension: Any, pos: Any, items: Mapping[str, int], *, complete: bool,
                       source: str, block: Any = None) -> None:
        """What the container held after the Body's last look or move; ``complete`` when every slot was read."""
        self._write({"kind": "container", "block": block_name(block) or None,
                     "items": {block_name(item): int(count) for item, count in items.items() if int(count) > 0},
                     "complete": bool(complete), "source": source}, dimension, pos)

    def furnace_seen(self, dimension: Any, pos: Any, contents: Mapping[str, Any], *, loaded: bool, source: str,
                     block: Any = None) -> None:
        """What a furnace holds by slot after the Body loaded it (``loaded``) or looked into or emptied it."""
        slots = {name: _slot_row(contents.get(name)) for name in ("input", "fuel", "output")}
        self._write({"kind": "furnace", "block": block_name(block) or "furnace", "slots": slots,
                     "loaded": bool(loaded), "source": source}, dimension, pos)

    def place_used(self, dimension: Any, pos: Any, block: Any, source: str, at: float | None = None) -> None:
        """A workstation, container or bed the Body used, with the tool that used it."""
        if is_place_block(block):
            self._write({"kind": "used", "block": block_name(block), "source": source}, dimension, pos, at=at)

    def place_gone(self, dimension: Any, pos: Any, reason: str) -> None:
        """A known place is no longer there: the Body took it down, or found another block in its cell."""
        key = _key(dimension, pos)
        with self._lock:
            known = key in self._places
        if known:
            self._write({"kind": "gone", "reason": reason}, dimension, pos)

    def respawn_set(self, dimension: Any, pos: Any, block: Any, said: str, at: float | None = None) -> None:
        """The game said the respawn point was set at this bed or anchor; ``at`` is when it said so."""
        if block_pos(pos) is None or dimension_id(dimension) is None:
            # Set, but the Body did not see at which block: the old point no longer holds.
            self._write({"kind": "respawn", "said": said}, at=at)
            return
        self._write({"kind": "respawn", "block": block_name(block) or None, "said": said}, dimension, pos, at=at)
        self.place_used(dimension, pos, block, "respawn point set here", at=at)

    def respawn_lost(self, said: str, at: float | None = None) -> None:
        """The game said the bed or anchor was missing or blocked at respawn."""
        with self._lock:
            known = self._respawn
            was = ({name: known.get(name) for name in ("dimension", "pos", "block")}
                   if known and not known.get("lost") and not known.get("unknown") else None)
        self._write({"kind": "respawn_lost", "said": said, "was": was}, at=at)

    def world_spawn_seen(self, dimension: Any, pos: Any) -> None:
        where = block_pos(pos)
        with self._lock:
            known = self._world_spawn
            if where is None or (known and known["pos"] == where and known["dimension"] == dimension_id(dimension)):
                return
        self._write({"kind": "world_spawn"}, dimension, pos)

    def portal_walked(self, from_dimension: Any, from_pos: Any, to_dimension: Any, to_pos: Any) -> None:
        """The Body went in at one end and came out at the other, with no death between."""
        ends = [(dimension_id(from_dimension), block_pos(from_pos)), (dimension_id(to_dimension), block_pos(to_pos))]
        if any(dimension is None or pos is None for dimension, pos in ends) or ends[0][0] == ends[1][0]:
            return
        self._write({"kind": "portal", "from": {"dimension": ends[0][0], "pos": ends[0][1]},
                     "to": {"dimension": ends[1][0], "pos": ends[1][1]}})

    # -- reads -------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Places most recently touched first, the respawn point, the world spawn, and the portal pairs."""
        with self._lock:
            respawn = copy.deepcopy(self._respawn)
            if respawn and not respawn.get("lost") and not respawn.get("unknown"):
                bed = self._places.get(_key(respawn["dimension"], respawn["pos"]))
                if bed and bed.get("gone"):
                    respawn["gone"] = copy.deepcopy(bed["gone"])
            return {"places": copy.deepcopy(list(reversed(self._places.values()))), "respawn": respawn,
                    "world_spawn": copy.deepcopy(self._world_spawn), "portals": copy.deepcopy(self._portals)}

    def respawn(self) -> dict[str, Any] | None:
        """The respawn point as last written, as in ``snapshot`` but without the bed's state."""
        with self._lock:
            return copy.deepcopy(self._respawn)

    def portals(self) -> list[dict[str, Any]]:
        """Both ends of every portal walked through, as in ``snapshot``."""
        with self._lock:
            return copy.deepcopy(self._portals)

    def place(self, dimension: Any, pos: Any) -> dict[str, Any] | None:
        key = _key(dimension, pos)
        with self._lock:
            found = self._places.get(key) if key else None
            return copy.deepcopy(found) if found else None

    # -- internals ---------------------------------------------------------

    def _write(self, row: dict[str, Any], dimension: Any = None, pos: Any = None, at: float | None = None) -> None:
        now = time.time() if at is None else float(at)
        row = {"t": round(now, 3), "at": _iso(now), **row}
        if pos is not None:
            # A place with no position or an unknown dimension is not written: it could not be found again.
            where, row["dimension"] = block_pos(pos), dimension_id(dimension)
            if where is None or row["dimension"] is None:
                return
            row["pos"] = where
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            finally:
                # A row that could not be written still holds for this process.
                self._fold(row)
                self._rows += 1
                self.revision += 1
            if self._rows > COMPACT_AFTER_ROWS:
                self._compact()

    def _fold(self, row: Mapping[str, Any]) -> None:
        kind = row.get("kind")
        if kind == "place":
            place = dict(row["place"])
            key = _key(place.get("dimension"), place.get("pos"))
            if key:
                self._places[key] = place
            return
        if kind == "respawn_lost":
            self._respawn = {"lost": True, "t": row["t"], "at": row["at"], "said": row.get("said"),
                             "was": row.get("was")}
            return
        if kind == "world_spawn":
            self._world_spawn = {"dimension": row["dimension"], "pos": row["pos"], "t": row["t"], "at": row["at"]}
            return
        if kind == "portal":
            self._fold_portal(row)
            return
        if kind == "respawn" and row.get("pos") is None:
            self._respawn = {"unknown": True, "t": row["t"], "at": row["at"], "said": row.get("said")}
            return
        key = _key(row.get("dimension"), row.get("pos"))
        if key is None:
            return
        if kind == "respawn":
            self._respawn = {"dimension": row["dimension"], "pos": row["pos"], "block": row.get("block"),
                             "t": row["t"], "at": row["at"], "said": row.get("said")}
            return
        if kind not in ("container", "used", "gone", "furnace"):
            return
        place = self._places.pop(key, None)
        if place is None:
            if kind == "gone":
                return
            place = {"dimension": row["dimension"], "pos": row["pos"]}
        self._places[key] = place
        place["t"] = row["t"]
        if kind == "gone":
            place["gone"] = {"t": row["t"], "at": row["at"], "reason": row.get("reason")}
            return
        place.pop("gone", None)
        if row.get("block"):
            place["block"] = row["block"]
        if kind == "furnace":
            previous = place.get("furnace") if isinstance(place.get("furnace"), Mapping) else {}
            place["furnace"] = {"slots": dict(row.get("slots") or {}), "t": row["t"], "at": row["at"],
                                "source": row.get("source"), "last_was_load": bool(row.get("loaded")),
                                # When it was last loaded: what was put in and when, kept across later looks.
                                "loaded": ({"t": row["t"], "at": row["at"], "slots": dict(row.get("slots") or {})}
                                           if row.get("loaded") else previous.get("loaded"))}
            place["used"] = {"t": row["t"], "at": row["at"], "source": row.get("source")}
        elif kind == "container":
            place.setdefault("block", "container")
            place["items"] = dict(row.get("items") or {})
            place["complete"] = bool(row.get("complete"))
            place["looked"] = {"t": row["t"], "at": row["at"], "source": row.get("source")}
        else:
            place["used"] = {"t": row["t"], "at": row["at"], "source": row.get("source")}
        self._bound_places()

    def _fold_portal(self, row: Mapping[str, Any]) -> None:
        start, end = row["from"], row["to"]
        for link in self._portals:
            if ((_near(start, link["from"]) and _near(end, link["to"]))
                    or (_near(start, link["to"]) and _near(end, link["from"]))):
                link["t"], link["at"] = row["t"], row["at"]
                return
        self._portals.append({"from": dict(start), "to": dict(end), "t": row["t"], "at": row["at"]})
        if len(self._portals) > KEPT_PORTALS:
            # The portals walked longest ago leave first.
            self._portals.sort(key=lambda link: link["t"])
            del self._portals[:-KEPT_PORTALS]

    def _bound_places(self) -> None:
        if len(self._places) <= KEPT_PLACES:
            return
        # Places already gone leave first, then the ones untouched longest; the respawn bed stays.
        respawn = self._respawn or {}
        kept = _key(respawn.get("dimension"), respawn.get("pos")) if not respawn.get("lost") else None
        order = sorted((key for key in self._places if key != kept), key=lambda key: "gone" not in self._places[key])
        for key in order[:len(self._places) - KEPT_PLACES]:
            del self._places[key]

    def _rows_now(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = [{"kind": "place", "place": place, "t": place.get("t", 0)}
                                      for place in self._places.values()]
        if self._world_spawn:
            rows.append({"kind": "world_spawn", **self._world_spawn})
        rows.extend({"kind": "portal", **link} for link in self._portals)
        respawn = self._respawn
        if respawn and respawn.get("lost"):
            rows.append({"kind": "respawn_lost", **respawn})
        elif respawn:
            rows.append({"kind": "respawn", **respawn})
        return rows

    def _compact(self) -> None:
        rows = self._rows_now()
        handle, temp = tempfile.mkstemp(prefix=self.path.name, dir=str(self.path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                for row in rows:
                    out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            os.replace(temp, self.path)
        except OSError:
            try:
                os.unlink(temp)
            except OSError:
                pass
            return
        self._rows = len(rows)

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("t"), (int, float)):
                try:
                    self._fold(row)
                except (KeyError, TypeError, ValueError):
                    continue
                self._rows += 1
        self.revision = self._rows


def observe_tool_result(record: WorldRecord, tool_name: str, params: Mapping[str, Any], result: Any,
                        dimension: Any) -> None:
    """Feed one Body tool result into the record; results that touch no place change nothing."""
    payload = result.to_payload() if hasattr(result, "to_payload") else result
    if not isinstance(payload, Mapping):
        return
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), Mapping) else {}
    success = bool(payload.get("success"))
    if tool_name == "read_container":
        # A read that stopped early still shows what it saw, marked as not every slot.
        if payload.get("reason") in ("container_read", "container_read_incomplete") \
                and isinstance(metrics.get("slots"), list):
            record.container_seen(dimension, metrics.get("pos") or params.get("pos"), slot_counts(metrics["slots"]),
                                  complete=success and metrics.get("complete") is True, source="read_container")
        return
    # Blocks the client saw the server change: a known place taken down or replaced is gone.
    for edit_dimension, edit in _world_edits(metrics):
        before, after = block_name(edit.get("before")), block_name(edit.get("after"))
        if before != after and is_place_block(before):
            record.place_gone(edit_dimension or dimension, edit.get("pos"), f"the block there became {after or 'air'}")
    for seen in [metrics, *(item for item in metrics.get("containers") or () if isinstance(item, Mapping))]:
        if isinstance(seen.get("container_items"), Mapping) and seen.get("container_pos") is not None:
            record.container_seen(dimension, seen["container_pos"], seen["container_items"],
                                  complete=bool(seen.get("container_complete")), source=tool_name,
                                  block=seen.get("container_type"))
    reason = str(payload.get("reason") or "")
    # What a furnace holds after the Body loaded it, looked into it or took things out.
    furnace_looks = [metrics, *(item for item in metrics.get("furnaces") or () if isinstance(item, Mapping))]
    for seen in furnace_looks:
        if isinstance(seen.get("furnace_after"), Mapping) and seen.get("furnace_pos") is not None:
            record.furnace_seen(dimension, seen["furnace_pos"], seen["furnace_after"],
                                loaded=tool_name == "smelt_item" and reason == "loaded", source=tool_name,
                                block=seen.get("furnace_type") or seen.get("furnace_item"))
    if reason == "container_wrong_type" and metrics.get("container_pos") is not None:
        record.place_gone(dimension, metrics["container_pos"],
                          f"the block there is {metrics.get('container_type') or 'something else'} now")
    workspace = metrics.get("workspace") if isinstance(metrics.get("workspace"), Mapping) else {}
    if workspace.get("mode") in ("existing_table", "temporary_table") and workspace.get("table_pos") is not None:
        if _succeeded(metrics.get("reclaim")):
            record.place_gone(dimension, workspace["table_pos"], "picked up again after crafting")
        else:
            record.place_used(dimension, workspace["table_pos"], workspace.get("table_type") or "crafting_table",
                              tool_name)
    if success and metrics.get("furnace_pos") is not None:
        # Only a furnace placed for this smelt has ``furnace_item``; its ``reclaim`` broke it again.  For a
        # standing furnace ``reclaim`` is taking the items back out of its slots.
        if "furnace_item" in metrics and _succeeded(metrics.get("reclaim")):
            record.place_gone(dimension, metrics["furnace_pos"], "picked up again after smelting")
        else:
            record.place_used(dimension, metrics["furnace_pos"],
                              metrics.get("furnace_type") or metrics.get("furnace_item") or "furnace", tool_name)
    target = metrics.get("target") if isinstance(metrics.get("target"), Mapping) else None
    if tool_name == "go_to_bed" and success and target and target.get("pos") is not None:
        record.place_used(dimension, target["pos"], target.get("type") or "bed", tool_name)
    if tool_name == "place_bed_and_sleep" and metrics.get("bed_placed") and metrics.get("bed_position") is not None:
        if metrics.get("bed_recovered"):
            record.place_gone(dimension, metrics["bed_position"], "picked up again after sleeping")
        else:
            record.place_used(dimension, metrics["bed_position"], "bed", tool_name)


def _slot_row(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or not value.get("item") or int(value.get("count") or 0) <= 0:
        return None
    return {"item": block_name(value["item"]), "count": int(value["count"])}


def _world_edits(value: Any):
    """Every confirmed block change in a result, with its receipt's dimension, wherever the receipt sits."""
    if isinstance(value, Mapping):
        receipt = value.get(WORLD_EDIT_RECEIPT)
        if isinstance(receipt, Mapping):
            for edit in receipt.get("edits") or ():
                if isinstance(edit, Mapping):
                    yield receipt.get("dimension"), edit
        for key, item in value.items():
            if key != WORLD_EDIT_RECEIPT:
                yield from _world_edits(item)
    elif isinstance(value, list):
        for item in value:
            yield from _world_edits(item)


def _succeeded(payload: Any) -> bool:
    return isinstance(payload, Mapping) and payload.get("success") is True


def _key(dimension: Any, pos: Any) -> tuple[str, int, int, int] | None:
    where = block_pos(pos)
    if where is None:
        return None
    return (dimension_id(dimension) or "minecraft:overworld", *where)


def _near(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return a.get("dimension") == b.get("dimension") and math.dist(a["pos"], b["pos"]) <= SAME_PORTAL_BLOCKS


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["WorldRecord", "block_name", "block_pos", "dimension_id", "dimension_words", "is_container",
           "is_place_block", "observe_tool_result", "slot_counts"]
