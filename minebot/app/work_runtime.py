"""Interpreter for the candidate ``minebot-work/0.2`` language.

This module is intentionally independent of the OpenAI SDK.  The slow Agent
produces a JSON tree; this runtime validates and executes it through injected
call, state and Decision seams.  That keeps the language testable with a fake
Body and prevents model-authored ``_runtime`` fields from becoming truth.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol

from jsonschema import Draft202012Validator

from minebot.app.decision_client import DecisionClient, DecisionPriority
from minebot.app import reflex_question as rq

NodeStatus = Literal["pending", "active", "waiting", "blocked", "done", "cancelled"]
ConditionStatus = Literal["satisfied", "pending", "unknown"]
MAX_NODE_UNKNOWN_STREAK = 3

WORK_FIELD_VOCABULARY: dict[str, str] = {
    "pos": "current Body position [x,y,z] from the latest complete state read",
    "food": "survival food level; the provider's native range and current value are authoritative",
    "health": "survival health level from the latest Body state",
    "inventory_counts": "complete item counts when inventory_complete is true",
    "inventory_complete": "true means the count map was read in full; false/null means absence is unknown",
    "inventory_includes_selected": "whether inventory_counts already includes the selected mainhand stack",
    "selected_item": "selected mainhand item; null is meaningful only when selected_item_known is true",
    "selected_item_known": "true means the selected mainhand slot was observed",
    "pending_action_count": "number of Python-side action handles; not progress or success evidence",
    "nearby_entities": "bounded nearest-first entity observation; absence is scoped to the sampled radius",
    "entity_id": "stable entity/player identifier when the Body provider supplies one",
    "entity_type": "provider entity type; player and mob types are distinct",
    "sampled_at_ms": "provider timestamp for the observation",
    "sample_age_s": "seconds since the observation was sampled",
}


class WorkPlanError(ValueError):
    """A plan is structurally invalid or uses an unsupported slice feature."""


class WorkCall(Protocol):
    async def __call__(self, tool: str, arguments: dict[str, Any], *, call_id: str, timeout_s: float | None) -> dict[str, Any]: ...


class WorkState(Protocol):
    async def __call__(self) -> dict[str, Any]: ...


class WorkDecision(Protocol):
    async def ask(self, payload: dict[str, Any], *, priority: DecisionPriority = DecisionPriority.PLAN) -> Any: ...


class WorkChoiceProvider(Protocol):
    def __call__(self, node: dict[str, Any], *, intents: list[str], state: dict[str, Any], item: Any) -> Any: ...


class WorkIdleSupervisor(Protocol):
    async def __call__(
        self, tool: str, *, call_id: str, plan_context: dict[str, Any],
        options: dict[str, str], facts: dict[str, Any],
    ) -> dict[str, Any]: ...


def _schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "docs" / "design-docs" / "agent-work-language-v0.2.schema.json"


def validate_plan(plan: dict[str, Any]) -> None:
    """Validate the candidate schema and reject features this slice defers."""

    if not isinstance(plan, dict):
        raise WorkPlanError("plan must be an object")

    def require_composite_intent(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        kind = node.get("kind")
        if kind in {"sequence", "batch", "choice"}:
            intent = node.get("intent")
            if not isinstance(intent, str) or not intent.strip():
                raise WorkPlanError(f"'intent' is a required property at {path}")
        if kind == "sequence":
            for index, child in enumerate(node.get("steps", [])):
                require_composite_intent(child, f"{path}.steps[{index}]")
        elif kind == "batch":
            require_composite_intent(node.get("body"), f"{path}.body")
        elif kind == "choice":
            for option in node.get("options", []):
                if isinstance(option, dict):
                    require_composite_intent(
                        option.get("work"), f"{path}.options.{option.get('id', '?')}"
                    )

    require_composite_intent(plan.get("work"), "work")
    path = _schema_path()
    if path.exists():
        schema = json.loads(path.read_text(encoding="utf-8"))
        errors = sorted(Draft202012Validator(schema).iter_errors(plan), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first.path) or "$"
            raise WorkPlanError(f"invalid plan at {location}: {first.message}")
    if plan.get("language") != "minebot-work/0.2":
        raise WorkPlanError("language must be minebot-work/0.2")
    if not isinstance(plan.get("timeout_s"), (int, float)) or plan["timeout_s"] <= 0:
        raise WorkPlanError("plan timeout_s must be positive")
    def visit(node: dict[str, Any], path: str) -> None:
        if node.get("interrupt_when") is not None:
            raise WorkPlanError(f"unsupported_in_slice2 at {path}: interrupt_when")
        repeat = node.get("repeat")
        if repeat is not None and not isinstance(repeat, dict):
            raise WorkPlanError(f"repeat must be an object at {path}")
        if isinstance(repeat, dict):
            for key in ("again_when", "until"):
                if key in repeat and not isinstance(repeat[key], (str, dict)):
                    raise WorkPlanError(f"repeat.{key} must be text or an object at {path}")
        if node.get("kind") == "wait" and (not isinstance(node.get("timeout_s"), (int, float)) or node["timeout_s"] <= 0):
            raise WorkPlanError(f"wait timeout_s must be positive at {path}")
        if node.get("kind") == "sequence":
            for index, child in enumerate(node.get("steps", [])):
                visit(child, f"{path}.steps[{index}]")
        elif node.get("kind") == "batch":
            visit(node["body"], f"{path}.body")
        elif node.get("kind") == "choice":
            for option in node.get("options", []):
                visit(option["work"], f"{path}.options.{option['id']}")
    visit(plan["work"], "work")


def _node_id(node: dict[str, Any], path: str) -> str:
    value = node.get("id")
    return str(value) if isinstance(value, str) and value else path


def _state_payload(state: Any) -> dict[str, Any]:
    if isinstance(state, dict):
        return copy.deepcopy(state)
    if is_dataclass(state):
        return copy.deepcopy(asdict(state))
    values = getattr(state, "__dict__", None)
    return copy.deepcopy(values) if isinstance(values, dict) else {}


def _decision_result_facts(value: Any) -> dict[str, Any]:
    """Keep the raw projected result; the result projector already bounds it."""
    if isinstance(value, dict):
        return copy.deepcopy(value)
    return {"result": str(value)[:500]}


def _condition_facts(
    state: Any,
    condition: dict[str, Any],
    observations: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Compile complete, binding-owned facts without silently cropping them."""
    raw = _state_payload(state)
    bindings = condition.get("bindings") if isinstance(condition.get("bindings"), dict) else {}
    facts: dict[str, Any] = {}
    unknown_fields: list[dict[str, str]] = []
    observations = observations or {}
    if not bindings:
        facts["bot"] = {
            "observed": bool(raw),
            "values": raw if raw else None,
            "source": "latest_body_state" if raw else "no_observation",
        }
    else:
        for name, handle_value in bindings.items():
            handle = str(handle_value)
            key = handle or str(name)
            # The current Body snapshot is the authoritative source for the
            # bot/body binding. Other handles require an object-specific read.
            is_body = (
                str(name) in {"bot", "body"}
                or handle in {"bot", "body"}
                or handle == raw.get("bot")
            )
            if is_body and raw:
                facts[key] = {"observed": True, "values": raw, "source": "latest_body_state"}
            elif is_body:
                facts[key] = {"observed": False, "values": None, "source": "no_observation"}
                unknown_fields.append({"object": key, "reason": "no_observation"})
            else:
                entity_read = observations.get("nearby_entities")
                entities = entity_read.get("entities") if isinstance(entity_read, dict) else None
                complete = bool(entity_read.get("complete")) if isinstance(entity_read, dict) else False
                if isinstance(entities, list):
                    matched = None
                    for entity in entities:
                        if not isinstance(entity, dict):
                            continue
                        identifiers = {str(value) for value in (
                            entity.get("id"), entity.get("uuid"), entity.get("name")
                        ) if value is not None}
                        if handle in identifiers or str(name) in identifiers:
                            matched = entity
                            break
                    sampled_at = entity_read.get("sampled_at_ms")
                    values = {
                        "present": matched is not None,
                        "entity": copy.deepcopy(matched),
                        "entities_complete": complete,
                        "radius": entity_read.get("radius"),
                    }
                    fact = {
                        "observed": complete or matched is not None,
                        "values": values,
                        "source": "read_nearby_entities",
                    }
                    if sampled_at is not None:
                        fact["sampled_at_ms"] = sampled_at
                        try:
                            fact["sample_age_s"] = max(0.0, time.time() - float(sampled_at) / 1000.0)
                        except (TypeError, ValueError):
                            pass
                    facts[key] = fact
                    if not fact["observed"]:
                        unknown_fields.append({"object": key, "reason": "object_not_read"})
                else:
                    facts[key] = {"observed": False, "values": None, "source": "object_not_read"}
                    unknown_fields.append({"object": key, "reason": "no_observation"})
    # Each bound object owns its complete facts. Do not duplicate the same
    # body snapshot under a generic ``background`` key.
    return facts, unknown_fields


