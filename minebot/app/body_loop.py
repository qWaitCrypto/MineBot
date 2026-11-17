"""The per-Body reflex loop.

The first reflex slice grew around :class:`CallSupervisor`, whose lifetime was
one physical call.  ``BodyLoop`` is the owner for the lifetime of the Body:
the existing call supervision remains the work-state implementation while the
loop also has an explicit lifecycle and an idle frame source.  Keeping the
old class as a base preserves the narrow SDK seams used by existing callers;
there is still one instance and one Decision client for a Body.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections import deque
from datetime import datetime, timezone
from uuid import uuid4
from dataclasses import asdict, is_dataclass, replace
from typing import Any, Callable, Mapping
from uuid import uuid4
from minebot.contract import Action, Event, ToolResult

from minebot.app import reflex_question as rq
from minebot.app import direct_call_supervision as supervision_module
from minebot.app.direct_call_supervision import (
    CallSupervisor,
    FACT_FRAME_INTERVAL_S,
)
from minebot.app.life_profile import LifeProfileUnreadable, load_profile, life_intent, stance_cleared_at, stance_written_at
from minebot.app.execution_snapshot import TERMINAL_STATES
from minebot.app.home_view import waking_notice
from minebot.app.body_events import MOVEMENT_RESET_ON_DEATH, WALK_GAZE_RESET_ON_DEATH
from minebot.app.ownership_ledger import OwnershipLedger

# How long the lifecycle task waits before it restarts after an error, and the
# longest it waits when errors follow each other.
LIFECYCLE_RESTART_DELAY_S = 1.0
LIFECYCLE_RESTART_MAX_DELAY_S = 30.0
# How long Decision passes over every offered ladder item before the Agent
# hears of it: a scheduling parameter that filters out a single frame, not a
# judgement of the scene (s7-later-20261029.md §8 D3).
NONE_STARTED_AFTER_S = 60.0
# Out of the world: how often the client is read so its return is seen (§8.15).
WORLD_LOOK_INTERVAL_S = 5.0



# A hostile that appears this close while a protective action runs is asked about.
NEW_HOSTILE_RADIUS = 8.0
# A seen hostile walking in is asked about once, at this distance; a creeper
# farther out, since its blast reaches past a blow (project-review §9.16).
CLOSING_RADIUS = 8.0
CREEPER_CLOSING_RADIUS = 12.0


def _within_closing_radius(entity: Mapping[str, Any]) -> bool:
    dist = entity.get("dist")
    if not isinstance(dist, (int, float)):
        return False
    radius = CREEPER_CLOSING_RADIUS if rq.mob_kind(entity) in rq.CREEPER_KINDS else CLOSING_RADIUS
    return dist <= radius
# Arrows remembered with their shooter, and Decision actions kept to match them.
# How often a Body off the ground is read and asked about: a thrown Body
# lands within 1-2.5 s (end run-33).
FALL_FRAME_INTERVAL_S = 0.25
# Actions a player commits to, which end by themselves within a couple of
# seconds: no running frame asks to replace them. The pour was replaced by
# eating a golden apple 0.7 s into a 35-block fall and the Body died on
# landing (end run-35, 12:34:18). Frames resume with its result.
COMMITTED_ACTIONS = frozenset({"__reflex_cushion_fall"})
SHOT_LOG_KEPT = 512
REFLEX_SPANS_KEPT = 64
# A release read a frame after its action ended still belongs to it.
SHOT_SPAN_SLACK_S = 0.5

def running_item_rules(profile: Any, item_id: str | None) -> list[dict[str, Any]] | None:
    """The running item's own rules ([] for none), or None when it has none
    of its own and the profile's rules hold."""
    if not item_id or not isinstance(profile, Mapping):
        return None
    item = next((row for row in profile.get("projects") or []
                 if isinstance(row, Mapping) and row.get("id") == item_id), None)
    rules = item.get("rules") if isinstance(item, Mapping) else None
    return copy.deepcopy([rule for rule in rules if isinstance(rule, Mapping)]) if isinstance(rules, list) else None


