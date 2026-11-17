"""Agent-facing reflex management tools and activity state."""

from __future__ import annotations

import os

import asyncio
import copy
import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from minebot.app.decision_client import DecisionClient
from minebot.app.projection import answer_value
from minebot.app.life_profile import (
    LifeProfileError,
    LifeProfileUnreadable,
    SkillMountError,
    project_plan,
    project_skills,
)
from minebot.app.skill_mounts import merged_mounts, plan_holder
from minebot.app.work_runtime import NodeRecord, WorkPlanError, WorkPlanExecutor, WorkRunResult, validate_plan
from minebot.brain.progress import material_progress_evidence
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar, execute_tool
from minebot.contract import ToolResult


def _execute_plan_call(tool: Any, arguments: Any, context: Any) -> Any:
    """Run one plan call without feeding the Agent's progress counters.

    Those counters stop an Agent that repeats itself.  A plan's repeats are
    Decision's choice; the counters hear of the plan once, when it ends.
    """
    authority = getattr(context, "authority", None)
    if authority is None or not callable(getattr(authority, "capture_steps", None)):
        return execute_tool(tool, arguments, context)
    with authority.capture_steps():
        return execute_tool(tool, arguments, context)


def body_now(state: Any) -> dict[str, Any]:
    """Where the Body is and what it holds, from one state read (s7-later-20261029.md §8 D4)."""
    if not isinstance(state, dict):
        return {}
    out: dict[str, Any] = {}
    position = state.get("position", state.get("pos"))
    if (isinstance(position, (list, tuple)) and len(position) == 3
            and all(isinstance(part, (int, float)) for part in position)):
        out["position"] = [round(float(part), 1) for part in position]
    if isinstance(state.get("dimension"), str) and state["dimension"]:
        out["dimension"] = state["dimension"]
    counts = next((state[key] for key in ("inventory", "inventory_counts") if isinstance(state.get(key), dict)), None)
    if counts is not None:
        out["inventory_counts"] = {str(name): int(count) for name, count in counts.items()
                                   if isinstance(count, (int, float))}
    return out