def _obligation_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _obligation_value(child)
            for key, child in value.items()
            if key not in {"intent", "_runtime", "next_call"}
        }
    if isinstance(value, list):
        return [_obligation_value(child) for child in value]
    return value


def _node_signature(node: dict[str, Any]) -> str:
    value = _obligation_value(node)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _legacy_node_key(signature: str, occurrence: int) -> str:
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
    return f"legacy:{digest}:{occurrence}"


def _resolve_ref(value: Any, *, results: dict[str, Any], item: Any, state: dict[str, Any]) -> Any:
    if not isinstance(value, dict) or set(value) != {"ref"}:
        if isinstance(value, dict):
            return {key: _resolve_ref(child, results=results, item=item, state=state) for key, child in value.items()}
        if isinstance(value, list):
            return [_resolve_ref(child, results=results, item=item, state=state) for child in value]
        return value
    ref = value.get("ref")
    if not isinstance(ref, str) or not ref:
        raise WorkPlanError("reference must be a non-empty string")
    if ref == "item":
        return item
    if ref.startswith("item."):
        current = item
        for part in ref.split(".")[1:]:
            if not isinstance(current, dict) or part not in current:
                raise WorkPlanError(f"unresolved reference: {ref}")
            current = current[part]
        return current
    roots: dict[str, Any] = {"state": state, "result": results, **results}
    current: Any = roots
    for part in ref.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise WorkPlanError(f"unresolved reference: {ref}")
    return current


