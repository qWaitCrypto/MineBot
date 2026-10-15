"""The Agent's own life profile: one JSON file per world and bot.

The Agent writes and revises it (``read_life`` / ``update_life`` /
``update_life_project``); the reflex loop reads it every idle frame.  The
program checks only shape and the write boundary of projects, never whether
the content is right (``docs/design-docs/life-g4-g5-20261015.md``).
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROFILE_FIELDS = ("home", "mine", "routine", "relationships", "personality")
PROJECT_STATUSES = ("active", "paused", "done")
MAX_TEXT_CHARS = 1000
MAX_RELATIONSHIPS = 20
MAX_AREAS = 16
MAX_CONTAINERS = 32
MAX_PROJECTS = 12
MAX_PROFILE_CHARS = 64_000
KEPT_CHANGES = 20
_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")


class LifeProfileError(ValueError):
    """The submitted profile or project does not have the required shape."""


class LifeRevisionConflict(LifeProfileError):
    """The profile changed since the writer read it."""


def load_profile(path: str | None = None) -> dict[str, Any]:
    raw = path or os.environ.get("MINEBOT_LIFE_PROFILE")
    if not raw:
        return {}
    try:
        value = json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def life_intent(profile: Mapping[str, Any]) -> str:
    parts = []
    for key in ("routine", "personality"):
        if isinstance(profile.get(key), str) and profile[key].strip():
            parts.append(profile[key].strip())
    relationships = profile.get("relationships")
    if isinstance(relationships, list):
        parts.extend(str(item.get("note")) for item in relationships
                     if isinstance(item, dict) and item.get("note"))
    return " ".join(parts)


def life_file_path(directory: Path, scope: Any) -> Path:
    """One file per server, world and bot; the name stays readable."""
    readable = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(scope.bot_id))[:48] or "bot"
    return Path(directory) / f"{readable}-{scope.key[:16]}.json"


class LifeStore:
    """Versioned life profile and project log for one ``RuntimeScope``.

    Writes go through a temporary file and a rename, so a crash never leaves a
    half-written profile.  ``changes`` keeps the reasons of the last
    ``KEPT_CHANGES`` revisions; project results are program-written facts and
    live in a separate append-only log beside the profile.
    """

    def __init__(self, directory: Path, scope: Any, *, seed_path: str | None = None) -> None:
        self.path = life_file_path(directory, scope)
        self.log_path = self.path.with_suffix(".project-log.jsonl")
        self.scope = scope
        self._lock = threading.Lock()
        if not self.path.exists():
            seed = load_profile(seed_path)
            if seed:
                try:
                    profile = normalize_profile(seed, partial=True)
                except LifeProfileError:
                    profile = None
                if profile is not None:
                    self._write({"revision": 1, "profile": profile, "changes": [
                        {"revision": 1, "at": _now(), "author": "user_file",
                         "reason": "imported from MINEBOT_LIFE_PROFILE"}]})

    # -- reads -------------------------------------------------------------

    def version(self) -> tuple[int, int] | None:
        """A cheap change marker for per-frame reads: (mtime_ns, size)."""
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def read(self) -> dict[str, Any]:
        with self._lock:
            return self._read()

    def profile(self) -> dict[str, Any]:
        return self.read()["profile"]

    def project_results(self, project_id: str | None = None, *, limit: int = 3) -> list[dict[str, Any]]:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and (project_id is None or row.get("project") == project_id):
                rows.append(row)
                if len(rows) >= limit:
                    break
        return list(reversed(rows))

    # -- writes ------------------------------------------------------------

    def update(self, changes: Mapping[str, Any], *, expected_revision: int, reason: str,
               author: str = "agent") -> dict[str, Any]:
        unknown = set(changes) - set(PROFILE_FIELDS)
        if unknown:
            raise LifeProfileError(
                f"unknown profile fields {sorted(unknown)}; use {list(PROFILE_FIELDS)} "
                "(projects change through update_life_project)")
        if not changes:
            raise LifeProfileError("no changes given")
        with self._lock:
            record = self._read()
            self._check_revision(record, expected_revision)
            profile = dict(record["profile"])
            for key, value in changes.items():
                if value is None:
                    profile.pop(key, None)
                else:
                    profile[key] = value
            profile = normalize_profile(profile, partial=True)
            return self._commit(record, profile, reason=reason, author=author)

    def update_project(self, operation: str, project: Mapping[str, Any], *, expected_revision: int,
                       reason: str, check_write: Callable[[Mapping[str, Any], Mapping[str, Any]], None] | None = None,
                       author: str = "agent") -> dict[str, Any]:
        if operation not in {"upsert", "remove"}:
            raise LifeProfileError("operation must be 'upsert' or 'remove'")
        project_id = str(project.get("id") or "")
        if not _PROJECT_ID.fullmatch(project_id):
            raise LifeProfileError("project id must be 1-48 lowercase letters, digits or '-'")
        with self._lock:
            record = self._read()
            self._check_revision(record, expected_revision)
            profile = dict(record["profile"])
            projects = [item for item in profile.get("projects") or [] if item.get("id") != project_id]
            if operation == "upsert":
                previous = next((item for item in profile.get("projects") or []
                                 if item.get("id") == project_id), {})
                merged = {**previous, **{key: value for key, value in project.items() if value is not None}}
                checked = normalize_project(merged, profile)
                if check_write is not None:
                    check_write(checked, profile)
                position = next((index for index, item in enumerate(profile.get("projects") or [])
                                 if item.get("id") == project_id), len(projects))
                projects.insert(position, checked)
            elif len(projects) == len(profile.get("projects") or []):
                raise LifeProfileError(f"no project {project_id!r}")
            if len(projects) > MAX_PROJECTS:
                raise LifeProfileError(f"at most {MAX_PROJECTS} projects")
            profile["projects"] = projects
            if not projects:
                profile.pop("projects")
            return self._commit(record, normalize_profile(profile, partial=True), reason=reason, author=author)

    def record_project_result(self, project_id: str, result: Mapping[str, Any]) -> None:
        row = {"at": _now(), "project": project_id, **json.loads(json.dumps(result, default=str))}
        with self._lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    # -- internals ---------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"revision": 0, "profile": {}, "changes": []}
        if not isinstance(value, dict) or not isinstance(value.get("profile"), dict):
            return {"revision": 0, "profile": {}, "changes": []}
        return {"revision": int(value.get("revision") or 0), "profile": value["profile"],
                "changes": list(value.get("changes") or [])}

    @staticmethod
    def _check_revision(record: Mapping[str, Any], expected: int) -> None:
        if int(expected) != int(record["revision"]):
            raise LifeRevisionConflict(
                f"the profile is at revision {record['revision']}, not {expected}; read_life and retry")

    def _commit(self, record: Mapping[str, Any], profile: dict[str, Any], *, reason: str,
                author: str) -> dict[str, Any]:
        reason = str(reason or "").strip()
        if not reason:
            raise LifeProfileError("say in one sentence why the profile changes")
        revision = int(record["revision"]) + 1
        changes = [*record["changes"], {"revision": revision, "at": _now(), "author": author,
                                        "reason": reason[:MAX_TEXT_CHARS]}][-KEPT_CHANGES:]
        value = {"revision": revision, "profile": profile, "changes": changes}
        self._write(value)
        return copy.deepcopy(value)

    def _write(self, value: Mapping[str, Any]) -> None:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        if len(text) > MAX_PROFILE_CHARS:
            raise LifeProfileError(f"the profile would exceed {MAX_PROFILE_CHARS} characters")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


# -- shape checks ------------------------------------------------------------


def normalize_profile(profile: Mapping[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """Check the shape of every known field; keep unknown seed fields out."""
    out: dict[str, Any] = {}
    if profile.get("home") is not None:
        home = profile["home"]
        if not isinstance(home, Mapping):
            raise LifeProfileError("home must be an object with pos")
        out["home"] = {"pos": _point(home.get("pos"), "home.pos")}
        _optional_text(home, out["home"], "dimension", "home.dimension")
        _optional_text(home, out["home"], "note", "home.note")
    if profile.get("mine") is not None:
        out["mine"] = _mine(profile["mine"])
    for key in ("routine", "personality"):
        if profile.get(key) is not None:
            out[key] = _text(profile[key], key)
    if profile.get("relationships") is not None:
        relationships = profile["relationships"]
        if not isinstance(relationships, list) or len(relationships) > MAX_RELATIONSHIPS:
            raise LifeProfileError(f"relationships must be a list of at most {MAX_RELATIONSHIPS}")
        out["relationships"] = []
        for index, item in enumerate(relationships):
            if not isinstance(item, Mapping) or not str(item.get("player") or "").strip():
                raise LifeProfileError(f"relationships[{index}] needs a player name")
            row = {"player": _text(item["player"], f"relationships[{index}].player")}
            _optional_text(item, row, "note", f"relationships[{index}].note")
            out["relationships"].append(row)
    if profile.get("projects") is not None:
        projects = profile["projects"]
        if not isinstance(projects, list):
            raise LifeProfileError("projects must be a list")
        out["projects"] = [normalize_project(item, {**profile, **out}) for item in projects]
    return out


def normalize_project(project: Mapping[str, Any], profile: Mapping[str, Any]) -> dict[str, Any]:
    from minebot.app.work_runtime import WorkPlanError, validate_plan

    if not isinstance(project, Mapping):
        raise LifeProfileError("a project must be an object")
    project_id = str(project.get("id") or "")
    if not _PROJECT_ID.fullmatch(project_id):
        raise LifeProfileError("project id must be 1-48 lowercase letters, digits or '-'")
    out = {"id": project_id, "intent": _text(project.get("intent"), f"{project_id}.intent")}
    status = str(project.get("status") or "active")
    if status not in PROJECT_STATUSES:
        raise LifeProfileError(f"{project_id}.status must be one of {list(PROJECT_STATUSES)}")
    out["status"] = status
    _optional_text(project, out, "note", f"{project_id}.note")
    areas = {area["name"] for area in (profile.get("mine") or {}).get("areas") or []
             if isinstance(area, Mapping) and isinstance(area.get("name"), str)}
    area = str(project.get("area") or "")
    if area not in areas:
        raise LifeProfileError(
            f"{project_id}.area must name one of the profile's mine.areas {sorted(areas)}; "
            "mark the place as yours first with update_life")
    out["area"] = area
    work = project.get("work")
    if not isinstance(work, Mapping):
        raise LifeProfileError(f"{project_id}.work must be one minebot-work/0.2 work node")
    if not str(work.get("intent") or "").strip():
        # As in delegate_work: the node's own intent, written as Body facts, is
        # what "is it done" checks; the project sentence is not.
        raise LifeProfileError(
            f"{project_id}.work.intent is required: the result as facts the Body reports "
            "(item ids and counts, held item, position)")
    try:
        validate_plan(project_plan(out, work))
    except WorkPlanError as exc:
        raise LifeProfileError(f"{project_id}.work: {exc}") from exc
    out["work"] = json.loads(json.dumps(work))
    return out


def project_plan(project: Mapping[str, Any], work: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The project as a delegate_work plan: same language, same executor."""
    node = dict(work if work is not None else project["work"])
    return {"language": "minebot-work/0.2", "goal": f"my project {project['id']}: {project['intent']}",
            "timeout_s": 1800, "work": node}


