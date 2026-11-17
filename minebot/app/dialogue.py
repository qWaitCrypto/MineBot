"""G1 in-game dialogue skeleton: authority grammar, confirmation, addressing.

Design: ``docs/design-docs/in-game-dialogue.md`` (decision
``in-game-dialogue-20260727``). Construction only, per the framework's
construction-vs-activation rule: pure grammar functions plus two tiny
injectable-clock stores. Nothing here wires into ingress — activation (P2)
adds the single call site behind :func:`authority_grammar_enabled`, whose
open-registry fall-through keeps today's default behavior untouched.

The grammar recognizes only authority-bearing acts. Intent interpretation
stays model-owned conversation (C4); this module must never grow into a
keyword-AI.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from minebot.app.principals import PrincipalRegistry
from minebot.app.session import SessionCommand, SessionCommandKind

CONFIRMATION_TTL_S = 60.0
ADDRESSING_WINDOW_S = 90.0
ADDRESSING_MAX_EXCHANGES = 3

_TRUST_TIERS = frozenset({"owner", "friend", "stranger"})
_STANCE_KINDS = frozenset({"guard", "follow", "standby", "none"})
_MAX_PLAYER_NAME = 32


@dataclass(frozen=True)
class AuthorityCommand:
    """One parsed authority act: the target kind plus structured arguments."""

    kind: SessionCommandKind
    args: Mapping[str, str]
    raw: str

    def session_command(self, *, sender: str = "") -> SessionCommand:
        return SessionCommand(
            kind=self.kind,
            text=self.raw,
            reason="dialogue_authority",
            sender=sender,
        )


def authority_grammar_enabled(registry: PrincipalRegistry | None) -> bool:
    """Open-registry fall-through (design §3 safety default).

    Without configured owners everyone is owner-equivalent, so ``/trust``
    would let anyone promote themselves; the authority grammar therefore
    yields commands only under an enforcing registry. On the inert default
    these lines remain ordinary conversation.
    """

    return registry is not None and registry.enforcing


def parse_authority_command(line: str) -> AuthorityCommand | None:
    """Parse one chat line into an authority command, or ``None``.

    Pure and deterministic; consults no state and no model. Marked formality
    (design P-2): only ``/``-prefixed text can be a command, and a malformed
    authority line is ``None`` — falling through to conversation — never a
    guessed command.
    """

    text = str(line or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split()
    word = parts[0].casefold()
    if word == "/trust":
        if len(parts) != 3:
            return None
        player, tier = parts[1], parts[2].casefold()
        if tier not in _TRUST_TIERS or not _plausible_player_name(player):
            return None
        return AuthorityCommand(
            SessionCommandKind.TRUST, {"player": player, "tier": tier}, text
        )
    if word == "/stance":
        if len(parts) < 2:
            return None
        stance = parts[1].casefold()
        if stance not in _STANCE_KINDS:
            return None
        params: dict[str, str] = {}
        for pair in parts[2:]:
            key, sep, value = pair.partition("=")
            if not sep or not key or not value:
                return None
            params[key.casefold()] = value
        if stance == "none" and params:
            return None
        return AuthorityCommand(
            SessionCommandKind.STANCE, {"stance": stance, **params}, text
        )
    if word == "/abandon":
        reason = " ".join(parts[1:])
        return AuthorityCommand(
            SessionCommandKind.ABANDON,
            {"reason": reason} if reason else {},
            text,
        )
    if word == "/confirm":
        if len(parts) != 2:
            return None
        return AuthorityCommand(
            SessionCommandKind.CONFIRM, {"token": parts[1]}, text
        )
    return None


def _plausible_player_name(name: str) -> bool:
    # Deliberately loose: CN offline servers allow non-ASCII names. The name
    # only needs to not be another command token and to fit a name-ish length.
    return bool(name) and len(name) <= _MAX_PLAYER_NAME and not name.startswith("/")


@dataclass(frozen=True)
class PendingConfirmation:
    """The exact action awaiting its same-principal confirmation (design §4)."""

    principal_id: str
    action: SessionCommand
    token: str
    created_at_s: float
    expires_at_s: float


@dataclass(frozen=True)
class ConfirmationResult:
    status: str  # "confirmed" | "no_pending" | "token_mismatch" | "expired"
    pending: PendingConfirmation | None = None

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"


class ConfirmationSlots:
    """One slot per principal, newest wins; expiry observed only when tried.

    There is deliberately no timer and no watcher (C1/C2): an expired slot
    simply fails at the next ``confirm`` and is dropped then. The caller
    re-checks the stored action's admission capability at confirm time —
    a trust demotion between the two steps voids the confirmation.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        ttl_s: float = CONFIRMATION_TTL_S,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("confirmation ttl_s must be > 0")
        self._clock = clock
        self._ttl_s = float(ttl_s)
        self._token_factory = token_factory or (lambda: secrets.token_hex(3))
        self._slots: dict[str, PendingConfirmation] = {}

    def request(self, principal_id: str, action: SessionCommand) -> PendingConfirmation:
        now = self._clock()
        pending = PendingConfirmation(
            principal_id=str(principal_id),
            action=action,
            token=self._token_factory(),
            created_at_s=now,
            expires_at_s=now + self._ttl_s,
        )
        self._slots[pending.principal_id] = pending
        return pending

    def confirm(self, principal_id: str, token: str) -> ConfirmationResult:
        pending = self._slots.get(str(principal_id))
        if pending is None:
            # Wrong principal lands here too: slots are same-principal only.
            return ConfirmationResult("no_pending")
        if self._clock() >= pending.expires_at_s:
            del self._slots[pending.principal_id]
            return ConfirmationResult("expired", pending)
        if token != pending.token:
            # A typo must not void the window; the slot stands until expiry.
            return ConfirmationResult("token_mismatch", None)
        del self._slots[pending.principal_id]
        return ConfirmationResult("confirmed", pending)

    def pending_for(self, principal_id: str) -> PendingConfirmation | None:
        return self._slots.get(str(principal_id))


