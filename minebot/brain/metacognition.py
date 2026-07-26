"""F7 Metacognition — decision-replay fixtures from runtime trace streams.

brain-cognitive-framework.md §10.1. P0 scope: the fixture schema and the pure
assembly logic that turns one persisted ``RuntimeTrace`` event stream into
decision fixtures — one fixture per progress epoch, i.e. per settled
model-response batch. File I/O lives in ``tools/harvest_decision_corpus.py``;
this module is pure data-in/data-out so the corpus format is unit-provable.

Backfill honesty: old traces do not record work-intent kinds, so only the
deterministically recoverable classes (``mobility`` / ``recovery`` / default
``normal``) are backfilled; every fixture labels its context as
``trace-recorded`` + ``backfilled`` so replay analysis never mistakes a
backfilled class for a live-classified one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from minebot.brain.deliberation import DecisionContext

JsonObject = dict[str, object]

# Bounded recorded facts copied into ``compiled_context`` (the exact context
# facts the runtime logged next to each tool decision — r50b mechanism).
_CONTEXT_FACT_KEYS = (
    "situational",
    "lifecycle",
    "policy_tags",
    "tool_focus",
    "last_known_body_state",
    "recent_session_messages",
    "recent_tool_results",
)
_MAX_COMPILED_CONTEXT_CHARS = 8_000


@dataclass(frozen=True)
class DecisionFixture:
    """One replayable model decision point (one settled progress epoch)."""

    fixture_id: str
    source_run: str
    seq: int
    decision_context: str
    compiled_context: str
    tool_surface_digest: str
    chosen: tuple[JsonObject, ...]
    settled: tuple[JsonObject, ...]
    outcome: JsonObject
    labels: JsonObject = field(default_factory=dict)

    def to_payload(self) -> JsonObject:
        return {
            "fixture_id": self.fixture_id,
            "source_run": self.source_run,
            "seq": self.seq,
            "decision_context": self.decision_context,
            "compiled_context": self.compiled_context,
            "tool_surface_digest": self.tool_surface_digest,
            "chosen": list(self.chosen),
            "settled": list(self.settled),
            "outcome": dict(self.outcome),
            "labels": dict(self.labels),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "DecisionFixture":
        return cls(
            fixture_id=str(payload["fixture_id"]),
            source_run=str(payload["source_run"]),
            seq=int(payload["seq"]),  # type: ignore[arg-type]
            decision_context=str(payload["decision_context"]),
            compiled_context=str(payload["compiled_context"]),
            tool_surface_digest=str(payload["tool_surface_digest"]),
            chosen=tuple(dict(item) for item in payload.get("chosen", ())),  # type: ignore[union-attr]
            settled=tuple(dict(item) for item in payload.get("settled", ())),  # type: ignore[union-attr]
            outcome=dict(payload.get("outcome", {})),  # type: ignore[arg-type]
            labels=dict(payload.get("labels", {})),  # type: ignore[arg-type]
        )


def backfill_decision_context(situational: object, lifecycle: object) -> DecisionContext:
    """Map recorded stance facts to the F1 class for pre-F1 traces.

    Only mobility/recovery are deterministically recoverable from old traces;
    boundary/maintenance/social need intent facts that were not recorded, so
    everything else honestly stays ``normal``.
    """

    stance = str(situational or "").strip().lower()
    state = str(lifecycle or "").strip().lower()
    if stance == "death" or state == "recovering":
        return DecisionContext.RECOVERY
    if stance == "mobility":
        return DecisionContext.MOBILITY
    return DecisionContext.NORMAL


def tool_surface_digest(manifest_tools: object) -> str:
    encoded = json.dumps(manifest_tools, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def fixtures_from_trace(
    events: Iterable[Mapping[str, object]],
    *,
    source_run: str,
) -> tuple[DecisionFixture, ...]:
    """Assemble one fixture per settled progress epoch from a trace stream.

    Pure single pass; tolerant of partial epochs (an unsettled member is
    recorded as ``{"status": "unsettled"}`` rather than dropped, so honest
    cancellation/abort evidence survives into the corpus).
    """

    surface_digest = ""
    invokes: dict[str, JsonObject] = {}
    contexts: dict[str, JsonObject] = {}
    results: dict[str, JsonObject] = {}
    open_epochs: dict[str, JsonObject] = {}
    fixtures: list[DecisionFixture] = []

    for event in events:
        kind = event.get("event")
        if kind == "tool_manifest":
            surface_digest = tool_surface_digest(event.get("tools"))
        elif kind == "tool_invoke":
            call_id = str(event.get("tool_call_id") or "")
            if call_id:
                invokes[call_id] = {
                    "tool": event.get("tool"),
                    "arguments": event.get("arguments_summary"),
                }
        elif kind == "tool_decision_context":
            call_id = str(event.get("tool_call_id") or "")
            if call_id:
                contexts[call_id] = {key: event.get(key) for key in _CONTEXT_FACT_KEYS if key in event}
        elif kind == "tool_result":
            call_id = str(event.get("tool_call_id") or "")
            if call_id:
                results[call_id] = {
                    "tool": event.get("tool"),
                    "success": event.get("success"),
                    "reason": event.get("reason"),
                    "model_result": event.get("model_result"),
                    "observation_handle": event.get("observation_handle"),
                }
        elif kind == "progress_epoch_opened":
            epoch_id = str(event.get("epoch_id") or "")
            if epoch_id:
                open_epochs[epoch_id] = {
                    "seq": event.get("seq"),
                    "model_turn": event.get("model_turn"),
                    "run_id": event.get("run_id"),
                    "member_ids": list(event.get("member_tool_call_ids") or ()),
                    "member_tools": list(event.get("member_tools") or ()),
                    "pre_body_fingerprint": event.get("pre_body_fingerprint"),
                }
        elif kind == "progress_epoch_settled":
            epoch_id = str(event.get("epoch_id") or "")
            opened = open_epochs.pop(epoch_id, None)
            if opened is None:
                continue
            fixtures.append(
                _assemble_fixture(
                    source_run=source_run,
                    surface_digest=surface_digest,
                    opened=opened,
                    settled_event=event,
                    epoch_id=epoch_id,
                    invokes=invokes,
                    contexts=contexts,
                    results=results,
                )
            )
    return tuple(fixtures)


def _assemble_fixture(
    *,
    source_run: str,
    surface_digest: str,
    opened: JsonObject,
    settled_event: Mapping[str, object],
    epoch_id: str,
    invokes: Mapping[str, JsonObject],
    contexts: Mapping[str, JsonObject],
    results: Mapping[str, JsonObject],
) -> DecisionFixture:
    member_ids = [str(item) for item in opened.get("member_ids", ())]  # type: ignore[union-attr]
    member_tools = [str(item) for item in opened.get("member_tools", ())]  # type: ignore[union-attr]

    chosen: list[JsonObject] = []
    settled: list[JsonObject] = []
    for index, call_id in enumerate(member_ids):
        tool = invokes.get(call_id, {}).get("tool") or (
            member_tools[index] if index < len(member_tools) else None
        )
        chosen.append(
            {
                "tool_call_id": call_id,
                "tool": tool,
                "arguments": invokes.get(call_id, {}).get("arguments"),
            }
        )
        result = results.get(call_id)
        if result is None:
            settled.append({"tool_call_id": call_id, "tool": tool, "status": "unsettled"})
        else:
            settled.append({"tool_call_id": call_id, **result})

    first_context = next(
        (contexts[call_id] for call_id in member_ids if call_id in contexts),
        {},
    )
    compiled_context = json.dumps(
        first_context, ensure_ascii=True, sort_keys=True, default=str
    )[:_MAX_COMPILED_CONTEXT_CHARS]
    context_class = backfill_decision_context(
        first_context.get("situational"), first_context.get("lifecycle")
    )

    outcome: JsonObject = {
        key: settled_event.get(key)
        for key in (
            "material_changed",
            "progress_aborted",
            "committed_progress_step_count",
            "captured_progress_step_count",
            "epistemic_steps",
            "pre_body_fingerprint",
            "post_body_fingerprint",
            "cursor",
        )
        if key in settled_event
    }

    seq_value = opened.get("seq")
    return DecisionFixture(
        fixture_id=f"{source_run}#{epoch_id}",
        source_run=source_run,
        seq=int(seq_value) if isinstance(seq_value, (int, float)) else 0,
        decision_context=context_class.value,
        compiled_context=compiled_context,
        tool_surface_digest=surface_digest,
        chosen=tuple(chosen),
        settled=tuple(settled),
        outcome=outcome,
        labels={
            "context_source": "trace-recorded",
            "decision_context_backfilled": True,
            "model_turn": opened.get("model_turn"),
            "run_id": opened.get("run_id"),
        },
    )


__all__ = [
    "DecisionFixture",
    "backfill_decision_context",
    "fixtures_from_trace",
    "tool_surface_digest",
]