class BodyLoop(CallSupervisor):
    """Own supervision for work calls and the Body's idle lifecycle."""

    def __init__(self, runtime: Any, **kwargs: Any) -> None:
        super().__init__(runtime, **kwargs)
        self.loop_state = "idle"
        # The Overworld daylight phase last seen; the Agent hears each sunset once.
        self._daylight: str | None = None
        self._dusk_count = 0
        self._dawn_count = 0
        self._lifecycle_task: asyncio.Task[Any] | None = None
        self._lifecycle_started = False
        self._active_monitor: tuple[dict[str, Any], Any, Mapping[str, Any], asyncio.Event, asyncio.Task[Any]] | None = None
        self._result_pending: dict[str, dict[str, Any]] = {}
        # Protection chosen in a result question runs in its caller's task;
        # stopping the loop must stop it like any protection it started.
        self._result_protections: set[asyncio.Task[str]] = set()
        self._terminal_questions: deque[tuple[asyncio.Future[str], tuple[Any, ...], dict[str, Any]]] = deque()
        self._loop_wakeup = asyncio.Event()
        self._idle_sequence = 0
        self._suspended: dict[str, Any] | None = None
        self._protection_task: asyncio.Task[Any] | None = None
        self._protective_redirect: tuple[str, tuple[str, dict[str, Any]]] | None = None
        self._protective_verdict: str | None = None
        self.recent_experiences: deque[str] = deque(maxlen=5)
        # The blocked-home fact last handed to the Agent; told again only when it changes.
        self._home_blocked_notice: str | None = None
        # The last idle life action that failed by itself, with the frame it
        # started from; while nothing changed it is not offered as it was.
        self._last_idle_failure: dict[str, Any] | None = None
        self._lifecycle_events: deque[Event] = deque()
        self._death_facts: dict[str, Any] | None = None
        # Dimension and position of the last death, until the notice on waking has named it.
        self._death_place: tuple[Any, Any] | None = None
        self._death_count = 0
        # When the Body last died, and when the stance Decision last read as no
        # longer holding was written: a stance written before either is not
        # said in frames (closed-loop-architecture-20261107.md §3 A/B).
        self._died_at: datetime | None = None
        # The stance (text, written at) Decision read as broken.
        self._stance_left: tuple[str, datetime] | None = None
        # Deaths, respawns and reconnects as they arrive, before the lifecycle task
        # takes them: a dimension change across one is not a portal walk.
        self._life_epoch = 0
        # Dimension, position and life epoch of the last living frame.
        self._last_place: tuple[str, list[float], int] | None = None
        # The client's last respawn message already handled: (seq, at_ms).
        self._spawn_said: tuple[Any, Any] | None = None
        self._native_lifecycle = False
        self._body_lifecycle_listener = None
        self._world_connected: bool | None = None
        self._disconnected_at: float | None = None
        self._world_look_task: asyncio.Task[Any] | None = None
        self._reaction_observations: dict[str, dict[str, Any]] = {}
        self._reaction_selection: dict[str, Any] | None = None
        self._reaction_actions: dict[str, dict[str, Any]] = {}
        self._reaction_event_loop: asyncio.AbstractEventLoop | None = None
        self._ambient_facts: dict[str, Any] = {}
        self._placed_beds: set[tuple[int, int, int]] = set()
        self._creeper_memory: dict[str, dict[str, Any]] = {}
        # Arrows loosed per target; kept across deaths (the shots were real).
        self._shot_record: dict[str, dict[str, Any]] = {}
        # Every arrow with who loosed it, for the Agent's SHOTS line: the
        # Agent saw only last_shot and never learned that Decision's own
        # protective shots spent the arrows (dragon-unit-tests §6.y.41).
        self._shot_log: list[dict[str, Any]] = []
        self._idle_airborne = False
        self._reflex_spans: list[dict[str, Any]] = []
        self._life_profile = load_profile()
        self._life_store = None
        self._life_version = None
        self._reflex_claim_holder = "threat"
        self._reflex_action_intent: str | None = None
        # The idle stretch in which Decision was offered ladder items and
        # started none: since when, the profile version, what was offered and
        # what it chose instead, and whether the Agent has heard of it.
        self._unpicked: dict[str, Any] | None = None

    def attach_life_store(self, store) -> None:
        """Read the Agent's own profile for this world instead of the seed file."""
        self._life_store = store
        self._life_version = None
        self.current_life_profile()

    def _check_ladder_low(self, facts: Mapping[str, Any]) -> None:
        """Wake the Agent once when no ladder item could start next (§7.5).

        Counted by the same four conditions the idle frame offers items by,
        without ``when``; an item still running does not count, so the Agent
        can prepare the next one while it runs.  Not while the Agent thinks;
        a new item that could start ends this low water.
        """
        profile = facts.get("life_profile") if isinstance(facts.get("life_profile"), Mapping) else {}
        startable = len(rq.ladder_options({**facts, "ladder_held": False}, profile))
        if startable or facts.get("ladder_held") is True:
            self._ladder_low_sent = False
            return
        if getattr(self, "_ladder_low_sent", False) or facts.get("agent_thinking") is True:
            return
        notify = getattr(self.runtime, "notify_reflex_fact", None)
        if not callable(notify):
            return
        self._ladder_low_sent = True
        states = facts.get("ladder_states") if isinstance(facts.get("ladder_states"), Mapping) else {}
        running = next((name for name, state in states.items() if state == "running"), None)
        self.runtime.trace.emit("ladder_low", running=running, items=len(profile.get("projects") or []),
                                same_as_last_run=list(facts.get("ladder_same_run") or ()))
        fact = {"activity_id": f"ladder-low-{uuid4().hex[:12]}", "kind": "ladder_low", "status": "ladder_low",
                "wake": True, "running": running,
                "note": "No item on your plan ladder can start next. Write the next walk item "
                        "(update_life_project with work, or delegate_work)."}
        same_again = list(facts.get("ladder_same_run") or ())
        if same_again:
            fact["same_as_last_run"] = same_again
            fact["note"] += (" Not offered now: " + ", ".join(same_again) + " completed last time with nothing"
                             " changed, and the Body is as it was then; it is offered again once the Body"
                             " changes or you change the item.")
        notify(fact)

    def _note_same_run(self, items: list[str], store) -> None:
        """Trace once per item end that the item is not offered as it was (§8.14)."""
        seen = getattr(self, "_same_run_traced", None)
        if seen is None:
            seen = self._same_run_traced = {}
        for item in items:
            rows = store.project_results(item, limit=1)
            ended = rows[-1].get("activity_id") if rows else None
            if seen.get(item) != ended:
                seen[item] = ended
                self.runtime.trace.emit("ladder_item_withheld", project=item, activity_id=ended,
                                        reason="completed_nothing_changed_body_unchanged")

    def _check_none_started(self, facts: Mapping[str, Any], options: Mapping[str, Any], choice: str,
                            *, now: float | None = None) -> None:
        """Wake the Agent once when ladder items were offered and none started (§8 D3).

        Read from Decision's own choices only: in every idle frame of the
        stretch it was offered the items and chose something else. The stretch
        ends when an item starts, when none is offered (low water counts that)
        or when the Agent changes its ladder. Not while the Agent thinks.
        """
        now = time.monotonic() if now is None else now
        offered = [key.removeprefix("project_") for key in options if key.startswith("project_")]
        stretch = self._unpicked
        if stretch is not None and stretch["version"] != self._life_version:
            stretch = self._unpicked = None
        if choice.startswith("project_") or not offered:
            self._unpicked = None
            return
        if choice == "unknown":
            return  # a timeout is no choice
        if stretch is None:
            stretch = self._unpicked = {"since": now, "version": self._life_version,
                                        "offered": {}, "chosen": {}, "sent": False}
        for item in offered:
            stretch["offered"][item] = stretch["offered"].get(item, 0) + 1
        stretch["chosen"][choice] = stretch["chosen"].get(choice, 0) + 1
        if choice == "call_agent":
            stretch["sent"] = True  # Decision called the Agent itself: this stretch's one wake
        if stretch["sent"] or now - stretch["since"] < NONE_STARTED_AFTER_S or facts.get("agent_thinking") is True:
            return
        notify = getattr(self.runtime, "notify_reflex_fact", None)
        if not callable(notify):
            return
        stretch["sent"] = True
        idle_s = round(now - stretch["since"])
        offered_ids, chosen = sorted(stretch["offered"]), dict(stretch["chosen"])
        self.runtime.trace.emit("ladder_low", reason="none_started", idle_s=idle_s,
                                offered=offered_ids, chosen_instead=chosen)
        notify({"activity_id": f"ladder-low-{uuid4().hex[:12]}", "kind": "ladder_low", "status": "ladder_low",
                "reason": "none_started", "wake": True, "idle_s": idle_s,
                "offered": offered_ids, "chosen_instead": chosen,
                "note": "For idle_s seconds Decision was offered these ladder items in every idle frame and "
                        "started none of them; chosen_instead is what it picked. It weighs each item by its "
                        "intent and when against what it sees."})

    def current_life_profile(self) -> dict[str, Any]:
        """The profile as of this frame: the Agent's edits apply on the next frame."""
        store = getattr(self, "_life_store", None)
        if store is not None:
            version = store.version()
            if version != self._life_version:
                self._life_version = version
                try:
                    self._life_profile = store.profile()
                except LifeProfileUnreadable as exc:
                    # Keep the last profile read; an unreadable file is never empty.
                    self.runtime.trace.emit("life_profile_unreadable", error=str(exc)[:500])
        return self._life_profile

    def world_disconnected(self) -> bool:
        """The client said it left the world and has not said it is back."""
        return self._world_connected is False

    def _queue_lifecycle_event(self, event: Event) -> None:
        if event.name in ("death", "respawned", "world_ready", "world_disconnected"):
            self._life_epoch += 1
        if event.name == "world_ready":
            self._world_connected = True
            if self._lifecycle_task is None or self._lifecycle_task.done():
                asyncio.create_task(self.start())
        elif event.name == "world_disconnected":
            self._world_connected = False
        self._lifecycle_events.append(event)
        self._loop_wakeup.set()
        if self._active_monitor is not None:
            self._active_monitor[3].set()

    async def _process_lifecycle_events(self, facts: Mapping[str, Any] | None = None) -> bool:
        """Deterministic edges, consumed only by the one Body lifecycle task."""
        if (not self._lifecycle_events and rq.dead(facts or {})
                and self.loop_state != "dead" and not (self._native_lifecycle and self._death_facts)):
            self._lifecycle_events.append(Event(seq=0, tick=0, bot="", name="death", data=dict(facts or {})))
        if self._lifecycle_task is not None and asyncio.current_task() is not self._lifecycle_task:
            self._loop_wakeup.set()
            return self.loop_state == "dead" or any(e.name == "death" for e in self._lifecycle_events)
        while self._lifecycle_events:
            event = self._lifecycle_events.popleft()
            if event.name == "world_disconnected":
                self._report_connection("disconnected", event)
                if self.current is not None:
                    self.current["cancel_requested"] = True
                    self.current["result_abandoned"] = True
                    await self.runtime.cancel_active_execution_with_facts("server_disconnected")
                await self.stop(keep_world_listener=True, reason="server_disconnected")
                self._look_for_world_soon()
                return True
            elif event.name == "world_ready":
                self._report_connection("ready", event)
                self._vitals = rq.VitalWindow()
                self._ambient_facts.clear()
                self._creeper_memory.clear()
                self._set_state("idle", "world_ready")
            elif event.name == "death":
                if self.loop_state == "dead":
                    continue
                self._set_state("dead", "death")
                self._death_count += 1
                self._died_at = datetime.now(timezone.utc)
                self._reaction_observations.clear()
                self._reaction_actions.clear()
                self._reaction_selection = None
                self._ambient_facts.clear()
                self._creeper_memory.clear()
                death = dict(event.data)
                death["vital_samples"] = copy.deepcopy(self._vitals._samples)
                if self._reset_walk_gaze_on_death():
                    death["walking_gaze"] = WALK_GAZE_RESET_ON_DEATH
                if death.get("movement_reset"):
                    death["movement"] = MOVEMENT_RESET_ON_DEATH
                self._death_facts = death
                self._death_place = self._place_of_death(death)
                record = getattr(self.runtime, "record_body_event_memory", None)
                if callable(record):
                    record(Event(seq=event.seq, tick=event.tick, bot=event.bot, name=event.name, data=death))
                self._result_pending.clear()
                self._suspended = None
                protection = self._protection_task
                self._protection_task = None
                if protection is not None and protection is not asyncio.current_task() and not protection.done():
                    protection.cancel()
                if self.current is not None:
                    self.current["cancel_requested"] = True
                    self.current["result_abandoned"] = True
                # The death facts do not depend on how the old action settles.
                # Tell the Agent first: settling a dead Body's action has
                # taken up to the lane's cancel timeout (35.5 s live, step7-f).
                self.runtime.trace.emit("reflex_death", facts=death)
                notify = getattr(self.runtime, "notify_body_lifecycle_event", None)
                if callable(notify):
                    notify(Event(seq=event.seq, tick=event.tick, bot=event.bot, name=event.name, data=death))
                settled = await self.runtime.cancel_active_execution_with_facts("death")
                if protection is not None and protection is not asyncio.current_task():
                    await asyncio.gather(protection, return_exceptions=True)
                self.runtime.trace.emit("reflex_death_settled", settlement=settled)
            elif event.name == "movement_ended":
                # The leg mode ended by itself (cannot move / no safe point): the Agent hears where and why.
                self.runtime.trace.emit("movement_ended", facts={key: event.data.get(key) for key in
                                                               ("reason", "position", "keep_moving")})
                notify = getattr(self.runtime, "notify_body_lifecycle_event", None)
                if callable(notify):
                    notify(event)
            elif event.name == "respawned":
                self._vitals = rq.VitalWindow()
                self._ambient_facts.clear()
                self._creeper_memory.clear()
                self._result_pending.clear()
                self._set_state("idle", "respawned")
                # The game's words on the bed or anchor reach the record before the notice reads it.
                self._note_spawn(event.data)
                woke = {**dict(event.data), **self._waking_notice(event.data), **self._stance_after_death()}
                self.runtime.trace.emit("reflex_respawned", facts=woke)
                notify = getattr(self.runtime, "notify_body_lifecycle_event", None)
                if callable(notify):
                    notify(Event(seq=event.seq, tick=event.tick, bot=event.bot, name=event.name, data=woke))
        return self.loop_state == "dead"

    def _reset_walk_gaze_on_death(self) -> bool:
        """The Agent's look-down walk setting ends with the life (user 2026-11-05:
        死亡之后刷新): back to level; the Agent turns it on again if it wants it.
        True when it was on."""
        setter = getattr(getattr(self.runtime, "body", None), "set_walk_look_down", None)
        if not callable(setter):
            return False
        try:
            return bool(setter(False, cause="reset_on_death"))
        except Exception as exc:
            self.runtime.trace.emit("walk_gaze_reset_failed", error=f"{type(exc).__name__}: {exc}"[:300])
            return False

    @staticmethod
    def _place_of_death(death: Mapping[str, Any]) -> tuple[Any, Any] | None:
        place = death.get("position", death.get("pos"))
        previous = death.get("last_living_state")
        if place is None and isinstance(previous, Mapping):
            place = previous.get("position", previous.get("pos"))
        return None if place is None else (death.get("dimension"), place)

    def _waking_notice(self, facts: Mapping[str, Any]) -> dict[str, str]:
        """Where the Body woke beside its home and its death place (vision-and-home-20261028.md §7.7)."""
        # Told once: a later respawn without a death seen in between names no death place.
        died, self._death_place = self._death_place, None
        store = getattr(self, "_life_store", None)
        if store is None:
            return {}
        try:
            return waking_notice(self.current_life_profile(), store.world.snapshot(), here=facts, died=died)
        except Exception as exc:  # the respawn itself still reaches the Agent
            self.runtime.trace.emit("waking_notice_failed", error=f"{type(exc).__name__}: {exc}"[:500])
            return {}

    def _stance_after_death(self) -> dict[str, str]:
        """A stance written before the death no longer says where the Body is;
        the Agent hears so on waking."""
        died = getattr(self, "_died_at", None)
        written = stance_written_at(self.current_life_profile())
        if written is None or died is None or not written[1] < died.replace(microsecond=0):
            return {}
        return {"stance": f"Your stance “{written[0]}” was written before this death; the reflex layer no longer "
                          "reads it. Write a new one with update_life when you have one."}

    def _add_item_rules(self, facts: dict[str, Any]) -> None:
        """While a ladder item with its own rules runs, only those enter the
        frames (ladder-item-consolidation-20261108 §3, step 2)."""
        manager = getattr(self.runtime, "reflex_manager", None)
        running = manager.item_running() if manager is not None and callable(
            getattr(manager, "item_running", None)) else None
        rules = running_item_rules(facts.get("life_profile"), running)
        if rules is not None:
            facts["item_rules"] = rules

    def _stance_fact(self, profile: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """The stance for the frames, with its age: the one a ladder item set
        by its first successful Body action, or the Agent's update_life stance
        when that was written later; unless a death, the
        Agent clearing its stance or Decision's own reading that it no longer
        holds came after it (ladder-item-consolidation-20261108 §3)."""
        written = stance_written_at(profile)
        chosen = None if written is None else {"text": written[0], "at": written[1]}
        manager = getattr(self.runtime, "reflex_manager", None)
        event = manager.stance_set() if manager is not None and callable(getattr(manager, "stance_set", None)) else None
        if isinstance(event, Mapping) and event.get("fact") and (chosen is None or event["at"] > chosen["at"]):
            chosen = {"text": event["fact"]["text"], "at": event["at"]}
        if chosen is None:
            return None
        text, at = chosen["text"], chosen["at"]
        # The stance time is a whole second: one written in the second of the
        # death or the clearing, after it, still holds, so only an earlier second ends it.
        for ended in (getattr(self, "_died_at", None), stance_cleared_at(profile)):
            if ended is not None and at < ended.replace(microsecond=0):
                return None
        if (text, at.replace(microsecond=0)) == getattr(self, "_stance_left", None):
            return None
        return {"text": text, "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "age_s": max(0.0, (datetime.now(timezone.utc) - at).total_seconds())}

    def _report_connection(self, state: str, event: Event) -> None:
        """A lost or regained world reaches the Agent and the panel like death does.

        Before, the loop only stopped itself: in the villa run the client quit
        at 01:30, no turn was woken and the panel showed 可控制 for 13 hours.
        The first ready of a session, and a second report of the same loss,
        are not news.
        """
        now = time.monotonic()
        if state == "ready":
            if self._disconnected_at is None:
                return
            data = {**dict(event.data), "disconnected_for_s": round(now - self._disconnected_at, 1)}
            self._disconnected_at = None
        else:
            if self._disconnected_at is not None:
                return
            self._disconnected_at = now
            last = self._idle_facts()
            data = {**dict(event.data), "last_known": {
                key: last[key] for key in ("position", "pos", "dimension", "health", "food") if key in last}}
        self.runtime.trace.emit("body_connection", state=state, facts=data)
        notify = getattr(self.runtime, "notify_body_lifecycle_event", None)
        if callable(notify):
            notify(Event(seq=event.seq, tick=event.tick, bot=event.bot, name=event.name, data=data))

    def _frame(self, execution: Mapping[str, Any] | None) -> dict[str, Any]:
        if not execution:
            return {}
        # Native lite progress omits inventory and perception. That is not an
        # empty inventory or an observation that all entities have disappeared.
        # Keep only Body observations across calls, never the previous call's
        # phase, navigation, deltas or progress. Explicit observations replace
        # every alias of that observation, including explicit empty containers.
        ambient_before = copy.deepcopy(self._ambient_facts)
        observed = dict(execution.get("facts") or {})
        groups = (
            ("inventory", "inventory_counts", "inventory_slots_used"),
            ("nearby_hostiles", "hostiles", "nearby_entities", "entities", "entities_near"),
        )
        for group in groups:
            if any(key in observed for key in group):
                for key in group:
                    self._ambient_facts.pop(key, None)
                self._ambient_facts.update({key: copy.deepcopy(observed[key])
                                            for key in group if key in observed})
        for key in ("selected_item", "selected_item_known", "inventory_complete",
                    "inventory_includes_selected", "creative", "dimension", "time"):
            if key in observed:
                self._ambient_facts[key] = copy.deepcopy(observed[key])
        combined = {**copy.deepcopy(self._ambient_facts), **observed}
        def remember_beds(value: Any) -> None:
            if isinstance(value, Mapping):
                receipt = value.get("world_edit_receipt")
                if isinstance(receipt, Mapping):
                    for edit in receipt.get("edits") or ():
                        if not isinstance(edit, Mapping) or not isinstance(edit.get("pos"), list):
                            continue
                        pos = edit["pos"]
                        if len(pos) != 3 or not all(type(item) is int for item in pos):
                            continue
                        before = str(edit.get("before") or "").removeprefix("minecraft:")
                        after = str(edit.get("after") or "").removeprefix("minecraft:")
                        cell = (pos[0], pos[1], pos[2])
                        if after == "bed" or after.endswith("_bed"):
                            self._placed_beds.add(cell)
                        elif before == "bed" or before.endswith("_bed"):
                            self._placed_beds.discard(cell)
                for key, child in value.items():
                    if key != "world_edit_receipt":
                        remember_beds(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    remember_beds(child)
        remember_beds(execution)
        if self._placed_beds:
            combined["placed_beds"] = [list(pos) for pos in sorted(self._placed_beds)]
        policy = getattr(self.runtime, "governance_policy", None)
        placements = getattr(policy, "all_bot_placements", None)
        if callable(placements):
            dimension = combined.get("dimension")
            # Only beds: every own block in every frame would bloat the facts,
            # and the bed options are the only reader.
            own = []
            for place_dimension, entries in placements().items():
                if dimension and place_dimension != dimension:
                    continue
                for pos, entry in entries.items():
                    block = str(entry.block_type).removeprefix("minecraft:")
                    if block == "bed" or block.endswith("_bed"):
                        own.append({"pos": list(pos), "block": entry.block_type,
                                    "dimension": place_dimension})
            combined["own_beds"] = own
        combined["creeper_memory"] = rq.update_creeper_memory(combined, self._creeper_memory)
        logged = len(self._shot_log)
        combined["shot_record"] = rq.update_shot_record(combined, self._shot_record, log=self._shot_log)
        for shot in self._shot_log[logged:]:
            shot["by"] = self._shooter_at(shot["released"])
        del self._shot_log[:-SHOT_LOG_KEPT]
        profile = self.current_life_profile()
        if profile:
            combined["life_profile"] = copy.deepcopy(profile)
            combined["life_intent"] = life_intent(profile)
            self._add_item_rules(combined)
        combined["agent_thinking"] = bool(getattr(self.runtime, "agent_thinking", False))
        manager = getattr(self.runtime, "reflex_manager", None)
        if manager is not None and callable(getattr(manager, "ladder_held", None)) and manager.ladder_held():
            combined["ladder_held"] = True
        stance = self._stance_fact(combined.get("life_profile"))
        if stance is not None:
            combined["agent_stance"] = stance
        store = getattr(self, "_life_store", None)
        projects = (combined.get("life_profile") or {}).get("projects") if isinstance(
            combined.get("life_profile"), Mapping) else None
        if store is not None and projects:
            # The Body's own record of each ladder item, read from the project log.
            states, same_again = {}, []
            for item in projects:
                if not (isinstance(item, Mapping) and item.get("id")):
                    continue
                state = store.project_state(item)
                states[str(item["id"])] = state["state"]
                end = state.get("last_result")
                if state["state"] == "waiting" and state.get("ended_last") and isinstance(end, Mapping) \
                        and rq.same_item_run_again(end, combined):
                    same_again.append(str(item["id"]))
            combined["ladder_states"] = states
            if same_again:
                combined["ladder_same_run"] = same_again
                self._note_same_run(same_again, store)
        if store is not None:
            self._check_ladder_low(combined)
            portals = store.world.portals()
            if portals:
                # Both ends of each portal walked through: going home from another dimension needs one.
                combined["known_portals"] = portals
            furnaces = rq.known_furnaces(store.world.snapshot(), combined.get("dimension"),
                                         combined.get("position", combined.get("pos")), time.time())
            if furnaces:
                # What the furnaces near the Body hold, so a ladder item can wait for them.
                combined["known_furnaces"] = furnaces
        frame = super()._frame({**execution, "facts": combined})
        # Store both raw sources so replay can reproduce exactly what online
        # compilation saw, rather than presenting carried observations as raw
        # data supplied by the current tool.
        frame["_frame_input"]["execution"] = copy.deepcopy(
            {key: value for key, value in execution.items() if key != "_frame_input"})
        frame["_frame_input"]["ambient_facts"] = ambient_before
        facts = frame["facts"]
        record = getattr(self.runtime, "record_body_facts_memory", None)
        if callable(record):
            record(facts)
        self._observe_reaction_frame(facts)
        self._note_dimension(facts)
        self._note_spawn(facts)
        # What the latest frame saw, for the words a protective choice was offered with.
        self._latest_facts = facts
        return frame

    def _note_dimension(self, facts: Mapping[str, Any]) -> None:
        """Two living frames in different dimensions with no death between: the Body walked through a portal."""
        dimension, position = facts.get("dimension"), facts.get("position", facts.get("pos"))
        if not isinstance(dimension, str) or not dimension or rq._cell(position) is None or rq.dead(facts):
            return
        epoch = self._death_count + self._life_epoch
        last, self._last_place = self._last_place, (dimension, list(position), epoch)
        store = getattr(self, "_life_store", None)
        if store is None or last is None or last[0] == dimension or last[2] != epoch:
            return
        store.world.portal_walked(last[0], last[1], dimension, position)
        self.runtime.trace.emit("portal_walked", from_dimension=last[0], from_pos=last[1],
                                to_dimension=dimension, to_pos=list(position))

    def _note_spawn(self, facts: Mapping[str, Any]) -> None:
        """The game's own words on the respawn point, and the world spawn, into the world record (§7.7 item 5)."""
        store = getattr(self, "_life_store", None)
        if store is None:
            return
        spawn = facts.get("world_spawn")
        if isinstance(spawn, Mapping):
            store.world.world_spawn_seen(spawn.get("dimension"), spawn.get("pos"))
        said = facts.get("spawn_message")
        if not isinstance(said, Mapping) or not isinstance(said.get("at_ms"), (int, float)):
            return
        message = (said.get("seq"), said.get("at_ms"))
        if message == self._spawn_said:
            return
        self._spawn_said = message
        at = round(float(said["at_ms"]) / 1000, 3)
        known = store.world.respawn()
        if known and isinstance(known.get("t"), (int, float)) and known["t"] >= at:
            # Written before this process started: the client keeps its last message until it disconnects.
            return
        text = str(said.get("text") or said.get("key") or "")
        if said.get("key") == "block.minecraft.set_spawn":
            store.world.respawn_set(said.get("dimension"), said.get("pos"), said.get("block"), text, at=at)
        elif said.get("key") == "block.minecraft.spawn.not_valid":
            store.world.respawn_lost(text, at=at)
        else:
            return
        self.runtime.trace.emit("respawn_message", key=said.get("key"), pos=said.get("pos"), block=said.get("block"))

    def _observe_reaction_frame(self, facts: Mapping[str, Any]) -> None:
        """Telemetry only: first frame in which an existing option is eligible."""
        near = rq.hostiles(facts)
        ids = set()
        for entity in near:
            identity = entity.get("id")
            if not isinstance(identity, str) or not identity:
                continue
            ids.add(identity)
            self._reaction_observations.setdefault(identity, {
                "target": identity, "event_monotonic_s": time.monotonic(),
                "event_sampled_at_ms": facts.get("sampled_at_ms"),
                "event_health": facts.get("health"), "event_distance": entity["dist"],
            })
        self._reaction_observations = {key: value for key, value in self._reaction_observations.items() if key in ids}

    def _reaction_question(self, facts: Mapping[str, Any], stamp: float) -> dict[str, Any] | None:
        near = rq.hostiles(facts)
        observed = self._reaction_observations.get(near[0].get("id")) if near else None
        return None if observed is None else {**observed, "request_monotonic_s": stamp}

    def _reaction_answer(self, chain: dict[str, Any] | None, choice: str) -> None:
        if chain is not None and choice in {"engage", "ranged", "move_away"}:
            self._reaction_selection = {**chain, "choice": choice, "answer_monotonic_s": time.monotonic()}

    def observe_native_action_progress(self, operation_id: str, payload: Mapping[str, Any]) -> None:
        """Called by the existing observation lane; no new sampler or loop."""
        event_loop = self._reaction_event_loop
        if event_loop is not None and not event_loop.is_closed() and payload.get("action_id"):
            event_loop.call_soon_threadsafe(self._reaction_progress, operation_id, time.monotonic(),
                                           payload.get("action_id"), payload.get("sampled_at_ms"))

    def _reaction_progress(self, operation_id: str, stamp: float, native_id: Any, sampled_at: Any) -> None:
        chain = self._reaction_actions.pop(operation_id, None)
        if chain is not None:
            self.runtime.trace.emit("reflex_reaction_chain", **chain,
                tool_call_id=operation_id, action_progress_monotonic_s=stamp,
                native_action_id=native_id, action_sampled_at_ms=sampled_at,
                event_to_request_s=chain["request_monotonic_s"]-chain["event_monotonic_s"],
                request_to_answer_s=chain["answer_monotonic_s"]-chain["request_monotonic_s"],
                answer_to_action_s=stamp-chain["answer_monotonic_s"],
                total_s=stamp-chain["event_monotonic_s"], first_native_progress=True)

    async def judge(self, call_id, tool, params, execution, idle_s, **kwargs):
        facts = execution.get("facts") or {}
        chain = self._reaction_question(facts, time.monotonic())
        choice = await super().judge(call_id, tool, params, execution, idle_s, **kwargs)
        self._reaction_answer(chain, choice)
        return choice

    def observe_call_result(self, tool, params, result) -> None:
        super().observe_call_result(tool, params, result)
        payload = result.to_payload() if isinstance(result, ToolResult) else result
        if not isinstance(payload, Mapping):
            return
        try:
            self.reconcile_ownership(payload, tool_name=tool)
        except Exception as exc:
            self.runtime.trace.emit("ownership_reconciliation_failed", tool=tool, error_type=type(exc).__name__)

    def reconcile_ownership(self, payload, *, tool_name="") -> None:
        archive = getattr(self.runtime, "observation_archive", None)
        if getattr(archive, "ledger", None) is not None:
            archive.reconcile_ownership(payload, tool_name=tool_name)
        else:
            policy = getattr(self.runtime, "governance_policy", None)
            if policy is not None:
                ledger = OwnershipLedger(policy)
                edits = ledger.removals_from(payload)
                if edits:
                    ledger.applied(edits)

    def _shooter_at(self, released: float) -> str:
        """Who loosed an arrow released then: Decision on its own, an Agent
        rule Decision carried out, or the Agent's own call or plan."""
        for span in reversed(self._reflex_spans):
            end = span["end"] if span["end"] is not None else float("inf")
            if span["start"] - SHOT_SPAN_SLACK_S <= released <= end + SHOT_SPAN_SLACK_S:
                return span["by"]
        return "you"

    def shot_log(self) -> list[dict[str, Any]]:
        """The arrows loosed this session (oldest first), each with ``by``."""
        return [dict(shot) for shot in self._shot_log]

    async def run(self, tool: Any, callback: Any, args: tuple[Any, ...], **kwargs: Any) -> Any:
        """Keep the lifecycle state aligned with the one physical call path."""
        context = kwargs.get("supervision_context") or {}
        context = context if isinstance(context, Mapping) else {}
        previous = self.loop_state
        death_count = self._death_count
        await self.start()
        if self._world_connected is False:
            return {"success": False, "reason": "server_disconnected", "canRetry": True}
        if await self._process_lifecycle_events():
            return {"success": False, "reason": "death", "canRetry": False}
        call_id = str(kwargs.get("call_id") or "")
        self._result_pending[call_id] = {"call_id": call_id}
        if context.get("is_life_action"):
            target = "idle"
        elif context.get("is_reflex_action"):
            choice = context.get("reflex_action_choice")
            target = ("threat" if choice in {"move_away", "engage", "ranged"} or str(choice).startswith(rq.RULE_PREFIX)
                      else "survival")
        else:
            target = "work"
        self._set_state(target, "agent_call" if target == "work" else "decision")
        span = None
        if context.get("is_reflex_action") and not context.get("is_life_action"):
            # Decision's own protective action, or an Agent rule it carried
            # out; opened only where the finally below closes it.
            chosen = str(context.get("reflex_action_choice") or "")
            span = {"by": "rule" if chosen.startswith(rq.RULE_PREFIX) else "decision",
                    "start": time.monotonic(), "end": None}
            self._reflex_spans.append(span)
            del self._reflex_spans[:-REFLEX_SPANS_KEPT]
        raised = False
        try:
            return await super().run(tool, callback, args, **kwargs)
        except BaseException:
            raised = True
            raise
        finally:
            if span is not None:
                span["end"] = time.monotonic()
            # A protective terminal still awaits Decision's return/handoff
            # choice. Keep its survival/threat state through that result.
            if not context.get("is_reflex_action") and death_count == self._death_count:
                self._set_state(previous, "call_ended")
            handoff = getattr(self.runtime, "direct_call_handoffs", {}).get(call_id, {})
            decided = handoff.get("facts", {}).get("decision", {}).get("choice")
            if raised or not context.get("is_plan") or decided in {"step_done", "next_step", "call_agent", "repeat", "interrupt"}:
                # WorkPlanExecutor consumes a running-frame verdict directly,
                # without asking another result question. There is therefore
                # no later decide_plan_result owner to release this slot.
                # A call that raised has no result question either; keeping
                # its slot stopped every idle frame for good (villa run
                # 2026-10-15, no frame after 23:32:52).
                self._release_result_pending(call_id)
            self._ensure_idle_task()

    def _start_monitor(
        self, record: dict[str, Any], tool: Any, params: Mapping[str, Any],
        wake: asyncio.Event, worker: asyncio.Task[Any],
    ) -> asyncio.Task[Any] | None:
        """All calls, including protective calls, use the lifecycle consumer."""
        if self._active_monitor is not None:
            raise RuntimeError("BodyLoop already has an active monitor")
        record["monitor_registered"] = True
        record["monitor_done"] = asyncio.Event()
        self._result_pending.pop(str(record.get("call_id") or ""), None)
        self._active_monitor = (record, tool, params, wake, worker)
        self._loop_wakeup.set()
        return None

    async def _wait_monitor(self, record: Mapping[str, Any]) -> None:
        done = record.get("monitor_done")
        if isinstance(done, asyncio.Event):
            await done.wait()
        transition = record.get("protection_task")
        if isinstance(transition, asyncio.Task):
            await transition

    async def _monitor(
        self, record: dict[str, Any], tool: Any, params: Mapping[str, Any],
        wake: asyncio.Event, worker: asyncio.Task[Any],
    ) -> None:
        """Consume work facts and execute the one in-flight Decision answer.

        ``CallSupervisor`` supplies the call registration and settlement
        helpers; this Body-owned method is the only frame consumer.  A newer
        fact replaces a pending frame while a Decision request is in flight.
        """
        last_revision = -1
        decision_task: asyncio.Task[str] | None = None
        decision_execution: dict[str, Any] | None = None
        pending: tuple[dict[str, Any], float, str] | None = None
        decision_revision = -1
        snapshots = getattr(self.runtime, "execution_snapshots", None)
        last_body_read = float("-inf")
        airborne = False
        while not record["cancel_requested"]:
            if await self._process_lifecycle_events():
                return
            if worker.done():
                if decision_task is not None:
                    decision_task.cancel()
                    await asyncio.gather(decision_task, return_exceptions=True)
                return
            wake_wait = asyncio.create_task(wake.wait())
            clock_wait = asyncio.create_task(asyncio.sleep(
                FALL_FRAME_INTERVAL_S if airborne else supervision_module.FACT_FRAME_INTERVAL_S))
            wait_set: set[asyncio.Future[Any]] = {wake_wait, clock_wait}
            if decision_task is not None:
                wait_set.add(decision_task)
            if not worker.done():
                wait_set.add(worker)
            done, _pending_tasks = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
            finished_decision = decision_task if decision_task is not None and decision_task in done else None
            if clock_wait in done and not worker.done() and snapshots is not None:
                snapshots.tick(record["call_id"])
            if wake_wait in done or clock_wait in done:
                wake.clear()
                # Lite action progress carries motion and vitals, but not
                # perception. Refresh the same Body read used while idle;
                # carrying the last call's entities cannot observe a new
                # attacker (or one that has gone away). This stays in the
                # one lifecycle consumer and the existing read lane.
                # Off the ground, the Body is read four times a second: a fall
                # fact is only in a full read, and a thrown Body lands within
                # 1-2.5 s (end run-33).
                read_gap = FALL_FRAME_INTERVAL_S if airborne else supervision_module.FACT_FRAME_INTERVAL_S
                if time.monotonic() - last_body_read >= read_gap:
                    await self._refresh_running_facts(record["call_id"])
                    last_body_read = time.monotonic()
                execution = self._frame(self._snapshot(record["call_id"]))
                airborne = (execution.get("facts") or {}).get("on_ground") is False
                if await self._process_lifecycle_events(execution.get("facts")) or record["cancel_requested"]:
                    if decision_task is not None:
                        decision_task.cancel()
                        await asyncio.gather(decision_task, return_exceptions=True)
                    for waiter in (wake_wait, clock_wait):
                        waiter.cancel()
                    await asyncio.gather(wake_wait, clock_wait, return_exceptions=True)
                    return
                if execution and getattr(tool, "name", "") not in COMMITTED_ACTIONS:
                    revision = int(execution.get("fact_revision") or 0)
                    phase_changed = execution.get("phase") != record.get("last_phase")
                    record["last_phase"] = execution.get("phase")
                    if revision > last_revision and (phase_changed or revision != last_revision):
                        last_revision = revision
                        idle_s = float(execution.get("since_progress_s") or 0.0)
                        if (not record["initial_reader_done"]
                                and callable(getattr(tool, "supervision_reader", None))):
                            record["initial_reader_done"] = True
                            await self._bounded_read(record, tool, params)
                            execution = self._frame(self._snapshot(record["call_id"]) or execution)
                        frame = (execution, idle_s, "running_frame")
                        frame_options = self._options_for(
                            tool, record,
                            supplied=self._tree_options(record["supervision_context"]),
                            execution=execution,
                        )
                        due = self._protection_frame_due(record, execution)
                        if record.pop("stance_due", False):
                            due = True
                        if not due and decision_task is None:
                            self._check_stance_soon(record, execution, wake, worker)
                        if due and decision_task is not None and not decision_task.done():
                            pending = frame
                        elif due and decision_task is None and execution.get("state") not in {"succeeded", "failed", "cancelled", "unknown"}:
                            decision_revision = revision
                            decision_execution = execution
                            decision_task = asyncio.create_task(self._judge_frame(
                                record["call_id"], tool, params, execution, idle_s,
                                purpose="running_frame",
                                supervision_context=record["supervision_context"], options=frame_options,
                            ))
                            record["decision_task"] = decision_task
            if finished_decision is not None:
                try:
                    choice = finished_decision.result()
                except (Exception, asyncio.CancelledError):
                    # A Decision task the call's own wrap-up cancelled
                    # (CallSupervisor.run, when the Agent stops the activity)
                    # is no answer. ``.result()`` raises CancelledError for it,
                    # which is not an Exception; letting it out ended the one
                    # lifecycle task for good (villa run 2026-10-16, 12:41).
                    choice = "unknown"
                if decision_task is finished_decision:
                    decision_task = None
                record["questions"] += 1
                if choice == "unknown":
                    record["unknown_answers"] += 1
                latest = self._snapshot(record["call_id"])
                if worker.done() or latest.get("state") in TERMINAL_STATES:
                    # This answer was about running work, which has already
                    # ended. Its one terminal question owns what follows; a
                    # late running answer cannot stop or redirect that result.
                    self.runtime.trace.emit(
                        "call_supervision_answer_discarded", tool_call_id=record["call_id"],
                        choice=choice, reason="call_ended", fact_revision=decision_revision,
                    )
                    for waiter in (wake_wait, clock_wait):
                        if not waiter.done():
                            waiter.cancel()
                    await asyncio.gather(wake_wait, clock_wait, return_exceptions=True)
                    return
                # The option was compiled from this complete frame. Native
                # lite snapshots omit carried inventory/perception and the
                # life profile; rebinding against one silently loses actions
                # or sends the chosen action to a different target.
                execution = decision_execution or {}
                if self._continues_protection(record, choice, execution):
                    choice = "keep_going"
                if choice == "inspect" and record["reads"] < self.max_reads:
                    record["reader_new_fact"] = False
                    read = await self._bounded_read(record, tool, params)
                    blocker = self._deterministic_terminal(
                        self._snapshot(record["call_id"]) or execution, read,
                    )
                    if blocker is not None:
                        await self._handoff(
                            record, self._snapshot(record["call_id"]) or execution,
                            blocker, decision=None,
                            plan=self._plan_context(
                                supervision_context=record["supervision_context"],
                                tool=tool, params=params,
                            ),
                        )
                        return
                elif choice == "inspect":
                    self.runtime.trace.emit(
                        "call_supervision_limit", tool_call_id=record["call_id"],
                        tool=getattr(tool, "name", "unknown"), kind="reads", limit=self.max_reads,
                    )
                elif choice in {"step_done", "next_step", "call_agent", "interrupt", "put_down", "set_aside"}:
                    record["decision"] = choice
                    plan = self._plan_context(
                        supervision_context=record["supervision_context"],
                        tool=tool, params=params,
                    )
                    if choice in {"put_down", "set_aside"}:
                        # The item ends here like a hand-off, but its record
                        # says it was put down (set aside), so it waits for
                        # the Agent to change it.
                        manager = getattr(self.runtime, "reflex_manager", None)
                        if manager is not None:
                            manager.put_down_project()
                    normalized_choice = ("step_done" if choice in {"step_done", "next_step"} else
                                         "call_agent" if choice in {"put_down", "set_aside"} else choice)
                    record["decision"] = normalized_choice
                    reason = ("decision_step_done" if normalized_choice == "step_done" else
                              "decision_interrupt" if choice == "interrupt" else
                              "decision_put_down" if choice == "put_down" else
                              "decision_set_aside" if choice == "set_aside" else "decision_call_agent")
                    await self._handoff(
                        record, execution,
                        {"reason": reason, "confirmed": normalized_choice == "step_done"},
                        decision=normalized_choice, plan=plan,
                    )
                    return
                elif choice == "stay_put" and record["supervision_context"].get("is_reflex_action"):
                    # The protective action stops and the Body stays where it is.
                    await self._handoff(record, execution, {"reason": "decision_stay_put"}, decision=choice)
                    return
                elif (rq.runs_default(choice) or choice == "go_to_surface"
                      or (record["supervision_context"].get("is_life_action")
                          and choice in {"go_home", "approach_player", "follow_player", "look_player", "seek_player", "stay"})):
                    if choice == "stay":
                        await self._handoff(record, execution, {"reason": "decision_stay"}, decision=choice)
                        return
                    action = self._default_action(choice, execution)
                    if action is not None:
                        if record["supervision_context"].get("is_reflex_action"):
                            self._protective_redirect = (choice, action)
                            await self._handoff(record, execution,
                                {"reason": "decision_default_tool", "confirmed": False},
                                decision=choice)
                        else:
                            self._suspended = record
                            # The execution transaction waits outside this
                            # consumer. Return to the main loop so the next
                            # protective call registers as current work.
                            self._protection_task = asyncio.create_task(
                                self._protect_work(record, execution, choice, action))
                            record["protection_task"] = self._protection_task
                        return
                    self.runtime.trace.emit(
                        "call_supervision_default_unknown", tool_call_id=record["call_id"],
                        choice=choice,
                    )
                if pending is not None:
                    frame = pending
                    pending = None
                    if int(frame[0].get("fact_revision") or -1) > decision_revision:
                        decision_revision = int(frame[0].get("fact_revision") or -1)
                        decision_execution = frame[0]
                        decision_task = asyncio.create_task(self._judge_frame(
                            record["call_id"], tool, params, frame[0], frame[1],
                            purpose=frame[2], supervision_context=record["supervision_context"],
                            options=self._options_for(
                                tool, record,
                                supplied=self._tree_options(record["supervision_context"]),
                                execution=frame[0],
                            ),
                        ))
                        record["decision_task"] = decision_task
            if worker in done and decision_task is None:
                for waiter in (wake_wait, clock_wait):
                    if waiter not in done:
                        waiter.cancel()
                await asyncio.gather(wake_wait, clock_wait, return_exceptions=True)
                return
            if wake_wait not in done and decision_task is None and worker.done():
                for waiter in (wake_wait, clock_wait):
                    if waiter not in done:
                        waiter.cancel()
                await asyncio.gather(wake_wait, clock_wait, return_exceptions=True)
                return
            for waiter in (wake_wait, clock_wait):
                if waiter not in done:
                    waiter.cancel()
            await asyncio.gather(wake_wait, clock_wait, return_exceptions=True)

    async def _handoff(
        self, record: dict[str, Any], execution: Mapping[str, Any],
        details: Mapping[str, Any], *, decision: str | None,
        plan: Mapping[str, Any] | None = None,
    ) -> None:
        if self.loop_state == "dead" or self.current is not record or record["cancel_requested"]:
            return
        record["cancel_requested"] = record["settling"] = True
        call_id = record["call_id"]
        safe = self._handoff_facts(
            self._snapshot(call_id) or execution,
            reason=str(details.get("reason") or "decision_call_agent"),
            confirmed=bool(details.get("confirmed")), decision=decision,
            plan=plan, extra=details,
        )
        self.runtime.trace.emit("direct_call_blocked", tool_call_id=call_id, facts=safe)
        self.runtime.trace.emit(
            "call_supervision_stop_requested", tool_call_id=call_id,
            tool=execution.get("tool"), reason=safe["reason"], decision=decision,
        )
        # The existing stop/settlement entry remains the only stop owner. Do
        # not revoke the containing plan: step_done must let it advance.
        is_plan = bool(record["supervision_context"].get("is_plan"))
        is_protection = bool(record["supervision_context"].get("is_reflex_action"))
        if is_protection:
            self._protective_verdict = str(decision or "call_agent")
        started = time.monotonic()
        settled = await self.runtime.cancel_active_execution_with_facts(
            "direct_call:" + safe["reason"], cancel_reflex=False,
            # A plan handoff stops the current node but does not invalidate
            # the containing activity. Direct calls have no owner to preserve
            # and keep the existing generation invalidation safety boundary.
            invalidate_generation=not (is_plan or is_protection),
        )
        self.runtime.trace.emit(
            "call_supervision_stop_settled", tool_call_id=call_id,
            elapsed_s=time.monotonic() - started,
            settled=bool(settled.get("settled")) if isinstance(settled, Mapping) else None,
        )
        self.runtime.trace.emit(
            "direct_call_handoff", tool_call_id=call_id, facts=safe, settlement=settled,
        )
        self.runtime.direct_call_handoffs[call_id] = {"facts": safe, "settlement": settled}
        # Only after the unified stop path has settled may a direct call send
        # its fact to the Agent. Plan calls are reported once by
        # ReflexManager, after WorkPlanExecutor records the node state; doing
        # both here and there would create two facts for one stopped node.
        if not is_plan:
            # Use the runtime's unified delivery path.  Responses sessions
            # forward this to the kernel; SDK sessions forward it to their
            # outer AgentSession listener.  Calling the kernel directly here
            # silently drops the handoff for SDK direct calls.
            notify_fact = getattr(self.runtime, "notify_reflex_fact", None)
            if callable(notify_fact):
                terminal_facts = execution.get("facts") if isinstance(execution.get("facts"), Mapping) else {}
                if is_protection:
                    tool_result = execution.get("result") if isinstance(execution.get("result"), Mapping) else {}
                    notify_fact({
                        "kind": "protection", "call_id": call_id,
                        "activity_id": f"protection-{call_id}", "status": "protection_result", "wake": False,
                        "tool": safe.get("tool") or record.get("tool"),
                        "reason": tool_result.get("reason") or safe.get("reason"),
                        "position": terminal_facts.get("position", terminal_facts.get("pos")),
                        **({"health_before": terminal_facts["health_before"]}
                           if "health_before" in terminal_facts else {}),
                        **({"health_after": terminal_facts["health"]} if "health" in terminal_facts else {}),
                        "ended": {"outcome": execution.get("state") or safe.get("reason"),
                                  "reason": tool_result.get("reason") or safe.get("reason")},
                        "result": dict(tool_result) if tool_result else safe,
                    })
                else:
                    # Preserve the established direct-call hand-back payload and wake behavior.
                    notify_fact({"kind": "direct_call", "call_id": call_id,
                                 "status": "handoff_requested", "result": safe, **safe})



    async def _decide_result(
        self, record: dict[str, Any], tool: Any, params: Mapping[str, Any], result: Any,
    ) -> str:
        at_terminal = self._snapshot(record["call_id"])
        if not at_terminal:
            at_terminal = {"tool": getattr(tool, "name", "unknown"), "facts": {}}
        payload = result if isinstance(result, Mapping) else getattr(result, "result", result)
        if not isinstance(payload, Mapping):
            payload = {"result": str(payload)[:500]}
        body_at_terminal = dict(at_terminal.get("facts") or {})
        history = record.setdefault("history", [])
        history.append({"result": self._compact_result(payload),
                        "body": {"position": body_at_terminal.get("position"),
                                 "inventory": body_at_terminal.get("inventory")}})
        record["history_total"] = int(record.get("history_total") or 0) + 1
        del history[:-5]
        while True:
            # The call's own facts explain the result; the Body as it is now
            # decides what follows (pro plan P7 §9.3).
            execution = self._frame({**at_terminal, "facts": {**body_at_terminal, **await self._body_now()},
                                     "state": "succeeded" if payload.get("success") is True else "failed",
                                     "result": dict(payload)})
            if await self._process_lifecycle_events(execution.get("facts")):
                return "unknown"
            options = self._options_for(tool, record, execution=execution)
            body = dict(execution.get("facts") or {})
            context = {
                "tool_result": dict(payload),
                "tool_arguments": dict(params),
                "body_state": body,
                "body_at_terminal": body_at_terminal,
                "execution": execution,
                "reads": [],
                "history": list(history),
                "history_earlier": record["history_total"] - len(history),
            }
            try:
                # A terminal result must never hold the Body or an Agent turn
                # indefinitely when the Decision endpoint is unavailable.
                choice = await asyncio.wait_for(self._judge_terminal_frame(
                    record["call_id"], tool, params, execution,
                    float(execution.get("since_progress_s") or 0.0),
                    purpose="result_frame", supervision_context=record["supervision_context"],
                    options=options, decision_context=context,
                ), timeout=2.2)
            except asyncio.TimeoutError:
                choice = "unknown"
            record["questions"] += 1
            if choice == "unknown":
                record["unknown_answers"] += 1
                return self._unjudged_after_protection(record, at_terminal, payload)
            action = (self._default_action(choice, execution)
                      if choice in options and rq.runs_default(choice) else None)
            if action is None:
                break
            # Outside the 2.2 s answer wait: protection runs on the call's
            # own remaining time, never a fresh budget (pro plan P7 §9.6).
            after = await self._protect_result(record, execution, choice, action)
            if after == "judge_again":
                # Back to judging this same result, not running it again.
                continue
            if after != "call_agent":
                return self._unjudged_after_protection(record, at_terminal, payload)
            choice = "call_agent"
            break
        if choice == "repeat":
            return "repeat"
        candidates = rq.equipment_candidates(body, payload)
        if choice in candidates:
            choice = await self._run_result_equipment(record, tool, params, candidates[choice])
            if choice in {"repeat", "unknown"}:
                return choice
        choice = {"next_step": "step_done"}.get(choice, choice)
        if choice in {"step_done", "call_agent"}:
            record["decision"] = choice
            # The Body is already terminal.  Store the result judgement for
            # the caller without routing a second stop through the running
            # call path (and without rewriting a real terminal result).
            execution = self._snapshot(record["call_id"]) or execution
            safe = self._handoff_facts(
                execution, reason=f"decision_{choice}",
                confirmed=choice == "step_done", decision=choice,
                plan=self._plan_context(
                    supervision_context=record["supervision_context"],
                    tool=tool, params=params,
                ),
                extra={"tool_result": self._compact_result(payload),
                       **({"default_action": record["equipment_result"]}
                          if "equipment_result" in record else {})},
            )
            if "result_protection" in record:
                safe["default_action"] = record["result_protection"]
            self.runtime.trace.emit("direct_call_handoff", tool_call_id=record["call_id"],
                                    facts=safe, settlement={"settled": True, "terminal": True})
            self.runtime.direct_call_handoffs[record["call_id"]] = {
                "facts": safe, "settlement": {"settled": True, "terminal": True},
            }
            if not bool(record["supervision_context"].get("is_plan")):
                notify = getattr(self.runtime, "notify_reflex_fact", None)
                if callable(notify):
                    notify({"call_id": record["call_id"], "status": "handoff_requested", **safe})
        return choice

    def _unjudged_after_protection(self, record: dict[str, Any], execution: Mapping[str, Any],
                                   payload: Mapping[str, Any]) -> str:
        """The result stays unjudged; if the Body protected itself meanwhile,
        the Agent's tool result says so. The call's own result is not rewritten."""
        if "result_protection" in record:
            safe = self._handoff_facts(execution, reason="decision_unknown", confirmed=False, decision="unknown",
                                       extra={"tool_result": self._compact_result(payload)})
            safe["default_action"] = record["result_protection"]
            self.runtime.direct_call_handoffs[record["call_id"]] = {
                "facts": safe, "settlement": {"settled": True, "terminal": True}}
        return "unknown"

    async def _protect_result(
        self, record: dict[str, Any], execution: Mapping[str, Any], choice: str,
        action: tuple[str, dict[str, Any]],
    ) -> str:
        """Run a protective action chosen in a result question (pro plan P7 §9.5).

        The ended call's result stays as it was. The same stop entry as a
        running frame's protection ends the call's claim and the protective
        call runs under its own claim. When it ends, the same result
        question is asked again on fresh facts ("judge_again"); it offers
        protection again while danger lasts, and the Agent. Unlike
        ``_run_default_action`` nothing is re-run.

        No "go back or hand to the Agent" question in between: with the
        danger gone it handed back 125 of 132 times, while the result
        question asked again went on with the plan in 115 of 144
        (docs/references/p7-resume-bench-20261019).
        """
        remaining = self._remaining(record)
        if remaining is not None and remaining <= 0:
            return "unknown"
        call_id = record["call_id"]
        self.runtime.trace.emit(
            "call_supervision_default_requested", tool_call_id=call_id, choice=choice,
            tool=action[0], arguments=action[1], purpose="result_frame")
        await self.runtime.cancel_active_execution_with_facts(
            "direct_call:decision_default_tool", cancel_reflex=False,
            # The ended call's owner stays valid; its result is still judged.
            invalidate_generation=False,
        )

        async def protect() -> str:
            await self._execute_protection(choice, action, record)
            if self._protective_verdict == "call_agent":
                return "call_agent"
            if await self._process_lifecycle_events() or self._world_connected is False:
                return "unknown"
            return "judge_again"

        task = asyncio.create_task(protect())
        self._result_protections.add(task)
        try:
            after = await (task if remaining is None else asyncio.wait_for(task, remaining))
        except asyncio.TimeoutError:
            after = "unknown"
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if not task.cancelled() or (caller is not None and caller.cancelling()):
                raise
            after = "unknown"  # the loop stopped it (session end, world lost)
        finally:
            self._result_protections.discard(task)
        done_choice, done_action = record.get("protective_action", (choice, action))
        record["result_protection"] = {
            "choice": done_choice, "tool": done_action[0], "then": after,
            "result": self._compact_result(record.get("protective_result", {}))}
        self.runtime.trace.emit("call_supervision_default_finished", tool_call_id=call_id,
                                choice=choice, purpose="result_frame",
                                facts=record.get("result_protection"))
        if after == "judge_again":
            self._set_state("work", "decision")
            self._remember_protection(done_action, record.get("protective_result", {}))
        return after

    async def _run_result_equipment(self, record, original_tool, original_params, item, *,
                                    claim_holder="agent_call", reflex_action_choice=None):
        """Result equipment uses the same judged single-node tree."""
        from minebot.app.work_runtime import WorkPlanExecutor
        from minebot.brain.registry import execute_tool
        budget = self._remaining(record)
        if budget is None or budget <= 0:
            return "unknown"
        await self.runtime.cancel_active_execution_with_facts(
            "direct_call:decision_equipment", cancel_reflex=False, invalidate_generation=False)
        node_id = f"equipment_{uuid4().hex}"
        spec = {"language": "minebot-work/0.2", "goal": str(record["supervision_context"].get("intent") or ""),
                "timeout_s": budget, "work": {"kind": "call", "id": node_id,
                    "tool": "equip_item", "intent": rq.equipment_intent(item),
                    "arguments": {"item": item, "target": "mainhand"}}}
        executor = None
        async def call(name, arguments, *, call_id, timeout_s):
            target = self.runtime.registry.get(name)
            transport_id = f"plan-{call_id}"
            context = executor.call_context(call_id)
            if claim_holder == "threat":
                context = {**context, "is_reflex_action": True, "reflex_action_choice": reflex_action_choice}
            raw = await self.runtime.run_tool(target, execute_tool, target, arguments,
                getattr(self.runtime, "weld_context", None), tool_call_id=transport_id,
                claim_holder=claim_holder, timeout_s=self._remaining(record),
                return_execution_receipt=True, supervision_context=context)
            projector = getattr(self.runtime, "shown_plan_result", None)
            shown = projector(name, raw, transport_id) if callable(projector) else dict(raw)
            if isinstance(raw.get("_execution"), dict):
                shown["_execution"] = raw["_execution"]
            handoff = (raw.get("metrics") or {}).get("direct_call_handoff")
            if isinstance(handoff, dict):
                shown["_supervision_handoff"] = copy.deepcopy(handoff)
            return shown
        executor = WorkPlanExecutor(spec, call=call, state=self._read_idle_facts,
                                    result_judge=self.decide_plan_result, emit=self.runtime.trace.emit)
        executor.plan["work"]["next_call"] = rq.call_text(getattr(original_tool, "name", "unknown"), original_params)
        while self._remaining(record) > 0:
            result = await asyncio.wait_for(executor.run(), timeout=self._remaining(record))
            record["equipment_result"] = {"tool": "equip_item", "result": result.results}
            if result.status == "completed":
                return "repeat"
            if result.status != "waiting":
                return "call_agent" if result.status != "cancelled" else "unknown"
            await asyncio.sleep(supervision_module.FACT_FRAME_INTERVAL_S)
        return "unknown"

    async def _judge_terminal_frame(self, *args: Any, **kwargs: Any) -> str:
        """Submit a terminal question to the single lifecycle consumer.

        Callers retain settlement/history and their existing timeout budget.
        They never open a parallel Decision request while waiting for a result.
        """
        await self.start()
        if self._world_connected is False:
            return "unknown"
        answer = asyncio.get_running_loop().create_future()
        self._terminal_questions.append((answer, args, kwargs))
        self._loop_wakeup.set()
        try:
            return await answer
        finally:
            if not answer.done():
                answer.cancel()
            self._loop_wakeup.set()

    async def _serve_terminal_question(self, answer, args, kwargs) -> None:
        """The lifecycle schedules at most one request, including terminals."""
        if answer.done():
            return
        request = asyncio.create_task(self._judge_frame(*args, **kwargs))
        wake = None
        try:
            while not answer.done():
                self._loop_wakeup.clear()
                wake = asyncio.create_task(self._loop_wakeup.wait())
                done, _ = await asyncio.wait({request, answer, wake}, return_when=asyncio.FIRST_COMPLETED)
                if wake not in done:
                    wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
                wake = None
                if self._lifecycle_events and await self._process_lifecycle_events():
                    if not answer.done():
                        answer.set_result("unknown")
                    break
                if answer.done():
                    break
                if request in done:
                    answer.set_result(request.result())
                    break
        except asyncio.CancelledError:
            answer.cancel()
            raise
        except Exception as exc:
            if not answer.done():
                answer.set_exception(exc)
        finally:
            if wake is not None:
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    async def _judge_frame(self, *args: Any, **kwargs: Any) -> str:
        if await self._process_lifecycle_events():
            return "unknown"
        death_count = self._death_count
        choice = await super()._judge_frame(*args, **kwargs)
        if death_count != self._death_count or await self._process_lifecycle_events():
            return "unknown"
        return choice

    @staticmethod
    def _remaining(record: Mapping[str, Any]) -> float | None:
        timeout = record.get("timeout_s")
        if timeout is None:
            return None
        return max(0.0, float(record.get("started_at", time.monotonic())) + float(timeout) - time.monotonic())

    async def _protect_work(self, record, execution, choice, action) -> None:
        """Execution waits outside the lifecycle; the original deadline remains authoritative."""
        try:
            remaining = self._remaining(record)
            if remaining is None:
                await self._run_default_action(record, execution, choice, action)
            else:
                await asyncio.wait_for(self._run_default_action(record, execution, choice, action), remaining)
        except asyncio.TimeoutError:
            self._refresh_interrupted_result(record)
            safe = self._handoff_facts(execution, reason="decision_default_tool",
                                       confirmed=False, decision="unknown")
            safe["default_action"] = {"choice": choice, "tool": action[0],
                                      "result": self._compact_result(record.get("protective_result", {})),
                                      "then": "unknown"}
            safe["history"] = copy.deepcopy(record.get("history", []))
            safe["history_earlier"] = int(record.get("history_total", 0)) - len(record.get("history", []))
            self.runtime.direct_call_handoffs[record["call_id"]] = {"facts": safe, "settlement": {"settled": True}}
            self.runtime.trace.emit("call_supervision_default_deadline", tool_call_id=record["call_id"], facts=safe)
        finally:
            self._suspended = None
            self._loop_wakeup.set()

    async def _execute_protection(self, choice, action, record=None, *, call_id=None):
        """Register each protective action as current work in the same lifecycle.

        Switching responses replaces the current action, never the original
        suspended slot. Only the enclosing transaction can notify the Agent.
        """
        from minebot.brain.registry import execute_tool
        while True:
            self._protective_redirect = None
            self._protective_verdict = None
            default_id = call_id or f"reflex-default-{uuid4()}"
            default_tool = self.runtime.registry.get(action[0])
            done = asyncio.get_running_loop().create_future()
            self._protection_doing = {"choice": choice, "tool": action[0]}
            self._protection_running = {"choice": choice, "tool": action[0], "arguments": dict(action[1]),
                                        "done": done}
            if getattr(self, "_reflex_claim_holder", "threat") == "life":
                default_tool = self._life_tool(default_tool)
            chain = self._reaction_selection
            self._reaction_selection = None
            if chain is not None and chain["choice"] == choice:
                self._reaction_actions[default_id] = {**chain, "tool": action[0]}
            if rq.is_protective(choice):
                self._vitals.note_protection(time.monotonic(), choice)
            context = {"is_reflex_action": True,
                       "is_life_action": getattr(self, "_reflex_claim_holder", "threat") == "life",
                       "reflex_action_choice": choice,
                       "intent": getattr(self, "_reflex_action_intent", None)
                       or supervision_module._DEFAULT_ACTION_TEXT.get(choice, choice)}
            if record is None or record.get("supervision_context", {}).get("idle_protection"):
                # No Agent work is suspended here. Its last durable goal is
                # historical context, not the task of this Body-owned action.
                context["goal"] = ""
            # What was chosen, where and when: the stance check says it back
            # while the action runs (closed-loop-architecture-20261107.md §3 B).
            seen = getattr(self, "_latest_facts", None) or self._idle_facts()
            context["reflex_action_text"] = rq.chosen_option_text(choice, seen)
            chosen_at = seen.get("position", seen.get("pos"))
            if isinstance(chosen_at, (list, tuple)) and len(chosen_at) == 3:
                context["reflex_action_at"] = list(chosen_at)
            context["reflex_action_started"] = time.monotonic()
            self._protection_seen = {"stance": bool(seen.get("agent_stance")),
                                     "saw": rq.nearest_hostile_words(seen)}
            try:
                result = await self.runtime.run_tool(
                    default_tool, execute_tool, default_tool, action[1],
                    getattr(self.runtime, "weld_context", None),
                    tool_call_id=default_id, claim_holder=getattr(self, "_reflex_claim_holder", "threat"),
                    supervision_context=context,
                )
            except Exception as exc:
                result = {"success": False, "reason": type(exc).__name__}
                self.runtime.trace.emit("call_supervision_default_failed", tool_call_id=default_id,
                                        choice=choice, error_type=type(exc).__name__,
                                        error=str(exc)[:200],
                                        facts=getattr(exc, "facts", None))
            except BaseException:
                if not done.done():
                    done.set_result({"success": False, "reason": "cancelled"})
                raise
            if not done.done():
                done.set_result(result)
            incomplete = self._reaction_actions.pop(default_id, None)
            if incomplete is not None:
                self.runtime.trace.emit("reflex_reaction_chain", **incomplete,
                                        tool_call_id=default_id, first_native_progress=False,
                                        result=self._compact_result(result))
            if record is not None:
                record["protective_result"] = result
                record["protective_action"] = (choice, action)
                self._refresh_interrupted_result(record)
            redirect = self._protective_redirect
            if redirect is None:
                return choice, action, result, default_id
            choice, action = redirect
            if rq.is_protective(choice):
                self._reflex_claim_holder = "threat"
                self._reflex_action_intent = None
            call_id = None

    def same_running_action(self, tool_name: str, arguments: Mapping[str, Any]) -> tuple[Any, dict[str, Any]] | None:
        """The protective action running now, if it is the call the Agent makes.

        Same tool, same target (the same entity, or one of the type the Agent
        named). The Agent's call then joins it instead of stopping it and
        starting it again: idempotent (user 2026-11-06). In run-27 the Agent's
        engage on the dragon restarted the reflex's running engage on it and
        the walk in began again (body-preemption-20261106.md §10).
        """
        running = getattr(self, "_protection_running", None)
        if not running or running["done"].done() or running["tool"] != tool_name:
            return None
        wanted = arguments.get("target")
        held = running["arguments"].get("target")
        if wanted is None or held is None:
            return None
        if str(wanted) != str(held):
            name = str(wanted).removeprefix("minecraft:")
            facts = [self._idle_facts(), ((getattr(self, "_last_frame_input", None) or {}).get("execution") or {}).get("facts") or {}]
            kinds = {str(item.get("type") or "").removeprefix("minecraft:")
                     for source in facts if isinstance(source, Mapping)
                     for item in rq.hostiles(source) if str(item.get("id")) == str(held)}
            if name not in kinds:
                return None
        return running["done"], {key: running[key] for key in ("choice", "tool", "arguments")}

    async def yield_protection_to_agent(self) -> list[dict[str, Any]]:
        """An Agent call arrived: protective work of the reflex layer stops.

        body-preemption-20261106.md §3: the Agent's call is the newest
        decision. The idle chain and any result protection of an earlier call
        end here; the caller then stops the Body once through the existing
        stop entry. Two chains alive at once refused each other's steps as
        body_busy (run-24: 43 of 64).
        """
        tasks = [task for task in (self._protection_task, *self._result_protections)
                 if task is not None and not task.done() and task is not asyncio.current_task()]
        if not tasks:
            return []
        doing = dict(getattr(self, "_protection_doing", None) or {})
        self._protection_task = None
        for task in tasks:
            task.cancel()
        await asyncio.wait(tasks, timeout=2.0)
        self.runtime.trace.emit("reflex_protection_yielded", reason="agent_call_arrived", doing=doing,
                                tasks=len(tasks))
        return [{"kind": "protection", **doing, "now": "stopped; your call runs instead"}]

    def _life_tool(self, tool):
        """Life movement may dig but not place. Breaking stays off the Agent's
        own blocks: the ownership ledger sends them to the Body as protected for
        travel. A walk home from a closed tunnel failed 12 times when life
        movement could not dig at all (B11, user 2026-10-29). Scope existing
        Body settings inside the admitted writer operation and restore them
        after settlement. This does not introduce another execution lane or
        stop entrance.
        """
        body = self.runtime.body
        def settings(values):
            action = Action.create("setBaritoneSettings", values)
            accepted = body.execute(action)
            if not accepted.ok:
                return False
            if getattr(accepted, "complete", True):
                return True
            # Native acceptance precedes application. Do not start movement
            # until the existing control terminal confirms these settings.
            terminal = body.await_action_terminal(action.id, timeout_s=tool.sidecar.timeout_s or FACT_FRAME_INTERVAL_S)
            return terminal.data.get("success") is True
        def execute(params):
            state = body.get_reflex_facts()
            previous = {key: state[key] for key in ("allow_break", "allow_place") if isinstance(state.get(key), bool)}
            if len(previous) != 2:
                return ToolResult(False, "life_movement_permissions_unknown", True)
            if not settings({"allow_break": True, "allow_place": False}):
                return ToolResult(False, "life_movement_permissions_rejected", True)
            try:
                return tool.callable(params)
            finally:
                if not settings(previous):
                    self.runtime.trace.emit("reflex_life_settings_restore_failed", tool=tool.name)
        return replace(tool, callable=execute)

    def _without_failed_repeat(self, facts: Mapping[str, Any], options: dict[str, str]) -> dict[str, str]:
        """Frame-question §10 in idle frames (user 2026-10-19).

        A life action that just failed by itself is not offered again as the
        same action while the Body is as it was (same block, inventory, hand).
        A walk home from a closed tunnel was chosen 12 times in 30 s, failing
        each time (life-stage session-surface); saying the failure in the
        question did not stop it (p5 home bench: 144/144 and 119/144). A new
        target or a changed Body offers it again.
        """
        failure = self._last_idle_failure
        if not failure or failure["choice"] not in options:
            return options
        same = self._default_action(failure["choice"], {"facts": dict(facts)}) == failure["action"]
        if not (same and rq.body_unchanged_by_call(facts, failure["before"])):
            return options
        if not failure.get("withheld"):
            failure["withheld"] = True
            self.runtime.trace.emit("idle_option_withheld", choice=failure["choice"],
                                    tool=failure["action"][0], arguments=failure["action"][1],
                                    reason=failure["reason"])
        return {key: text for key, text in options.items() if key != failure["choice"]}

    async def _run_idle_action(self, choice: str, facts: Mapping[str, Any]) -> None:
        """Run one reversible life-profile action through the shared Body loop."""
        action = self._default_action(choice, {"facts": dict(facts)})
        if action is None:
            return
        requested = (choice, action)
        self.runtime.trace.emit(
            "call_supervision_default_requested", tool_call_id="idle-loop",
            choice=choice, tool=action[0], arguments=action[1], loop_state=self.loop_state,
        )
        previous_holder = getattr(self, "_reflex_claim_holder", "threat")
        previous_intent = getattr(self, "_reflex_action_intent", None)
        self._reflex_claim_holder = "life"
        self._reflex_action_intent = rq.action_intent(choice, facts)
        try:
            choice, action, result, call_id = await self._execute_protection(
                choice, action, call_id=f"reflex-life-{self._idle_sequence}")
            if self._protective_verdict == "call_agent":
                notify = getattr(self.runtime, "notify_reflex_fact", None)
                if callable(notify):
                    notify({"call_id": call_id, "activity_id": f"{call_id}-{uuid4().hex[:12]}",
                        "kind": "life", "status": "handoff_requested",
                        "reason": "decision_call_agent", "result": self._compact_result(result)})
            else:
                self._remember_protection(action, result)
            outcome = result if isinstance(result, Mapping) else {}
            reason = str(outcome.get("reason") or "")
            self._last_idle_failure = (
                {"choice": requested[0], "action": requested[1], "before": dict(facts), "reason": reason}
                if outcome.get("success") is False and reason not in rq._STOPPED_FROM_OUTSIDE else None)
            self.runtime.trace.emit(
                "call_supervision_default_finished", tool_call_id="idle-loop",
                choice=choice, tool=action[0], result=self._compact_result(result),
            )
        finally:
            self._reflex_claim_holder = previous_holder
            self._reflex_action_intent = previous_intent
            self._set_state("idle", "call_ended")
            self._loop_wakeup.set()

    def _refresh_interrupted_result(self, record) -> None:
        """Keep actual terminals when settlement and the caller resume separately."""
        outcome = record.get("interrupted_result")
        if not isinstance(outcome, dict):
            return
        interruption = outcome["metrics"]["direct_call_handoff"]
        original = record.get("result")
        if isinstance(original, Mapping):
            outcome.clear()
            outcome.update(self._compact_result(original))
            outcome["metrics"] = {"direct_call_handoff": interruption}
        protective = record.get("protective_action")
        if protective is not None:
            choice, action = protective
            interruption["facts"]["default_action"].update(
                choice=choice, tool=action[0],
                result=self._compact_result(record.get("protective_result", {})))

    def _remember_protection(self, action, result) -> None:
        facts = self._idle_facts()
        # Native action terminals use ``position``; the Agent's cached state
        # may still be its read before the protective movement.
        metrics = result.get("metrics") if isinstance(result, Mapping) else None
        if isinstance(metrics, Mapping):
            position = metrics.get("position", metrics.get("pos"))
            if isinstance(position, (list, tuple)) and len(position) == 3:
                facts["position"] = list(position)
        text = rq.protection_experience(time.time(), action[0], action[1], self._compact_result(result), facts)
        seen = getattr(self, "_protection_seen", None)
        if isinstance(seen, Mapping) and seen.get("stance"):
            # Up to the Agent: its stance was read and the hand moved anyway.
            text += f" It was chosen while your stance was set; the hand saw {seen.get('saw')}."
        self.record_experience(text)

    async def _run_default_action(
        self, record: dict[str, Any], execution: Mapping[str, Any], choice: str,
        action: tuple[str, dict[str, Any]],
    ) -> None:
        """Stop and settle the current call, then run one generic escape tool."""
        if self.current is not record or record["cancel_requested"]:
            return
        record["cancel_requested"] = record["settling"] = True
        call_id = record["call_id"]
        safe = self._handoff_facts(
            self._snapshot(call_id) or execution,
            reason="decision_default_tool", confirmed=False, decision=choice,
            plan=self._plan_context(
                supervision_context=record["supervision_context"],
                tool=type("Tool", (), {"name": execution.get("tool")})(), params={},
            ),
            extra={"default_choice": choice},
        )
        self.runtime.trace.emit(
            "call_supervision_default_requested", tool_call_id=call_id,
            choice=choice, tool=action[0], arguments=action[1], facts=safe,
        )
        settled = await self.runtime.cancel_active_execution_with_facts(
            "direct_call:decision_default_tool", cancel_reflex=False,
            # The earlier work may be resumed, so its owner stays valid.
            invalidate_generation=False,
        )
        original = record.get("result")
        original = original if isinstance(original, Mapping) else {"success": False, "reason": "stopped_by_decision"}
        outcome = self._compact_result(original)
        outcome["metrics"] = {"direct_call_handoff": {"facts": {
            "reason": "decision_default_tool", "decision": {"choice": choice},
            "default_action": {"choice": choice, "tool": action[0]},
        }}}
        record["interrupted_result"] = outcome
        record.pop("protective_action", None)
        record.pop("protective_result", None)
        body = execution.get("facts") or {}
        history = record.setdefault("history", [])
        history.append({"result": outcome, "body": {
            "position": body.get("position", body.get("pos")),
            "inventory": body.get("inventory", body.get("inventory_counts")),
        }})
        record["history_total"] = int(record.get("history_total") or 0) + 1
        del history[:-5]
        choice, action, default_result, default_id = await self._execute_protection(choice, action, record)
        record["protective_result"] = default_result
        record["protective_action"] = (choice, action)
        self._refresh_interrupted_result(record)
        outcome["metrics"]["direct_call_handoff"]["facts"]["default_action"].update(
            choice=choice, tool=action[0], result=self._compact_result(default_result))
        after = await self._after_default_action(
            record, action, default_result, default_id, protective_choice=choice,
        )
        self._refresh_interrupted_result(record)
        choice, action = record.get("protective_action", (choice, action))
        default_result = record.get("protective_result", default_result)
        handoff = {**safe, "default_action": {
            "choice": choice, "tool": action[0], "result": self._compact_result(default_result),
            "then": after,
        }, "settlement": settled}
        # Reuse the existing handoff facts rather than adding tool receipt
        # fields. The Agent and next node attempt receive the same raw record.
        handoff["history"] = copy.deepcopy(history)
        handoff["history_earlier"] = record["history_total"] - len(history)
        # The plan runtime reads this choice: "repeat" re-enters the stopped node.
        handoff["decision"] = {**dict(handoff.get("decision") or {}),
                               "choice": "repeat" if after == "resume" else after}
        self.runtime.direct_call_handoffs[call_id] = {"facts": handoff, "settlement": settled}
        self.runtime.trace.emit(
            "call_supervision_default_finished", tool_call_id=call_id,
            choice=choice, facts=handoff,
        )
        if after == "resume":
            self._set_state("work", "decision")
            record["resume_original"] = True
            self._remember_protection(action, default_result)
            # The work goes on; the Agent hears of it asynchronously and may
            # change the plan or take over at any time (user, 2026-10-15). It
            # is told facts only: what stopped the work, what was done, where,
            # and the recent interruptions of this same work.
            notify_fact = getattr(self.runtime, "notify_reflex_fact", None)
            if callable(notify_fact):
                notify_fact({
                    "kind": "protection", "status": "work_resumed_after_protection",
                    "activity_id": f"protection-{call_id}-{record['history_total']}",
                    "call_id": call_id, "work": rq.call_text(record["tool"], record["params"]),
                    "default_action": handoff["default_action"],
                    "interruptions_of_this_work": handoff["history"],
                    "earlier_interruptions": handoff["history_earlier"],
                })
        elif after == "call_agent" and not bool(record["supervision_context"].get("is_plan")):
            self._set_state("idle", "decision")
            notify_fact = getattr(self.runtime, "notify_reflex_fact", None)
            if callable(notify_fact):
                notify_fact({"call_id": call_id, "status": "default_action_completed", **handoff})

    async def _after_default_action(
        self, record: dict[str, Any], action: tuple[str, dict[str, Any]],
        default_result: Any, default_id: str, *, protective_choice: str = "",
    ) -> str:
        # The Body remains in its protective result state during unknown
        # retries. Reserve the existing result slot for the entire transaction,
        # including the cadence between requests, so idle never consumes a
        # result answer or starts unrelated work in that gap.
        call_id = str(record["call_id"])
        reserved_here = call_id not in self._result_pending
        if reserved_here:
            self._result_pending[call_id] = record
        try:
            return await self._follow_default_result(
                record, action, default_result, default_id, protective_choice=protective_choice)
        finally:
            if reserved_here:
                self._release_result_pending(call_id)

    @staticmethod
    def _protection_frame_due(record: dict[str, Any], execution: Mapping[str, Any]) -> bool:
        """A running protective action is asked again when the Body has been
        hurt since the previous frame (user, 2026-10-15), or when the visible
        hostiles are no longer the same ones (pro plan E1, user 2026-10-19:
        "E1随便你").

        Asked every second, Decision chose by distance alone and switched
        between fighting and fleeing before a single hit landed
        (step6-spare-claude2); the same mob coming closer or going farther is
        still not asked about. But a move_away ran on for 47 s after its husk
        was gone (house-continuity 2026-10-19), and a second mob arriving
        unhurt was never seen. A frame without an entity read compares nothing.
        """
        context = record.get("supervision_context") or {}
        if not context.get("is_reflex_action") or context.get("is_life_action"):
            return True
        facts = execution.get("facts") or {}
        due = False
        if any(isinstance(facts.get(key), (list, tuple, Mapping)) for key in rq.HOSTILE_KEYS):
            kinds = {str(entity.get("id")): entity for entity in rq.hostiles(facts) if entity.get("id")}
            seen = {key: entity.get("dist") for key, entity in kinds.items()}
            close = lambda dist: isinstance(dist, (int, float)) and dist <= NEW_HOSTILE_RADIUS
            previous = record.get("protection_hostiles")
            record["protection_hostiles"] = dict(seen)
            # A hostile coming close or a close one gone, or the action's own
            # target gone (a move_away ran on 47 s after its husk was gone).
            # Mobs coming and going far off are no new danger: in the End
            # endermen teleport about and the set changed almost every second,
            # so a fight with the perched dragon was asked again and replaced
            # each time (body-preemption-20261106.md §8.3).
            if isinstance(previous, Mapping):
                target = str((record.get("params") or {}).get("target") or "")
                arrived = any(key not in previous and close(dist) for key, dist in seen.items())
                left = any(key not in seen and (close(dist) or key == target) for key, dist in previous.items())
                due = arrived or left
            # A mob already seen farther off that walks in is asked about once,
            # the first time it comes within reach of a blow (a creeper: of its
            # blast). Once per mob, so a fight is not re-asked at every step.
            # run-37 33:03-33:11: a creeper seen at 25 blocks came 13 -> 11 ->
            # 7 -> 1 while `surface` ran; nothing asked for 27 s, health 20 -> 2.
            # run-38 21:16-21:36: a spider first seen 12 blocks off came to 4.6
            # during a 27 s move_away with no frame (project-review §9.16).
            near_now = {key for key, entity in kinds.items() if _within_closing_radius(entity)}
            asked = record.get("protection_closing_asked")
            if not isinstance(asked, set):
                # Those already near when the action began were in the frame that chose it.
                record["protection_closing_asked"] = set(near_now)
            elif near_now - asked:
                asked |= near_now
                due = True
        air = facts.get("oxygen")
        if isinstance(air, (int, float)):
            # Air full when the action began and falling now: asked once. A
            # fight ran on for 18 s while the Body drowned and nothing asked
            # (run-3, 12:57:44).
            first = record.setdefault("protection_air", float(air))
            if first >= rq.AIR_FULL and air < rq.AIR_FULL and not record.get("protection_air_fell"):
                record["protection_air_fell"] = True
                due = True
        # Falling: asked once, the moment the fall is seen. The dragon threw
        # the Body off a tower and it hit the ground 1-2.5 s later; nothing
        # asked in between, so cushion_fall came with 1-3 blocks left or not
        # at all (end run-33, 19:47:47-49).
        if isinstance(facts.get("fall"), Mapping) and facts.get("on_ground") is not True:
            if not record.get("protection_fall"):
                record["protection_fall"] = True
                due = True
        else:
            record.pop("protection_fall", None)
        health = facts.get("health")
        if not isinstance(health, (int, float)):
            return due
        previous = record.get("protection_health")
        record["protection_health"] = float(health)
        return due or (isinstance(previous, (int, float)) and health < previous - rq.HURT_MARGIN)

    def _check_stance_soon(self, record: dict[str, Any], execution: Mapping[str, Any],
                           wake: asyncio.Event, worker: asyncio.Task[Any]) -> None:
        """While a protective action runs under the Agent's stance, ask at most
        once a second whether the stance still holds (§3 B). Only until it is
        read broken; hurt, a close mob and the target gone ask the protective
        choice again by themselves."""
        context = record.get("supervision_context") or {}
        if not context.get("is_reflex_action") or context.get("is_life_action") or record.get("stance_broken"):
            return
        facts = dict(execution.get("facts") or {})
        if not isinstance(facts.get("agent_stance"), Mapping):
            return
        if "position" not in facts and "pos" not in facts:
            # A running frame without the position (easy run-5, 17:44:51)
            # would leave the stance question without where the Body is.
            seen = getattr(self, "_latest_facts", None) or {}
            for key in ("position", "pos"):
                if key in seen:
                    facts[key] = seen[key]
        running = record.get("stance_task")
        if running is not None and not running.done():
            return
        now = time.monotonic()
        if now - float(record.get("stance_asked_at", float("-inf"))) < supervision_module.FACT_FRAME_INTERVAL_S:
            return
        record["stance_asked_at"] = now
        task = asyncio.create_task(self._check_stance(record, facts, wake))
        record["stance_task"] = task
        worker.add_done_callback(lambda _worker, task=task: task.cancel())

    async def _check_stance(self, record: dict[str, Any], facts: Mapping[str, Any], wake: asyncio.Event) -> None:
        context = record.get("supervision_context") or {}
        started = context.get("reflex_action_started")
        request = rq.stance_check_question(
            facts, chosen_at=context.get("reflex_action_at"),
            chosen_s_ago=time.monotonic() - started if isinstance(started, (int, float)) else None)
        if request is None:
            return
        self.runtime.trace.emit(
            "call_supervision_request", tool_call_id=record.get("call_id"), tool=record.get("tool"),
            purpose="stance_frame", loop_state=self.loop_state, request=request)
        try:
            answer = await asyncio.wait_for(
                self._client.ask(request, priority=supervision_module.DecisionPriority.SUPERVISION), timeout=2.2)
            raw = answer.raw
        except Exception:
            raw = None
        choice = rq.stance_check_choice(raw)
        self.runtime.trace.emit(
            "call_supervision_result", tool_call_id=record.get("call_id"), tool=record.get("tool"),
            purpose="stance_frame", response=raw, choice=choice or "unknown", unknown=choice is None)
        if choice != "broken" or record.get("cancel_requested") or record.get("stance_broken"):
            return
        record["stance_broken"] = True
        record["stance_due"] = True
        stance = facts.get("agent_stance") or {}
        written = stance_written_at({"stance": stance})
        if written is not None:
            self._stance_left = written
        notify = getattr(self.runtime, "notify_reflex_fact", None)
        if callable(notify):
            notify({"call_id": str(record.get("call_id") or ""),
                    "activity_id": f"{record.get('call_id')}-stance-{uuid4().hex[:12]}",
                    "kind": "stance", "status": "stance_left", "stance": stance.get("text"),
                    "doing": str(context.get("reflex_action_text") or ""),
                    "said": ("While it protects the Body, the reflex layer reads that your stance no longer holds; "
                             "frames stop saying it and the protective choice is asked again now. Write a new "
                             "stance with update_life when you have one."),
                    "body": {key: facts[key] for key in ("position", "pos", "dimension", "health", "oxygen")
                             if key in facts}})
        wake.set()

    def _continues_protection(self, record: Mapping[str, Any], choice: str,
                              execution: Mapping[str, Any]) -> bool:
        """The running protective action chosen again, bound to the same
        target: carry on.  Fighting a different nearest mob is a switch."""
        context = record.get("supervision_context") or {}
        if context.get("is_reflex_action") and choice == "keep_fighting":
            return True  # the running fight, bound to its own target (rq.running_fight)
        if not context.get("is_reflex_action") or choice != context.get("reflex_action_choice"):
            return False
        action = self._default_action(choice, execution)
        target = (record.get("params") or {}).get("target")
        return action is None or target is None or action[1].get("target") == target

    async def _stopped_again_at_the_same_place(self, record: Mapping[str, Any]) -> bool:
        """Whether danger stopped this work again where it stopped it before
        the Body last went back.  Decision compares the two recorded places;
        a missing or invalid answer leaves the ordinary result question."""
        stops = [entry.get("body", {}).get("position") for entry in record.get("history", [])
                 if isinstance(entry, Mapping)
                 and ((((entry.get("result") or {}).get("metrics") or {}).get("direct_call_handoff") or {})
                      .get("facts") or {}).get("reason") == "decision_default_tool"]
        if len(stops) < 2:
            return False
        context = record["supervision_context"]
        request = rq.return_place_question(
            work_call=rq.call_text(record["tool"], record["params"]),
            intent=str(context.get("intent") or context.get("node_description") or ""),
            before=stops[-2], now=stops[-1])
        if request is None:
            return False
        self._with_latest_result(request)
        self.runtime.trace.emit(
            "call_supervision_request", tool_call_id=record["call_id"], tool=record["tool"],
            purpose="return_place_frame", loop_state=self.loop_state, request=request)
        raw = None
        try:
            answer = await asyncio.wait_for(
                self._client.ask(request, priority=supervision_module.DecisionPriority.SUPERVISION), timeout=2.2)
            raw = answer.raw
        except Exception:
            raw = None
        choice = rq.return_place_choice(raw)
        self.runtime.trace.emit(
            "call_supervision_result", tool_call_id=record["call_id"], tool=record["tool"],
            purpose="return_place_frame", response=raw, choice=choice or "unknown", unknown=choice is None)
        return choice == "same"

    async def _follow_default_result(
        self, record: dict[str, Any], action: tuple[str, dict[str, Any]],
        default_result: Any, default_id: str, *, protective_choice: str = "",
    ) -> str:
        """Ask what follows a protective action: go back, do it again, or the Agent."""
        if self._protective_verdict == "call_agent":
            return "call_agent"
        context = record["supervision_context"]
        if not context.get("idle_protection") and await self._stopped_again_at_the_same_place(record):
            # Going back into the same danger is a risk the Agent takes or not.
            return "call_agent"
        intent = str(context.get("intent") or context.get("node_description") or "")
        registry = getattr(self.runtime, "registry", None)
        default_tool = registry.get(action[0]) if registry is not None else None
        history: list[dict[str, Any]] = []
        total = 0
        new_attempt = True
        while default_tool is not None:
            self._refresh_interrupted_result(record)
            if await self._process_lifecycle_events() or self._world_connected is False:
                # Without a world every answer is unknown; asking again until
                # the deadline only keeps the work's slot.
                return "unknown"
            remaining = self._remaining(record)
            if remaining is not None and remaining <= 0:
                return "unknown"
            payload = default_result if isinstance(default_result, Mapping) else getattr(default_result, "result", default_result)
            if not isinstance(payload, Mapping):
                payload = {"success": False, "reason": str(payload)[:200]}
            if new_attempt and protective_choice:
                self._vitals.note_protection_result(protective_choice, payload.get("success") is True)
            facts = await self._read_idle_facts()
            execution = {"tool": action[0], "state": "succeeded" if payload.get("success") is True else "failed",
                         "facts": facts, "result": dict(payload)}
            if new_attempt:
                history.append({"result": self._compact_result(payload),
                                "body": {"position": facts.get("position", facts.get("pos")),
                                         "inventory": facts.get("inventory", facts.get("inventory_counts"))}})
                total += 1
                del history[:-5]
                new_attempt = False
            options = rq.after_default_options(
                original_call="" if context.get("idle_protection") else rq.call_text(record["tool"], record["params"]), intent=intent,
                default_call=rq.call_text(action[0], action[1]), result=payload,
                facts=execution["facts"], protective_choice=protective_choice,
            )
            try:
                choice = await asyncio.wait_for(self._judge_terminal_frame(
                    record["call_id"], default_tool, action[1], execution, 0.0,
                    purpose="default_result_frame", supervision_context=context, options=options,
                    decision_context={"tool_result": dict(payload), "tool_arguments": dict(action[1]),
                                      "body_state": execution["facts"], "history": list(history),
                                      "history_earlier": total - len(history),
                                      "interrupted_history": copy.deepcopy(record.get("history", [])),
                                      "interrupted_history_earlier": int(record.get("history_total", 0)) - len(record.get("history", []))},
                ), timeout=2.2 if remaining is None else min(2.2, remaining))
            except asyncio.TimeoutError:
                choice = "unknown"
            record["questions"] += 1
            if choice in {"resume", "stay", "call_agent"}:
                return choice
            candidates = rq.equipment_candidates(execution["facts"], payload)
            if choice in candidates:
                # A protective call is a Body call too. Reuse the existing
                # judged equipment node, then retry this protective call;
                # resuming the suspended work is still a later choice.
                choice = await self._run_result_equipment(
                    record, default_tool, action[1], candidates[choice], claim_holder="threat",
                    reflex_action_choice=protective_choice)
                if choice == "call_agent":
                    return choice
            # The option was compiled from these facts, so the chosen
            # protective action (the same one again or another) is bound to
            # them too. Re-running the ended call kept its old danger position
            # and distance: move_away answered already_safe 40 times in 18 s
            # while the zombie followed (step5-threat-claude7).
            rebound = (self._default_action(choice, execution)
                       if choice in options and rq.runs_default(choice) else None)
            if rebound is not None:
                protective_choice, action = choice, rebound
                default_tool = registry.get(action[0]) if registry is not None else None
                choice = "repeat"
            elif choice == protective_choice:
                choice = "repeat"
            if choice != "repeat":
                # A missing/illegal/late answer is no choice. Keep the
                # interrupted work pending, refresh facts at the next cadence,
                # and do not wake the Agent or add a fictitious tool attempt.
                record["unknown_answers"] = int(record.get("unknown_answers") or 0) + 1
                remaining = self._remaining(record)
                await asyncio.sleep(FACT_FRAME_INTERVAL_S if remaining is None else min(FACT_FRAME_INTERVAL_S, remaining))
                continue
            protective_choice, action, default_result, default_id = await self._execute_protection(
                protective_choice, action, record, call_id=f"{default_id}-{total}")
            default_tool = registry.get(action[0])
            if self._protective_verdict == "call_agent":
                return "call_agent"
            new_attempt = True
        return "unknown"


    def _set_state(self, state: str, cause: str) -> None:
        if self.loop_state == "dead" and cause != "respawned":
            return
        if state == self.loop_state:
            return
        previous = self.loop_state
        self.loop_state = state
        self.runtime.trace.emit(
            "reflex_state_changed", **{"from": previous, "to": state, "cause": cause}
        )

    def record_experience(self, text: str) -> None:
        """Keep the short reflex handoff history used by live Agent context."""
        value = str(text).strip()
        if value:
            self.recent_experiences.append(value[:500])

    async def decide_plan_result(
        self, *, call_id: str, tool: Any, params: Mapping[str, Any],
        plan_context: Mapping[str, Any], decision_context: Mapping[str, Any],
        next_call: str = "", repeat_note: str = "",
        question: Mapping[str, Any] | None = None,
        compile: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        deadline: float | None = None,
    ) -> str:
        """Judge a plan call's terminal fact through this Body's loop.

        WorkPlanExecutor supplies the terminal question, or ``compile`` to
        write it on the Body as it is now; the Body loop owns the Decision
        channel and never writes a different version. A protective choice
        runs here, the one consumer that can (pro plan P7 §9.3-§9.5), and
        the result is then judged again; the plan sees only the result
        choice. Unknown stays unknown and the plan asks again later.
        ``deadline`` (monotonic) is the plan's own end.
        """
        result = decision_context.get("tool_result")
        result = result if isinstance(result, Mapping) else {}
        facts = decision_context.get("body_state")
        facts = facts if isinstance(facts, Mapping) else {}
        if await self._process_lifecycle_events(facts):
            return "unknown"
        execution = decision_context.get("execution")
        execution = dict(execution) if isinstance(execution, Mapping) else {}
        execution.setdefault("tool", getattr(tool, "name", "unknown"))
        execution["state"] = "succeeded" if result.get("success") is True else "failed"
        execution["facts"] = dict(facts)
        # The plan's transport id holds the result slot through protection.
        record = {"call_id": f"plan-{call_id}", "tool": str(getattr(tool, "name", "unknown")),
                  "params": dict(params), "supervision_context": dict(plan_context),
                  "questions": 0, "history": [], "history_total": 0, "started_at": time.monotonic(),
                  **({"timeout_s": max(0.0, deadline - time.monotonic())} if deadline is not None else {})}
        try:
            while True:
                if compile is not None or question is None:
                    # The plan's read has no entities; a fresh read does.
                    execution["facts"] = self._frame({"facts": {**facts, **await self._body_now()}})["facts"]
                options = None
                if compile is not None:
                    question = compile(execution["facts"])
                    sent = (question.get("questions") or {}) if isinstance(question, Mapping) else {}
                    options = {key: text for name in ("q", "p") for key, text in
                               ((sent.get(name) or {}).get("criteria") or {}).items()}
                elif question is None:
                    options = rq.frame_options(
                        tool=str(getattr(tool, "name", "unknown")), params=params,
                        intent=str(plan_context.get("intent") or ""), facts=execution["facts"],
                        running=False, result=result, next_call=next_call,
                        repeat_note=repeat_note,
                        unchanged=rq.body_unchanged_by_call(execution["facts"], decision_context.get("body_before")),
                    )
                    options = {key: value for key, value in options.items() if key != "inspect"}
                choice = await self._judge_terminal_frame(
                    call_id, tool, params, execution, 0.0,
                    purpose="plan_result_frame", supervision_context=plan_context,
                    options=None if compile is not None else options, decision_context=decision_context,
                    question=question,
                )
                if not rq.runs_default(choice):
                    return choice
                action = self._default_action(choice, execution) if options and choice in options else None
                if action is None:
                    return "unknown"
                after = await self._protect_result(record, execution, choice, action)
                if isinstance(decision_context, dict) and "result_protection" in record:
                    # The Agent hears of it with the node, if the node goes back to it.
                    decision_context["protection"] = copy.deepcopy(record["result_protection"])
                if after != "judge_again":
                    return "call_agent" if after == "call_agent" else "unknown"
        finally:
            self._release_result_pending(str(call_id))

    def _release_result_pending(self, call_id: str) -> None:
        # Plan calls enter the shared runtime with the transport id
        # ``plan-{node_id}``, while the result judge is deliberately passed
        # the tree's node id.  Release both spellings so a judged plan result
        # cannot leave the lifecycle loop permanently waiting for a slot.
        value = str(call_id)
        keys = {value, f"plan-{value}"}
        if value.startswith("plan-"):
            keys.add(value[5:])
        for key in keys:
            self._result_pending.pop(key, None)
        self._loop_wakeup.set()

    async def start(self) -> None:
        """Start the one lifecycle task when the SDK session enters the world."""
        if self._lifecycle_started or self._world_connected is False:
            return
        self._lifecycle_started = True
        loop = asyncio.get_running_loop()
        self._reaction_event_loop = loop
        body = getattr(self.runtime, "body", None)
        register = getattr(body, "add_lifecycle_listener", None)
        if callable(register) and self._body_lifecycle_listener is None:
            def receive(event):
                if not loop.is_closed():
                    loop.call_soon_threadsafe(self._queue_lifecycle_event, event)
            self._body_lifecycle_listener = receive
            self._native_lifecycle = True
            register(receive)
        self.runtime.trace.emit("reflex_loop_started", loop_state=self.loop_state)
        self._lifecycle_task = asyncio.create_task(self._run_lifecycle())
        self._lifecycle_task.add_done_callback(self._restart_after_world_ready)

    async def _run_lifecycle(self) -> None:
        """Keep the one lifecycle consumer alive until ``stop`` ends it.

        An error that left ``_idle_frames`` used to end the task silently while
        ``_lifecycle_started`` stayed set: nothing asked another frame, no
        successor started, and every later call that needed a monitor failed
        with "BodyLoop already has an active monitor" (69 failures in two hours
        of the 2026-10-16 DeepSeek run). The layer reports the failure and goes
        on, waiting longer after each failure that follows closely on the last.
        """
        delay = LIFECYCLE_RESTART_DELAY_S
        while True:
            started = time.monotonic()
            try:
                return await self._idle_frames()
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if (task is not None and task.cancelling()) or not self._lifecycle_started:
                    raise
                error_type, message = "CancelledError", "raised by a child task, not a stop request"
            except Exception as exc:
                error_type, message = type(exc).__name__, str(exc)[:200]
            self.runtime.trace.emit(
                "reflex_loop_failed", loop_state=self.loop_state,
                error_type=error_type, message=message, restart_in_s=delay,
            )
            if time.monotonic() - started > 60.0:
                delay = LIFECYCLE_RESTART_DELAY_S
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, LIFECYCLE_RESTART_MAX_DELAY_S)
            if not self._lifecycle_started:
                return

    def _look_for_world_soon(self) -> None:
        if self._world_look_task is None or self._world_look_task.done():
            self._world_look_task = asyncio.create_task(self._look_for_world())

    async def _look_for_world(self) -> None:
        """While the client is out of the world, read it now and then.

        The client's IPC reconnects on a read, and only then replays
        world_ready. With the loop stopped nothing read it: in H6's second
        staged kick (2026-10-29) the client was back at 13:52:35 and the
        session heard it at 13:53:56, when a heartbeat happened to read the
        Body (s7-later-20261029.md §8.15). Ends with world_ready or a stop.
        """
        body = getattr(self.runtime, "body", None)
        read = getattr(self.runtime, "run_read", None)
        if body is None or not callable(read):
            return
        while self._world_connected is False:
            await asyncio.sleep(WORLD_LOOK_INTERVAL_S)
            if self._world_connected is not False:
                return
            try:
                await read(body.get_state)
            except Exception:
                pass  # still gone; the next look tries again

    def _restart_after_world_ready(self, task: asyncio.Task[Any]) -> None:
        # A rapid reconnect may arrive while the old consumer is settling.
        # Start its successor only after that consumer has finished.
        if (self._body_lifecycle_listener is not None and self._world_connected is True
                and not self._lifecycle_started):
            asyncio.create_task(self.start())

    def _ensure_idle_task(self) -> None:
        # The lifecycle task never ends after a call; it switches back to its
        # idle branch once the registered monitor signals completion.
        if self._lifecycle_started and self._lifecycle_task is not None:
            self._loop_wakeup.set()

    async def stop(self, *, keep_world_listener: bool = False, reason: str = "session_closed") -> None:
        """Stop the lifecycle task before the runtime closes its read lane."""
        task = self._lifecycle_task
        remove = getattr(getattr(self.runtime, "body", None), "remove_lifecycle_listener", None)
        if not keep_world_listener and callable(remove) and self._body_lifecycle_listener is not None:
            remove(self._body_lifecycle_listener)
        if not keep_world_listener:
            self._body_lifecycle_listener = None
            self._world_connected = None
        # Keep the old task reference until it exits: a reconnect must not
        # create another consumer while this one is still settling.
        if task is not asyncio.current_task():
            self._lifecycle_task = None
        active = self._active_monitor
        self._active_monitor = None
        self._result_pending.clear()
        while self._terminal_questions:
            answer, _, _ = self._terminal_questions.popleft()
            answer.cancel()
        if active is not None:
            active[0]["cancel_requested"] = True
            decision = active[0].get("decision_task")
            if isinstance(decision, asyncio.Task) and not decision.done():
                decision.cancel()
                await asyncio.gather(decision, return_exceptions=True)
            done = active[0].get("monitor_done")
            if isinstance(done, asyncio.Event):
                done.set()
        self._loop_wakeup.set()
        protection = self._protection_task
        self._protection_task = None
        if protection is not None and not protection.done():
            protection.cancel()
            await asyncio.gather(protection, return_exceptions=True)
        result_protections = [task for task in self._result_protections if not task.done()]
        for task in result_protections:
            task.cancel()
        await asyncio.gather(*result_protections, return_exceptions=True)
        self._suspended = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._lifecycle_started:
            self.runtime.trace.emit("reflex_loop_stopped", loop_state=self.loop_state, reason=reason)
        self._lifecycle_started = False
        self._ambient_facts.clear()
        self._creeper_memory.clear()
        self._vitals = rq.VitalWindow()

    def _idle_facts(self) -> dict[str, Any]:
        cached = getattr(self.runtime, "last_known_body_state", None)
        if isinstance(cached, Mapping):
            return dict(cached)
        return {}

    async def _read_body_facts(self, *, fresh_only: bool = False) -> dict[str, Any]:
        """Read raw state and perception on the same lane in work and idle.

        ``fresh_only`` returns nothing instead of the cached state when the
        state read fails.
        """
        body = getattr(self.runtime, "body", None)
        getter = getattr(body, "get_reflex_facts", None) or getattr(body, "get_state", None)
        read_lane = getattr(self.runtime, "_read_lane", None)
        facts = {} if fresh_only else self._idle_facts()
        if fresh_only and not (callable(getter) and read_lane is not None):
            return {}
        if callable(getter) and read_lane is not None:
            try:
                value = await read_lane.run(getter)
                if isinstance(value, Mapping):
                    facts = dict(value)
                elif is_dataclass(value):
                    facts = asdict(value)
                else:
                    raw = getattr(value, "__dict__", None)
                    if isinstance(raw, dict):
                        facts = dict(raw)
            except Exception as exc:
                self.runtime.trace.emit(
                    "reflex_idle_read_unknown", scope="state", error_type=type(exc).__name__
                )
                if fresh_only:
                    return {}
            # BodyState intentionally projects state, not nearby entities.
            # Use the existing perception API rather than adding BodyState or
            # tool-result fields. An entity read gap must not discard a state
            # read that already succeeded.
            perceive = getattr(body, "perceive", None)
            if callable(perceive) and not any(
                    isinstance(facts.get(key), (list, tuple))
                    for key in ("entities", "nearby_entities", "hostiles", "nearby_hostiles")):
                try:
                    nearby = await read_lane.run(perceive, "nearbyEntities", {"radius": 64, "limit": 32})
                    ok = nearby.get("ok") if isinstance(nearby, Mapping) else getattr(nearby, "ok", False)
                    data = nearby.get("data") if isinstance(nearby, Mapping) else getattr(nearby, "data", None)
                    if ok is True and isinstance(data, Mapping) and isinstance(data.get("entities"), list):
                        facts["nearby_entities"] = list(data["entities"])
                    else:
                        self.runtime.trace.emit("reflex_idle_read_unknown", scope="nearbyEntities")
                except Exception as exc:
                    self.runtime.trace.emit(
                        "reflex_idle_read_unknown", scope="nearbyEntities", error_type=type(exc).__name__
                    )
        used = rq.inventory_slots_used(facts)
        if used is not None:
            # The count, not the 46 slot facts: running frames carry it on every refresh.
            facts["inventory_slots_used"] = used
        profile = self.current_life_profile()
        if profile:
            facts["life_profile"] = copy.deepcopy(profile)
            facts["life_intent"] = life_intent(profile)
            self._add_item_rules(facts)
        self._note_daylight(facts)
        return facts

    def _note_daylight(self, facts: Mapping[str, Any]) -> None:
        """A player sees the sun set; the Agent hears it once at each sunset (easy-run-20261106.md).

        Only a day-to-dusk (or day-to-night) change in the Overworld is told:
        a join or respawn at night, the Nether and the End say nothing here.
        """
        dimension = str(facts.get("dimension") or "")
        if dimension and not dimension.endswith("overworld"):
            self._daylight = None
            return
        clock = facts.get("time")
        phase = rq.daylight_phase(clock)
        if phase is None:
            return
        before, self._daylight = self._daylight, phase
        if phase == "day" and before in {"dusk", "night"}:
            # A player sees the sun come up too: without it an Agent sealed in
            # for the night slept through a whole day (easy run-11, 2026-11-09).
            self._dawn_count += 1
            self._tell_daylight(facts, clock, before, phase, rq.dawn_notice(clock), "dawn", self._dawn_count)
            return
        if before != "day" or phase == "day":
            return
        self._dusk_count += 1
        self._tell_daylight(facts, clock, before, phase, rq.dusk_notice(clock), "dusk", self._dusk_count)

    def _tell_daylight(self, facts: Mapping[str, Any], clock: Any, before: str | None, phase: str,
                       message: str | None, name: str, seq: int) -> None:
        position = facts.get("position") or facts.get("pos")
        data = {"message": message, "time_of_day": int(clock % 24000), "daylight": phase,
                "native_epoch": f"{name}-{uuid4().hex[:8]}"}
        if isinstance(position, (list, tuple)) and len(position) == 3:
            data["position"] = [round(float(v), 1) for v in position]
        self.runtime.trace.emit("daylight_changed", before=before, after=phase, time_of_day=data["time_of_day"])
        notify = getattr(self.runtime, "notify_body_lifecycle_event", None)
        if callable(notify):
            notify(Event(seq=seq, tick=0, bot="", name=name, data=data))

    async def _read_idle_facts(self) -> dict[str, Any]:
        facts = self._frame({"facts": await self._read_body_facts()})["facts"]
        arrival = await self._home_arrival(facts)
        if arrival is not None:
            facts["home_arrival"] = arrival
            self._tell_agent_if_home_blocked(arrival)
        return facts

    async def _home_arrival(self, facts: Mapping[str, Any]) -> list[dict[str, Any]] | None:
        """Read whether the Body can stand in each home cell (pro plan P5)."""
        candidates = rq.home_candidates(facts)
        # Home cells read in another dimension would be other blocks at the same numbers.
        if not candidates or rq.at_home(facts) or not rq.home_here(facts):
            return None
        body = getattr(self.runtime, "body", None)
        perceive = getattr(body, "perceive", None)
        read_lane = getattr(self.runtime, "_read_lane", None)
        if not callable(perceive) or read_lane is None:
            return None
        cells = [[x, y + dy, z] for x, y, z in candidates for dy in (0, 1, -1)]
        found = None
        try:
            read = await read_lane.run(perceive, "blockCells", {"cells": cells})
            ok = read.get("ok") if isinstance(read, Mapping) else getattr(read, "ok", False)
            data = read.get("data") if isinstance(read, Mapping) else getattr(read, "data", None)
            if ok is True and isinstance(data, Mapping) and isinstance(data.get("cells"), list):
                found = data["cells"]
            else:
                self.runtime.trace.emit("reflex_idle_read_unknown", scope="blockCells")
        except Exception as exc:
            self.runtime.trace.emit("reflex_idle_read_unknown", scope="blockCells", error_type=type(exc).__name__)
        return rq.home_arrival(candidates, found)

    def _tell_agent_if_home_blocked(self, arrival: list[dict[str, Any]]) -> None:
        blocked = all(item.get("standable") is False for item in arrival)
        key = json.dumps(arrival, sort_keys=True) if blocked else None
        if key == self._home_blocked_notice:
            return
        self._home_blocked_notice = key
        notify = getattr(self.runtime, "notify_reflex_fact", None)
        if key is None or not callable(notify):
            return
        notify({"call_id": "idle-loop", "activity_id": f"home-{uuid4().hex[:12]}", "kind": "life",
                "status": "home_blocked", "reason": "home_arrival_blocked",
                "facts": {"home_arrival": arrival, "summary": rq.home_blocked_sentence(arrival)}})

    # What a fresh Body read adds to a call's own execution facts. The action
    # observer owns its navigation, phase and movement; a read supplies what
    # lite progress lacks. Explicit complete inventory stays complete.
    _AMBIENT_KEYS = ("inventory", "inventory_counts", "inventory_complete", "inventory_slots_used",
        "inventory_includes_selected", "selected_item", "selected_item_known",
        "entities", "nearby_entities", "hostiles", "nearby_hostiles",
        "health", "food", "oxygen", "head_in_liquid", "on_ground", "in_lava", "on_fire", "lava_exit", "standing_on", "cloud", "steps", "exposed_ores", "fall", "water_source_in_reach", "hurt_here", "last_shot")

    async def _refresh_running_facts(self, call_id: str) -> None:
        body = getattr(self.runtime, "body", None)
        getter = getattr(body, "get_reflex_facts", None) or getattr(body, "get_state", None)
        snapshots = getattr(self.runtime, "execution_snapshots", None)
        observe = getattr(snapshots, "observe", None)
        if (callable(getter) and getattr(self.runtime, "_read_lane", None) is not None
                and callable(observe)):
            # observe already rejects late reads after a terminal or a change
            # of call ownership. Do not write enriched history back as raw.
            facts = await self._read_body_facts(fresh_only=True)
            # Only a read that succeeded now may say a place fact is over;
            # the idle cache is older than the action's own samples.
            fresh = bool(facts)
            if not fresh:
                facts = self._idle_facts()
            # Not a second execution progress stream.
            observe(call_id, {"lite": True, **({"body_read": True} if fresh else {}),
                              **{key: facts[key] for key in self._AMBIENT_KEYS if key in facts}})

    async def _body_now(self) -> dict[str, Any]:
        """The ambient facts of a fresh read; empty when the state read failed.

        A result is judged on the Body as it is when the question is written
        (pro plan P7 §9.3): the last running frame may be a second old, and a
        call shorter than the grace window never read an entity at all.
        """
        facts = await self._read_body_facts(fresh_only=True)
        return {key: facts[key] for key in self._AMBIENT_KEYS if key in facts}

    @staticmethod
    def _idle_options(facts: Mapping[str, Any]) -> dict[str, str]:
        return rq.idle_options(facts)

    def _idle_question(self, facts: Mapping[str, Any], options: Mapping[str, str]) -> dict[str, Any]:
        # An idle frame minutes later must not repeat an old result as news.
        return rq.idle_question(facts, options)

    async def _run_idle_protection(self, choice: str, facts: Mapping[str, Any]) -> None:
        """Execute one reversible idle protection action through run_tool."""
        action = self._default_action(choice, {"facts": dict(facts)})
        if action is None:
            return
        default_tool = self.runtime.registry.get(action[0])
        timeout = action[1].get("timeout_s", getattr(getattr(default_tool, "sidecar", None), "timeout_s", None))
        record = {"call_id": f"reflex-idle-{self._idle_sequence}", "tool": action[0], "params": dict(action[1]),
                  "started_at": time.monotonic(), "timeout_s": timeout, "questions": 0,
                  "supervision_context": {"goal": "", "idle_protection": True, "is_reflex_action": True,
                      "intent": supervision_module._DEFAULT_ACTION_TEXT.get(choice, choice)}}
        self.runtime.trace.emit(
            "call_supervision_default_requested", tool_call_id="idle-loop",
            choice=choice, tool=action[0], arguments=action[1], loop_state=self.loop_state,
        )
        try:
            choice, action, result, call_id = await self._execute_protection(
                choice, action, record, call_id=record["call_id"])
            after = "call_agent" if self._protective_verdict == "call_agent" else "unknown"
            if after != "call_agent":
                remaining = self._remaining(record)
                if remaining is None or remaining > 0:
                    try:
                        after = await asyncio.wait_for(
                            self._after_default_action(record, action, result, call_id, protective_choice=choice),
                            timeout=remaining)
                    except asyncio.TimeoutError:
                        after = "unknown"
                choice, action = record.get("protective_action", (choice, action))
                result = record.get("protective_result", result)
            if after == "call_agent":
                notify = getattr(self.runtime, "notify_reflex_fact", None)
                if callable(notify):
                    notify({"call_id": call_id, "activity_id": f"{call_id}-{uuid4().hex[:12]}",
                            "status": "handoff_requested",
                            "reason": "decision_call_agent", "result": self._compact_result(result)})
            else:
                self._remember_protection(action, result)
            self.runtime.trace.emit("call_supervision_default_finished", tool_call_id="idle-loop",
                                    choice=choice, tool=action[0], result=self._compact_result(result))
        finally:
            self._set_state("idle", "call_ended")
            self._loop_wakeup.set()

    @staticmethod
    def _default_action(choice: str, execution: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        return CallSupervisor._default_action(choice, execution)

    def _start_project(self, project_id: str, facts: Mapping[str, Any]) -> None:
        """Hand the chosen project's tree to the plan executor as idle life."""
        profile = facts.get("life_profile") if isinstance(facts.get("life_profile"), Mapping) else {}
        project = next((item for item in profile.get("projects") or []
                        if isinstance(item, Mapping) and item.get("id") == project_id), None)
        manager = getattr(self.runtime, "reflex_manager", None)
        if project is None or manager is None:
            return
        # The manager learns its loop from the Agent's first turn; idle life may
        # come first (a session the Agent has not spoken in: life-stage c2).
        manager.bind_loop(asyncio.get_running_loop())
        if manager.start_project(dict(project)) is None:
            self.runtime.trace.emit("life_project_not_started", project=project_id, reason="activity_running")

    async def _idle_frames(self) -> None:
        """Produce idle facts at the same cadence as running frames.

        Unknown answers remain inert.  A selected protective action is the
        only write this step executes while idle, and it uses the existing
        ``run_tool`` admission and settlement path.
        """
        try:
            while self._lifecycle_started:
                await self._process_lifecycle_events()
                if not self._lifecycle_started:
                    return
                active = self._active_monitor
                if active is not None:
                    self._unpicked = None  # the Body is working, not idle
                    record, tool, params, wake, worker = active
                    try:
                        await self._monitor(record, tool, params, wake, worker)
                    finally:
                        if self._active_monitor is active:
                            self._active_monitor = None
                        if not record.get("result_abandoned"):
                            self._result_pending[str(record.get("call_id") or "")] = record
                        done = record.get("monitor_done")
                        if isinstance(done, asyncio.Event):
                            done.set()
                    continue

                if self._terminal_questions:
                    answer, args, kwargs = self._terminal_questions.popleft()
                    await self._serve_terminal_question(answer, args, kwargs)
                    continue

                self._loop_wakeup.clear()
                try:
                    await asyncio.wait_for(self._loop_wakeup.wait(), timeout=(
                        FALL_FRAME_INTERVAL_S if self._idle_airborne else FACT_FRAME_INTERVAL_S))
                    continue
                except asyncio.TimeoutError:
                    pass
                if self._active_monitor is not None:
                    continue
                if self._result_pending or (self._protection_task is not None and not self._protection_task.done()):
                    # The worker has ended, but the Agent/plan result judge is
                    # still using the Body facts.  Do not start idle work or
                    # ask an idle Decision question until it releases this
                    # result slot.
                    continue
                snapshots = getattr(self.runtime, "execution_snapshots", None)
                if snapshots is not None and snapshots.current() is not None:
                    self._unpicked = None
                    continue
                manager = getattr(self.runtime, "reflex_manager", None)
                if manager is not None and manager.life_project_running() is not None:
                    # The project's own frames supervise the Body between its calls.
                    self._unpicked = None
                    continue
                facts = await self._read_idle_facts()
                self._idle_airborne = facts.get("on_ground") is False
                if await self._process_lifecycle_events(facts):
                    continue
                # An Agent call can arrive while the read lane is sampling.
                if self._active_monitor is not None or self._result_pending:
                    continue
                options = self._without_failed_repeat(facts, self._idle_options(facts))
                request = self._idle_question(facts, options)
                self._idle_sequence += 1
                started = time.monotonic()
                reaction = self._reaction_question(facts, started)
                self.runtime.trace.emit(
                    "call_supervision_request", tool_call_id="idle-loop",
                    tool="__idle__", purpose="idle_frame", loop_state="idle",
                    fact_revision=self._idle_sequence, request=request,
                    request_chars=len(str(request)),
                    frame_input=getattr(self, "_last_frame_input", {"execution": {"facts": dict(facts)}}),
                    compiler_input={"facts": dict(facts), "options": dict(options)},
                    clock={"request_monotonic_s": started},
                )
                answer_task = wake_task = None
                try:
                    # Idle is a live state, so still ask even when the only
                    # reversible normal option is ``stay``.  This keeps the
                    # same one-question-per-frame contract as a running call.
                    answer_task = asyncio.create_task(self._client.ask(request))
                    wake_task = asyncio.create_task(self._loop_wakeup.wait())
                    done, _ = await asyncio.wait(
                        {answer_task, wake_task}, return_when=asyncio.FIRST_COMPLETED,
                    )
                    if wake_task in done:
                        # Even a completed answer belongs to the superseded
                        # idle frame when work arrived in the same wait cycle.
                        choice = "unknown"
                    else:
                        answer = await answer_task
                        choice = rq.frame_choice(answer.raw, request["questions"])
                    if wake_task not in done:
                        wake_task.cancel()
                    await asyncio.gather(wake_task, return_exceptions=True)
                except Exception:
                    choice = "unknown"
                finally:
                    children = [child for child in (answer_task, wake_task) if child is not None]
                    for child in children:
                        if not child.done():
                            child.cancel()
                    await asyncio.gather(*children, return_exceptions=True)
                self.runtime.trace.emit(
                    "call_supervision_result", tool_call_id="idle-loop",
                    tool="__idle__", purpose="idle_frame", choice=choice,
                    unknown=choice == "unknown",
                    decision_ms=round((time.monotonic() - started) * 1000.0, 3),
                )
                self._reaction_answer(reaction, choice)
                self._check_none_started(facts, options, choice)
                if rq.runs_default(choice):
                    self._protection_task = asyncio.create_task(self._run_idle_protection(choice, facts))
                elif choice in {"go_home", "approach_player", "follow_player", "look_player", "seek_player"}:
                    self._protection_task = asyncio.create_task(self._run_idle_action(choice, facts))
                elif choice.startswith("project_"):
                    self._start_project(choice.removeprefix("project_"), facts)
                elif choice == "call_agent":
                    notify = getattr(self.runtime, "notify_reflex_fact", None)
                    if callable(notify):
                        notify({"call_id": "idle-loop", "activity_id": f"idle-{uuid4().hex[:12]}",
                                "kind": "life", "status": "handoff_requested",
                                "reason": "decision_call_agent", "facts": dict(facts)})
        except asyncio.CancelledError:
            raise


__all__ = ["BodyLoop"]
