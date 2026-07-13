import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from minebot.app.memory import MemoryWorkspace
from minebot.app.observation_artifacts import PersistentToolObservationArchive
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_agent_runtime
from minebot.app.runner import RuntimeRunContext, RuntimeTrace, _model_tool_payload, sdk_tool_for
from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.app.skills import SkillCatalog, SkillWorkspace
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
    "load_skill",
    "wiki_search",
    "wiki_read",
}


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
                "load_skill": "load_skill",
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
            first_skills = SkillWorkspace(first_store, scope, SkillCatalog())
            first_archive = PersistentToolObservationArchive(first_store, scope)
            first = build_phase1_agent_runtime(
                body=RuntimeBody(),
                goal_text="remember the base approach",
                model_provider=None,
                config=Phase1RuntimeConfig(
                    natural_region=REGION,
                    observation_archive=first_archive,
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
            second_skills = SkillWorkspace(second_store, scope, SkillCatalog())
            self.assertEqual(len(second_skills.activations()), 1)
            second_archive = PersistentToolObservationArchive(second_store, scope)
            second = build_phase1_agent_runtime(
                body=RuntimeBody(),
                goal_text="continue from durable context",
                model_provider=None,
                config=Phase1RuntimeConfig(
                    natural_region=REGION,
                    observation_archive=second_archive,
                    memory_workspace=second_memory,
                    skill_workspace=second_skills,
                    wiki_knowledge=WikiKnowledge(
                        second_store,
                        transport=OutageTransport(),
                    ),
                ),
            )
            second_outputs = []

            async def second_runner(agent, input_text, *, context=None, **kwargs):
                second_outputs.append(
                    await _invoke(agent, context, "search_memory", {"query": "east ridge"})
                )
                second_outputs.append(
                    await _invoke(
                        agent,
                        context,
                        "load_skill",
                        {"name": "recovery-and-continuation"},
                    )
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
            self.assertTrue(second_outputs[1]["summary"]["instructions"])
            other_scope = MemoryWorkspace(
                second_store,
                RuntimeScope("server", "world", "OtherBot"),
            )
            self.assertEqual(other_scope.search({"query": "east ridge"})["candidate_count"], 0)
            second.runtime.close()
            second_store.close()


if __name__ == "__main__":
    unittest.main()
