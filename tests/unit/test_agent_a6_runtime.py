import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from minebot.app.memory import MemoryWorkspace
from minebot.app.observation_artifacts import PersistentToolObservationArchive
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from agents import RunContextWrapper

from minebot.app.runner import RuntimeRunContext, RuntimeTrace, _model_tool_payload, sdk_tool_for
from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.app.skills import SkillCatalog, SkillWorkspace
from minebot.app.tasks import TaskWorkspace
from minebot.app.wiki import WikiKnowledge, WikiTransport, WikiUnavailable
from minebot.brain.context import AgentContext
from minebot.brain.lifecycle import LifecycleState
from minebot.brain.modes import ModeRuntime
from minebot.brain.progress import ProgressAuthority
from minebot.brain.registry import RegisteredTool, ToolSidecar, WeldContext
from minebot.contract import BodyState, PerceptionResult, Region, Result, ToolResult


REGION = Region("test", (-64, -64, -64), (64, 320, 64))
A6_TOOLS = {
    "search_memory",
    "read_memory",
    "write_memory",
    "update_memory",
    "delete_memory",
    "list_skills",
    "read_skill",
    "load_skill",
    "create_skill",
    "update_skill",
    "delete_skill",
    "wiki_search",
    "wiki_read",
}

LEARNED_SKILL_BODY = """# Verify Safe Position

## Use When

Use this after repeated movement evidence requires a fresh position check.

## Do Not Use When

Do not use it when no physical movement occurred.

## Method

Read authoritative state and compare the current position with the objective.

## Evidence Of Success

Require the authoritative position and matching terminal event.

## Failure And Adaptation

Re-observe after a typed transient failure instead of assuming movement.

## Boundaries

Never bypass governance, hide tools, or replace Body truth.
"""


def _body():
    body = Mock()
    body.bot_name = "Bot1"
    return body


class RuntimeBody:
    bot_name = "Bot1"

    def get_state(self):
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
        return PerceptionResult(self.bot_name, scope, "perception", True, True, {})

    def interrupt(self, reason=None):
        return Result(None, self.bot_name, "result", True, True, True)


class OutageTransport(WikiTransport):
    def request(self, url, headers, timeout_s):
        raise WikiUnavailable("wiki_transport_error:TimeoutError", retryable=True)


async def _invoke(agent, context, name, params):
    class Wrapper:
        def __init__(self, value):
            self.context = value

    tool = next(item for item in agent.tools if item.name == name)
    return await tool.on_invoke_tool(Wrapper(context), json.dumps(params))


