"""Durable control-plane record types and their scope identity.

Extracted verbatim from ``app/runtime_state.py`` (framework §12 H2): the
vocabulary MineBot persists, with no storage behaviour attached. Keeping the
shapes free of the SQLite owner is what lets both the store and the pure
mapping layer depend on them without a cycle.

``runtime_state`` re-exports every name here, so no consumer import changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

_MAX_SCOPE_COMPONENT_LENGTH = 256


class RuntimeStateError(RuntimeError):
    """Persistent runtime state is invalid or incompatible."""


class RuntimeStateConflict(RuntimeStateError):
    """A revision or single-foreground-task invariant was violated."""


class MemoryStateConflict(RuntimeStateConflict):
    """A memory revision, subject, or source-precedence invariant was violated."""


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_EVENT = "waiting_event"
    PAUSED = "paused"
    YIELDED = "yielded"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class CompletionAuthority(str, Enum):
    NONE = "none"
    BODY_TRUTH = "body_truth"
    MODEL = "model"
    HUMAN = "human"


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class CheckpointDisposition(str, Enum):
    CONTINUE = "continue"
    WAIT_EVENT = "wait_event"
    YIELD = "yield"
    COMPLETE = "complete"


class ContinuationOperationClass(str, Enum):
    EPISTEMIC = "epistemic"
    MATERIAL = "material"
    MIXED = "mixed"


class MemoryKind(str, Enum):
    SPATIAL = "spatial"
    EPISODIC = "episodic"
    REFLECTIVE = "reflective"


class MemorySource(str, Enum):
    OBSERVED = "observed"
    PLAYER_TOLD = "player_told"
    SELF_INFERRED = "self_inferred"


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    scope_key: str
    revision: int
    goal_text: str
    source: str
    requested_by: str
    status: TaskStatus
    completion_authority: CompletionAuthority
    active_plan_id: str | None
    latest_checkpoint_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PlanStepRecord:
    step_id: str
    ordinal: int
    title: str
    status: PlanStepStatus
    evidence: tuple[str, ...]
    blocker: str | None
    updated_at: str


@dataclass(frozen=True)
class TaskPlanRecord:
    plan_id: str
    task_id: str
    revision: int
    summary: str
    steps: tuple[PlanStepRecord, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskCheckpointRecord:
    checkpoint_id: str
    task_id: str
    revision: int
    disposition: CheckpointDisposition
    summary: str
    next_step: str
    evidence: tuple[str, ...]
    wait_for: tuple[str, ...]
    body_fingerprint: dict[str, object] | None
    continuation: "ContinuationContract | None"
    created_at: str


@dataclass(frozen=True)
class ContinuationContract:
    objective: str
    operation_class: ContinuationOperationClass
    target_descriptor: dict[str, object]
    expected_evidence: tuple[str, ...]
    bounded_epoch_budget: int
    approach_key: str
    evidence_cursor: int
    generation: int


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    scope_key: str
    revision: int
    kind: MemoryKind
    source: MemorySource
    subject_key: str
    title: str
    content: str
    evidence_ref: str
    dimension: str | None
    point: tuple[float, float, float] | None
    region: tuple[float, float, float, float, float, float] | None
    created_at: str
    updated_at: str
    # F3 §6.2: plural resolvable evidence handles and the supersession chain.
    # Additive with defaults so every existing construction site stays valid.
    evidence_handles: tuple[str, ...] = ()
    superseded_by: str | None = None


@dataclass(frozen=True)
class SkillActivationRecord:
    activation_id: str
    scope_key: str
    task_id: str | None
    owner_kind: str
    owner_id: str
    skill_id: str
    skill_name: str
    skill_version: str
    activated_at: str
    ended_at: str | None


@dataclass(frozen=True)
class SkillHeadRecord:
    skill_id: str
    server_id: str
    bot_id: str
    name: str
    head_revision: int
    head_version: str
    status: str
    origin: str
    derived_from: str
    retired_at: str | None
    retirement_evidence_refs: tuple[str, ...]
    retirement_reason: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SkillVersionRecord:
    skill_id: str
    revision: int
    version_digest: str
    description: str
    tools: tuple[str, ...]
    body: str
    evidence_refs: tuple[str, ...]
    change_reason: str
    created_at: str


@dataclass(frozen=True)
class WikiCacheRecord:
    cache_key: str
    endpoint: str
    kind: str
    request_key: str
    payload: dict[str, object]
    etag: str | None
    last_modified: str | None
    fetched_at: str
    expires_at: str


@dataclass(frozen=True)
class RuntimeScope:
    """Stable identity boundary for all durable state owned by one bot."""

    server_id: str
    world_id: str
    bot_id: str

    def __post_init__(self) -> None:
        for field_name in ("server_id", "world_id", "bot_id"):
            value = _validated_scope_component(field_name, getattr(self, field_name))
            object.__setattr__(self, field_name, value)

    @property
    def key(self) -> str:
        encoded = json.dumps(
            self.to_payload(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def conversation_session_id(self) -> str:
        return f"minebot:{self.key}:conversation"

    def to_payload(self) -> dict[str, str]:
        return {
            "server_id": self.server_id,
            "world_id": self.world_id,
            "bot_id": self.bot_id,
        }


# Source-as-trust ordering (memory-and-knowledge.md): a lower-trust source may
# not overwrite a higher-trust fact with the same identity. Lives beside
# MemorySource so the ranking cannot drift from the enum it ranks.
_MEMORY_SOURCE_RANK: dict["MemorySource", int] = {
    MemorySource.OBSERVED: 4,
    MemorySource.PLAYER_TOLD: 3,
    MemorySource.SELF_INFERRED: 2,
}


def _validated_scope_component(field_name: str, value: object) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{field_name} must not be empty")
    if len(clean) > _MAX_SCOPE_COMPONENT_LENGTH:
        raise ValueError(f"{field_name} exceeds {_MAX_SCOPE_COMPONENT_LENGTH} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in clean):
        raise ValueError(f"{field_name} contains control characters")
    return clean
