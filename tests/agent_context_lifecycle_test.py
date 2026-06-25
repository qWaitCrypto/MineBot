#!/usr/bin/env python3
"""Deterministic unit test for AgentContext (seam ③) and LifecycleController (④).

Pure agent-core: stdlib + contract only, no SDK, no Body, no network.

Run:  python tests/agent_context_lifecycle_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.brain.context import AgentContext  # noqa: E402
from minebot.brain.lifecycle import (  # noqa: E402
    LifecycleController,
    LifecycleError,
    LifecycleState,
)
from minebot.brain.modes import RuntimeProfile  # noqa: E402
from minebot.contract import BodyState  # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def make_state(x: float = 0.0, dim: str | None = None) -> BodyState:
    return BodyState(
        bot="TestBot", pos=(x, 64.0, 0.0), yaw=None, pitch=None, health=20.0, food=18,
        oxygen=None, inventory_raw="x", inventory_hash="x", effects=None, time=1000,
        weather=None, dimension=dim, complete=True,
    )


# --- AgentContext -----------------------------------------------------------

def test_goal_single_ownership() -> None:
    ctx = AgentContext(system_prompt="sys", goal_text="mine 10 iron", goal_reinject_every=3)
    ctx.begin_turn()
    check("goal injected on turn 1", "GOAL: mine 10 iron" in ctx.turn_preamble())
    ctx.begin_turn()
    check("goal NOT injected on turn 2 (within cadence)", "GOAL:" not in ctx.turn_preamble())
    ctx.begin_turn()
    check("turn 3 still within window", "GOAL:" not in ctx.turn_preamble())
    ctx.begin_turn()
    check("goal re-injected on turn 4 (cadence=3)", "GOAL: mine 10 iron" in ctx.turn_preamble())


def test_set_goal_resets_cadence() -> None:
    ctx = AgentContext(system_prompt="sys", goal_text="a", goal_reinject_every=5)
    for _ in range(3):
        ctx.begin_turn()
    ctx.set_goal("build a shelter")
    ctx.begin_turn()
    pre = ctx.turn_preamble()
    check("new goal appears immediately after set_goal", "GOAL: build a shelter" in pre)
    check("old goal gone", "GOAL: a" not in pre)


def test_state_injection_every_turn() -> None:
    ctx = AgentContext(system_prompt="sys", goal_text="g", goal_reinject_every=2)
    ctx.observe_state(make_state(x=12.5, dim="the_nether"))
    ctx.begin_turn()
    pre = ctx.turn_preamble()
    check("state injected", "STATE:" in pre and "health=20.0" in pre and "food=18" in pre)
    check("dimension surfaced", "the_nether" in pre)
    ctx.observe_state(make_state(x=99.0))
    ctx.begin_turn()
    check("state refreshes each turn", "99.0" in ctx.turn_preamble())


def test_profile_injection() -> None:
    ctx = AgentContext(system_prompt="sys", goal_text="g")
    ctx.observe_profile(
        RuntimeProfile(
            relationship="autonomous.user_request",
            situational="mobility",
            lifecycle="active",
            goal_lock="mutable",
            context_frame="Mobility frame",
            tool_focus=("navigation", "perception"),
            model_route="primary",
            effort="standard",
            policy_tags=("mobility",),
        )
    )
    ctx.begin_turn()
    pre = ctx.turn_preamble()
    check("profile injected", "PROFILE:" in pre and "situational=mobility" in pre)
    check("tool focus injected", "focus=navigation,perception" in pre)


def test_no_state_no_crash() -> None:
    ctx = AgentContext(system_prompt="sys", goal_text="g")
    ctx.begin_turn()
    check("preamble works before any state observed", "GOAL: g" in ctx.turn_preamble())


# --- LifecycleController ----------------------------------------------------

def test_single_active_entry() -> None:
    lc = LifecycleController()
    check("starts INIT", lc.state is LifecycleState.INIT)
    lc.ready()
    lc.start()
    check("normal entry IDLE->ACTIVE", lc.is_active)
    # The only other path into ACTIVE is RESUMING->ACTIVE.
    inbound = [s for s, targets in _inbound_to_active() if LifecycleState.ACTIVE in targets]
    check("exactly two states can reach ACTIVE (IDLE, RESUMING)",
          set(inbound) == {LifecycleState.IDLE, LifecycleState.RESUMING},
          f"inbound={inbound}")


def _inbound_to_active():
    from minebot.brain.lifecycle import _TRANSITIONS
    return _TRANSITIONS.items()


def test_no_backdoor_into_active() -> None:
    lc = LifecycleController()
    lc.ready()
    lc.start()
    lc.yield_()
    check("now YIELDED", lc.state is LifecycleState.YIELDED)
    try:
        lc.transition(LifecycleState.ACTIVE)  # YIELDED->ACTIVE must be illegal
    except LifecycleError:
        check("YIELDED->ACTIVE is forbidden (no backdoor)", True)
    else:
        check("YIELDED->ACTIVE is forbidden (no backdoor)", False)


def test_symmetric_resume() -> None:
    lc = LifecycleController()
    lc.ready()
    lc.start()
    lc.yield_()
    lc.resume()
    check("YIELDED->RESUMING ok", lc.state is LifecycleState.RESUMING)
    lc.reenter_active()
    check("RESUMING->ACTIVE re-enters via the sanctioned door", lc.is_active)


def test_recovery_has_resume_side() -> None:
    """The exact prior bug: a stop side (recovery) with no resume side."""
    lc = LifecycleController()
    lc.ready()
    lc.start()
    lc.enter_recovery()
    check("ACTIVE->RECOVERING ok", lc.state is LifecycleState.RECOVERING)
    lc.resume()
    lc.reenter_active()
    check("RECOVERING path returns to ACTIVE (symmetry restored)", lc.is_active)


def test_illegal_transition_lists_allowed() -> None:
    lc = LifecycleController()
    try:
        lc.start()  # INIT->ACTIVE illegal; must go through IDLE
    except LifecycleError as exc:
        check("illegal transition raises with allowed set", "idle" in str(exc))
    else:
        check("illegal transition raises with allowed set", False)


def test_history_tracked() -> None:
    lc = LifecycleController()
    lc.ready()
    lc.start()
    lc.yield_()
    lc.resume()
    lc.reenter_active()
    states = [s.value for s in lc.history]
    check("history records full path", states == ["init", "idle", "active", "yielded", "resuming", "active"])


def main() -> int:
    for test in (
        test_goal_single_ownership,
        test_set_goal_resets_cadence,
        test_state_injection_every_turn,
        test_profile_injection,
        test_no_state_no_crash,
        test_single_active_entry,
        test_no_backdoor_into_active,
        test_symmetric_resume,
        test_recovery_has_resume_side,
        test_illegal_transition_lists_allowed,
        test_history_tracked,
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
