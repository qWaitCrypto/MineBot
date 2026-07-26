"""G1 dialogue skeleton: grammar, confirmation slots, addressing windows.

in-game-dialogue.md. The fixture pack required by its §6 construction row:
grammar table, lock two-step, expiry, cross-principal confirm rejection, and
the open-registry fall-through. Plus the session-side inertness proof: the
new kinds admit correctly and degrade to conversation while unwired.
"""

from __future__ import annotations

import unittest

from minebot.app.dialogue import (
    AddressingWindows,
    AuthorityCommand,
    ConfirmationSlots,
    authority_grammar_enabled,
    mentions_name,
    parse_authority_command,
)
from minebot.app.principals import (
    AdmissionCapability,
    InMemoryPrincipalStore,
    PrincipalRegistry,
)
from minebot.app.session import (
    _ADMISSION_CAPABILITY,
    AgentSession,
    SessionCommand,
    SessionCommandKind,
)
from minebot.app.work_queue import MemoryWorkIntentQueue, WorkIntentKind


def _enforcing() -> PrincipalRegistry:
    return PrincipalRegistry(
        owners=frozenset({"qWait"}), store=InMemoryPrincipalStore()
    )


class GrammarTests(unittest.TestCase):
    def test_trust_parses_player_and_tier(self) -> None:
        parsed = parse_authority_command("/trust Steve friend")
        assert parsed is not None
        self.assertIs(parsed.kind, SessionCommandKind.TRUST)
        self.assertEqual(parsed.args, {"player": "Steve", "tier": "friend"})
        self.assertEqual(parsed.raw, "/trust Steve friend")

    def test_trust_tier_is_case_insensitive_but_validated(self) -> None:
        parsed = parse_authority_command("/TRUST 小明 OWNER")
        assert parsed is not None
        self.assertEqual(parsed.args, {"player": "小明", "tier": "owner"})
        self.assertIsNone(parse_authority_command("/trust Steve admin"))
        self.assertIsNone(parse_authority_command("/trust Steve"))
        self.assertIsNone(parse_authority_command("/trust Steve friend extra"))

    def test_stance_parses_kind_and_params(self) -> None:
        parsed = parse_authority_command("/stance guard radius=16 max_wake_rate=4")
        assert parsed is not None
        self.assertIs(parsed.kind, SessionCommandKind.STANCE)
        self.assertEqual(
            parsed.args,
            {"stance": "guard", "radius": "16", "max_wake_rate": "4"},
        )

    def test_stance_none_revokes_and_rejects_params(self) -> None:
        parsed = parse_authority_command("/stance none")
        assert parsed is not None
        self.assertEqual(parsed.args, {"stance": "none"})
        self.assertIsNone(parse_authority_command("/stance none radius=16"))
        self.assertIsNone(parse_authority_command("/stance fly"))
        self.assertIsNone(parse_authority_command("/stance guard radius="))
        self.assertIsNone(parse_authority_command("/stance"))

    def test_abandon_with_and_without_reason(self) -> None:
        bare = parse_authority_command("/abandon")
        assert bare is not None
        self.assertIs(bare.kind, SessionCommandKind.ABANDON)
        self.assertEqual(bare.args, {})
        reasoned = parse_authority_command("/abandon too dangerous tonight")
        assert reasoned is not None
        self.assertEqual(reasoned.args, {"reason": "too dangerous tonight"})

    def test_confirm_requires_exactly_one_token(self) -> None:
        parsed = parse_authority_command("/confirm a1b2c3")
        assert parsed is not None
        self.assertIs(parsed.kind, SessionCommandKind.CONFIRM)
        self.assertEqual(parsed.args, {"token": "a1b2c3"})
        self.assertIsNone(parse_authority_command("/confirm"))
        self.assertIsNone(parse_authority_command("/confirm a b"))

    def test_marked_formality_natural_language_never_parses(self) -> None:
        for line in (
            "trust Steve friend",          # unmarked
            "please /trust Steve friend",  # not line-leading
            "/trusty Steve friend",        # prefix must not bleed
            "把Steve设为朋友",
            "stop",
            "",
        ):
            self.assertIsNone(parse_authority_command(line), line)

    def test_session_command_projection_carries_raw_text(self) -> None:
        parsed = parse_authority_command("/abandon")
        assert parsed is not None
        command = parsed.session_command(sender="qWait")
        self.assertIs(command.kind, SessionCommandKind.ABANDON)
        self.assertEqual(command.text, "/abandon")
        self.assertEqual(command.sender, "qWait")
        self.assertEqual(command.reason, "dialogue_authority")


