import tempfile
import unittest
from pathlib import Path

from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.app.skill_format import SkillFormatError, parse_skill_markdown
from minebot.app.skills import (
    SkillCatalog,
    SkillCatalogError,
    SkillOperationError,
    SkillWorkspace,
    register_skill_tools,
)
from minebot.app.tasks import TaskWorkspace
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import ToolResult


SKILL_TOOL_NAMES = {
    "list_skills",
    "read_skill",
    "load_skill",
    "create_skill",
    "update_skill",
    "delete_skill",
}


def skill_body(*, title: str = "Safe Route Selection", marker: str = "v1") -> str:
    return f"""# {title}

## Use When

Use this when repeated route evidence shows that one approach is safer. {marker}

## Do Not Use When

Do not use it when current world truth already proves the direct route safe.

## Method

1. Read current state and compare at least two governed candidates.
2. Use `move_to` only after selecting a candidate from current evidence.

## Evidence Of Success

Require authoritative final position and the matching action terminal event.

## Failure And Adaptation

Change candidate after a typed mobility blocker instead of repeating parameters.

## Boundaries

Never bypass block governance, hide tools, or claim success without Body truth.
"""


def bind_workspace(workspace: SkillWorkspace, *, extra_tools: tuple[str, ...] = ()) -> ToolRegistry:
    registry = ToolRegistry()
    register_skill_tools(registry, workspace)
    required = {
        tool
        for name in workspace.catalog.names
        for tool in workspace.catalog.load(name).tools
    }
    for name in sorted(required | set(extra_tools)):
        if name in registry:
            continue
        registry.register(
            RegisteredTool(
                name,
                f"test tool {name}",
                {"type": "object", "properties": {}, "additionalProperties": False},
                lambda _params: ToolResult(True, "ok", False),
                ToolSidecar(
                    name,
                    mutating=False,
                    source="test",
                    tool_type="test",
                    permission=name,
                    body_scope=(),
                    terminal_truth=("test",),
                ),
            )
        )
    workspace.bind_registry(registry)
    return registry


