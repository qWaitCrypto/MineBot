"""F5 principals: trust resolution, admission matrix, session integration.

brain-cognitive-framework.md §8. The matrix must gate *work creation* only
(never tool visibility), denied commands must degrade to conversation without
touching the execution lane, and the default configuration must reproduce
today's behavior exactly.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from minebot.app.principals import (
    AdmissionCapability,
    FRIENDS_ENV,
    InMemoryPrincipalStore,
    OPERATOR_PRINCIPAL_ID,
    OWNERS_ENV,
    PRINCIPAL_DB_ENV,
    PrincipalKind,
    PrincipalRecord,
    PrincipalRegistry,
    SqlitePrincipalStore,
    TrustTier,
    principal_registry_from_env,
)
from minebot.app.session import AgentSession, SessionCommand, SessionCommandKind
from minebot.app.work_queue import MemoryWorkIntentQueue, WorkIntentKind


def _enforcing(**kwargs) -> PrincipalRegistry:
    return PrincipalRegistry(
        owners=frozenset({"qWait"}),
        store=InMemoryPrincipalStore(),
        **kwargs,
    )


class TrustResolutionTests(unittest.TestCase):
    def test_empty_sender_is_owner_equivalent_operator(self) -> None:
        principal = _enforcing().resolve("")
        self.assertEqual(principal.principal_id, OPERATOR_PRINCIPAL_ID)
        self.assertIs(principal.kind, PrincipalKind.OPERATOR)
        self.assertIs(principal.trust, TrustTier.OWNER)

    def test_configured_owner_and_friend_and_default_stranger(self) -> None:
        registry = PrincipalRegistry(
            owners=frozenset({"qWait"}), friends=frozenset({"Guide"})
        )
        self.assertIs(registry.resolve("qWait").trust, TrustTier.OWNER)
        self.assertIs(registry.resolve("Guide").trust, TrustTier.FRIEND)
        self.assertIs(registry.resolve("Nobody").trust, TrustTier.STRANGER)

    def test_resolution_is_stable_and_promotion_persists_in_store(self) -> None:
        registry = _enforcing()
        first = registry.resolve("Visitor")
        self.assertIs(first.trust, TrustTier.STRANGER)
        registry.promote("Visitor", TrustTier.FRIEND)
        self.assertIs(registry.resolve("Visitor").trust, TrustTier.FRIEND)

    def test_open_registry_is_not_enforcing(self) -> None:
        self.assertFalse(PrincipalRegistry.open_registry().enforcing)
        self.assertTrue(_enforcing().enforcing)


class DurablePrincipalStoreTests(unittest.TestCase):
    """F5 §8.1 durable trust seam: promotion must survive a restart."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "state", "principals.db")

    def test_record_round_trip_including_granted_tuple(self) -> None:
        store = SqlitePrincipalStore(self.path)
        self.addCleanup(store.close)
        record = PrincipalRecord(
            principal_id="小明",
            kind=PrincipalKind.PLAYER,
            trust=TrustTier.FRIEND,
            granted=("stance:guard",),
            first_seen="2026-07-27T00:00:00Z",
            last_seen="2026-07-27T01:00:00Z",
            notes="met at spawn",
        )
        store.put(record)
        self.assertEqual(store.get("小明"), record)
        self.assertEqual(store.all(), (record,))
        self.assertIsNone(store.get("Nobody"))

    def test_put_is_an_upsert(self) -> None:
        store = SqlitePrincipalStore(self.path)
        self.addCleanup(store.close)
        base = PrincipalRecord("Steve", PrincipalKind.PLAYER, TrustTier.STRANGER)
        store.put(base)
        promoted = PrincipalRecord("Steve", PrincipalKind.PLAYER, TrustTier.FRIEND)
        store.put(promoted)
        self.assertEqual(store.get("Steve"), promoted)
        self.assertEqual(len(store.all()), 1)

    def test_promotion_survives_a_restart(self) -> None:
        registry = PrincipalRegistry(
            owners=frozenset({"qWait"}), store=SqlitePrincipalStore(self.path)
        )
        self.assertIs(registry.resolve("Visitor").trust, TrustTier.STRANGER)
        registry.promote("Visitor", TrustTier.FRIEND)
        registry.store.close()  # type: ignore[attr-defined]

        reopened = PrincipalRegistry(
            owners=frozenset({"qWait"}), store=SqlitePrincipalStore(self.path)
        )
        self.addCleanup(reopened.store.close)  # type: ignore[attr-defined]
        self.assertIs(reopened.resolve("Visitor").trust, TrustTier.FRIEND)
        # The stored record wins over the env-derived default: promotion is
        # the explicit owner act, config lists are only bootstrap.
        self.assertTrue(
            reopened.evaluate(AdmissionCapability.START_WORK, "Visitor").allowed
        )

    def test_env_opt_in_selects_the_durable_store(self) -> None:
        registry = principal_registry_from_env(
            {OWNERS_ENV: "qWait", PRINCIPAL_DB_ENV: self.path}
        )
        self.assertIsInstance(registry.store, SqlitePrincipalStore)
        registry.resolve("Guest")
        registry.store.close()  # type: ignore[attr-defined]
        reopened = SqlitePrincipalStore(self.path)
        self.addCleanup(reopened.close)
        self.assertIsNotNone(reopened.get("Guest"))

    def test_unset_env_keeps_the_in_memory_default(self) -> None:
        registry = principal_registry_from_env({OWNERS_ENV: "qWait"})
        self.assertIsInstance(registry.store, InMemoryPrincipalStore)


