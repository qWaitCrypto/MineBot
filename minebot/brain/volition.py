"""F4 Volition — standby stances and long-horizon campaign artifacts.

brain-cognitive-framework.md §7. Two things live here, and both are
**artifacts the model and the operator read and edit — never engines**:

1. **Stances** (`guard` / `follow` / `standby`): a durable, owner-granted
   record that lets the existing event pump treat additional Body events as
   material wakes, rate-limited by explicit constraints. C2 stands: idle time
   alone still never wakes the model; an owner-granted stance is the explicit
   human act that authorizes ambient wakes, and revoking it restores exact
   prior behavior.

2. **Campaigns**: the data home for week-scale goals (the Ender Dragon is the
   first content). A campaign has no timer, no watcher, and no
   "next objective" selector in framework code. `unblocked_objectives` is a
   read-only projection for context; nothing in the spine consumes it
   automatically, and a guard test pins that.

Framework-agnostic: imports only stdlib.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum


# -- stances ---------------------------------------------------------------


class StanceKind(str, Enum):
    NONE = "none"
    GUARD = "guard"
    FOLLOW = "follow"
    STANDBY = "standby"


# Body events a stance additionally treats as material. With StanceKind.NONE
# the set is empty, so the pump's wake behavior is byte-identical to today.
_STANCE_WAKE_EVENTS: Mapping[StanceKind, frozenset[str]] = {
    StanceKind.NONE: frozenset(),
    StanceKind.GUARD: frozenset({"hostileNearby", "underAttack", "playerDamaged"}),
    StanceKind.FOLLOW: frozenset({"playerMoved", "playerLeft", "hostileNearby"}),
    StanceKind.STANDBY: frozenset({"hostileNearby"}),
}


@dataclass(frozen=True)
class StanceConstraints:
    """Bounds an owner places on ambient wakes.

    ``max_wakes_per_minute`` is the anti-token-leak rule from
    `operating-modes.md` §Standby: near-zero standby must stay near-zero.
    ``radius`` is advisory for the Body/consumer and is carried, not enforced,
    here.
    """

    max_wakes_per_minute: int = 4
    radius: int | None = None


@dataclass(frozen=True)
class StancePolicy:
    stance: StanceKind = StanceKind.NONE
    granted_by: str = ""            # principal id; owner tier only (F5)
    constraints: StanceConstraints = field(default_factory=StanceConstraints)
    note: str = ""

    @classmethod
    def none(cls) -> "StancePolicy":
        """Today's behavior: no ambient wakes whatsoever."""
        return cls()

    @property
    def active(self) -> bool:
        return self.stance is not StanceKind.NONE and bool(self.granted_by)

    def wake_events(self) -> frozenset[str]:
        """Extra material event names; empty unless a grant is in force."""
        if not self.active:
            return frozenset()
        return _STANCE_WAKE_EVENTS[self.stance]


@dataclass(frozen=True)
class StanceWakeDecision:
    allow: bool
    reason: str


class StanceWakeGate:
    """Rate-limits stance-originated wakes; pure w.r.t. an injected clock.

    Only stance-originated wakes pass through here. Events that were already
    material (death, bodyMissing, respawned, underAttack, mobilityBlocked, and
    awaited task terminals) are never gated — throttling those would suppress
    survival and terminal truth.
    """

    def __init__(self, policy: StancePolicy | None = None) -> None:
        self.policy = policy or StancePolicy.none()
        self._woke_at: deque[float] = deque()

    def decide(self, event_name: str, *, now: float) -> StanceWakeDecision:
        if not self.policy.active:
            return StanceWakeDecision(False, "no_stance")
        if event_name not in self.policy.wake_events():
            return StanceWakeDecision(False, "not_stance_relevant")
        self._evict(now)
        if len(self._woke_at) >= self.policy.constraints.max_wakes_per_minute:
            return StanceWakeDecision(False, "stance_rate_limited")
        return StanceWakeDecision(True, "stance_wake")

    def record(self, *, now: float) -> None:
        self._woke_at.append(now)
        self._evict(now)

    def _evict(self, now: float) -> None:
        while self._woke_at and now - self._woke_at[0] >= 60.0:
            self._woke_at.popleft()


# -- campaigns -------------------------------------------------------------


class ObjectiveStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    BLOCKED = "blocked"
    DONE = "done"
    DROPPED = "dropped"


class CampaignStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class GoalLock(str, Enum):
    """From `operating-modes.md`: how strongly the mission resists change."""

    MUTABLE = "mutable"   # casual redirection is allowed
    LOCKED = "locked"     # only a formal command may replace the mission


# Statuses whose transition is a truth claim and therefore needs evidence (C5).
EVIDENCE_REQUIRED_STATUSES = frozenset({ObjectiveStatus.DONE, ObjectiveStatus.BLOCKED})

MAX_OBJECTIVES = 128
MAX_TITLE_CHARS = 200
MAX_NOTES_CHARS = 2000


class CampaignError(ValueError):
    """A campaign edit would violate the artifact's invariants."""


