"""Progressively disclosed, scoped, versioned Agent Skills."""

from __future__ import annotations

import hashlib
from importlib import resources
import json
from dataclasses import dataclass, field
from pathlib import Path
import re
import threading
from typing import Iterable, Protocol

from minebot.app.runtime_state import (
    RuntimeScope,
    RuntimeStateConflict,
    RuntimeStateStore,
    SkillActivationRecord,
    SkillHeadRecord,
    SkillVersionRecord,
    skill_activation_payload,
)
from minebot.app.skill_format import (
    ParsedSkill,
    SkillFormatError,
    build_skill_markdown,
    canonical_skill_markdown,
    parse_skill_markdown,
)
from minebot.app.tasks import TaskWorkspace
from minebot.brain.context import AgentContext
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import ToolResult


MAX_SKILL_DESCRIPTOR_CONTEXT_CHARS = 16_000
MAX_ACTIVE_SKILLS = 3
MAX_ACTIVE_SKILL_CONTEXT_CHARS = 24_000
_SECRET_PATTERN = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{20,}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bAIza[A-Za-z0-9_-]{30,}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


class SkillCatalogError(RuntimeError):
    """The configured Skill catalog is invalid or unsafe to load."""


class SkillOperationError(RuntimeError):
    """A typed, model-correctable Skill operation failure."""

    def __init__(self, code: str, message: str, *, can_retry: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.can_retry = can_retry


class SkillRecoveryError(SkillOperationError):
    """An active pinned version cannot be reconstructed exactly."""


class _Readable(Protocol):
    @property
    def name(self) -> str: ...

    def joinpath(self, *descendants: str): ...

    def read_text(self, encoding: str = "utf-8") -> str: ...

    def iterdir(self): ...

    def is_dir(self) -> bool: ...


@dataclass(frozen=True)
class SkillDocument:
    name: str
    description: str
    version: str
    body: str
    source: str
    tools: tuple[str, ...]
    origin: str
    revision: int
    skill_id: str
    status: str = "active"
    derived_from: str = ""
    evidence_refs: tuple[str, ...] = ()
    change_reason: str = ""
    loadable: bool = True
    missing_tools: tuple[str, ...] = ()

    @property
    def content(self) -> str:
        """Backward-compatible alias for the activated methodology body."""

        return self.body

    def metadata(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "head_version": self.version,
            "revision": self.revision,
            "origin": self.origin,
            "status": self.status,
            "tools": list(self.tools),
            "loadable": self.loadable,
            "missing_tools": list(self.missing_tools),
            "derived_from": self.derived_from or None,
        }


@dataclass(frozen=True)
class SkillCatalogSnapshot:
    revision: str
    descriptors: tuple[dict[str, object], ...]
    rendered: str


class SkillCatalog:
    """Immutable packaged Skill source; ``SkillWorkspace`` adds learned heads."""

    def __init__(self, root: Path | _Readable | None = None) -> None:
        self._root = root or resources.files("minebot.skills").joinpath("catalog")
        self._documents = self._load()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._documents))

    def list(self, query: str = "", *, limit: int = 20) -> list[dict[str, object]]:
        documents = _rank_documents(self._documents.values(), query)
        return [document.metadata() for document in documents[: max(1, min(50, int(limit)))]]

    def load(self, name: str, version: str | None = None) -> SkillDocument | None:
        document = self._documents.get(str(name or "").strip())
        if document is None:
            return None
        if version is not None and str(version) != document.version:
            return None
        return document

    def validate_dependencies(self, registered_tools: Iterable[str]) -> None:
        available = frozenset(str(item) for item in registered_tools)
        broken = {
            name: tuple(tool for tool in document.tools if tool not in available)
            for name, document in self._documents.items()
        }
        broken = {name: missing for name, missing in broken.items() if missing}
        if broken:
            details = "; ".join(
                f"{name}: {', '.join(missing)}" for name, missing in sorted(broken.items())
            )
            raise SkillCatalogError(f"built-in Skill dependency mismatch: {details}")

    def _load(self) -> dict[str, SkillDocument]:
        try:
            children = tuple(self._root.iterdir())
        except OSError as exc:
            raise SkillCatalogError(f"cannot read Skill catalog: {exc}") from exc
        documents: dict[str, SkillDocument] = {}
        for child in sorted(children, key=lambda item: item.name):
            if not child.is_dir():
                raise SkillCatalogError(f"unexpected file in Skill catalog: {child.name}")
            try:
                parsed = parse_skill_markdown(
                    child.joinpath("SKILL.md").read_text(encoding="utf-8")
                )
            except (OSError, SkillFormatError) as exc:
                raise SkillCatalogError(f"invalid built-in Skill {child.name}: {exc}") from exc
            if child.name != parsed.name:
                raise SkillCatalogError(
                    f"Skill directory/name mismatch: {child.name!r} != {parsed.name!r}"
                )
            if parsed.name in documents:
                raise SkillCatalogError(f"duplicate Skill name: {parsed.name}")
            documents[parsed.name] = _builtin_document(parsed)
        if not documents:
            raise SkillCatalogError("built-in Skill catalog is empty")
        return documents


