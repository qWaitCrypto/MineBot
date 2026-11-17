import pytest

from minebot.app.body_control import BodyControl


def test_claim_generation_and_settlement_are_explicit():
    control = BodyControl()
    initial = control.snapshot()
    claimed = control.claim("call-1")
    assert claimed.holder == "agent_call"
    assert claimed.stop_state == "running"
    assert claimed.generation > initial.generation
    assert control.owns("call-1", claimed.generation)
    settled = control.settle(claim_id="call-1")
    assert settled.holder == "none"
    assert settled.stop_state == "settled"
    assert not control.owns("call-1", claimed.generation)


def test_unknown_cannot_be_cleared_by_time_or_claim():
    control = BodyControl()
    control.claim("call-1")
    control.mark_unknown(claim_id="call-1")
    assert control.unknown
    with pytest.raises(RuntimeError, match="body_control_unknown"):
        control.claim("call-2")
    control.confirm_native_stopped(True)
    assert control.snapshot().stop_state == "settled"
    control.claim("call-2")


def test_late_claim_cannot_settle_new_owner():
    control = BodyControl()
    first = control.claim("call-1")
    control.settle(claim_id="call-1")
    second = control.claim("call-2")
    assert control.owns("call-2", second.generation)
    control.mark_unknown(claim_id="call-1")
    assert control.owns("call-2", second.generation)


def test_external_authority_invalidation_quarantines_current_owner():
    control = BodyControl()
    control.claim("plan-A", holder="agent_plan")
    control.authority.invalidate_generation("goal_replaced")
    snapshot = control.snapshot()
    assert snapshot.holder == "unknown"
    assert snapshot.stop_state == "unknown"
    assert control.unknown
    assert control.last_stop["reason"] == "goal_replaced"
    with pytest.raises(RuntimeError, match="body_control_unknown"):
        control.claim("plan-B")