class FallThroughTests(unittest.TestCase):
    def test_open_registry_disables_authority_grammar(self) -> None:
        self.assertFalse(authority_grammar_enabled(None))
        self.assertFalse(authority_grammar_enabled(PrincipalRegistry.open_registry()))
        self.assertTrue(authority_grammar_enabled(_enforcing()))


class ConfirmationSlotTests(unittest.TestCase):
    def _slots(self, start: float = 100.0) -> tuple[ConfirmationSlots, list[float]]:
        now = [start]
        tokens = iter(f"tok{i}" for i in range(100))
        slots = ConfirmationSlots(
            clock=lambda: now[0], token_factory=lambda: next(tokens)
        )
        return slots, now

    def _action(self) -> SessionCommand:
        return SessionCommand(
            kind=SessionCommandKind.ABANDON, text="/abandon", sender="qWait"
        )

    def test_two_step_round_trip_returns_exact_action(self) -> None:
        slots, _now = self._slots()
        pending = slots.request("qWait", self._action())
        result = slots.confirm("qWait", pending.token)
        self.assertTrue(result.confirmed)
        assert result.pending is not None
        self.assertEqual(result.pending.action, self._action())
        # Consumed: a second confirm finds nothing.
        self.assertEqual(slots.confirm("qWait", pending.token).status, "no_pending")

    def test_cross_principal_confirm_is_rejected(self) -> None:
        slots, _now = self._slots()
        pending = slots.request("qWait", self._action())
        self.assertEqual(slots.confirm("Steve", pending.token).status, "no_pending")
        # The owner's slot still stands.
        self.assertTrue(slots.confirm("qWait", pending.token).confirmed)

    def test_expiry_is_checked_at_confirm_time(self) -> None:
        slots, now = self._slots(start=100.0)
        pending = slots.request("qWait", self._action())
        now[0] = 100.0 + 60.0  # exactly at the boundary: expired
        result = slots.confirm("qWait", pending.token)
        self.assertEqual(result.status, "expired")
        self.assertEqual(slots.confirm("qWait", pending.token).status, "no_pending")

    def test_token_mismatch_keeps_the_slot(self) -> None:
        slots, _now = self._slots()
        pending = slots.request("qWait", self._action())
        self.assertEqual(slots.confirm("qWait", "wrong").status, "token_mismatch")
        self.assertTrue(slots.confirm("qWait", pending.token).confirmed)

    def test_newest_request_wins(self) -> None:
        slots, _now = self._slots()
        first = slots.request("qWait", self._action())
        second = slots.request("qWait", self._action())
        self.assertEqual(slots.confirm("qWait", first.token).status, "token_mismatch")
        self.assertTrue(slots.confirm("qWait", second.token).confirmed)


