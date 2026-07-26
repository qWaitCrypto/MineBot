"""F5 Social — principals, trust tiers, and the work-admission matrix.

brain-cognitive-framework.md §8. This is the data realization of the
Relationship axis that `operating-modes.md` specified but never built: who is
asking, how much they are trusted, and therefore what work they may create.

Layered injection defense, first net: an untrusted principal cannot create or
mutate durable work. A stranger's "tear that house down" is conversation, not
a goal — it still reaches the model as dialogue, so the bot can answer, refuse,
or discuss it. Exact mutation legality remains enforced in Body code (the last
net, unchanged).

Behavior preservation: the default mode is ``open`` — every principal is
treated as owner-equivalent, which is exactly today's behavior. Configuring
owner names switches the registry to ``enforcing``. Disabling the config
returns the prior behavior exactly (framework §3.4 rollback rule).

This module is an independent domain store by construction: it does not touch
``runtime_state.py``. Durable trust promotion lands with activation; the
``PrincipalStore`` protocol is the seam for it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterable, Protocol


class TrustTier(str, Enum):
    """Trust ordering; higher tiers strictly contain lower-tier capability."""

    OWNER = "owner"
    FRIEND = "friend"
    STRANGER = "stranger"


class PrincipalKind(str, Enum):
    PLAYER = "player"
    OPERATOR = "operator"   # console / launcher / harness — owner-equivalent
    SYSTEM = "system"       # runtime-internal intents


class AdmissionCapability(str, Enum):
    """What a principal is trying to do, independent of command spelling."""

    CONVERSE = "converse"
    START_WORK = "start_work"
    CONTROL_WORK = "control_work"      # pause / continue / cancel / replace
    CONTROL_PROCESS = "control_process"  # quit, and later: grants, trust edits


# The operator/system principal id used when a command carries no sender
# (console `/goal`, launcher, restart reconciliation, internal intents).
OPERATOR_PRINCIPAL_ID = "@operator"


@dataclass(frozen=True)
class PrincipalRecord:
    principal_id: str
    kind: PrincipalKind
    trust: TrustTier
    granted: tuple[str, ...] = ()
    first_seen: str = ""
    last_seen: str = ""
    notes: str = ""


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    capability: AdmissionCapability
    principal: PrincipalRecord
    reason: str

    @property
    def denied(self) -> bool:
        return not self.allowed


class PrincipalStore(Protocol):
    """Durable trust seam. Activation supplies a persistent implementation."""

    def get(self, principal_id: str) -> PrincipalRecord | None: ...

    def put(self, record: PrincipalRecord) -> None: ...

    def all(self) -> tuple[PrincipalRecord, ...]: ...


class InMemoryPrincipalStore:
    def __init__(self, records: Iterable[PrincipalRecord] = ()) -> None:
        self._records: dict[str, PrincipalRecord] = {
            record.principal_id: record for record in records
        }

    def get(self, principal_id: str) -> PrincipalRecord | None:
        return self._records.get(principal_id)

    def put(self, record: PrincipalRecord) -> None:
        self._records[record.principal_id] = record

    def all(self) -> tuple[PrincipalRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))


class SqlitePrincipalStore:
    """Durable ``PrincipalStore`` so trust promotion survives restarts.

    The one pre-buildable item on F5's activation list (framework §8.1).
    Independent domain persistence by construction: its own file, connection,
    and lock — never a table appended to the ``runtime_state.py`` monolith.
    Pure persistence only: no timestamps, no trust logic; the registry stays
    the single place that decides anything.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = str(path)
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS principals (
                    principal_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    trust TEXT NOT NULL,
                    granted TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    notes TEXT NOT NULL
                )
                """
            )

    def get(self, principal_id: str) -> PrincipalRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT principal_id, kind, trust, granted, first_seen,"
                " last_seen, notes FROM principals WHERE principal_id = ?",
                (str(principal_id),),
            ).fetchone()
        return None if row is None else _principal_from_row(row)

    def put(self, record: PrincipalRecord) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO principals (principal_id, kind, trust, granted,"
                " first_seen, last_seen, notes) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(principal_id) DO UPDATE SET kind = excluded.kind,"
                " trust = excluded.trust, granted = excluded.granted,"
                " first_seen = excluded.first_seen, last_seen = excluded.last_seen,"
                " notes = excluded.notes",
                (
                    record.principal_id,
                    record.kind.value,
                    record.trust.value,
                    json.dumps(list(record.granted), ensure_ascii=False),
                    record.first_seen,
                    record.last_seen,
                    record.notes,
                ),
            )

    def all(self) -> tuple[PrincipalRecord, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT principal_id, kind, trust, granted, first_seen,"
                " last_seen, notes FROM principals ORDER BY principal_id"
            ).fetchall()
        return tuple(_principal_from_row(row) for row in rows)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _principal_from_row(row: tuple[str, str, str, str, str, str, str]) -> PrincipalRecord:
    principal_id, kind, trust, granted, first_seen, last_seen, notes = row
    return PrincipalRecord(
        principal_id=str(principal_id),
        kind=PrincipalKind(str(kind)),
        trust=TrustTier(str(trust)),
        granted=tuple(str(item) for item in json.loads(granted or "[]")),
        first_seen=str(first_seen),
        last_seen=str(last_seen),
        notes=str(notes),
    )


# Capability matrix per trust tier. CONTROL_WORK for FRIEND is conditional on
# owning the current goal, expressed by the sentinel below rather than a second
# table, so there is exactly one place that defines admission.
_OWN_WORK_ONLY = "own_work_only"

_MATRIX: dict[TrustTier, dict[AdmissionCapability, object]] = {
    TrustTier.OWNER: {
        AdmissionCapability.CONVERSE: True,
        AdmissionCapability.START_WORK: True,
        AdmissionCapability.CONTROL_WORK: True,
        AdmissionCapability.CONTROL_PROCESS: True,
    },
    TrustTier.FRIEND: {
        AdmissionCapability.CONVERSE: True,
        AdmissionCapability.START_WORK: True,
        AdmissionCapability.CONTROL_WORK: _OWN_WORK_ONLY,
        AdmissionCapability.CONTROL_PROCESS: False,
    },
    TrustTier.STRANGER: {
        AdmissionCapability.CONVERSE: True,
        AdmissionCapability.START_WORK: False,
        AdmissionCapability.CONTROL_WORK: False,
        AdmissionCapability.CONTROL_PROCESS: False,
    },
}


@dataclass
class PrincipalRegistry:
    """Resolves senders to principals and answers admission questions.

    ``enforcing`` is derived, not configured twice: a registry with no owner
    names cannot meaningfully enforce a hierarchy, so it stays ``open`` and
    reproduces today's behavior byte-for-byte.
    """

    owners: frozenset[str] = frozenset()
    friends: frozenset[str] = frozenset()
    store: PrincipalStore = field(default_factory=InMemoryPrincipalStore)
    default_trust: TrustTier = TrustTier.STRANGER

    @classmethod
    def open_registry(cls) -> "PrincipalRegistry":
        """Today's behavior: no owners configured, everyone may command."""
        return cls()

    @property
    def enforcing(self) -> bool:
        return bool(self.owners)

    def resolve(self, sender: str | None) -> PrincipalRecord:
        """Map a raw ingress sender to a principal record.

        An empty sender is the console/launcher/internal path and resolves to
        the operator principal, which is owner-equivalent. Without this,
        enabling enforcement would deny the harness its own commands.
        """
        name = str(sender or "").strip()
        if not name:
            return PrincipalRecord(
                principal_id=OPERATOR_PRINCIPAL_ID,
                kind=PrincipalKind.OPERATOR,
                trust=TrustTier.OWNER,
            )
        existing = self.store.get(name)
        if existing is not None:
            return existing
        record = PrincipalRecord(
            principal_id=name,
            kind=PrincipalKind.PLAYER,
            trust=self._configured_trust(name),
        )
        self.store.put(record)
        return record

    def promote(self, principal_id: str, trust: TrustTier) -> PrincipalRecord:
        """Explicit owner act; durable persistence arrives with activation."""
        record = self.resolve(principal_id)
        updated = replace(record, trust=trust)
        self.store.put(updated)
        return updated

    def evaluate(
        self,
        capability: AdmissionCapability,
        sender: str | None,
        *,
        work_owner_id: str | None = None,
    ) -> AdmissionDecision:
        principal = self.resolve(sender)
        if not self.enforcing:
            return AdmissionDecision(True, capability, principal, "admission_open")
        verdict = _MATRIX[principal.trust][capability]
        if verdict is True:
            return AdmissionDecision(True, capability, principal, "trusted")
        if verdict is False:
            return AdmissionDecision(
                False, capability, principal, f"{principal.trust.value}_not_permitted"
            )
        # _OWN_WORK_ONLY: a friend may control only work they started.
        if work_owner_id is not None and work_owner_id == principal.principal_id:
            return AdmissionDecision(True, capability, principal, "own_work")
        return AdmissionDecision(False, capability, principal, "not_work_owner")

    def _configured_trust(self, name: str) -> TrustTier:
        if name in self.owners:
            return TrustTier.OWNER
        if name in self.friends:
            return TrustTier.FRIEND
        return self.default_trust


