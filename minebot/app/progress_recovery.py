"""Decode durable epoch inputs and replay only the progress decision, never tools."""

from __future__ import annotations

import math

from minebot.brain.progress import ProgressAuthority, ProgressStep
from minebot.contract import ProgressAbort, ProgressFacts, ProgressStopReason, dominant_progress_abort


def _action_key(value: object) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise ValueError("progress action key must be an array")
    items = []
    for item in value:
        if isinstance(item, (list, tuple)):
            items.append(_action_key(item))
        elif item is None or type(item) in (str, int, bool):
            items.append(item)
        elif type(item) is float and math.isfinite(item):
            items.append(item)
        else:
            raise ValueError("invalid progress action key component")
    return tuple(items)


def _text_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError("progress evidence must be an array of text")
    return list(value)


def replay_progress_epoch(record: dict[str, object]) -> tuple[ProgressFacts, ProgressAbort | None]:
    """Reject missing inputs instead of inferring a fresh progress allowance."""
    commit = record.get("commit_input")
    if not isinstance(commit, dict) or type(commit.get("version")) is not int or commit["version"] != 1:
        raise ValueError("unsupported or missing progress commit input")
    if type(commit.get("apply")) is not bool:
        raise ValueError("progress commit applicability is missing")
    before = commit.get("authority_before")
    if not isinstance(before, dict):
        raise ValueError("progress authority baseline is missing")
    if "last_action" not in before:
        raise ValueError("progress action baseline is missing")
    for key in ("stagnant_steps", "stalled_steps", "failure_steps", "epistemic_steps"):
        if type(before.get(key)) is not int or before[key] < 0:
            raise ValueError(f"invalid progress counter: {key}")
    for key in ("goal", "last_fingerprint", "current_fingerprint"):
        if not isinstance(before.get(key), str):
            raise ValueError(f"invalid progress field: {key}")
    facts = ProgressFacts(
        **{key: before[key] for key in (
            "goal", "stagnant_steps", "stalled_steps", "failure_steps", "epistemic_steps",
            "last_fingerprint", "current_fingerprint",
        )},
        last_action=None if before["last_action"] is None else _action_key(before["last_action"]),
        last_epistemic_keys=tuple(_text_list(before.get("last_epistemic_keys"))),
        recent_events=_text_list(before.get("recent_events")),
    )
    raw_steps = commit.get("steps")
    if not isinstance(raw_steps, list) or (not commit["apply"] and raw_steps):
        raise ValueError("invalid collapsed progress steps")
    steps = []
    for step in raw_steps:
        if (
            not isinstance(step, dict) or step.get("kind") not in ("note", "observe")
            or not isinstance(step.get("fingerprint"), str)
            or type(step.get("neutral")) is not bool
            or (step["kind"] == "note" and type(step.get("success")) is not bool)
            or (step["kind"] == "observe" and step.get("success") is not None)
        ):
            raise ValueError("invalid collapsed progress step")
        steps.append(ProgressStep(
            step["kind"], _action_key(step.get("action_key")), step["fingerprint"],
            success=step.get("success"), neutral=step["neutral"],
        ))
    novel = _text_list(record.get("novel_epistemic_keys"))
    if type(record.get("material_changed")) is not bool or type(record.get("progress_aborted")) is not bool:
        raise ValueError("progress epoch outcome is missing")
    pending = [ProgressAbort("restored progress stop", stop_reason=ProgressStopReason(reason))
        for reason in _text_list(commit.get("pending_stop_reasons"))]
    if record["progress_aborted"]:
        pending.append(ProgressAbort("restored progress stop",
            stop_reason=ProgressStopReason(record.get("progress_stop_reason"))))
    authority = ProgressAuthority()
    authority.restore_facts(facts)
    abort = None
    if commit["apply"]:
        try:
            authority.commit_steps(steps, facts.goal,
                novel_epistemic_keys=novel, material_changed=record["material_changed"])
        except ProgressAbort as exc:
            abort = exc
    restored = dominant_progress_abort(abort, *pending)
    if restored is not None:
        # Keep the stop bound to its epoch across restart, so a reconsideration
        # grant cannot be re-bought for a stop that already paid for one.
        restored.stop_epoch_id = str(record.get("epoch_id") or "") or None
    return authority.facts(facts.goal), restored