@dataclass
class SkillWorkspace:
    store: RuntimeStateStore
    scope: RuntimeScope
    catalog: SkillCatalog
    task_workspace: TaskWorkspace | None = None
    _registered_tools: frozenset[str] = field(default_factory=frozenset, init=False, repr=False)
    _owner_kind: str | None = field(default=None, init=False, repr=False)
    _owner_id: str | None = field(default=None, init=False, repr=False)
    _owner_task_id: str | None = field(default=None, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.store.register_scope(self.scope)
        self.store.end_transient_skill_activations(self.scope)
        self.store.end_terminal_task_skill_activations(self.scope)

    def bind_registry(self, registry: ToolRegistry) -> None:
        names = frozenset(registry.names())
        self.catalog.validate_dependencies(names)
        self._registered_tools = names
        self.catalog_snapshot()

    def set_activation_owner(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        task_id: str | None = None,
    ) -> None:
        if owner_kind not in {"turn", "task", "maintenance"}:
            raise ValueError(f"invalid Skill activation owner: {owner_kind}")
        with self._lock:
            self._owner_kind = owner_kind
            self._owner_id = str(owner_id)
            self._owner_task_id = None if task_id is None else str(task_id)

    def end_activation_owner(self, *, owner_kind: str, owner_id: str) -> int:
        ended = self.store.end_skill_activation_owner(
            self.scope,
            owner_kind=owner_kind,
            owner_id=owner_id,
        )
        with self._lock:
            if self._owner_kind == owner_kind and self._owner_id == owner_id:
                self._owner_kind = None
                self._owner_id = None
                self._owner_task_id = None
        return ended

    def end_task(self, task_id: str) -> int:
        return self.store.end_task_skill_activations(self.scope, task_id)

    def list(
        self,
        query: str = "",
        *,
        start: int = 0,
        limit: int = 20,
    ) -> dict[str, object]:
        documents = _rank_documents(self._active_catalog_documents(), query)
        start = max(0, int(start))
        limit = max(1, min(50, int(limit)))
        page = documents[start : start + limit]
        next_start = start + len(page) if start + len(page) < len(documents) else None
        return {
            "query": " ".join(str(query or "").split())[:500],
            "start": start,
            "limit": limit,
            "count": len(page),
            "total_matches": len(documents),
            "skills": [document.metadata() for document in page],
            "next_start": next_start,
            "complete": next_start is None,
        }

    def read(self, name: str, version: str | None = None) -> SkillDocument | None:
        clean_name = str(name or "").strip()
        builtin = self.catalog.load(clean_name, version)
        if builtin is not None:
            return builtin
        head = self.store.get_skill_head(self.scope, clean_name, include_retired=True)
        if head is None:
            return None
        if head.status == "retired" and version is None:
            return None
        record = self.store.get_skill_version(
            head.skill_id,
            version_digest=version or head.head_version,
        )
        if record is None:
            return None
        return self._learned_document(head, record)

    def load(self, name: str, version: str | None = None) -> tuple[SkillDocument, SkillActivationRecord]:
        document = self.read(name, version)
        if document is None or document.status != "active":
            raise SkillOperationError("skill_not_found", f"Skill is not active: {name}")
        if not document.loadable:
            raise SkillOperationError(
                "skill_dependency_unavailable",
                f"Skill dependencies are unavailable: {', '.join(document.missing_tools)}",
                can_retry=True,
            )
        owner_kind, owner_id, task_id = self._activation_owner()
        active = [
            item
            for item in self.active_documents()
            if item.name != document.name
        ]
        _validate_active_context([*active, document])
        self.store.end_skill_activations_for_name(
            self.scope,
            owner_kind=owner_kind,
            owner_id=owner_id,
            skill_name=document.name,
            except_version=document.version,
        )
        activation = self.store.record_skill_activation(
            self.scope,
            skill_id=document.skill_id,
            skill_name=document.name,
            skill_version=document.version,
            owner_kind=owner_kind,
            owner_id=owner_id,
            task_id=task_id,
        )
        return document, activation

    def create(
        self,
        *,
        name: str,
        description: str,
        tools: Iterable[str],
        body: str,
        evidence_refs: Iterable[str],
        derived_from: str = "",
    ) -> SkillDocument:
        self._require_bound_registry()
        clean_evidence = _validate_evidence_refs(evidence_refs)
        parsed = self._parse_fields(name, description, tools, body)
        if parsed.name in self.catalog.names:
            raise SkillOperationError(
                "builtin_skill_immutable",
                f"Built-in Skill names are reserved: {parsed.name}",
            )
        if self.store.get_skill_head(self.scope, parsed.name, include_retired=True) is not None:
            raise SkillOperationError(
                "skill_name_conflict",
                f"A learned Skill already owns this name: {parsed.name}",
                can_retry=True,
            )
        candidate = _prospective_learned_document(parsed, revision=1, derived_from=derived_from)
        self._validate_catalog_after(candidate)
        try:
            head, version = self.store.create_skill_head(
                self.scope,
                name=parsed.name,
                version_digest=parsed.version_digest,
                description=parsed.description,
                tools=parsed.tools,
                body=parsed.body,
                evidence_refs=clean_evidence,
                change_reason="created",
                derived_from=str(derived_from or ""),
            )
        except (RuntimeStateConflict, ValueError) as exc:
            raise SkillOperationError("skill_create_conflict", str(exc), can_retry=True) from exc
        return self._learned_document(head, version)

    def update(
        self,
        *,
        name: str,
        expected_revision: int,
        description: str | None,
        tools: Iterable[str] | None,
        body: str | None,
        evidence_refs: Iterable[str],
        change_reason: str,
    ) -> SkillDocument:
        self._require_bound_registry()
        clean_name = str(name or "").strip()
        if clean_name in self.catalog.names:
            raise SkillOperationError("builtin_skill_immutable", f"Built-in Skill is immutable: {clean_name}")
        current = self.read(clean_name)
        if current is None or current.origin != "learned":
            raise SkillOperationError("skill_not_found", f"Learned Skill is not active: {clean_name}")
        clean_evidence = _validate_evidence_refs(evidence_refs)
        clean_reason = _validate_change_reason(change_reason)
        parsed = self._parse_fields(
            current.name,
            current.description if description is None else description,
            current.tools if tools is None else tools,
            current.body if body is None else body,
        )
        if parsed.version_digest == current.version:
            raise SkillOperationError("skill_no_changes", f"Skill content is unchanged: {name}")
        candidate = _prospective_learned_document(
            parsed,
            revision=current.revision + 1,
            derived_from=current.derived_from,
        )
        self._validate_catalog_after(candidate, replacing=clean_name)
        try:
            head, version = self.store.update_skill_head(
                self.scope,
                name=clean_name,
                expected_revision=int(expected_revision),
                version_digest=parsed.version_digest,
                description=parsed.description,
                tools=parsed.tools,
                body=parsed.body,
                evidence_refs=clean_evidence,
                change_reason=clean_reason,
            )
        except (RuntimeStateConflict, ValueError) as exc:
            raise SkillOperationError("skill_update_conflict", str(exc), can_retry=True) from exc
        return self._learned_document(head, version)

    def delete(
        self,
        *,
        name: str,
        expected_revision: int,
        evidence_refs: Iterable[str],
        reason: str,
    ) -> SkillHeadRecord:
        clean_name = str(name or "").strip()
        if clean_name in self.catalog.names:
            raise SkillOperationError("builtin_skill_immutable", f"Built-in Skill is immutable: {clean_name}")
        clean_evidence = _validate_evidence_refs(evidence_refs)
        clean_reason = _validate_change_reason(reason)
        try:
            return self.store.retire_skill_head(
                self.scope,
                name=clean_name,
                expected_revision=int(expected_revision),
                evidence_refs=clean_evidence,
                reason=clean_reason,
            )
        except (RuntimeStateConflict, ValueError) as exc:
            raise SkillOperationError("skill_delete_conflict", str(exc), can_retry=True) from exc

    def activations(self, *, include_ended: bool = False) -> tuple[SkillActivationRecord, ...]:
        task = None if self.task_workspace is None else self.task_workspace.current_task
        return self.store.list_skill_activations(
            self.scope,
            task_id=None if task is None else task.task_id,
            include_scope_activations=True,
            include_ended=include_ended,
        )

    def active_documents(self) -> tuple[SkillDocument, ...]:
        task = None if self.task_workspace is None else self.task_workspace.current_task
        records: list[SkillActivationRecord] = []
        if task is not None:
            records.extend(
                self.store.list_skill_activations(
                    self.scope,
                    task_id=task.task_id,
                    include_scope_activations=False,
                )
            )
        with self._lock:
            owner_kind = self._owner_kind
            owner_id = self._owner_id
        if owner_kind in {"turn", "maintenance"} and owner_id is not None:
            records.extend(
                self.store.list_skill_activations(
                    self.scope,
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                )
            )
        unique: dict[tuple[str, str], SkillDocument] = {}
        for activation in records:
            document = self._resolve_activation(activation)
            unique[(document.name, document.version)] = document
        documents = tuple(sorted(unique.values(), key=lambda item: (item.name, item.version)))
        _validate_active_context(documents)
        return documents

    def catalog_snapshot(self) -> SkillCatalogSnapshot:
        documents = tuple(sorted(self._active_catalog_documents(), key=lambda item: item.name))
        descriptors = tuple(document.metadata() for document in documents)
        lines = ["AVAILABLE_SKILLS complete=true"]
        lines.extend(_descriptor_line(document) for document in documents)
        rendered_without_revision = "\n".join(lines)
        revision = "sha256:" + hashlib.sha256(
            rendered_without_revision.encode("utf-8")
        ).hexdigest()
        rendered = rendered_without_revision.replace(
            "AVAILABLE_SKILLS complete=true",
            f"AVAILABLE_SKILLS catalog_revision={revision} complete=true",
            1,
        )
        if len(rendered) > MAX_SKILL_DESCRIPTOR_CONTEXT_CHARS:
            raise SkillOperationError(
                "skill_catalog_context_budget_exceeded",
                f"Skill descriptor context uses {len(rendered)} characters; "
                f"limit is {MAX_SKILL_DESCRIPTOR_CONTEXT_CHARS}",
                can_retry=True,
            )
        return SkillCatalogSnapshot(revision, descriptors, rendered)

    def sync_context(self, context: AgentContext) -> None:
        snapshot = self.catalog_snapshot()
        context.observe_skills(
            catalog_revision=snapshot.revision,
            descriptors=list(snapshot.descriptors),
            available_rendered=snapshot.rendered,
            active=[
                {
                    **document.metadata(),
                    "instructions": document.body,
                }
                for document in self.active_documents()
            ],
        )

    def _activation_owner(self) -> tuple[str, str, str | None]:
        with self._lock:
            if self._owner_kind is not None and self._owner_id is not None:
                return self._owner_kind, self._owner_id, self._owner_task_id
        task = None if self.task_workspace is None else self.task_workspace.current_task
        if task is not None:
            return "task", task.task_id, task.task_id
        return "turn", "direct-turn", None

    def _active_catalog_documents(self) -> tuple[SkillDocument, ...]:
        documents = [self.catalog.load(name) for name in self.catalog.names]
        resolved: list[SkillDocument] = [item for item in documents if item is not None]
        for head in self.store.list_skill_heads(self.scope):
            version = self.store.get_skill_version(head.skill_id, version_digest=head.head_version)
            if version is None:
                raise SkillRecoveryError(
                    "skill_head_version_unavailable",
                    f"Learned Skill head has no exact version: {head.name} {head.head_version}",
                )
            resolved.append(self._learned_document(head, version))
        return tuple(resolved)

    def _learned_document(
        self,
        head: SkillHeadRecord,
        version: SkillVersionRecord,
    ) -> SkillDocument:
        source = canonical_skill_markdown(
            name=head.name,
            description=version.description,
            tools=version.tools,
            body=version.body,
        )
        try:
            parsed = parse_skill_markdown(source)
        except SkillFormatError as exc:
            raise SkillRecoveryError(
                "skill_version_corrupt",
                f"Stored learned Skill is corrupt: {head.name}: {exc}",
            ) from exc
        if parsed.version_digest != version.version_digest:
            raise SkillRecoveryError(
                "skill_version_digest_mismatch",
                f"Stored learned Skill digest mismatch: {head.name} {version.version_digest}",
            )
        missing = tuple(tool for tool in parsed.tools if tool not in self._registered_tools)
        return SkillDocument(
            name=parsed.name,
            description=parsed.description,
            version=parsed.version_digest,
            body=parsed.body,
            source=parsed.source,
            tools=parsed.tools,
            origin="learned",
            revision=version.revision,
            skill_id=head.skill_id,
            status=head.status,
            derived_from=head.derived_from,
            evidence_refs=version.evidence_refs,
            change_reason=version.change_reason,
            loadable=not missing,
            missing_tools=missing,
        )

    def _resolve_activation(self, activation: SkillActivationRecord) -> SkillDocument:
        builtin = self.catalog.load(activation.skill_name, activation.skill_version)
        if builtin is not None:
            return builtin
        version = self.store.get_skill_version(
            activation.skill_id,
            version_digest=activation.skill_version,
        )
        head = next(
            (
                item
                for item in self.store.list_skill_heads(self.scope, include_retired=True)
                if item.skill_id == activation.skill_id
            ),
            None,
        )
        if head is None or version is None:
            raise SkillRecoveryError(
                "skill_pinned_version_unavailable",
                f"Active Skill version cannot be resolved: {activation.skill_name} "
                f"{activation.skill_version}",
            )
        document = self._learned_document(head, version)
        if not document.loadable:
            raise SkillRecoveryError(
                "skill_dependency_unavailable",
                f"Active Skill dependencies are unavailable: {', '.join(document.missing_tools)}",
            )
        return document

    def _parse_fields(
        self,
        name: str,
        description: str,
        tools: Iterable[str],
        body: str,
    ) -> ParsedSkill:
        try:
            return build_skill_markdown(
                name=name,
                description=description,
                tools=tools,
                body=body,
                registered_tools=self._registered_tools,
            )
        except SkillFormatError as exc:
            raise SkillOperationError(exc.code, str(exc), can_retry=True) from exc

    def _validate_catalog_after(
        self,
        candidate: SkillDocument,
        *,
        replacing: str | None = None,
    ) -> None:
        documents = [
            document
            for document in self._active_catalog_documents()
            if document.name != replacing
        ]
        lines = ["AVAILABLE_SKILLS complete=true"]
        lines.extend(
            _descriptor_line(document)
            for document in sorted([*documents, candidate], key=lambda item: item.name)
        )
        length = len("\n".join(lines)) + 80
        if length > MAX_SKILL_DESCRIPTOR_CONTEXT_CHARS:
            raise SkillOperationError(
                "skill_catalog_context_budget_exceeded",
                f"Skill descriptor context would use {length} characters; "
                f"limit is {MAX_SKILL_DESCRIPTOR_CONTEXT_CHARS}",
                can_retry=True,
            )

    def _require_bound_registry(self) -> None:
        if not self._registered_tools:
            raise SkillOperationError(
                "skill_registry_unbound",
                "Skill workspace has not been bound to the shared tool registry",
            )


def register_skill_tools(registry: ToolRegistry, workspace: SkillWorkspace) -> None:
    registry.register(_list_skills_tool(workspace))
    registry.register(_read_skill_tool(workspace))
    registry.register(_load_skill_tool(workspace))
    registry.register(_create_skill_tool(workspace))
    registry.register(_update_skill_tool(workspace))
    registry.register(_delete_skill_tool(workspace))


def _list_skills_tool(workspace: SkillWorkspace) -> RegisteredTool:
    def list_skills(params: dict[str, object]) -> ToolResult:
        return ToolResult(
            True,
            "skill_catalog",
            False,
            metrics=workspace.list(
                str(params.get("query") or ""),
                start=int(params.get("start") or 0),
                limit=int(params.get("limit") or 20),
            ),
        )

    return RegisteredTool(
        "list_skills",
        "Search or paginate the complete Skill descriptor catalog already summarized in dynamic instructions.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 500},
                "start": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "additionalProperties": False,
        },
        list_skills,
        _skill_sidecar("list_skills", "read_skill_catalog", "skill_catalog"),
    )