class EnvConfigTests(unittest.TestCase):
    def test_unset_owners_produce_an_inert_open_registry(self) -> None:
        registry = principal_registry_from_env({})
        self.assertFalse(registry.enforcing)
        self.assertTrue(
            registry.evaluate(AdmissionCapability.CONTROL_PROCESS, "Anyone").allowed
        )

    def test_configured_names_are_parsed_and_trimmed(self) -> None:
        registry = principal_registry_from_env(
            {OWNERS_ENV: " qWait , Second ", FRIENDS_ENV: "Guide,,Helper"}
        )
        self.assertTrue(registry.enforcing)
        self.assertEqual(registry.owners, frozenset({"qWait", "Second"}))
        self.assertEqual(registry.friends, frozenset({"Guide", "Helper"}))


class AdmissionMatrixTests(unittest.TestCase):
    def test_open_registry_allows_every_capability(self) -> None:
        registry = PrincipalRegistry.open_registry()
        for capability in AdmissionCapability:
            decision = registry.evaluate(capability, "AnyStranger")
            self.assertTrue(decision.allowed, capability)
            self.assertEqual(decision.reason, "admission_open")

    def test_owner_may_do_everything(self) -> None:
        registry = _enforcing()
        for capability in AdmissionCapability:
            self.assertTrue(registry.evaluate(capability, "qWait").allowed, capability)

    def test_stranger_may_only_converse(self) -> None:
        registry = _enforcing()
        self.assertTrue(registry.evaluate(AdmissionCapability.CONVERSE, "Visitor").allowed)
        for capability in (
            AdmissionCapability.START_WORK,
            AdmissionCapability.CONTROL_WORK,
            AdmissionCapability.CONTROL_PROCESS,
        ):
            decision = registry.evaluate(capability, "Visitor")
            self.assertFalse(decision.allowed, capability)
            self.assertEqual(decision.reason, "stranger_not_permitted")

    def test_friend_may_start_work_but_not_control_the_process(self) -> None:
        registry = PrincipalRegistry(owners=frozenset({"qWait"}), friends=frozenset({"Guide"}))
        self.assertTrue(registry.evaluate(AdmissionCapability.START_WORK, "Guide").allowed)
        self.assertFalse(
            registry.evaluate(AdmissionCapability.CONTROL_PROCESS, "Guide").allowed
        )

    def test_friend_controls_only_work_they_started(self) -> None:
        registry = PrincipalRegistry(owners=frozenset({"qWait"}), friends=frozenset({"Guide"}))
        own = registry.evaluate(
            AdmissionCapability.CONTROL_WORK, "Guide", work_owner_id="Guide"
        )
        self.assertTrue(own.allowed)
        self.assertEqual(own.reason, "own_work")
        other = registry.evaluate(
            AdmissionCapability.CONTROL_WORK, "Guide", work_owner_id="qWait"
        )
        self.assertFalse(other.allowed)
        self.assertEqual(other.reason, "not_work_owner")

    def test_owner_controls_work_started_by_someone_else(self) -> None:
        registry = PrincipalRegistry(owners=frozenset({"qWait"}), friends=frozenset({"Guide"}))
        decision = registry.evaluate(
            AdmissionCapability.CONTROL_WORK, "qWait", work_owner_id="Guide"
        )
        self.assertTrue(decision.allowed)