OWNERS_ENV = "MINEBOT_PRINCIPAL_OWNERS"
FRIENDS_ENV = "MINEBOT_PRINCIPAL_FRIENDS"
PRINCIPAL_DB_ENV = "MINEBOT_PRINCIPAL_DB"


def principal_registry_from_env(
    env: Mapping[str, str] | None = None,
) -> PrincipalRegistry:
    """Build the registry from config; unset owners keep today's behavior.

    ``MINEBOT_PRINCIPAL_OWNERS`` / ``MINEBOT_PRINCIPAL_FRIENDS`` are
    comma-separated Minecraft names. With no owners configured the registry is
    ``open`` and admission is a no-op, which is the documented rollback.
    ``MINEBOT_PRINCIPAL_DB`` opts into durable trust (a SQLite path); unset
    keeps the in-memory store, so persistence is additive config too.
    """

    env = os.environ if env is None else env
    db_path = str(env.get(PRINCIPAL_DB_ENV) or "").strip()
    store: PrincipalStore = (
        SqlitePrincipalStore(db_path) if db_path else InMemoryPrincipalStore()
    )
    return PrincipalRegistry(
        owners=_name_set(env.get(OWNERS_ENV)),
        friends=_name_set(env.get(FRIENDS_ENV)),
        store=store,
    )


def _name_set(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


__all__ = [
    "AdmissionCapability",
    "AdmissionDecision",
    "FRIENDS_ENV",
    "InMemoryPrincipalStore",
    "OPERATOR_PRINCIPAL_ID",
    "OWNERS_ENV",
    "PRINCIPAL_DB_ENV",
    "PrincipalKind",
    "PrincipalRecord",
    "PrincipalRegistry",
    "PrincipalStore",
    "SqlitePrincipalStore",
    "TrustTier",
    "principal_registry_from_env",
]