def _read_skill_tool(workspace: SkillWorkspace) -> RegisteredTool:
    def read_skill(params: dict[str, object]) -> ToolResult:
        document = workspace.read(
            str(params.get("name") or ""),
            None if params.get("version") is None else str(params["version"]),
        )
        if document is None:
            return ToolResult(False, "skill_not_found", False, metrics={"name": params.get("name")})
        return ToolResult(
            True,
            "skill_read",
            False,
            metrics={
                **document.metadata(),
                "instructions": document.body,
                "skill_markdown": document.source,
                "evidence_refs": list(document.evidence_refs),
                "change_reason": document.change_reason,
                "complete": True,
            },
        )

    return RegisteredTool(
        "read_skill",
        "Read one exact Skill version without activating it. Read related Skills before revising methodology.",
        _skill_reference_schema(),
        read_skill,
        _skill_sidecar("read_skill", "read_skill", "skill_catalog"),
    )


def _load_skill_tool(workspace: SkillWorkspace) -> RegisteredTool:
    def load_skill(params: dict[str, object]) -> ToolResult:
        try:
            document, activation = workspace.load(
                str(params.get("name") or ""),
                None if params.get("version") is None else str(params["version"]),
            )
        except SkillOperationError as exc:
            return _skill_error_result(exc, name=params.get("name"))
        return ToolResult(
            True,
            "skill_loaded",
            False,
            metrics={
                **document.metadata(),
                "instructions": document.body,
                "activation": skill_activation_payload(activation),
                "complete": True,
            },
        )

    return RegisteredTool(
        "load_skill",
        "Activate one exact methodology version for the runtime-owned turn, task, or maintenance lifetime. It advises but never grants capability.",
        _skill_reference_schema(),
        load_skill,
        _skill_sidecar("load_skill", "load_skill", "skill_activation"),
    )


