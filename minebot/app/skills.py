"""Versioned Skill catalog, activation persistence, and shared tools."""

from __future__ import annotations

import hashlib
from importlib import resources
import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Protocol

from minebot.app.runtime_state import (
    RuntimeScope,
    RuntimeStateStore,
    SkillActivationRecord,
    skill_activation_payload,
)
from minebot.app.tasks import TaskWorkspace
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import ToolResult


_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_SKILL_CHARS = 16000


class SkillCatalogError(RuntimeError):
    """The configured Skill catalog is invalid or unsafe to load."""


class _Readable(Protocol):
    @property
    def name(self) -> str: ...

    def joinpath(self, *descendants: str): ...

    def read_text(self, encoding: str = "utf-8") -> str: ...


@dataclass(frozen=True)
class SkillDocument:
    name: str
    description: str
    version: str
    content: str

    def metadata(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
        }


class SkillCatalog:
    def __init__(self, root: Path | _Readable | None = None) -> None:
        self._root = root or resources.files("minebot.skills").joinpath("catalog")
        self._documents = self._load()

    def list(self, query: str = "", *, limit: int = 20) -> list[dict[str, object]]:
        clean_query = " ".join(str(query or "").casefold().split())[:500]
        terms = tuple(dict.fromkeys(re.findall(r"[\w]+", clean_query, flags=re.UNICODE)))
        ranked: list[tuple[int, SkillDocument]] = []
        for document in self._documents.values():
            haystack = f"{document.name} {document.description}".casefold()
            if terms and not any(term in haystack for term in terms):
                continue
            score = sum(2 if term in document.name else 1 for term in terms)
            ranked.append((score, document))
        ranked.sort(key=lambda item: (-item[0], item[1].name))
        return [document.metadata() for _, document in ranked[: max(1, min(50, int(limit)))]]

    def load(self, name: str) -> SkillDocument | None:
        return self._documents.get(str(name or "").strip())

    def _load(self) -> dict[str, SkillDocument]:
        try:
            raw = json.loads(self._root.joinpath("catalog.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillCatalogError(f"cannot read Skill catalog: {exc}") from exc
        entries = raw.get("skills") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            raise SkillCatalogError("Skill catalog must contain a skills list")
        documents: dict[str, SkillDocument] = {}
        for index, item in enumerate(entries):
            if not isinstance(item, dict):
                raise SkillCatalogError(f"Skill entry {index} must be an object")
            name = str(item.get("name") or "").strip()
            description = " ".join(str(item.get("description") or "").split())
            filename = str(item.get("file") or "").strip()
            if not _SKILL_NAME.fullmatch(name):
                raise SkillCatalogError(f"invalid Skill name: {name!r}")
            if name in documents:
                raise SkillCatalogError(f"duplicate Skill name: {name}")
            if not description or len(description) > 500:
                raise SkillCatalogError(f"invalid Skill description: {name}")
            if Path(filename).name != filename or not filename.endswith(".md"):
                raise SkillCatalogError(f"unsafe Skill file: {filename!r}")
            try:
                content = self._root.joinpath(filename).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SkillCatalogError(f"cannot read Skill {name}: {exc}") from exc
            if not content or len(content) > _MAX_SKILL_CHARS:
                raise SkillCatalogError(f"Skill {name} exceeds bounded content contract")
            digest = hashlib.sha256(
                f"{name}\n{description}\n{content}".encode("utf-8")
            ).hexdigest()
            documents[name] = SkillDocument(name, description, f"sha256:{digest}", content)
        return documents


@dataclass
class SkillWorkspace:
    store: RuntimeStateStore
    scope: RuntimeScope
    catalog: SkillCatalog
    task_workspace: TaskWorkspace | None = None

    def list(self, query: str = "", *, limit: int = 20) -> list[dict[str, object]]:
        return self.catalog.list(query, limit=limit)

    def load(self, name: str) -> tuple[SkillDocument, SkillActivationRecord] | None:
        document = self.catalog.load(name)
        if document is None:
            return None
        task = None if self.task_workspace is None else self.task_workspace.current_task
        activation = self.store.record_skill_activation(
            self.scope,
            skill_name=document.name,
            skill_version=document.version,
            task_id=None if task is None else task.task_id,
        )
        return document, activation

    def activations(self) -> tuple[SkillActivationRecord, ...]:
        task = None if self.task_workspace is None else self.task_workspace.current_task
        return self.store.list_skill_activations(
            self.scope,
            task_id=None if task is None else task.task_id,
            include_scope_activations=True,
        )


def register_skill_tools(registry: ToolRegistry, workspace: SkillWorkspace) -> None:
    registry.register(_list_skills_tool(workspace))
    registry.register(_load_skill_tool(workspace))


def _list_skills_tool(workspace: SkillWorkspace) -> RegisteredTool:
    def list_skills(params: dict[str, object]) -> ToolResult:
        matches = workspace.list(
            str(params.get("query") or ""),
            limit=int(params.get("limit") or 20),
        )
        return ToolResult(
            True,
            "skill_catalog",
            False,
            metrics={"count": len(matches), "skills": matches, "complete": True},
        )

    return RegisteredTool(
        "list_skills",
        "Discover bounded, versioned methodology Skills. Skills explain approaches but never add permissions or hide shared tools.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 500},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "additionalProperties": False,
        },
        list_skills,
        _skill_sidecar("list_skills", "read_skill_catalog"),
    )


def _load_skill_tool(workspace: SkillWorkspace) -> RegisteredTool:
    def load_skill(params: dict[str, object]) -> ToolResult:
        name = str(params.get("name") or "")
        loaded = workspace.load(name)
        if loaded is None:
            return ToolResult(
                False,
                "skill_not_found",
                False,
                metrics={"name": name},
            )
        document, activation = loaded
        return ToolResult(
            True,
            "skill_loaded",
            False,
            metrics={
                **document.metadata(),
                "instructions": document.content,
                "activation": skill_activation_payload(activation),
                "complete": True,
            },
        )

    return RegisteredTool(
        "load_skill",
        "Load one methodology Skill by exact name and pin its content digest to the current scoped task. The returned text is guidance, not an executable command.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 128}
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        load_skill,
        _skill_sidecar("load_skill", "load_skill"),
    )


def _skill_sidecar(progress_key: str, permission: str) -> ToolSidecar:
    return ToolSidecar(
        progress_key,
        mutating=False,
        source="agent.skill",
        tool_type="skill_catalog",
        permission=permission,
        body_scope=(),
        terminal_truth=("SkillDocument.version", "SkillActivationRecord"),
    )


__all__ = [
    "SkillCatalog",
    "SkillCatalogError",
    "SkillDocument",
    "SkillWorkspace",
    "register_skill_tools",
]