class AddressingTests(unittest.TestCase):
    def _windows(self, start: float = 0.0) -> tuple[AddressingWindows, list[float]]:
        now = [start]
        return AddressingWindows(clock=lambda: now[0]), now

    def test_mention_addresses_with_cjk_adjacency(self) -> None:
        self.assertTrue(mentions_name("你好MineBot我们走", "MineBot"))
        self.assertTrue(mentions_name("minebot, follow me", "MineBot"))
        self.assertFalse(mentions_name("MineBotFan says hi", "MineBot"))
        self.assertFalse(mentions_name("no bots here", "MineBot"))

    def test_window_admits_three_exchanges_within_ninety_seconds(self) -> None:
        windows, now = self._windows()
        windows.record_bot_reply("Steve")
        for i in range(3):
            decision = windows.decide(text=f"line {i}", sender="Steve", bot_name="MineBot")
            self.assertTrue(decision.addressed, i)
            self.assertEqual(decision.reason, "conversation_window")
        self.assertEqual(
            windows.decide(text="line 4", sender="Steve", bot_name="MineBot").reason,
            "ambient",
        )

    def test_window_expires_and_ambient_is_default(self) -> None:
        windows, now = self._windows()
        windows.record_bot_reply("Steve")
        now[0] = 91.0
        self.assertEqual(
            windows.decide(text="hi", sender="Steve", bot_name="MineBot").reason,
            "ambient",
        )
        self.assertEqual(
            windows.decide(text="hi", sender="Nobody", bot_name="MineBot").reason,
            "ambient",
        )

    def test_bot_reply_reopens_the_window(self) -> None:
        windows, now = self._windows()
        windows.record_bot_reply("Steve")
        for i in range(3):
            windows.decide(text=f"l{i}", sender="Steve", bot_name="MineBot")
        windows.record_bot_reply("Steve")
        self.assertTrue(
            windows.decide(text="again", sender="Steve", bot_name="MineBot").addressed
        )


class SessionVocabularyTests(unittest.TestCase):
    def test_every_command_kind_has_an_admission_row(self) -> None:
        # _admit does a direct dict lookup; a kind without a row would crash
        # the admission path the moment principals are configured.
        for kind in SessionCommandKind:
            self.assertIn(kind, _ADMISSION_CAPABILITY, kind)

    def test_authority_kind_capabilities_match_the_design_table(self) -> None:
        self.assertIs(
            _ADMISSION_CAPABILITY[SessionCommandKind.TRUST],
            AdmissionCapability.CONTROL_PROCESS,
        )
        self.assertIs(
            _ADMISSION_CAPABILITY[SessionCommandKind.STANCE],
            AdmissionCapability.CONTROL_PROCESS,
        )
        self.assertIs(
            _ADMISSION_CAPABILITY[SessionCommandKind.ABANDON],
            AdmissionCapability.CONTROL_WORK,
        )
        self.assertIs(
            _ADMISSION_CAPABILITY[SessionCommandKind.CONFIRM],
            AdmissionCapability.CONVERSE,
        )

    def _session(self, registry: PrincipalRegistry | None) -> AgentSession:
        return AgentSession(
            parts_factory=lambda goal: (_ for _ in ()).throw(
                AssertionError("parts must not be built by submit")
            ),
            work_queue=MemoryWorkIntentQueue(),
            principals=registry,
        )

    def test_unwired_authority_command_degrades_to_conversation(self) -> None:
        # Owner-admitted TRUST reaches the degrade, not the scheduler: the
        # intent lands as MESSAGE with the typed dialogue_unwired reason.
        session = self._session(_enforcing())
        intent = session.submit(
            SessionCommand(
                kind=SessionCommandKind.TRUST,
                text="/trust Steve friend",
                sender="qWait",
            )
        )
        self.assertIs(intent.kind, WorkIntentKind.MESSAGE)
        self.assertEqual(intent.payload["reason"], "dialogue_unwired:trust")
        self.assertEqual(intent.payload["text"], "/trust Steve friend")

    def test_stranger_authority_command_is_denied_before_degrade(self) -> None:
        session = self._session(_enforcing())
        intent = session.submit(
            SessionCommand(
                kind=SessionCommandKind.TRUST,
                text="/trust Nobody owner",
                sender="Nobody",
            )
        )
        self.assertIs(intent.kind, WorkIntentKind.MESSAGE)
        self.assertTrue(
            str(intent.payload["reason"]).startswith("admission_denied:trust:")
        )

    def test_open_default_treats_authority_kind_as_conversation(self) -> None:
        # No principal layer at all (today's default): still no scheduler
        # error, still conversation — inert by construction.
        session = self._session(None)
        intent = session.submit(
            SessionCommand(
                kind=SessionCommandKind.CONFIRM, text="/confirm abc", sender="Steve"
            )
        )
        self.assertIs(intent.kind, WorkIntentKind.MESSAGE)
        self.assertEqual(intent.payload["reason"], "dialogue_unwired:confirm")


if __name__ == "__main__":
    unittest.main()
