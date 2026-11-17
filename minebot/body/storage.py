"""Storing and taking by the Agent's own storage plan (vision-and-home-20261028.md §7.7, Body).

``store_items`` puts what the Agent does not keep into the containers its plan says hold it, at home
or at an outpost; what no planned container holds stays on the Body and is reported. ``take_items``
takes items from the containers the Body's own record says hold them. Both move items with
``ContainerTransactions.transfer_item``, so every move is checked slot by slot, and both work on
chest, trapped_chest and barrel only, as ``transfer_container_item`` does. Neither decides what to
keep or where a kind of item belongs: the Agent's ``keep`` list and storage plan do.
"""

from __future__ import annotations

import fnmatch
import math
from typing import Any, Callable, Iterable, Mapping

from minebot.body.container import (
    BODY_SLOT_PAGE_SIZE,
    DEFAULT_CONTAINER_TYPES,
    ContainerTransactions,
    _perception_failure,
    _read_paged,
    _slots,
)
from minebot.body.interaction_support import InteractionNavigator, ensure_interaction_range
from minebot.contract import Body, Position, ToolResult

DOUBLE_CHEST_SLOTS = 54
MAIN_SLOTS = 36


def matches(item: str, patterns: Iterable[str]) -> bool:
    """An item id against ids or patterns such as ``*_log``; ``minecraft:`` is ignored on both sides."""
    name = item.removeprefix("minecraft:")
    return any(fnmatch.fnmatchcase(name, str(pattern).removeprefix("minecraft:")) for pattern in patterns)


