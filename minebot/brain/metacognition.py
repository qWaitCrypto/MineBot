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


# -- replay drift analysis (brain-cognitive-framework.md §10.1) -------------
#
# A drift report compares what the original run's model chose (the fixture's
# ``chosen`` batch) against what a candidate model chooses when replayed on
# the same compiled context. Comparison is deterministic and bounded:
#
#   identical   — same tool multiset AND normalized arguments
#   same_tools  — same tool multiset, different arguments
#   divergent   — different tool multiset
#   unreplayed  — the replay produced no decision for this fixture
#
# Aggregation is per decision context, because that is the unit routing
# decisions are made at.


@dataclass(frozen=True)
class ReplayedDecision:
    fixture_id: str
    chosen: tuple[JsonObject, ...]
    error: str | None = None


@dataclass(frozen=True)
class FixtureComparison:
    fixture_id: str
    decision_context: str
    verdict: str                      # identical|same_tools|divergent|unreplayed
    original_tools: tuple[str, ...]
    replayed_tools: tuple[str, ...]
    surface_digest_match: bool
    error: str | None = None


@dataclass(frozen=True)
class DriftReport:
    total: int
    by_context: JsonObject            # context -> {verdict -> count, drift_rate}
    substitutions: tuple[tuple[str, int], ...]  # "orig->replayed" tool drift pairs
    comparisons: tuple[FixtureComparison, ...]

    def to_payload(self) -> JsonObject:
        return {
            "total": self.total,
            "by_context": dict(self.by_context),
            "substitutions": [list(item) for item in self.substitutions],
            "comparisons": [
                {
                    "fixture_id": item.fixture_id,
                    "decision_context": item.decision_context,
                    "verdict": item.verdict,
                    "original_tools": list(item.original_tools),
                    "replayed_tools": list(item.replayed_tools),
                    "surface_digest_match": item.surface_digest_match,
                    "error": item.error,
                }
                for item in self.comparisons
            ],
        }


def compare_decision(
    fixture: DecisionFixture,
    replay: ReplayedDecision | None,
    *,
    current_surface_digest: str | None = None,
) -> FixtureComparison:
    original_tools = _tool_sequence(fixture.chosen)
    surface_match = (
        current_surface_digest is None
        or current_surface_digest == fixture.tool_surface_digest
    )
    if replay is None or replay.error is not None:
        return FixtureComparison(
            fixture_id=fixture.fixture_id,
            decision_context=fixture.decision_context,
            verdict="unreplayed",
            original_tools=original_tools,
            replayed_tools=(),
            surface_digest_match=surface_match,
            error=None if replay is None else replay.error,
        )
    replayed_tools = _tool_sequence(replay.chosen)
    if sorted(original_tools) != sorted(replayed_tools):
        verdict = "divergent"
    elif _normalized_argument_multiset(fixture.chosen) == _normalized_argument_multiset(replay.chosen):
        verdict = "identical"
    else:
        verdict = "same_tools"
    return FixtureComparison(
        fixture_id=fixture.fixture_id,
        decision_context=fixture.decision_context,
        verdict=verdict,
        original_tools=original_tools,
        replayed_tools=replayed_tools,
        surface_digest_match=surface_match,
    )


def drift_report(
    fixtures: Iterable[DecisionFixture],
    replays: Mapping[str, ReplayedDecision],
    *,
    current_surface_digest: str | None = None,
) -> DriftReport:
    comparisons = tuple(
        compare_decision(
            fixture,
            replays.get(fixture.fixture_id),
            current_surface_digest=current_surface_digest,
        )
        for fixture in fixtures
    )
    by_context: dict[str, dict[str, object]] = {}
    substitution_counts: dict[str, int] = {}
    for item in comparisons:
        bucket = by_context.setdefault(
            item.decision_context,
            {"identical": 0, "same_tools": 0, "divergent": 0, "unreplayed": 0},
        )
        bucket[item.verdict] = int(bucket[item.verdict]) + 1  # type: ignore[call-overload]
        if item.verdict == "divergent":
            for original, replayed in _tool_substitutions(item.original_tools, item.replayed_tools):
                key = f"{original}->{replayed}"
                substitution_counts[key] = substitution_counts.get(key, 0) + 1
    for bucket in by_context.values():
        replayed_total = sum(
            int(bucket[verdict]) for verdict in ("identical", "same_tools", "divergent")  # type: ignore[call-overload]
        )
        drifted = int(bucket["divergent"]) + int(bucket["same_tools"])  # type: ignore[call-overload]
        bucket["drift_rate"] = (
            None if replayed_total == 0 else round(drifted / replayed_total, 4)
        )
    substitutions = tuple(
        sorted(substitution_counts.items(), key=lambda item: (-item[1], item[0]))[:16]
    )
    return DriftReport(
        total=len(comparisons),
        by_context=dict(by_context),
        substitutions=substitutions,
        comparisons=comparisons,
    )


def _tool_sequence(chosen: Iterable[Mapping[str, object]]) -> tuple[str, ...]:
    return tuple(str(item.get("tool") or "") for item in chosen)


def _normalized_argument_multiset(chosen: Iterable[Mapping[str, object]]) -> list[tuple[str, str]]:
    normalized = [
        (str(item.get("tool") or ""), _normalized_arguments(item.get("arguments")))
        for item in chosen
    ]
    return sorted(normalized)


def _normalized_arguments(raw: object) -> str:
    if raw is None:
        return "{}"
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=True, sort_keys=True, default=str)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return str(text)
    return json.dumps(parsed, ensure_ascii=True, sort_keys=True, default=str)


def _tool_substitutions(
    original: tuple[str, ...], replayed: tuple[str, ...]
) -> list[tuple[str, str]]:
    original_only = _multiset_difference(original, replayed)
    replayed_only = _multiset_difference(replayed, original)
    pairs: list[tuple[str, str]] = []
    for index in range(max(len(original_only), len(replayed_only))):
        left = original_only[index] if index < len(original_only) else "<none>"
        right = replayed_only[index] if index < len(replayed_only) else "<none>"
        pairs.append((left, right))
    return pairs


def _multiset_difference(left: tuple[str, ...], right: tuple[str, ...]) -> list[str]:
    remaining = list(right)
    out: list[str] = []
    for item in left:
        if item in remaining:
            remaining.remove(item)
        else:
            out.append(item)
    return sorted(out)


__all__ = [
    "DecisionFixture",
    "DriftReport",
    "FixtureComparison",
    "ReplayedDecision",
    "backfill_decision_context",
    "compare_decision",
    "drift_report",
    "fixtures_from_trace",
    "tool_surface_digest",
]
