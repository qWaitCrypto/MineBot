import pytest

from minebot.brain.feedback import failure_evidence_key
from minebot.contract.tool_trace import tool_tactic_signature


def test_failed_approach_identity_ignores_coordinates_counts_and_budgets():
    first = tool_tactic_signature("collect_resource", {"item": "logs", "count": 4, "budget": 4, "pos": [1, 2, 3]})
    renamed = tool_tactic_signature("collect_resource", {"item": "logs", "count": 64, "budget": 32, "pos": [7, 8, 9]})
    assert failure_evidence_key(first, "target_not_found") == failure_evidence_key(renamed, "candidate_targets_exhausted")
    different = tool_tactic_signature("collect_resource", {"item": "dirt", "count": 4})
    assert failure_evidence_key(first, "target_not_found") != failure_evidence_key(different, "target_not_found")


def test_lost_combat_target_is_a_failure_fact_not_a_new_budget():
    signature = tool_tactic_signature("engage_entity", {"target_spec": "nearest_hostile"})
    assert failure_evidence_key(signature, "target_lost") is not None
    assert failure_evidence_key(signature, "target_lost") == failure_evidence_key(signature, "target_gone")


@pytest.mark.parametrize("reason", [
    "governance_denied", "hazard_unresolved", "body_missing", "preempted",
    "canceled", "body_unavailable", "body_transport_unstable", "owner_busy",
    "action_reconciliation_unknown", "timeout", "unclassified_body_terminal",
])
def test_hard_stops_and_unknown_errors_are_not_reconsideration_evidence(reason):
    assert failure_evidence_key("move_to:signature", reason) is None
