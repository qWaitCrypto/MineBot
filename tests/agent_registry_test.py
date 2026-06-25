#!/usr/bin/env python3
"""Deterministic unit test for the tool registry + progress weld (seams ①②).

Pure agent-core: fake Body + fake callables, no SDK, no live server, no network.
Proves two-face separation, the read-only bypass, the 7-step mutating weld,
stale-generation neutrality, single-writer rejection, and sensor trips.

Run:  python tests/agent_registry_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.brain.progress import ProgressAuthority  # noqa: E402
from minebot.brain.registry import (  # noqa: E402
    ProgressAbort,
    RegisteredTool,
    SingleWriterGuard,
    ToolRegistry,
    ToolSidecar,
    WeldContext,
    execute_tool,
)
from minebot.contract import BodyState, ToolResult  # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


class FakeBody:
    """Minimal Body stub: emits a BodyState whose fingerprint we can steer."""

    bot_name = "TestBot"

    def __init__(self) -> None:
        self.x = 0.0
        self.inv = "empty"
        self.get_state_calls = 0

    def get_state(self) -> BodyState:
        self.get_state_calls += 1
        return BodyState(
            bot=self.bot_name,
            pos=(self.x, 64.0, 0.0),
            yaw=None,
            pitch=None,
            health=20.0,
            food=20,
            oxygen=None,
            inventory_raw=self.inv,
            inventory_hash=self.inv,
            effects=None,
            time=1000,
            weather=None,
            dimension=None,
            complete=True,
        )


def tool(name: str, callable_, *, mutating: bool, key: str | None = None) -> RegisteredTool:
    return RegisteredTool(
        name=name,
        description=f"{name} tool",
        input_schema={"type": "object", "properties": {}},
        callable=callable_,
        sidecar=ToolSidecar(progress_key=key or name, mutating=mutating, permission="none"),
    )


def ctx_for(body: FakeBody, goal: str = "test goal") -> WeldContext:
    return WeldContext(body=body, authority=ProgressAuthority(), goal_text=goal)


# --- registry: two faces ----------------------------------------------------

def test_two_faces() -> None:
    reg = ToolRegistry()
    reg.register(tool("read_state", lambda p: ToolResult(True, "completed", False), mutating=False))
    fw = reg.framework_tools()
    check("framework_tools exposes exactly name/description/input_schema",
          fw and set(fw[0]) == {"name", "description", "input_schema"})
    check("sidecar NOT in framework face", "permission" not in fw[0] and "progress_key" not in fw[0])
    side = reg.sidecar("read_state")
    check("sidecar carries progress_key + mutating", side.progress_key == "read_state" and side.mutating is False)


def test_register_guards() -> None:
    reg = ToolRegistry()
    reg.register(tool("a", lambda p: ToolResult(True, "completed", False), mutating=False))
    try:
        reg.register(tool("a", lambda p: ToolResult(True, "completed", False), mutating=False))
    except ValueError:
        check("duplicate tool name rejected", True)
    else:
        check("duplicate tool name rejected", False)
    try:
        RegisteredTool("b", "d", {}, lambda p: ToolResult(True, "x", False),
                       ToolSidecar(progress_key="", mutating=True))
        reg.register(RegisteredTool("b", "d", {}, lambda p: ToolResult(True, "x", False),
                                    ToolSidecar(progress_key="", mutating=True)))
    except ValueError:
        check("empty progress_key rejected", True)
    else:
        check("empty progress_key rejected", False)
    try:
        reg.get("nope")
    except KeyError:
        check("unknown tool get -> KeyError", True)
    else:
        check("unknown tool get -> KeyError", False)


# --- read-only bypass -------------------------------------------------------

def test_readonly_bypass() -> None:
    body = FakeBody()
    ctx = ctx_for(body)
    t = tool("read_state", lambda p: ToolResult(True, "completed", False, metrics={"hp": 20}), mutating=False)
    out = execute_tool(t, {}, ctx)
    check("read-only returns payload", out["success"] is True and out["reason"] == "completed")
    check("read-only does NOT touch authority (no progress step)",
          ctx.authority.last_fingerprint == "" and ctx.authority.stalled_steps == 0)
    check("read-only does NOT call body.get_state (pure callable)", body.get_state_calls == 0)


# --- mutating weld happy path ----------------------------------------------

def test_mutating_progress() -> None:
    body = FakeBody()
    ctx = ctx_for(body)

    def move(p):
        body.x += 1.0  # world changes -> fingerprint progresses
        return ToolResult(True, "arrived", False, metrics={"pos": body.x})

    t = tool("move_to", move, mutating=True)
    out = execute_tool(t, {"target": [1, 64, 0]}, ctx)
    check("mutating returns terminal payload", out["success"] is True and out["reason"] == "arrived")
    check("mutating reads pre+post fingerprint (2 get_state)", body.get_state_calls == 2)
    check("authority noted a step", ctx.authority.last_action is not None)
    check("writer released after call", ctx.writer.holder is None)


# --- stale generation -> neutral preempted ----------------------------------

def test_stale_generation_neutral() -> None:
    body = FakeBody()
    ctx = ctx_for(body)

    def usurped(p):
        # Simulate a higher-priority owner bumping the generation mid-call.
        ctx.authority.invalidate_generation("reflex")
        return ToolResult(True, "arrived", False)

    t = tool("move_to", usurped, mutating=True)
    out = execute_tool(t, {}, ctx)
    check("stale generation -> preempted sentinel", out["reason"] == "preempted")
    check("preempted is truthy-neutral (success True, paused)", out["success"] is True and out["metrics"]["paused"] is True)
    check("preempted does NOT increment failure storm", ctx.authority.failure_steps == 0)
    check("preempted not noted as fresh world truth", ctx.authority.last_action is None)
    check("writer released after preemption", ctx.writer.holder is None)


# --- single-writer rejection ------------------------------------------------

def test_single_writer_rejects_second() -> None:
    body = FakeBody()
    ctx = ctx_for(body)
    reentrant: list = []

    def outer(p):
        # While "in flight", a second mutating tool is attempted for same bot.
        inner = tool("place", lambda q: ToolResult(True, "completed", False), mutating=True, key="place")
        reentrant.append(execute_tool(inner, {}, ctx))
        return ToolResult(True, "arrived", False)

    t = tool("move_to", outer, mutating=True)
    execute_tool(t, {}, ctx)
    check("second mutating tool rejected as owner_busy", reentrant[0]["reason"] == "owner_busy")
    check("rejection is can_retry truth", reentrant[0]["canRetry"] is True)


def test_writer_guard_unit() -> None:
    g = SingleWriterGuard()
    check("acquire succeeds when free", g.try_acquire("k1") is True)
    check("second acquire blocked", g.try_acquire("k2") is False)
    check("holder reported", g.holder == "k1")
    g.release("k1")
    check("after release, acquire succeeds", g.try_acquire("k2") is True)


# --- failure storm trips into ProgressAbort (yield) -------------------------

def test_failure_storm_yields() -> None:
    body = FakeBody()
    ctx = ctx_for(body)
    # Same failing action, world frozen -> failure_steps climbs to the limit.
    t = tool("mine", lambda p: ToolResult(False, "blocked", True), mutating=True)
    aborted = False
    calls = 0
    try:
        for _ in range(10):
            calls += 1
            execute_tool(t, {"target": [9, 9, 9]}, ctx)
    except ProgressAbort:
        aborted = True
    check("repeated failure trips ProgressAbort (yield, not crash)", aborted)
    check("tripped at/under failure-storm limit", calls <= 5, f"calls={calls}")
    check("writer released even when weld aborts", ctx.writer.holder is None)


# --- stagnation: same action, world unchanged -------------------------------

def test_stagnation_yields() -> None:
    body = FakeBody()
    ctx = ctx_for(body)
    # Succeeds every time but world never changes & same action repeated.
    t = tool("idle_move", lambda p: ToolResult(True, "completed", False), mutating=True)
    aborted = False
    try:
        for _ in range(10):
            execute_tool(t, {"same": "input"}, ctx)
    except ProgressAbort:
        aborted = True
    check("stagnant repeats (success but frozen world) trip ProgressAbort", aborted)


def main() -> int:
    for test in (
        test_two_faces,
        test_register_guards,
        test_readonly_bypass,
        test_mutating_progress,
        test_stale_generation_neutral,
        test_single_writer_rejects_second,
        test_writer_guard_unit,
        test_failure_storm_yields,
        test_stagnation_yields,
    ):
        test()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