def _session(principals: PrincipalRegistry | None) -> AgentSession:
    return AgentSession(
        parts_factory=lambda goal: None,  # type: ignore[arg-type,return-value]
        work_queue=MemoryWorkIntentQueue(),
        principals=principals,
    )


class SessionAdmissionTests(unittest.TestCase):
    def test_default_session_has_no_principal_layer(self) -> None:
        session = _session(None)
        intent = session.submit(SessionCommand.start("collect 64 logs", sender="Stranger"))
        self.assertIs(intent.kind, WorkIntentKind.START)

    def test_open_registry_preserves_todays_behavior(self) -> None:
        session = _session(PrincipalRegistry.open_registry())
        for command, expected in (
            (SessionCommand.start("goal", sender="Stranger"), WorkIntentKind.START),
            (SessionCommand.cancel(sender="Stranger"), WorkIntentKind.CANCEL),
            (SessionCommand.quit(sender="Stranger"), WorkIntentKind.QUIT),
        ):
            self.assertIs(session.submit(command).kind, expected)

    def test_stranger_start_degrades_to_conversation_preserving_text(self) -> None:
        session = _session(_enforcing())
        intent = session.submit(SessionCommand.start("tear that house down", sender="Visitor"))

        self.assertIs(intent.kind, WorkIntentKind.MESSAGE)
        self.assertEqual(intent.payload["text"], "tear that house down")
        self.assertEqual(intent.payload["sender"], "Visitor")
        self.assertIn("admission_denied:start", str(intent.payload["reason"]))

    def test_stranger_quit_is_denied_and_described_as_dialogue(self) -> None:
        session = _session(_enforcing())
        intent = session.submit(SessionCommand.quit(sender="Visitor"))

        self.assertIs(intent.kind, WorkIntentKind.MESSAGE)
        self.assertEqual(intent.payload["text"], "(requested quit)")

    def test_owner_commands_pass_through_unchanged(self) -> None:
        session = _session(_enforcing())
        self.assertIs(
            session.submit(SessionCommand.start("collect", sender="qWait")).kind,
            WorkIntentKind.START,
        )
        self.assertIs(
            session.submit(SessionCommand.quit(sender="qWait")).kind, WorkIntentKind.QUIT
        )

    def test_console_and_internal_paths_stay_admitted_under_enforcement(self) -> None:
        # Console `/goal` and launcher paths carry no sender; denying them
        # would deny the harness its own commands.
        session = _session(_enforcing())
        self.assertIs(session.submit(SessionCommand.start("goal")).kind, WorkIntentKind.START)
        self.assertIs(session.submit(SessionCommand.quit()).kind, WorkIntentKind.QUIT)

    def test_goal_owner_is_recorded_so_friends_control_only_their_own_work(self) -> None:
        registry = PrincipalRegistry(owners=frozenset({"qWait"}), friends=frozenset({"Guide"}))
        session = _session(registry)

        session.submit(SessionCommand.start("guide goal", sender="Guide"))
        self.assertIs(
            session.submit(SessionCommand.cancel(sender="Guide")).kind, WorkIntentKind.CANCEL
        )

        session.submit(SessionCommand.start("owner goal", sender="qWait"))
        degraded = session.submit(SessionCommand.cancel(sender="Guide"))
        self.assertIs(degraded.kind, WorkIntentKind.MESSAGE)

    def test_stranger_conversation_is_always_admitted(self) -> None:
        session = _session(_enforcing())
        intent = session.submit(SessionCommand.message("hello!", sender="Visitor"))
        self.assertIs(intent.kind, WorkIntentKind.MESSAGE)
        self.assertEqual(intent.payload["reason"], "user_message")


if __name__ == "__main__":
    unittest.main()