def _create_skill_tool(workspace: SkillWorkspace) -> RegisteredTool:
    def create_skill(params: dict[str, object]) -> ToolResult:
        try:
            document = workspace.create(
                name=str(params.get("name") or ""),
                description=str(params.get("description") or ""),
                tools=list(params.get("tools") or []),
                body=str(params.get("body") or ""),
                evidence_refs=list(params.get("evidence_refs") or []),
                derived_from=str(params.get("derived_from") or ""),
            )
        except SkillOperationError as exc:
            return _skill_error_result(exc, name=params.get("name"))
        return ToolResult(
            True,
            "skill_created",
            False,
            metrics={
                **document.metadata(),
                "skill_markdown": document.source,
                "evidence_refs": list(document.evidence_refs),
                "change_reason": document.change_reason,
                "complete": True,
            },
        )

    return RegisteredTool(
        "create_skill",
        "Create one evidence-backed learned Skill. Load skill-authoring first when authoring judgment or canonical structure is uncertain.",
        {
            "type": "object",
            "properties": {
                **_skill_definition_properties(),
                "evidence_refs": _evidence_refs_schema(),
                "derived_from": {"type": "string", "maxLength": 256},
            },
            "required": ["name", "description", "tools", "body", "evidence_refs"],
            "additionalProperties": False,
        },
        create_skill,
        _skill_sidecar("create_skill", "write_skill", "skill_control"),
    )


