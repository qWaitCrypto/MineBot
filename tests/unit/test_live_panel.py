"""Live panel: telemetry projection, runtime wiring, the trace fold and page rendering."""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("rich", reason="the panel extra is not installed")
from rich.cells import cell_len  # noqa: E402

from minebot.app.client_body_tools import client_body_tools
from minebot.app.panel_telemetry import PanelTelemetry, panel_telemetry_enabled
from minebot.app.runtime_trace import RuntimeTrace
from minebot.app.wiring import build_agent_runtime
from minebot.brain.registry import ToolRegistry, execute_tool
from minebot.contract.execution import current_tool_observation
from minebot.game.real_client_body import RealClientBody
from minebot.game.real_client_ipc import RealClientTerminal
from minebot.panel.model import PanelState, Timeline, alerts, apply, fold
from minebot.panel.projection import progress_digest, project_progress
from minebot.panel.source import TraceFollower, latest_trace, resolve_trace
from minebot.panel.views import PAGES, render
from minebot.panel.views.common import Frame

ATTEMPT_5 = Path(os.environ.get("MINEBOT_ATTEMPT5_TRACE",
                                "logs/house-decor-20261008-231202/attempt-5-baritone-execution/trace.jsonl"))
EDIT = {"index": 0, "pos": [10, 127, -40], "before": "oak_planks", "after": "air", "op": "break",
        "action_kind": "clear", "target": False, "tick": 12}


class ListSink:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)

    def close(self):
        pass

    def named(self, event):
        return [record for record in self.records if record["event"] == event]


def native_sample(**overrides):
    sample = {
        "type": "progress", "action_id": "a1", "position": [10.537, 128.0, -39.5], "yaw": -272.25, "pitch": 88.0,
        "health": 20.0, "food": 20, "oxygen": 300, "selected_item": "dirt", "selected_slot": 1,
        "inventory": {"dirt": 41, "oak_planks": 13},
        "inventory_slots": [{"slot": 1, "slotType": "hotbar", "empty": False, "item": "minecraft:dirt", "count": 41}],
        "entities": [{"type": "zombie", "name": "Zombie", "pos": [14.0, 128.0, -39.0], "dist2": 16.0, "hostile": True},
                     {"type": "sheep", "name": "Sheep", "pos": [40.0, 64.0, 0.0], "dist2": 900.0, "hostile": False}],
        "navigation": {"src": [10, 128, -40], "dest": [10, 119, -40], "movement": "MovementFall", "path_position": 1,
                       "path_movements": 2, "idle_ms": 120, "pending_break": [[10, 127, -40]], "pending_place": [],
                       "unrelated": 1},
        "goal_active": True, "builder_active": False, "pathing": True, "baritone_goal": "GoalBlock{10,119,-40}",
        "path_events": {"CALC_STARTED": 1}, "heap_used_bytes": 1,
    }
    sample.update(overrides)
    return sample


# ----------------------------------------------------------------- projection and telemetry

def test_projection_keeps_panel_fields_and_drops_the_rest():
    frame = project_progress(native_sample())
    assert frame["pos"] == [10.54, 128.0, -39.5]
    assert frame["processes"]["goal"] is True and frame["processes"]["builder"] is False
    assert "unrelated" not in frame["navigation"] and frame["navigation"]["movement"] == "MovementFall"
    assert frame["entities_near"][0] == {"type": "zombie", "dist": 4.0, "hostile": True, "name": "Zombie",
                                         "pos": [14.0, 128.0, -39.0]}
    assert frame["hotbar"][1] == ["dirt", 41]
    assert "heap_used_bytes" not in frame
    assert progress_digest({**frame, "sampled_at_ms": 1}) == progress_digest({**frame, "sampled_at_ms": 2})