def project_write_points(work: Any, is_world_write: Callable[[str], bool]) -> Iterable[tuple[str, list[float]]]:
    """Every coordinate a world-writing call in the tree names."""
    if isinstance(work, Mapping):
        if work.get("kind") == "call" and is_world_write(str(work.get("tool") or "")):
            for point in _points(work.get("arguments")):
                yield str(work.get("tool")), point
        for value in work.values():
            if isinstance(value, (Mapping, list)):
                yield from project_write_points(value, is_world_write)
    elif isinstance(work, list):
        for item in work:
            yield from project_write_points(item, is_world_write)


def inside_area(point: list[float], area: Mapping[str, Any]) -> bool:
    low = [min(a, b) for a, b in zip(area["from"], area["to"])]
    high = [max(a, b) for a, b in zip(area["from"], area["to"])]
    return all(lo <= value <= hi + 1 for lo, value, hi in zip(low, point, high))


def life_summary(record: Mapping[str, Any], results: Callable[[str], list[dict[str, Any]]]) -> dict[str, Any]:
    """What the Agent sees each turn: where it lives, how, and its projects."""
    profile = record.get("profile") or {}
    summary: dict[str, Any] = {"revision": record.get("revision", 0)}
    if not profile:
        summary["note"] = ("You have no life profile in this world yet. Write one with update_life: "
                           "a home at coordinates you have read, a routine, how you treat players.")
        return summary
    for key in ("home", "routine", "personality", "relationships"):
        if key in profile:
            summary[key] = profile[key]
    if profile.get("mine"):
        summary["mine_areas"] = [area["name"] for area in profile["mine"].get("areas") or []]
    projects = []
    for project in profile.get("projects") or []:
        row = {key: project[key] for key in ("id", "intent", "status", "note", "area") if key in project}
        last = results(project["id"])
        if last:
            row["last_result"] = last[-1]
        projects.append(row)
    if projects:
        summary["projects"] = projects
    return summary


