#!/usr/bin/env python3
"""Real-model A6 gate for scoped Memory, Wiki knowledge, and Skills."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.config import AppConfigError, provider_registry_from_env  # noqa: E402
from minebot.app.memory import MemoryWorkspace  # noqa: E402
from minebot.app.model_provider import ProviderConfigError  # noqa: E402
from minebot.app.observation_artifacts import PersistentToolObservationArchive  # noqa: E402
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime  # noqa: E402
from minebot.app.runner import RuntimeTrace  # noqa: E402
from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore  # noqa: E402
from minebot.app.skills import SkillCatalog, SkillWorkspace  # noqa: E402
from minebot.app.tasks import TaskWorkspace  # noqa: E402
from minebot.app.wiki import WikiKnowledge  # noqa: E402
from minebot.contract import BodyState, PerceptionResult, Result  # noqa: E402
from minebot.contract import Region  # noqa: E402


SKIP_EXIT_CODE = 77
SUBJECT_KEY = "gate:a6-live-route"
LEARNED_SKILL_NAME = "verify-east-ridge-memory"
REGION = Region("a6-live-gate", (-64, -64, -64), (64, 320, 64))


class ReadOnlyBody:
    bot_name = "A6LiveGate"

    def get_state(self) -> BodyState:
        return BodyState(
            bot=self.bot_name,
            pos=(0.0, 64.0, 0.0),
            yaw=0.0,
            pitch=0.0,
            health=20.0,
            food=20,
            oxygen=300,
            inventory_raw="[]",
            inventory_hash="empty",
            effects=None,
            time=1000,
            weather="clear",
            dimension="overworld",
            complete=True,
        )

    def poll_events(self):
        return []

    def perceive(self, scope, params):
        return PerceptionResult(
            self.bot_name,
            scope,
            "perception",
            True,
            True,
            {},
        )

    def interrupt(self, reason=None):
        return Result(None, self.bot_name, "result", True, True, True)


def _build_runtime(store, scope, provider, *, goal_text, trace):
    tasks = TaskWorkspace(store, scope)
    if tasks.current_task is None:
        tasks.start("A6 learned Skill restart gate", source="test")
    memory = MemoryWorkspace(store, scope)
    skills = SkillWorkspace(store, scope, SkillCatalog(), task_workspace=tasks)
    wiki = WikiKnowledge(store)
    archive = PersistentToolObservationArchive(store, scope)
    parts = build_phase1_agent_runtime(
        body=ReadOnlyBody(),
        goal_text=goal_text,
        model_provider=provider,
        config=Phase1RuntimeConfig(
            natural_region=REGION,
            task_workspace=tasks,
            observation_archive=archive,
            memory_workspace=memory,
            skill_workspace=skills,
            wiki_knowledge=wiki,
        ),
        language="English",
        agent_name="MineBotA6LiveGate",
        trace=trace,
    )
    parts.runtime.max_turns = 20
    return parts, tasks, memory, skills


def _successful_tools(trace: RuntimeTrace) -> set[str]:
    return {
        str(event["tool"])
        for event in trace.snapshot()
        if event.get("event") == "tool_result" and event.get("success") is True
    }


async def run_gate() -> dict[str, object]:
    provider = provider_registry_from_env()
    scope = RuntimeScope("a6-live-gate", "isolated-world", "A6LiveGate")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "agent-state.sqlite3"
            first_store = RuntimeStateStore(state_path)
            first_trace = RuntimeTrace(session_id="a6-live-first")
            first, first_tasks, first_memory, first_skills = _build_runtime(
                first_store,
                scope,
                provider,
                goal_text=(
                    "Call wiki_search for 'diamond ore mining', then wiki_read the "
                    "best matching page. Call write_memory with kind='episodic', "
                    "source='player_told', title='A6 live gate route', content='The "
                    "A6 live gate marker says approach the test base from the east "
                    "ridge.', subject_key='gate:a6-live-route', and "
                    "evidence_ref='test:a6-live-gate'. Without calling list_skills, "
                    "use the always-visible Skill descriptions to identify and load "
                    "the built-in methodology for authoring Skills. Follow it to call "
                    "create_skill for a learned Skill named 'verify-east-ridge-memory' "
                    "with description 'Verify remembered east-ridge routes against "
                    "live world truth when navigation may rely on stale spatial memory.', "
                    "no tool dependencies, evidence_refs=['test:a6-live-gate'], and a "
                    "strict canonical body about verifying memory before navigation. "
                    "Then load the learned Skill. Complete all operations before finishing."
                ),
                trace=first_trace,
            )
            first_outcome = await first.runtime.run_turn(body_actions_allowed=False)
            first_tools = _successful_tools(first_trace)
            expected_first = {
                "wiki_search",
                "wiki_read",
                "write_memory",
                "load_skill",
                "create_skill",
            }
            if not expected_first.issubset(first_tools):
                raise AssertionError(
                    f"first turn missing successful tools: {sorted(expected_first - first_tools)}"
                )
            first_results = first_memory.search({"subject_key": SUBJECT_KEY, "limit": 5})
            if first_results["candidate_count"] != 1:
                raise AssertionError("first turn did not persist the scoped memory")
            if "list_skills" in {
                str(event.get("tool"))
                for event in first_trace.snapshot()
                if event.get("event") == "tool_invoke"
            }:
                raise AssertionError("first turn used list_skills despite descriptor discovery contract")
            learned = first_skills.read(LEARNED_SKILL_NAME)
            if learned is None or learned.origin != "learned":
                raise AssertionError("first turn did not create the learned Skill")
            active_names = {item.skill_name for item in first_skills.activations()}
            if LEARNED_SKILL_NAME not in active_names or "skill-authoring" not in active_names:
                raise AssertionError("first turn did not activate authoring and learned Skills")
            task_id = first_tasks.current_task.task_id
            first.runtime.close()
            first_store.close()

            second_store = RuntimeStateStore(state_path)
            second_trace = RuntimeTrace(session_id="a6-live-second")
            second, second_tasks, second_memory, second_skills = _build_runtime(
                second_store,
                scope,
                provider,
                goal_text=(
                    "After restart, do not call list_skills or load_skill. Use the "
                    "always-visible descriptors to identify the learned Skill about "
                    "east-ridge memory verification and call read_skill on that exact "
                    "name. Call search_memory for 'east ridge test base' and verify "
                    "the returned subject_key is 'gate:a6-live-route'. Finish only "
                    "after both calls; rely on the already ACTIVE methodology."
                ),
                trace=second_trace,
            )
            second_outcome = await second.runtime.run_turn(body_actions_allowed=False)
            second_tools = _successful_tools(second_trace)
            expected_second = {"search_memory", "read_skill"}
            if not expected_second.issubset(second_tools):
                raise AssertionError(
                    f"second turn missing successful tools: {sorted(expected_second - second_tools)}"
                )
            retrieved = second_memory.search({"query": "east ridge test base", "limit": 5})
            subjects = [item["subject_key"] for item in retrieved["results"]]
            if SUBJECT_KEY not in subjects:
                raise AssertionError("persisted memory was not retrievable after restart")
            second_invocations = {
                str(event.get("tool"))
                for event in second_trace.snapshot()
                if event.get("event") == "tool_invoke"
            }
            if "load_skill" in second_invocations or "list_skills" in second_invocations:
                raise AssertionError("restart turn reconstructed Skills through an explicit discovery/load call")
            active_context = second.context.skill_preamble()
            if LEARNED_SKILL_NAME not in active_context or "ACTIVE_SKILLS" not in active_context:
                raise AssertionError("exact active Skill body was not restored before the restart turn")
            if second_tasks.current_task.task_id != task_id:
                raise AssertionError("restart did not preserve the durable activation owner task")
            if LEARNED_SKILL_NAME not in {item.skill_name for item in second_skills.activations()}:
                raise AssertionError("learned Skill activation did not survive restart")
            other_scope = MemoryWorkspace(
                second_store,
                RuntimeScope("a6-live-gate", "isolated-world", "OtherBot"),
            )
            if other_scope.search({"query": "east ridge", "limit": 5})["candidate_count"]:
                raise AssertionError("memory leaked across bot scope")
            other_skills = SkillWorkspace(
                second_store,
                RuntimeScope("a6-live-gate", "isolated-world", "OtherBot"),
                SkillCatalog(),
            )
            if other_skills.read(LEARNED_SKILL_NAME) is not None:
                raise AssertionError("learned Skill leaked across bot scope")
            skill_activation_count = len(second_skills.activations())
            second.runtime.close()
            second_store.close()

            return {
                "first_status": first_outcome.status,
                "first_tools": sorted(first_tools),
                "second_status": second_outcome.status,
                "second_tools": sorted(second_tools),
                "memory_count": len(subjects),
                "skill_activation_count": skill_activation_count,
                "cross_scope_leakage": 0,
            }
    finally:
        await provider.aclose()


def main() -> None:
    try:
        provider = provider_registry_from_env()
        provider.resolve("primary")
    except (AppConfigError, ProviderConfigError) as exc:
        if os.environ.get("MINEBOT_E2E_REQUIRED") == "1":
            raise
        print(f"SKIP: real model provider not configured: {exc}")
        raise SystemExit(SKIP_EXIT_CODE)
    else:
        asyncio.run(provider.aclose())
    print(asyncio.run(run_gate()))


if __name__ == "__main__":
    main()
