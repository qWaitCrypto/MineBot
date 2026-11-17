"""AgentContext — owned seam ③: goal single-ownership + state injection.

The SDK's Sessions manage conversation history; they do not own the goal, inject
per-turn Body state, or re-inject the goal as context scrolls. Per
``agent-loop.md`` §5 the goal must have exactly one textual owner, re-injected on
a fixed cadence. ``AgentContext`` is that owner.

This is the **thin** version (``agent-layer-architecture.md`` §10): the slot
physically exists and is unit-testable. Sliding-window/summary depth and the
exact re-injection cadence are deferred to the first long-running e2e (§12); the
single-ownership contract is what must exist now so the stance-FSM, Skills, and
memory/RAG slots are additive later (§8).

Framework-agnostic: imports only ``minebot.contract``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import json

from minebot.brain.context_compiler import (
    CompiledContext,
    ContextBudget,
    ContextFacts,
    compile_context,
)
from minebot.brain.modes import RuntimeProfile
from minebot.contract import BodyState

# Re-inject the goal every N model turns so it never scrolls out of the window.
DEFAULT_GOAL_REINJECT_EVERY = 5

# Marks a harness fact inside turn input, which the model receives as user-role text.
HARNESS_FACT_PREFIX = "HARNESS_FACT: "

# First line of a mounted Skill's message, and the one-line unmount message
# (agent-skills-v2-20261105.md §2.1). Each is one user-role conversation item.
SKILL_MOUNTED_PREFIX = "SKILL_MOUNTED "
SKILL_UNMOUNTED_PREFIX = "SKILL_UNMOUNTED "


def skill_mounted_message(name: str, item_id: str, body: str) -> str:
    return f"{SKILL_MOUNTED_PREFIX}name={name} for={item_id}\n{str(body).strip()}"


def skill_unmounted_message(name: str) -> str:
    return f"{SKILL_UNMOUNTED_PREFIX}name={name}: no longer in effect"


def is_skill_mount_text(text: object) -> bool:
    return isinstance(text, str) and text.startswith((SKILL_MOUNTED_PREFIX, SKILL_UNMOUNTED_PREFIX))


@dataclass
class AgentContext:
    """Single textual owner of the goal + per-turn state injection point.

    This is the shared feed for three future slots: the stance FSM swaps a
    "context profile" here, agent Skills inject methodology here, and memory/RAG
    retrieval feeds here. Designing it as the one context owner now is what makes
    all three additive.
    """

    system_prompt: str
    goal_text: str
    goal_reinject_every: int = DEFAULT_GOAL_REINJECT_EVERY
    language: str = "English"
    max_session_messages: int = 8
    _turn: int = 0
    _last_state: BodyState | None = field(default=None, repr=False)
    _nearby_entities: tuple[Mapping[str, object], ...] = field(default=(), repr=False)
    _goal_facts: dict[str, object] | None = field(default=None, repr=False)
    _last_profile: RuntimeProfile | None = field(default=None, repr=False)
    _resume_facts: dict[str, object] | None = field(default=None, repr=False)
    _task_artifact: dict[str, object] | None = field(default=None, repr=False)
    _conversation_summary: dict[str, object] | None = field(default=None, repr=False)
    _life: dict[str, object] | None = field(default=None, repr=False)
    _ladder: str | None = field(default=None, repr=False)
    _home: str | None = field(default=None, repr=False)
    _skill_catalog_revision: str | None = field(default=None, repr=False)
    _skill_descriptors: list[dict[str, object]] = field(default_factory=list, repr=False)
    _available_skills_rendered: str = field(default="", repr=False)
    _active_skills: list[dict[str, object]] = field(default_factory=list, repr=False)
    _session_messages: list[tuple[str, str]] = field(default_factory=list, repr=False)
    _pending_turn_messages: list[tuple[str, str, str | None]] = field(default_factory=list, repr=False)

    # -- goal ownership -------------------------------------------------------

    def set_goal(self, goal_text: str) -> None:
        """Replace the goal. Re-injection cadence restarts so the new goal is
        guaranteed to appear on the very next turn."""
        self.goal_text = goal_text
        self._turn = 0

    def observe_user_message(self, text: str, *, sender: str | None = None) -> None:
        """Record user-visible session text for the context window."""
        clean = self._clean_text(text)
        if not clean:
            return
        clean_sender = self._clean_sender(sender)
        visible = f"{clean_sender}: {clean}" if clean_sender else clean
        self._append_session_message("user", visible)
        self._append_pending_turn_message("user", clean, clean_sender)

    def observe_assistant_message(self, text: str) -> None:
        """Record assistant-visible speech for the context window."""
        self._append_session_message("assistant", text)

    def observe_system_message(self, text: str) -> None:
        """Record a harness fact that must remain visible across turns."""
        clean = self._append_session_message("system", text)
        if clean:
            self._append_pending_turn_message("system", clean, None)

    def queue_harness_fact(self, text: str) -> None:
        """Queue one per-turn harness fact without changing the live preamble.

        Automatic recall is turn-local input.  It must not become part of the
        stable instructions or the rolling session message window.
        """
        clean = " ".join(str(text).strip().split())
        if clean:
            self._append_pending_turn_message("system", clean, None)

    def queue_conversation_item(self, text: str) -> None:
        """Queue one message that enters the conversation as its own user-role item.

        Unlike a harness fact it is not folded into one line with the other
        pending input: its text (a mounted Skill body) is kept whole, and the
        compactor recognises it by its first line.
        """
        clean = str(text or "").strip()
        if clean:
            self._append_pending_turn_message("item", clean, None)

    def pending_item_texts(self) -> list[str]:
        """Texts of separate conversation items queued but not yet given to a turn."""
        return [text for role, text, _sender in self._pending_turn_messages if role == "item"]

    def observe_state(
        self, state: BodyState, *, nearby_entities: Sequence[Mapping[str, object]] = (),
    ) -> None:
        """Record the latest authoritative Body state for per-turn injection."""
        self._last_state = state
        self._nearby_entities = tuple(deepcopy(entity) for entity in nearby_entities)

    def observe_goal_facts(self, facts: dict[str, object] | None) -> None:
        """Record a read-only, authoritative projection of goal completion."""
        self._goal_facts = (
            None
            if facts is None
            else json.loads(json.dumps(facts, ensure_ascii=False, sort_keys=True))
        )

    def observe_profile(self, profile: RuntimeProfile) -> None:
        """Record the current stance profile for per-turn context framing."""
        self._last_profile = profile

    def observe_resume(self, facts: dict[str, object]) -> None:
        """Inject one resume frame after a situational interruption."""
        self._resume_facts = dict(facts)

    def observe_task(self, artifact: dict[str, object] | None) -> None:
        """Inject the current persisted task artifact without owning its state."""
        if artifact is None:
            self._task_artifact = None
            return
        self._task_artifact = json.loads(
            json.dumps(artifact, ensure_ascii=False, sort_keys=True)
        )

    def observe_life(self, summary: dict[str, object] | None) -> None:
        """Inject the Agent's own life profile summary for this world."""
        self._life = (
            None
            if summary is None
            else json.loads(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        )

    def observe_ladder(self, text: str | None) -> None:
        """The plan ladder and Body timeline, refreshed every model call."""
        self._ladder = text or None

    def observe_home(self, text: str | None) -> None:
        """Home, outposts, storage and the respawn point, refreshed every model call."""
        self._home = text or None

    def observe_conversation_summary(self, summary: dict[str, object] | None) -> None:
        self._conversation_summary = (
            None
            if summary is None
            else json.loads(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        )

    def observe_skills(
        self,
        *,
        catalog_revision: str,
        descriptors: list[dict[str, object]],
        available_rendered: str,
        active: list[dict[str, object]],
    ) -> None:
        """Replace the complete dynamic Skill snapshot atomically."""

        self._skill_catalog_revision = str(catalog_revision)
        self._skill_descriptors = json.loads(
            json.dumps(descriptors, ensure_ascii=False, sort_keys=True)
        )
        self._available_skills_rendered = str(available_rendered).strip()
        self._active_skills = json.loads(
            json.dumps(active, ensure_ascii=False, sort_keys=True)
        )

    def skill_preamble(self) -> str:
        """The Skill catalog for the instructions; never a Skill body.

        A Skill in effect is mounted on a ladder item and enters the
        conversation as its own message (agent-skills-v2-20261105.md §2.1).
        """
        return self._available_skills_rendered

    def session_messages(self) -> list[tuple[str, str]]:
        return list(self._session_messages)

    def budget_facts(self) -> dict[str, object]:
        task_chars = 0
        if self._task_artifact is not None:
            task_chars = len(
                json.dumps(self._task_artifact, ensure_ascii=False, sort_keys=True)
            )
        summary_chars = 0
        if self._conversation_summary is not None:
            summary_chars = len(
                json.dumps(self._conversation_summary, ensure_ascii=False, sort_keys=True)
            )
        return {
            "goal_chars": len(self.goal_text),
            "task_artifact_present": self._task_artifact is not None,
            "task_artifact_chars": task_chars,
            "conversation_summary_present": self._conversation_summary is not None,
            "conversation_summary_chars": summary_chars,
            "conversation_summary_complete": (
                None
                if self._conversation_summary is None
                else self._conversation_summary.get("complete") is True
            ),
            "conversation_archive_revision": (
                None
                if self._conversation_summary is None
                else self._conversation_summary.get("archive_revision")
            ),
            "conversation_archive_item_count": (
                0
                if self._conversation_summary is None
                else int(self._conversation_summary.get("archive_item_count") or 0)
            ),
            "conversation_live_item_count": (
                0
                if self._conversation_summary is None
                else int(self._conversation_summary.get("live_item_count") or 0)
            ),
            "conversation_live_item_chars": (
                0
                if self._conversation_summary is None
                else int(self._conversation_summary.get("live_item_chars") or 0)
            ),
            "conversation_archive_item_chars": (
                0
                if self._conversation_summary is None
                else int(self._conversation_summary.get("archive_item_chars") or 0)
            ),
            "conversation_total_closed_turns": (
                0
                if self._conversation_summary is None
                else int(self._conversation_summary.get("total_closed_turns") or 0)
            ),
            "conversation_compacted_turns": (
                0
                if self._conversation_summary is None
                else int(self._conversation_summary.get("compacted_turns") or 0)
            ),
            "session_message_count": len(self._session_messages),
            "pending_input_count": len(self._pending_turn_messages),
            "skill_catalog_revision": self._skill_catalog_revision,
            "skill_descriptor_count": len(self._skill_descriptors),
            "skill_descriptor_chars": len(self._available_skills_rendered),
            "active_skill_count": len(self._active_skills),
            "active_skill_chars": sum(
                len(str(item.get("instructions") or "")) for item in self._active_skills
            ),
        }

    def pending_turn_input(self, *, fallback: str) -> tuple[str, int]:
        """Return only facts not yet presented as a new SDK-session turn.

        The SDK Session owns detailed model/tool conversation history. This
        queue therefore carries only newly arrived user text and harness facts;
        live goal/state/profile framing remains in dynamic instructions.
        """
        pending = list(self._pending_turn_messages)
        if not pending:
            return fallback, 0
        return self._render_pending(pending), len(pending)

    def pending_turn_items(self, *, fallback: str) -> tuple[str | list[dict[str, str]], int]:
        """Like ``pending_turn_input``, but a queued conversation item stays its own item.

        Without queued items this is exactly ``pending_turn_input`` (one text).
        With them, the pending input becomes a list of user-role items in queue
        order: each run of ordinary messages is one item rendered as before,
        and each queued item is one item of its own.
        """
        pending = list(self._pending_turn_messages)
        if not any(role == "item" for role, _text, _sender in pending):
            return self.pending_turn_input(fallback=fallback)
        items: list[dict[str, str]] = []
        group: list[tuple[str, str, str | None]] = []
        for entry in pending:
            if entry[0] != "item":
                group.append(entry)
                continue
            if group:
                items.append({"role": "user", "content": self._render_pending(group)})
                group = []
            items.append({"role": "user", "content": entry[1]})
        if group:
            items.append({"role": "user", "content": self._render_pending(group)})
        return items, len(pending)

    def _render_pending(self, pending: list[tuple[str, str, str | None]]) -> str:
        if len(pending) == 1 and pending[0][0] == "user":
            _role, text, sender = pending[0]
            return self._user_input_line(text, sender=sender, prefixed=False)
        lines = [
            f"{HARNESS_FACT_PREFIX}{text}"
            if role == "system"
            else text
            if role == "item"
            else self._user_input_line(text, sender=sender, prefixed=True)
            for role, text, sender in pending
        ]
        return "\n".join(lines)

    def acknowledge_turn_input(self, count: int) -> None:
        """Consume the exact input snapshot accepted by a completed SDK run."""
        if count <= 0:
            return
        del self._pending_turn_messages[:count]

    def discard_pending_turn_input(self) -> None:
        """Drop obsolete pending input after authoritative goal termination."""
        self._pending_turn_messages.clear()

    # -- per-turn assembly ----------------------------------------------------

    def begin_turn(self) -> int:
        self._turn += 1
        return self._turn

    def should_reinject_goal(self) -> bool:
        """True on the first turn and every Nth turn thereafter."""
        return self._turn <= 1 or (self._turn - 1) % self.goal_reinject_every == 0

    def snapshot_facts(
        self,
        *,
        include_goal: bool = True,
        include_session_messages: bool = True,
        continuous_tools: bool = False,
    ) -> ContextFacts:
        """Immutable snapshot of everything observed, for the F2 compiler."""
        return ContextFacts(
            continuous_tools=continuous_tools,
            goal_text=self.goal_text,
            goal_facts=self._goal_facts,
            turn=self._turn,
            language=self.language,
            include_goal=include_goal,
            include_session_messages=include_session_messages,
            session_messages=tuple(self._session_messages),
            task_artifact=self._task_artifact,
            conversation_summary=self._conversation_summary,
            life=self._life,
            ladder=self._ladder,
            home=self._home,
            body_state=self._last_state,
            nearby_entities=self._nearby_entities,
            profile=self._last_profile,
            resume_facts=self._resume_facts,
        )

    def compile(
        self,
        profile: str = "full",
        *,
        include_goal: bool = True,
        include_session_messages: bool = True,
        budget: ContextBudget | None = None,
        continuous_tools: bool = False,
    ) -> CompiledContext:
        """Compile the turn preamble through the F2 section pipeline.

        The one-shot resume frame is consumed only when it was actually
        rendered, so a profile or budget that omits it cannot silently
        destroy the fact.
        """
        compiled = compile_context(
            profile,
            self.snapshot_facts(
                include_goal=include_goal,
                include_session_messages=include_session_messages,
                continuous_tools=continuous_tools,
            ),
            budget,
        )
        if "resume_facts" in compiled.sections:
            self._resume_facts = None
        return compiled

    def turn_preamble(
        self,
        *,
        include_goal: bool = True,
        include_session_messages: bool = True,
    ) -> str:
        """The text prepended to a model turn: current goal + live session facts.

        The goal line is always available in Phase 1. Cadence remains available
        as metadata for future compression policy, but SDK dynamic-instructions
        callback cadence must never hide the goal from the model. Delegates to
        the F2 compiler's ``full`` profile (golden-locked byte compatibility).
        """
        return self.compile(
            "full",
            include_goal=include_goal,
            include_session_messages=include_session_messages,
        ).text

    def _append_session_message(self, role: str, text: str) -> str:
        clean = " ".join(text.strip().split())
        if not clean:
            return ""
        self._session_messages.append((role, clean))
        if len(self._session_messages) > self.max_session_messages:
            del self._session_messages[: len(self._session_messages) - self.max_session_messages]
        return clean

    def _append_pending_turn_message(self, role: str, text: str, sender: str | None) -> None:
        self._pending_turn_messages.append((role, text, sender))

    @staticmethod
    def _clean_text(text: str) -> str:
        return " ".join(text.strip().split())

    @staticmethod
    def _clean_sender(sender: str | None) -> str | None:
        clean = " ".join(str(sender or "").strip().split())[:64]
        return clean or None

    @staticmethod
    def _user_input_line(text: str, *, sender: str | None, prefixed: bool) -> str:
        if sender:
            payload = json.dumps(
                {"sender": sender, "message": text},
                ensure_ascii=False,
                sort_keys=True,
            )
            return f"MINECRAFT_CHAT: {payload}"
        return f"USER_MESSAGE: {text}" if prefixed else text

__all__ = [
    "AgentContext",
    "DEFAULT_GOAL_REINJECT_EVERY",
    "HARNESS_FACT_PREFIX",
    "SKILL_MOUNTED_PREFIX",
    "SKILL_UNMOUNTED_PREFIX",
    "is_skill_mount_text",
    "skill_mounted_message",
    "skill_unmounted_message",
]
