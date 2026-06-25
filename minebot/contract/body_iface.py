"""Typing-only Body protocol shared by agent and Body transactions."""

from __future__ import annotations

from typing import Protocol

from .messages import Action, BodyState, Event, PerceptionResult, Result


class Body(Protocol):
    bot_name: str

    def spawn(
        self,
        pos: tuple[int, int, int] | None = None,
        *,
        yaw: float | None = None,
        pitch: float | None = None,
        dimension: str | None = None,
        gamemode: str | None = None,
        emit_respawned: bool = False,
    ) -> Result: ...
    def despawn(self) -> Result: ...
    def get_state(self) -> BodyState: ...
    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult: ...
    def execute(self, action: Action) -> Result: ...
    def await_action_terminal(self, action_id: str, timeout_s: float = 15.0) -> Event: ...
    def poll_events(self) -> list[Event]: ...
    def ignite_block(
        self,
        pos: tuple[int, int, int],
        *,
        item: str | None = None,
        allow_server_substitute: bool = False,
        timeout_s: float = 8.0,
    ) -> Event: ...
    def sow_crop(
        self,
        pos: tuple[int, int, int],
        *,
        crop_block: str,
        seed_item: str | None = None,
        allow_server_substitute: bool = False,
        timeout_s: float = 8.0,
    ) -> Event: ...
    def interrupt(self, reason: str | None = None) -> Result: ...