def inventory_change(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    """Signed counts of the items that changed; empty when nothing did."""
    change: dict[str, int] = {}
    for name in sorted(set(before) | set(after)):
        delta = int(after.get(name) or 0) - int(before.get(name) or 0)
        if delta:
            change[str(name)] = delta
    return change


def step_words(node: Any, tool: Any, result: Any) -> dict[str, Any]:
    """One step as the ladder names it: its node, its call and the call's own reason."""
    words: dict[str, Any] = {"node": str(node)}
    if tool:
        words["tool"] = str(tool)
    if isinstance(result, dict) and result.get("reason"):
        words["result"] = str(result["reason"])[:120]
    return words


def stopped_at(tree: Any) -> dict[str, Any] | None:
    """The step a run stopped on (the deepest one), and why it stopped there."""
    found: list[dict[str, Any]] = []

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        runtime = node.get("_runtime") if isinstance(node.get("_runtime"), dict) else {}
        status = runtime.get("status")
        stop = runtime.get(status) if status in {"blocked", "needs_agent"} else None
        if isinstance(stop, dict):
            words = step_words(node.get("id") or path, node.get("tool"), runtime.get("result"))
            words["why"] = str(stop.get("reason") or status)
            found.append(words)
        kind = node.get("kind")
        if kind == "sequence":
            for index, child in enumerate(node.get("steps") or []):
                walk(child, f"{path}.steps[{index}]")
        elif kind == "batch":
            walk(node.get("body"), f"{path}.body")
        elif kind == "choice":
            for option in node.get("options") or []:
                if isinstance(option, dict):
                    walk(option.get("work"), f"{path}.options.{option.get('id')}")

    walk(tree.get("work") if isinstance(tree, dict) else None, "work")
    return found[-1] if found else None


@dataclass
class ReflexActivity:
    activity_id: str
    plan: dict[str, Any]
    records: dict[str, NodeRecord] = field(default_factory=dict)
    status: str = "accepted"
    result: WorkRunResult | None = None
    # ``task`` may be an asyncio Task (same-loop caller) or a
    # concurrent.futures.Future (tool invoked from the read lane).
    task: Any = None
    revision: int = 1
    created_at: float = field(default_factory=time.time)
    cancel_reason: str | None = None
    # Projected call results already shown to the Agent, so repeated reads of
    # one tree do not store the same original again.
    shown: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The Agent's plans hold the Body as ``agent_plan``; the Agent's own
    # projects run in idle life as ``life`` and yield to any Agent call.
    holder: str = "agent_plan"
    project_id: str | None = None
    put_down: bool = False
    recorded: bool = False
    # Set when the plan stopped because the code under it raised; the Agent
    # sees it instead of a bare "blocked" with no facts.
    error: str | None = None
    # Facts this activity has sent the Agent. A resumed plan can end with the
    # same status again; numbered, that later report is a new wake.
    reports: int = 0
    # The ladder item an Agent plan walks (delegate_work writes one), and
    # whether this run's end is in the project log yet.
    item_id: str | None = None
    # A life project that gave the Body to the Agent ends "yielded" and waits.
    yielded: bool = False
    # Decision chose to hand this run back to the Agent: its end wakes it.
    handed_back: bool = False
    # The Body at the start of this run and as last read: the end row says
    # where it ended, what it holds and what changed (s7-later-20261029.md §8 D4).
    start_body: dict[str, Any] | None = None
    last_body: dict[str, Any] | None = None
    # Steps Decision reported and went on past: node, call, the call's reason.
    reported: list[dict[str, Any]] = field(default_factory=list)


# A plan in these states holds its Skills mounted (skill_mounts.py).
_PLAN_SKILLS_LIVE = frozenset({"accepted", "running", "waiting", "updating"})


class ReflexManager:
    """Own one plan activity and expose a same-tree management surface."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self._lock = threading.RLock()
        self._activities: dict[str, ReflexActivity] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_activity_id: str | None = None
        # take_control holds the ladder: no item is offered until the Agent
        # resumes or delegates again (plan-ladder-20261028.md §7.2).
        self._ladder_held = False
        # The Body timeline the Agent reads (§7.5): busy since when, with how
        # many runs, or idle since when.
        self._busy_since: float | None = None
        self._busy_runs = 0
        self._idle_since = time.time()
        # The stance a ladder item sets (ladder-item-consolidation-20261108
        # §3): the running item's, pending until its first successful Body
        # result; that result sets the stance in frames to the item's own
        # until another item's stance is set, the Agent's later
        # update_life stance, a broken check or a death.
        self._item_stance: dict[str, Any] | None = None   # {item, text, pending}
        self._stance_set: dict[str, Any] | None = None    # {item, at, fact}
        # The ladder item whose run is going on now: its rules, when it has
        # them, are the ones in frames (ladder-item-consolidation §3, step 2).
        self._item_running: str | None = None
        authority = getattr(runtime, "authority", None)
        if authority is not None and callable(getattr(authority, "add_generation_listener", None)):
            authority.add_generation_listener(self._on_generation_invalidated)
        # Plan Skills (skill_mounts.py): set by composition when a Skill
        # catalog exists. ``skill_check(written, mounted)`` raises
        # SkillMountError; ``ladder_mounts()`` is what the ladder holds now.
        self.skill_check: Any = None
        self.ladder_mounts: Any = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def close(self) -> None:
        with self._lock:
            tasks = [activity.task for activity in self._activities.values()
                     if activity.task is not None and not activity.task.done()]
            for activity in self._activities.values():
                if activity.task is not None and not activity.task.done():
                    activity.status = "cancelled"
                    activity.cancel_reason = "runtime_closed"
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def request_cancel(self, reason: str, *, final_status: str = "cancelled") -> None:
        """Revoke a plan before an external/native stop begins."""
        with self._lock:
            for activity in self._activities.values():
                if activity.status not in {"accepted", "running", "waiting", "stopping"}:
                    continue
                activity.cancel_reason = reason
                activity.status = final_status
                if activity.task is not None and not activity.task.done():
                    task = activity.task
                    loop = self._loop
                    if loop is not None:
                        try:
                            running = asyncio.get_running_loop()
                        except RuntimeError:
                            running = None
                        if running is loop:
                            task.cancel()
                        else:
                            loop.call_soon_threadsafe(task.cancel)
                    else:
                        task.cancel()
                break

    def _on_generation_invalidated(self, reason: str, _generation: int) -> None:
        """External invalidation revokes an active plan immediately."""
        self.request_cancel(f"generation_invalidated:{reason}")

    def _notify_activity(self, activity: ReflexActivity) -> None:
        """Publish one terminal activity fact to whichever Agent channel exists.

        A plan end is a notice the Agent reads on its next turn; only Decision
        handing the work back wakes it (plan-ladder-20261028.md §7.4).
        """
        ended = self._record_item_end(activity)
        activity.reports += 1
        fact = {
            "activity_id": activity.activity_id,
            "status": activity.status,
            "report": activity.reports,
            "wake": activity.handed_back and not activity.put_down,
            "tree": self._shown_tree(activity),
            "facts": self._shown_facts(activity),
        }
        if activity.item_id is not None:
            fact["item"] = activity.item_id
        if ended is not None:
            fact["ended"] = ended
            off = self._off_ladder(activity.item_id)
            if off is not None:
                fact["off_ladder"] = off
        if activity.error:
            fact["error"] = activity.error
        wake = getattr(self.runtime, "notify_reflex_fact", None)
        if callable(wake):
            wake(fact)
        elif self.runtime.responses_kernel is not None:
            self.runtime.responses_kernel.notify_fact(fact)

    def _submit(self, coroutine):
        loop = self._loop
        if loop is None:
            return None
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            return loop.create_task(coroutine)
        return asyncio.run_coroutine_threadsafe(coroutine, loop)

    async def _await_task(self, task: Any) -> None:
        if task is None or task is asyncio.current_task():
            return
        if isinstance(task, asyncio.Future):
            await task
            return
        await asyncio.wrap_future(task)

    def delegate(self, spec: dict[str, Any]) -> dict[str, Any]:
        plan = spec.get("plan", spec)
        root = plan.get("work") if isinstance(plan, dict) else None
        if not isinstance(root, dict) or not str(root.get("intent") or "").strip():
            return ToolResult(
                False, "missing_intent", False,
                next_suggestion="Add an intent to the plan's outermost work node before delegating it.",
                metrics={"missing": "work.intent"},
            ).to_payload()
        rejected = self._plan_skill_rejection(plan)
        if rejected is not None:
            return rejected
        try:
            validate_plan(plan)
        except WorkPlanError as exc:
            return ToolResult(False, "invalid_plan", True, metrics={"error": str(exc)}).to_payload()
        self.yield_life("agent_plan_arrived")
        with self._lock:
            active = next((item for item in self._activities.values() if item.status in {"accepted", "running", "waiting"}), None)
            if active is not None and active.project_id is not None:
                return ToolResult(False, "waiting_current", True, metrics={"activity_id": active.activity_id}).to_payload()
            # The plan is the top item of the ladder, or right after the
            # Agent's own item running now.
            ladder: dict[str, Any] = {}
            store = getattr(self.runtime, "life_store", None)
            if store is not None:
                given = spec.get("id") if "plan" in spec else None
                try:
                    written = store.put_delegated(plan, item_id=None if given is None else str(given),
                                                  after=None if active is None else active.item_id)
                except (LifeProfileError, LifeProfileUnreadable) as exc:
                    return ToolResult(False, "ladder_rejected", True, metrics={"error": str(exc)}).to_payload()
                ladder = {"item_id": written["item_id"], "life_revision": written["revision"]}
            self._ladder_held = False
            if active is not None:
                # Thinking ahead while the Body works: the plan waits on the
                # ladder right after the running one; when that ends, the
                # reflex layer picks the next item (§7.3), with no queue.
                if not ladder:
                    return ToolResult(False, "plan_running", True, metrics={
                        "running_activity_id": active.activity_id}).to_payload()
                self.runtime.trace.emit("reflex_plan_on_ladder", after_activity_id=active.activity_id,
                                        item_id=ladder["item_id"], goal=str(plan.get("goal", ""))[:200])
                return ToolResult(True, "on_ladder_after_running_plan", True, metrics={
                    "running_activity_id": active.activity_id,
                    "starts_when": "the running plan has ended and the reflex layer picks it from the ladder",
                    **ladder, **self._skills_metrics(plan)}).to_payload()
            activity = ReflexActivity(f"activity-{uuid4().hex}", copy.deepcopy(plan),
                                      item_id=ladder.get("item_id"))
            self._activities[activity.activity_id] = activity
            loop = self._loop
            if loop is None:
                return ToolResult(True, "accepted", True, metrics={"activity_id": activity.activity_id, "status": "accepted",
                                                                   **ladder, **self._skills_metrics(plan)}).to_payload()
            activity.status = "running"
            self._record_item_start(activity)
            activity.task = self._submit(self._run(activity))
        return ToolResult(True, "accepted", True, metrics={
            "activity_id": activity.activity_id,
            "status": activity.status,
            **ladder,
            **self._skills_metrics(plan),
            "tree": self._shown_tree(activity),
        }).to_payload()

    def plan_skill_mounts(self, *, excluding: ReflexActivity | None = None) -> list[tuple[str, str]]:
        """(name, ``plan:<activity id>``) for the Skills of every plan running now.

        A plan's Skills stay mounted while it runs (waiting and being updated
        count); its end in any way unmounts them at the next turn start.
        """
        with self._lock:
            rows: list[tuple[str, str]] = []
            for activity in self._activities.values():
                if activity is excluding or activity.status not in _PLAN_SKILLS_LIVE:
                    continue
                names = activity.plan.get("skills") if isinstance(activity.plan, dict) else None
                for name in names or ():
                    rows.append((str(name), plan_holder(activity.activity_id)))
            return rows

    def _plan_skill_rejection(self, plan: Any, *, replacing: ReflexActivity | None = None) -> dict[str, Any] | None:
        """A typed refusal when the plan's Skills cannot all be mounted, else None.

        Every name must exist and load, and everything mounted with them (the
        ladder's Skills and other running plans') must stay within the caps.
        """
        if not isinstance(plan, dict) or plan.get("skills") is None:
            return None
        try:
            names = project_skills({"skills": plan["skills"]}, "plan")
        except SkillMountError as exc:
            return ToolResult(False, exc.code, True, metrics={"error": str(exc)}).to_payload()
        except LifeProfileError as exc:
            return ToolResult(False, "invalid_plan", True, metrics={"error": str(exc)}).to_payload()
        if not names:
            return None
        if self.skill_check is None:
            return ToolResult(False, "skill_not_found", True, metrics={
                "error": "this runtime has no Skill catalog to mount from"}).to_payload()
        ladder = self.ladder_mounts() if callable(self.ladder_mounts) else None
        held = merged_mounts(ladder or [], self.plan_skill_mounts(excluding=replacing),
                             [(name, "this plan") for name in names])
        try:
            self.skill_check(names, [name for name, _holder in held])
        except SkillMountError as exc:
            return ToolResult(False, exc.code, True, metrics={"error": str(exc), "skills": names}).to_payload()
        return None

    @staticmethod
    def _skills_metrics(plan: Any) -> dict[str, Any]:
        names = plan.get("skills") if isinstance(plan, dict) else None
        if not names:
            return {}
        return {"plan_skills": list(names),
                "skills_note": ("Each Skill not yet mounted arrives as a SKILL_MOUNTED message at the start of your "
                                "next turn while the plan runs; when it ends, one nothing else holds is announced "
                                "as SKILL_UNMOUNTED.")}

    def ended_activity(self, activity_id: str) -> dict[str, Any] | None:
        """How a plan ended, or None when it is still going or not a plan."""
        with self._lock:
            activity = self._activities.get(activity_id)
            if activity is None or activity.status in {"accepted", "running", "waiting", "updating", "stopping"}:
                return None
            return {"activity_id": activity.activity_id, "status": activity.status,
                    "goal": str(activity.plan.get("goal", ""))[:200]}

    def request_deep_thought(self, params: dict[str, Any]) -> dict[str, Any]:
        """The Agent asks for its next turn at the deepest effort (user 2026-11-09)."""
        about = str((params or {}).get("about") or "").strip()[:400]
        if not about:
            return ToolResult(False, "missing_about", True,
                              next_suggestion="Say in one sentence what you want to think through.").to_payload()
        self.runtime.deep_thought_request = {"about": about}
        self._deep_thought_seq = getattr(self, "_deep_thought_seq", 0) + 1
        notify = getattr(self.runtime, "notify_reflex_fact", None)
        if callable(notify):
            notify({"kind": "deep_thought", "activity_id": f"deep-thought-{self._deep_thought_seq}",
                    "status": "requested", "reason": about,
                    "note": "You asked for this turn to think deeply about this."})
        return ToolResult(True, "deep_thought_requested", True, metrics={
            "about": about,
            "next_turn": ("starts when this turn ends and thinks at xhigh effort; if the Body was hurt in the "
                          "last 30 seconds or a hostile mob is within 8 blocks then, the first turn without that "
                          "is the deep one"),
        }).to_payload()

    def body_busy(self) -> bool:
        """Anything is working the Body: a ladder item Decision walks, an
        Agent plan, or a backgrounded direct call.

        The one answer to "is the Body working?" for the Agent's side: while
        it is, a continue checkpoint brings no new turn and a resting Agent
        hears no heartbeat (s7-later-20261029.md §8 D2, §8.12).
        """
        with self._lock:
            if self._direct_call() is not None:
                return True
            return any(item.status in {"accepted", "running", "waiting", "updating"}
                       for item in self._activities.values())

    def start_project(self, project: dict[str, Any], *, who: str = "decision") -> str | None:
        """Walk one of the Agent's own projects as idle life (holder ``life``).

        The tree starts from the top every time; steps already shown by the
        facts pass quickly through the "is it done" question.
        """
        plan = project_plan(project)
        with self._lock:
            if self._loop is None or any(item.status in {"accepted", "running", "waiting", "stopping", "updating"}
                                         for item in self._activities.values()):
                return None
            activity = ReflexActivity(f"project-{project['id']}-{uuid4().hex[:12]}", copy.deepcopy(plan),
                                      holder="life", project_id=str(project["id"]))
            self._activities[activity.activity_id] = activity
            activity.status = "running"
            activity.task = self._submit(self._run(activity))
        store = getattr(self.runtime, "life_store", None)
        if store is not None:
            try:
                store.record_project_event(str(activity.project_id), "started", who=who,
                                           activity_id=activity.activity_id)
            except OSError as exc:
                self.runtime.trace.emit("life_project_record_failed", project=activity.project_id,
                                        error_type=type(exc).__name__)
        self.runtime.trace.emit("life_project_started", activity_id=activity.activity_id,
                                project=activity.project_id, who=who)
        self._item_stance_begin(str(activity.project_id))
        return activity.activity_id

    def _item_stance_begin(self, item_id: str) -> None:
        """The item's run starts: its stance (or its having none) is pending
        until the item's first successful Body result (ladder-item-consolidation §3)."""
        self._item_running = str(item_id)
        text = None
        store = getattr(self.runtime, "life_store", None)
        if store is not None:
            try:
                item = next((row for row in store.profile().get("projects") or [] if row.get("id") == item_id), None)
            except LifeProfileUnreadable:
                item = None
            stance = item.get("stance") if isinstance(item, Mapping) else None
            value = stance.get("text") if isinstance(stance, Mapping) else stance
            text = value.strip() if isinstance(value, str) and value.strip() else None
        # An item without a stance leaves the stance as it is: the Agent's
        # update_life stance is written for the work its items do.
        self._item_stance = {"item": item_id, "text": text, "pending": True} if text else None
        if text:
            self.runtime.trace.emit("ladder_item_stance", project=item_id, status="pending")

    def _item_stance_result(self, result: Any) -> None:
        """A Body result inside the item's run: the first success sets the
        stance in frames to the item's own."""
        state = self._item_stance
        if state is None or not state["pending"]:
            return
        payload = result if isinstance(result, Mapping) else getattr(result, "result", None)
        if not (isinstance(payload, Mapping) and payload.get("success") is True):
            return
        state["pending"] = False
        now = datetime.now(timezone.utc)
        self._stance_set = {"item": state["item"], "at": now, "fact": {"text": state["text"]}}
        self.runtime.trace.emit("ladder_item_stance", project=state["item"], status="in_force", text=state["text"])

    STANCE_NOT_SET = ("This item's stance was not set: none of its Body calls succeeded. If you finish its "
                      "work with your own calls, write the stance with update_life in the same turn as the "
                      "call that puts the Body there.")

    def _item_stance_end(self, item_id: str | None, reason: str) -> str | None:
        """The item's run ends; a stance still pending is reported to the Agent."""
        if item_id is not None and self._item_running == str(item_id):
            self._item_running = None
        state = self._item_stance
        if state is None or item_id is None or state["item"] != str(item_id):
            return None
        self._item_stance = None
        if state["pending"] and state["text"]:
            self.runtime.trace.emit("ladder_item_stance", project=item_id, status="not_set", reason=reason)
            return self.STANCE_NOT_SET
        return None

    def item_running(self) -> str | None:
        """The ladder item whose run (a life project or the Agent's plan on it) is going on now."""
        return self._item_running

    def stance_set(self) -> dict[str, Any] | None:
        """The latest stance a ladder item set by acting: {item, at, fact|None}."""
        return self._stance_set

    def life_project_running(self) -> str | None:
        """The project walking its tree now; a project in a wait node is not."""
        with self._lock:
            return next((item.project_id for item in self._activities.values()
                         if item.holder == "life" and item.status in {"accepted", "running"}), None)

    def yield_life(self, reason: str, *, stop_body: bool = True) -> bool:
        """An Agent call or plan arrived: a running life project stops.

        The project is revoked first so no late Decision answer admits its
        next write; then the Body gets the ordinary real stop, which settles
        the life claim (``stop_body=False`` when the caller stops it itself).
        """
        with self._lock:
            life = next((item for item in self._activities.values()
                         if item.holder == "life" and item.status in {"accepted", "running", "waiting"}), None)
        if life is None:
            return False
        life.yielded = True
        self.request_cancel(reason)
        snapshot = self.runtime.body_control.snapshot()
        if stop_body and snapshot.holder == "life":
            self._submit(self.runtime.cancel_active_execution_with_facts(
                reason, invalidate_generation=False, cancel_reflex=False))
        return True

    async def yield_to_agent_call(self, reason: str) -> list[dict[str, Any]]:
        """The Agent's call takes the Body (body-preemption-20261106.md §3).

        A running life project yields as before. The Agent's own plan, or a
        ladder item Decision walks, pauses instead of refusing the call with
        body_busy: control_reflex resume continues it. The caller stops the
        Body once afterwards, which settles the plan's claim.
        """
        taken: list[dict[str, Any]] = []
        with self._lock:
            life = next((item for item in self._activities.values()
                         if item.holder == "life" and item.status in {"accepted", "running", "waiting"}), None)
            plan = next((item for item in self._activities.values()
                         if item.holder != "life" and item.status in {"accepted", "running", "waiting"}), None)
        if life is not None and self.yield_life(reason, stop_body=False):
            taken.append({"kind": "life_project", "project_id": life.project_id,
                          "now": "yielded; it waits"})
        if plan is not None:
            plan.status = "paused"
            plan.cancel_reason = reason
            self._item_stance_end(plan.item_id, "paused")
            previous = plan.result
            plan.result = WorkRunResult(
                "paused", self._tree(plan),
                copy.deepcopy(previous.results) if previous is not None else {},
                {**(copy.deepcopy(previous.facts) if previous is not None else {}),
                 "status": "paused", "reason": "taken_over_by_agent_call"},
            )
            task = plan.task
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
                try:
                    await asyncio.wait_for(self._await_task(task), timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                except Exception:
                    pass
            self.runtime.trace.emit("reflex_activity_stopped", activity_id=plan.activity_id,
                                    status="paused", reason=reason, settlement={"by": "agent_call"})
            taken.append({"kind": "plan", "activity_id": plan.activity_id,
                          "goal": str(plan.plan.get("goal", ""))[:200],
                          "now": "paused; control_reflex resume continues it"})
        return taken

    def put_down_project(self) -> None:
        """Decision chose to put the running ladder item down for now."""
        with self._lock:
            for item in self._activities.values():
                if (item.holder == "life" or item.item_id) and item.status in {"accepted", "running", "waiting"}:
                    item.put_down = True

    def _holder(self) -> str:
        activity = self._activities.get(self._active_activity_id or "")
        return "agent_plan" if activity is None else activity.holder

    def _settle_project(self, activity: ReflexActivity) -> None:
        """Record how the project run ended; wake the Agent when it is done or stuck."""
        activity.recorded = True
        outcome = "put_down" if activity.put_down else "yielded" if activity.yielded else activity.status
        stance_note = self._item_stance_end(activity.project_id, outcome)
        facts = {} if activity.result is None else self._shown_facts(activity)
        row = self._end_row(activity, outcome, facts)
        if stance_note:
            row["stance"] = stance_note
        self._log_item(str(activity.project_id), row)
        self.runtime.trace.emit("life_project_finished", project=activity.project_id, who="decision", **row)
        if outcome in {"completed", "blocked", "needs_agent", "put_down"}:
            # A notice, unless Decision handed the item back (§7.4); an item
            # the Agent took over (yielded) or cancelled tells it nothing new.
            wake = getattr(self.runtime, "notify_reflex_fact", None)
            if callable(wake):
                fact = {"activity_id": activity.activity_id, "kind": "life_project",
                        "project": activity.project_id, "item": activity.project_id, "status": outcome,
                        "wake": outcome == "needs_agent" and activity.handed_back, "ended": row,
                        "tree": self._shown_tree(activity), "facts": facts}
                off = self._off_ladder(activity.project_id)
                if off is not None:
                    fact["off_ladder"] = off
                wake(fact)

    def _off_ladder(self, item_id: str | None) -> str | None:
        """Why an item that just ended is off the ladder now, or None (s7-later-20261029.md §8 D5)."""
        store = getattr(self.runtime, "life_store", None)
        if item_id is None or store is None:
            return None
        try:
            item = next((row for row in store.profile().get("projects") or [] if row.get("id") == item_id), None)
        except LifeProfileUnreadable:
            return None
        return None if item is None else store.project_state(item).get("off_ladder")

    def _end_row(self, activity: ReflexActivity, outcome: str, facts: dict[str, Any]) -> dict[str, Any]:
        row = {"outcome": outcome, "activity_id": activity.activity_id,
               "duration_s": round(time.time() - activity.created_at, 1)}
        if activity.cancel_reason:
            row["reason"] = activity.cancel_reason
        for key in ("reason", "position", "inventory", "inventory_counts"):
            if key in facts and key not in row:
                row[key] = facts[key]
        # Every end says where the Body is, what it holds and what changed
        # since the start, in its own numbers (s7-later-20261029.md §8 D4).
        for key, value in (activity.last_body or body_now(self.runtime.last_known_body_state or {})).items():
            row.setdefault(key, value)
        start = activity.start_body or {}
        if start.get("position") is not None:
            row["start_position"] = start["position"]
        if isinstance(start.get("inventory_counts"), dict) and isinstance(row.get("inventory_counts"), dict):
            row["inventory_change"] = inventory_change(start["inventory_counts"], row["inventory_counts"])
        if outcome in {"needs_agent", "blocked"} and activity.result is not None:
            stopped = stopped_at(activity.result.tree)
            if stopped is not None:
                row["stopped_at"] = stopped
        if activity.reported:
            row["reported_steps"] = copy.deepcopy(activity.reported)
        return row

    def _log_item(self, item_id: str, row: dict[str, Any], *, event: str | None = None) -> None:
        store = getattr(self.runtime, "life_store", None)
        if store is None:
            return
        try:
            if event is None:
                store.record_project_result(item_id, row)
            else:
                store.record_project_event(item_id, event, **row)
        except OSError as exc:
            self.runtime.trace.emit("life_project_record_failed", project=item_id,
                                    error_type=type(exc).__name__)

    def _record_item_start(self, activity: ReflexActivity) -> None:
        """An Agent plan on a ladder item starts (again): the log says so."""
        if activity.item_id is None:
            return
        activity.recorded = False
        activity.put_down = False
        activity.handed_back = False
        activity.start_body, activity.reported = None, []
        self._log_item(activity.item_id, {"who": "agent", "activity_id": activity.activity_id}, event="started")
        self.runtime.trace.emit("life_project_started", activity_id=activity.activity_id,
                                project=activity.item_id, who="agent")
        self._item_stance_begin(str(activity.item_id))

    def _record_item_end(self, activity: ReflexActivity) -> dict[str, Any] | None:
        if activity.item_id is None or activity.recorded:
            return None
        activity.recorded = True
        outcome = "put_down" if activity.put_down else activity.status
        stance_note = self._item_stance_end(activity.item_id, outcome)
        row = self._end_row(activity, outcome, self._shown_facts(activity))
        if stance_note:
            row["stance"] = stance_note
        self._log_item(activity.item_id, row)
        self.runtime.trace.emit("life_project_finished", project=activity.item_id, who="agent", **row)
        return row

    async def _progress_state(self) -> Any:
        read = getattr(self.runtime, "read_progress_state", None)
        return await read() if callable(read) else None

    async def _note_plan_outcome(self, activity: ReflexActivity, status: str, before: Any) -> None:
        """Tell the Agent's progress counters about the plan once, as one step.

        The plan's own calls are the reflex layer's attempts and are judged by
        Decision.  For the Agent the step is the delegation: it worked when
        the plan completed or the Body ended up holding more than before.
        """
        authority = getattr(self.runtime, "authority", None)
        after = await self._progress_state()
        if authority is None or after is None:
            return
        gained = material_progress_evidence(before, after, successful_body_action=False)
        work = json.dumps(activity.plan.get("work"), sort_keys=True, default=str, ensure_ascii=False)
        authority.note_step(
            ("delegate_work", hashlib.sha256(work.encode()).hexdigest()[:16]),
            status == "completed" or bool(gained), authority.fingerprint(after),
        )
        self.runtime.trace.emit(
            "reflex_activity_progress_noted", activity_id=activity.activity_id,
            status=status, material_evidence=list(gained),
        )

    def body_timeline(self) -> dict[str, Any]:
        with self._lock:
            if self._busy_since is not None and not any(
                    item.status in {"accepted", "running", "waiting", "updating"}
                    for item in self._activities.values()):
                # The last run is over but has not closed yet (it reads the
                # Body once more first), or it was cancelled before it began:
                # the Body is free from now. Read as still busy, it let a
                # resting Agent's heartbeat ring the moment an item ended (T7
                # 2026-10-29, s7-later-20261029.md §8.13).
                self._busy_since, self._idle_since = None, time.time()
            return {"busy_since": self._busy_since, "runs": self._busy_runs,
                    "idle_since": None if self._busy_since is not None else self._idle_since}

    async def _run(self, activity: ReflexActivity) -> None:
        run_revision = activity.revision
        activity.error = None
        self._active_activity_id = activity.activity_id
        with self._lock:
            if self._busy_since is None:
                self._busy_since, self._busy_runs = time.time(), 0
            self._busy_runs += 1
        state_before = await self._progress_state()
        if activity.start_body is None:
            activity.start_body = activity.last_body = body_now(await self._state())

        async def read_state() -> dict[str, Any]:
            state = await self._state()
            activity.last_body = body_now(state)
            return state

        async def dispatch(tool_name, arguments, *, call_id, timeout_s):
            return await self._call(
                tool_name, arguments, call_id=call_id, timeout_s=timeout_s,
                supervision_context=executor.call_context(call_id),
            )

        executor = WorkPlanExecutor(
            activity.plan,
            call=dispatch,
            state=read_state,
            decision=self.runtime.decision_client,
            choice_provider=getattr(self.runtime, "choice_provider", None),
            observe=self._observe_condition,
            emit=lambda event, **payload: self.runtime.trace.emit(event, activity_id=activity.activity_id, **payload),
            records=activity.records,
            result_judge=getattr(getattr(self.runtime, "body_loop", None), "decide_plan_result", None),
            report=lambda step: self._report_step(activity, step),
        )
        try:
            wait_attempt = 0
            while True:
                if activity.revision != run_revision or activity.status in {"cancelled", "paused", "stopping", "updating"}:
                    return
                # A waiting plan does not monopolize the physical Body.  It
                # reacquires the claim only for the next real call/choice.
                while True:
                    try:
                        self.runtime.body_control.claim(activity.activity_id, holder=activity.holder)
                        break
                    except RuntimeError as exc:
                        reason = str(exc)
                        if reason.startswith("body_busy:"):
                            activity.status = "waiting"
                            self.runtime.trace.emit(
                                "reflex_activity_waiting_current",
                                activity_id=activity.activity_id,
                                reason=reason,
                            )
                            await asyncio.sleep(0.1)
                            if activity.revision != run_revision or activity.status in {"cancelled", "paused", "stopping", "updating"}:
                                return
                            continue
                        if reason == "body_control_unknown":
                            native = await self._probe_native_stop()
                            if native:
                                self.runtime.body_control.confirm_native_stopped(True)
                                self.runtime.trace.emit(
                                    "reflex_activity_control_recovered",
                                    activity_id=activity.activity_id,
                                )
                                continue
                            if activity.revision != run_revision:
                                return
                            activity.status = "blocked"
                            activity.result = WorkRunResult(
                                "blocked", self._tree(activity), {},
                                {"status": "blocked", "reason": "control_unknown", "native_stopped": False},
                            )
                            self.runtime.trace.emit(
                                "reflex_activity_blocked",
                                activity_id=activity.activity_id,
                                reason="control_unknown",
                            )
                            self._finish(activity)
                            return
                        if activity.revision != run_revision:
                            return
                        activity.status = "waiting"
                        activity.result = WorkRunResult("waiting", self._tree(activity), {}, {"reason": reason})
                        self.runtime.trace.emit(
                            "reflex_activity_waiting_current",
                            activity_id=activity.activity_id,
                            reason=reason,
                        )
                        return
                result = await executor.run()
                if activity.revision != run_revision or activity.status in {"cancelled", "paused", "stopping", "updating"}:
                    return
                activity.result = result
                activity.records = executor.records
                activity.status = result.status
                activity.handed_back = executor.handed_back
                if result.status != "waiting":
                    break
                wait_attempt += 1
                self.runtime.trace.emit("reflex_activity_waiting", activity_id=activity.activity_id,
                                        tree=result.tree, facts=result.facts,
                                        next_check_s=min(2.0, 0.25 * (2 ** min(wait_attempt - 1, 3))))
                if self.runtime.body_control.owns(activity.activity_id):
                    self.runtime.body_control.settle(claim_id=activity.activity_id)
                # Keep the activity alive, but cap Decision traffic and allow
                # direct calls to use an idle Body while this plan waits.
                await asyncio.sleep(min(2.0, 0.25 * (2 ** min(wait_attempt - 1, 3))))
                if activity.revision != run_revision or activity.status in {"cancelled", "paused", "stopping", "updating"}:
                    return
            if activity.revision != run_revision or activity.status in {"cancelled", "paused", "stopping", "updating"}:
                return
            self.runtime.trace.emit("reflex_activity_finished", activity_id=activity.activity_id,
                                    status=result.status, tree=result.tree)
            activity.last_body = body_now(await self._state())
            # The fact goes out with the status it reports; the counters are
            # brought up to date long before the woken Agent can act on it.
            self._finish(activity)
            if activity.project_id is None:
                await self._note_plan_outcome(activity, result.status, state_before)
        except asyncio.CancelledError:
            if activity.revision == run_revision and activity.status not in {"paused", "cancelled", "updating"}:
                activity.status = "cancelled"
            self.runtime.trace.emit("reflex_activity_cancelled", activity_id=activity.activity_id)
            raise
        except Exception as exc:
            if activity.revision != run_revision or activity.status in {"cancelled", "paused", "stopping", "updating"}:
                return
            activity.status = "blocked"
            activity.error = f"The plan stopped on an internal error, not on the world: {type(exc).__name__}: {str(exc)[:300]}"
            self.runtime.trace.emit("reflex_activity_failed", activity_id=activity.activity_id,
                                    error_type=type(exc).__name__, message=str(exc)[:400])
            activity.last_body = body_now(await self._state())
            self._finish(activity)
        finally:
            if self._active_activity_id == activity.activity_id:
                self._active_activity_id = None
            with self._lock:
                if not any(item is not activity and item.status in {"accepted", "running", "waiting", "updating"}
                           for item in self._activities.values()):
                    self._busy_since, self._idle_since = None, time.time()
            if activity.project_id is not None and not activity.recorded:
                # Preempted or revoked: every project run leaves one record.
                self._settle_project(activity)
            if activity.status in {"cancelled", "blocked", "completed", "needs_agent", "failed"}:
                # A plan revoked from outside sends no fact; its item still ends.
                self._record_item_end(activity)
            if (
                activity.status not in {"stopping", "cancelled", "paused", "updating"}
                and self.runtime.body_control.owns(activity.activity_id)
            ):
                self.runtime.body_control.settle(claim_id=activity.activity_id)

    def _report_step(self, activity: ReflexActivity, step: dict[str, Any]) -> None:
        """A failed step the plan went on past: the Agent hears of it now.

        The plan keeps the Body; the Agent may update, take over or cancel.
        """
        self.runtime.trace.emit("reflex_step_reported", activity_id=activity.activity_id,
                                node=step.get("node"), tool=step.get("tool"))
        activity.reported.append(step_words(step.get("node"), step.get("tool"), step.get("result")))
        wake = getattr(self.runtime, "notify_reflex_fact", None)
        activity.reports += 1
        fact = {"activity_id": activity.activity_id, "status": "step_reported", "report": activity.reports,
                "plan_status": "running", "kind": "life_project" if activity.project_id else "plan",
                "wake": False, **step}
        item = activity.item_id or activity.project_id
        if item:
            # The notice names the ladder item (S6 live check: it said null).
            fact["item"] = item
        if callable(wake):
            wake(fact)
        elif self.runtime.responses_kernel is not None:
            self.runtime.responses_kernel.notify_fact(fact)

    def _finish(self, activity: ReflexActivity) -> None:
        if activity.project_id is not None:
            self._settle_project(activity)
        else:
            self._notify_activity(activity)

    async def _state(self) -> dict[str, Any]:
        try:
            getter = (getattr(self.runtime.body, "get_reflex_facts", None)
                      or self.runtime.body.get_state)
            state = await self.runtime._read_lane.run(getter)
            if isinstance(state, dict):
                return copy.deepcopy(state)
            if is_dataclass(state):
                return copy.deepcopy(asdict(state))
            values = getattr(state, "__dict__", None)
            return copy.deepcopy(values) if isinstance(values, dict) else {}
        except Exception:
            # A plan can run while the Agent has no turns. Its cache may be
            # the observation before that plan, so use it only if sampling
            # fails, never in preference to the present Body observation.
            return copy.deepcopy(self.runtime.last_known_body_state or {})

    async def _observe_condition(self, condition: dict[str, Any]) -> dict[str, Any]:
        """Read one generic object projection for non-body bindings.

        The work language does not contain scene-specific detectors. A named
        player/entity binding is resolved through the existing bounded entity
        perception, and the complete/age metadata remains visible to
        Decision. If the provider cannot expose the read, the binding stays
        unknown instead of being treated as absent.
        """
        bindings = condition.get("bindings") if isinstance(condition, dict) else None
        if not isinstance(bindings, dict):
            return {}
        handles = [str(value) for name, value in bindings.items()
                   if str(name) not in {"bot", "body"} and str(value) not in {"bot", "body"}]
        if not handles:
            return {}
        registry = getattr(self.runtime, "registry", None)
        run_tool = getattr(self.runtime, "run_tool", None)
        if registry is None or not callable(run_tool):
            return {}
        try:
            tool = registry.get("read_nearby_entities")
        except Exception:
            return {}
        try:
            result = await run_tool(
                tool,
                execute_tool,
                tool,
                {"radius": 64, "limit": 128},
                self.runtime.weld_context,
                timeout_s=getattr(tool.sidecar, "timeout_s", 20.0),
                tool_call_id=f"plan-observe-{self._active_activity_id or 'unknown'}",
                claim_holder=self._holder(),
                body_claim_id=self._active_activity_id,
                retain_claim=True,
            )
        except Exception as exc:
            self.runtime.trace.emit("reflex_condition_observation_failed",
                                    activity_id=self._active_activity_id,
                                    error_type=type(exc).__name__)
            return {}
        metrics = result.get("metrics") if isinstance(result, dict) else None
        data = metrics.get("data") if isinstance(metrics, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("entities"), list):
            return {}
        sampled_at = data.get("sampled_at_ms") or metrics.get("sampled_at_ms")
        return {"nearby_entities": {
            "entities": copy.deepcopy(data["entities"]),
            "complete": bool(metrics.get("complete", False)),
            "radius": 64,
            "sampled_at_ms": sampled_at,
        }}

    async def _call(
        self, tool_name: str, arguments: dict[str, Any], *, call_id: str,
        timeout_s: float | None, supervision_context: dict[str, Any],
    ) -> dict[str, Any]:
        tool = self.runtime.registry.get(tool_name)
        holder = self._holder()
        if holder == "life":
            supervision_context = {**supervision_context, "is_life_project": True}
        elif (active := self._activities.get(self._active_activity_id or "")) is not None and active.item_id:
            supervision_context = {**supervision_context, "is_ladder_item": True}
        raw = await self.runtime.run_tool(
            tool,
            _execute_plan_call,
            tool,
            arguments,
            self.runtime.weld_context,
            timeout_s=timeout_s or tool.sidecar.timeout_s,
            tool_call_id=f"plan-{call_id}",
            claim_holder=holder,
            body_claim_id=self._active_activity_id,
            retain_claim=self._active_activity_id is not None,
            return_execution_receipt=True,
            supervision_context=supervision_context,
        )
        if not isinstance(raw, dict):
            return raw
        self._item_stance_result(raw)
        # Plan calls execute outside the Agent tool wrapper, whose settlement
        # normally records these observations. Update from the real result
        # before the executor takes its terminal Body snapshot or projects it.
        remember_body = getattr(self.runtime, "remember_tool_body_facts", None)
        if callable(remember_body):
            remember_body(raw)
        # WorkPlanExecutor must judge the same bounded projector that the
        # Agent sees for an ordinary tool call. Preserve supervision handoff
        # facts privately so the interpreter can record the Decision choice
        # without putting raw metrics back into the question.
        projector = getattr(self.runtime, "shown_plan_result", None)
        shown = projector(tool_name, raw, f"plan-{call_id}") if callable(projector) else dict(raw)
        metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
        handoff = metrics.get("direct_call_handoff")
        if isinstance(handoff, dict):
            shown["_supervision_handoff"] = copy.deepcopy(handoff)
        if isinstance(raw.get("_execution"), dict):
            shown["_execution"] = copy.deepcopy(raw["_execution"])
        return shown

    def _direct_call(self) -> dict[str, Any] | None:
        """The direct Body call still running after its turn stopped waiting."""
        call = getattr(self.runtime, "background_call", None)
        if call is None or call.task.done():
            return None
        return call.payload()

    def active_summary(self) -> dict[str, Any] | None:
        """One line of the live plan for the Agent's current-context snapshot."""
        with self._lock:
            activity = next((item for item in self._activities.values()
                             if item.status in {"accepted", "running", "waiting", "stopping", "updating"}), None)
            if activity is None:
                return None
            summary = {"activity_id": activity.activity_id, "status": activity.status,
                       "goal": str(activity.plan.get("goal", ""))[:200],
                       "elapsed_s": round(time.time() - activity.created_at, 1)}
            return summary

    def read(self, activity_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            direct = self._direct_call()
            if direct is not None and activity_id in {None, direct["tool_call_id"]}:
                # A direct call is one call node without a plan around it.
                return ToolResult(True, "ok", True, metrics={
                    "activity_id": direct["tool_call_id"],
                    "status": "running",
                    "direct_call": direct,
                    "execution": self._execution(),
                    "body_control": self.runtime.body_control.snapshot().to_payload(),
                }).to_payload()
            activity = self._select(activity_id)
            if activity is None:
                return ToolResult(False, "no_reflex", True).to_payload()
            return ToolResult(True, "ok", True, metrics={
                "activity_id": activity.activity_id,
                "status": activity.status,
                "revision": activity.revision,
                "tree": self._shown_tree(activity),
                "facts": self._shown_facts(activity),
                **({"error": activity.error} if activity.error else {}),
                "execution": self._execution(),
                "body_control": self.runtime.body_control.snapshot().to_payload(),
            }).to_payload()

    def _execution(self) -> dict[str, Any] | None:
        """What the Body operation in flight is doing right now, if any."""
        snapshots = getattr(self.runtime, "execution_snapshots", None)
        return None if snapshots is None else snapshots.current()

    def control(self, params: dict[str, Any]) -> dict[str, Any]:
        operation = str(params.get("operation", ""))
        activity_id = params.get("activity_id")
        with self._lock:
            activity = self._select(str(activity_id) if activity_id else None)
            direct = self._direct_call()
            if direct is not None and operation in {"cancel", "take_control"} and (
                    not activity_id or activity_id == direct["tool_call_id"]):
                # Stopping a direct call uses the one stop entry; its own
                # final result then reports what the stop left behind.
                activity = None
                operation = "take_control"
            if operation == "take_control":
                # The Body is the Agent's until it resumes or delegates again.
                self._ladder_held = True
                self.runtime.trace.emit("ladder_held", by="take_control")
                if activity is None:
                    loop = self._loop
                    stop = getattr(self.runtime, "cancel_active_execution_with_facts", None)
                    if loop is None or not callable(stop):
                        return ToolResult(False, "event_loop_unavailable", True).to_payload()
                    async def stop_direct() -> Any:
                        try:
                            return await stop("reflex_take_control", cancel_reflex=False)
                        except TypeError:
                            return await stop("reflex_take_control")
                    coroutine = stop_direct()
                    try:
                        running = asyncio.get_running_loop()
                    except RuntimeError:
                        running = None
                    if running is loop:
                        task = loop.create_task(coroutine)
                        return ToolResult(True, "take_control_requested", True,
                                          metrics={"settlement_pending": True, "task": id(task)}).to_payload()
                    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
                    return ToolResult(True, "take_control_requested", True,
                                      metrics={"settlement_pending": True, "task": id(future)}).to_payload()
                return self._schedule_stop(activity, "reflex_take_control", "cancelled")
            if operation == "resume" and self._ladder_held and (
                    activity is None or activity.status not in {"paused", "needs_agent"}):
                self._ladder_held = False
                self.runtime.trace.emit("ladder_released", by="resume")
                return ToolResult(True, "ladder_resumed", True).to_payload()
            if activity is None:
                return ToolResult(False, "no_reflex", True).to_payload()
            if operation == "cancel":
                if activity.status in {"accepted", "running", "waiting", "stopping", "updating"}:
                    # Cancelling is the Agent's word on the item: paused until it says otherwise.
                    self._pause_item(activity.item_id or activity.project_id)
                return self._schedule_stop(activity, "reflex_cancel", "cancelled")
            if operation == "pause":
                return self._schedule_stop(activity, "reflex_pause", "paused")
            if operation == "resume":
                # A plan handed back to the Agent resumes by judging the
                # results it has; its calls run again only if Decision repeats.
                if activity.status not in {"paused", "needs_agent"} or self._loop is None:
                    # Nothing was paused: say so instead of "resumed".
                    return ToolResult(False, "nothing_to_resume", True, metrics={
                        "activity_id": activity.activity_id, "status": activity.status}).to_payload()
                released = self._ladder_held
                self._ladder_held = False
                activity.cancel_reason = None
                activity.status = "running"
                self._record_item_start(activity)
                previous = activity.task

                async def resume_after_previous() -> None:
                    # The previous run may still be settling (its outcome
                    # note, its finally releasing the claim). Starting now
                    # let that finally release the new run's claim.
                    if previous is not None and previous is not asyncio.current_task():
                        try:
                            await self._await_task(previous)
                        except asyncio.CancelledError:
                            current = asyncio.current_task()
                            if current is not None and current.cancelling():
                                raise
                        except Exception:
                            pass
                    if activity.status == "running":
                        await self._run(activity)

                activity.task = self._submit(resume_after_previous())
                return ToolResult(True, "resumed", True, metrics={
                    "activity_id": activity.activity_id, "status": activity.status,
                    **({"ladder_released": True} if released else {})}).to_payload()
            if operation == "update":
                spec = params.get("spec")
                rejected = self._plan_skill_rejection(spec, replacing=activity)
                if rejected is not None:
                    return rejected
                try:
                    validate_plan(spec)
                except (WorkPlanError, TypeError) as exc:
                    return ToolResult(False, "invalid_plan", True, metrics={"error": str(exc)}).to_payload()
                store = getattr(self.runtime, "life_store", None)
                if activity.item_id is not None and store is not None:
                    # The ladder item is the plan: it changes with it.
                    try:
                        store.put_delegated(spec, item_id=activity.item_id, after=activity.item_id)
                    except (LifeProfileError, LifeProfileUnreadable) as exc:
                        return ToolResult(False, "ladder_rejected", True, metrics={"error": str(exc)}).to_payload()
                if self._loop is None:
                    activity.plan = copy.deepcopy(spec)
                    activity.revision += 1
                    activity.status = "accepted"
                    return ToolResult(True, "updated", True, metrics={"activity_id": activity.activity_id, "tree": self._shown_tree(activity)}).to_payload()

                # Publish the new revision synchronously. Native stop and the
                # replacement run remain asynchronous, but an immediate read
                # must not expose the old terminal status and old plan.
                activity.status = "updating"
                activity.cancel_reason = "update"
                activity.revision += 1
                activity.plan = copy.deepcopy(spec)
                activity.result = None

                async def replace_plan() -> None:
                    revision = activity.revision
                    old_task = activity.task
                    if old_task is not None and not old_task.done():
                        old_task.cancel()
                    stop = getattr(self.runtime, "cancel_active_execution_with_facts", None)
                    if callable(stop):
                        facts = await stop("reflex_update", cancel_reflex=False)
                        if not facts.get("settled", False):
                            if activity.revision == revision:
                                activity.status = "blocked"
                            return
                    if old_task is not None and old_task is not asyncio.current_task():
                        # The old run was cancelled above: awaiting it raises
                        # CancelledError, which is its end, not this task's.
                        # Letting it through left the plan "updating" with
                        # nothing running for 16 minutes (easy run-11, 2026-11-09).
                        try:
                            await self._await_task(old_task)
                        except asyncio.CancelledError:
                            current = asyncio.current_task()
                            if current is not None and current.cancelling():
                                raise
                        except Exception:
                            pass
                    if activity.revision != revision:
                        return
                    activity.cancel_reason = None
                    activity.status = "running"
                    activity.task = self._submit(self._run(activity))

                self._submit(replace_plan())
                return ToolResult(True, "updated", True, metrics={
                    "activity_id": activity.activity_id,
                    "status": "updating",
                    "revision": activity.revision,
                }).to_payload()
            return ToolResult(False, "unknown_control_operation", True).to_payload()

    def ladder_held(self) -> bool:
        """take_control holds the ladder until resume or the next delegate_work."""
        return self._ladder_held

    def _pause_item(self, item_id: str | None) -> None:
        store = getattr(self.runtime, "life_store", None)
        if item_id is None or store is None:
            return
        try:
            revision = store.read()["revision"]
            store.update_project("upsert", {"id": item_id, "status": "paused"}, expected_revision=revision,
                                 reason="control_reflex cancel: the Agent stopped this item")
        except (LifeProfileError, LifeProfileUnreadable) as exc:
            self.runtime.trace.emit("ladder_pause_failed", project=item_id, error=str(exc)[:300])

    def _schedule_stop(self, activity: ReflexActivity, reason: str, final_status: str) -> dict[str, Any]:
        activity.status = "stopping"
        activity.cancel_reason = reason

        async def stop_activity() -> dict[str, Any]:
            task = activity.task
            if task is not None and not task.done():
                task.cancel()
            stop = getattr(self.runtime, "cancel_active_execution_with_facts", None)
            facts = {"settled": True, "native_stopped": True}
            if callable(stop):
                try:
                    facts = await stop(reason, cancel_reflex=False)
                except TypeError:
                    facts = await stop(reason)
            if task is not None and task is not asyncio.current_task():
                try:
                    await self._await_task(task)
                except asyncio.CancelledError:
                    pass
            if facts.get("settled", False):
                activity.status = final_status
                previous = activity.result
                activity.result = WorkRunResult(
                    final_status,
                    self._tree(activity),
                    copy.deepcopy(previous.results) if previous is not None else {},
                    {
                        **(copy.deepcopy(previous.facts) if previous is not None else {}),
                        "status": final_status,
                    },
                )
            else:
                activity.status = "blocked"
                previous = activity.result
                activity.result = WorkRunResult(
                    "blocked",
                    self._tree(activity),
                    copy.deepcopy(previous.results) if previous is not None else {},
                    {
                        **(copy.deepcopy(previous.facts) if previous is not None else {}),
                        "status": "blocked",
                        "reason": "native_stop_unconfirmed",
                    },
                )
            self.runtime.trace.emit(
                "reflex_activity_stopped",
                activity_id=activity.activity_id,
                status=activity.status,
                reason=reason,
                settlement=facts,
            )
            self._notify_activity(activity)
            return facts

        loop = self._loop
        if loop is None:
            return ToolResult(False, "event_loop_unavailable", True).to_payload()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            task = loop.create_task(stop_activity())
            return ToolResult(True, "stop_requested", True, metrics={
                "activity_id": activity.activity_id,
                "status": "stopping",
                "settlement_pending": True,
                "task": id(task),
            }).to_payload()
        future = asyncio.run_coroutine_threadsafe(stop_activity(), loop)
        return ToolResult(True, "stop_requested", True, metrics={
            "activity_id": activity.activity_id,
            "status": "stopping",
            "settlement_pending": True,
            "task": id(future),
        }).to_payload()

    async def _probe_native_stop(self) -> bool:
        """Confirm the native Body is idle before retrying an unknown claim."""
        probe = getattr(self.runtime.body, "confirm_control_idle", None)
        if not callable(probe):
            return False
        try:
            lane = getattr(self.runtime, "_read_lane", None)
            if lane is not None and callable(getattr(lane, "run", None)):
                native = await lane.run(probe)
            else:
                native = await asyncio.to_thread(probe)
        except Exception as exc:
            self.runtime.trace.emit(
                "reflex_activity_control_probe_failed",
                activity_id=self._active_activity_id,
                error_type=type(exc).__name__,
            )
            return False
        return isinstance(native, dict) and native.get("native_stopped") is True

    def _select(self, activity_id: str | None) -> ReflexActivity | None:
        if activity_id:
            return self._activities.get(activity_id)
        return next(reversed(self._activities.values()), None) if self._activities else None

    @staticmethod
    def _shown_facts(activity: ReflexActivity) -> dict[str, Any]:
        """Run facts without the call results, which the tree already shows per node."""
        if activity.result is None:
            return {}
        return {key: copy.deepcopy(value) for key, value in activity.result.facts.items() if key != "results"}

    def _shown_tree(self, activity: ReflexActivity) -> dict[str, Any]:
        """The plan tree as the Agent reads it.

        The executor keeps every call's original result, because later nodes
        and conditions refer to it. The Agent reads each of them the way it
        reads a direct call: projected, with a handle to the stored original.
        """
        tree = copy.deepcopy(self._tree(activity))
        project = getattr(self.runtime, "shown_plan_result", None)
        if not callable(project):
            return tree

        def show(runtime: Any, tool: Any, ref: str) -> None:
            if not isinstance(runtime, dict):
                return
            result = runtime.get("result")
            if isinstance(result, dict) and isinstance(tool, str):
                key = f"{ref}:{runtime.get('updated_at')}"
                if key not in activity.shown:
                    activity.shown[key] = project(tool, result, f"plan-{ref}")
                runtime["result"] = copy.deepcopy(activity.shown[key])
            blocked = runtime.get("blocked")
            if isinstance(blocked, dict) and "result" in blocked and "result" in runtime:
                # The same result twice; the reason is what the block adds.
                blocked.pop("result")

        def walk(node: Any, path: str) -> None:
            if not isinstance(node, dict):
                return
            ref = node.get("id") if isinstance(node.get("id"), str) and node.get("id") else path
            runtime = node.get("_runtime")
            show(runtime, node.get("tool"), ref)
            kind = node.get("kind")
            if kind == "sequence":
                for index, child in enumerate(node.get("steps", [])):
                    walk(child, f"{path}.steps[{index}]")
            elif kind == "batch":
                body = node.get("body")
                walk(body, f"{path}.body")
                instances = runtime.get("instances") if isinstance(runtime, dict) else None
                if isinstance(instances, dict) and isinstance(body, dict):
                    for index, instance in instances.items():
                        show(instance, body.get("tool"), f"{ref}[{index}]")
            elif kind == "choice":
                for option in node.get("options", []):
                    if isinstance(option, dict):
                        walk(option.get("work"), f"{path}.options.{option.get('id')}")

        walk(tree.get("work"), "work")
        return tree

    def _tree(self, activity: ReflexActivity) -> dict[str, Any]:
        if activity.result is not None:
            return activity.result.tree
        try:
            executor = WorkPlanExecutor(activity.plan, call=self._call, state=self._state,
                                        decision=self.runtime.decision_client,
                                        choice_provider=getattr(self.runtime, "choice_provider", None),
                                        observe=self._observe_condition,
                                        records=activity.records)
            return executor.annotated_tree()
        except Exception:
            return copy.deepcopy(activity.plan)


# The tree with projected results is what read_reflex is asked for.
REFLEX_TREE_CHARS = 12_000


def reflex_observation_projector(reason: str, metrics: dict[str, object]) -> dict[str, Any]:
    if reason == "invalid_plan" and "error" in metrics:
        # Validation is the rejection's answer, even beyond the generic
        # 300-character text bound used for incidental metrics.
        return {"error": metrics["error"]}
    tree = metrics.get("tree")
    if not isinstance(tree, dict):
        return {}
    shown, whole = answer_value(tree, budget=REFLEX_TREE_CHARS)
    return {"tree": shown, "tree_complete": whole}


def register_reflex_tools(registry: ToolRegistry, manager: ReflexManager) -> None:
    registry.register(RegisteredTool(
        "delegate_work",
        "Use this tool when a goal involves multiple dependent steps, a long-running action followed by later actions, a condition that may remain pending, or work that may need pause, resume, cancellation, or revision. The reflex layer walks the submitted tree one step at a time from bounded facts; you do not need to poll or wait after delegating, and terminal results are delivered asynchronously. The tree is a range of allowed steps and intents, not a perfect script.\n\nFor work that means ‘do this until enough’, express the range as repeat around a call or choice and write one natural-language ending condition. The reflex layer decides whether the current facts satisfy that condition, whether the same call can make progress again, or whether to return control. After handing over a plan, use your turn to prepare later reasoning instead of reading progress. A plan may carry skills: [names from AVAILABLE_SKILLS]; each one's full text is added to your conversation and stays in effect while the plan runs, then unmounts unless a ladder item or another running plan still holds it (the same caps as ladder items; a name that is unknown or over the caps is refused). The plan is written as the top item of your plan ladder (see read_life; give id to name it, otherwise it is work-<n>), and the result returns its item_id and the new life revision. A plan delegated while another of yours is running goes on the ladder right after it; when the running plan ends, the reflex layer picks from your ladder the item that fits then.\n\nSubmit one minebot-work/0.2 plan object with: language='minebot-work/0.2', goal (a string in English: the reflex layer reads only English), positive timeout_s, a required work.intent, and work (the root node). Every mutating direct body call also requires an intent field. Intent states the result to achieve, not the thought or tactic while starting it, written in English as facts the Body reports: inventory item ids and counts, the item held in the main hand, or a position or height. Use the item ids the inventory shows; when any kind will do, list them, e.g. \'at least 8 logs of any kind (oak_log, birch_log, spruce_log, ...) in the inventory\', never a bare category such as \'logs\'. Good: \'at least 4 oak_log in the inventory\', \'stone_pickaxe held in the main hand\', \'standing at y 70 or higher\'. Bad: \'I will try the trees first\', \'back on open ground\'. Child nodes may inherit their parent's intent; read-only calls do not need one. Missing intent is rejected.\n\nNode kinds:\n- call   {kind:'call', tool, arguments, optional id/when/timeout_s/repeat} — one tool call; Decision judges its result and may repeat it\n- sequence {kind:'sequence', intent, steps:[...]} — ordered range of steps; intent is required and must describe the composite action\n- wait   {kind:'wait', condition:{text,...}, timeout_s} — hold until a condition is satisfied or times out\n- batch  {kind:'batch', intent, items:[...], body:<node>} — run body once per item; intent required\n- choice {kind:'choice', intent, options:[{id,intent,work},...]} — Decision picks among the declared branches whenever the choice is entered; every option needs id, intent and work\n\nA call repeat is {again_when:<text or object>, until:<text or object>}; those conditions are interpreted by Decision after each result. Decision may also run the same call again when its result shows the call can still make progress; repeat adds your own wording for when to go again and when to stop. A plan may include interrupt_when={condition:{text:...},handler:<work node with intent>}. The condition and handler intent appear in each running work frame. Decision stops and settles the current action, executes the handler, then chooses resume, repeat the handler, or call the Agent. User cancellation cancels both.\n\nsequence, batch, choice, and every choice option all require a non-empty intent field.\n\nExample multi-step plan (move then place):\n  {language:'minebot-work/0.2', goal:'go to [10,64,20] and place oak_log at [10,65,20]', timeout_s:60,\n   work:{kind:'sequence', intent:'oak_log placed at [10,65,20]',\n     steps:[\n       {kind:'call', tool:'move_to', arguments:{x:10,y:64,z:20}},\n       {kind:'call', tool:'place_block', arguments:{block:'oak_log',x:10,y:65,z:20}}\n     ]}}\n\nA plan ends completed, needs_agent, blocked or cancelled. When a step fails and later steps stand on their own, Decision may note it (step_reported) and go on; the plan still completes, and its facts list the reported steps. needs_agent means Decision handed the plan back to you, and only that wakes you; it is not a failure: every node shows its real result, successes included, and resume or update continues from those results. Decision may also set the plan aside: it stops and waits on your ladder until you change it. blocked means the plan could not go on, with the reason on the node. Every end other than a hand-back reaches you as a notice at the start of your next turn.\n\nUse only registered tool names and keep arguments as the tool schema requires. Let the reflex layer judge enough, completion and off-intent from facts; do not encode scene detectors or a polling loop in the plan.",
        {"type": "object", "properties": {
            "plan": {"type": "object"},
            "id": {"type": "string", "maxLength": 48,
                   "description": "Optional ladder item id: 1-48 lowercase letters, digits or '-'. "
                                  "An existing item with this id is replaced and moved to the top."},
        }, "required": ["plan"], "additionalProperties": False},
        manager.delegate,
        ToolSidecar("reflex_delegate", mutating=False, source="agent.reflex", tool_type="plan", permission="manage_reflex"),
        projector=reflex_observation_projector,
    ))
    registry.register(RegisteredTool(
        "control_reflex", "Pause, resume, cancel, revise, or take control of a reflex activity. Resume continues a paused "
        "plan, or a plan that ended needs_agent: the reflex layer judges the results it already has and runs a call "
        "again only if it decides to repeat it; with nothing paused it returns nothing_to_resume. take_control stops "
        "what runs and holds your plan ladder: no ladder item starts until you resume or delegate again; "
        "the reflex layer still protects the Body: it may fight, flee or surface when it reads danger; your stance tells it where the Body should stay. cancel stops "
        "the running item and marks it paused on the ladder. update also rewrites the ladder item the plan walks.",
        {"type": "object", "properties": {
            "operation": {"type": "string", "enum": ["update", "pause", "resume", "cancel", "take_control"]},
            "activity_id": {"type": "string"}, "spec": {"type": "object"},
        }, "required": ["operation"], "additionalProperties": False},
        manager.control,
        ToolSidecar("reflex_control", mutating=False, source="agent.reflex", tool_type="plan", permission="manage_reflex"),
        projector=reflex_observation_projector,
    ))
    if os.environ.get("MINEBOT_AGENT_EFFORT_ROUTING") == "1":
        registry.register(RegisteredTool(
            "think_deeply",
            "Ask for your next turn to think at the deepest effort. Most turns think briefly so you answer the "
            "world quickly; turns while the Body works a plan, or that start a new plan, already think longer. Use "
            "this when you want more: before setting a long plan, after a setback you want to understand, or when "
            "the plan ladder no longer fits. The next turn starts as soon as this one ends; while the Body is "
            "just hurt or a hostile mob is close, it waits for a calmer turn. The Body keeps going meanwhile, and "
            "the reflex layer still protects it. Give what you want to think through.",
            {"type": "object", "properties": {"about": {"type": "string", "maxLength": 400}},
             "required": ["about"], "additionalProperties": False},
            manager.request_deep_thought,
            ToolSidecar("reflex_read", mutating=False, source="agent.reflex", tool_type="plan", permission="observe_reflex"),
        ))
    registry.register(RegisteredTool(
        "read_reflex", "Read the annotated plan tree, real results, and current Body control.",
        {"type": "object", "properties": {"activity_id": {"type": "string"}}, "additionalProperties": False},
        lambda params: manager.read(params.get("activity_id")),
        ToolSidecar("reflex_read", mutating=False, source="agent.reflex", tool_type="plan", permission="observe_reflex"),
        projector=reflex_observation_projector,
    ))


__all__ = ["ReflexActivity", "ReflexManager", "register_reflex_tools"]