class SkillFormatTests(unittest.TestCase):
    def test_default_catalog_uses_strict_skill_md_and_contains_bootstrap_skill(self):
        catalog = SkillCatalog()

        self.assertEqual(
            catalog.names,
            (
                "recovery-and-continuation",
                "resource-progression",
                "skill-authoring",
            ),
        )
        loaded = catalog.load("resource-progression")
        self.assertTrue(loaded.version.startswith("sha256:"))
        self.assertIn("authoritative inventory", loaded.content)
        self.assertNotIn("/player", loaded.content)
        self.assertIn("Create, revise, merge, or retire", catalog.load("skill-authoring").description)

    def test_parser_rejects_alias_duplicate_unknown_field_and_raw_command(self):
        base = f"""---
name: safe-route
description: Select a governed route when repeated mobility evidence supports a safer approach.
tools:
  - move_to
---

{skill_body()}
"""
        malformed = (
            base.replace("tools:\n", "tools: &shared\n", 1),
            base.replace("name: safe-route", "name: safe-route\nname: duplicate", 1),
            base.replace("tools:\n", "unknown: value\ntools:\n", 1),
            base.replace("1. Read current state", "1. Run `/player Bot move forward`", 1),
            base.replace("name: safe-route", "? [unsafe, key]\n: value", 1),
            base.replace(
                "1. Read current state",
                "1. Use sk-abcdefghijklmnopqrstuvwxyz123456 as evidence",
                1,
            ),
        )

        for source in malformed:
            with self.subTest(source=source[:80]), self.assertRaises(SkillFormatError):
                parse_skill_markdown(source, registered_tools={"move_to"})

        parsed = parse_skill_markdown(
            base.replace("## Use When", "## Use When   ", 1),
            registered_tools={"move_to"},
        )
        self.assertEqual(parsed.name, "safe-route")

    def test_catalog_rejects_non_directory_debris(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "catalog.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(SkillCatalogError):
                SkillCatalog(root)


class SkillCrudTests(unittest.TestCase):
    def test_create_update_retire_preserves_immutable_versions(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = SkillWorkspace(store, scope, SkillCatalog())
        bind_workspace(workspace, extra_tools=("move_to",))

        created = workspace.create(
            name="safe-route",
            description="Select a governed route when repeated mobility evidence supports a safer approach.",
            tools=["move_to"],
            body=skill_body(marker="v1"),
            evidence_refs=["observation:route-1", "task:route-2"],
        )
        updated = workspace.update(
            name="safe-route",
            expected_revision=1,
            description=None,
            tools=None,
            body=skill_body(marker="v2"),
            evidence_refs=["observation:route-3"],
            change_reason="Add the second verified adaptation.",
        )

        self.assertEqual(created.revision, 1)
        self.assertEqual(updated.revision, 2)
        self.assertNotEqual(created.version, updated.version)
        self.assertEqual(workspace.read("safe-route", created.version).body, created.body)
        with self.assertRaises(SkillOperationError) as conflict:
            workspace.update(
                name="safe-route",
                expected_revision=1,
                description=None,
                tools=None,
                body=skill_body(marker="v3"),
                evidence_refs=["observation:route-4"],
                change_reason="Stale writer.",
            )
        self.assertEqual(conflict.exception.code, "skill_update_conflict")

        retired = workspace.delete(
            name="safe-route",
            expected_revision=2,
            evidence_refs=["task:route-retired"],
            reason="Superseded by a broader verified method.",
        )

        self.assertEqual(retired.status, "retired")
        self.assertNotIn("safe-route", [item["name"] for item in workspace.list()["skills"]])
        self.assertIsNone(workspace.read("safe-route"))
        self.assertEqual(workspace.read("safe-route", created.version).version, created.version)
        store.close()

    def test_definitions_cross_world_for_same_bot_but_never_cross_bot(self):
        store = RuntimeStateStore(":memory:")
        source = SkillWorkspace(store, RuntimeScope("server", "world-a", "Bot1"), SkillCatalog())
        bind_workspace(source, extra_tools=("move_to",))
        source.create(
            name="safe-route",
            description="Select a governed route when repeated mobility evidence supports a safer approach.",
            tools=["move_to"],
            body=skill_body(),
            evidence_refs=["observation:world-a"],
        )
        same_bot = SkillWorkspace(store, RuntimeScope("server", "world-b", "Bot1"), SkillCatalog())
        other_bot = SkillWorkspace(store, RuntimeScope("server", "world-a", "Bot2"), SkillCatalog())
        bind_workspace(same_bot, extra_tools=("move_to",))
        bind_workspace(other_bot, extra_tools=("move_to",))

        self.assertIsNotNone(same_bot.read("safe-route"))
        self.assertIsNone(other_bot.read("safe-route"))
        store.close()

    def test_unknown_dependency_and_builtin_mutation_are_typed_rejections(self):
        store = RuntimeStateStore(":memory:")
        workspace = SkillWorkspace(store, RuntimeScope("server", "world", "Bot1"), SkillCatalog())
        bind_workspace(workspace)

        with self.assertRaises(SkillOperationError) as dependency:
            workspace.create(
                name="unsafe-dependency",
                description="Use a missing dependency when a route needs an unavailable capability.",
                tools=["raw_rcon"],
                body=skill_body(),
                evidence_refs=["observation:1"],
            )
        self.assertEqual(dependency.exception.code, "skill_dependency_unavailable")
        with self.assertRaises(SkillOperationError) as immutable:
            workspace.delete(
                name=" skill-authoring ",
                expected_revision=1,
                evidence_refs=["observation:2"],
                reason="Attempted mutation.",
            )
        self.assertEqual(immutable.exception.code, "builtin_skill_immutable")
        store.close()

    def test_learned_dependency_drift_stays_discoverable_but_not_loadable(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        original = SkillWorkspace(store, scope, SkillCatalog())
        bind_workspace(original, extra_tools=("move_to",))
        original.create(
            name="safe-route",
            description="Select a governed route when repeated mobility evidence supports a safer approach.",
            tools=["move_to"],
            body=skill_body(),
            evidence_refs=["observation:route"],
        )

        drifted = SkillWorkspace(store, scope, SkillCatalog())
        bind_workspace(drifted)
        descriptor = next(item for item in drifted.list()["skills"] if item["name"] == "safe-route")

        self.assertFalse(descriptor["loadable"])
        self.assertEqual(descriptor["missing_tools"], ["move_to"])
        with self.assertRaises(SkillOperationError) as unavailable:
            drifted.load("safe-route")
        self.assertEqual(unavailable.exception.code, "skill_dependency_unavailable")
        store.close()


class SkillActivationTests(unittest.TestCase):
    def test_active_and_descriptor_budgets_reject_instead_of_silently_omitting(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = SkillWorkspace(store, scope, SkillCatalog())
        bind_workspace(workspace, extra_tools=("move_to",))
        workspace.set_activation_owner(owner_kind="turn", owner_id="turn-budget")
        for name in workspace.catalog.names:
            workspace.load(name)
        learned = workspace.create(
            name="safe-route",
            description="Select a governed route when repeated mobility evidence supports a safer approach.",
            tools=["move_to"],
            body=skill_body(),
            evidence_refs=["observation:route"],
        )

        with self.assertRaises(SkillOperationError) as active_limit:
            workspace.load(learned.name)
        self.assertEqual(active_limit.exception.code, "skill_active_limit_exceeded")

        catalog_error = None
        for index in range(80):
            name = f"terrain-method-{index}"
            description = (
                f"Select terrain method {index} when repeated observations show that "
                + "the current governed approach needs a distinct evidence-backed adaptation. " * 2
            )
            try:
                workspace.create(
                    name=name,
                    description=description,
                    tools=["move_to"],
                    body=skill_body(title=f"Terrain Method {index}"),
                    evidence_refs=[f"observation:terrain-{index}"],
                )
            except SkillOperationError as exc:
                catalog_error = exc
                self.assertIsNone(workspace.read(name))
                break
        self.assertIsNotNone(catalog_error)
        self.assertEqual(catalog_error.code, "skill_catalog_context_budget_exceeded")
        store.close()

    def test_task_activation_pins_exact_version_across_head_update_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            scope = RuntimeScope("server", "world", "Bot1")
            store = RuntimeStateStore(path)
            tasks = TaskWorkspace(store, scope)
            task = tasks.start("prepare tools", source="user")
            workspace = SkillWorkspace(store, scope, SkillCatalog(), task_workspace=tasks)
            bind_workspace(workspace, extra_tools=("move_to",))
            v1 = workspace.create(
                name="safe-route",
                description="Select a governed route when repeated mobility evidence supports a safer approach.",
                tools=["move_to"],
                body=skill_body(marker="v1"),
                evidence_refs=["observation:1"],
            )
            loaded, first_activation = workspace.load("safe-route")
            workspace.update(
                name="safe-route",
                expected_revision=1,
                description=None,
                tools=None,
                body=skill_body(marker="v2"),
                evidence_refs=["observation:2"],
                change_reason="Verified update.",
            )
            self.assertEqual(workspace.active_documents()[0].version, v1.version)
            self.assertEqual(first_activation.owner_kind, "task")
            self.assertEqual(first_activation.owner_id, task.task_id)
            self.assertEqual(loaded.version, v1.version)
            store.close()

            reopened_store = RuntimeStateStore(path)
            reopened_tasks = TaskWorkspace(reopened_store, scope)
            reopened = SkillWorkspace(
                reopened_store,
                scope,
                SkillCatalog(),
                task_workspace=reopened_tasks,
            )
            bind_workspace(reopened, extra_tools=("move_to",))

            self.assertEqual(reopened.active_documents()[0].version, v1.version)
            reopened.end_task(task.task_id)
            self.assertEqual(reopened.active_documents(), ())
            reopened_store.close()

    def test_tools_expose_full_crud_without_changing_body_permissions(self):
        store = RuntimeStateStore(":memory:")
        workspace = SkillWorkspace(store, RuntimeScope("server", "world", "Bot1"), SkillCatalog())
        registry = bind_workspace(workspace)

        listed = registry.get("list_skills").callable({"query": "authoring"})
        loaded = registry.get("load_skill").callable({"name": "skill-authoring"})
        missing = registry.get("load_skill").callable({"name": "missing"})

        self.assertTrue(listed.success)
        self.assertTrue(loaded.success)
        self.assertIn("instructions", loaded.metrics)
        self.assertFalse(missing.success)
        self.assertEqual(SKILL_TOOL_NAMES.issubset(set(registry.names())), True)
        self.assertTrue(all(not registry.sidecar(name).mutating for name in SKILL_TOOL_NAMES))
        self.assertTrue(all(registry.sidecar(name).body_scope == () for name in SKILL_TOOL_NAMES))
        self.assertEqual(registry.sidecar("load_skill").permission, "load_skill")
        store.close()


if __name__ == "__main__":
    unittest.main()