def _update_skill_tool(workspace: SkillWorkspace) -> RegisteredTool:
    def update_skill(params: dict[str, object]) -> ToolResult:
        try:
            document = workspace.update(
                name=str(params.get("name") or ""),
                expected_revision=int(params.get("expected_revision") or 0),
                description=None if params.get("description") is None else str(params["description"]),
                tools=None if params.get("tools") is None else list(params["tools"]),
                body=None if params.get("body") is None else str(params["body"]),
                evidence_refs=list(params.get("evidence_refs") or []),
                change_reason=str(params.get("change_reason") or ""),
            )
        except SkillOperationError as exc:
            return _skill_error_result(exc, name=params.get("name"))
        return ToolResult(
            True,
            "skill_updated",
            False,
            metrics={
                **document.metadata(),
                "skill_markdown": document.source,
                "evidence_refs": list(document.evidence_refs),
                "change_reason": document.change_reason,
                "complete": True,
            },
        )

    properties = _skill_definition_properties()
    properties.pop("name")
    return RegisteredTool(
        "update_skill",
        "Append an evidence-backed immutable version to one learned Skill using optimistic revision control.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "expected_revision": {"type": "integer", "minimum": 1},
                **properties,
                "evidence_refs": _evidence_refs_schema(),
                "change_reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            },
            "required": ["name", "expected_revision", "evidence_refs", "change_reason"],
            "additionalProperties": False,
        },
        update_skill,
        _skill_sidecar("update_skill", "write_skill", "skill_control"),
    )