@dataclass(frozen=True)
class ObjectiveNode:
    node_id: str
    title: str
    status: ObjectiveStatus = ObjectiveStatus.PENDING
    depends_on: tuple[str, ...] = ()
    evidence_handles: tuple[str, ...] = ()
    assignee_bot: str | None = None      # F8 delegation hook
    notes: str = ""


@dataclass(frozen=True)
class MilestoneRecord:
    milestone_id: str
    title: str
    objective_ids: tuple[str, ...] = ()
    reached_at: str = ""


@dataclass(frozen=True)
class CampaignRecord:
    campaign_id: str
    title: str
    mission: str
    principal_id: str = ""
    goal_lock: GoalLock = GoalLock.MUTABLE
    status: CampaignStatus = CampaignStatus.ACTIVE
    objectives: tuple[ObjectiveNode, ...] = ()
    milestones: tuple[MilestoneRecord, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    def objective(self, node_id: str) -> ObjectiveNode | None:
        return next((node for node in self.objectives if node.node_id == node_id), None)

    def unblocked_objectives(self) -> tuple[ObjectiveNode, ...]:
        """Read-only projection: objectives whose dependencies are all done.

        This is context for the model, NOT a scheduler. Nothing in the spine
        consumes it; a guard test pins that so a convenience call cannot turn
        the artifact into an engine (C1/C4).
        """

        done = {
            node.node_id
            for node in self.objectives
            if node.status is ObjectiveStatus.DONE
        }
        return tuple(
            node
            for node in self.objectives
            if node.status in {ObjectiveStatus.PENDING, ObjectiveStatus.ACTIVE}
            and all(dependency in done for dependency in node.depends_on)
        )

    def with_objective_status(
        self,
        node_id: str,
        status: ObjectiveStatus,
        *,
        evidence_handles: Iterable[str] = (),
    ) -> "CampaignRecord":
        node = self.objective(node_id)
        if node is None:
            raise CampaignError(f"unknown objective: {node_id}")
        handles = tuple(str(item) for item in evidence_handles if str(item).strip())
        if status in EVIDENCE_REQUIRED_STATUSES and not (handles or node.evidence_handles):
            raise CampaignError(
                f"objective {node_id} -> {status.value} requires evidence handles"
            )
        updated = replace(
            node,
            status=status,
            evidence_handles=node.evidence_handles + handles,
        )
        return replace(
            self,
            objectives=tuple(
                updated if item.node_id == node_id else item for item in self.objectives
            ),
        )

    def with_mission(self, mission: str, *, formal_command: bool = False) -> "CampaignRecord":
        """Replace the mission, honoring the goal lock.

        A locked campaign exists so hours of progress cannot be nulled by a
        casual redirect; only an explicit formal command may replace it.
        """

        if self.goal_lock is GoalLock.LOCKED and not formal_command:
            raise CampaignError("locked campaign mission requires a formal command")
        return replace(self, mission=mission)


def validate_campaign(record: CampaignRecord) -> CampaignRecord:
    """Structural validation: bounded size, unique ids, resolvable acyclic DAG."""

    if not record.campaign_id.strip():
        raise CampaignError("campaign_id is required")
    if len(record.objectives) > MAX_OBJECTIVES:
        raise CampaignError(f"campaign exceeds {MAX_OBJECTIVES} objectives")
    seen: set[str] = set()
    for node in record.objectives:
        if not node.node_id.strip():
            raise CampaignError("objective node_id is required")
        if node.node_id in seen:
            raise CampaignError(f"duplicate objective id: {node.node_id}")
        seen.add(node.node_id)
        if len(node.title) > MAX_TITLE_CHARS:
            raise CampaignError(f"objective {node.node_id} title exceeds bounds")
        if len(node.notes) > MAX_NOTES_CHARS:
            raise CampaignError(f"objective {node.node_id} notes exceed bounds")
    for node in record.objectives:
        for dependency in node.depends_on:
            if dependency not in seen:
                raise CampaignError(
                    f"objective {node.node_id} depends on unknown {dependency}"
                )
            if dependency == node.node_id:
                raise CampaignError(f"objective {node.node_id} depends on itself")
    _require_acyclic(record.objectives)
    return record


def _require_acyclic(objectives: tuple[ObjectiveNode, ...]) -> None:
    dependencies = {node.node_id: set(node.depends_on) for node in objectives}
    resolved: set[str] = set()
    progressing = True
    while progressing:
        progressing = False
        for node_id, deps in dependencies.items():
            if node_id in resolved:
                continue
            if deps <= resolved:
                resolved.add(node_id)
                progressing = True
    unresolved = sorted(set(dependencies) - resolved)
    if unresolved:
        raise CampaignError(f"objective dependency cycle: {', '.join(unresolved)}")


__all__ = [
    "EVIDENCE_REQUIRED_STATUSES",
    "MAX_OBJECTIVES",
    "CampaignError",
    "CampaignRecord",
    "CampaignStatus",
    "GoalLock",
    "MilestoneRecord",
    "ObjectiveNode",
    "ObjectiveStatus",
    "StanceConstraints",
    "StanceKind",
    "StancePolicy",
    "StanceWakeDecision",
    "StanceWakeGate",
]
