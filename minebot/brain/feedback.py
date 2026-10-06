"""Finite, strategy-relevant failure facts, distinct from execution permission."""

from __future__ import annotations


FAILURE_EVIDENCE_PREFIX = "approach_failure:"

# Only understood gameplay terminals teach a reusable failed-approach fact.
# Safety, cancellation, transport, and arbitrary exception text are not novelty.
_FAILURE_CLASSES = {
    "no_path": "unreachable",
    "resource_navigation_segment_budget_exhausted": "unreachable",
    "target_not_found": "resource_unavailable",
    "candidate_targets_exhausted": "resource_unavailable",
    "resource_candidates_not_found": "resource_unavailable",
    "resource_candidate_domain_exhausted": "resource_unavailable",
    "target_gone": "target_changed",
    "target_lost": "target_changed",
    "target_block_mismatch": "target_changed",
    "pickup_not_observed": "pickup_unobserved",
    "resource_domain_budget_exhausted": "work_budget_exhausted",
}


def failure_evidence_key(tactic_signature: str, reason: str) -> str | None:
    failure_class = _FAILURE_CLASSES.get(reason)
    if not tactic_signature or failure_class is None:
        return None
    return f"{FAILURE_EVIDENCE_PREFIX}{tactic_signature}:{failure_class}"
