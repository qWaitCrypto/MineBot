import tempfile
import unittest
from pathlib import Path

from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.app.skill_format import (
    MAX_V2_SKILL_BODY_CHARS,
    V2_SKILL_SECTIONS,
    SkillFormatError,
    parse_skill_markdown,
    skill_template,
)
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


def v2_body(*, order=V2_SKILL_SECTIONS, filler: str = "", lines: int = 0) -> str:
    parts = ["# Guarded Target Clearing"]
    for name in order:
        parts.append(f"## {name}\n\nRead live facts for {name.lower()} before each step.")
    parts[3] += filler + "\n" * 0 + "".join(f"\n{index}. Check the target again." for index in range(lines))
    return "\n\n".join(parts) + "\n"


def v2_document(body: str) -> str:
    return (
        "---\nname: guarded-target\n"
        'description: "Clear a guarded target safely when a ladder item holds this Skill."\n'
        "tools: []\n---\n\n" + body
    )


class SkillTemplateV2Tests(unittest.TestCase):
    def test_v2_sections_parse_in_any_order_each_exactly_once(self):
        parsed = parse_skill_markdown(v2_document(v2_body()))
        self.assertEqual(skill_template(parsed.body), "v2")
        shuffled = tuple(reversed(V2_SKILL_SECTIONS))
        self.assertEqual(skill_template(parse_skill_markdown(v2_document(v2_body(order=shuffled))).body), "v2")

    def test_v2_body_may_be_longer_than_the_july_limit_up_to_its_own(self):
        long_line = " Read the live facts again." * 380  # about 10,300 characters
        parsed = parse_skill_markdown(v2_document(v2_body(filler=long_line)))
        self.assertGreater(len(parsed.body), 8_000)
        with self.assertRaises(SkillFormatError) as too_long:
            parse_skill_markdown(v2_document(v2_body(filler=long_line * 2)))
        self.assertIn(str(MAX_V2_SKILL_BODY_CHARS), str(too_long.exception))
        self.assertEqual(len(parse_skill_markdown(v2_document(v2_body(lines=250))).body.splitlines()) > 200, True)
        with self.assertRaises(SkillFormatError) as too_many:
            parse_skill_markdown(v2_document(v2_body(lines=300)))
        self.assertIn("300 lines", str(too_many.exception))

    def test_v2_rejects_a_missing_repeated_or_mixed_section(self):
        for order in (
            V2_SKILL_SECTIONS[:-1],
            (*V2_SKILL_SECTIONS, "Cautions"),
            (*V2_SKILL_SECTIONS[:-1], "Boundaries"),
        ):
            with self.subTest(order=order), self.assertRaises(SkillFormatError):
                parse_skill_markdown(v2_document(v2_body(order=order)))

    def test_july_template_stays_accepted_with_its_own_limit(self):
        self.assertEqual(skill_template(skill_body()), "july")
        catalog = SkillCatalog()
        self.assertIn("resource-progression", catalog.names)
        oversized = skill_body(marker="x " * 4_100)
        with self.assertRaises(SkillFormatError) as too_long:
            parse_skill_markdown(
                "---\nname: safe-route\ndescription: \"Choose a safer route from repeated route evidence.\"\ntools: []\n---\n\n"
                + oversized
            )
        self.assertIn("8000", str(too_long.exception))


class SkillCatalogRenderTests(unittest.TestCase):
    def test_catalog_lines_are_name_and_description_only_and_stable(self):
        store = RuntimeStateStore(":memory:")
        workspace = SkillWorkspace(store, RuntimeScope("server", "world", "Bot1"), SkillCatalog())
        bind_workspace(workspace)
        first = workspace.catalog_snapshot().rendered
        second = workspace.catalog_snapshot().rendered
        self.assertEqual(first, second)
        lines = first.splitlines()
        self.assertTrue(lines[0].startswith("AVAILABLE_SKILLS"))
        for name in workspace.catalog.names:
            document = workspace.catalog.load(name)
            self.assertIn(f"- {name}: {document.description}", lines)
            self.assertNotIn(document.version, first)
        for absent in ("catalog_revision", "origin=", "head_version", "sha256:", "loadable=true"):
            self.assertNotIn(absent, first)
        store.close()

    def test_unloadable_skill_line_names_missing_tools(self):
        from dataclasses import replace

        from minebot.app.skills import _descriptor_line

        document = replace(SkillCatalog().load("resource-progression"), loadable=False,
                           missing_tools=("craft_item",))
        self.assertEqual(
            _descriptor_line(document),
            f"- resource-progression: {document.description} loadable=false missing_tools=[\"craft_item\"]",
        )


