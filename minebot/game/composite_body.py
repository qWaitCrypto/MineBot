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

    def spawn(self, pos=None, **kwargs) -> Result:
        return self.java.spawn(pos, **kwargs)

    def despawn(self) -> Result:
        return self.java.despawn()

    def get_state(self):
        # Keep the complete legacy state until Java has the full state ledger.
        return self.scarpet.get_state()

    def perceive(self, scope: str, params: dict[str, object]) -> PerceptionResult:
        provider = self.java if scope in self.JAVA_PERCEPTIONS else self.scarpet
        return provider.perceive(scope, params)

    def execute(self, action: Action) -> Result:
        provider = self.java if action.name in self.JAVA_ACTIONS else self.scarpet
        result = provider.execute(action)
        if action.name in self.JAVA_TERMINAL_ACTIONS and result.ok and result.accepted:
            self._terminal_providers[action.id] = provider
        return result

    def await_action_terminal(
        self,
        action_id: str,
        timeout_s: float = 15.0,
        poll_interval_s: float = 0.10,
        terminal_events: set[str] | None = None,
        intermediate_events: set[str] | None = None,
    ) -> Event:
        provider = self._terminal_providers.pop(action_id, self.scarpet)
        return provider.await_action_terminal(
            action_id,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            terminal_events=terminal_events,
            intermediate_events=intermediate_events,
        )

    def poll_events(self) -> list[Event]:
        # Legacy transactions still depend on Scarpet event names and epochs.
        return self.scarpet.poll_events()

    def event_head(self, proposed_epoch: str) -> dict[str, object]:
        # Event sequence/epoch still belong to the legacy stream until survival
        # events migrate. Owner settlement must nevertheless observe both
        # independent migration-time owner ledgers.
        scarpet = dict(self.scarpet.event_head(proposed_epoch))
        java = dict(self.java.event_head(proposed_epoch))
        owners = [
            str(owner)
            for owner in (java.get("owner"), scarpet.get("owner"))
            if owner is not None
        ]
        scarpet["owner"] = None if not owners else "+".join(owners)
        scarpet["pending_action_count"] = int(
            scarpet.get("pending_action_count") or 0
        ) + int(java.get("pending_action_count") or 0)
        scarpet["java_event_seq"] = int(java.get("event_seq") or 0)
        scarpet["java_epoch"] = java.get("epoch")
        return scarpet

    def ignite_block(self, pos, **kwargs) -> Event:
        return self.java.ignite_block(pos, **kwargs)

    def sow_crop(self, pos, **kwargs) -> Event:
        return self.java.sow_crop(pos, **kwargs)

    def interrupt(self, reason: str | None = None) -> Result:
        # Composite temporarily has two independent server-side owner ledgers.
        # Cancellation therefore fans out and succeeds only when both owners
        # accept it; this is cleanup, not runtime behavior fallback.
        java = self.java.interrupt(reason)
        scarpet = self.scarpet.interrupt(reason)
        ok = java.ok and scarpet.ok
        accepted = java.accepted and scarpet.accepted
        return Result(
            id=None,
            bot=self.bot_name,
            type="result",
            ok=ok,
            accepted=accepted,
            complete=java.complete and scarpet.complete,
            data={
                "action": "interrupt",
                "java": dict(java.data),
                "scarpet": dict(scarpet.data),
            },
            error=None if ok and accepted else (java.error or scarpet.error or "interrupt_rejected"),
        )

    def __getattr__(self, name: str):
        # Migration-only extensions such as chat and telemetry remain legacy-
        # owned until their contract equivalents land.
        return getattr(self.scarpet, name)


__all__ = ["CompositeBody"]
