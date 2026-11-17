"""Skills mounted on plan-ladder items (agent-skills-v2-20261105.md §2.1).

A ladder item (a life-profile project) names Skills in ``skills``. The mounted
set is the union of those names over the items on the ladder, in the order
they were first mounted. A mount appends one conversation message carrying the
Skill's full body; an unmount appends one line. History is never edited, and
the instructions never carry a body, so a mount change costs no cached prefix.
The compactor puts still-mounted bodies back after any part of the history it
summarises (``context_compaction.py``).

A ``delegate_work`` plan may carry ``skills`` too (dragon-unit-tests-20261030.md
§6.y.22 item 2): they are mounted the same way while the plan runs, held by
``plan:<activity id>``, and unmount when it ends unless a ladder item or
another running plan still holds them. The caps cover everything mounted.

The Agent chooses every mount; the program only checks names and caps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from minebot.app.life_profile import (
    MAX_MOUNTED_SKILL_CHARS,
    MAX_MOUNTED_SKILLS,
    SkillMountError,
    mounted_skill_names,
    off_ladder,
)
from minebot.brain.context import skill_mounted_message, skill_unmounted_message


class _SkillSource(Protocol):
    def read(self, name: str, version: str | None = None) -> Any: ...


@dataclass(frozen=True)
class Mount:
    name: str
    item_id: str
    text: str
    chars: int


def _document(source: _SkillSource, name: str) -> Any:
    document = source.read(name)
    if document is None or getattr(document, "status", "active") != "active":
        return None
    return document


def skill_mount_check(source: _SkillSource) -> Callable[[list[str], list[str]], None]:
    """The ``update_life_project`` check: written names exist and load; caps hold after the write."""

    def check(written: list[str], mounted: list[str]) -> None:
        for name in written:
            document = _document(source, name)
            if document is None:
                raise SkillMountError("skill_not_found", f"no Skill named {name!r} in AVAILABLE_SKILLS")
            if not getattr(document, "loadable", True):
                missing = ", ".join(getattr(document, "missing_tools", ()) or ())
                raise SkillMountError("skill_dependency_unavailable",
                                      f"Skill {name!r} cannot be loaded now: missing tools {missing}")
        if len(mounted) > MAX_MOUNTED_SKILLS:
            raise SkillMountError(
                "skill_mount_count_exceeded",
                f"this write would mount {len(mounted)} Skills ({', '.join(mounted)}); at most "
                f"{MAX_MOUNTED_SKILLS} Skills may be mounted at once. Remove one from an item first.")
        chars = 0
        for name in mounted:
            document = _document(source, name)
            chars += len(str(getattr(document, "body", "") or "")) if document is not None else 0
        if chars > MAX_MOUNTED_SKILL_CHARS:
            raise SkillMountError(
                "skill_mount_chars_exceeded",
                f"this write would mount {chars} characters of Skill text ({', '.join(mounted)}); at most "
                f"{MAX_MOUNTED_SKILL_CHARS} characters may be mounted at once. Remove one from an item first.")

    return check


PLAN_HOLDER_PREFIX = "plan:"


def plan_holder(activity_id: str) -> str:
    return f"{PLAN_HOLDER_PREFIX}{activity_id}"


def merged_mounts(*groups: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """One (name, holder) per Skill: the first holder in group order keeps it."""
    held: dict[str, str] = {}
    for group in groups:
        for name, holder in group:
            held.setdefault(str(name), str(holder))
    return list(held.items())


def ladder_mounts(projects: Iterable[Mapping[str, Any]],
                  state_of: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> list[tuple[str, str]]:
    """(name, item id) mounted by the items still on the ladder: waiting, running, set aside.

    An item that finished stays in ``projects`` until the Agent's next write;
    it is already off the ladder, as LADDER shows it, so its Skills unmount.
    """
    live = []
    for item in projects:
        if not isinstance(item, Mapping):
            continue
        run = state_of(item) if isinstance(item.get("work"), Mapping) else {}
        if off_ladder(item, run or {}) is None:
            live.append(item)
    return mounted_skill_names(live)


class SkillMounts:
    """Follows the ladder's mounted set and appends a message for each change."""

    def __init__(self, source: _SkillSource, *, deliver: Callable[[str], None],
                 emit: Callable[..., None] = lambda *args, **kwargs: None) -> None:
        self.source = source
        self.deliver = deliver
        self.emit = emit
        self._mounted: dict[str, Mount] = {}

    def sync(self, wanted: Iterable[tuple[str, str]], *, announce: bool = True) -> None:
        """Make the mounted set ``wanted`` (name, item id), in first-mount order.

        ``announce=False`` takes the set as already in the conversation (a
        restart): no message, and the compactor shows any body the visible
        history no longer has.
        """
        wanted = list(wanted)
        names = {name for name, _item in wanted}
        for name in [name for name in self._mounted if name not in names]:
            mount = self._mounted.pop(name)
            if announce:
                self.deliver(skill_unmounted_message(name))
            self.emit("skill_unmounted", name=name, item_id=mount.item_id, chars=mount.chars,
                      announced=announce, **_plan_fields(mount.item_id))
        for name, item_id in wanted:
            if name in self._mounted:
                continue
            document = _document(self.source, name)
            if document is None or not getattr(document, "loadable", True):
                # Checked when written; a Skill file that went away since is
                # reported, never silently swapped.
                self.emit("skill_mount_unavailable", name=name, item_id=item_id, **_plan_fields(item_id))
                continue
            body = str(document.body)
            mount = Mount(name, item_id, skill_mounted_message(name, item_id, body), len(body))
            self._mounted[name] = mount
            if announce:
                self.deliver(mount.text)
            self.emit("skill_mounted", name=name, item_id=item_id, chars=mount.chars, announced=announce,
                      **_plan_fields(item_id))

    def mounted(self) -> list[Mount]:
        return list(self._mounted.values())

    def mounted_texts(self) -> list[str]:
        return [mount.text for mount in self._mounted.values()]

    def budget(self) -> dict[str, int]:
        return {"mounted_skill_count": len(self._mounted),
                "mounted_skill_chars": sum(mount.chars for mount in self._mounted.values())}


def _plan_fields(holder: str) -> dict[str, str]:
    """Trace fields naming the plan that holds a mount, if a plan does."""
    if str(holder).startswith(PLAN_HOLDER_PREFIX):
        return {"holder": "plan", "plan_id": str(holder)[len(PLAN_HOLDER_PREFIX):]}
    return {}


__all__ = ["Mount", "PLAN_HOLDER_PREFIX", "SkillMounts", "ladder_mounts", "merged_mounts", "plan_holder",
           "skill_mount_check"]