def _delete_skill_tool(workspace: SkillWorkspace) -> RegisteredTool:
    def delete_skill(params: dict[str, object]) -> ToolResult:
        try:
            head = workspace.delete(
                name=str(params.get("name") or ""),
                expected_revision=int(params.get("expected_revision") or 0),
                evidence_refs=list(params.get("evidence_refs") or []),
                reason=str(params.get("reason") or ""),
            )
        except SkillOperationError as exc:
            return _skill_error_result(exc, name=params.get("name"))
        return ToolResult(
            True,
            "skill_retired",
            False,
            metrics={
                "name": head.name,
                "revision": head.head_revision,
                "version": head.head_version,
                "status": head.status,
                "retired_at": head.retired_at,
                "evidence_refs": list(head.retirement_evidence_refs),
                "reason": head.retirement_reason,
                "complete": True,
            },
        )

    return RegisteredTool(
        "delete_skill",
        "Retire one learned Skill head using optimistic revision control while preserving immutable historical versions.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "expected_revision": {"type": "integer", "minimum": 1},
                "evidence_refs": _evidence_refs_schema(),
                "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            },
            "required": ["name", "expected_revision", "evidence_refs", "reason"],
            "additionalProperties": False,
        },
        delete_skill,
        _skill_sidecar("delete_skill", "delete_skill", "skill_control"),
    )


