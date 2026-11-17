"""Result protection at lifecycle/deadline edges; no BodyLoop methods are stubbed."""
import asyncio
import inspect
import time
from collections import deque
from types import SimpleNamespace

import pytest

from minebot.app.body_control import BodyControl
from minebot.app.body_loop import BodyLoop
from minebot.app.decision_client import DecisionAnswer
from minebot.app.execution_snapshot import ExecutionSnapshots
from minebot.app.runner import AgentRuntime
from minebot.app.work_runtime import WorkPlanExecutor
from minebot.contract import Event


FACTS = {"position": [10, 64, 10], "health": 20, "food": 20, "oxygen": 300,
         "inventory": {"oak_log": 4}, "nearby_entities": [
             {"id": "z1", "name": "Zombie", "hostile": True, "dist": 4,
              "pos": [14, 64, 10]}]}


class ProtectionStillRunning(AssertionError):
    pass


class ProtectionWorkerOrphaned(AssertionError):
    pass


class ObservedQuestions(deque):
    """Record queue occupancy/futures without replacing any consumer method."""

    def __init__(self):
        super().__init__()
        self.futures, self.peak = [], 0

    def append(self, item):
        super().append(item)
        self.futures.append(item[0])
        self.peak = max(self.peak, len(self))


def _await_chain(coro):
    names = []
    while inspect.iscoroutine(coro):
        names.append(coro.cr_code.co_name)
        coro = coro.cr_await
    return names