@dataclass(frozen=True)
class AddressingDecision:
    addressed: bool
    reason: str  # "mention" | "conversation_window" | "ambient"


@dataclass
class _Window:
    opened_at_s: float
    exchanges_used: int = 0


class AddressingWindows:
    """Deterministic public-screen addressing (design §5, rules 2-3).

    Command lines are handled before this policy is consulted (rule 1), so it
    only decides mention-or-window. A window opens when the bot replies to a
    sender and admits ``max_exchanges`` lines within ``window_s`` seconds;
    everything else is ambient — observed context, never a turn.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        window_s: float = ADDRESSING_WINDOW_S,
        max_exchanges: int = ADDRESSING_MAX_EXCHANGES,
    ) -> None:
        self._clock = clock
        self._window_s = float(window_s)
        self._max_exchanges = int(max_exchanges)
        self._windows: dict[str, _Window] = {}

    def record_bot_reply(self, sender: str) -> None:
        self._windows[str(sender)] = _Window(opened_at_s=self._clock())

    def decide(self, *, text: str, sender: str, bot_name: str) -> AddressingDecision:
        if mentions_name(text, bot_name):
            return AddressingDecision(True, "mention")
        window = self._windows.get(str(sender))
        if window is not None:
            expired = self._clock() - window.opened_at_s > self._window_s
            exhausted = window.exchanges_used >= self._max_exchanges
            if not expired and not exhausted:
                window.exchanges_used += 1
                return AddressingDecision(True, "conversation_window")
            del self._windows[str(sender)]
        return AddressingDecision(False, "ambient")


def mentions_name(text: str, name: str) -> bool:
    """Case-insensitive mention check that tolerates CJK adjacency.

    ``\\b`` treats a CJK character as a word character, so ``你好MineBot`` would
    never match; plain substring would match ``MineBotFan``. Instead: the
    neighbors of the match may be anything except ASCII word characters.
    """

    haystack = str(text or "").casefold()
    needle = str(name or "").casefold()
    if not needle:
        return False
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return False
        before = haystack[index - 1] if index > 0 else ""
        after_index = index + len(needle)
        after = haystack[after_index] if after_index < len(haystack) else ""
        if not _ascii_word_char(before) and not _ascii_word_char(after):
            return True
        start = index + 1


def _ascii_word_char(ch: str) -> bool:
    return bool(ch) and (ch.isascii() and (ch.isalnum() or ch == "_"))


__all__ = [
    "ADDRESSING_MAX_EXCHANGES",
    "ADDRESSING_WINDOW_S",
    "AddressingDecision",
    "AddressingWindows",
    "AuthorityCommand",
    "CONFIRMATION_TTL_S",
    "ConfirmationResult",
    "ConfirmationSlots",
    "PendingConfirmation",
    "authority_grammar_enabled",
    "mentions_name",
    "parse_authority_command",
]
