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

from dataclasses import dataclass, field
import json

from minebot.brain.modes import RuntimeProfile
from minebot.contract import BodyState

# Re-inject the goal every N model turns so it never scrolls out of the window.
DEFAULT_GOAL_REINJECT_EVERY = 5


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
    _last_profile: RuntimeProfile | None = field(default=None, repr=False)
    _resume_facts: dict[str, object] | None = field(default=None, repr=False)
    _task_artifact: dict[str, object] | None = field(default=None, repr=False)
    _conversation_summary: dict[str, object] | None = field(default=None, repr=False)
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

    def observe_state(self, state: BodyState) -> None:
        """Record the latest authoritative Body state for per-turn injection."""
        self._last_state = state

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
        parts: list[str] = []
        if self._available_skills_rendered:
            parts.append(self._available_skills_rendered)
        if self._active_skills:
            parts.append("ACTIVE_SKILLS complete=true")
            for skill in sorted(
                self._active_skills,
                key=lambda item: (str(item.get("name") or ""), str(item.get("version") or "")),
            ):
                name = str(skill.get("name") or "")
                version = str(skill.get("version") or "")
                instructions = str(skill.get("instructions") or "").strip()
                parts.extend(
                    (
                        f"BEGIN_SKILL name={name} version={version}",
                        instructions,
                        f"END_SKILL name={name}",
                    )
                )
        return "\n".join(parts)

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
        if len(pending) == 1 and pending[0][0] == "user":
            role, text, sender = pending[0]
            return self._user_input_line(text, sender=sender, prefixed=False), 1
        lines = [
            f"HARNESS_FACT: {text}"
            if role == "system"
            else self._user_input_line(text, sender=sender, prefixed=True)
            for role, text, sender in pending
        ]
        return "\n".join(lines), len(pending)

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

    def turn_preamble(
        self,
        *,
        include_goal: bool = True,
        include_session_messages: bool = True,
    ) -> str:
        """The text prepended to a model turn: current goal + live session facts.

        The goal line is always available in Phase 1. Cadence remains available
        as metadata for future compression policy, but SDK dynamic-instructions
        callback cadence must never hide the goal from the model.
        """
        parts: list[str] = []
        if include_goal and self.goal_text.strip():
            parts.append(f"GOAL: {self.goal_text}")
        parts.append(f"SESSION: turn={self._turn} language={self.language}")
        if include_session_messages and self._session_messages:
            parts.append(self._session_window_line())
        if self._task_artifact is not None:
            parts.append(
                "TASK_ARTIFACT: "
                + json.dumps(self._task_artifact, ensure_ascii=False, sort_keys=True)
            )
            task = self._task_artifact.get("task")
            if isinstance(task, dict) and task.get("status") == "running":
                parts.append(
                    "TASK_RUNTIME_CONTRACT: A durable task spans finite SDK runs only "
                    "through checkpoint_task. Before final output, record exactly one "
                    "explicit disposition: continue with a structured continuation when "
                    "the unfinished goal remains actionable; wait_event only for a named "
                    "material wake condition; yield only for a grounded bounded blocker; "
                    "complete only with authoritative evidence."
                )
        if self._conversation_summary is not None and self._conversation_summary.get("compacted_turns", 0):
            parts.append(
                "CONVERSATION_SUMMARY: "
                + json.dumps(self._conversation_summary, ensure_ascii=False, sort_keys=True)
            )
        if self._last_state is not None:
            parts.append(self._state_line(self._last_state))
        if self._last_profile is not None:
            parts.append(self._profile_line(self._last_profile))
        if self._resume_facts is not None:
            parts.append(self._resume_line(self._resume_facts))
            self._resume_facts = None
        return "\n".join(parts)

    @staticmethod
    def _state_line(state: BodyState) -> str:
        pos = ", ".join(f"{value:.1f}" for value in state.pos)
        return (
            f"STATE: pos=({pos}) health={state.health:.1f} food={state.food} "
            f"dim={state.dimension or 'overworld'}"
        )

    @staticmethod
    def _profile_line(profile: RuntimeProfile) -> str:
        focus = ",".join(profile.tool_focus)
        tags = ",".join(profile.policy_tags)
        return (
            f"PROFILE: relationship={profile.relationship} situational={profile.situational} "
            f"lifecycle={profile.lifecycle} focus={focus} model={profile.model_route} "
            f"effort={profile.effort} policy={tags} frame={profile.context_frame}"
        )

    @staticmethod
    def _resume_line(facts: dict[str, object]) -> str:
        reason = facts.get("reason") or "resume"
        goal = facts.get("goal") or ""
        progress = facts.get("last_progress") or {}
        return f"RESUME: reason={reason} goal={goal} last_progress={progress}"

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

    def _session_window_line(self) -> str:
        chunks = [f"{role}: {text}" for role, text in self._session_messages]
        return "SESSION_MESSAGES: " + " | ".join(chunks)


__all__ = ["AgentContext", "DEFAULT_GOAL_REINJECT_EVERY"]