class Harness:
    """A synchronous Body read and a controllable writer lane, like the runtime tests.

    Admission, supervision, terminal arbitration and run_tool's finally are real.
    Only the physical lane and native stop are simulated; stop cancels the lane
    worker, never the result-judging caller on behalf of BodyLoop.
    """

    def __init__(self, *, repeat=False, stale=False):
        self.entered, self.release, self.cancelled = (asyncio.Event() for _ in range(3))
        self.repeat, self.stale = repeat, stale
        self.requests, self.events, self.stops, self.protections = [], [], [], []
        self.workers, self.depths = set(), []
        self.original_calls = 0
        self.late_settlement = None
        self.new_entered, self.new_release = asyncio.Event(), asyncio.Event()
        self.runtime = SimpleNamespace(
            body_control=BodyControl(), execution_snapshots=ExecutionSnapshots(),
            body=SimpleNamespace(get_reflex_facts=lambda: dict(FACTS)),
            _read_lane=SimpleNamespace(run=self.read),
            execution_lane=SimpleNamespace(run=self.write),
            decision_client=SimpleNamespace(ask=self.answer),
            trace=SimpleNamespace(emit=self.emit),
            registry=SimpleNamespace(get=self.tool),
            last_known_body_state=dict(FACTS), direct_call_handoffs={},
            cancel_active_execution_with_facts=self.stop_body,
            notify_body_lifecycle_event=lambda event: None,
            notify_reflex_fact=lambda facts: None,
            reflex_manager=SimpleNamespace(yield_life=lambda *a, **k: None,
                                           life_project_running=lambda: None),
        )
        self.loop = BodyLoop(self.runtime)
        self.loop._terminal_questions = ObservedQuestions()
        self.runtime.call_supervisor = self.loop
        self.runtime.run_tool = lambda *a, **k: AgentRuntime.run_tool(self.runtime, *a, **k)

    @staticmethod
    def tool(name):
        return SimpleNamespace(name=name, sidecar=SimpleNamespace(can_mutate_body=True, timeout_s=None))

    @staticmethod
    async def read(fn, *args):
        return fn(*args)

    def emit(self, event, **facts):
        self.events.append((event, facts))
        if event == "call_supervision_request":
            self.requests.append(facts["purpose"])

    async def answer(self, request, **kwargs):
        if self.stale:
            return DecisionAnswer("unknown", None, 0, len(self.requests), stale=True)
        questions = request["questions"]
        # Always pick protection while alive. A re-judge after an event is
        # therefore visible, rather than hidden by an exhausted answer list.
        answers = {"d": {"choice": "in_danger"}, "p": {"choice": "move_away"},
                   "q": {"choice": "move_away"}}
        assert "move_away" in (questions.get("p") or questions["q"])["criteria"]
        return DecisionAnswer("move_away", {"answers": answers}, 0, len(self.requests))

    async def write(self, fn, *args, **kwargs):
        if self.loop.current["tool"] == "new_call":
            self.new_entered.set()
            await self.new_release.wait()
            return {"success": True, "reason": "new_call_done"}
        if self.loop.current["tool"] != "move_away":
            self.original_calls += 1
            return {"success": True, "reason": "collected"}
        worker = asyncio.current_task()
        self.workers.add(worker)
        self.protections.append(self.runtime.body_control.snapshot())
        self.entered.set()
        chains = [_await_chain(task.get_coro()) for task in asyncio.all_tasks()]
        self.depths.append((max(c.count("_protect_result") for c in chains),
                            max(c.count("_follow_default_result") for c in chains),
                            max(map(len, chains))))
        try:
            if self.repeat and len(self.protections) < 8:
                await asyncio.sleep(.001)
            else:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            if self.late_settlement is not None:
                await self.late_settlement()
            raise
        return {"success": True, "reason": "moved_away"}

    async def stop_body(self, reason, **kwargs):
        self.stops.append((reason, kwargs))
        for worker in self.workers:
            if not worker.done():
                worker.cancel()
        self.runtime.body_control.mark_stopping()
        self.runtime.body_control.confirm_native_stopped(True)
        return {"settled": True}

    def start(self, kind, timeout_s):
        async def call(name, arguments, *, call_id, timeout_s):
            context = self.executor.call_context(call_id)
            return await self.runtime.run_tool(
                self.tool(name), lambda: None, self.tool(name), arguments,
                tool_call_id=f"plan-{call_id}", timeout_s=timeout_s,
                claim_holder="agent_plan", supervision_context=context)

        async def state():
            return {key: value for key, value in FACTS.items() if key != "nearby_entities"}

        if kind == "plan":
            plan = {"language": "minebot-work/0.2", "goal": "Get logs", "timeout_s": timeout_s,
                    "work": {"kind": "call", "id": "original", "tool": "collect_resource",
                             "intent": "Have four oak logs", "arguments": {"name": "oak_log", "count": 4}}}
            self.executor = WorkPlanExecutor(plan, call=call, state=state,
                                             result_judge=self.loop.decide_plan_result)
            return asyncio.create_task(self.executor.run())
        return asyncio.create_task(self.runtime.run_tool(
            self.tool("collect_resource"), lambda: None, self.tool("collect_resource"), {},
            tool_call_id="original", timeout_s=timeout_s,
            supervision_context={"intent": "Have four oak logs"}))

    async def wait_entered(self):
        await asyncio.wait_for(self.entered.wait(), 1)
        # Exercise loss while the protective call has a registered monitor,
        # beyond the fast-completion race window used by CallSupervisor.

        async def monitored():
            while self.loop._active_monitor is None:
                await asyncio.sleep(.001)

        await asyncio.wait_for(monitored(), .5)
        assert self.protections[-1].holder == "threat"
        assert self.stops[0] == ("direct_call:decision_default_tool",
                                 {"cancel_reflex": False, "invalidate_generation": False})

    async def assert_clean(self):
        assert all(answer.done() for answer, _, _ in self.loop._terminal_questions)
        assert all(answer.done() for answer in self.loop._terminal_questions.futures)
        assert not {"original", "plan-original"} & self.loop._result_pending.keys()
        assert self.runtime.body_control.snapshot().holder != "threat"
        assert all(task.done() for task in self.workers)
        # Stop only the idle lifecycle, after checking its queues and workers;
        # stop's clearing of the result slots must not hide a missing release.
        await self.loop.stop()
        await asyncio.sleep(0)
        assert not (asyncio.all_tasks() - {asyncio.current_task()})

    async def cleanup(self, job):
        # Used even on failure so asyncio.run's implicit cancellation cannot
        # conceal the leak under test or contaminate the following case.
        job.cancel()
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(job, *self.workers, return_exceptions=True)
        await self.loop.stop()
        leaked = asyncio.all_tasks() - {asyncio.current_task()}
        for task in leaked:
            task.cancel()
        await asyncio.gather(*leaked, return_exceptions=True)


