"""Small synthetic evaluation evidence; no server, model, or large fixtures."""
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import struct
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from tools.eval.common import BusyTime, player_files, read_level, server_events, trace_bounds, trace_events
from tools.eval.failures import extract_failures, write_failures
from tools.eval.report import build_report, death_records, render_report
from tools.eval.sampler import inventory_counts, sample
from tools.eval.seeds import register, seed_digest, update
from tools.eval.wakes import Wakes
from minebot.app.trace_facts import TraceFactsWriter, encode_line


def write_trace(run, events):
    (run / "session").mkdir(parents=True, exist_ok=True)
    rows = [{"seq": i, "ts": 100 + i, "session_id": "Bot", **row} for i,row in enumerate(events, 1)]
    (run / "session" / "trace.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    return rows


def test_seeds_register_assign_discard_and_integrity(tmp_path):
    path = tmp_path / "seeds.json"
    with patch("tools.eval.seeds.secrets.randbits", side_effect=[0, 0, (1 << 64)-1, 1 << 63]):
        data = register(3, path)
    assert data["seeds"] == [-(1 << 63), (1 << 63)-1, 0]
    assert data["sha256"] == hashlib.sha256(json.dumps(data["seeds"], separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(FileExistsError):
        register(1, path)
    with pytest.raises(ValueError, match="not registered"):
        update(path, tmp_path / "a", seed=1)
    update(path, tmp_path / "a", seed=0)
    with pytest.raises(ValueError, match="already assigned"):
        update(path, tmp_path / "b", seed=0)
    with pytest.raises(ValueError, match="nonempty"):
        update(path, tmp_path / "a", reason=" ")
    update(path, tmp_path / "a", reason="server crashed")
    with pytest.raises(ValueError, match="already assigned"):
        update(path, tmp_path / "b", seed=0)
    data = json.loads(path.read_text())
    assert data["discards"][0]["reason"] == "server crashed"
    data["seeds"].append(3)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="digest"):
        update(path, tmp_path / "c", seed=3)


def test_sampler_counts_components_and_does_not_count_nested_items():
    text = ('[{Slot: 0b, components: {"minecraft:damage": 12, "minecraft:custom_name": "a,}b", '
            '"minecraft:container": [{id: "minecraft:diamond", count: 64}]}, count: 1, id: "minecraft:stone_pickaxe"}, '
            '{id: "minecraft:stone_pickaxe", components: {"minecraft:damage": 1}, count: 2}, '
            '{id: "minecraft:torch", count: 12}]')
    assert inventory_counts(text) == {"stone_pickaxe": 3, "torch": 12}
    assert inventory_counts('[{id: "minecraft:stone", Count: 3b}]') == {"stone": 3}
    assert inventory_counts("") == {}


def test_sampler_compatible_row():
    class Rcon:
        def command(self, command):
            if command.startswith("time"):
                return "The time is 42 ticks"
            if command.endswith(" Inventory"):
                return 'Bot has the following entity data: [{count: 1, components: {}, id: "minecraft:iron_pickaxe"}]'
            return "Bot has the following entity data: 20"
    row = sample(Rcon(), "Bot")
    assert set(row) == {"ts", "gametime", "health", "food", "pos", "dimension", "xp", "inventory"}
    assert row["gametime"] == "42"
    assert row["inventory"] == {"iron_pickaxe": 1}


def test_stream_prefilters_before_json_decode(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text('not json with "event": "huge_unused"\n' + json.dumps({"event": "llm_end", "seq": 2}) + '\n')
    assert list(trace_events(path, {"llm_end"})) == [{"event": "llm_end", "seq": 2}]


def test_wakes_require_consumed_matching_evidence_and_prefer_explicit():
    wakes = Wakes()
    rows = [
        {"event": "user_message", "command": "start"},
        {"event": "turn_decision_context", "intent_kind": "start"},
        {"event": "agent_start"},
        {"event": "llm_start"}, {"event": "llm_start"},
        {"event": "direct_call_handoff"}, {"event": "reflex_death"},
        {"event": "agent_start"},
        {"event": "reflex_fact_wake", "status": "handoff_requested"},
        {"event": "turn_decision_context", "intent_kind": "reflex_fact"},
        {"event": "agent_start"},
        {"event": "body_event_wake", "name": "world_ready"},
        {"event": "turn_decision_context", "intent_kind": "body_event"},
        {"event": "agent_start"},
        {"event": "turn_decision_context", "intent_kind": "task_continue"},
        {"event": "agent_start"},
        {"event": "agent_wake", "reason": "habit", "detail": {"kind": "habit"}},
        {"event": "agent_start"},
    ]
    for i,row in enumerate(rows):
        wakes.consume({"ts": i, "seq": i, **row})
    assert wakes.finish()["by_reason"] == {"user_message": 1, "unknown": 1, "decision_handback": 1, "world_ready": 1, "task_continue": 1, "habit": 1}
    assert wakes.finish()["count"] == 6
    native = Wakes()
    native.consume({"event": "agent_wake", "ts": 1, "seq": 3, "reason": "life", "detail": {}})
    assert native.finish()["count"] == 1


def test_busy_intervals_overlap_cancel_and_close_at_end():
    busy = BusyTime()
    for row in [dict(event="agent_start", ts=0), dict(event="llm_start", ts=0),
                dict(event="reflex_state_changed", ts=2, to="work"),
                dict(event="llm_end", ts=4), dict(event="reflex_state_changed", ts=6, **{"from": "work", "to": "idle"}),
                dict(event="turn_cancelled", ts=8), dict(event="tool_start", ts=9, tool="read")]:
        busy.consume(row)
    result = busy.finish(0, 10)
    assert result["busy_s"] == 4
    assert result["idle_s"] == 6
    assert sum(result["categories_s"].values()) == 10
    assert result["categories_s"]["Agent thinking (LLM), no plan"] == 2


def nbt_string(text):
    value = text.encode()
    return struct.pack(">H", len(value)) + value


def make_level(path):
    payload = (b'\x0a\x00\x00' + b'\x0a' + nbt_string("Data") +
               b'\x0a' + nbt_string("WorldGenSettings") + b'\x04' + nbt_string("seed") + struct.pack(">q", -17) + b'\x00' +
               b'\x01' + nbt_string("Difficulty") + b'\x01\x00\x00')
    path.write_bytes(gzip.compress(payload))


def test_nbt_and_server_logs_scope_dates_and_duplicates(tmp_path):
    make_level(tmp_path / "level.dat")
    assert read_level(tmp_path / "level.dat")["WorldGenSettings"]["seed"] == -17
    logs = tmp_path / "logs"
    logs.mkdir()
    text = ('[23:59:58] [Server thread/INFO]: Preparing level "eval"\n'
            '[23:59:59] [Server thread/INFO]: Bot was slain by Spider\n'
            '[00:00:02] [Server thread/INFO]: [Rcon: The difficulty has been set to Easy]\n')
    (logs / "2026-10-19-1.log.gz").write_bytes(gzip.compress(text.encode()))
    (logs / "2026-10-19-2.log.gz").write_bytes(gzip.compress(text.encode()))
    start = datetime(2026,10,19,23,59,tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    events = server_events(tmp_path, "eval", "Bot", start, start+120, ZoneInfo("Asia/Taipei"))
    assert [e["kind"] for e in events] == ["death", "difficulty"]
    assert events[1]["ts"] - events[0]["ts"] == 3
    assert server_events(tmp_path, "other", "Bot", start, start+120, ZoneInfo("Asia/Taipei")) == []


def test_death_fields_provenance_and_no_nearest_mob_guess():
    deaths = [{"ts": 50.4, "seq": 2, "facts": {"position": [1,2,3], "entities": [{"type": "zombie"}]}}]
    logs = [{"ts": 50, "kind": "death", "text": "Bot was blown up by Creeper", "source": "log:2"}]
    row = death_records(deaths, logs, [], 0, 100)[0]
    assert row["cause"] == "Bot was blown up by Creeper"
    assert row["position_source"] == "trace:reflex_death"
    assert row["time_source"] == "log:2"
    row = death_records([{"ts": 50, "facts": {}}], [], [{"ts": 40, "pos": "[1.0d, -2d, 3d]"}], 0, 100)[0]
    assert row["position"] == [1,-2,3]
    assert row["cause"] == "unknown"


def test_failure_matching_protection_window_native_dedup_and_structural_plan(tmp_path):
    def plan(titles, revision):
        return {"event": "tool_result", "tool": "update_plan", "success": True,
                "full_result": {"metrics": {"plan": {"plan_id": "p", "revision": revision, "steps": [{"title": t} for t in titles]}}}}
    native = {"seq": 42, "name": "mineDone", "bot": "Bot", "data": {"action_id": "a", "success": False, "reason": "blocked"}}
    write_trace(tmp_path, [
        {"event": "call_supervision_request", "tool_call_id": "x", "tool": "dig", "purpose": "running_frame",
         "request": {"questions": {"q": {"criteria": {"call_agent": "ask", "keep_going": "continue"}}}}},
        {"event": "call_supervision_result", "tool_call_id": "x", "tool": "dig", "purpose": "running_frame", "choice": "call_agent"},
        {"event": "call_supervision_result", "choice": "unknown", "purpose": "idle_frame"},
        {"event": "call_supervision_result", "choice": "surface", "purpose": "idle_frame", "ts": 104},
        {"event": "call_supervision_result", "choice": "move_away", "purpose": "idle_frame", "ts": 150},
        {"event": "reflex_death", "ts": 165},
        {"event": "body_events", "events": [native, native]},
        {"event": "tool_result", "tool": "dig", "success": False, "reason": "blocked", "args_hash": "same"},
        {"event": "tool_result", "tool": "dig", "success": False, "reason": "blocked", "args_hash": "same"},
        plan(["mine"],1), plan(["mine"],2), plan(["shelter"],3),
    ])
    records = list(extract_failures(tmp_path))
    kinds = [r["kind"] for r in records]
    assert kinds.count("protection_followed_by_death") == 1
    assert kinds.count("action_terminal_failure") == 1
    assert kinds.count("tool_failure") == 2
    assert kinds.count("plan_rewritten") == 1
    assert "repeated_failure_unchanged_body" not in kinds
    handback = next(r for r in records if r["kind"] == "call_agent")
    assert handback["question_options"]["q"]["call_agent"] == "ask"
    assert handback["trace_seqs"] == [2,1]
    assert all(len(r["context"]) <= 300 for r in records)
    out = tmp_path / "out"
    first = write_failures(tmp_path, out)
    write_failures(tmp_path, out)
    assert (out / "report.md").read_text().count("## Failure corpus") == 1
    assert sum(r["count"] for r in first["groups"]) == len(records)


def test_report_synthetic_reproduces_completed_request_count_and_usage(tmp_path):
    run, server = tmp_path / "run", tmp_path / "server"
    write_trace(run, [
        {"event": "user_message", "command": "start", "ts": 100},
        {"event": "turn_decision_context", "intent_kind": "start", "ts": 100},
        {"event": "agent_start", "ts": 100}, {"event": "llm_start", "ts": 101},
        {"event": "llm_end", "ts": 103},
        {"event": "model_usage", "usage": {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 5}, "output_tokens_details": {"reasoning_tokens": 4}}},
        {"event": "call_supervision_request", "purpose": "idle_frame", "request": {}},
        {"event": "decision_request"},
        {"event": "call_supervision_result", "purpose": "idle_frame", "choice": "stay", "decision_ms": 50},
        {"event": "model_tool_call", "tool": "dig"},
        {"event": "tool_result", "tool": "dig", "success": False, "reason": "blocked"},
        {"event": "ladder_low", "running": None, "items": 0},
        {"event": "ladder_low", "reason": "none_started", "idle_s": 61, "offered": ["camp"]},
        {"event": "event_loop_stalled", "stalled_s": 2},
        {"event": "llm_start", "ts": 119}, {"event": "session_terminal", "ts": 120},
    ])
    world = server / "eval"
    (world / "stats").mkdir(parents=True)
    (world / "advancements").mkdir()
    (server / "usercache.json").write_text(json.dumps([{"name": "Bot", "uuid": "id"}]))
    (world / "stats" / "id.json").write_text(json.dumps({"stats": {"minecraft:custom": {"minecraft:deaths": 0}, "minecraft:killed": {"minecraft:ender_dragon": 1}}}))
    (world / "advancements" / "id.json").write_text(json.dumps({
        "minecraft:story/root": {"done": True, "criteria": {"first": "1970-01-01 00:01:41 +0000", "last": "1970-01-01 00:01:45 +0000"}},
        "minecraft:recipes/x": {"done": True, "criteria": {}},
    }))
    make_level(world / "level.dat")
    (run / "interventions.jsonl").write_text(json.dumps({"ts": 111, "kind": "restart", "note": "operator restarted"}) + "\n")
    report = build_report(run, server, "eval", "Bot")
    assert report["run"]["duration_s"] == 20
    assert report["run"]["seed"] == -17
    assert report["run"]["interventions"][0]["kind"] == "restart"
    assert report["progress"]["passed"] is True
    assert report["progress"]["advancements"][0]["ts"] == 105
    assert report["agent"]["model_requests"]["count"] == 1
    assert report["agent"]["model_requests"]["started"] == 2
    assert report["agent"]["model_requests"]["latency_s"]["p50"] == 2
    assert report["agent"]["usage"]["cache_read_ratio"] == .5
    assert report["agent"]["wake_ups"]["by_reason"] == {"user_message": 1}
    assert report["decision"]["decision_request_count"] == 1
    assert report["body"]["model_tool_calls_by_tool"] == {"dig": 1}
    assert report["engineering"]["stalled_s"] == 2
    assert (report["ladder"]["ladder_low"], report["ladder"]["ladder_low_none_started"]) == (2, 1)
    assert "## Decision" in render_report(report)
    assert "(items offered but none started: 1)" in render_report(report)


def test_trace_bounds_ignore_nested_timestamps(tmp_path):
    rows = write_trace(tmp_path, [
        {"event": "first", "ts": 100, "payload": {"ts": 1}},
        {"event": "middle", "ts": 110, "payload": {"ts": 0}},
        {"event": "last", "ts": 120, "payload": {"ts": 999999}},
    ])
    assert trace_bounds(tmp_path / "session" / "trace.jsonl") == (100,120)


def test_modern_world_generation_nbt_root(tmp_path):
    path = tmp_path / "world_gen_settings.dat"
    path.write_bytes(gzip.compress(b'\x0a\x00\x00' + b'\x0a' + nbt_string("data") +
                                   b'\x04' + nbt_string("seed") + struct.pack(">q", -123) + b'\x00\x00'))
    assert read_level(path)["seed"] == -123


def test_trace_events_restore_facts_from_frame_facts_lines(tmp_path):
    death = {"event": "reflex_death", "seq": 3, "ts": 103,
             "facts": {"position": [1.5, 64.0, -2.5], "dimension": "overworld", "pad": "x" * 600}}
    rows = [{"event": "session_start", "seq": 1, "ts": 101}, death,
            {"event": "llm_end", "seq": 4, "ts": 104}]
    writer = TraceFactsWriter()
    path = tmp_path / "trace.jsonl"
    path.write_text("".join(encode_line(row) for record in rows for row in writer.encode(record)))
    assert '"frame_facts"' in path.read_text()
    assert list(trace_events(path, {"reflex_death"})) == [death]
    assert [row["event"] for row in trace_events(path, {"llm_end"})] == ["llm_end"]
    assert trace_bounds(path) == (101, 104)