class SkillFormatTests(unittest.TestCase):
    def test_default_catalog_uses_strict_skill_md_and_contains_bootstrap_skill(self):
        catalog = SkillCatalog()

        self.assertEqual(
            catalog.names,
            (
                "end-crystals",
                "end-dragon-beds",
                "end-dragon-fight",
                "end-preparation",
                "ender-eyes-stronghold",
                "evidence-led-exploration",
                "hostile-survival",
                "nether-expedition",
                "nether-portal",
                "recovery-and-continuation",
                "resource-progression",
                "skill-authoring",
            ),
        )
        loaded = catalog.load("resource-progression")
        self.assertTrue(loaded.version.startswith("sha256:"))
        self.assertIn("authoritative inventory", loaded.content)
        self.assertIn("prefer `collect_resource`", loaded.content)
        self.assertIn("manual\n   `search_for_block` -> `move_to` -> `mine_block_collect` chain", loaded.content)
        self.assertIn("Generic `move_to` is\n   coordinate travel", loaded.content)
        self.assertIn("capability dependencies", loaded.content)
        self.assertNotIn("/player", loaded.content)
        self.assertIn("Create, revise, merge, or retire", catalog.load("skill-authoring").description)
        exploration = catalog.load("evidence-led-exploration")
        self.assertIn("explore_for", exploration.tools)
        nether = catalog.load("nether-expedition")
        self.assertIn("equip_item", nether.tools)
        self.assertIn("Never sleep in the Nether", nether.content)
        self.assertIn("Blaze rods cannot be had on peaceful", nether.content)

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
        bind_workspace(original, extra_tools=("temporary_route_tool",))
        original.create(
            name="safe-route",
            description="Select a governed route when repeated mobility evidence supports a safer approach.",
            tools=["temporary_route_tool"],
            body=skill_body(),
            evidence_refs=["observation:route"],
        )

        drifted = SkillWorkspace(store, scope, SkillCatalog())
        bind_workspace(drifted)
        descriptor = next(item for item in drifted.list()["skills"] if item["name"] == "safe-route")

        self.assertFalse(descriptor["loadable"])
        self.assertEqual(descriptor["missing_tools"], ["temporary_route_tool"])
        with self.assertRaises(SkillOperationError) as unavailable:
            drifted.load("safe-route")
        self.assertEqual(unavailable.exception.code, "skill_dependency_unavailable")
        store.close()


class SkillActivationTests(unittest.TestCase):
    def test_load_by_name_returns_the_current_body_without_pinning_it(self):
        store = RuntimeStateStore(":memory:")
        workspace = SkillWorkspace(store, RuntimeScope("server", "world", "Bot1"), SkillCatalog())
        registry = bind_workspace(workspace)
        document = workspace.catalog.load("resource-progression")

        loaded = registry.get("load_skill").callable({"name": document.name})

        self.assertTrue(loaded.success)
        self.assertEqual(loaded.metrics["version"], document.version)
        self.assertEqual(loaded.metrics["instructions"], document.body)
        # A one-turn look: no activation record, nothing kept in effect.
        self.assertNotIn("activation", loaded.metrics)
        self.assertEqual(workspace.activations(), ())
        store.close()

    def test_advertised_builtin_version_round_trips_through_load_skill(self):
        store = RuntimeStateStore(":memory:")
        workspace = SkillWorkspace(store, RuntimeScope("server", "world", "Bot1"), SkillCatalog())
        registry = bind_workspace(workspace)
        document = workspace.catalog.load("resource-progression")

        snapshot = workspace.catalog_snapshot()
        loaded = registry.get("load_skill").callable(
            {
                "name": document.name,
                "version": f"builtin {document.version}",
            }
        )

        self.assertIn(f"- {document.name}: {document.description}", snapshot.rendered)
        self.assertNotIn(document.version, snapshot.rendered)
        self.assertIn(
            "Omit to read the current Skill",
            registry.get("load_skill").input_schema["properties"]["version"]["description"],
        )
        self.assertTrue(loaded.success)
        self.assertEqual(loaded.reason, "skill_loaded")
        self.assertEqual(loaded.metrics["version"], document.version)
        store.close()

    def test_wrong_explicit_version_reports_current_head_as_retryable(self):
        store = RuntimeStateStore(":memory:")
        workspace = SkillWorkspace(store, RuntimeScope("server", "world", "Bot1"), SkillCatalog())
        registry = bind_workspace(workspace)
        document = workspace.catalog.load("resource-progression")

        loaded = registry.get("load_skill").callable(
            {"name": document.name, "version": "sha256:" + "0" * 64}
        )

        self.assertFalse(loaded.success)
        self.assertEqual(loaded.reason, "skill_version_mismatch")
        self.assertTrue(loaded.can_retry)
        self.assertIn(document.version, loaded.metrics["error"])
        store.close()

    def test_active_and_descriptor_budgets_reject_instead_of_silently_omitting(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world", "Bot1")
        workspace = SkillWorkspace(store, scope, SkillCatalog())
        bind_workspace(workspace, extra_tools=("move_to",))
        workspace.set_activation_owner(owner_kind="turn", owner_id="turn-budget")
        for name in workspace.catalog.names[:3]:
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