class A6RuntimeIntegrationTests(unittest.TestCase):
    def test_maintenance_can_create_skill_without_mutating_or_hiding_body_tools(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        skills = SkillWorkspace(store, scope, SkillCatalog())
        parts = build_phase1_agent_runtime(
            body=RuntimeBody(),
            goal_text="",
            model_provider=None,
            config=Phase1RuntimeConfig(natural_region=REGION, skill_workspace=skills),
        )
        skills.set_activation_owner(owner_kind="maintenance", owner_id="maintenance-1")
        context = RuntimeRunContext(
            agent_context=parts.context,
            weld_context=parts.runtime.weld_context,
            profile=parts.modes.profile_for(LifecycleState.ACTIVE),
            runtime=parts.runtime,
            body_actions_allowed=False,
        )

        created = asyncio.run(
            _invoke(
                parts.runtime.agent,
                context,
                "create_skill",
                {
                    "name": "verify-safe-position",
                    "description": "Verify current position when repeated movement evidence may be stale after interruption.",
                    "tools": ["read_state"],
                    "body": LEARNED_SKILL_BODY,
                    "evidence_refs": ["observation:movement-1"],
                },
            )
        )

        self.assertTrue(created["success"])
        self.assertIsNotNone(skills.read("verify-safe-position"))
        self.assertIn("move_to", parts.registry.names())
        self.assertFalse(parts.registry.sidecar("create_skill").can_mutate_body)
        parts.runtime.close()
        store.close()

    def test_descriptors_precede_tool_use_and_load_refreshes_next_model_request(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        skills = SkillWorkspace(store, scope, SkillCatalog())
        parts = build_phase1_agent_runtime(
            body=RuntimeBody(),
            goal_text="prepare tools",
            model_provider=None,
            config=Phase1RuntimeConfig(
                natural_region=REGION,
                skill_workspace=skills,
            ),
        )
        profile = parts.modes.profile_for(LifecycleState.ACTIVE)
        run_context = RuntimeRunContext(
            agent_context=parts.context,
            weld_context=parts.runtime.weld_context,
            profile=profile,
            runtime=parts.runtime,
            instruction_preamble=parts.context.turn_preamble(),
        )
        wrapper = RunContextWrapper(run_context)

        before = parts.runtime._instructions(wrapper, parts.runtime.agent)
        skills.set_activation_owner(owner_kind="turn", owner_id="turn-1")
        skills.load("resource-progression")
        after = parts.runtime._instructions(wrapper, parts.runtime.agent)

        self.assertIn("AVAILABLE_SKILLS", before)
        self.assertIn("resource-progression", before)
        self.assertIn("skill-authoring", before)
        self.assertNotIn("ACTIVE_SKILLS", before)
        self.assertIn("ACTIVE_SKILLS", after)
        self.assertIn("# Resource Progression", after)
        parts.runtime.close()
        store.close()

    def test_missing_pinned_version_yields_before_model_instead_of_switching_head(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        tasks = TaskWorkspace(store, scope)
        task = tasks.start("prepare tools", source="test")
        skills = SkillWorkspace(store, scope, SkillCatalog(), task_workspace=tasks)
        parts = build_phase1_agent_runtime(
            body=RuntimeBody(),
            goal_text=task.goal_text,
            model_provider=None,
            config=Phase1RuntimeConfig(
                natural_region=REGION,
                task_workspace=tasks,
                skill_workspace=skills,
            ),
        )
        skills.set_activation_owner(
            owner_kind="task",
            owner_id=task.task_id,
            task_id=task.task_id,
        )
        _document, activation = skills.load("resource-progression")
        store._connection.execute(
            "UPDATE skill_activations SET skill_version = 'sha256:missing' WHERE activation_id = ?",
            (activation.activation_id,),
        )
        store._connection.commit()
        calls = []

        async def runner(*args, **kwargs):
            calls.append("model")
            return {"ok": True}

        parts.runtime.runner_run = runner
        parts.runtime.runner_run_streamed = None
        outcome = asyncio.run(parts.runtime.run_turn())

        self.assertEqual(outcome.status, "yielded")
        self.assertEqual(outcome.message, "skill_pinned_version_unavailable")
        self.assertEqual(calls, [])
        self.assertTrue(
            any(event["event"] == "skill_context_recovery_required" for event in parts.runtime.trace.snapshot())
        )
        parts.runtime.close()
        store.close()

    def test_corrupt_learned_version_yields_typed_truth_before_model(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        tasks = TaskWorkspace(store, scope)
        task = tasks.start("prepare tools", source="test")
        skills = SkillWorkspace(store, scope, SkillCatalog(), task_workspace=tasks)
        parts = build_phase1_agent_runtime(
            body=RuntimeBody(),
            goal_text=task.goal_text,
            model_provider=None,
            config=Phase1RuntimeConfig(
                natural_region=REGION,
                task_workspace=tasks,
                skill_workspace=skills,
            ),
        )
        learned = skills.create(
            name="verify-safe-position",
            description="Verify current position when repeated movement evidence may be stale after interruption.",
            tools=["read_state"],
            body=LEARNED_SKILL_BODY,
            evidence_refs=["observation:movement-1"],
        )
        skills.set_activation_owner(
            owner_kind="task",
            owner_id=task.task_id,
            task_id=task.task_id,
        )
        skills.load(learned.name)
        store._connection.execute(
            "UPDATE skill_versions SET body = 'corrupt' WHERE skill_id = ?",
            (learned.skill_id,),
        )
        store._connection.commit()
        calls = []

        async def runner(*args, **kwargs):
            calls.append("model")
            return {"ok": True}

        parts.runtime.runner_run = runner
        parts.runtime.runner_run_streamed = None
        outcome = asyncio.run(parts.runtime.run_turn())

        self.assertEqual(outcome.status, "yielded")
        self.assertEqual(outcome.message, "skill_version_corrupt")
        self.assertEqual(calls, [])
        parts.runtime.close()
        store.close()

    def test_maintenance_keeps_body_tool_visible_but_denies_execution(self):
        body = RuntimeBody()
        calls: list[dict[str, object]] = []

        def move(params):
            calls.append(dict(params))
            return ToolResult(True, "completed", False)

        sdk_tool = sdk_tool_for(
            RegisteredTool(
                name="move_step",
                description="Move one step.",
                input_schema={"type": "object", "properties": {}},
                callable=move,
                sidecar=ToolSidecar(
                    progress_key="move_step",
                    mutating=True,
                    permission="move",
                    body_scope=("navigation",),
                ),
            )
        )
        trace = RuntimeTrace()
        context = RuntimeRunContext(
            agent_context=AgentContext(system_prompt="sys", goal_text=""),
            weld_context=WeldContext(
                body=body,
                authority=ProgressAuthority(),
                goal_text="",
            ),
            profile=ModeRuntime().profile_for(LifecycleState.ACTIVE),
            trace=trace,
            body_actions_allowed=False,
        )

        class Wrapper:
            def __init__(self, value):
                self.context = value

        wrapper = Wrapper(context)
        self.assertTrue(sdk_tool.is_enabled(wrapper, object()))
        out = asyncio.run(sdk_tool.on_invoke_tool(wrapper, "{}"))

        self.assertFalse(out["success"])
        self.assertEqual(out["reason"], "body_action_denied_during_maintenance")
        self.assertEqual(calls, [])
        self.assertTrue(
            any(
                event["event"] == "tool_policy_denied"
                and event["tool"] == "move_step"
                for event in trace.snapshot()
            )
        )

    def test_a6_tools_extend_the_shared_registry_without_changing_body_visibility(self):
        baseline = build_phase1_agent_runtime(
            body=_body(),
            goal_text="prepare for the End",
            model_provider=None,
            config=Phase1RuntimeConfig(natural_region=REGION),
        )
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        memory = MemoryWorkspace(store, scope)
        skills = SkillWorkspace(store, scope, SkillCatalog())
        wiki = WikiKnowledge(store)
        runtime = build_phase1_agent_runtime(
            body=_body(),
            goal_text="prepare for the End",
            model_provider=None,
            config=Phase1RuntimeConfig(
                natural_region=REGION,
                memory_workspace=memory,
                skill_workspace=skills,
                wiki_knowledge=wiki,
            ),
        )
        try:
            baseline_names = set(baseline.registry.names())
            runtime_names = set(runtime.registry.names())
            self.assertEqual(runtime_names - baseline_names, A6_TOOLS)
            self.assertEqual(runtime_names & baseline_names, baseline_names)

            expected_permissions = {
                "search_memory": "read_memory",
                "read_memory": "read_memory",
                "write_memory": "write_memory",
                "update_memory": "write_memory",
                "delete_memory": "delete_memory",
                "list_skills": "read_skill_catalog",
                "read_skill": "read_skill",
                "load_skill": "load_skill",
                "create_skill": "write_skill",
                "update_skill": "write_skill",
                "delete_skill": "delete_skill",
                "wiki_search": "read_external_knowledge",
                "wiki_read": "read_external_knowledge",
            }
            for name, permission in expected_permissions.items():
                with self.subTest(tool=name):
                    sidecar = runtime.registry.sidecar(name)
                    self.assertEqual(sidecar.permission, permission)
                    self.assertFalse(sidecar.mutating)
                    self.assertEqual(sidecar.body_scope, ())
        finally:
            baseline.runtime.close()
            runtime.runtime.close()
            store.close()

    def test_a6_model_projection_keeps_decision_facts_bounded_and_queryable(self):
        memory_payload = _model_tool_payload(
            "search_memory",
            {
                "success": True,
                "reason": "memory_search",
                "canRetry": False,
                "metrics": {
                    "query": "portal",
                    "candidate_count": 1,
                    "complete": True,
                    "results": [
                        {
                            "memory_id": "memory-1",
                            "revision": 2,
                            "kind": "spatial",
                            "source": "observed",
                            "subject_key": "place:portal",
                            "title": "Portal",
                            "excerpt": "The portal is beside the tower.",
                            "match_lanes": ["terms", "trigram"],
                        }
                    ],
                },
            },
            trace_ref="trace-memory",
            observation_handle="observation-memory",
        )
        self.assertEqual(memory_payload["summary"]["results"][0]["memory_id"], "memory-1")
        self.assertEqual(
            memory_payload["summary"]["results"][0]["excerpt"],
            "The portal is beside the tower.",
        )
        self.assertEqual(memory_payload["observationHandle"], "observation-memory")

        skill_text = "method\n" * 2000
        skill_payload = _model_tool_payload(
            "load_skill",
            {
                "success": True,
                "reason": "skill_loaded",
                "canRetry": False,
                "metrics": {
                    "name": "recovery-and-continuation",
                    "version": "sha256:test",
                    "instructions": skill_text,
                    "complete": True,
                },
            },
            trace_ref="trace-skill",
            observation_handle="observation-skill",
        )
        self.assertLessEqual(len(skill_payload["summary"]["instructions"]), 8000)
        self.assertFalse(skill_payload["summary"]["instructions_complete"])
        self.assertFalse(skill_payload["projection"]["complete"])
        self.assertTrue(skill_payload["projection"]["queryable"])

        created_payload = _model_tool_payload(
            "create_skill",
            {
                "success": True,
                "reason": "skill_created",
                "canRetry": False,
                "metrics": {
                    "name": "verify-safe-position",
                    "description": "Verify current position when movement evidence may be stale.",
                    "version": "sha256:created",
                    "revision": 1,
                    "origin": "learned",
                    "skill_markdown": LEARNED_SKILL_BODY,
                    "complete": True,
                },
            },
            trace_ref="trace-skill-create",
            observation_handle="observation-skill-create",
        )
        self.assertNotIn("skill_markdown", created_payload["summary"])
        self.assertFalse(created_payload["projection"]["complete"])
        self.assertTrue(created_payload["projection"]["queryable"])

        wiki_payload = _model_tool_payload(
            "wiki_read",
            {
                "success": True,
                "reason": "wiki_read",
                "canRetry": False,
                "metrics": {
                    "title": "Trading",
                    "markdown": "# Trading\n\n" + ("advisory text " * 1000),
                    "source": "minecraft.wiki",
                    "source_url": "https://minecraft.wiki/w/Trading",
                    "revision_id": 42,
                    "complete": True,
                    "stale": False,
                    "advisory": True,
                },
            },
            trace_ref="trace-wiki",
            observation_handle="observation-wiki",
        )
        self.assertEqual(wiki_payload["summary"]["source"], "minecraft.wiki")
        self.assertTrue(wiki_payload["summary"]["advisory"])
        self.assertLessEqual(len(wiki_payload["summary"]["markdown"]), 6000)
        self.assertFalse(wiki_payload["summary"]["markdown_complete"])

    def test_restart_scenario_uses_normal_sdk_tools_and_continues_through_wiki_outage(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "agent-state.sqlite3"
            scope = RuntimeScope("server", "world", "Bot1")
            first_store = RuntimeStateStore(state_path)
            first_memory = MemoryWorkspace(first_store, scope)
            first_tasks = TaskWorkspace(first_store, scope)
            first_tasks.start("remember the base approach", source="test")
            first_skills = SkillWorkspace(
                first_store,
                scope,
                SkillCatalog(),
                task_workspace=first_tasks,
            )
            first_archive = PersistentToolObservationArchive(first_store, scope)
            first = build_phase1_agent_runtime(
                body=RuntimeBody(),
                goal_text="remember the base approach",
                model_provider=None,
                config=Phase1RuntimeConfig(
                    natural_region=REGION,
                    observation_archive=first_archive,
                    task_workspace=first_tasks,
                    memory_workspace=first_memory,
                    skill_workspace=first_skills,
                    wiki_knowledge=WikiKnowledge(
                        first_store,
                        transport=OutageTransport(),
                    ),
                ),
            )
            first_outputs = []

            async def first_runner(agent, input_text, *, context=None, **kwargs):
                first_outputs.append(
                    await _invoke(agent, context, "wiki_search", {"query": "trial chambers"})
                )
                first_outputs.append(
                    await _invoke(
                        agent,
                        context,
                        "write_memory",
                        {
                            "kind": "episodic",
                            "source": "player_told",
                            "title": "Base approach",
                            "content": "Approach the base from the east ridge.",
                            "subject_key": "route:base-approach",
                        },
                    )
                )
                first_outputs.append(
                    await _invoke(
                        agent,
                        context,
                        "load_skill",
                        {"name": "recovery-and-continuation"},
                    )
                )
                return {"ok": True}

            first.runtime.runner_run = first_runner
            first.runtime.runner_run_streamed = None
            first_outcome = asyncio.run(first.runtime.run_turn())
            self.assertEqual(first_outcome.status, "completed_turn")
            self.assertEqual(first_outputs[0]["reason"], "wiki_unavailable")
            self.assertFalse(first_outputs[0]["success"])
            self.assertTrue(first_outputs[1]["success"])
            self.assertTrue(first_outputs[2]["success"])
            first.runtime.close()
            first_store.close()

            second_store = RuntimeStateStore(state_path)
            second_memory = MemoryWorkspace(second_store, scope)
            second_tasks = TaskWorkspace(second_store, scope)
            second_skills = SkillWorkspace(
                second_store,
                scope,
                SkillCatalog(),
                task_workspace=second_tasks,
            )
            self.assertEqual(len(second_skills.activations()), 1)
            second_archive = PersistentToolObservationArchive(second_store, scope)
            second = build_phase1_agent_runtime(
                body=RuntimeBody(),
                goal_text="continue from durable context",
                model_provider=None,
                config=Phase1RuntimeConfig(
                    natural_region=REGION,
                    observation_archive=second_archive,
                    task_workspace=second_tasks,
                    memory_workspace=second_memory,
                    skill_workspace=second_skills,
                    wiki_knowledge=WikiKnowledge(
                        second_store,
                        transport=OutageTransport(),
                    ),
                ),
            )
            second_outputs = []
            second_skill_context = []

            async def second_runner(agent, input_text, *, context=None, **kwargs):
                second_skill_context.append(context.agent_context.skill_preamble())
                second_outputs.append(
                    await _invoke(agent, context, "search_memory", {"query": "east ridge"})
                )
                return {"ok": True}

            second.runtime.runner_run = second_runner
            second.runtime.runner_run_streamed = None
            second_outcome = asyncio.run(second.runtime.run_turn())
            self.assertEqual(second_outcome.status, "completed_turn")
            self.assertEqual(
                second_outputs[0]["summary"]["results"][0]["subject_key"],
                "route:base-approach",
            )
            self.assertIn("AVAILABLE_SKILLS", second_skill_context[0])
            self.assertIn("ACTIVE_SKILLS", second_skill_context[0])
            self.assertIn("Recovery And Continuation", second_skill_context[0])
            other_scope = MemoryWorkspace(
                second_store,
                RuntimeScope("server", "world", "OtherBot"),
            )
            self.assertEqual(other_scope.search({"query": "east ridge"})["candidate_count"], 0)
            second.runtime.close()
            second_store.close()


if __name__ == "__main__":
    unittest.main()
