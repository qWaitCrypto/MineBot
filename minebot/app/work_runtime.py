"""Interpreter for the candidate ``minebot-work/0.2`` language.

This module is intentionally independent of the OpenAI SDK.  The slow Agent
produces a JSON tree; this runtime validates and executes it through injected
call, state and Decision seams.  That keeps the language testable with a fake
Body and prevents model-authored ``_runtime`` fields from becoming truth.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol

from jsonschema import Draft202012Validator

from minebot.app.decision_client import DecisionClient, DecisionPriority

NodeStatus = Literal["pending", "active", "waiting", "blocked", "done", "cancelled"]
ConditionStatus = Literal["satisfied", "pending", "unknown"]


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


def _schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "docs" / "design-docs" / "agent-work-language-v0.2.schema.json"


def validate_plan(plan: dict[str, Any]) -> None:
    """Validate the candidate schema and reject features this slice defers."""

    if not isinstance(plan, dict):
        raise WorkPlanError("plan must be an object")
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
        if "repeat" in node or node.get("interrupt_when") is not None:
            raise WorkPlanError(f"unsupported_in_slice2 at {path}: repeat/interrupt_when")
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
    started_at: float | None = None
    updated_at: float = field(default_factory=time.time)

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
        emit: Callable[..., None] | None = None,
        records: dict[str, NodeRecord] | None = None,
    ) -> None:
        validate_plan(plan)
        self.plan = copy.deepcopy(plan)
        self.call = call
        self.state = state or (lambda: _async_empty_state())
        self.decision = decision or DecisionClient()
        self.choice_provider = choice_provider
        self.emit = emit or (lambda *_args, **_kwargs: None)
        # An empty mapping is a meaningful caller-owned revision store.  Do
        # not replace it with a new mapping or completed nodes would be lost
        # between the first run and an Agent revision.
        self.records = records if records is not None else {}
        self.results: dict[str, Any] = {}
        self._cancelled = False
        self._deadline = time.monotonic() + float(plan["timeout_s"])

    async def run(self) -> WorkRunResult:
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

    async def _execute(self, node: dict[str, Any], path: str, intents: list[str], *, item: Any = None) -> str:
        node_key = _node_id(node, path)
        record = self.records.setdefault(node_key, NodeRecord())
        if record.status == "done":
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
                return self._wait_or_block(record, condition_status, facts, node, "when")
        kind = node.get("kind")
        if kind == "call":
            result = await self.call(
                str(node["tool"]),
                _resolve_ref(node.get("arguments", {}), results=self.results, item=item, state=await self.state()),
                call_id=node_key,
                timeout_s=node.get("timeout_s"),
            )
            record.result = copy.deepcopy(result)
            self.results[node_key] = copy.deepcopy(result)
            if result.get("success") is True:
                record.status = "done"
                record.delivered = result.get("metrics", {}).get("delivered") if isinstance(result.get("metrics"), dict) else None
                return "done"
            blocked = {"reason": result.get("reason", "call_failed"), "result": copy.deepcopy(result)}
            record.blocked = blocked
            record.status = "blocked"
            return "blocked"
        if kind == "sequence":
            for index, child in enumerate(node.get("steps", [])):
                child_status = await self._execute(child, f"{path}.steps[{index}]", chain, item=item)
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
            option = await self._choose(node, chain, item=item)
            if option is None:
                record.status = "blocked"
                return "blocked"
            child_status = await self._execute(option["work"], f"{path}.options.{option['id']}", chain, item=item)
            if child_status == "done":
                record.status = "done"
            return child_status
        if kind == "wait":
            return await self._wait(node, node_key, chain, item=item)
        raise WorkPlanError(f"unsupported node kind at {path}: {kind!r}")

    async def _condition(self, condition: dict[str, Any], intents: list[str], *, item: Any = None) -> tuple[ConditionStatus, dict[str, Any]]:
        state = await self.state()
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
                    "facts": state,
                    "evidence_scope": "Only supplied facts are known; omitted facts are unknown.",
                },
                "questions": {"q": {"type": "choice", "instructions": "Choose satisfied, pending, or unknown.",
                    "criteria": {"satisfied": "The condition is supported by the supplied facts.",
                                 "pending": "The condition is not yet true, but the facts are sufficient to say so.",
                                 "unknown": "The supplied facts cannot establish the condition."}}},
            }
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
        while time.monotonic() <= deadline and time.monotonic() <= self._deadline:
            status, facts = await self._condition(node["condition"], intents, item=item)
            record.condition = facts
            if status == "satisfied":
                record.status = "done"
                return "done"
            if status == "unknown":
                record.status = "waiting"
            else:
                record.status = "waiting"
            return "waiting"
        record.status = "blocked"
        record.blocked = {"reason": "wait_timeout"}
        return "blocked"

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
    "WorkChoiceProvider", "WorkPlanExecutor", "WorkRunResult", "validate_plan",
]