def _builtin_document(parsed: ParsedSkill) -> SkillDocument:
    return SkillDocument(
        name=parsed.name,
        description=parsed.description,
        version=parsed.version_digest,
        body=parsed.body,
        source=parsed.source,
        tools=parsed.tools,
        origin="builtin",
        revision=1,
        skill_id=f"builtin:{parsed.name}",
    )


def _prospective_learned_document(
    parsed: ParsedSkill,
    *,
    revision: int,
    derived_from: str,
) -> SkillDocument:
    return SkillDocument(
        name=parsed.name,
        description=parsed.description,
        version=parsed.version_digest,
        body=parsed.body,
        source=parsed.source,
        tools=parsed.tools,
        origin="learned",
        revision=revision,
        skill_id="prospective",
        derived_from=str(derived_from or ""),
    )


def _rank_documents(documents: Iterable[SkillDocument], query: str) -> list[SkillDocument]:
    clean_query = " ".join(str(query or "").casefold().split())[:500]
    terms = tuple(dict.fromkeys(re.findall(r"[\w]+", clean_query, flags=re.UNICODE)))
    ranked: list[tuple[int, SkillDocument]] = []
    for document in documents:
        haystack = f"{document.name} {document.description}".casefold()
        if terms and not any(term in haystack for term in terms):
            continue
        score = sum(2 if term in document.name else 1 for term in terms)
        ranked.append((score, document))
    ranked.sort(key=lambda item: (-item[0], item[1].name))
    return [document for _, document in ranked]


def _descriptor_line(document: SkillDocument) -> str:
    line = (
        f"- {document.name} [{document.origin} {document.version}] "
        f"loadable={'true' if document.loadable else 'false'}: {document.description}"
    )
    if document.missing_tools:
        line += " missing_tools=" + json.dumps(list(document.missing_tools), ensure_ascii=False)
    return line


