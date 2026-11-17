"""Recovery-attempt accounting inside the existing work-intent ledger."""

from __future__ import annotations

from collections.abc import Iterable
import math

from minebot.app.runtime_records import RuntimeStateConflict
from minebot.contract import BodyState


def _validated_admission(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeStateConflict("invalid recovery admission")
    for name in ("sequence", "attempt", "limit"):
        if type(value.get(name)) is not int or value[name] < 1:
            raise RuntimeStateConflict(f"invalid recovery admission {name}")
    if (
        not isinstance(value.get("episode_id"), str) or not value["episode_id"]
        or type(value.get("confirmed")) is not bool or value["attempt"] > value["limit"]
    ):
        raise RuntimeStateConflict("invalid recovery admission state")
    return value


def next_recovery_admission(
    payload: dict[str, object], history: Iterable[dict[str, object]], *,
    intent_id: str, max_attempts: int,
) -> dict[str, object] | None:
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("recovery attempt limit must be positive")
    if not {"admission_task_id", "budget_checkpoint_id", "admission_evidence_cursor"} <= payload.keys():
        raise RuntimeStateConflict("recovery requires bound work admission")
    if "recovery_admission" in payload:
        raise RuntimeStateConflict("a reserved recovery invocation must not be replayed")
    owner = payload["admission_task_id"]
    previous = []
    for record in history:
        if record["intent_id"] == intent_id:
            continue
        prior = record["payload"]
        if "admission_task_id" not in prior or prior["admission_task_id"] != owner:
            continue
        if "recovery_admission" in prior:
            previous.append(_validated_admission(prior["recovery_admission"]))
        elif prior.get("recovery_admission_denied") is not True and record["source"] == "lifecycle_recovery" and (
            record["state"] in {"completed", "failed", "superseded"}
            or (record["state"] == "queued" and record.get("attempt_count", 0) > 0)
        ):
            raise RuntimeStateConflict("legacy recovery attempt has no durable reservation")
    sequences = [value["sequence"] for value in previous]
    if len(set(sequences)) != len(sequences):
        raise RuntimeStateConflict("duplicate recovery admission sequence")
    latest = max(previous, key=lambda value: value["sequence"], default=None)
    continuing = latest is not None and not latest["confirmed"]
    limit = min(max_attempts, latest["limit"]) if continuing else max_attempts
    attempt = latest["attempt"] + 1 if continuing else 1
    if attempt > limit:
        return None
    return {
        "episode_id": latest["episode_id"] if continuing else intent_id,
        "sequence": 1 if latest is None else latest["sequence"] + 1,
        "attempt": attempt,
        "limit": limit,
        "confirmed": False,
    }


def confirmed_recovery_payload(payload: dict[str, object]) -> dict[str, object]:
    admission = _validated_admission(payload.get("recovery_admission"))
    return {**payload, "recovery_admission": {**admission, "confirmed": True}}


def reconciled_recovery_payload(
    current: dict[str, object], history: Iterable[dict[str, object]], *, state: BodyState,
) -> tuple[str, dict[str, object]] | None:
    """Settle a terminal episode using a newly owned startup observation."""
    payload = current["payload"]
    task_id = current["task_id"]
    if current["kind"] != "recovery_reconcile" or current["source"] != "startup_reconcile":
        raise RuntimeStateConflict("recovery reconciliation requires startup ownership")
    task = payload.get("task")
    if (not task_id or not isinstance(task, dict) or task.get("task_id") != task_id
        or payload.get("admission_task_id", task_id) != task_id):
        raise RuntimeStateConflict("recovery reconciliation requires the original task")
    if "recovery_reconciled_intent_id" in payload:
        return None
    observed = payload.get("state")
    if (not isinstance(observed, dict) or observed.get("bot") != state.bot
        or state.complete is not True or state.missing is not False
        or not math.isfinite(state.health) or state.health <= 0
        or state.body_owner is not None or type(state.pending_action_count) is not int
        or state.pending_action_count != 0 or state.hazard_unresolved is not None):
        return None
    candidates = []
    for record in history:
        prior = record["payload"]
        if prior.get("admission_task_id") != task_id:
            if record.get("task_id") == task_id and "recovery_admission" in prior:
                raise RuntimeStateConflict("recovery history has ambiguous task ownership")
            continue
        if "recovery_admission" not in prior:
            continue
        admission = _validated_admission(prior["recovery_admission"])
        if record["state"] not in {"completed", "failed", "superseded"}:
            raise RuntimeStateConflict("cannot reconcile a live recovery invocation")
        candidates.append((record, admission))
    sequences = [admission["sequence"] for _, admission in candidates]
    if len(sequences) != len(set(sequences)):
        raise RuntimeStateConflict("duplicate recovery admission sequence")
    latest = max(candidates, key=lambda item: item[1]["sequence"], default=None)
    if latest is None or latest[1]["confirmed"]:
        return None
    record, _ = latest
    confirmed = confirmed_recovery_payload(record["payload"])
    confirmed["recovery_confirmation"] = {
        "source": "startup_body_state", "intent_id": current["intent_id"],
        "task_id": task_id, "bot": state.bot, "health": state.health,
        "complete": state.complete, "missing": state.missing,
        "body_owner": state.body_owner, "pending_action_count": state.pending_action_count,
        "hazard_unresolved": state.hazard_unresolved,
    }
    return record["intent_id"], confirmed
