"""Scope rebinds use temporary databases only; no live runtime is needed."""

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from agents import SQLiteSession
import pytest

from minebot.app.life_profile import LifeStore, life_file_path, rebind_life_files
from minebot.app.runtime_state import (
    CheckpointDisposition, MemoryKind, MemorySource, PlacementEdit,
    RuntimeScope, RuntimeStateConflict, RuntimeStateError, RuntimeStateStore,
)


OLD = RuntimeScope("server", "real-client:minecraft:overworld", "HouseBot")
NEW = replace(OLD, world_id="house-continuity-20261017")
OTHER = replace(OLD, bot_id="OtherBot")
ROOT = Path(__file__).resolve().parents[2]


def snapshot(store):
    return tuple(store._connection.iterdump())


def seed_sdk(store, session_id, items):
    # Let the installed SDK create its real schema. Its async worker-thread
    # methods can hang in the restricted sandbox, so seed these SDK-owned rows
    # synchronously; all MineBot state below uses the store's public methods.
    with closing(SQLiteSession(session_id, store.db_path)):
        pass
    with store._connection:
        store._connection.execute("INSERT INTO agent_sessions(session_id) VALUES (?)", (session_id,))
        store._connection.executemany(
            "INSERT INTO agent_messages(session_id, message_data) VALUES (?, ?)",
            [(session_id, json.dumps(item)) for item in items],
        )


def sdk_items(store, session_id):
    return [json.loads(row[0]) for row in store._connection.execute(
        "SELECT message_data FROM agent_messages WHERE session_id = ? ORDER BY id", (session_id,)
    )]