def _dimension(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "minecraft:overworld"
    if ":" in text:
        return text
    return "minecraft:" + {"nether": "the_nether", "end": "the_end"}.get(text, text)


def _cell(value: Any) -> Position | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        return tuple(int(math.floor(float(axis))) for axis in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


class StorageTransactions:
    """``store_items`` and ``take_items`` over the container transfer that verifies every slot."""

    def __init__(
        self,
        body: Body,
        container: ContainerTransactions,
        *,
        navigator: InteractionNavigator | None,
        profile: Callable[[], Mapping[str, Any] | None],
        places: Callable[[], Iterable[Mapping[str, Any]]],
        dimension: Callable[[], str | None],
    ) -> None:
        self.body = body
        self.container = container
        self.navigator = navigator
        self._profile = profile
        self._places = places
        self._dimension = dimension

    # -- the place ---------------------------------------------------------

    def _site(self, place: str | None) -> tuple[str, Mapping[str, Any]] | ToolResult:
        """Home, or the outpost named ``place``, in the dimension the Body is in."""
        try:
            profile = self._profile() or {}
        except Exception as exc:  # an unreadable profile is not an empty one
            return ToolResult(success=False, reason="life_profile_unreadable", can_retry=False,
                              metrics={"error": str(exc)[:300]})
        home = profile.get("home") if isinstance(profile.get("home"), Mapping) else None
        if place in (None, "", "home") or (home and place == home.get("name")):
            if home is None:
                return ToolResult(success=False, reason="no_home", can_retry=False,
                                  next_suggestion="write a home with update_life first")
            site: tuple[str, Mapping[str, Any]] = ("home", home)
        else:
            outpost = next((row for row in profile.get("outposts") or ()
                            if isinstance(row, Mapping) and row.get("name") == place), None)
            if outpost is None:
                names = [row.get("name") for row in profile.get("outposts") or () if isinstance(row, Mapping)]
                return ToolResult(success=False, reason="place_not_found", can_retry=False,
                                  metrics={"place": place, "outposts": names})
            site = (str(place), outpost)
        here = self._dimension()
        there = _dimension(site[1].get("dimension"))
        # A Body that does not report its dimension is taken at its word, as by every other tool.
        if here is not None and _dimension(here) != there:
            return ToolResult(success=False, reason="place_in_another_dimension", can_retry=False,
                              metrics={"place": site[0], "place_dimension": there, "body_dimension": _dimension(here)})
        return site

    # -- store -------------------------------------------------------------

    def store_items(self, *, keep: Iterable[str], only: Iterable[str] | None = None,
                    place: str | None = None, timeout_s: float = 2.0,
                    approach_timeout_s: float = 30.0) -> ToolResult:
        site = self._site(place)
        if isinstance(site, ToolResult):
            return site
        name, spot = site
        rows = [row for row in spot.get("storage") or () if isinstance(row, Mapping) and _cell(row.get("pos"))]
        carried = self._carried()
        if isinstance(carried, ToolResult):
            return carried
        keep, only = list(keep or ()), list(only or ())
        wanted = {item: count for item, count in carried.items()
                  if not matches(item, keep) and (not only or matches(item, only))}
        planned: dict[Position, list[str]] = {}
        not_stored: list[dict[str, object]] = []
        for item, count in wanted.items():
            row = next((row for row in rows if matches(item, row.get("holds") or ())), None)
            if row is None:
                not_stored.append({"item": item, "count": count, "why": f"no container in the {name}'s storage plan holds it"})
            else:
                planned.setdefault(_cell(row["pos"]), []).append(item)  # type: ignore[arg-type]
        stored: list[dict[str, object]] = []
        containers: list[dict[str, object]] = []
        for pos, items in planned.items():
            moved = self._move(pos, {item: wanted[item] for item in items}, "bot_to_container",
                               timeout_s=timeout_s, approach_timeout_s=approach_timeout_s)
            stored += moved["done"]
            not_stored += moved["short"]
            containers += moved["containers"]
        return self._result("store", name, stored, not_stored, containers,
                            asked=sum(wanted.values()), kept=sorted(set(carried) - set(wanted)))

    # -- take --------------------------------------------------------------

    def take_items(self, *, items: Mapping[str, int], place: str | None = None, timeout_s: float = 2.0,
                   approach_timeout_s: float = 30.0) -> ToolResult:
        site = self._site(place)
        if isinstance(site, ToolResult):
            return site
        name, spot = site
        here = _dimension(spot.get("dimension"))
        cells = {_cell(row.get("pos")) for row in spot.get("storage") or () if isinstance(row, Mapping)}
        area = spot.get("area") if isinstance(spot.get("area"), Mapping) else None
        known = [place_row for place_row in self._places()
                 if _dimension(place_row.get("dimension")) == here and not place_row.get("gone")
                 and str(place_row.get("block") or "").removeprefix("minecraft:") in DEFAULT_CONTAINER_TYPES
                 and isinstance(place_row.get("items"), Mapping)
                 and (_cell(place_row.get("pos")) in cells or _inside(area, place_row.get("pos")))]
        taken: list[dict[str, object]] = []
        short: list[dict[str, object]] = []
        containers: list[dict[str, object]] = []
        for item, count in items.items():
            item = str(item).removeprefix("minecraft:")
            need = int(count)
            holders = sorted((row for row in known if int(row["items"].get(item) or 0) > 0),
                             key=lambda row: -int(row["items"].get(item) or 0))
            for row in holders:
                if need <= 0:
                    break
                moved = self._move(_cell(row["pos"]), {item: min(need, int(row["items"][item]))},  # type: ignore[arg-type]
                                   "container_to_bot", timeout_s=timeout_s, approach_timeout_s=approach_timeout_s)
                got = sum(int(done["count"]) for done in moved["done"])
                need -= got
                taken += moved["done"]
                containers += moved["containers"]
            if need > 0:
                why = ("the Body's record knows no container here holding it" if not holders
                       else "the containers that held it gave less")
                short.append({"item": item, "count": need, "why": why})
        return self._result("take", name, taken, short, containers, asked=sum(int(v) for v in items.values()))

    # -- shared ------------------------------------------------------------

    def _carried(self) -> dict[str, int] | ToolResult:
        inventory = _read_paged(self.body, "inventory", {}, page_size=BODY_SLOT_PAGE_SIZE)
        failed = _perception_failure(inventory)
        if failed is not None:
            return failed
        counts: dict[str, int] = {}
        for slot in _slots(inventory):
            if 0 <= slot.slot < MAIN_SLOTS and not slot.empty and slot.item:
                item = str(slot.item).removeprefix("minecraft:")
                counts[item] = counts.get(item, 0) + slot.count
        return counts

    def _move(self, pos: Position, counts: Mapping[str, int], direction: str, *, timeout_s: float,
              approach_timeout_s: float) -> dict[str, list[dict[str, object]]]:
        out: dict[str, list[dict[str, object]]] = {"done": [], "short": [], "containers": []}
        approach = ensure_interaction_range(
            self.body, self.navigator, pos, timeout_s=approach_timeout_s,
            missing_reason="container_navigation_missing", failure_prefix="container_navigation_failed",
            no_stand_reason="container_no_stand_point")
        if isinstance(approach, ToolResult):
            out["short"] = [{"item": item, "count": count, "pos": list(pos), "why": approach.reason}
                            for item, count in counts.items()]
            return out
        for item, count in counts.items():
            result = self.container.transfer_item(pos, item=item, count=count, direction=direction,
                                                  total_slots=DOUBLE_CHEST_SLOTS, timeout_s=timeout_s)
            metrics = result.metrics or {}
            moved = int(metrics.get("moved_count") or 0)
            if moved > 0:
                out["done"].append({"item": item, "count": moved, "pos": list(pos)})
            if moved < count:
                out["short"].append({"item": item, "count": count - moved, "pos": list(pos), "why": result.reason})
            if "container_items" in metrics:
                # The world record reads these keys from each entry of ``containers``.
                out["containers"].append({"container_pos": list(pos), "container_type": metrics.get("container_type"),
                                          "container_items": metrics["container_items"],
                                          "container_complete": metrics.get("container_complete")})
        return out

    @staticmethod
    def _result(kind: str, place: str, done: list[dict[str, object]], short: list[dict[str, object]],
                containers: list[dict[str, object]], *, asked: int,
                kept: list[str] | None = None) -> ToolResult:
        moved = sum(int(row["count"]) for row in done)
        # The last read of each container is what it holds now.
        latest = {tuple(row["container_pos"]): row for row in containers}
        metrics: dict[str, object] = {"place": place, "containers": list(latest.values()),
                                      "stored" if kind == "store" else "taken": done,
                                      "not_stored" if kind == "store" else "short": short}
        if kept is not None:
            metrics["kept"] = kept
        if asked and moved >= asked:
            return ToolResult(success=True, reason="completed", can_retry=False, metrics=metrics)
        reason = ("nothing_to_store" if kind == "store" and not asked else
                  f"{'stored' if kind == 'store' else 'took'}_none" if moved == 0 else
                  f"{'stored' if kind == 'store' else 'took'}_part")
        return ToolResult(success=kind == "store" and not asked, reason=reason, can_retry=False, metrics=metrics)


def _inside(area: Mapping[str, Any] | None, pos: Any) -> bool:
    cell = _cell(pos)
    if not area or cell is None:
        return False
    start, end = _cell(area.get("from")), _cell(area.get("to"))
    if start is None or end is None:
        return False
    return all(min(a, b) <= value <= max(a, b) for a, b, value in zip(start, end, cell))
