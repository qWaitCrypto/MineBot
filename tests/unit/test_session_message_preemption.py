from __future__ import annotations

from types import SimpleNamespace

from minebot.app.session import AgentSession, SessionCommand
from minebot.brain.lifecycle import LifecycleState


class RecordingAuthority:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def invalidate_generation(self, reason: str) -> None:
        self.reasons.append(reason)

    def current_generation(self) -> int:
        return 1


def test_message_does_not_cancel_active_physical_work_but_cancel_does() -> None:
    authority = RecordingAuthority()
    parts = SimpleNamespace(
        lifecycle=SimpleNamespace(state=LifecycleState.ACTIVE),
        authority=authority,
        runtime=SimpleNamespace(request_execution_cancel=lambda _reason: None),
    )
    session = AgentSession(lambda _goal: None, parts=parts)
    session._work_in_flight = True

    message = session.submit(SessionCommand.message("what is around me?"))
    assert message.kind.value == "message"
    assert authority.reasons == []

    session.submit(SessionCommand.cancel("stop now"))
    assert authority.reasons == ["session_command:cancel"]