@pytest.fixture
def populated(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = RuntimeStateStore(path)
    try:
        task = store.create_task(OLD, goal_text="Build a home", source="user")
        plan = store.update_plan(task.task_id, expected_revision=0, summary="Build",
                                 steps=[{"title": "Place a table"}, {"title": "Build a roof"}])
        task, checkpoint = store.create_checkpoint(
            task.task_id, expected_task_revision=store.get_task(task.task_id).revision,
            disposition=CheckpointDisposition.CONTINUE, summary="Have wood", next_step="Place table",
        )
        memory = store.create_memory(
            OLD, kind=MemoryKind.EPISODIC, source=MemorySource.OBSERVED,
            title="Home", content=f"Historical scope was {OLD.key}", subject_key="home", evidence_ref="body:home",
        )
        observation = store.create_tool_observation(
            OLD, tool_name="world_edit", tool_call_id="place-table", result={"success": True}, complete=True,
            placement_edits=[PlacementEdit("overworld", (1, 64, 2), "place", "crafting_table",
                                           "home", "action-1", None, None)],
        )
        dedupe = f"task_continue:{OLD.key}:{checkpoint.checkpoint_id}:g1"
        intent = store.enqueue_work_intent(
            OLD, kind="task_continue", source="autonomy", priority=10, task_id=task.task_id,
            payload={"checkpoint_id": checkpoint.checkpoint_id}, dedupe_key=dedupe,
        )
        skill, version = store.create_skill_head(
            OLD, name="build_home", version_digest="v1", description="Build a home",
            tools=("world_edit",), body="Place a table", evidence_refs=(), change_reason="learned",
        )
        activation = store.record_skill_activation(
            OLD, skill_name=skill.name, skill_id=skill.skill_id,
            skill_version=version.version_digest, task_id=task.task_id,
        )
        store.replace_conversation_archive(
            OLD, items=[{"role": "user", "content": "Build a home"}],
            summary={"scope_key": OLD.key, "covered_turn_handles": [f"conversation:{OLD.key}:turn:1"],
                     "note": f"{OLD.conversation_session_id} was the previous identifier"},
        )
        store.set_event_cursor(OLD, last_seq=8, last_chat_seq=3, event_epoch="epoch-1")
        other_task = store.create_task(OTHER, goal_text="Stay here", source="user")
        for scope in (OLD, OTHER):
            seed_sdk(store, scope.conversation_session_id, [{"role": "user", "content": scope.bot_id}])
        # An additional SDK session embeds the same scope key.
        extra_session = f"minebot:{OLD.key}:reflex"
        seed_sdk(store, extra_session, [{"role": "assistant", "content": "Watching"}])
        # Future scope tables, including WITHOUT ROWID and quoted names, must
        # participate without changing the implementation's table list.
        store._connection.execute('CREATE TABLE "future state" (scope_key TEXT PRIMARY KEY '
                                  'REFERENCES runtime_scopes(scope_key), payload TEXT) WITHOUT ROWID')
        store._connection.execute('INSERT INTO "future state" VALUES (?, ?)', (OLD.key, "kept"))
        store._connection.commit()
        yield store, {"task": task, "plan": plan, "checkpoint": checkpoint, "memory": memory,
                      "observation": observation, "intent": intent, "dedupe": dedupe,
                      "other_task": other_task, "extra_session": extra_session,
                      "skill": skill, "version": version, "activation": activation}
    finally:
        store.close()


def test_dry_run_counts_rows_and_changes_nothing(populated):
    store, data = populated
    before = snapshot(store)
    plan = store.rebind_scope(OLD, NEW, dry_run=True)
    assert plan["old"]["key"] == OLD.key
    assert plan["new"]["session_id"] == NEW.conversation_session_id
    for table in ("runtime_scopes", "tasks", "memory_entries", "tool_observations", "bot_placements",
                  "work_intents", "conversation_archives", "event_cursors", "skill_activations", "future state"):
        assert plan["row_counts"][table] == 1
    assert plan["row_counts"]["agent_sessions"] == 2
    assert plan["row_counts"]["agent_messages"] == 2
    assert "scope_key" in plan["moved_columns"]["skill_activations"]
    assert "dedupe_key" in plan["moved_columns"]["work_intents"]
    assert "summary_json" in plan["moved_columns"]["conversation_archives"]
    assert any(row["table"] == "memory_entries" and row["column"] == "content" and row["reason"]
               for row in plan["remaining_old_key_text"])
    assert snapshot(store) == before


def test_rebind_preserves_records_and_other_bot(populated):
    store, data = populated
    placements = store.list_bot_placements(OLD)
    cursor = store.get_event_cursor(OLD)
    store.rebind_scope(OLD, NEW, dry_run=False)
    assert not store.has_scope(OLD) and store.has_scope(NEW)
    assert store.get_foreground_task(OLD) is None
    assert store.get_foreground_task(NEW) == replace(data["task"], scope_key=NEW.key)
    assert store.get_plan(data["task"].task_id) == data["plan"]
    assert store.get_latest_checkpoint(data["task"].task_id) == data["checkpoint"]
    assert store.get_memory(OLD, data["memory"].memory_id) is None
    assert store.get_memory(NEW, data["memory"].memory_id) == replace(data["memory"], scope_key=NEW.key)
    assert store.search_memories(NEW, query="Historical")["results"][0]["memory_id"] == data["memory"].memory_id
    assert store.list_bot_placements(NEW) == placements
    assert store.list_bot_placements(OLD) == []
    assert store.get_tool_observation(OLD, data["observation"]["handle"]) is None
    assert store.get_tool_observation(NEW, data["observation"]["handle"])["result"] == {"success": True}
    assert store.get_work_intent_by_dedupe(OLD, data["dedupe"]) is None
    new_dedupe = data["dedupe"].replace(OLD.key, NEW.key)
    assert store.get_work_intent_by_dedupe(NEW, new_dedupe)["intent_id"] == data["intent"]["intent_id"]
    assert store.get_conversation_archive(OLD) is None
    archive = store.get_conversation_archive(NEW)
    assert archive["items"] == [{"role": "user", "content": "Build a home"}]
    assert archive["summary"] == {"scope_key": NEW.key,
                                   "covered_turn_handles": [f"conversation:{NEW.key}:turn:1"],
                                   "note": f"{OLD.conversation_session_id} was the previous identifier"}
    assert store.get_event_cursor(OLD) is None
    assert store.get_event_cursor(NEW) == cursor
    assert store.list_skill_activations(OLD) == ()
    assert store.list_skill_activations(NEW) == (replace(data["activation"], scope_key=NEW.key),)
    # Learned skill ownership is server/bot-wide, so its ids and versions stay.
    assert store.get_skill_head(NEW, name="build_home") == data["skill"]
    assert store.get_skill_version(data["skill"].skill_id, version_digest="v1") == data["version"]
    assert store.get_foreground_task(OTHER) == data["other_task"]
    assert store._connection.execute('SELECT * FROM "future state"').fetchone()["scope_key"] == NEW.key
    assert store._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    for scope, expected in ((NEW, [{"role": "user", "content": OLD.bot_id}]),
                            (OTHER, [{"role": "user", "content": OTHER.bot_id}]), (OLD, [])):
        assert sdk_items(store, scope.conversation_session_id) == expected
    assert sdk_items(store, data["extra_session"].replace(OLD.key, NEW.key)) == [
        {"role": "assistant", "content": "Watching"}]
    # The registration cache must permit the old identity to be registered again.
    store.register_scope(OLD)
    assert store.has_scope(OLD)


@pytest.mark.parametrize("destination", ["scope", "session", "orphan_scope_row"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_destination_refused_without_changes(populated, destination, dry_run):
    store, _ = populated
    if destination == "scope":
        store.register_scope(NEW)
    elif destination == "session":
        seed_sdk(store, f"minebot:{NEW.key}:reflex", [{"role": "user", "content": "occupied"}])
    else:
        store._connection.execute("CREATE TABLE future_orphan (scope_key TEXT)")
        store._connection.execute("INSERT INTO future_orphan VALUES (?)", (NEW.key,))
        store._connection.commit()
    before = snapshot(store)
    with pytest.raises(RuntimeStateConflict, match="new scope already"):
        store.rebind_scope(OLD, NEW, dry_run=dry_run)
    assert snapshot(store) == before


@pytest.mark.parametrize("new", [OLD, replace(NEW, server_id="different"), replace(NEW, bot_id="different")])
def test_invalid_identity_refused(populated, new):
    store, _ = populated
    before = snapshot(store)
    with pytest.raises(ValueError, match="same server and bot"):
        store.rebind_scope(OLD, new, dry_run=False)
    assert snapshot(store) == before


def test_missing_old_scope_refused(tmp_path):
    with closing(RuntimeStateStore(tmp_path / "empty.sqlite3")) as store:
        before = snapshot(store)
        with pytest.raises(RuntimeStateConflict, match="old scope has no"):
            store.rebind_scope(OLD, NEW, dry_run=False)
        assert snapshot(store) == before


def test_foreign_key_violation_rolls_back_everything(populated):
    store, _ = populated
    # A trigger simulates a broken future migration. The final FK audit must
    # reject the move even though every original scope row was valid.
    store._connection.execute("CREATE TABLE bad_reference (owner TEXT REFERENCES runtime_scopes(scope_key))")
    store._connection.execute("CREATE TRIGGER break_rebind AFTER INSERT ON runtime_scopes "
                              "WHEN new.world_id = 'house-continuity-20261017' BEGIN "
                              "INSERT INTO bad_reference VALUES ('missing'); END")
    before = snapshot(store)
    with pytest.raises(RuntimeStateError, match="foreign_key_check failed"):
        store.rebind_scope(OLD, NEW, dry_run=False)
    assert snapshot(store) == before
    assert store.has_scope(OLD) and not store.has_scope(NEW)


def cli(db, life_dir, *options):
    return subprocess.run(
        [sys.executable, str(ROOT / "tools/rebind_world_scope.py"), "--db", str(db),
         "--life-dir", str(life_dir), "--server-id", OLD.server_id, "--bot", OLD.bot_id,
         "--from-world", OLD.world_id, "--to-world", NEW.world_id, *options],
        cwd=ROOT, text=True, capture_output=True, timeout=20,
    )


@pytest.fixture
def cli_state(tmp_path):
    db = tmp_path / "state.sqlite3"
    with closing(RuntimeStateStore(db)) as store:
        store.create_task(OLD, goal_text="Keep home", source="user")
    life_dir = tmp_path / "life"
    life = LifeStore(life_dir, OLD)
    life.update({"routine": "Stay home"}, expected_revision=0, reason="I live here")
    life.log_path.write_text('{"project":"home","result":"done"}\n')
    return db, life_dir, life


def test_cli_defaults_to_dry_run(cli_state):
    db, life_dir, life = cli_state
    result = cli(db, life_dir)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["dry_run"] is True
    assert life.path.exists() and life.log_path.exists()
    assert not life_file_path(life_dir, NEW).exists()
    assert not list(db.parent.glob("*.before-rebind-*"))
    with closing(RuntimeStateStore(db)) as store:
        assert store.has_scope(OLD) and not store.has_scope(NEW)


def test_cli_refuses_while_a_session_holds_the_scheduler_lock(cli_state):
    from filelock import FileLock

    db, life_dir, _ = cli_state
    with FileLock(db.with_name(f"{db.name}.{OLD.key}.scheduler.lock")):
        result = cli(db, life_dir, "--confirm")
    assert result.returncode != 0 and "scheduler lock is held" in result.stderr
    assert not list(db.parent.glob("*.before-rebind-*"))
    with closing(RuntimeStateStore(db)) as store:
        assert store.has_scope(OLD) and not store.has_scope(NEW)


@pytest.mark.parametrize("lock_kind", ["writer", "reader"])
def test_cli_refuses_database_lock(cli_state, lock_kind):
    db, life_dir, _ = cli_state
    with closing(sqlite3.connect(db)) as blocker:
        blocker.execute("BEGIN IMMEDIATE" if lock_kind == "writer" else "BEGIN")
        blocker.execute("SELECT * FROM runtime_scopes").fetchall()
        result = cli(db, life_dir, "--confirm")
    assert result.returncode != 0 and "exclusive database lock" in result.stderr
    assert not list(db.parent.glob("*.before-rebind-*"))


def test_cli_confirm_backs_up_and_renames_life(cli_state):
    db, life_dir, life = cli_state
    profile, project_log = life.path.read_bytes(), life.log_path.read_bytes()
    result = cli(db, life_dir, "--confirm")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["dry_run"] is False
    backups = list(db.parent.glob("*.before-rebind-*"))
    assert backups == [Path(plan["backup"])]
    with closing(sqlite3.connect(backups[0])) as backup:
        assert backup.execute("SELECT scope_key FROM runtime_scopes").fetchall() == [(OLD.key,)]
        assert backup.execute("SELECT scope_key FROM tasks").fetchall() == [(OLD.key,)]
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not life.path.exists() and not life.log_path.exists()
    new_path = life_file_path(life_dir, NEW)
    assert new_path.read_bytes() == profile
    assert new_path.with_suffix(".project-log.jsonl").read_bytes() == project_log
    with closing(RuntimeStateStore(db)) as store:
        assert store.has_scope(NEW) and not store.has_scope(OLD)
        assert store._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    # The operation is explicitly one-shot.
    repeated = cli(db, life_dir, "--confirm")
    assert repeated.returncode != 0
    assert len(list(db.parent.glob("*.before-rebind-*"))) == 1


@pytest.mark.parametrize("suffix", [".json", ".project-log.jsonl", ".world.jsonl"])
def test_cli_refuses_existing_life_destination_before_db_move(cli_state, suffix):
    db, life_dir, life = cli_state
    target = life_file_path(life_dir, NEW).with_suffix(suffix)
    target.write_text("do not overwrite")
    for flags in ((), ("--confirm",)):
        result = cli(db, life_dir, *flags)
        assert result.returncode != 0 and "refusing to overwrite life file" in result.stderr
    assert target.read_text() == "do not overwrite"
    assert life.path.exists() and life.log_path.exists()
    assert not list(db.parent.glob("*.before-rebind-*"))
    with closing(RuntimeStateStore(db)) as store:
        assert store.has_scope(OLD) and not store.has_scope(NEW)


def test_life_helper_allows_absent_source_files(tmp_path):
    plan = rebind_life_files(tmp_path, OLD, NEW, dry_run=False)
    assert len(plan) == 3 and not any(item["exists"] for item in plan)


def test_cli_runs_when_a_stopped_session_left_its_lock_file(cli_state):
    db, life_dir, _ = cli_state
    # FileLock leaves the file in place after release, as a stopped session does.
    db.with_name(f"{db.name}.{OLD.key}.scheduler.lock").touch()
    result = cli(db, life_dir, "--confirm")
    assert result.returncode == 0, result.stderr
    with closing(RuntimeStateStore(db)) as store:
        assert store.has_scope(NEW) and not store.has_scope(OLD)
