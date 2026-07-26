"""Row/record mapping, payload projection, and validation for runtime state.

Extracted verbatim from ``app/runtime_state.py`` (framework §12 H2). This is
the pure half of the control-plane store: nothing here touches a connection,
a lock, or a transaction, so the shapes MineBot persists can be read and
tested without the SQLite owner. ``RuntimeStateStore`` imports these back and
re-exports the public payload builders, so no consumer changes.

The remaining per-domain split of the store class itself stays touch-to-split;
this seam is what makes it approachable.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import UTC, datetime, timedelta

from minebot.app.runtime_records import (
    RuntimeStateError,
    _MEMORY_SOURCE_RANK,
    CheckpointDisposition,
    CompletionAuthority,
    ContinuationContract,
    ContinuationOperationClass,
    MemoryKind,
    MemoryRecord,
    MemorySource,
    PlanStepRecord,
    PlanStepStatus,
    SkillActivationRecord,
    SkillHeadRecord,
    SkillVersionRecord,
    TaskCheckpointRecord,
    TaskPlanRecord,
    TaskRecord,
    TaskStatus,
    WikiCacheRecord,
)


def _validated_memory_values(
    *,
    kind: MemoryKind,
    source: MemorySource,
    title: str,
    content: str,
    subject_key: str,
    evidence_ref: str,
    dimension: str | None,
    point: tuple[float, float, float] | None,
    region: tuple[float, float, float, float, float, float] | None,
) -> dict[str, object]:
    clean_kind = MemoryKind(kind)
    clean_source = MemorySource(source)
    clean_title = _required_text("title", title, max_length=500)
    clean_content = _required_text("content", content, max_length=12000)
    clean_subject = _bounded_text(subject_key, max_length=256).strip()
    clean_evidence = _bounded_text(evidence_ref, max_length=512).strip()
    if clean_source is MemorySource.OBSERVED and not clean_evidence:
        raise ValueError("observed memory requires an authoritative evidence_ref")
    clean_dimension = None
    if dimension is not None and str(dimension).strip():
        clean_dimension = _bounded_text(dimension, max_length=128).strip()
    clean_point = _validated_point(point, field_name="point")
    clean_region = _validated_region(region, field_name="region")
    if clean_point is not None and clean_region is not None:
        raise ValueError("memory geometry must be a point or region, not both")
    if (clean_point is not None or clean_region is not None) and clean_dimension is None:
        raise ValueError("memory geometry requires dimension")
    if clean_kind is MemoryKind.SPATIAL and clean_point is None and clean_region is None:
        raise ValueError("spatial memory requires point or region geometry")
    return {
        "kind": clean_kind,
        "source": clean_source,
        "title": clean_title,
        "content": clean_content,
        "subject_key": clean_subject,
        "evidence_ref": clean_evidence,
        "dimension": clean_dimension,
        "point": clean_point,
        "region": clean_region,
    }


def _validated_memory_search_geometry(
    *,
    center: tuple[float, float, float] | None,
    radius: float | None,
    region: tuple[float, float, float, float, float, float] | None,
) -> tuple[
    tuple[float, float, float] | None,
    float | None,
    tuple[float, float, float, float, float, float] | None,
]:
    clean_center = _validated_point(center, field_name="center")
    clean_region = _validated_region(region, field_name="region")
    clean_radius = None if radius is None else float(radius)
    if clean_radius is not None and (not math.isfinite(clean_radius) or clean_radius < 0):
        raise ValueError("radius must be a finite non-negative number")
    if (clean_center is None) != (clean_radius is None):
        raise ValueError("center and radius must be supplied together")
    if clean_center is not None and clean_region is not None:
        raise ValueError("search geometry must use center/radius or region, not both")
    return clean_center, clean_radius, clean_region


def _validated_point(
    value: tuple[float, float, float] | None,
    *,
    field_name: str,
) -> tuple[float, float, float] | None:
    if value is None:
        return None
    if len(value) != 3:
        raise ValueError(f"{field_name} must contain exactly 3 coordinates")
    clean = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in clean):
        raise ValueError(f"{field_name} coordinates must be finite")
    return clean


def _validated_region(
    value: tuple[float, float, float, float, float, float] | None,
    *,
    field_name: str,
) -> tuple[float, float, float, float, float, float] | None:
    if value is None:
        return None
    if len(value) != 6:
        raise ValueError(f"{field_name} must contain exactly 6 bounds")
    clean = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in clean):
        raise ValueError(f"{field_name} bounds must be finite")
    if clean[0] > clean[3] or clean[1] > clean[4] or clean[2] > clean[5]:
        raise ValueError(f"{field_name} minimum bounds must not exceed maximum bounds")
    return clean


def _point_columns(point: object) -> tuple[float | None, float | None, float | None]:
    if point is None:
        return None, None, None
    return tuple(point)  # type: ignore[arg-type,return-value]


def _region_columns(
    region: object,
) -> tuple[float | None, float | None, float | None, float | None, float | None, float | None]:
    if region is None:
        return None, None, None, None, None, None
    return tuple(region)  # type: ignore[arg-type,return-value]


def _memory_filter_sql(
    *,
    scope_key: str,
    kinds: tuple[MemoryKind, ...],
    sources: tuple[MemorySource, ...],
    subject_key: str,
    dimension: str | None,
    center: tuple[float, float, float] | None,
    radius: float | None,
    region: tuple[float, float, float, float, float, float] | None,
    alias: str,
) -> tuple[list[str], list[object]]:
    clauses = [f"{alias}.scope_key = ?"]
    params: list[object] = [scope_key]
    if kinds:
        clauses.append(f"{alias}.kind IN ({','.join('?' for _ in kinds)})")
        params.extend(item.value for item in kinds)
    if sources:
        clauses.append(f"{alias}.source IN ({','.join('?' for _ in sources)})")
        params.extend(item.value for item in sources)
    if subject_key:
        clauses.append(f"{alias}.subject_key = ?")
        params.append(subject_key)
    if dimension:
        clauses.append(f"{alias}.dimension = ?")
        params.append(dimension)
    if center is not None and radius is not None:
        cx, cy, cz = center
        clauses.append(
            f"""(
                ({alias}.x IS NOT NULL AND {alias}.x BETWEEN ? AND ?
                    AND {alias}.y BETWEEN ? AND ? AND {alias}.z BETWEEN ? AND ?)
                OR
                ({alias}.min_x IS NOT NULL AND {alias}.max_x >= ? AND {alias}.min_x <= ?
                    AND {alias}.max_y >= ? AND {alias}.min_y <= ?
                    AND {alias}.max_z >= ? AND {alias}.min_z <= ?)
            )"""
        )
        params.extend(
            (
                cx - radius,
                cx + radius,
                cy - radius,
                cy + radius,
                cz - radius,
                cz + radius,
                cx - radius,
                cx + radius,
                cy - radius,
                cy + radius,
                cz - radius,
                cz + radius,
            )
        )
    elif region is not None:
        min_x, min_y, min_z, max_x, max_y, max_z = region
        clauses.append(
            f"""(
                ({alias}.x IS NOT NULL AND {alias}.x BETWEEN ? AND ?
                    AND {alias}.y BETWEEN ? AND ? AND {alias}.z BETWEEN ? AND ?)
                OR
                ({alias}.min_x IS NOT NULL AND {alias}.max_x >= ? AND {alias}.min_x <= ?
                    AND {alias}.max_y >= ? AND {alias}.min_y <= ?
                    AND {alias}.max_z >= ? AND {alias}.min_z <= ?)
            )"""
        )
        params.extend(
            (
                min_x,
                max_x,
                min_y,
                max_y,
                min_z,
                max_z,
                min_x,
                max_x,
                min_y,
                max_y,
                min_z,
                max_z,
            )
        )
    return clauses, params


def _memory_terms_query(query: str) -> str:
    tokens = re.findall(r"[\w]+", query.casefold(), flags=re.UNICODE)
    unique = list(dict.fromkeys(token for token in tokens if token))[:24]
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in unique)


def _memory_trigram_query(query: str) -> str:
    tokens = re.findall(r"[\w]+", query.casefold(), flags=re.UNICODE)
    unique = list(dict.fromkeys(token for token in tokens if len(token) >= 3))[:16]
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in unique)


def _memory_like_query(query: str) -> tuple[str, list[object]]:
    tokens = re.findall(r"[\w]+", query.casefold(), flags=re.UNICODE)
    unique = list(dict.fromkeys(token for token in tokens if len(token) >= 2))[:12]
    clauses: list[str] = []
    params: list[object] = []
    for token in unique:
        escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        clauses.append(
            "(lower(e.title) LIKE ? ESCAPE '\\' OR lower(e.content) LIKE ? ESCAPE '\\' "
            "OR lower(e.subject_key) LIKE ? ESCAPE '\\')"
        )
        params.extend((pattern, pattern, pattern))
    return " OR ".join(clauses), params


def _memory_distance(
    record: MemoryRecord,
    center: tuple[float, float, float],
) -> float | None:
    if record.point is not None:
        return math.dist(record.point, center)
    if record.region is not None:
        min_x, min_y, min_z, max_x, max_y, max_z = record.region
        nearest = (
            min(max(center[0], min_x), max_x),
            min(max(center[1], min_y), max_y),
            min(max(center[2], min_z), max_z),
        )
        return math.dist(nearest, center)
    return None


def _memory_matches_geometry(
    record: MemoryRecord,
    *,
    center: tuple[float, float, float] | None,
    radius: float | None,
    region: tuple[float, float, float, float, float, float] | None,
) -> bool:
    if center is not None and radius is not None:
        distance = _memory_distance(record, center)
        return distance is not None and distance <= radius
    if region is None:
        return True
    min_x, min_y, min_z, max_x, max_y, max_z = region
    if record.point is not None:
        x, y, z = record.point
        return min_x <= x <= max_x and min_y <= y <= max_y and min_z <= z <= max_z
    if record.region is not None:
        rmin_x, rmin_y, rmin_z, rmax_x, rmax_y, rmax_z = record.region
        return (
            rmax_x >= min_x
            and rmin_x <= max_x
            and rmax_y >= min_y
            and rmin_y <= max_y
            and rmax_z >= min_z
            and rmin_z <= max_z
        )
    return False


def _fuse_memory_lanes(
    lanes: dict[str, list[MemoryRecord]],
    *,
    center: tuple[float, float, float] | None,
    radius: float | None,
    region: tuple[float, float, float, float, float, float] | None,
) -> list[dict[str, object]]:
    fused: dict[str, dict[str, object]] = {}
    for lane_name, records in lanes.items():
        if lane_name == "structured":
            records = sorted(
                records,
                key=lambda record: (
                    -_MEMORY_SOURCE_RANK[record.source],
                    record.title.casefold(),
                    record.memory_id,
                ),
            )
        for rank, record in enumerate(records, start=1):
            if not _memory_matches_geometry(
                record,
                center=center,
                radius=radius,
                region=region,
            ):
                continue
            item = fused.setdefault(
                record.memory_id,
                {"record": record, "score": 0.0, "lanes": []},
            )
            item["score"] = float(item["score"]) + 1.0 / (60.0 + rank)
            item["lanes"].append(lane_name)  # type: ignore[union-attr]
    ordered = sorted(
        fused.values(),
        key=lambda item: (
            -float(item["score"]),
            -_MEMORY_SOURCE_RANK[item["record"].source],  # type: ignore[union-attr]
            (
                _memory_distance(item["record"], center)  # type: ignore[arg-type]
                if center is not None
                else 0.0
            ),
            item["record"].memory_id,  # type: ignore[union-attr]
        ),
    )
    return [
        {
            **_memory_payload(item["record"], include_content=False),  # type: ignore[arg-type]
            "retrieval_score": round(float(item["score"]), 8),
            "match_lanes": list(item["lanes"]),
            "distance": (
                None
                if center is None
                else _memory_distance(item["record"], center)  # type: ignore[arg-type]
            ),
        }
        for item in ordered
    ]


def _memory_from_row(row: sqlite3.Row) -> MemoryRecord:
    point = None
    if row["x"] is not None:
        point = (float(row["x"]), float(row["y"]), float(row["z"]))
    region = None
    if row["min_x"] is not None:
        region = (
            float(row["min_x"]),
            float(row["min_y"]),
            float(row["min_z"]),
            float(row["max_x"]),
            float(row["max_y"]),
            float(row["max_z"]),
        )
    return MemoryRecord(
        memory_id=str(row["memory_id"]),
        scope_key=str(row["scope_key"]),
        revision=int(row["revision"]),
        kind=MemoryKind(str(row["kind"])),
        source=MemorySource(str(row["source"])),
        subject_key=str(row["subject_key"]),
        title=str(row["title"]),
        content=str(row["content"]),
        evidence_ref=str(row["evidence_ref"]),
        dimension=None if row["dimension"] is None else str(row["dimension"]),
        point=point,
        region=region,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        evidence_handles=_memory_evidence_handles(row),
        superseded_by=_memory_superseded_by(row),
    )


def _normalized_evidence_handles(raw: object) -> tuple[str, ...]:
    """Bounded, de-duplicated, order-preserving handle list."""
    if not isinstance(raw, (tuple, list)):
        return ()
    seen: list[str] = []
    for item in raw:
        handle = str(item).strip()[:256]
        if handle and handle not in seen:
            seen.append(handle)
        if len(seen) >= 16:
            break
    return tuple(seen)


def _memory_evidence_handles(row: sqlite3.Row) -> tuple[str, ...]:
    """Read plural handles defensively; pre-migration rows have none."""
    try:
        raw = row["evidence_handles_json"]
    except (IndexError, KeyError):
        return ()
    if not raw:
        return ()
    try:
        decoded = json.loads(str(raw))
    except json.JSONDecodeError:
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(str(item) for item in decoded if str(item).strip())


def _memory_superseded_by(row: sqlite3.Row) -> str | None:
    try:
        raw = row["superseded_by"]
    except (IndexError, KeyError):
        return None
    return None if raw is None else str(raw)


def _memory_payload(record: MemoryRecord, *, include_content: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "memory_id": record.memory_id,
        "revision": record.revision,
        "kind": record.kind.value,
        "source": record.source.value,
        "subject_key": record.subject_key or None,
        "title": record.title,
        "evidence_ref": record.evidence_ref or None,
        "evidence_handles": list(record.evidence_handles),
        "superseded_by": record.superseded_by,
        "dimension": record.dimension,
        "point": None if record.point is None else list(record.point),
        "region": None if record.region is None else list(record.region),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    if include_content:
        payload["content"] = record.content
        payload["complete"] = True
    else:
        payload["excerpt"] = record.content[:500]
        payload["content_truncated"] = len(record.content) > 500
    return payload


def memory_record_payload(
    record: MemoryRecord,
    *,
    include_content: bool = True,
) -> dict[str, object]:
    return _memory_payload(record, include_content=include_content)


def _skill_activation_from_row(row: sqlite3.Row) -> SkillActivationRecord:
    return SkillActivationRecord(
        activation_id=str(row["activation_id"]),
        scope_key=str(row["scope_key"]),
        task_id=None if row["task_id"] is None else str(row["task_id"]),
        owner_kind=str(row["owner_kind"]),
        owner_id=str(row["owner_id"]),
        skill_id=str(row["skill_id"]),
        skill_name=str(row["skill_name"]),
        skill_version=str(row["skill_version"]),
        activated_at=str(row["activated_at"]),
        ended_at=None if row["ended_at"] is None else str(row["ended_at"]),
    )


def skill_activation_payload(record: SkillActivationRecord) -> dict[str, object]:
    return {
        "activation_id": record.activation_id,
        "task_id": record.task_id,
        "owner_kind": record.owner_kind,
        "owner_id": record.owner_id,
        "skill_id": record.skill_id,
        "skill_name": record.skill_name,
        "skill_version": record.skill_version,
        "activated_at": record.activated_at,
        "ended_at": record.ended_at,
    }


def _skill_head_from_row(row: sqlite3.Row) -> SkillHeadRecord:
    return SkillHeadRecord(
        skill_id=str(row["skill_id"]),
        server_id=str(row["server_id"]),
        bot_id=str(row["bot_id"]),
        name=str(row["name"]),
        head_revision=int(row["head_revision"]),
        head_version=str(row["head_version"]),
        status=str(row["status"]),
        origin=str(row["origin"]),
        derived_from=str(row["derived_from"]),
        retired_at=None if row["retired_at"] is None else str(row["retired_at"]),
        retirement_evidence_refs=tuple(
            _json_string_list(row["retirement_evidence_refs_json"])
        ),
        retirement_reason=str(row["retirement_reason"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _skill_version_from_row(row: sqlite3.Row) -> SkillVersionRecord:
    return SkillVersionRecord(
        skill_id=str(row["skill_id"]),
        revision=int(row["revision"]),
        version_digest=str(row["version_digest"]),
        description=str(row["description"]),
        tools=tuple(_json_string_list(row["tools_json"])),
        body=str(row["body"]),
        evidence_refs=tuple(_json_string_list(row["evidence_refs_json"])),
        change_reason=str(row["change_reason"]),
        created_at=str(row["created_at"]),
    )


def _wiki_cache_from_row(row: sqlite3.Row) -> WikiCacheRecord:
    payload = _json_load(row["payload_json"], default={})
    if not isinstance(payload, dict):
        raise RuntimeStateError("stored Wiki cache payload is not an object")
    return WikiCacheRecord(
        cache_key=str(row["cache_key"]),
        endpoint=str(row["endpoint"]),
        kind=str(row["kind"]),
        request_key=str(row["request_key"]),
        payload=payload,
        etag=None if row["etag"] is None else str(row["etag"]),
        last_modified=None if row["last_modified"] is None else str(row["last_modified"]),
        fetched_at=str(row["fetched_at"]),
        expires_at=str(row["expires_at"]),
    )


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=str(row["task_id"]),
        scope_key=str(row["scope_key"]),
        revision=int(row["revision"]),
        goal_text=str(row["goal_text"]),
        source=str(row["source"]),
        requested_by=str(row["requested_by"]),
        status=TaskStatus(str(row["status"])),
        completion_authority=CompletionAuthority(str(row["completion_authority"])),
        active_plan_id=None if row["active_plan_id"] is None else str(row["active_plan_id"]),
        latest_checkpoint_id=(
            None if row["latest_checkpoint_id"] is None else str(row["latest_checkpoint_id"])
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _plan_from_rows(plan_row: sqlite3.Row, step_rows: list[sqlite3.Row]) -> TaskPlanRecord:
    return TaskPlanRecord(
        plan_id=str(plan_row["plan_id"]),
        task_id=str(plan_row["task_id"]),
        revision=int(plan_row["revision"]),
        summary=str(plan_row["summary"]),
        steps=tuple(
            PlanStepRecord(
                step_id=str(row["step_id"]),
                ordinal=int(row["ordinal"]),
                title=str(row["title"]),
                status=PlanStepStatus(str(row["status"])),
                evidence=tuple(_json_string_list(row["evidence_json"])),
                blocker=None if row["blocker"] is None else str(row["blocker"]),
                updated_at=str(row["updated_at"]),
            )
            for row in step_rows
        ),
        created_at=str(plan_row["created_at"]),
        updated_at=str(plan_row["updated_at"]),
    )


def _checkpoint_from_row(row: sqlite3.Row) -> TaskCheckpointRecord:
    raw_fingerprint = row["body_fingerprint_json"]
    fingerprint: dict[str, object] | None = None
    if raw_fingerprint is not None:
        try:
            decoded = json.loads(str(raw_fingerprint))
        except json.JSONDecodeError as exc:
            raise RuntimeStateError("stored body fingerprint JSON is corrupt") from exc
        if isinstance(decoded, dict):
            fingerprint = decoded
        else:
            raise RuntimeStateError("stored body fingerprint is not an object")
    continuation = _continuation_from_json(row["continuation_json"])
    return TaskCheckpointRecord(
        checkpoint_id=str(row["checkpoint_id"]),
        task_id=str(row["task_id"]),
        revision=int(row["revision"]),
        disposition=CheckpointDisposition(str(row["disposition"])),
        summary=str(row["summary"]),
        next_step=str(row["next_step"]),
        evidence=tuple(_json_string_list(row["evidence_json"])),
        wait_for=tuple(_json_string_list(row["wait_for_json"])),
        body_fingerprint=fingerprint,
        continuation=continuation,
        created_at=str(row["created_at"]),
    )


def _continuation_payload(contract: ContinuationContract) -> dict[str, object]:
    return {
        "objective": contract.objective,
        "operation_class": contract.operation_class.value,
        "target_descriptor": dict(contract.target_descriptor),
        "expected_evidence": list(contract.expected_evidence),
        "bounded_epoch_budget": contract.bounded_epoch_budget,
        "approach_key": contract.approach_key,
        "evidence_cursor": contract.evidence_cursor,
        "generation": contract.generation,
    }


def _continuation_from_json(raw: object) -> ContinuationContract | None:
    if raw is None:
        return None
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise RuntimeStateError("stored continuation contract JSON is corrupt") from exc
    if not isinstance(payload, dict):
        raise RuntimeStateError("stored continuation contract is not an object")
    descriptor = payload.get("target_descriptor")
    if not isinstance(descriptor, dict):
        raise RuntimeStateError("stored continuation target descriptor is not an object")
    expected = payload.get("expected_evidence")
    if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
        raise RuntimeStateError("stored continuation expected evidence is corrupt")
    try:
        return ContinuationContract(
            objective=str(payload["objective"]),
            operation_class=ContinuationOperationClass(str(payload["operation_class"])),
            target_descriptor=dict(descriptor),
            expected_evidence=tuple(expected),
            bounded_epoch_budget=int(payload["bounded_epoch_budget"]),
            approach_key=str(payload["approach_key"]),
            evidence_cursor=int(payload["evidence_cursor"]),
            generation=int(payload["generation"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeStateError("stored continuation contract fields are invalid") from exc


def _work_intent_from_row(row: sqlite3.Row) -> dict[str, object]:
    try:
        payload = json.loads(str(row["payload_json"]))
        error = None if row["error_json"] is None else json.loads(str(row["error_json"]))
    except json.JSONDecodeError as exc:
        raise RuntimeStateError("stored work intent JSON is corrupt") from exc
    if not isinstance(payload, dict):
        raise RuntimeStateError("stored work intent payload is not an object")
    if error is not None and not isinstance(error, dict):
        raise RuntimeStateError("stored work intent error is not an object")
    return {
        "intent_id": str(row["intent_id"]),
        "scope_key": str(row["scope_key"]),
        "revision": int(row["revision"]),
        "kind": str(row["kind"]),
        "source": str(row["source"]),
        "priority": int(row["priority"]),
        "payload": payload,
        "dedupe_key": None if row["dedupe_key"] is None else str(row["dedupe_key"]),
        "task_id": None if row["task_id"] is None else str(row["task_id"]),
        "generation": None if row["generation"] is None else int(row["generation"]),
        "state": str(row["state"]),
        "available_at": str(row["available_at"]),
        "created_at": str(row["created_at"]),
        "lease_owner": None if row["lease_owner"] is None else str(row["lease_owner"]),
        "lease_expires_at": (
            None if row["lease_expires_at"] is None else str(row["lease_expires_at"])
        ),
        "attempt_count": int(row["attempt_count"]),
        "leased_at": None if row["leased_at"] is None else str(row["leased_at"]),
        "completed_at": None if row["completed_at"] is None else str(row["completed_at"]),
        "error": error,
    }


def _progress_epoch_from_row(row: sqlite3.Row) -> dict[str, object]:
    members = _json_load(row["members_json"], default=[])
    evidence_refs = _json_load(row["evidence_refs_json"], default=[])
    epistemic_keys = _json_load(row["epistemic_keys_json"], default=[])
    novel_epistemic_keys = _json_load(row["novel_epistemic_keys_json"], default=[])
    if not isinstance(members, list) or not all(isinstance(item, dict) for item in members):
        raise RuntimeStateError("stored progress epoch members are corrupt")
    if not isinstance(evidence_refs, list) or not all(
        isinstance(item, str) for item in evidence_refs
    ):
        raise RuntimeStateError("stored progress epoch evidence refs are corrupt")
    if not isinstance(epistemic_keys, list) or not all(
        isinstance(item, str) for item in epistemic_keys
    ):
        raise RuntimeStateError("stored progress epoch epistemic keys are corrupt")
    if not isinstance(novel_epistemic_keys, list) or not all(
        isinstance(item, str) for item in novel_epistemic_keys
    ):
        raise RuntimeStateError("stored progress epoch novel epistemic keys are corrupt")
    return {
        "cursor": int(row["cursor"]),
        "epoch_id": str(row["epoch_id"]),
        "scope_key": str(row["scope_key"]),
        "run_id": str(row["run_id"]),
        "model_turn": int(row["model_turn"]),
        "members": members,
        "pre_body_fingerprint": (
            None
            if row["pre_body_fingerprint"] is None
            else str(row["pre_body_fingerprint"])
        ),
        "post_body_fingerprint": (
            None
            if row["post_body_fingerprint"] is None
            else str(row["post_body_fingerprint"])
        ),
        "evidence_refs": evidence_refs,
        "epistemic_keys": epistemic_keys,
        "novel_epistemic_keys": novel_epistemic_keys,
        "material_changed": bool(row["material_changed"]),
        "progress_aborted": bool(row["progress_aborted"]),
        "settled_at": str(row["settled_at"]),
    }


def _exploration_coverage_from_row(row: sqlite3.Row) -> dict[str, object]:
    center = _json_load(row["center_json"], default=[])
    observations = _json_load(row["observations_json"], default=[])
    negative_evidence = _json_load(row["negative_evidence_json"], default=[])
    uncertainty = _json_load(row["uncertainty_json"], default=[])
    if not isinstance(center, list) or len(center) != 3:
        raise RuntimeStateError("stored exploration coverage center is corrupt")
    if not isinstance(observations, list) or not all(isinstance(item, dict) for item in observations):
        raise RuntimeStateError("stored exploration coverage observations are corrupt")
    if not isinstance(negative_evidence, list) or not all(
        isinstance(item, str) for item in negative_evidence
    ):
        raise RuntimeStateError("stored exploration negative evidence is corrupt")
    if not isinstance(uncertainty, list) or not all(isinstance(item, dict) for item in uncertainty):
        raise RuntimeStateError("stored exploration uncertainty is corrupt")
    return {
        "cursor": int(row["cursor"]),
        "scope_key": str(row["scope_key"]),
        "dimension": str(row["dimension"]),
        "query_signature": str(row["query_signature"]),
        "region_x": int(row["region_x"]),
        "region_z": int(row["region_z"]),
        "status": str(row["status"]),
        "center": [int(value) for value in center],
        "reason": str(row["reason"]),
        "observations": [dict(item) for item in observations],
        "negative_evidence": list(negative_evidence),
        "uncertainty": [dict(item) for item in uncertainty],
        "created_at": str(row["created_at"]),
    }


def _tool_observation_from_row(
    row: sqlite3.Row,
    *,
    include_result: bool = True,
) -> dict[str, object]:
    result: object | None = None
    if include_result:
        result = _json_load(row["result_json"], default={})
        if not isinstance(result, dict):
            raise RuntimeStateError("stored tool observation result is not an object")
    complete = row["complete"]
    record: dict[str, object] = {
        "observation_id": str(row["observation_id"]),
        "scope_key": str(row["scope_key"]),
        "handle": str(row["handle"]),
        "tool": str(row["tool_name"]),
        "tool_call_id": str(row["tool_call_id"]),
        "success": bool(row["success"]),
        "reason": str(row["reason"]),
        "complete": None if complete is None else bool(complete),
        "payload_bytes": int(row["payload_bytes"]),
        "created_at": str(row["created_at"]),
    }
    if include_result:
        record["result"] = result
    return record


def _normalize_plan_steps(steps: list[dict[str, object]]) -> list[dict[str, object]]:
    if not isinstance(steps, list):
        raise ValueError("steps must be a list")
    if len(steps) > 64:
        raise ValueError("plan exceeds 64 steps")
    normalized: list[dict[str, object]] = []
    in_progress = 0
    for index, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise ValueError(f"plan step {index} must be an object")
        title = _required_text(f"steps[{index}].title", raw.get("title"), max_length=500)
        try:
            status = PlanStepStatus(str(raw.get("status") or PlanStepStatus.PENDING.value))
        except ValueError as exc:
            raise ValueError(f"invalid plan step status at index {index}") from exc
        if status is PlanStepStatus.IN_PROGRESS:
            in_progress += 1
        evidence = _bounded_text_list(
            raw.get("evidence") or (),
            max_items=16,
            max_length=1000,
        )
        blocker = _bounded_text(raw.get("blocker") or "", max_length=1000) or None
        normalized.append(
            {
                "title": title,
                "status": status.value,
                "evidence": evidence,
                "blocker": blocker,
            }
        )
    if in_progress > 1:
        raise ValueError("at most one plan step may be in_progress")
    return normalized


def _required_text(field_name: str, value: object, *, max_length: int) -> str:
    clean = _bounded_text(value, max_length=max_length)
    if not clean:
        raise ValueError(f"{field_name} must not be empty")
    return clean


def _strict_required_text(field_name: str, value: object, *, max_length: int) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{field_name} must not be empty")
    if len(clean) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    return clean


def _strict_optional_text(field_name: str, value: object, *, max_length: int) -> str:
    clean = str(value or "").strip()
    if len(clean) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    return clean


def _validated_string_tuple(
    field_name: str,
    values: object,
    *,
    max_items: int,
    max_length: int,
) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of strings")
    if len(values) > max_items:
        raise ValueError(f"{field_name} exceeds {max_items} items")
    clean: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must contain only strings")
        item = value.strip()
        if not item:
            raise ValueError(f"{field_name} contains an empty item")
        if len(item) > max_length:
            raise ValueError(f"{field_name} item exceeds {max_length} characters")
        clean.append(item)
    if len(set(clean)) != len(clean):
        raise ValueError(f"{field_name} contains duplicate items")
    return tuple(clean)


def _validated_json_objects(
    field_name: str,
    values: object,
    *,
    max_items: int,
) -> list[dict[str, object]]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of objects")
    if len(values) > max_items:
        raise ValueError(f"{field_name} exceeds {max_items} items")
    normalized: list[dict[str, object]] = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError(f"{field_name} must contain only objects")
        encoded = _json_dump(value)
        if len(encoded.encode("utf-8")) > 16_384:
            raise ValueError(f"{field_name} item exceeds 16384 bytes")
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):
            raise ValueError(f"{field_name} must contain only objects")
        normalized.append(decoded)
    return normalized


def _bounded_text(value: object, *, max_length: int) -> str:
    return " ".join(str(value or "").strip().split())[:max_length]


def _bounded_text_list(
    values: object,
    *,
    max_items: int,
    max_length: int,
) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("expected a list of strings")
    items = [
        clean
        for value in values[:max_items]
        if (clean := _bounded_text(value, max_length=max_length))
    ]
    return items


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_load(value: object, *, default: object) -> object:
    if value is None:
        return default
    try:
        return json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise RuntimeStateError("stored JSON value is corrupt") from exc


def _json_string_list(value: object) -> list[str]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise RuntimeStateError("stored JSON list is corrupt") from exc
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise RuntimeStateError("stored JSON value is not a string list")
    return decoded


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _utc_after(seconds: float) -> str:
    from datetime import timedelta

    return (datetime.now(UTC) + timedelta(seconds=max(0.0, float(seconds)))).isoformat(
        timespec="milliseconds"
    )