@dataclass
class NodeRecord:
    status: NodeStatus = "pending"
    result: dict[str, Any] | None = None
    delivered: Any = None
    remaining: Any = None
    blocked: dict[str, Any] | None = None
    condition: dict[str, Any] | None = None
    signature: str | None = None
    started_at: float | None = None
    updated_at: float = field(default_factory=time.time)
    decision: dict[str, Any] | None = None
    decision_facts: dict[str, Any] | None = None
    decision_pending: bool = False
    decision_reads: int = 0
    unknown_streak: int = 0
    repeat_count: int = 0
    choice_id: str | None = None

    def payload(self) -> dict[str, Any]:
        value: dict[str, Any] = {"status": self.status, "updated_at": self.updated_at}
        if self.started_at is not None:
            value["started_at"] = self.started_at
        if self.result is not None:
            value["result"] = copy.deepcopy(self.result)
        if self.delivered is not None:
            value["delivered"] = copy.deepcopy(self.delivered)
        if self.remaining is not None:
            value["remaining"] = copy.deepcopy(self.remaining)
        if self.blocked is not None:
            value["blocked"] = copy.deepcopy(self.blocked)
        if self.condition is not None:
            value["condition"] = copy.deepcopy(self.condition)
        if self.signature is not None:
            value["signature"] = self.signature
        if self.decision is not None:
            value["decision"] = copy.deepcopy(self.decision)
        if self.decision_facts is not None:
            value["decision_facts"] = copy.deepcopy(self.decision_facts)
        if self.decision_pending:
            value["decision_pending"] = True
        if self.decision_reads:
            value["decision_reads"] = self.decision_reads
        if self.unknown_streak:
            value["unknown_streak"] = self.unknown_streak
        if self.repeat_count:
            value["repeat_count"] = self.repeat_count
        if self.choice_id:
            value["choice_id"] = self.choice_id
        return value


@dataclass
class WorkRunResult:
    status: Literal["completed", "waiting", "blocked", "cancelled"]
    tree: dict[str, Any]
    results: dict[str, Any]
    facts: dict[str, Any] = field(default_factory=dict)


