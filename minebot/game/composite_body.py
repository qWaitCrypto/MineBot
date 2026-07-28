"""Explicit migration Body combining Java objectives with legacy primitives."""

from __future__ import annotations

from minebot.contract import Action, Body, Event, PerceptionResult, Result


class CompositeBody:
    """Route migrated whole objectives to Java and everything else to legacy.

    This is a startup-selected migration posture, not a runtime fallback.  The
    routing table is fixed for the life of the Body and a Java failure is
    returned unchanged; it is never retried through Scarpet.
    """

    JAVA_ACTIONS = frozenset({
        "navigate",
        "collectBlock",
        "ascend",
        "engageEntity",
        "followEntity",
        "containerTransfer",
        "craftItem",
        "furnaceTransfer",
        "handoffItem",
        "igniteBlock",
        "jump",
        "mineBlock",
        "placeBlock",
        "dropItem",
        "lookAt",
        "moveItem",
        "selectItem",
        "stop",
        "sowCrop",
        "useItem",
    })
    JAVA_TERMINAL_ACTIONS = frozenset({
        "containerTransfer",
        "engageEntity",
        "followEntity",
        "craftItem",
        "furnaceTransfer",
        "handoffItem",
        "igniteBlock",
        "jump",
        "mineBlock",
        "placeBlock",
        "dropItem",
        "lookAt",
        "moveItem",
        "selectItem",
        "stop",
        "sowCrop",
        "useItem",
    })
    JAVA_PERCEPTIONS = frozenset({
        "findBlocks",
        "inventory",
        "container",
        "blockAt",
        "blockCells",
        "surfaceColumns",
        "nearbyBlocks",
        "debugBlocks",
        "nearbyEntities",
        "nearbyHostiles",
        "recipeData",
    })

    def __init__(self, scarpet: Body, java: Body) -> None:
        if scarpet.bot_name != java.bot_name:
            raise ValueError("composite bodies must control the same bot")
        self.bot_name = scarpet.bot_name
        self.scarpet = scarpet
        self.java = java
        self._terminal_providers: dict[str, Body] = {}

    @property
    def preferred_inventory_page_size(self) -> int:
        return int(getattr(self.java, "preferred_inventory_page_size", 12))

    def spawn(self, pos=None, **kwargs) -> Result:
        return self.java.spawn(pos, **kwargs)

    def despawn(self) -> Result:
        return self.java.despawn()

    def get_state(self):
        return self.java.get_state()

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        return self.java.perceive(scope, params)

    def execute(self, action: Action) -> Result:
        result = self.java.execute(action)
        if action.name in self.JAVA_TERMINAL_ACTIONS and result.ok and result.accepted:
            self._terminal_providers[action.id] = self.java
        return result

    def await_action_terminal(
        self,
        action_id: str,
        timeout_s: float = 15.0,
        poll_interval_s: float = 0.10,
        terminal_events: set[str] | None = None,
        intermediate_events: set[str] | None = None,
    ) -> Event:
        provider = self._terminal_providers.pop(action_id, self.java)
        return provider.await_action_terminal(
            action_id,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            terminal_events=terminal_events,
            intermediate_events=intermediate_events,
        )

    def poll_events(self) -> list[Event]:
        return self.java.poll_events()

    def event_head(self, proposed_epoch: str) -> dict[str, object]:
        return self.java.event_head(proposed_epoch)

    def ignite_block(self, pos, **kwargs) -> Event:
        return self.java.ignite_block(pos, **kwargs)

    def sow_crop(self, pos, **kwargs) -> Event:
        return self.java.sow_crop(pos, **kwargs)

    def interrupt(self, reason: str | None = None) -> Result:
        return self.java.interrupt(reason)

    def __getattr__(self, name: str):
        # Migration-only extensions such as chat and telemetry remain legacy-
        # owned until their contract equivalents land.
        return getattr(self.scarpet, name)


__all__ = ["CompositeBody"]