# -- Agent tools -------------------------------------------------------------

_LIFE_TEXT_RULE = (
    "Write everything in English. Use coordinates you have actually read, with the dimension. "
    "Follow or stand-by is not a switch: say it in a routine or relationship sentence; the reflex "
    "layer reads those sentences every second while you are not driving the Body."
)


def register_life_tools(registry: Any, store: LifeStore) -> None:
    """``read_life`` / ``update_life`` / ``update_life_project``; none moves the Body."""
    from minebot.brain.registry import RegisteredTool, ToolSidecar
    from minebot.contract import ToolResult

    def sidecar(name: str, permission: str, tool_type: str) -> Any:
        return ToolSidecar(name, mutating=False, source="agent.life", tool_type=tool_type,
                           permission=permission, body_scope=(), terminal_truth=("LifeProfile.revision",))

    def shown(_reason: str, metrics: dict[str, object]) -> dict[str, object]:
        return dict(metrics)

    def read(_params: dict[str, object]) -> Any:
        record = store.read()
        projects = [item.get("id") for item in record["profile"].get("projects") or []]
        return ToolResult(True, "life_read", False, metrics={
            "revision": record["revision"], "profile": record["profile"],
            "recent_changes": record["changes"][-5:],
            "project_results": {project: store.project_results(project) for project in projects},
        })

    def rejected(reason: str, exc: Exception) -> Any:
        return ToolResult(False, reason, True, metrics={"error": str(exc), "revision": store.read()["revision"]})

    def update(params: dict[str, object]) -> Any:
        changes = params.get("changes")
        try:
            if not isinstance(changes, dict):
                raise LifeProfileError("changes must be an object of profile fields")
            record = store.update(changes, expected_revision=int(params.get("expected_revision") or 0),
                                  reason=str(params.get("reason") or ""))
        except (LifeProfileError, TypeError, ValueError) as exc:
            return rejected("life_update_rejected", exc)
        return ToolResult(True, "life_updated", False, metrics={"revision": record["revision"],
                                                                 "profile": record["profile"]})

    def is_world_write(tool_name: str) -> bool:
        if tool_name not in registry:
            return False
        tool_sidecar = registry.get(tool_name).sidecar
        return tool_sidecar.can_mutate_body and "blocks" in tool_sidecar.body_scope

    def check_write(project: Mapping[str, Any], profile: Mapping[str, Any]) -> None:
        unknown = sorted({str(node.get("tool")) for node in _calls(project["work"])} - set(registry.names()))
        if unknown:
            raise LifeProfileError(f"{project['id']}.work uses unregistered tools {unknown}")
        area = next(item for item in profile["mine"]["areas"] if item["name"] == project["area"])
        for tool_name, point in project_write_points(project["work"], is_world_write):
            if not inside_area(point, area):
                raise LifeProfileError(
                    f"{project['id']}: {tool_name} writes at {point}, outside your area "
                    f"{area['name']!r} {area['from']}..{area['to']}; a project only changes blocks in its own area")

    def update_project(params: dict[str, object]) -> Any:
        project = params.get("project")
        try:
            if not isinstance(project, dict):
                raise LifeProfileError("project must be an object with at least an id")
            record = store.update_project(
                str(params.get("operation") or ""), project,
                expected_revision=int(params.get("expected_revision") or 0),
                reason=str(params.get("reason") or ""), check_write=check_write)
        except (LifeProfileError, TypeError, ValueError) as exc:
            return rejected("life_project_rejected", exc)
        return ToolResult(True, "life_project_updated", False, metrics={
            "revision": record["revision"], "projects": record["profile"].get("projects") or []})

    revision = {"type": "integer", "minimum": 0,
                "description": "The revision read_life returned; a stale revision is rejected."}
    reason = {"type": "string", "description": "One sentence: why your life changes."}
    registry.register(RegisteredTool(
        "read_life",
        "Read your own life profile in this world: home, the areas and containers that are yours, "
        "routine, personality, how you treat each player, your projects, and the last results of each project.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        read, sidecar("read_life", "read_life", "life_query"), projector=shown,
    ))
    registry.register(RegisteredTool(
        "update_life",
        "Write or revise your own life profile. It persists across sessions in this world. Give only the "
        "fields that change; null removes one. Fields: home {pos, dimension, note}; mine {areas: [{name, "
        "from, to, dimension}], containers: [[x,y,z]]} — only places you made or claimed, never a "
        "player's; routine (text); personality (text); relationships [{player, note}]. " + _LIFE_TEXT_RULE,
        {"type": "object", "properties": {
            "changes": {"type": "object", "properties": {
                "home": {"type": ["object", "null"]}, "mine": {"type": ["object", "null"]},
                "routine": {"type": ["string", "null"]}, "personality": {"type": ["string", "null"]},
                "relationships": {"type": ["array", "null"]},
            }, "additionalProperties": False},
            "expected_revision": revision, "reason": reason,
        }, "required": ["changes", "expected_revision", "reason"], "additionalProperties": False},
        update, sidecar("update_life", "write_life", "life_write"), projector=shown,
    ))
    registry.register(RegisteredTool(
        "update_life_project",
        "Add, change or remove one of your own projects (a field, tidying a chest, a small build). "
        "project: {id, intent, status: active|paused|done, note, area, work}. intent is one sentence on what "
        "the project is for; it is shown when the Body chooses what to do next. area names one of your "
        "mine.areas; every block a project call changes must lie in it. work is one minebot-work/0.2 work "
        "node, as in delegate_work, and its own intent (and each step's) is written as facts the Body "
        "reports (item ids and counts, held item, position): that is what is checked to see a step is done. When idle, the reflex layer may pick an active project and walk "
        "its tree; it starts again from the top each time, so use calls that are safe to repeat "
        "(farm_area harvests only ripe crops, build_schematic skips placed blocks). Upsert merges the "
        "given fields into the existing project. " + _LIFE_TEXT_RULE,
        {"type": "object", "properties": {
            "operation": {"type": "string", "enum": ["upsert", "remove"]},
            "project": {"type": "object"},
            "expected_revision": revision, "reason": reason,
        }, "required": ["operation", "project", "expected_revision", "reason"], "additionalProperties": False},
        update_project, sidecar("update_life_project", "write_life", "life_write"), projector=shown,
    ))


