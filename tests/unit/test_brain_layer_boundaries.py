"""Structural guards for the brain layer (brain-cognitive-framework.md §12 H3).

These tests make architectural drift a test failure instead of a review
opinion:

1. ``minebot/brain/`` stays framework-agnostic: it imports only
   ``minebot.contract`` and sibling ``brain`` modules — never the agent SDK,
   ``app``, ``body``, ``game``, or ``camera``.
2. The framework binding ring (``app/runner.py``) stays capability-free: it
   contains no registered tool-name string literals beyond a justified
   structural whitelist, and it never imports Body transaction modules.
3. Tools with dedicated model-visible projections carry their projector at
   the registration site (``RegisteredTool.projector``), so per-tool
   projection knowledge cannot silently return to the runner.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest
from unittest.mock import Mock

from minebot.app.conversation_tools import register_conversation_archive_tools
from minebot.app.memory import register_memory_tools
from minebot.app.observation_artifacts import register_tool_observation_tools
from minebot.app.phase1_runtime import Phase1RuntimeConfig, build_phase1_registry
from minebot.app.skills import register_skill_tools
from minebot.app.tasks import register_task_tools
from minebot.app.wiki import register_wiki_tools
from minebot.brain.registry import ToolRegistry
from minebot.contract import Region


BRAIN_ROOT = Path("minebot/brain")
RUNNER_PATH = Path("minebot/app/runner.py")

# Import roots minebot/brain is allowed to reach. Everything else that starts
# with ``minebot.`` — and the agent SDK — is a layering violation
# (agent-layer-architecture.md §9; brain-cognitive-framework.md §1 C6/§3.2).
ALLOWED_BRAIN_ROOTS = ("minebot.contract", "minebot.brain")
FORBIDDEN_BRAIN_ROOTS = ("minebot.app", "minebot.body", "minebot.game", "minebot.camera", "agents")

# Structural tool-name literals the runner may keep. Every entry must be
# justified here; adding one is an architecture decision, not a convenience.
#   read_state — the recovery/idle probes ask the Body for authoritative state
#     through the registry by name; this is spine bookkeeping, not a
#     capability-specific branch.
RUNNER_TOOL_NAME_WHITELIST = frozenset({"read_state"})

# Tools whose model-visible summaries are dedicated projections. Their
# projector must be registered next to the tool itself.
PROJECTED_WORKSPACE_TOOLS = frozenset(
    {
        "read_task",
        "update_plan",
        "checkpoint_task",
        "query_conversation_archive",
        "read_conversation_archive",
        "query_tool_observations",
        "read_tool_observation",
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
)
PROJECTED_PHASE1_TOOLS = frozenset({"explore_for", "collect_block_domain"})


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)
    return modules


def _phase1_registry() -> ToolRegistry:
    body = Mock()
    body.bot_name = "Bot1"
    return build_phase1_registry(
        body,
        Phase1RuntimeConfig(natural_region=Region("test", (-64, -64, -64), (64, 320, 64))),
    )


def _workspace_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_task_tools(registry, Mock())
    register_conversation_archive_tools(registry, Mock())
    register_tool_observation_tools(registry, Mock())
    register_memory_tools(registry, Mock())
    register_skill_tools(registry, Mock())
    register_wiki_tools(registry, Mock())
    return registry


class BrainImportBoundaryTests(unittest.TestCase):
    def test_brain_imports_only_contract_and_sibling_brain_modules(self) -> None:
        offenders: list[str] = []
        for path in sorted(BRAIN_ROOT.rglob("*.py")):
            for module in _imported_modules(path):
                if module.startswith(FORBIDDEN_BRAIN_ROOTS):
                    offenders.append(f"{path}:{module}")
                elif module.startswith("minebot") and not module.startswith(ALLOWED_BRAIN_ROOTS):
                    offenders.append(f"{path}:{module}")
        self.assertEqual(offenders, [])


class BindingRingBoundaryTests(unittest.TestCase):
    def test_runner_imports_no_body_transaction_modules(self) -> None:
        offenders = [
            module
            for module in _imported_modules(RUNNER_PATH)
            if module.startswith("minebot.body")
        ]
        self.assertEqual(offenders, [])

    def test_runner_has_no_registered_tool_name_literals(self) -> None:
        registered = set(_phase1_registry().names()) | set(_workspace_registry().names())
        tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"), filename=str(RUNNER_PATH))
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        offenders = sorted((literals & registered) - RUNNER_TOOL_NAME_WHITELIST)
        self.assertEqual(
            offenders,
            [],
            "runner.py must stay capability-free: move per-tool logic to the "
            "owning module and register it (RegisteredTool.projector or the "
            "tool callable); extending RUNNER_TOOL_NAME_WHITELIST requires an "
            "architecture justification.",
        )


class ProjectorWiringTests(unittest.TestCase):
    def test_workspace_tools_carry_their_projectors(self) -> None:
        registry = _workspace_registry()
        missing = sorted(
            name
            for name in PROJECTED_WORKSPACE_TOOLS
            if registry.get(name).projector is None
        )
        self.assertEqual(missing, [])

    def test_phase1_tools_carry_their_projectors(self) -> None:
        registry = _phase1_registry()
        missing = sorted(
            name
            for name in PROJECTED_PHASE1_TOOLS
            if registry.get(name).projector is None
        )
        self.assertEqual(missing, [])

    def test_projected_summaries_reach_the_model_payload(self) -> None:
        # End-to-end: the runner threads a registered projector into the
        # model-visible payload without knowing the tool by name.
        from minebot.app.runner import _model_tool_payload

        registry = _workspace_registry()
        tool = registry.get("read_task")
        result = {
            "success": True,
            "reason": "task_read",
            "canRetry": False,
            "metrics": {"task": {"task_id": "t-1", "revision": 3}},
        }
        payload = _model_tool_payload(
            "read_task", result, trace_ref="guard-trace", projector=tool.projector
        )
        self.assertEqual(payload["summary"]["task_artifact"]["task"]["revision"], 3)


if __name__ == "__main__":
    unittest.main()
