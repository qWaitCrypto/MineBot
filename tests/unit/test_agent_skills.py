import json
import tempfile
import unittest
from pathlib import Path

from minebot.app.runtime_state import RuntimeScope, RuntimeStateStore
from minebot.app.skills import (
    SkillCatalog,
    SkillCatalogError,
    SkillWorkspace,
    register_skill_tools,
)
from minebot.app.tasks import TaskWorkspace
from minebot.brain.registry import ToolRegistry


class SkillCatalogTests(unittest.TestCase):
    def test_default_catalog_lists_and_loads_versioned_bounded_methodology(self):
        catalog = SkillCatalog()

        matches = catalog.list("resource")
        loaded = catalog.load("resource-progression")

        self.assertEqual([item["name"] for item in matches], ["resource-progression"])
        self.assertIsNotNone(loaded)
        self.assertTrue(loaded.version.startswith("sha256:"))
        self.assertIn("authoritative inventory", loaded.content)
        self.assertNotIn("/player", loaded.content)

    def test_catalog_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "catalog.json").write_text(
                json.dumps(
                    {
                        "skills": [
                            {
                                "name": "unsafe",
                                "description": "unsafe path",
                                "file": "../unsafe.md",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SkillCatalogError):
                SkillCatalog(root)


class SkillActivationTests(unittest.TestCase):
    def test_load_pins_exact_version_to_task_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            scope = RuntimeScope("server", "world", "Bot1")
            store = RuntimeStateStore(path)
            tasks = TaskWorkspace(store, scope)
            task = tasks.start("prepare tools", source="user")
            workspace = SkillWorkspace(store, scope, SkillCatalog(), task_workspace=tasks)

            first = workspace.load("resource-progression")
            second = workspace.load("resource-progression")

            self.assertIsNotNone(first)
            self.assertEqual(first[1].activation_id, second[1].activation_id)
            self.assertEqual(first[1].task_id, task.task_id)
            self.assertEqual(tasks.payload()["skills"][0]["skill_version"], first[0].version)
            store.close()

            reopened_store = RuntimeStateStore(path)
            reopened_tasks = TaskWorkspace(reopened_store, scope)
            self.assertEqual(
                reopened_tasks.payload()["skills"][0]["skill_name"],
                "resource-progression",
            )
            reopened_store.close()

    def test_skill_activations_never_cross_runtime_scope(self):
        store = RuntimeStateStore(":memory:")
        scope = RuntimeScope("server", "world-a", "Bot1")
        other = RuntimeScope("server", "world-b", "Bot1")
        workspace = SkillWorkspace(store, scope, SkillCatalog())
        workspace.load("recovery-and-continuation")

        self.assertEqual(len(workspace.activations()), 1)
        self.assertEqual(
            len(SkillWorkspace(store, other, SkillCatalog()).activations()),
            0,
        )
        store.close()

    def test_tools_return_methodology_without_changing_capability_permissions(self):
        store = RuntimeStateStore(":memory:")
        workspace = SkillWorkspace(
            store,
            RuntimeScope("server", "world", "Bot1"),
            SkillCatalog(),
        )
        registry = ToolRegistry()
        register_skill_tools(registry, workspace)

        listed = registry.get("list_skills").callable({"query": "recovery"})
        loaded = registry.get("load_skill").callable(
            {"name": "recovery-and-continuation"}
        )
        missing = registry.get("load_skill").callable({"name": "missing"})

        self.assertTrue(listed.success)
        self.assertTrue(loaded.success)
        self.assertIn("instructions", loaded.metrics)
        self.assertFalse(missing.success)
        self.assertEqual(set(registry.names()), {"list_skills", "load_skill"})
        self.assertTrue(all(not registry.sidecar(name).mutating for name in registry.names()))
        self.assertEqual(registry.sidecar("load_skill").permission, "load_skill")
        store.close()


if __name__ == "__main__":
    unittest.main()