def test_telemetry_dedupes_frames_heartbeats_and_reports_each_edit_once():
    sink, clock = ListSink(), [100.0]
    trace = RuntimeTrace(sink=sink)
    telemetry = PanelTelemetry(trace, enabled=True, clock=lambda: clock[0])
    telemetry.observe("call-1", native_sample(world_edits=[EDIT]))
    telemetry.observe("call-1", native_sample(world_edits=[EDIT]))
    clock[0] += 2.5
    telemetry.observe("call-1", native_sample(lite=True))
    telemetry.observe("call-1", {"type": "terminal", "action_id": "a1"})
    frames = sink.named("body_progress")
    assert len(frames) == 2 and frames[1]["lite"] is True
    assert frames[0]["inventory_counts"] == {"dirt": 41, "oak_planks": 13}
    assert "inventory_counts" not in frames[1]
    edits = sink.named("world_edit")
    assert len(edits) == 1 and edits[0]["pos"] == [10, 127, -40] and edits[0]["tool_call_id"] == "call-1"
    assert trace.events == [] and trace.last_seq == 3


def test_telemetry_switch_and_scope():
    assert panel_telemetry_enabled({}) and not panel_telemetry_enabled({"MINEBOT_PANEL_TELEMETRY": "0"})
    sink = ListSink()
    disabled = PanelTelemetry(RuntimeTrace(sink=sink), enabled=False)
    disabled.observe("call", native_sample())
    disabled.sample_idle(native_sample())
    assert sink.records == []
    assert disabled.scoped(len, "call") is len
    enabled = PanelTelemetry(RuntimeTrace(sink=sink), enabled=True)
    seen = enabled.scoped(lambda: current_tool_observation()[0], "call-9")()
    assert seen == "call-9" and current_tool_observation() is None


class ProgressIpc:
    """Native client that streams one full frame, two lite frames and a terminal."""

    def connect(self):
        pass

    def close(self):
        pass

    def state(self):
        return {"position": [0.5, 5.0, 0.5], "health": 20, "food": 20, "inventory": {"dirt": 3},
                "disposable_world": True}

    def command(self, objective, *, action_id, timeout_ms, on_progress=None, cancel_after_s=None):
        del objective, timeout_ms, cancel_after_s
        on_progress(native_sample(action_id=action_id, world_edits=[EDIT]))
        on_progress({"type": "progress", "lite": True, "action_id": action_id, "position": [1.0, 5.0, 0.5],
                     "health": 19.0, "navigation": {"movement": "MovementTraverse"}})
        on_progress({"type": "progress", "lite": True, "action_id": action_id, "position": [1.0, 5.0, 0.5],
                     "health": 19.0, "navigation": {"movement": "MovementTraverse"}})
        return RealClientTerminal(action_id, "completed", "world_result_verified", {**self.state(), "type": "terminal"})


def test_runtime_traces_progress_for_every_body_mutation_without_touching_state_cache():
    asyncio.run(_runtime_traces_progress())


async def _runtime_traces_progress():
    body = RealClientBody("Bot", ProgressIpc())
    registry = ToolRegistry()
    for tool in client_body_tools(body):
        registry.register(tool)
    runtime = build_agent_runtime(body=body, registry=registry, goal_text="clear").runtime
    sink = ListSink()
    runtime.trace.sink = sink
    try:
        tool = registry.get("clear_area")
        await asyncio.wait_for(runtime.run_tool(tool, execute_tool, tool, {"start": [0, 5, 0], "end": [0, 5, 0], "intent": "clear the marked block"},
                                                runtime.weld_context, tool_call_id="call-clear", timeout_s=10), 5)
    finally:
        await runtime.aclose()
    frames = sink.named("body_progress")
    assert [frame["lite"] for frame in frames] == [False, True]
    assert all(frame["tool_call_id"] == "call-clear" for frame in frames)
    assert sink.named("world_edit")[0]["op"] == "break"
    cached = body._last_state_payload
    assert "world_edits" not in cached and "path_events" not in cached and "lite" not in cached
    assert cached["position"] != [1.0, 5.0, 0.5]
    assert not [event for event in runtime.trace.events if event["event"] in ("body_progress", "world_edit")]