def _assert_unknown(harness, kind, outcome):
    if kind == "plan":
        assert outcome.status in {"waiting", "blocked"}
        assert harness.executor.records["original"].decision["choice"] == "unknown"
        assert harness.executor.records["original"].status != "done"
    else:
        # The call's own result is not rewritten (a hand-back never rewrites a
        # physical result); the Agent learns the judgement did not finish and
        # what the protection did.
        assert outcome.get("reason") == "collected"
        facts = outcome.get("metrics", {}).get("direct_call_handoff", {}).get("facts", {})
        if harness.protections:
            assert facts["decision"]["choice"] == "unknown"
            assert facts["default_action"]["tool"] == "move_away"
        else:
            assert facts.get("decision", {}).get("choice", "unknown") == "unknown"


@pytest.mark.parametrize("kind", ["direct", "plan"])
def test_session_stop_cancels_result_protection(kind):
    async def run():
        h = Harness()
        job = h.start(kind, 10)
        try:
            await h.wait_entered()
            await h.loop.stop()
            done, _ = await asyncio.wait({job}, timeout=.15)
            if not done:
                raise ProtectionStillRunning("session stop left the result protection running")
            outcome = await job
            await h.assert_clean()
            _assert_unknown(h, kind, outcome)
        finally:
            await h.cleanup(job)
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["direct", "plan"])
def test_original_deadline_cancels_result_protection(kind):
    async def run():
        h = Harness()
        started = time.monotonic()
        job = h.start(kind, .08)
        try:
            await h.wait_entered()
            outcome = await asyncio.wait_for(asyncio.shield(job), .5)
            assert h.cancelled.is_set(), "the original deadline must cancel the physical call"
            assert time.monotonic() - started < .5
            assert h.original_calls == 1 and len(h.protections) == 1
            await h.assert_clean()
            _assert_unknown(h, kind, outcome)
        finally:
            await h.cleanup(job)
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["direct", "plan"])
def test_deadline_during_completion_grace_cancels_the_protection_worker(kind):
    async def run():
        h = Harness()
        job = h.start(kind, .015)
        try:
            await asyncio.wait_for(h.entered.wait(), 1)
            assert h.loop._active_monitor is None
            outcome = await asyncio.wait_for(asyncio.shield(job), .5)
            if not h.cancelled.is_set() or any(not task.done() for task in h.workers):
                raise ProtectionWorkerOrphaned("deadline left the protective writer running in completion grace")
            await h.assert_clean()
            _assert_unknown(h, kind, outcome)
        finally:
            await h.cleanup(job)
    asyncio.run(run())


@pytest.mark.parametrize("event,kind", [
    ("death", "direct"), ("death", "plan"),
    ("world_disconnected", "direct"), ("world_disconnected", "plan"),
])
def test_lifecycle_loss_cancels_protection_without_rejudging(kind, event):
    async def run():
        h = Harness()
        job = h.start(kind, 10)
        try:
            await h.wait_entered()
            h.loop._queue_lifecycle_event(Event(seq=1, tick=1, bot="Bot", name=event,
                                               data={"health": 0} if event == "death" else {}))
            done, _ = await asyncio.wait({job}, timeout=.3)
            assert h.cancelled.is_set(), f"{event} did not cancel the physical protection"
            if not done:
                raise ProtectionStillRunning(f"{event} left the result protection running")
            outcome = await job
            assert h.requests == ["result_frame" if kind == "direct" else "plan_result_frame"]
            assert any(name == ("reflex_death" if event == "death" else "body_connection")
                       for name, _ in h.events)
            await h.assert_clean()
            _assert_unknown(h, kind, outcome)
        finally:
            await h.cleanup(job)
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["direct", "plan"])
def test_stale_result_answer_is_unknown_and_starts_no_protection(kind):
    async def run():
        h = Harness(stale=True)
        job = h.start(kind, 1)
        try:
            outcome = await asyncio.wait_for(asyncio.shield(job), .5)
            assert not h.protections and not h.stops
            assert h.requests == ["result_frame" if kind == "direct" else "plan_result_frame"]
            assert [facts["choice"] for event, facts in h.events
                    if event == "call_supervision_result"] == ["unknown"]
            await h.assert_clean()
            _assert_unknown(h, kind, outcome)
        finally:
            await h.cleanup(job)
    asyncio.run(run())