class WorkPlanExecutor:
    def __init__(
        self,
        plan: dict[str, Any],
        *,
        call: WorkCall,
        state: WorkState | None = None,
        decision: WorkDecision | None = None,
        choice_provider: WorkChoiceProvider | None = None,
        observe: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        idle_supervise: WorkIdleSupervisor | None = None,
        emit: Callable[..., None] | None = None,
        records: dict[str, NodeRecord] | None = None,
    ) -> None:
        validate_plan(plan)
        self.plan = copy.deepcopy(plan)
        self.call = call
        self.state = state or (lambda: _async_empty_state())
        self.decision = decision or DecisionClient()
        self.choice_provider = choice_provider
        self.observe = observe
        self.idle_supervise = idle_supervise
        self.emit = emit or (lambda *_args, **_kwargs: None)
        # An empty mapping is a meaningful caller-owned revision store.  Do
        # not replace it with a new mapping or completed nodes would be lost
        # between the first run and an Agent revision.
        self.records = records if records is not None else {}
        self.results: dict[str, Any] = {}
        self._cancelled = False
        self._deadline = time.monotonic() + float(plan["timeout_s"])
        self._legacy_occurrences: dict[str, int] = {}
        self._path_keys: dict[str, str] = {}
        self._repeat_history: dict[str, list[str]] = {}
        self._call_histories: dict[str, list[dict[str, Any]]] = {}
        self._call_history_counts: dict[str, int] = {}

    async def run(self) -> WorkRunResult:
        self._legacy_occurrences.clear()
        self._path_keys.clear()
        self._repeat_history.clear()
        self._call_histories.clear()
        self._call_history_counts.clear()
        try:
            status = await self._execute(self.plan["work"], "work", [])
        except asyncio.CancelledError:
            status = "cancelled"
        except WorkPlanError as exc:
            self.emit("work_plan_blocked", reason=str(exc))
            status = "blocked"
        public_status = "completed" if status == "done" else status
        tree = self.annotated_tree()
        facts = {"status": public_status, "results": copy.deepcopy(self.results)}
        return WorkRunResult(public_status, tree, copy.deepcopy(self.results), facts)

    async def _execute(
        self, node: dict[str, Any], path: str, intents: list[str], *, item: Any = None,
        invocation: int = 0,
    ) -> str:
        signature = _node_signature(node)
        explicit_id = node.get("id")
        if isinstance(explicit_id, str) and explicit_id:
            node_key = explicit_id if invocation == 0 else f"{explicit_id}__repeat_{invocation}"
        else:
            occurrence = self._legacy_occurrences.get(signature, 0)
            self._legacy_occurrences[signature] = occurrence + 1
            node_key = _legacy_node_key(signature, occurrence)
        self._path_keys[path] = node_key
        record = self.records.get(node_key)
        if record is None:
            record = NodeRecord(signature=signature)
            self.records[node_key] = record
        elif record.signature != signature:
            previous = record
            # A completed physical call is evidence of an already delivered
            # obligation. Rewording it is harmless, but changing its tool or
            # arguments must never replay the write. The Agent must submit a
            # new node for the remaining obligation.
            if (
                previous.status == "done"
                and node.get("kind") == "call"
                # A physical write records the delivered effect. Pure reads
                # and legacy test doubles may have no such marker and remain
                # safe to re-evaluate under the same id.
                and previous.delivered is not None
            ):
                record = NodeRecord(
                    status="blocked", signature=signature,
                    blocked={
                        "reason": "completed_obligation_changed",
                        "previous_result": copy.deepcopy(previous.result),
                    },
                )
            else:
                record = NodeRecord(signature=signature)
            self.records[node_key] = record
            if record.status == "blocked":
                return "blocked"
        if record.status == "done":
            if record.result is not None:
                self.results[node_key] = copy.deepcopy(record.result)
            return "done"
        record.status = "active"
        if record.started_at is None:
            record.started_at = time.monotonic()
        record.updated_at = time.time()
        chain = intents + ([str(node["intent"])] if node.get("intent") else [])
        if node.get("when") is not None:
            condition_status, facts = await self._condition(node["when"], chain, item=item)
            record.condition = facts
            if condition_status != "satisfied":
                if time.monotonic() >= self._deadline:
                    record.blocked = {"reason": "condition_timeout", "facts": facts}
                    record.status = "blocked"
                    return "blocked"
                if node.get("timeout_s") is not None and record.started_at is not None:
                    if time.monotonic() >= record.started_at + float(node["timeout_s"]):
                        record.blocked = {"reason": "node_timeout", "facts": facts}
                        record.status = "blocked"
                        return "blocked"
                return self._wait_or_block(record, condition_status, facts, node, "when")
        kind = node.get("kind")
        if kind == "call":
            while True:
                if record.decision_pending and record.result is not None:
                    status = await self._decide_call(node, node_key, chain, record, item=item)
                else:
                    call_state = await self.state()
                    arguments = _resolve_ref(
                        node.get("arguments", {}), results=self.results, item=item, state=call_state,
                    )
                    result = await self.call(
                        str(node["tool"]), arguments, call_id=node_key,
                        timeout_s=node.get("timeout_s"),
                    )
                    execution = result.get("_execution") if isinstance(result, dict) else None
                    public_result = dict(result)
                    public_result.pop("_execution", None)
                    private_handoff = public_result.pop("_supervision_handoff", None)
                    record.result = copy.deepcopy(public_result)
                    self.results[node_key] = copy.deepcopy(public_result)
                    if isinstance(execution, dict) and execution.get("delivered") is True:
                        record.delivered = {
                            "tool": execution.get("tool", node.get("tool")),
                            "execution_channel": True,
                        }
                    try:
                        decision_state = await self.state()
                    except Exception:
                        decision_state = call_state
                    record.decision_facts = {
                        "tool_result": copy.deepcopy(public_result),
                        "tool_arguments": copy.deepcopy(arguments),
                        "body_state": _state_payload(decision_state),
                        "execution": {key: copy.deepcopy(value) for key, value in execution.items()
                                       if key in {"tool", "phase", "delivered", "reason", "normal_work"}}
                                       if isinstance(execution, dict) else {},
                        "reads": [],
                    }
                    base_key = str(node.get("id") or node_key.split("__repeat_", 1)[0])
                    history = self._call_histories.setdefault(base_key, [])
                    history_count = self._call_history_counts.get(base_key, 0) + 1
                    self._call_history_counts[base_key] = history_count
                    body_facts = record.decision_facts["body_state"]
                    raw_body = _state_payload(decision_state)
                    history.append({
                        "reason": public_result.get("reason"),
                        "result": _decision_result_facts(public_result),
                        "body": {
                            "position": raw_body.get("position"),
                            "inventory": raw_body.get("inventory"),
                        },
                    })
                    # The question carries the five most recent raw results;
                    # the count separately tells Decision how many earlier
                    # attempts are omitted.
                    if len(history) > 5:
                        self._call_histories[base_key] = history[-5:]
                        history = self._call_histories[base_key]
                    record.decision_facts["history"] = copy.deepcopy(history)
                    record.decision_facts["history_count"] = history_count
                    record.decision_facts["history_earlier"] = max(0, history_count - len(history))
                    handoff = private_handoff
                    if handoff is None:
                        handoff = public_result.get("metrics", {}).get("direct_call_handoff") if isinstance(public_result.get("metrics"), dict) else None
                    handoff_choice = handoff.get("facts", {}).get("decision", {}).get("choice") if isinstance(handoff, dict) else None
                    if handoff_choice in {"step_done", "next_step", "call_agent"}:
                        status = self._apply_call_choice(node, node_key, chain, record, handoff_choice, item=item)
                    else:
                        status = await self._decide_call(node, node_key, chain, record, item=item)
                if status != "repeat":
                    return status
                # Repeat is an iterative call re-entry. Keep the base record's
                # total and only the newest few invocation records.
                base_key = str(node.get("id") or node_key.split("__repeat_", 1)[0])
                base_record = self.records.get(base_key, record)
                base_record.repeat_count += 1
                invocation += 1
                next_key = f"{base_key}__repeat_{invocation}"
                self._repeat_history.setdefault(base_key, []).append(next_key)
                for old_key in self._repeat_history[base_key][:-3]:
                    self.records.pop(old_key, None)
                    self.results.pop(old_key, None)
                node_key = next_key
                self._path_keys[f"{path}.repeat[{invocation}]"] = node_key
                record = NodeRecord(signature=signature)
                self.records[node_key] = record
                continue
        if kind == "sequence":
            steps = node.get("steps", [])
            for index, child in enumerate(steps):
                if not isinstance(child, dict):
                    continue
                dispatch = copy.deepcopy(child)
                if index + 1 < len(steps) and isinstance(steps[index + 1], dict):
                    dispatch["next_call"] = self._first_call_description(steps[index + 1])
                child_status = await self._execute(dispatch, f"{path}.steps[{index}]", chain, item=item)
                if child_status != "done":
                    return child_status
            record.status = "done"
            return "done"
        if kind == "batch":
            items = node.get("items", [])
            if node.get("order", "listed") == "adaptive":
                return await self._adaptive_batch(node, node_key, path, chain, items)
            for index, value in enumerate(items):
                child = copy.deepcopy(node["body"])
                if isinstance(child.get("id"), str):
                    child["id"] = f"{child['id']}__item_{index}"
                child_status = await self._execute(child, f"{path}.item[{index}]", chain, item=value)
                if child_status != "done":
                    return child_status
            record.status = "done"
            return "done"
        if kind == "choice":
            option = None
            if record.choice_id:
                option = next((candidate for candidate in node.get("options", [])
                               if isinstance(candidate, dict) and candidate.get("id") == record.choice_id), None)
                if option is None:
                    record.status = "blocked"
                    record.blocked = {"reason": "choice_option_removed", "choice": record.choice_id}
                    return "blocked"
            else:
                option = await self._choose(node, chain, item=item)
            if option is None:
                record.status = "blocked"
                return "blocked"
            record.choice_id = str(option.get("id"))
            child_status = await self._execute(option["work"], f"{path}.options.{option['id']}", chain, item=item)
            if child_status == "done":
                record.status = "done"
            return child_status
        if kind == "wait":
            return await self._wait(node, node_key, chain, item=item)
        raise WorkPlanError(f"unsupported node kind at {path}: {kind!r}")

    @staticmethod
    def _first_call_description(node: dict[str, Any]) -> str:
        kind = node.get("kind")
        if kind == "call":
            return rq.call_text(
                str(node.get("tool", "unknown")),
                node.get("arguments") if isinstance(node.get("arguments"), dict) else {},
            )
        if kind == "sequence":
            for child in node.get("steps", []):
                if isinstance(child, dict):
                    value = WorkPlanExecutor._first_call_description(child)
                    if value:
                        return value
        if kind == "batch" and isinstance(node.get("body"), dict):
            return WorkPlanExecutor._first_call_description(node["body"])
        if kind == "choice":
            for option in node.get("options", []):
                if isinstance(option, dict) and isinstance(option.get("work"), dict):
                    value = WorkPlanExecutor._first_call_description(option["work"])
                    if value:
                        return value
        return ""

    @staticmethod
    def _repeat_text(value: Any) -> str:
        if isinstance(value, str):
            return value[:800]
        if isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str):
                return text[:800]
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)[:800]

    def _call_options(
        self, node: dict[str, Any], *, include_inspect: bool = False,
        plan: dict[str, Any] | None = None, normal_work: str = "",
        result: Mapping[str, Any] | None = None, facts: Mapping[str, Any] | None = None,
    ) -> dict[str, str]:
        plan = plan or {}
        repeat = node.get("repeat")
        repeat_note = ""
        if isinstance(repeat, dict):
            again = self._repeat_text(repeat.get("again_when", ""))
            until = self._repeat_text(repeat.get("until", ""))
            repeat_note = f"The Agent said to repeat when: {again}; and to stop when: {until}."
        options = rq.frame_options(
            tool=str(node.get("tool", "unknown")),
            params=node.get("arguments") if isinstance(node.get("arguments"), dict) else {},
            intent=str(plan.get("intent") or node.get("intent") or ""),
            facts=facts or {}, running=False, result=result,
            next_call=str(plan.get("next_call") or node.get("next_call") or ""),
            repeat_note=repeat_note,
        )
        # Between two plan calls nothing is running to interrupt; the next
        # running frame offers the protective actions again.
        return {key: text for key, text in options.items() if key not in rq.PROTECTIVE}

    def _call_plan(self, node: dict[str, Any], intents: list[str]) -> dict[str, Any]:
        intent = str(node.get("intent") or node.get("description") or (intents[-1] if intents else ""))[:800]
        current_step = intent or (intents[-1] if intents else "")
        return {
            "goal": str(self.plan.get("goal") or "")[:800],
            # The top-level current_step is a mutable Agent note and may lag
            # behind the node being dispatched.  Use the tree position only.
            "current_step": current_step[:800],
            "node_description": intent or f"call {node.get('tool', 'unknown')}",
            "intent": intent,
            "intent_chain": [str(value)[:400] for value in intents[-8:]],
        }

    def _call_question(
        self, node: dict[str, Any], node_key: str, intents: list[str], record: NodeRecord,
    ) -> dict[str, Any]:
        """The result question for one plan call; wording lives in reflex_question."""
        facts = record.decision_facts or {}
        plan = self._call_plan(node, intents)
        body = facts.get("body_state") if isinstance(facts.get("body_state"), dict) else {}
        history = facts.get("history") if isinstance(facts.get("history"), list) else []
        state = rq.frame_state(
            goal=str(plan.get("goal") or ""),
            intent=str(plan.get("intent") or ""),
            tool=str(node.get("tool", "unknown")),
            params=facts.get("tool_arguments", node.get("arguments", {})),
            facts=body, running=False, result=facts.get("tool_result"),
            earlier_attempts=rq.attempt_lines(history, int(facts.get("history_earlier", 0) or 0)),
        )
        options = self._call_options(node, result=facts.get("tool_result"), plan=plan, facts=body)
        payload = {"state": state, "questions": rq.frame_questions(options)}
        payload["state"]["request_chars"] = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))
        return payload


    @staticmethod
    def _decision_choice(answer: Any, options: dict[str, str],
                         questions: dict[str, Any] | None = None) -> str:
        raw = getattr(answer, "raw", None)
        if questions is not None and isinstance(raw, dict) and isinstance(raw.get("answers"), dict):
            choice = rq.frame_choice(raw, questions)
        else:
            choice = str(getattr(answer, "choice", answer if isinstance(answer, str) else "unknown"))
        # "step_done" is the earlier name of "next_step"; local Decision seams still use it.
        if choice == "step_done":
            choice = "next_step"
        return choice if choice in options else "unknown"

    async def _decide_call(
        self, node: dict[str, Any], node_key: str, intents: list[str], record: NodeRecord,
        *, item: Any = None,
    ) -> str:
        facts = record.decision_facts or {}
        options = self._call_options(
            node,
            result=facts.get("tool_result") if isinstance(facts, dict) else None,
            # WorkPlan always has a state callback that can provide a bounded
            # fact read.  Direct-call supervision applies its stricter
            # registered-reader gate in its own option compiler.
            include_inspect=True,
            plan=self._call_plan(node, intents),
            normal_work=str(facts.get("execution", {}).get("normal_work") or ""),
        )
        while True:
            payload = self._call_question(node, node_key, intents, record)
            options = dict(payload["questions"]["q"]["criteria"])
            purpose = "call_inspect" if record.decision_reads else "call_result"
            decision_started = time.monotonic()
            self.emit(
                "work_decision_request", purpose=purpose, node_id=node_key,
                request_chars=int(payload["state"].get("request_chars", 0)), request=copy.deepcopy(payload),
            )
            try:
                choice = rq.only_option(payload["questions"])
                if choice is None:
                    answer = await self.decision.ask(payload, priority=DecisionPriority.PLAN)
                    choice = self._decision_choice(answer, options, payload["questions"])
            except Exception:
                choice = "unknown"
            elapsed = time.monotonic() - decision_started
            self.emit(
                "work_decision_result", purpose=purpose, node_id=node_key, choice=choice,
                unknown=choice == "unknown", decision_ms=round(elapsed * 1000.0, 3),
            )
            record.decision = {"choice": choice, "options": list(options), "reads": record.decision_reads}
            if choice == "inspect":
                if record.decision_reads >= 4:
                    choice = "unknown"
                    record.decision["choice"] = choice
                else:
                    record.decision_reads += 1
                    try:
                        read = await self.state()
                    except Exception:
                        read = {}
                    if not isinstance(record.decision_facts, dict):
                        record.decision_facts = {}
                    record.decision_facts.setdefault("reads", []).append(_state_payload(read))
                    continue
            if choice == "unknown":
                record.unknown_streak += 1
                if record.unknown_streak >= MAX_NODE_UNKNOWN_STREAK:
                    return self._apply_call_choice(
                        node, node_key, intents, record, "call_agent", item=item,
                        block_reason="decision_unknown_limit",
                    )
                record.decision_pending = True
                record.status = "waiting"
                if self.idle_supervise is not None:
                    try:
                        idle = await self.idle_supervise(
                            str(node.get("tool", "unknown")), call_id=node_key,
                            plan_context=self._call_plan(node, intents), options=options,
                            facts=copy.deepcopy(record.decision_facts or {}),
                        )
                    except TypeError as exc:
                        # Keep the pre-R2 test seam usable while all production
                        # supervisors receive the unified result facts.
                        if "facts" not in str(exc):
                            raise
                        idle = await self.idle_supervise(
                            str(node.get("tool", "unknown")), call_id=node_key,
                            plan_context=self._call_plan(node, intents), options=options,
                        )
                    idle_choice = idle.get("choice") if isinstance(idle, dict) else None
                    if idle_choice in options:
                        choice = idle_choice
                        if choice == "call_agent":
                            return self._apply_call_choice(
                                node, node_key, intents, record, choice, item=item,
                                block_reason=str((idle or {}).get("reason") or "decision_unknown_limit"),
                            )
                        if choice == "inspect":
                            continue
                    else:
                        return "waiting"
                else:
                    return "waiting"
            record.unknown_streak = 0
            if choice == "repeat":
                record.decision_pending = False
                record.status = "active"
                return "repeat"
            return self._apply_call_choice(node, node_key, intents, record, choice, item=item)

    def _apply_call_choice(
        self, node: dict[str, Any], node_key: str, intents: list[str], record: NodeRecord,
        choice: str, *, item: Any = None, block_reason: str = "decision_call_agent",
    ) -> str:
        record.decision_pending = False
        if choice in {"step_done", "next_step"}:
            record.status = "done"
            return "done"
        if choice == "call_agent":
            record.status = "blocked"
            record.blocked = {"reason": block_reason, "facts": copy.deepcopy(record.decision_facts or {})}
            return "blocked"
        if choice == "inspect":
            record.decision_pending = True
            record.status = "waiting"
            return "waiting"
        if choice == "repeat":
            record.status = "active"
            return "repeat"
        record.decision_pending = True
        record.status = "waiting"
        return "waiting"

    async def _condition(self, condition: dict[str, Any], intents: list[str], *, item: Any = None) -> tuple[ConditionStatus, dict[str, Any]]:
        state = await self.state()
        observations = await self.observe(condition) if self.observe is not None else {}
        compiled_facts, unknown_fields = _condition_facts(state, condition, observations)
        requirements = condition.get("requires") or [condition]
        statuses: list[str] = []
        evidence: list[Any] = []
        for requirement in requirements:
            about = requirement.get("about") if isinstance(requirement, dict) else None
            text_value = requirement.get("text") if isinstance(requirement, dict) else str(requirement)
            payload = {
                "state": {
                    "protocol": "minebot-decision-context/0.3.1",
                    "intent_chain": intents,
                    "condition": text_value,
                    "about": about,
                    "facts": compiled_facts,
                    "unknown_fields": unknown_fields,
                    "field_vocabulary": WORK_FIELD_VOCABULARY,
                    "evidence_scope": "Only supplied facts are known; omitted facts are unknown.",
                },
                "questions": {"q": {"type": "choice", "instructions": "Choose satisfied, pending, or unknown.",
                    "criteria": {"satisfied": "The condition is supported by the supplied facts.",
                                 "pending": "The condition is not yet true, but the facts are sufficient to say so.",
                                 "unknown": "The supplied facts cannot establish the condition."}}},
            }
            statements = requirement.get("statements") if isinstance(requirement, dict) else None
            if statements is None and isinstance(condition, dict):
                statements = condition.get("statements")
            if isinstance(statements, dict):
                payload["questions"]["q"]["criteria"].update({
                    "satisfied": str(statements.get("satisfied", payload["questions"]["q"]["criteria"]["satisfied"])),
                    "pending": str(statements.get("pending", payload["questions"]["q"]["criteria"]["pending"])),
                })
            answer = await self.decision.ask(payload, priority=DecisionPriority.PLAN)
            choice = getattr(answer, "choice", answer if isinstance(answer, str) else "unknown")
            normalized = choice if choice in {"satisfied", "pending", "unknown"} else "unknown"
            statuses.append(normalized)
            evidence.append({"about": about, "text": text_value, "choice": normalized, "response": getattr(answer, "raw", None)})
            if normalized == "pending":
                return "pending", {"requirements": evidence}
        result: ConditionStatus = "satisfied" if statuses and all(value == "satisfied" for value in statuses) else "unknown"
        return result, {"requirements": evidence}

    def _wait_or_block(self, record: NodeRecord, status: str, facts: dict[str, Any], node: dict[str, Any], phase: str) -> str:
        record.condition = {"phase": phase, **facts}
        record.status = "waiting" if status == "pending" else "blocked"
        record.blocked = None if status == "pending" else {"reason": "condition_unknown", "facts": facts}
        return record.status

    async def _wait(self, node: dict[str, Any], node_key: str, intents: list[str], *, item: Any = None) -> str:
        record = self.records[node_key]
        deadline = (record.started_at or time.monotonic()) + float(node["timeout_s"])
        if time.monotonic() > min(deadline, self._deadline):
            record.status = "blocked"
            record.blocked = {"reason": "wait_timeout", "last_condition": record.condition}
            return "blocked"
        status, facts = await self._condition(node["condition"], intents, item=item)
        record.condition = facts
        if status == "satisfied":
            record.status = "done"
            return "done"
        # The manager keeps the same executor alive and re-enters this node;
        # direct callers can inspect the waiting tree without a hidden sleep.
        record.status = "waiting"
        return "waiting"

    async def _choose(self, node: dict[str, Any], intents: list[str], *, item: Any = None) -> dict[str, Any] | None:
        state = await self.state()
        options = node.get("options") or []
        if node.get("source", "author") == "runtime":
            if self.choice_provider is None:
                return None
            provided = self.choice_provider(node, intents=intents, state=state, item=item)
            if inspect.isawaitable(provided):
                provided = await provided
            if not isinstance(provided, list):
                return None
            options = provided
        payload = {"state": {"protocol": "minebot-decision-context/0.3.1", "intent_chain": intents,
                             "facts": state},
                   "questions": {"q": {"type": "choice", "instructions": "Choose one option by id.",
                                         "criteria": {str(option["id"]): str(option.get("intent", option["id"])) for option in options}}}}
        answer = await self.decision.ask(payload, priority=DecisionPriority.PLAN)
        choice = getattr(answer, "choice", answer if isinstance(answer, str) else "unknown")
        for option in options:
            if option["id"] == choice:
                return option
        return None

    async def _adaptive_batch(self, node: dict[str, Any], node_key: str, path: str, intents: list[str], items: list[Any]) -> str:
        remaining = list(items)
        while remaining:
            payload = {"state": {"protocol": "minebot-decision-context/0.3.1", "intent_chain": intents,
                                 "facts": await self.state(), "items": remaining},
                       "questions": {"q": {"type": "choice", "instructions": "Choose the next item index.",
                                             "criteria": {str(index): str(value) for index, value in enumerate(remaining)}}}}
            answer = await self.decision.ask(payload, priority=DecisionPriority.PLAN)
            choice = getattr(answer, "choice", answer if isinstance(answer, str) else "unknown")
            try:
                index = int(choice)
                value = remaining.pop(index)
            except (ValueError, TypeError, IndexError):
                self.records[node_key].status = "blocked"
                self.records[node_key].blocked = {"reason": "choice_unknown", "choice": choice}
                return "blocked"
            child = copy.deepcopy(node["body"])
            if isinstance(child.get("id"), str):
                child["id"] = f"{child['id']}__item_{len(items)-len(remaining)-1}"
            child_status = await self._execute(child, f"{path}.item[{len(items)-len(remaining)-1}]", intents, item=value)
            if child_status != "done":
                return child_status
        self.records[node_key].status = "done"
        return "done"

    def annotated_tree(self) -> dict[str, Any]:
        tree = copy.deepcopy(self.plan)
        def annotate(node: dict[str, Any], path: str) -> None:
            key = self._path_keys.get(path)
            if key is None:
                signature = _node_signature(node)
                explicit_id = node.get("id")
                if isinstance(explicit_id, str) and explicit_id:
                    key = explicit_id
                else:
                    key = _node_id(node, path)
            node["_runtime"] = self.records.get(key, NodeRecord()).payload()
            if node.get("kind") == "sequence":
                for index, child in enumerate(node.get("steps", [])):
                    annotate(child, f"{path}.steps[{index}]")
            elif node.get("kind") == "batch":
                annotate(node["body"], f"{path}.body")
                batch_runtime = node["_runtime"]
                instances: dict[str, Any] = {}
                body_id = node["body"].get("id")
                if isinstance(body_id, str):
                    for index, _item in enumerate(node.get("items", [])):
                        key = f"{body_id}__item_{index}"
                        if key in self.records:
                            instances[str(index)] = self.records[key].payload()
                if instances:
                    batch_runtime["instances"] = instances
            elif node.get("kind") == "choice":
                for option in node.get("options", []):
                    annotate(option["work"], f"{path}.options.{option['id']}")
        annotate(tree["work"], "work")
        return tree


async def _async_empty_state() -> dict[str, Any]:
    return {}


__all__ = [
    "ConditionStatus", "NodeRecord", "NodeStatus", "WorkCall", "WorkPlanError",
    "WorkChoiceProvider", "WorkIdleSupervisor", "WorkPlanExecutor", "WorkRunResult", "validate_plan",
]