def _calls(work: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(work, Mapping):
        if work.get("kind") == "call":
            yield work
        for value in work.values():
            if isinstance(value, (Mapping, list)):
                yield from _calls(value)
    elif isinstance(work, list):
        for item in work:
            yield from _calls(item)


def _mine(mine: Any) -> dict[str, Any]:
    if not isinstance(mine, Mapping):
        raise LifeProfileError("mine must be an object with areas and containers")
    out: dict[str, Any] = {}
    areas = mine.get("areas") or []
    if not isinstance(areas, list) or len(areas) > MAX_AREAS:
        raise LifeProfileError(f"mine.areas must be a list of at most {MAX_AREAS}")
    names = set()
    out["areas"] = []
    for index, area in enumerate(areas):
        if not isinstance(area, Mapping):
            raise LifeProfileError(f"mine.areas[{index}] must be an object")
        name = _text(area.get("name"), f"mine.areas[{index}].name")
        if name in names:
            raise LifeProfileError(f"mine.areas has two areas named {name!r}")
        names.add(name)
        row = {"name": name, "from": _point(area.get("from"), f"mine.areas[{index}].from"),
               "to": _point(area.get("to"), f"mine.areas[{index}].to")}
        _optional_text(area, row, "dimension", f"mine.areas[{index}].dimension")
        out["areas"].append(row)
    containers = mine.get("containers") or []
    if not isinstance(containers, list) or len(containers) > MAX_CONTAINERS:
        raise LifeProfileError(f"mine.containers must be a list of at most {MAX_CONTAINERS}")
    out["containers"] = [_point(item, f"mine.containers[{index}]") for index, item in enumerate(containers)]
    return out


def _points(value: Any) -> Iterable[list[float]]:
    if isinstance(value, list):
        if len(value) == 3 and all(_number(item) for item in value):
            yield [float(item) for item in value]
            return
        for item in value:
            yield from _points(item)
    elif isinstance(value, Mapping):
        if all(_number(value.get(axis)) for axis in ("x", "y", "z")):
            yield [float(value[axis]) for axis in ("x", "y", "z")]
        for item in value.values():
            if isinstance(item, (list, Mapping)):
                yield from _points(item)


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _point(value: Any, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3 or not all(_number(item) for item in value):
        raise LifeProfileError(f"{name} must be three numbers [x, y, z]")
    return [float(item) if isinstance(item, float) else item for item in value]


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LifeProfileError(f"{name} must be non-empty text")
    if len(value) > MAX_TEXT_CHARS:
        raise LifeProfileError(f"{name} must be at most {MAX_TEXT_CHARS} characters")
    return value.strip()


def _optional_text(source: Mapping[str, Any], target: dict[str, Any], key: str, name: str) -> None:
    if source.get(key) is not None:
        target[key] = _text(source[key], name)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "LifeProfileError", "LifeRevisionConflict", "LifeStore", "inside_area", "life_file_path",
    "life_intent", "life_summary", "load_profile", "normalize_profile", "normalize_project",
    "project_plan", "project_write_points", "register_life_tools",
]