def _validate_active_context(documents: Iterable[SkillDocument]) -> None:
    items = tuple(documents)
    if len(items) > MAX_ACTIVE_SKILLS:
        raise SkillOperationError(
            "skill_active_limit_exceeded",
            f"At most {MAX_ACTIVE_SKILLS} Skills may be active at once",
            can_retry=True,
        )
    rendered = _render_active_documents(items)
    if len(rendered) > MAX_ACTIVE_SKILL_CONTEXT_CHARS:
        raise SkillOperationError(
            "skill_active_context_budget_exceeded",
            f"Active Skill context uses {len(rendered)} characters; "
            f"limit is {MAX_ACTIVE_SKILL_CONTEXT_CHARS}",
            can_retry=True,
        )


def _render_active_documents(documents: Iterable[SkillDocument]) -> str:
    items = tuple(documents)
    if not items:
        return ""
    sections = ["ACTIVE_SKILLS complete=true"]
    for document in items:
        sections.append(f"BEGIN_SKILL name={document.name} version={document.version}")
        sections.append(document.body)
        sections.append(f"END_SKILL name={document.name}")
    return "\n".join(sections)


def _validate_evidence_refs(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise SkillOperationError("skill_evidence_invalid", "evidence_refs must be a list", can_retry=True)
    clean = tuple(str(value or "").strip() for value in values)
    if not clean:
        raise SkillOperationError(
            "skill_evidence_required",
            "At least one concrete evidence reference is required",
            can_retry=True,
        )
    if len(clean) > 32 or any(not item or len(item) > 1_000 for item in clean):
        raise SkillOperationError("skill_evidence_invalid", "evidence_refs exceed the bounded schema", can_retry=True)
    if len(set(clean)) != len(clean):
        raise SkillOperationError("skill_evidence_invalid", "evidence_refs must be unique", can_retry=True)
    if any(_SECRET_PATTERN.search(item) for item in clean):
        raise SkillOperationError("skill_evidence_invalid", "evidence_refs cannot contain secrets", can_retry=True)
    return clean


def _validate_change_reason(value: str) -> str:
    clean = " ".join(str(value or "").split())
    if not clean or len(clean) > 1_000:
        raise SkillOperationError("skill_change_reason_invalid", "A bounded change reason is required", can_retry=True)
    if _SECRET_PATTERN.search(clean):
        raise SkillOperationError("skill_change_reason_invalid", "Change reason cannot contain secrets", can_retry=True)
    return clean


def _skill_error_result(exc: SkillOperationError, **metrics: object) -> ToolResult:
    return ToolResult(
        False,
        exc.code,
        exc.can_retry,
        metrics={**metrics, "error": str(exc)},
    )


def _skill_reference_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 64},
            "version": {"type": "string", "maxLength": 128},
        },
        "required": ["name"],
        "additionalProperties": False,
    }


def _skill_definition_properties() -> dict[str, object]:
    return {
        "name": {"type": "string", "minLength": 1, "maxLength": 64},
        "description": {"type": "string", "minLength": 24, "maxLength": 320},
        "tools": {
            "type": "array",
            "maxItems": 64,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "body": {"type": "string", "minLength": 1, "maxLength": 8_000},
    }


def _evidence_refs_schema() -> dict[str, object]:
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": 32,
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1, "maxLength": 1_000},
    }


def _skill_sidecar(progress_key: str, permission: str, tool_type: str) -> ToolSidecar:
    return ToolSidecar(
        progress_key,
        mutating=False,
        source="agent.skill",
        tool_type=tool_type,
        permission=permission,
        body_scope=(),
        terminal_truth=("SkillVersion.version_digest", "SkillActivationRecord"),
    )


__all__ = [
    "MAX_ACTIVE_SKILLS",
    "MAX_ACTIVE_SKILL_CONTEXT_CHARS",
    "MAX_SKILL_DESCRIPTOR_CONTEXT_CHARS",
    "SkillCatalog",
    "SkillCatalogError",
    "SkillCatalogSnapshot",
    "SkillDocument",
    "SkillOperationError",
    "SkillRecoveryError",
    "SkillWorkspace",
    "register_skill_tools",
]
