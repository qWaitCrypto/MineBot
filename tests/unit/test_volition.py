"""F4 volition: stance wake gating and campaign artifact invariants.

brain-cognitive-framework.md §7. Two rules dominate these tests: a campaign
is an artifact and never an engine, and an ungranted stance changes nothing
about which Body events wake the model.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

from minebot.app.body_events import _coalesce_material_events
from minebot.app.campaign_store import InMemoryCampaignStore, InMemoryStanceStore
from minebot.brain.volition import (
    CampaignError,
    CampaignRecord,
    GoalLock,
    ObjectiveNode,
    ObjectiveStatus,
    StanceConstraints,
    StanceKind,
    StancePolicy,
    StanceWakeGate,
    validate_campaign,
)
from minebot.contract import Event


def _event(name: str, seq: int = 1) -> Event:
    return Event(seq=seq, tick=seq, bot="Bot1", name=name, data={})


def _guard(max_wakes_per_minute: int = 4) -> StancePolicy:
    return StancePolicy(
        stance=StanceKind.GUARD,
        granted_by="qWait",
        constraints=StanceConstraints(max_wakes_per_minute=max_wakes_per_minute),
    )


class StanceGrantTests(unittest.TestCase):
    def test_default_stance_is_inactive_and_wakes_nothing(self) -> None:
        policy = StancePolicy.none()
        self.assertFalse(policy.active)
        self.assertEqual(policy.wake_events(), frozenset())

    def test_a_stance_without_a_grantor_is_not_active(self) -> None:
        # A stance must be an explicit human act; a bare kind is not a grant.
        policy = StancePolicy(stance=StanceKind.GUARD)
        self.assertFalse(policy.active)
        self.assertEqual(policy.wake_events(), frozenset())

    def test_granted_guard_adds_ambient_wake_events(self) -> None:
        self.assertIn("hostileNearby", _guard().wake_events())

    def test_revoking_returns_to_no_ambient_wakes(self) -> None:
        store = InMemoryStanceStore(_guard())
        self.assertTrue(store.get().active)
        store.put(StancePolicy.none())
        self.assertFalse(store.get().active)
        self.assertEqual(store.get().wake_events(), frozenset())


class StanceWakeGateTests(unittest.TestCase):
    def test_gate_declines_everything_without_a_grant(self) -> None:
        gate = StanceWakeGate()
        decision = gate.decide("hostileNearby", now=0.0)
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, "no_stance")

    def test_gate_declines_events_outside_the_stance(self) -> None:
        gate = StanceWakeGate(_guard())
        decision = gate.decide("craftDone", now=0.0)
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, "not_stance_relevant")

    def test_gate_enforces_the_owner_configured_wake_budget(self) -> None:
        gate = StanceWakeGate(_guard(max_wakes_per_minute=2))
        for index in range(2):
            self.assertTrue(gate.decide("hostileNearby", now=float(index)).allow)
            gate.record(now=float(index))

        blocked = gate.decide("hostileNearby", now=3.0)
        self.assertFalse(blocked.allow)
        self.assertEqual(blocked.reason, "stance_rate_limited")

        self.assertTrue(gate.decide("hostileNearby", now=61.0).allow)


class EventPumpStanceIntegrationTests(unittest.TestCase):
    def test_no_gate_reproduces_todays_material_selection(self) -> None:
        events = [_event("hostileNearby", 1), _event("death", 2), _event("craftDone", 3)]
        selected = _coalesce_material_events(events)
        self.assertEqual([event.name for event in selected], ["death"])

    def test_granted_stance_promotes_its_events_to_material(self) -> None:
        events = [_event("hostileNearby", 1), _event("death", 2), _event("craftDone", 3)]
        selected = _coalesce_material_events(
            events, stance_gate=StanceWakeGate(_guard()), now=0.0
        )
        self.assertEqual([event.name for event in selected], ["hostileNearby", "death"])

    def test_stance_rate_limit_never_suppresses_already_material_events(self) -> None:
        gate = StanceWakeGate(_guard(max_wakes_per_minute=1))
        events = [
            _event("hostileNearby", 1),
            _event("hostileNearby", 2),
            _event("death", 3),
            _event("bodyMissing", 4),
        ]
        selected = _coalesce_material_events(events, stance_gate=gate, now=0.0)
        names = [event.name for event in selected]
        self.assertIn("death", names)
        self.assertIn("bodyMissing", names)
        # Only one ambient wake survived the budget (coalescing keeps the last).
        self.assertEqual(names.count("hostileNearby"), 1)


class CampaignArtifactTests(unittest.TestCase):
    def _campaign(self) -> CampaignRecord:
        return CampaignRecord(
            campaign_id="dragon",
            title="Ender Dragon",
            mission="kill the ender dragon",
            objectives=(
                ObjectiveNode("iron", "get iron gear"),
                ObjectiveNode("portal", "build a nether portal", depends_on=("iron",)),
                ObjectiveNode("eyes", "collect ender eyes", depends_on=("portal",)),
            ),
        )

    def test_unblocked_projection_respects_dependencies(self) -> None:
        campaign = self._campaign()
        self.assertEqual(
            [node.node_id for node in campaign.unblocked_objectives()], ["iron"]
        )

        advanced = campaign.with_objective_status(
            "iron", ObjectiveStatus.DONE, evidence_handles=("observation:1",)
        )
        self.assertEqual(
            [node.node_id for node in advanced.unblocked_objectives()], ["portal"]
        )

    def test_done_and_blocked_transitions_require_evidence(self) -> None:
        campaign = self._campaign()
        for status in (ObjectiveStatus.DONE, ObjectiveStatus.BLOCKED):
            with self.assertRaises(CampaignError):
                campaign.with_objective_status("iron", status)

    def test_ordinary_transitions_need_no_evidence(self) -> None:
        campaign = self._campaign().with_objective_status("iron", ObjectiveStatus.ACTIVE)
        self.assertIs(campaign.objective("iron").status, ObjectiveStatus.ACTIVE)

    def test_locked_mission_resists_casual_replacement(self) -> None:
        campaign = CampaignRecord(
            campaign_id="dragon",
            title="Ender Dragon",
            mission="kill the ender dragon",
            goal_lock=GoalLock.LOCKED,
        )
        with self.assertRaises(CampaignError):
            campaign.with_mission("go fishing")
        self.assertEqual(
            campaign.with_mission("go fishing", formal_command=True).mission, "go fishing"
        )

    def test_mutable_mission_may_be_redirected(self) -> None:
        campaign = self._campaign().with_mission("gather wood instead")
        self.assertEqual(campaign.mission, "gather wood instead")

    def test_validation_rejects_cycles_unknown_and_duplicate_dependencies(self) -> None:
        cyclic = CampaignRecord(
            campaign_id="c",
            title="t",
            mission="m",
            objectives=(
                ObjectiveNode("a", "a", depends_on=("b",)),
                ObjectiveNode("b", "b", depends_on=("a",)),
            ),
        )
        with self.assertRaises(CampaignError):
            validate_campaign(cyclic)

        unknown = CampaignRecord(
            campaign_id="c",
            title="t",
            mission="m",
            objectives=(ObjectiveNode("a", "a", depends_on=("ghost",)),),
        )
        with self.assertRaises(CampaignError):
            validate_campaign(unknown)

        duplicate = CampaignRecord(
            campaign_id="c",
            title="t",
            mission="m",
            objectives=(ObjectiveNode("a", "a"), ObjectiveNode("a", "again")),
        )
        with self.assertRaises(CampaignError):
            validate_campaign(duplicate)

    def test_store_validates_on_write_and_round_trips(self) -> None:
        store = InMemoryCampaignStore()
        store.put(self._campaign())
        self.assertEqual(store.get("dragon").mission, "kill the ender dragon")
        self.assertEqual(len(store.all()), 1)
        self.assertTrue(store.delete("dragon"))
        self.assertIsNone(store.get("dragon"))

        with self.assertRaises(CampaignError):
            store.put(
                CampaignRecord(
                    campaign_id="bad",
                    title="t",
                    mission="m",
                    objectives=(ObjectiveNode("a", "a", depends_on=("a",)),),
                )
            )


class AntiEngineGuardTests(unittest.TestCase):
    """The campaign artifact must never be consumed by the spine (C1/C4)."""

    SPINE_MODULES = (
        "minebot/app/session.py",
        "minebot/app/runner.py",
        "minebot/app/work_queue.py",
        "minebot/app/autonomy.py",
        "minebot/brain/progress.py",
    )

    def test_no_spine_module_consumes_campaign_projections(self) -> None:
        offenders: list[str] = []
        for path in self.SPINE_MODULES:
            source = Path(path).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in {
                    "unblocked_objectives",
                    "objectives",
                }:
                    offenders.append(f"{path}:{node.attr}")
                if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                    ("volition", "campaign_store")
                ):
                    offenders.append(f"{path}:import")
        self.assertEqual(
            offenders,
            [],
            "a campaign is an artifact the model reads, never a scheduler the "
            "spine consumes; wiring it into spine control flow re-creates the "
            "continuation-engine antipattern at campaign scale.",
        )


if __name__ == "__main__":
    unittest.main()