def test_old_protection_finally_preserves_a_new_generation_of_the_same_claim():
    async def run():
        h = Harness()
        newer, new_jobs = [], []

        async def replace_claim():
            old = h.runtime.body_control.snapshot()
            h.runtime.body_control.mark_stopping(old.claim_id)
            h.runtime.body_control.confirm_native_stopped(True)
            # A real new call reuses the id, so settle(claim_id=...) alone
            # cannot save this test. Hold it live past the old runner's finally.
            new_jobs.append(asyncio.create_task(h.runtime.run_tool(
                h.tool("new_call"), lambda: None, h.tool("new_call"), {},
                tool_call_id="new-call", body_claim_id=old.claim_id,
                supervision_context={"intent": "Start new work", "is_plan": True})))
            await h.new_entered.wait()
            newer.append(h.runtime.body_control.snapshot())

        h.late_settlement = replace_claim
        job = h.start("plan", .08)
        try:
            await h.wait_entered()
            outcome = await asyncio.wait_for(asyncio.shield(job), .5)
            assert h.cancelled.is_set() and len(newer) == 1
            new = newer[0]
            assert new.generation > h.protections[0].generation
            assert h.runtime.body_control.owns(new.claim_id, new.generation)
            assert h.runtime.body_control.snapshot() == new
            _assert_unknown(h, "plan", outcome)
            h.new_release.set()
            assert (await new_jobs[0])["success"] is True
            assert h.runtime.body_control.snapshot().holder == "none"
            # This stand-in plan call has no containing executor to judge it.
            await h.loop.decide_plan_result(
                call_id="new-call", tool=h.tool("new_call"), params={},
                plan_context={"intent": "Start new work"},
                decision_context={"tool_result": {"success": True}},
                question={"state": {}, "questions": {"q": {"criteria": {"next_step": "Done"}}}})
            await h.assert_clean()
        finally:
            h.new_release.set()
            for task in new_jobs:
                task.cancel()
            await asyncio.gather(*new_jobs, return_exceptions=True)
            await h.cleanup(job)
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["direct", "plan"])
def test_repeated_protection_is_iterative_and_bounded_by_original_deadline(kind):
    async def run():
        h = Harness(repeat=True)
        job = h.start(kind, .15)
        try:
            outcome = await asyncio.wait_for(asyncio.shield(job), .6)
            assert h.original_calls == 1 and len(h.protections) >= 5
            assert h.requests[0] == ("result_frame" if kind == "direct" else "plan_result_frame")
            # The same result question again after each protection, never a nested wrapper.
            assert set(h.requests) == {"result_frame" if kind == "direct" else "plan_result_frame"}
            assert max(p for p, _, _ in h.depths) <= 1
            assert max(f for _, f, _ in h.depths) <= 1
            depths = [depth for _, _, depth in h.depths[1:]]
            assert max(depths) - min(depths) <= 2
            assert h.loop._terminal_questions.peak == 1
            assert len(h.loop._terminal_questions.futures) >= 5
            await h.assert_clean()
            _assert_unknown(h, kind, outcome)
        finally:
            await h.cleanup(job)
    asyncio.run(run())