def test_idle_sample_skips_while_an_action_owns_the_body():
    body = RealClientBody("Bot", ProgressIpc())
    assert body.sample_native_state() is None
    body._ready = True
    assert body.sample_native_state()["position"] == [0.5, 5.0, 0.5]
    body._active = object()
    assert body.sample_native_state() is None


# ----------------------------------------------------------------- source

def test_follower_yields_only_complete_lines_and_restarts_after_truncation(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_bytes(b'{"seq": 1, "event": "a"}\n{"seq": 2, "ev')
    follower = TraceFollower(path)
    records, reset = follower.read()
    assert [record["seq"] for record in records] == [1] and not reset
    with path.open("ab") as handle:
        handle.write(b'ent": "b"}\nnot json\n')
    records, _ = follower.read()
    assert [record["seq"] for record in records] == [2]
    path.write_bytes(b'{"seq": 9, "event": "c"}\n')
    records, reset = follower.read()
    assert reset and [record["seq"] for record in records] == [9]
    assert resolve_trace(tmp_path) == path and latest_trace(tmp_path) == path


# ----------------------------------------------------------------- fold

def synthetic_trace():
    t = 1000.0
    records = []

    def add(event, dt=0.1, **fields):
        nonlocal t
        t += dt
        records.append({"seq": len(records) + 1, "ts": t, "session_id": "s", "event": event, **fields})

    add("runtime_scope", bot_id="Bot", world_id="w1", server_id="srv")
    add("user_message", sender="", content="盖房子", command="continue")
    add("responses_prepare", tool_count=70)
    add("responses_http_attempt", attempt=1, request_key="k1")
    add("responses_http_phase", phase="http11.receive_response_headers.started", request_key="k1", attempt=1)
    add("responses_first_byte", elapsed_s=2.0, request_key="k1", attempt=1)
    add("responses_tool_started", tool="move_to", tool_call_id="c1", asynchronous=False)
    add("responses_terminal", status="completed", usage={"input_tokens": 1000, "output_tokens": 20,
        "input_tokens_details": {"cached_tokens": 800}}, cache_read_ratio=0.8, response_model="m")
    add("tool_invoke", tool="move_to", tool_call_id="c1", source="body.movement", permission="move", mutating=True,
        arguments_summary='{"pos": [11, 119, -40]}')
    add("body_progress", tool_call_id="c1", action_id="a1", source="action", lite=False,
        **project_progress(native_sample()))
    add("world_edit", tool_call_id="c1", action_id="a1", **EDIT)
    add("body_progress", 0.25, tool_call_id="c1", action_id="a1", source="action", lite=True, pos=[10.5, 120.0, -39.5],
        navigation={"movement": "MovementFall", "path_position": 2, "path_movements": 2, "idle_ms": 50},
        path_events={"CALC_STARTED": 1, "AT_GOAL": 1})
    add("tool_result", 1.0, tool="move_to", tool_call_id="c1", success=True, reason="arrived",
        full_result={"success": True, "reason": "arrived", "canRetry": False,
                     "metrics": {"position": [10.5, 119.0, -39.5], "inventory_before": {"oak_planks": 13},
                                 "inventory_after": {"oak_planks": 14}}})
    add("responses_tool_output", tool="move_to", tool_call_id="c1")
    add("tool_invoke", tool="dig_up", tool_call_id="c2", source="body.objective", permission="dig_vertical",
        mutating=True, arguments_summary='{"intent":"finish the requested body work"}')
    add("tool_result", 2.0, tool="dig_up", tool_call_id="c2", success=False, reason="vertical_no_path",
        full_result={"success": False, "reason": "vertical_no_path", "canRetry": True, "metrics": {}})
    add("assistant_message", content="檐口坏了，先修。")
    add("task_outcome", status="blocked", summary="eave broken", evidence=[])
    add("mystery_event")
    return records


def test_fold_reconstructs_calls_requests_body_and_world_edits():
    state = fold(synthetic_trace())
    assert state.bot == "Bot" and state.request_total == 1 and state.request.ttfb_s == 2.0
    assert state.usage["input"] == 1000 and state.cache_ratio == 0.8
    move, dig = state.calls["c1"], state.calls["c2"]
    assert move.success and move.reason == "arrived" and move.frames == 2 and move.delivered_ts
    assert move.inventory_delta == {"oak_planks": 1} and move.edits[0]["during_move"]
    assert dig.success is False and dig.can_retry and dig.group == "move"
    assert state.stats["dig_up"].failed == 1
    assert state.body["pos"] == [10.5, 119.0, -39.5] or state.body["pos"] == [10.5, 120.0, -39.5]
    assert [name for _, name in state.path_event_log] == ["CALC_STARTED", "AT_GOAL"]
    assert state.entities[0]["hostile"] and state.hotbar[1] == ["dirt", 41]
    assert state.outcome["status"] == "blocked" and state.unknown == {"mystery_event": 1}
    texts = [alert.text for alert in alerts(state, state.last_ts, live=False)]
    assert any("移动改了世界" in text for text in texts) and any("敌对靠近" in text for text in texts)


def test_timeline_keyframes_match_a_straight_fold():
    records = synthetic_trace() * 30
    for index, record in enumerate(records):
        record["seq"] = index + 1
    timeline = Timeline(records)
    for count in (0, 1, 199, 200, 201, 455, len(records)):
        expected = fold(records[:count])
        actual = timeline.state_at(count)
        assert (actual.events, actual.request_total, len(actual.calls), len(actual.world_edits)) == (
            expected.events, expected.request_total, len(expected.calls), len(expected.world_edits))


def test_malformed_records_are_counted_not_fatal():
    state = PanelState()
    for record in ({}, {"event": "tool_result", "tool_call_id": "x", "full_result": "bad"},
                   {"event": "body_state", "pos": "nowhere"}, {"event": "world_edit", "pos": None}):
        apply(state, record)
    assert state.events == 4


@pytest.mark.skipif(not ATTEMPT_5.exists(), reason="local house experiment trace not present")
def test_attempt_5_replay_matches_the_trace():
    records = [json.loads(line) for line in ATTEMPT_5.open()]
    state = fold(records)
    by_tool = {}
    for call in state.calls.values():
        by_tool.setdefault(call.tool, []).append(call)
    assert sum(stats.count for stats in state.stats.values()) == 60
    assert by_tool["dig_up"][0].reason == "vertical_no_path" and by_tool["dig_up"][0].can_retry
    assert [call.reason for call in by_tool["place_block"]] == ["placed_server_confirmed"] * 3
    move = by_tool["move_to"][0]
    assert move.reason == "arrived" and move.inventory_delta == {} and move.position[1] == 119.0
    assert state.outcome["status"] == "blocked" and not state.unknown


# ----------------------------------------------------------------- rendering

@pytest.mark.parametrize("size", ((80, 30), (120, 46), (200, 60)))
def test_every_page_renders_inside_the_terminal(size):
    records = synthetic_trace()
    state = fold(records)
    width, height = size
    for page in range(len(PAGES)):
        for now in (state.last_ts, state.last_ts + 0.3, state.last_ts + 60):
            frame = Frame(state=state, now=now, anim=time.monotonic(), live=page % 2 == 0, title="t", page=page,
                          replay=None if page % 2 == 0 else (len(records), len(records)), raw=records)
            canvas = render(frame, width, height, detail=records[0] if page == 6 else None)
            rows = canvas.plain_rows()
            assert len(rows) == height
            assert all(cell_len(row) == width for row in rows), (page, size)
            assert canvas.to_text().plain.count("\n") == height - 1


def test_overview_shows_the_running_call_and_the_flagged_edit():
    records = synthetic_trace()
    state = fold(records[:11])
    frame = Frame(state=state, now=state.last_ts + 0.2, anim=0.0, live=True, title="t")
    text = "\n".join(render(frame, 120, 46).plain_rows())
    assert "move_to" in text and "AGENT" in text and "BARITONE" in text
    assert "MovementFall" in text and "oak_planks" in text and "⚠" in text
