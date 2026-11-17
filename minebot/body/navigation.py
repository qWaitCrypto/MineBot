"""Body transaction navigation runtime."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import dist, floor, hypot
from time import monotonic, sleep
from typing import Callable

from minebot.contract import action_with_timeout, action_wait_timeout_s
from minebot.body.combat import entity_target_terminal_mismatch, resolve_entity_target
from minebot.body.reach import cell_is_clear, cell_is_solid_support
from minebot.body.world_read import read_block_facts
from minebot.body.threat_reach import DODGE_MARGIN, DODGE_STEP_REACH, DODGE_TIMEOUT_S, attack_reach
from minebot.contract import (
    Action,
    Body,
    BodyState,
    BreakContext,
    Event,
    InteractionContext,
    InventorySlot,
    LocalProgressController,
    PlaceContext,
    Position,
    ProgressController,
    ToolResult,
    body_rejection_to_tool_result,
    perception_next_cursor,
)
from minebot.game.navigation import (
    GoalBlock,
    GoalComposite,
    GoalLike,
    GoalNear,
    normalize_goal,
)


SERVER_GOAL_SET_LIMIT = 32
RCON_MUTATION_COMMAND_TARGET_CHARS = 1400
SURVIVAL_REFLEX_OWNERS = frozenset({"lavaReflex", "fireReflex", "waterReflex", "combatReflex"})


@dataclass(frozen=True)
class NavigationRunConfig:
    max_segments: int = 32
    max_partial_segments: int | None = None
    segment_timeout_s: float = 15.0
    server_grid_radius: int = 64
    server_max_expand: int = 2500
    server_no_progress_ticks: int = 120
    recheck_lookahead: int = 5
    min_partial_progress: int = 5
    allow_diagonal: bool = True
    allow_ascend: bool = True
    allow_descend: bool = True
    allow_swim: bool = True
    aquatic_traversal: bool = False
    # Set only for a Body-owned escape transaction.  Scarpet uses this marker
    # to keep an unresolved survival hazard from accepting ordinary actions.
    survival_recovery: bool = False
    aquatic_replan_attempts: int = 2
    max_safe_fall_depth: int = 3
    max_water_drop_depth: int = 32
    allow_break: bool = True
    max_break_steps: int = 8
    allow_place: bool = True
    max_place_steps: int = 8
    allow_pillar: bool = True
    max_pillar_steps: int = 8
    allow_downward: bool = True
    max_downward_steps: int = 8
    allow_open: bool = True
    max_open_steps: int = 8
    scaffold_blocks: tuple[str, ...] = (
        "cobblestone",
        "cobbled_deepslate",
        "deepslate",
        "stone",
        "dirt",
        "netherrack",
    )
    recovery_attempts: int = 2
    recovery_detour_distances: tuple[int, ...] = (1,)
    recovery_detour_offsets: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))
    recovery_detour_y_offsets: tuple[int, ...] = (0, 1, -1)
    movement_arrival_radius: float | None = None
    # Production FakePlayer navigation is load-limited by default.  A caller
    # may disable this only for a bounded diagnostic/baseline probe; ordinary
    # Body consumers must not silently reintroduce the 2,500-node burst.
    load_limited: bool = True


def pure_movement_navigation_config(
    config: NavigationRunConfig | None = None,
) -> NavigationRunConfig:
    """Return a movement profile that cannot alter terrain geometry."""

    return replace(
        config or NavigationRunConfig(),
        allow_break=False,
        max_break_steps=0,
        allow_place=False,
        max_place_steps=0,
        allow_pillar=False,
        max_pillar_steps=0,
        allow_downward=False,
        max_downward_steps=0,
    )


def dry_land_navigation_config(
    config: NavigationRunConfig | None = None,
) -> NavigationRunConfig:
    """Keep a normal land objective from treating a swim route as its default."""

    return replace(config or NavigationRunConfig(), allow_swim=False)


def governed_mobility_navigation_config(
    config: NavigationRunConfig | None = None,
) -> NavigationRunConfig:
    """Use the provider's governed movement/mutation operator union.

    This is an escalation profile for a dry-first objective.  It does not
    alter mutation permissions or budgets; it only makes the already-governed
    aquatic traversal operators visible to the same server-side search.
    """

    return replace(
        config or NavigationRunConfig(),
        allow_swim=True,
        aquatic_traversal=True,
    )


def load_limited_navigation_config(
    config: NavigationRunConfig | None = None,
) -> NavigationRunConfig:
    """Bound server-side search bursts for resource-domain long approaches.

    Q4t load evidence showed that a 2,500-node Scarpet burst can stall the
    server tick when a resource approach is far from its selected stand.  This
    profile keeps the normal operator union and governance intact, but caps
    the per-search work and the number of partial re-plans.  A caller may pass
    a stricter configuration; the profile only clamps more permissive values.
    """

    base = config or NavigationRunConfig()
    max_segments = min(base.max_segments, 16)
    partial_segments = base.max_partial_segments
    if partial_segments is None:
        partial_segments = max_segments
    return replace(
        base,
        max_segments=max_segments,
        max_partial_segments=min(partial_segments, 8),
        server_grid_radius=min(base.server_grid_radius, 48),
        server_max_expand=min(base.server_max_expand, 1200),
    )


def server_load_limited_navigation_config(
    config: NavigationRunConfig | None = None,
) -> NavigationRunConfig:
    """Cap one Scarpet search burst without changing route retry semantics.

    The full ``load_limited_navigation_config`` profile is appropriate for
    bounded resource-domain transactions, where the total number of partial
    replans is part of the measured load envelope.  The provider-wide default
    must still preserve each consumer's route/retry budget; otherwise a load
    fix silently turns a long but progressing route into an early terminal.
    """

    base = config or NavigationRunConfig()
    return replace(
        base,
        server_grid_radius=min(base.server_grid_radius, 48),
        server_max_expand=min(base.server_max_expand, 1200),
    )


def _compact_navigate_action_params(params: dict[str, object]) -> dict[str, object]:
    """Drop Scarpet-default or inactive fields from a navigateTo action payload."""

    compact = dict(params)
    defaults: dict[str, object] = {
        "y_below": 8,
        "y_above": 8,
        "arrival_radius": 0.75,
        "goal_radius": 0,
        "min_partial_progress": 5,
        "segment_index": 0,
        "allow_diagonal": True,
        "allow_ascend": True,
        "allow_descend": True,
        "allow_swim": True,
        "aquatic_traversal": False,
        "survival_recovery": False,
        "aquatic_replan_attempts": 0,
        "max_fall_depth": 3,
        "max_water_drop_depth": 32,
        "recheck_lookahead": 5,
        "allow_break": False,
        "break_budget": 0,
        "break_timeout_ticks": 300,
        "allow_place": False,
        "allow_pillar": False,
        "pillar_budget": 0,
        "allow_downward": False,
        "downward_budget": 0,
        "allow_open": False,
        "open_budget": 0,
        "scaffold_count": 0,
        "place_budget": 0,
    }
    for key, default in defaults.items():
        if compact.get(key) == default:
            compact.pop(key, None)

    if compact.get("partial_replans") in (None, 0):
        compact.pop("partial_replans", None)
    if not compact.get("allow_break"):
        compact.pop("break_budget", None)
    if not (compact.get("allow_break") or compact.get("allow_downward")):
        for key in ("break_timeout_ticks", "break_pickaxe", "break_axe", "break_shovel"):
            compact.pop(key, None)
    if not (compact.get("allow_place") or compact.get("allow_pillar")):
        for key in ("scaffold_item", "scaffold_count", "place_budget", "pillar_budget"):
            compact.pop(key, None)
    if not compact.get("allow_place"):
        compact.pop("place_budget", None)
    if not compact.get("allow_pillar"):
        compact.pop("pillar_budget", None)
    if not compact.get("allow_downward"):
        compact.pop("downward_budget", None)
    if not compact.get("allow_open"):
        compact.pop("open_budget", None)
    if compact.get("denied_mutations") == []:
        compact.pop("denied_mutations", None)
    for key in ("break_pickaxe", "break_axe", "break_shovel", "scaffold_item"):
        if compact.get(key) is None:
            compact.pop(key, None)

    return compact


def aquatic_navigation_config(
    config: NavigationRunConfig | None = None,
) -> NavigationRunConfig:
    """Permit one explicit water crossing without permitting terrain mutation."""

    return replace(
        pure_movement_navigation_config(config),
        allow_swim=True,
        aquatic_traversal=True,
    )


def navigation_zero_progress(
    result: ToolResult,
    before: Position,
    after: Position,
) -> bool:
    """Return whether a failed route has authoritative zero-progress evidence.

    Dry-first consumers may widen their movement profile only when the failed
    route produced neither displacement nor a usable path. Missing progress
    evidence is deliberately not treated as zero progress.
    """

    if _position_distance(before, after) > 0.25:
        return False
    observed, positive = _navigation_progress_evidence(result, before)
    return observed and not positive


def navigation_governed_mobility_upgrade_reason(reason: object) -> bool:
    """Return whether a dry-first zero-progress failure may widen mobility.

    Navigation recovery wraps the underlying route terminal as
    ``recovery_exhausted:<terminal>``. Dry-first consumers must classify that by
    reason family, not by the literal outer string; otherwise a genuine no-path
    route can bypass the governed swim/place/break/pillar escalation that exists
    precisely for compound terrain.
    """

    value = str(reason or "")
    if value in {
        "no_path",
        "budget_exceeded",
        "node_budget_without_progress",
        "unloaded_boundary_without_progress",
        "no_path_incomplete_coverage_without_progress",
        # Java objective slices carry their own bounded timeout. A caller may
        # widen once only after ``navigation_zero_progress`` has independently
        # proved this terminal made no physical/path progress.
        "timeout_ticks_exhausted",
    }:
        return True
    if value.startswith(("recovery_exhausted:", "replan_budget_exhausted:")):
        return navigation_governed_mobility_upgrade_reason(value.split(":", 1)[1])
    return False


def navigation_surface_egress_fallback_allowed(reason: object) -> bool:
    """Return whether a dry egress terminal may enter the governed profile.

    ``egress_to_dry`` deliberately tries a pure aquatic route first.  A lack
    of a nearby dry stand is therefore a recoverable capability handoff, not a
    terminal resource or exploration failure.  Keep this classification next
    to the navigation profile predicates so consumers cannot invent their own
    reason-family rules.
    """

    value = str(reason or "")
    if value in {"dry_egress_unavailable", "dry_egress_not_liquid"}:
        return True
    if value.startswith("dry_egress_failed:"):
        inner = value.split(":", 1)[1]
        if inner in {"stuck", "replan_budget_exhausted"}:
            # Surface recovery is a bounded provider handoff.  A follower
            # that gets stuck while leaving a damaged/floating column may
            # retry the already-governed surface domain with swim/mutation
            # operators; resource candidate navigation must still blacklist
            # a failed target rather than silently retry it under a new mode.
            return True
        return navigation_governed_mobility_upgrade_reason(inner)
    return False


def navigation_governed_mobility_upgrade_allowed(
    result: ToolResult,
    before: Position,
    after: Position,
) -> bool:
    """Decide whether a dry-first terminal may enter governed mobility.

    A zero-progress terminal is the original escalation case.  A route that
    actually advanced toward its goal before ending in ``no_path`` or
    ``budget_exceeded`` is also eligible, but only when both pieces of evidence
    agree: the authoritative body moved and the navigation result reports a
    positive path/movement diagnostic.  This prevents a stale or merely
    hypothetical partial path from widening every ordinary failed route.
    """

    if result.success:
        return False
    # An objective time slice can expire after some useful movement. That is
    # not evidence that wider mutation operators are required. Only the
    # independently proven zero-progress timeout may hand off once.
    if str(result.reason or "") == "timeout_ticks_exhausted":
        return navigation_zero_progress(result, before, after)
    if not navigation_governed_mobility_upgrade_reason(result.reason):
        return False
    if navigation_zero_progress(result, before, after):
        return True
    if _position_distance(before, after) <= 0.25:
        return False
    _observed, positive = _navigation_progress_evidence(result, before)
    return positive


def _navigation_progress_evidence(
    result: ToolResult,
    before: Position,
) -> tuple[bool, bool]:
    """Return ``(observed, positive)`` path evidence from a navigation result."""

    observed = False
    positive = False

    def visit(value: object) -> None:
        nonlocal observed, positive
        if isinstance(value, dict):
            movement_counts = value.get("movement_counts")
            if isinstance(movement_counts, dict):
                observed = True
                if any(_positive_number(count) for count in movement_counts.values()):
                    positive = True
            # Java Body's compact NAVIGATE terminal contract carries the same
            # authoritative position as precise scalar final_x/final_y/final_z
            # fields and may also include a block-quantized final_pos summary.
            # Prefer the scalar triplet when both are present: comparing a
            # player's fractional start to the quantized summary can invent
            # displacement on a terminal where the player never moved.
            scalar_position = all(
                key in value and value[key] is not None
                for key in ("final_x", "final_y", "final_z")
            )
            if scalar_position:
                try:
                    final_xyz = (
                        float(value["final_x"]),
                        float(value["final_y"]),
                        float(value["final_z"]),
                    )
                    observed = True
                    if _position_distance(before, final_xyz) > 0.25:
                        positive = True
                except (TypeError, ValueError):
                    pass
            else:
                final_pos = value.get("final_pos")
                if isinstance(final_pos, (list, tuple)) and len(final_pos) >= 3:
                    try:
                        final_xyz = (
                            float(final_pos[0]),
                            float(final_pos[1]),
                            float(final_pos[2]),
                        )
                        observed = True
                        if _position_distance(before, final_xyz) > 0.25:
                            positive = True
                    except (TypeError, ValueError):
                        pass
            for key in ("path_length", "waypoints", "move_ticks", "partial_distance"):
                if key in value and value[key] is not None:
                    observed = True
                    if _positive_number(value[key]):
                        positive = True
            for nested in value.values():
                if isinstance(nested, (dict, list, tuple)):
                    visit(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                if isinstance(nested, (dict, list, tuple)):
                    visit(nested)

    visit(result.metrics or {})
    return observed, positive


def _position_distance(
    before: Position,
    after: Position,
) -> float:
    return (
        (before[0] - after[0]) ** 2
        + (before[1] - after[1]) ** 2
        + (before[2] - after[2]) ** 2
    ) ** 0.5


def _positive_number(value: object) -> bool:
    try:
        return float(value) > 0.0
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class ExecutedSegment:
    index: int
    status: str
    target: Position | None
    terminal_reason: str | None
    success: bool
    action_id: str | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SurvivalReflexSettle:
    state: BodyState
    idle: bool
    owner: str | None
    pending_action_count: int | None
    waited_s: float
    events: tuple[dict[str, object], ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "idle": self.idle,
            "owner": self.owner,
            "pending_action_count": self.pending_action_count,
            "waited_s": round(self.waited_s, 3),
            "events": list(self.events),
        }


class NavigationTransactions:
    def _walking_only(self, run: Callable[[], ToolResult]) -> ToolResult:
        """Run a leg with the client Body's pathing kept from breaking or placing.

        The navigateTo the client Body runs carries only its goal, so the
        movement profile's allow_break=False never reached Baritone, whose own
        setting stays on: a run from creepers broke 5 blocks of the Body's
        sealed pit and took it out among 7 creepers (run-17 05:11,
        body-tool-boundaries-20261110 §13). A Body without these settings
        (Scarpet, tests) runs the leg as before.
        """
        read = getattr(self.body, "get_reflex_facts", None)
        try:
            state = read() if callable(read) else {}
        except Exception:
            state = {}
        previous = {key: state[key] for key in ("allow_break", "allow_place") if isinstance(state.get(key), bool)}
        if len(previous) != 2:
            return run()

        def settings(values: dict[str, bool]) -> bool:
            action = Action.create("setBaritoneSettings", values)
            accepted = self.body.execute(action)
            if not accepted.ok:
                return False
            if getattr(accepted, "complete", True):
                return True
            terminal = self.body.await_action_terminal(action.id, timeout_s=5.0)
            return terminal.data.get("success") is True

        if not settings({"allow_break": False, "allow_place": False}):
            return run()
        try:
            return run()
        finally:
            settings(previous)


    """Executes navigation objectives through the Body navigation controller.

    The production ``navigate_to`` path delegates primary pathfinding and
    movement to Scarpet ``navigateTo`` so terrain reads stay server-side.
    Python remains the Body transaction glue: it starts the action, authorizes
    mutations, waits for terminal truth, and records progress.
    """

    def __init__(
        self,
        body: Body,
        conformance_model: object | None = None,
        *,
        progress: ProgressController | None = None,
        governance=None,
    ):
        self.body = body
        self.progress = progress or LocalProgressController()
        self.governance = governance or getattr(getattr(conformance_model, "costs", None), "governance", None)
        # A failed survival reflex is a provider-wide precondition, not a
        # consumer-specific navigation error.  Keep one bounded recovery
        # signature so a still-unchanged hazard cannot turn a frontier loop
        # into repeated recovery attempts.
        self._last_survival_recovery_signature: tuple[object, ...] | None = None

    @classmethod
    def server_side(
        cls,
        body: Body,
        governance,
        *,
        progress: ProgressController | None = None,
    ) -> "NavigationTransactions":
        """Build the production runtime without constructing a Python planner."""

        return cls(
            body,
            progress=progress,
            governance=governance,
        )

    def _recover_survival_hazard(
        self,
        state: BodyState,
        parent_config: NavigationRunConfig,
    ) -> ToolResult:
        """Run one bounded provider-owned recovery from an unresolved hazard.

        Scarpet is the authority for the hazard predicate and supplies a
        recovery target derived from that same predicate.  Python must not
        reconstruct a lava/water safety rule from a stale local map; it only
        dispatches the target with the explicit recovery marker and verifies
        the authoritative postcondition afterward.
        """

        hazard = dict(state.hazard_unresolved or {})
        raw_target = hazard.get("recovery_target")
        if not isinstance(raw_target, (list, tuple)) or len(raw_target) != 3:
            return ToolResult(
                False,
                "survival_recovery_target_unavailable",
                False,
                next_suggestion="observe the Body hazard state again before attempting ordinary actions",
                metrics={
                    "attempted": False,
                    "hazard_unresolved": hazard,
                    "provider_target": raw_target,
                },
            )
        try:
            target = (
                floor(float(raw_target[0])),
                floor(float(raw_target[1])),
                floor(float(raw_target[2])),
            )
        except (TypeError, ValueError):
            return ToolResult(
                False,
                "survival_recovery_target_invalid",
                False,
                next_suggestion="observe the Body hazard state again before attempting ordinary actions",
                metrics={
                    "attempted": False,
                    "hazard_unresolved": hazard,
                    "provider_target": raw_target,
                },
            )

        kind = str(hazard.get("kind") or "unknown")
        recovery_config = pure_movement_navigation_config(
            NavigationRunConfig(
                max_segments=max(1, min(parent_config.max_segments, 8)),
                max_partial_segments=max(1, min(
                    parent_config.max_partial_segments
                    if parent_config.max_partial_segments is not None
                    else parent_config.max_segments,
                    4,
                )),
                segment_timeout_s=max(1.0, min(parent_config.segment_timeout_s, 15.0)),
                server_grid_radius=min(parent_config.server_grid_radius, 32),
                server_max_expand=min(parent_config.server_max_expand, 800),
                recheck_lookahead=parent_config.recheck_lookahead,
                allow_swim=True,
                aquatic_traversal=kind == "water",
                survival_recovery=True,
                recovery_attempts=0,
                # Recovery targets are provider-verified block centers.  The
                # ordinary 0.75 arrival radius can stop in the adjacent cell,
                # which may still be inside the hazard neighborhood.
                movement_arrival_radius=0.25,
                load_limited=parent_config.load_limited,
            )
        )
        navigation = self.navigate_to(
            GoalNear(target, radius=0),
            break_context=BreakContext.RECOVERY,
            config=recovery_config,
        )
        after = self.body.get_state()
        cleared = after.hazard_unresolved is None
        metrics = {
            "attempted": True,
            "kind": kind,
            "hazard_unresolved": hazard,
            "recovery_target": list(target),
            "navigation": navigation.to_payload(),
            "final_pos": list(after.pos),
            "hazard_cleared": cleared,
            "owner": after.body_owner,
            "pending_action_count": after.pending_action_count,
        }
        if cleared:
            return ToolResult(True, "survival_recovered", False, metrics=metrics)
        return ToolResult(
            False,
            "survival_recovery_incomplete",
            False,
            next_suggestion="the provider-owned recovery did not clear the hazard; keep ordinary actions blocked",
            metrics=metrics,
        )

    def navigate_to(
        self,
        goal: GoalLike,
        *,
        break_context: BreakContext | str = BreakContext.TRAVEL,
        config: NavigationRunConfig | None = None,
        timeout_s: float | None = None,
        arrival_radius: float | None = None,
        mutation_blacklist: set[Position] | None = None,
        continuation_check: Callable[[], str | None] | None = None,
    ) -> ToolResult:
        """Walk to a goal.

        A movement profile that may neither break nor place keeps the client
        Body's pathing from doing either for the whole call. Every "pure
        movement" walk goes through here: the approach to a block, to a spot
        to place from, to drops, an exploration leg, a run from mobs. Only
        the run from mobs was held to it before, and the approach of a
        place_here broke two blocks of the Agent's sealed cell with a swelling
        creeper outside (easy run-23 12:14:48, health 20 to 3, death a minute
        later; project-review-20261110.md §8.10).
        """
        def run() -> ToolResult:
            return self._navigate_to(
                goal, break_context=break_context, config=config, timeout_s=timeout_s,
                arrival_radius=arrival_radius, mutation_blacklist=mutation_blacklist,
                continuation_check=continuation_check,
            )
        if config is not None and not config.allow_break and not config.allow_place:
            return self._walking_only(run)
        return run()

    def _navigate_to(
        self,
        goal: GoalLike,
        *,
        break_context: BreakContext | str = BreakContext.TRAVEL,
        config: NavigationRunConfig | None = None,
        timeout_s: float | None = None,
        arrival_radius: float | None = None,
        mutation_blacklist: set[Position] | None = None,
        continuation_check: Callable[[], str | None] | None = None,
    ) -> ToolResult:
        cfg = config or NavigationRunConfig()
        if cfg.load_limited:
            cfg = server_load_limited_navigation_config(cfg)
        if cfg.recheck_lookahead < 0:
            raise ValueError("recheck_lookahead must be >= 0")
        if cfg.max_safe_fall_depth < 0 or cfg.max_safe_fall_depth > 3:
            raise ValueError("max_safe_fall_depth must be between 0 and 3")
        if cfg.max_water_drop_depth < 1 or cfg.max_water_drop_depth > 64:
            raise ValueError("max_water_drop_depth must be between 1 and 64")
        if cfg.aquatic_replan_attempts < 0:
            raise ValueError("aquatic_replan_attempts must be >= 0")
        if cfg.max_break_steps < 0:
            raise ValueError("max_break_steps must be >= 0")
        if cfg.max_place_steps < 0:
            raise ValueError("max_place_steps must be >= 0")
        if cfg.max_pillar_steps < 0:
            raise ValueError("max_pillar_steps must be >= 0")
        if cfg.max_downward_steps < 0:
            raise ValueError("max_downward_steps must be >= 0")
        if cfg.max_open_steps < 0:
            raise ValueError("max_open_steps must be >= 0")
        if cfg.recovery_attempts < 0:
            raise ValueError("recovery_attempts must be >= 0")
        if timeout_s is not None:
            if timeout_s <= 0:
                raise ValueError("timeout_s must be > 0")
            per_segment = max(cfg.segment_timeout_s, timeout_s / max(1, cfg.max_segments))
            cfg = replace(cfg, segment_timeout_s=per_segment)
        if arrival_radius is not None:
            if arrival_radius <= 0:
                raise ValueError("arrival_radius must be > 0")
            cfg = replace(cfg, movement_arrival_radius=arrival_radius)

        generation = self.progress.current_generation()
        executed: list[ExecutedSegment] = []
        nav_goal = normalize_goal(goal)
        settle = _wait_for_survival_reflex_idle(
            self.body,
            timeout_s=_survival_reflex_settle_timeout(cfg.segment_timeout_s),
        )
        state = settle.state
        start = _block_pos(state)
        break_policy_context = BreakContext(break_context)
        goal_anchor = nav_goal.representative(start)
        if not settle.idle:
            return _result(
                False,
                "survival_reflex_active",
                True,
                goal_anchor,
                executed,
                {
                    "navigation_goal": nav_goal.payload(),
                    "survival_reflex_settle": settle.to_payload(),
                },
            )
        if state.hazard_unresolved is not None and not cfg.survival_recovery:
            hazard = dict(state.hazard_unresolved)
            signature = _survival_recovery_signature(state)
            recovery: ToolResult | None = None
            if signature != self._last_survival_recovery_signature:
                self._last_survival_recovery_signature = signature
                recovery = self._recover_survival_hazard(state, cfg)
                if recovery.success:
                    refreshed = self.body.get_state()
                    if refreshed.hazard_unresolved is None:
                        self._last_survival_recovery_signature = None
                        state = refreshed
                        start = _block_pos(state)
                        goal_anchor = nav_goal.representative(start)
                    else:
                        recovery = ToolResult(
                            False,
                            "survival_recovery_incomplete",
                            False,
                            metrics={
                                **dict(recovery.metrics or {}),
                                "hazard_unresolved_after": dict(refreshed.hazard_unresolved),
                            },
                        )
            if recovery is None or not recovery.success:
                return _result(
                    False,
                    "survival_hazard_unresolved",
                    False,
                    goal_anchor,
                    executed,
                    {
                        "navigation_goal": nav_goal.payload(),
                        "hazard_unresolved": hazard,
                        "next_action": "survival_recovery",
                        "survival_recovery": (
                            dict(recovery.metrics or {}) | {"reason": recovery.reason}
                            if recovery is not None
                            else {"attempted": False, "reason": "unchanged_hazard_signature"}
                        ),
                    },
                )
        server_goals, goal_set_preserved = _server_goal_set(nav_goal, start)
        gx, gy, gz = int(goal_anchor[0]), int(goal_anchor[1]), int(goal_anchor[2])
        ar = cfg.movement_arrival_radius or 0.75
        goal_radius = int(getattr(nav_goal, "radius", 0) or 0)
        capability_snapshot = _navigation_capability_snapshot(self.body, cfg)
        denied_mutations = mutation_blacklist if mutation_blacklist is not None else set()
        broken_steps = 0
        placed_steps = 0
        pillar_steps = 0
        downward_steps = 0
        open_steps = 0
        recovery_attempts: list[dict[str, object]] = []

        segment_index = 0
        partial_segments = 0
        max_partial_segments = cfg.max_partial_segments if cfg.max_partial_segments is not None else cfg.max_segments
        while segment_index < cfg.max_segments:
            if not self.progress.generation_current(generation):
                return _result(False, "preempted", True, goal_anchor, executed, {"generation_current": False})

            continuation_reason = (
                continuation_check() if continuation_check is not None else None
            )
            if continuation_reason is not None:
                return _result(
                    False,
                    continuation_reason,
                    True,
                    goal_anchor,
                    executed,
                    {
                        "navigation_goal": nav_goal.payload(),
                        "continuation_yield": {
                            "reason": continuation_reason,
                            "completed_segments": len(executed),
                        },
                    },
                )
            action_params = _compact_navigate_action_params(
                {
                    "target": [gx, gy, gz],
                    "goals": [list(candidate) for candidate in server_goals],
                    "grid_radius": cfg.server_grid_radius,
                    "max_expand": cfg.server_max_expand,
                    "y_below": 8,
                    "y_above": 8,
                    "arrival_radius": ar,
                    "goal_radius": goal_radius,
                    "timeout_ticks": max(20, int(cfg.segment_timeout_s * 20)),
                    "no_progress_ticks": cfg.server_no_progress_ticks,
                    "min_partial_progress": max(1, cfg.min_partial_progress),
                    "partial_replans": max(0, max_partial_segments - partial_segments - 1),
                    "segment_index": segment_index,
                    "allow_diagonal": cfg.allow_diagonal,
                    "allow_ascend": cfg.allow_ascend,
                    "allow_descend": cfg.allow_descend,
                    "allow_swim": cfg.allow_swim,
                    "aquatic_traversal": cfg.aquatic_traversal,
                    "survival_recovery": cfg.survival_recovery,
                    "aquatic_replan_attempts": max(0, cfg.aquatic_replan_attempts),
                    "max_fall_depth": cfg.max_safe_fall_depth,
                    "max_water_drop_depth": cfg.max_water_drop_depth,
                    "recheck_lookahead": cfg.recheck_lookahead,
                    "allow_break": bool(cfg.allow_break and cfg.max_break_steps > broken_steps),
                    "break_budget": max(0, cfg.max_break_steps - broken_steps),
                    "break_timeout_ticks": max(20, int(cfg.segment_timeout_s * 20)),
                    "break_pickaxe": capability_snapshot["break_pickaxe"],
                    "break_axe": capability_snapshot["break_axe"],
                    "break_shovel": capability_snapshot["break_shovel"],
                    "allow_place": bool(capability_snapshot["allow_place"]),
                    "scaffold_item": capability_snapshot["scaffold_item"],
                    "scaffold_count": max(
                        0,
                        int(capability_snapshot["scaffold_count"]) - placed_steps - pillar_steps,
                    ),
                    "place_budget": max(0, cfg.max_place_steps - placed_steps),
                    "allow_pillar": bool(
                        cfg.allow_pillar
                        and capability_snapshot["has_scaffold"]
                        and cfg.max_pillar_steps > pillar_steps
                    ),
                    "pillar_budget": max(0, cfg.max_pillar_steps - pillar_steps),
                    "allow_downward": bool(
                        cfg.allow_downward and cfg.max_downward_steps > downward_steps
                    ),
                    "downward_budget": max(0, cfg.max_downward_steps - downward_steps),
                    "allow_open": bool(cfg.allow_open and cfg.max_open_steps > open_steps),
                    "open_budget": max(0, cfg.max_open_steps - open_steps),
                    "denied_mutations": [list(pos) for pos in sorted(denied_mutations)],
                }
            )
            action = Action.create("navigateTo", action_params)
            from minebot.game.protocol import build_action_call

            action = action_with_timeout(action, cfg.segment_timeout_s)
            action_command_len = len(build_action_call(getattr(self.body, "bot_name", ""), action))
            result = self.body.execute(action)
            if not (result.ok and result.accepted):
                executed.append(ExecutedSegment(
                    index=segment_index, status="rejected", target=goal_anchor,
                    terminal_reason="body_rejected", success=False, action_id=action.id,
                    diagnostics={"error": result.error, "data": result.data},
                ))
                rejected = body_rejection_to_tool_result(result, {"error": result.error})
                if rejected is not None:
                    return _result(
                        False,
                        rejected.reason,
                        rejected.can_retry,
                        goal_anchor,
                        executed,
                        {**dict(rejected.metrics or {}), "next_suggestion": rejected.next_suggestion},
                    )

            navigation_events: list[dict[str, object]] = []
            mutation_events: list[dict[str, object]] = []
            last_proposal_pos: Position | None = None
            while True:
                terminal = self.body.await_action_terminal(
                    action.id,
                    timeout_s=action_wait_timeout_s(action),
                    terminal_events={
                        "navigateDone",
                        "navigateMutationProposed",
                        "navigateMutationDone",
                        "navigateStartTrace",
                        "moveDone",
                        "death",
                        "respawned",
                        "ownerPreempted",
                    },
                    intermediate_events={
                        "navigateMutationProposed",
                        "navigateMutationDone",
                        "navigateStartTrace",
                        "moveDone",
                    },
                )
                if terminal.name in {"navigateStartTrace", "moveDone"}:
                    navigation_events.append({"event": terminal.name, "data": dict(terminal.data)})
                    continue
                if terminal.name == "navigateMutationProposed":
                    proposal = dict(terminal.data)
                    mutation_events.append({"event": terminal.name, "data": proposal})
                    last_proposal_pos = _event_position(proposal.get("pos"))
                    self._answer_navigation_mutation(action.id, proposal, break_context=break_policy_context)
                    continue
                if terminal.name == "navigateMutationDone":
                    mutation = dict(terminal.data)
                    mutation_events.append({"event": terminal.name, "data": mutation})
                    mutation_pos = _event_position(mutation.get("pos"))
                    if bool(mutation.get("success")):
                        if mutation.get("kind") == "break":
                            broken_steps += 1
                        if mutation.get("kind") == "downward":
                            downward_steps += 1
                        if mutation.get("kind") == "open":
                            open_steps += 1
                        if mutation.get("kind") in {"place", "pillar"} and mutation_pos is not None:
                            mutation_kind = str(mutation.get("kind"))
                            purpose = "pillar" if mutation_kind == "pillar" else "bridge"
                            if mutation_kind == "pillar":
                                pillar_steps += 1
                            else:
                                placed_steps += 1
                            block_type = str(mutation.get("block_type") or capability_snapshot["scaffold_item"] or "unknown")
                            if self.governance is not None:
                                self.governance.record_bot_placement(
                                    mutation_pos,
                                    block_type,
                                    purpose,
                                    self.body.bot_name,
                                )
                    continue
                break
            if not self.progress.generation_current(generation):
                return _result(False, "preempted", True, goal_anchor, executed, {"generation_current": False})

            action_start_seq = getattr(self.body, "_action_start_seqs", {}).get(action.id, 0)
            td = terminal.data
            raw_nav_reason = td.get("reason") or td.get("nav_reason") or td.get("stopped_reason") or terminal.name
            nav_reason = "preempted" if terminal.name == "ownerPreempted" else raw_nav_reason
            nav_arrived = bool(td.get("arrived", False)) or nav_reason == "arrived"
            goal_dist = td.get("goal_dist", td.get("dist_to_target", 9999.0))
            selected_goal = _event_position(td.get("selected_goal")) or goal_anchor

            executed.append(ExecutedSegment(
                index=segment_index, status=nav_reason, target=selected_goal,
                terminal_reason=nav_reason, success=nav_arrived, action_id=action.id,
                diagnostics={
                    "expanded": td.get("expanded", 0),
                    "waypoints": td.get("waypoints", 0),
                    "goal_dist": goal_dist,
                    "event": terminal.name,
                    "raw_reason": raw_nav_reason,
                    "move_ticks": td.get("move_ticks"),
                    "move_min_dist": td.get("move_min_dist"),
                    "move_stuck_ticks": td.get("move_stuck_ticks"),
                    "move_deviation": td.get("move_deviation"),
                    "move_waypoint_index": td.get("move_waypoint_index"),
                    "move_waypoint_count": td.get("move_waypoint_count"),
                    "move_current_waypoint": td.get("move_current_waypoint"),
                    "movement_counts": td.get("movement_counts"),
                    "capability_snapshot": td.get("capability_snapshot"),
                    "partial_coefficient": td.get("partial_coefficient"),
                    "partial_distance": td.get("partial_distance"),
                    "recheck_reason": td.get("recheck_reason"),
                    "navigation_events": navigation_events,
                    "mutation_events": mutation_events,
                    "event_data": dict(td),
                    "selected_goal": list(selected_goal),
                    "goal_count": len(server_goals),
                    "goal_set_preserved": goal_set_preserved,
                    "action_command_len": action_command_len,
                    "action_command_target_chars": RCON_MUTATION_COMMAND_TARGET_CHARS,
                },
            ))

            neutral_step = nav_reason in {"partial", "preempted", "world_changed"}
            self.progress.note_step(
                ("navigate.segment", start, nav_goal.payload(), nav_reason, goal_anchor),
                success=nav_arrived or nav_reason == "partial",
                fingerprint=self.progress.fingerprint(self.body.get_state()),
                neutral=neutral_step,
            )

            if nav_arrived:
                return _result(
                    True,
                    "arrived",
                    False,
                    selected_goal,
                    executed,
                    {
                        "navigation_goal": nav_goal.payload(),
                        "selected_goal": list(selected_goal),
                        "goal_count": len(server_goals),
                        "goal_set_preserved": goal_set_preserved,
                        "movement_counts": td.get("movement_counts"),
                        "capability_snapshot": td.get("capability_snapshot"),
                    },
                )

            if nav_reason == "preempted":
                reflex = _wait_for_reflex_completion(
                    self.body,
                    timeout_s=min(5.0, max(1.0, cfg.segment_timeout_s / 3.0)),
                    after_seq=action_start_seq,
                )
                if reflex is not None and reflex.name == "reflexCompleted":
                    if executed:
                        executed[-1].diagnostics["reflex_handoff"] = {
                            "event": reflex.name,
                            "seq": reflex.seq,
                            "data": dict(reflex.data),
                        }
                    water_egress_confirmed = (
                        reflex.data.get("kind") == "water"
                        and reflex.data.get("escaped_hazard") is True
                        and reflex.data.get("final_is_dry_stand") is True
                    )
                    if reflex.data.get("escaped_hazard") is False or (
                        reflex.data.get("kind") == "water" and not water_egress_confirmed
                    ):
                        return _result(
                            False,
                            "water_egress_failed" if reflex.data.get("kind") == "water" else "reflex_failed",
                            True,
                            goal_anchor,
                            executed,
                            {
                                "navigation_goal": nav_goal.payload(),
                                "paused": False,
                                "reflex_handoff": "reflex_failed",
                                "reflex": dict(reflex.data),
                            },
                        )
                    state = self.body.get_state()
                    return _result(
                        False,
                        "survival_reflex_recovered",
                        True,
                        goal_anchor,
                        executed,
                        {
                            "navigation_goal": nav_goal.payload(),
                            "paused": False,
                            "reflex_handoff": "route_invalidated",
                            "reflex": dict(reflex.data),
                            "safe_pos": list(state.pos),
                        },
                    )
                return _result(
                    True,
                    "preempted",
                    True,
                    goal_anchor,
                    executed,
                    {
                        "navigation_goal": nav_goal.payload(),
                        "paused": True,
                        "reflex_handoff": "timeout" if reflex is None else reflex.name,
                    },
                )

            if nav_reason == "partial":
                state = self.body.get_state()
                start = _block_pos(state)
                partial_segments += 1
                if partial_segments >= max_partial_segments:
                    return _result(
                        False,
                        "partial_segment_budget_exhausted",
                        True,
                        goal_anchor,
                        executed,
                        {
                            "navigation_goal": nav_goal.payload(),
                            "goal_dist": goal_dist,
                            "partial_segments": partial_segments,
                            "max_partial_segments": max_partial_segments,
                        },
                    )
                segment_index += 1
                continue

            if nav_reason == "world_changed":
                state = self.body.get_state()
                start = _block_pos(state)
                segment_index += 1
                continue

            if nav_reason == "mutation_denied":
                if last_proposal_pos is not None:
                    denied_mutations.add(last_proposal_pos)
                state = self.body.get_state()
                start = _block_pos(state)
                segment_index += 1
                continue

            if nav_reason in {"stuck", "deviated", "no_path"} and len(recovery_attempts) < cfg.recovery_attempts:
                recovery = self._run_navigation_recovery(
                    original_goal=goal_anchor,
                    original_reason=str(nav_reason),
                    cfg=cfg,
                    mutation_blacklist=denied_mutations,
                )
                recovery_attempts.append(recovery.to_payload())
                executed[-1].diagnostics["recovery"] = recovery.to_payload()
                if recovery.success and recovery.reason == "arrived":
                    state = self.body.get_state()
                    start = _block_pos(state)
                    segment_index += 1
                    continue
                return _result(
                    False,
                    f"recovery_exhausted:{nav_reason}",
                    True,
                    goal_anchor,
                    executed,
                    {
                        "navigation_goal": nav_goal.payload(),
                        "goal_dist": goal_dist,
                        "recovery_attempts": recovery_attempts,
                    },
                )

            return _result(False, nav_reason, nav_reason in ("stuck", "timeout", "partial_segment_budget_exhausted"), goal_anchor, executed, {
                "navigation_goal": nav_goal.payload(), "goal_dist": goal_dist,
            })

        if executed and all(segment.status == "mutation_denied" for segment in executed):
            return _result(
                False,
                "protected_or_denied",
                True,
                goal_anchor,
                executed,
                {
                    "navigation_goal": nav_goal.payload(),
                    **_denied_navigation_summary(executed),
                },
            )
        return _result(False, "segment_budget_exhausted", True, goal_anchor, executed, {"navigation_goal": nav_goal.payload()})

    def _run_navigation_recovery(
        self,
        *,
        original_goal: Position,
        original_reason: str,
        cfg: NavigationRunConfig,
        mutation_blacklist: set[Position],
    ) -> ToolResult:
        origin = _block_pos(self.body.get_state())
        candidates = _recovery_goal_candidates(
            origin,
            original_goal,
            distances=cfg.recovery_detour_distances,
            offsets=cfg.recovery_detour_offsets,
            y_offsets=cfg.recovery_detour_y_offsets,
        )
        if not candidates:
            return ToolResult(
                success=False,
                reason="recovery_candidate_exhausted",
                can_retry=True,
                metrics={"origin": list(origin), "original_reason": original_reason},
            )
        recovery_config = replace(
            cfg,
            max_segments=max(1, min(4, cfg.max_segments)),
            max_partial_segments=max(1, min(4, cfg.max_partial_segments or cfg.max_segments)),
            recovery_attempts=0,
        )
        goal = GoalComposite(tuple(GoalNear(candidate, radius=0) for candidate in candidates))
        result = self.navigate_to(
            goal,
            break_context=BreakContext.RECOVERY,
            config=recovery_config,
            mutation_blacklist=mutation_blacklist,
        )
        metrics = dict(result.metrics or {})
        metrics.update(
            {
                "origin": list(origin),
                "original_goal": list(original_goal),
                "original_reason": original_reason,
                "recovery_goal": goal.payload(),
                "recovery_candidates": [list(candidate) for candidate in candidates],
            }
        )
        return ToolResult(
            success=result.success,
            reason=result.reason,
            can_retry=result.can_retry,
            next_suggestion=result.next_suggestion,
            metrics=metrics,
        )

    def _answer_navigation_mutation(
        self,
        navigation_action_id: str,
        proposal: dict[str, object],
        *,
        break_context: BreakContext,
    ) -> None:
        mutation_kind = str(proposal.get("kind") or "unknown")
        pos = _event_position(proposal.get("pos"))
        block_type = str(proposal.get("block_type") or "unknown")
        allowed = False
        reason = "unsupported_mutation"
        if mutation_kind in {"break", "downward"} and pos is not None and self.governance is not None:
            try:
                fact = self.body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
            except Exception as exc:
                fact = None
                reason = f"world_read_failed:{type(exc).__name__}"
            observed_type = (
                "unknown"
                if fact is None
                else str(fact.data.get("type") or "unknown").removeprefix("minecraft:")
            )
            proposed_type = block_type.removeprefix("minecraft:")
            if fact is None:
                pass
            elif not fact.ok or not fact.complete:
                reason = "world_read_failed"
            elif observed_type != proposed_type:
                reason = "world_changed"
            else:
                decision = self.governance.can_break(pos, observed_type, break_context)
                allowed = decision.allowed
                reason = decision.reason
        elif mutation_kind in {"place", "pillar"} and pos is not None and self.governance is not None:
            decision = self.governance.can_place(
                pos,
                block_type,
                PlaceContext.WORK,
                self.body.bot_name,
            )
            allowed = decision.allowed
            reason = decision.reason
        elif mutation_kind == "open" and pos is not None and self.governance is not None:
            try:
                fact = self.body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
            except Exception as exc:
                fact = None
                reason = f"world_read_failed:{type(exc).__name__}"
            observed_type = (
                "unknown"
                if fact is None
                else str(fact.data.get("type") or "unknown").removeprefix("minecraft:")
            )
            proposed_type = block_type.removeprefix("minecraft:")
            if fact is None:
                pass
            elif not fact.ok or not fact.complete:
                reason = "world_read_failed"
            elif observed_type != proposed_type:
                reason = "world_changed"
            elif str((fact.data.get("properties") or {}).get("open") or "false").lower() == "true":
                reason = "world_changed"
            else:
                decision = self.governance.can_interact(pos, observed_type, InteractionContext.ACTIVATE)
                allowed = decision.allowed
                reason = decision.reason
        decision_action = Action.create(
            "navigationMutationDecision",
            {
                "navigation_action_id": navigation_action_id,
                "proposal_id": proposal.get("proposal_id"),
                "authorized": allowed,
                "reason": reason,
                "pos": list(pos) if pos is not None else None,
                "kind": mutation_kind,
                "block_type": block_type,
            },
        )
        accepted = self.body.execute(decision_action)
        if not (accepted.ok and accepted.accepted):
            # A survival reflex may preempt a waiting navigation mutation while
            # Python is still collecting the authoritative governance evidence
            # for the proposal.  In that case Scarpet has already emitted the
            # mutation/navigation terminal truth and rejects this late decision
            # because no matching proposal is waiting anymore.  Do not turn
            # that race into a Python exception; keep draining the Body events
            # so the caller receives the typed ``preempted``/reflex terminal.
            return


    def follow_entity(
        self,
        target_spec: str,
        *,
        keep_distance: float = 3.0,
        timeout_s: float = 30.0,
        config: NavigationRunConfig | None = None,
    ) -> ToolResult:
        """Follow a moving entity (player or named mob) by name.

        The Body owns the physical pursuit: Scarpet ``followEntity`` re-plans a
        server-side path to the target's live position on a cadence (move done
        or target drifted beyond ``replan_distance``) and ``run_move_tick``
        walks it with jump/obstacle handling. The brain emits one intent
        ("follow X") and waits for ``followDone``. This is ``navigate_to`` with
        a moving goal — the same body-owns-execution, brain-stays-thin shape.
        """
        cfg = config or NavigationRunConfig()
        if not target_spec:
            raise ValueError("target_spec must be a non-empty name/uuid")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if keep_distance < 0:
            raise ValueError("keep_distance must be >= 0")

        generation = self.progress.current_generation()
        if not self.progress.generation_current(generation):
            return _result(False, "preempted", True, _block_pos(self.body.get_state()), [], {"generation_current": False})

        settle = _wait_for_survival_reflex_idle(
            self.body,
            timeout_s=_survival_reflex_settle_timeout(timeout_s),
        )
        start = _block_pos(settle.state)
        if not settle.idle:
            return _result(
                False,
                "survival_reflex_active",
                True,
                start,
                [],
                {
                    "target_spec": target_spec,
                    "survival_reflex_settle": settle.to_payload(),
                },
            )

        resolved = resolve_entity_target(self.body, target_spec, radius=32)
        if isinstance(resolved, ToolResult):
            return resolved
        target_uuid = resolved.target_uuid

        action = Action.create(
            "followEntity",
            {
                "target_uuid": target_uuid,
                "keep_radius": keep_distance,
                "replan_distance": 2.0,
                "acquire_radius": 32,
                "grid_radius": cfg.server_grid_radius,
                "max_expand": cfg.server_max_expand,
                "no_progress_ticks": cfg.server_no_progress_ticks,
                "min_partial_progress": max(1, cfg.min_partial_progress),
                "allow_diagonal": cfg.allow_diagonal,
                "allow_ascend": cfg.allow_ascend,
                "allow_descend": cfg.allow_descend,
                "allow_swim": cfg.allow_swim,
                "max_fall_depth": cfg.max_safe_fall_depth,
                "max_water_drop_depth": cfg.max_water_drop_depth,
                "timeout_ticks": max(20, int(timeout_s * 20)),
            },
        )
        action = action_with_timeout(action, timeout_s)
        result = self.body.execute(action)
        if not (result.ok and result.accepted):
            rejected = body_rejection_to_tool_result(
                result,
                {"error": result.error, **resolved.evidence()},
            )
            if rejected is None:
                return _result(False, "body_rejected", True, start, [], {"error": result.error, **resolved.evidence()})
            return _result(
                False,
                rejected.reason,
                rejected.can_retry,
                start,
                [],
                {**dict(rejected.metrics or {}), **resolved.evidence()},
            )

        terminal = self.body.await_action_terminal(
            action.id,
            timeout_s=action_wait_timeout_s(action),
            terminal_events={"followDone", "death", "respawned"},
        )
        if not self.progress.generation_current(generation):
            return _result(False, "preempted", True, start, [], {"generation_current": False, **resolved.evidence()})

        td = terminal.data
        mismatch = entity_target_terminal_mismatch(resolved, td)
        if mismatch is not None:
            return mismatch
        arrived = bool(td.get("arrived", False))
        reason = str(td.get("reason") or "unknown")
        self.progress.note_step(
            ("follow.tick", start, target_uuid, reason),
            success=arrived,
            fingerprint=self.progress.fingerprint(self.body.get_state()),
            neutral=reason in ("timeout", "target_lost"),
        )
        return _result(
            arrived,
            reason,
            reason in ("timeout", "target_lost", "stuck"),
            start,
            [],
            {**resolved.evidence(), "keep_distance": keep_distance, "event": terminal.name},
        )

    def _hostiles_near(self, radius: float) -> list[dict[str, object]] | None:
        """Hostile mobs the Body sees within ``radius`` now, or None when that cannot be read."""
        try:
            perception = self.body.perceive("nearbyHostiles", {"radius": int(radius), "limit": 16})
        except Exception:
            return None
        if not perception.ok or not isinstance(perception.data, dict):
            return None
        found = []
        for item in perception.data.get("entities") or []:
            pos = item.get("pos") if isinstance(item, dict) else None
            if isinstance(pos, (list, tuple)) and len(pos) == 3:
                found.append({"name": item.get("name") or item.get("type"), "pos": [float(value) for value in pos]})
        return found

    def _falling(self) -> dict[str, object] | None:
        """The Body's own fall fact when it is in the air and coming down, else None."""
        reader = getattr(self.body, "get_reflex_facts", None)
        if not callable(reader):
            return None
        try:
            fall = reader().get("fall")
        except Exception:
            return None
        return dict(fall) if isinstance(fall, dict) else None

    def _hurt_here(self) -> dict[str, object] | None:
        """The Body's own count of hurts from the place it stands on now, else None."""
        reader = getattr(self.body, "get_reflex_facts", None)
        if not callable(reader):
            return None
        try:
            hurt = reader().get("hurt_here")
        except Exception:
            return None
        return dict(hurt) if isinstance(hurt, dict) else None

    def _still_losing_health(self, origin, required_distance, attempts) -> ToolResult | None:
        # Far from the danger is not safe while the place keeps taking health:
        # 68 blocks from the dragon, move_away said already_safe while its
        # breath took the Body from 20 to 3 (end run-7, 22:32:28).
        hurt = self._hurt_here()
        if hurt is None:
            return None
        final_state = self.body.get_state()
        return ToolResult(
            success=False,
            reason="still_losing_health",
            can_retry=True,
            metrics={"hurt_here": hurt, "final_pos": list(final_state.pos), "origin": list(origin),
                     "required_distance": required_distance, "attempts": attempts},
        )

    def move_away(
        self,
        danger: Position | tuple[float, float, float],
        *,
        min_distance: float = 6.0,
        target_distance: float | None = None,
        hazard_radius: float = 0.0,
        maintenance_checks: int = 1,
        maintenance_interval_s: float = 0.0,
        danger_refresh: Callable[[], Position | tuple[float, float, float]] | None = None,
        candidate_radii: tuple[int, ...] | None = None,
        max_candidates: int = 12,
        config: NavigationRunConfig | None = None,
        from_hostiles: bool = False,
        hostile_radius: float = 16.0,
        threat_id: str | None = None,
        threat_kind: str | None = None,
    ) -> ToolResult:
        """Increase distance from one hazard through shared avoid-goal navigation.

        Decision supplies ``threat_kind``/``threat_id`` for a single short
        dodge, bounded to native step facts and DODGE_TIMEOUT_S. The generic
        hazard-spacing parameters below apply to explicit hazard tools.

        This Body transaction routes through the shared navigation planner's
        inverse-goal shape instead of sampling bespoke local targets. It can
        also re-check a refreshed hazard anchor a bounded number of times so a
        moving hazard cannot silently invalidate the first escape segment.

        With ``from_hostiles`` the hazards are the hostile mobs the Body sees
        within ``hostile_radius`` at every check, not the first anchor: a mob
        that follows is measured where it is now, and stand points are ranked
        away from all of them together. ``danger`` is used only while that
        perception cannot be read.
        """

        if threat_kind is not None:
            return self._short_dodge(_xyz_pos(danger), threat_id, threat_kind, config)

        if min_distance <= 0:
            raise ValueError("min_distance must be > 0")
        if hazard_radius < 0:
            raise ValueError("hazard_radius must be >= 0")
        if maintenance_checks < 1:
            raise ValueError("maintenance_checks must be >= 1")
        if maintenance_interval_s < 0:
            raise ValueError("maintenance_interval_s must be >= 0")
        if max_candidates < 1:
            raise ValueError("max_candidates must be >= 1")
        if max_candidates > SERVER_GOAL_SET_LIMIT:
            raise ValueError(f"max_candidates must be <= {SERVER_GOAL_SET_LIMIT}")

        cfg = pure_movement_navigation_config(config)
        static_danger = _xyz_pos(danger)
        danger_provider = danger_refresh or (lambda: static_danger)
        seen_hostiles: list[dict[str, object]] = []

        def dangers() -> list[tuple[float, float, float]]:
            # Every move_away of M2 run-1 "ended normally" while the mob that
            # followed was measured at the spot it had left; the Body was
            # killed after 35 of them in a row (magma cubes, 2026-10-20).
            if from_hostiles:
                live = self._hostiles_near(hostile_radius)
                if live is not None:
                    seen_hostiles[:] = live
                    return [_xyz_pos(item["pos"]) for item in live]  # type: ignore[arg-type]
            return [_xyz_pos(danger_provider())]

        def nearest(pos: tuple[float, float, float], points: list[tuple[float, float, float]]) -> float:
            return min((dist(pos, point) for point in points), default=float("inf"))
        desired_distance = max(min_distance, target_distance or min_distance)
        required_distance = desired_distance + hazard_radius
        # A mob just outside the band must still be seen to be measured.
        hostile_radius = max(hostile_radius, required_distance + 4.0)
        if candidate_radii is None:
            candidate_radii = (2, 4, 6, 8)
            if required_distance > 8:
                candidate_radii += (int(required_distance) + 2,)
        attempts: list[dict[str, object]] = []
        moved = False
        origin = _block_pos(self.body.get_state())
        last_failure: ToolResult | None = None
        first_initial_distance: float | None = None
        last_initial_distance: float | None = None

        final_goal_payload: dict[str, object] | None = None
        for check_index in range(maintenance_checks):
            state = self.body.get_state()
            current = _block_pos(state)
            points = dangers()
            danger_xyz = min(points, key=lambda point: dist(state.pos, point)) if points else _xyz_pos(state.pos)
            initial_distance = nearest(state.pos, points)
            if first_initial_distance is None:
                first_initial_distance = initial_distance
            last_initial_distance = initial_distance
            attempt_context = {
                "check": check_index + 1,
                "danger": list(danger_xyz),
                "danger_block": list(_danger_block_pos(danger_xyz)),
                "origin": list(current),
                "initial_distance": initial_distance,
                "required_distance": required_distance,
            }

            if initial_distance >= required_distance:
                if maintenance_checks == 1 and not moved:
                    losing = self._still_losing_health(origin, required_distance, attempts)
                    if losing is not None:
                        return losing
                    return ToolResult(
                        success=True,
                        reason="already_safe",
                        can_retry=False,
                        metrics={
                            "danger": list(danger_xyz),
                            "origin": list(current),
                            "initial_distance": initial_distance,
                            "desired_distance": desired_distance,
                            "hazard_radius": hazard_radius,
                            "required_distance": required_distance,
                            "final_distance": initial_distance,
                            "attempts": [],
                        },
                    )
                attempts.append({**attempt_context, "result": {"success": True, "reason": "already_safe"}})
                if check_index + 1 < maintenance_checks and maintenance_interval_s > 0:
                    sleep(maintenance_interval_s)
                continue

            witness_candidates = _move_away_candidates(
                current,
                points or [danger_xyz],
                candidate_radii=candidate_radii,
                min_distance=required_distance,
                max_candidates=max_candidates,
            )
            if not witness_candidates:
                return ToolResult(
                    success=False,
                    reason="move_away_no_candidate",
                    can_retry=True,
                    next_suggestion="expand the move-away radius or fall back to a broader navigation/reflex escape path",
                    metrics={
                        "danger": list(danger_xyz),
                        "origin": list(current),
                        "initial_distance": initial_distance,
                        "desired_distance": desired_distance,
                        "hazard_radius": hazard_radius,
                        "required_distance": required_distance,
                            "candidate_radii": list(candidate_radii),
                        "attempts": attempts,
                    },
                )

            goal = GoalComposite(tuple(GoalNear(candidate, radius=0) for candidate in witness_candidates))
            final_goal_payload = goal.payload()
            result = self.navigate_to(goal, break_context=BreakContext.TRAVEL, config=cfg)
            chosen_goal = _selected_candidate(result, witness_candidates)
            final_state = self.body.get_state()
            refreshed = dangers()
            refreshed_danger = min(refreshed, key=lambda point: dist(final_state.pos, point)) if refreshed else danger_xyz
            final_distance = nearest(final_state.pos, refreshed)
            attempt = {
                **attempt_context,
                "goal": goal.payload(),
                "chosen_goal": list(chosen_goal),
                "result": result.to_payload(),
                "danger_after": list(refreshed_danger),
                "final_distance": final_distance,
            }
            attempts.append(attempt)
            if final_distance >= required_distance and final_distance > initial_distance:
                moved = True
                last_failure = None
                if check_index + 1 < maintenance_checks and maintenance_interval_s > 0:
                    sleep(maintenance_interval_s)
                continue

            if result.success:
                last_failure = ToolResult(
                    success=False,
                    reason="move_away_out_of_band",
                    can_retry=True,
                    next_suggestion="retry from a broader inverse-goal distance or refresh the hazard anchor before continuing",
                    metrics=attempt,
                )
            else:
                last_failure = result

        falling = self._falling()
        if falling is not None:
            # In the air the distance proves nothing: thrown by the dragon, the
            # Body saw no hostile and "moved away" while it fell out of the
            # world (end run-6, 21:12:14 and 21:20:12).
            final_state = self.body.get_state()
            return ToolResult(
                success=False,
                reason="falling",
                can_retry=True,
                metrics={"fall": falling, "final_pos": list(final_state.pos), "origin": list(origin),
                         "required_distance": required_distance, "attempts": attempts},
            )
        if last_failure is None:
            losing = self._still_losing_health(origin, required_distance, attempts)
            if losing is not None:
                return losing
            final_state = self.body.get_state()
            final_points = dangers()
            final_danger = (min(final_points, key=lambda point: dist(final_state.pos, point))
                            if final_points else _xyz_pos(final_state.pos))
            final_distance = nearest(final_state.pos, final_points)
            return ToolResult(
                success=final_distance >= required_distance,
                reason="moved_away" if moved else "already_safe",
                can_retry=False if final_distance >= required_distance else True,
                next_suggestion=None if final_distance >= required_distance else "retry from a broader inverse-goal distance",
                metrics={
                    "danger": list(final_danger),
                    "origin": list(origin),
                    "initial_distance": first_initial_distance if first_initial_distance is not None else final_distance,
                    "last_initial_distance": last_initial_distance if last_initial_distance is not None else final_distance,
                    "desired_distance": desired_distance,
                    "hazard_radius": hazard_radius,
                    "required_distance": required_distance,
                    "final_distance": final_distance,
                    **_hostile_metrics(seen_hostiles, final_state.pos, from_hostiles),
                    "maintenance_checks": maintenance_checks,
                    "chosen_goal": attempts[-1].get("chosen_goal"),
                    "navigation_goal": final_goal_payload,
                    "candidate_radii": list(candidate_radii),
                    "attempts": attempts,
                },
            )

        if last_failure is not None:
            if from_hostiles and last_failure.reason == "move_away_out_of_band":
                # The legs were walked and a hostile mob is still within the
                # band: said as that, not as "no candidate".
                final_state = self.body.get_state()
                final_points = dangers()
                return ToolResult(
                    success=False,
                    reason="hostile_still_near",
                    can_retry=True,
                    next_suggestion="a hostile mob kept up with the Body",
                    metrics={
                        "origin": list(origin),
                        "initial_distance": first_initial_distance,
                        "required_distance": required_distance,
                        "final_distance": nearest(final_state.pos, final_points),
                        **_hostile_metrics(seen_hostiles, final_state.pos, from_hostiles),
                        "maintenance_checks": maintenance_checks,
                        "navigation_goal": final_goal_payload,
                        "attempts": attempts,
                    },
                )
            if (
                last_failure.reason == "move_away_out_of_band"
                and not any((attempt.get("result") or {}).get("success") is False for attempt in attempts)
            ):
                return ToolResult(
                    success=False,
                    reason="move_away_no_candidate",
                    can_retry=True,
                    next_suggestion="expand the move-away radius or fall back to a broader navigation/reflex escape path",
                    metrics={
                        "danger": list(_xyz_pos(danger_provider())),
                        "origin": list(origin),
                        "initial_distance": first_initial_distance if first_initial_distance is not None else required_distance,
                        "last_initial_distance": last_initial_distance if last_initial_distance is not None else required_distance,
                        "desired_distance": desired_distance,
                        "hazard_radius": hazard_radius,
                        "required_distance": required_distance,
                        "maintenance_checks": maintenance_checks,
                        "navigation_goal": final_goal_payload,
                        "candidate_radii": list(candidate_radii),
                        "attempts": attempts,
                    },
                )
            return ToolResult(
                success=False,
                reason=f"move_away_failed:{last_failure.reason}",
                can_retry=True,
                next_suggestion=last_failure.next_suggestion or "retry with a wider escape radius or different hazard anchor",
                metrics={
                    "danger": list(_xyz_pos(danger_provider())),
                    "origin": list(origin),
                    "initial_distance": first_initial_distance if first_initial_distance is not None else required_distance,
                    "last_initial_distance": last_initial_distance if last_initial_distance is not None else required_distance,
                    "desired_distance": desired_distance,
                    "hazard_radius": hazard_radius,
                    "required_distance": required_distance,
                    "maintenance_checks": maintenance_checks,
                    "navigation_goal": final_goal_payload,
                    "candidate_radii": list(candidate_radii),
                    "attempts": attempts,
                },
            )
        return ToolResult(
            success=False,
            reason="move_away_failed",
            can_retry=True,
            next_suggestion="retry with a wider escape radius or different hazard anchor",
            metrics={
                "danger": list(_xyz_pos(danger_provider())),
                "origin": list(origin),
                "initial_distance": first_initial_distance if first_initial_distance is not None else required_distance,
                "last_initial_distance": last_initial_distance if last_initial_distance is not None else required_distance,
                "desired_distance": desired_distance,
                "hazard_radius": hazard_radius,
                "required_distance": required_distance,
                "maintenance_checks": maintenance_checks,
                "navigation_goal": final_goal_payload,
                "candidate_radii": list(candidate_radii),
                "attempts": attempts,
            },
        )

    def _short_dodge(
        self,
        danger: tuple[float, float, float],
        threat_id: str | None,
        threat_kind: str,
        config: NavigationRunConfig | None,
    ) -> ToolResult:
        """One local leg against the named threat; Decision owns repetition.

        The native steps fact supplies short, traversable rays (currently up
        to four cells). Consider prefixes so a melee dodge ends as soon as
        reach + margin is cleared. Never grow that domain to a firing range.
        """
        state = self.body.get_state()
        origin = _block_pos(state)
        reader = getattr(self.body, "get_reflex_facts", None)
        facts = reader() if callable(reader) else {}
        threats = _dodge_entities(facts)
        target = next((mob for mob in threats if threat_id and mob.get("id") == threat_id), None)
        anchor = _xyz_pos(target["pos"]) if target else danger
        required = attack_reach(threat_kind) + DODGE_MARGIN
        initial_distance = dist(state.pos, anchor)

        # Why each considered cell was left out, said when none is left
        # (dragon-unit-tests-20261030 §6.y.41: 117 bare dodge_no_safe_point).
        rejected = {"no_floor_beside_path": 0, "cloud_or_hurting_place": 0,
                    "toward_another_mob": 0, "nearer_the_threat": 0}

        def finish(success, reason, *, navigation=None, chosen=None):
            final = self.body.get_state()
            latest = reader() if callable(reader) else {}
            live = next((mob for mob in _dodge_entities(latest)
                         if threat_id and mob.get("id") == threat_id), None)
            there = _xyz_pos(live["pos"]) if live else anchor
            distance = dist(final.pos, there)
            return ToolResult(success, reason, not success, metrics={
                "origin": list(state.pos), "final_pos": list(final.pos),
                "threat_id": threat_id, "threat_kind": threat_kind,
                "danger": list(there), "threat_observed": live is not None,
                "distance_source": "observed" if live else "last_known_position",
                "initial_distance": initial_distance, "final_distance": distance,
                "required_distance": required, "outside_reach_margin": distance >= required,
                "chosen_goal": list(chosen) if chosen else None,
                "navigation": navigation.to_payload() if navigation else None,
                "maintenance_checks": 1, "movement_timeout_s": DODGE_TIMEOUT_S,
                **({"step_reach": DODGE_STEP_REACH, "native_step_directions": len(facts.get("steps") or []),
                    "level_step_cells": len(paths), "rejected_cells": dict(rejected)}
                   if reason == "dodge_no_safe_point" else {}),
            })

        paths: dict = {}
        if self._falling() is not None:
            return finish(False, "falling")
        if state.hazard_unresolved is not None:
            return finish(False, "survival_hazard_unresolved")

        paths = _dodge_paths(origin, facts)
        # Reuse the shared navigation world reads and stand predicates. Require
        # support beside each walked cell as well, so an island edge cannot
        # win merely because its destination itself has a floor.
        stands = {cell for path in paths.values() for cell in path}
        checked = {(x + dx, y, z + dz) for x, y, z in stands
                   for dx in (-1, 0, 1) for dz in (-1, 0, 1)}
        cells = tuple(sorted({(x, y + dy, z) for x, y, z in checked for dy in (-1, 0, 1)}))
        try:
            blocks = read_block_facts(self.body, cells, failure_label="dodge") if cells else {}
        except (ValueError, RuntimeError):
            return finish(False, "dodge_world_unreadable")

        def safe(cell):
            x, y, z = cell
            support = blocks.get((x, y - 1, z))
            # Solid hazard blocks still have collision/support.
            bad = {"magma_block", "cactus", "campfire", "soul_campfire", "sweet_berry_bush",
                   "fire", "soul_fire", "powder_snow", "wither_rose", "lava", "water"}
            return (cell_is_solid_support(support) and cell_is_clear(blocks.get(cell))
                    and cell_is_clear(blocks.get((x, y + 1, z)))
                    and all(str(blocks[pos].data.get("type", "")).removeprefix("minecraft:") not in bad
                            for pos in ((x, y - 1, z), cell, (x, y + 1, z))))

        candidates = []
        for cell, path in paths.items():
            if any(not safe((x + dx, y, z + dz)) for x, y, z in path
                   for dx in (-1, 0, 1) for dz in (-1, 0, 1)):
                rejected["no_floor_beside_path"] += 1
                continue
            end = (cell[0] + 0.5, float(cell[1]), cell[2] + 0.5)
            if not _dodge_place_clear(end, state.pos, facts):
                rejected["cloud_or_hurting_place"] += 1
                continue
            if any(not _dodge_place_clear((x + 0.5, float(y), z + 0.5), state.pos, facts,
                                         leaving=True) for x, y, z in path):
                rejected["cloud_or_hurting_place"] += 1
                continue
            # Other mobs only veto a step TOWARD their own attack envelope.
            # They contribute no pressure/rank, so distant calm endermen have
            # exactly no influence on this threat's dodge.
            if any(dist(point, mob["pos"]) < dist(state.pos, mob["pos"])
                   and dist(point, mob["pos"]) <= attack_reach(str(mob.get("type") or mob.get("name") or "")) + DODGE_MARGIN
                   for mob in threats if mob is not target
                   for point in ((x + 0.5, float(y), z + 0.5) for x, y, z in path)):
                rejected["toward_another_mob"] += 1
                continue
            distance = dist(end, anchor)
            if distance < initial_distance:
                rejected["nearer_the_threat"] += 1
                continue
            travel = dist(state.pos, end)
            dx, dz = end[0] - state.pos[0], end[2] - state.pos[2]
            ax, az = anchor[0] - state.pos[0], anchor[2] - state.pos[2]
            lateral = abs(dx * az - dz * ax) / (hypot(ax, az) or 1.0)
            candidates.append((cell, distance, travel, lateral))
        if not candidates:
            return finish(False, "dodge_no_safe_point")
        clearing = [item for item in candidates if item[1] >= required]
        if initial_distance < required and clearing:
            chosen = min(clearing, key=lambda item: (item[2], -item[1], item[0]))[0]
        else:
            # Already outside reach, or a shooter whose range cannot be left
            # in one step: leave its current line with the shortest local step.
            chosen = min(candidates, key=lambda item: (-item[3] / item[2], item[2], -item[1], item[0]))[0]
        cfg = replace(pure_movement_navigation_config(config), max_segments=1, max_partial_segments=1,
                      segment_timeout_s=DODGE_TIMEOUT_S, recovery_attempts=0, aquatic_replan_attempts=0,
                      allow_swim=False, allow_ascend=False, allow_descend=False, max_safe_fall_depth=0)
        result = self.navigate_to(GoalNear(chosen, radius=0), config=cfg)
        if self._falling() is not None:
            return finish(False, "falling", navigation=result, chosen=chosen)
        if self._hurt_here() is not None:
            return finish(False, "still_losing_health", navigation=result, chosen=chosen)
        return finish(result.success, "dodged" if result.success else f"dodge_failed:{result.reason}",
                      navigation=result, chosen=chosen)


def _block_pos(state: BodyState) -> Position:
    return (floor(state.pos[0]), floor(state.pos[1]), floor(state.pos[2]))


def _hostile_metrics(seen: list[dict[str, object]], pos: tuple[float, float, float],
                     enabled: bool) -> dict[str, object]:
    if not enabled:
        return {}
    near = sorted(({"name": item.get("name"), "dist": round(dist(pos, _xyz_pos(item["pos"])), 1)}  # type: ignore[arg-type]
                   for item in seen), key=lambda item: item["dist"])
    return {"hostiles_after": near[:8], "nearest_hostile": near[0] if near else None}


def _survival_recovery_signature(state: BodyState) -> tuple[object, ...]:
    hazard = state.hazard_unresolved or {}
    raw_pos = hazard.get("pos") if isinstance(hazard, dict) else None
    raw_target = hazard.get("recovery_target") if isinstance(hazard, dict) else None

    def xyz(value: object) -> tuple[object, ...]:
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            return ()
        return tuple(value)

    return (
        str(hazard.get("kind") or "unknown") if isinstance(hazard, dict) else "unknown",
        xyz(raw_pos),
        xyz(raw_target),
        _block_pos(state),
    )


def _server_goal_set(goal, start: Position) -> tuple[tuple[tuple[int, int, int, int], ...], bool]:
    if isinstance(goal, GoalBlock):
        return ((int(goal.pos[0]), int(goal.pos[1]), int(goal.pos[2]), 0),), True
    if isinstance(goal, GoalNear):
        return ((int(goal.pos[0]), int(goal.pos[1]), int(goal.pos[2]), int(goal.radius)),), True
    if isinstance(goal, GoalComposite) and goal.mode == "any":
        candidates: list[tuple[int, int, int, int]] = []
        fully_preserved = True
        for child in goal.goals:
            child_goals, preserved = _server_goal_set(child, start)
            fully_preserved = fully_preserved and preserved
            for candidate in child_goals:
                if candidate in candidates:
                    continue
                if len(candidates) >= SERVER_GOAL_SET_LIMIT:
                    return tuple(candidates), False
                candidates.append(candidate)
        if candidates:
            return tuple(candidates), fully_preserved
    anchor = goal.representative(start)
    radius = max(0, int(getattr(goal, "radius", 0) or 0))
    return ((int(anchor[0]), int(anchor[1]), int(anchor[2]), radius),), False


def _event_position(value: object) -> Position | None:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        return (int(value[0]), int(value[1]), int(value[2]))
    except (TypeError, ValueError):
        return None



def _result(
    success: bool,
    reason: str,
    can_retry: bool,
    goal: Position,
    executed: list[ExecutedSegment],
    extra: dict[str, object] | None = None,
) -> ToolResult:
    metrics: dict[str, object] = {
        "goal": list(goal),
        "segments": [_executed_payload(segment) for segment in executed],
        "segment_count": len(executed),
    }
    if extra:
        metrics.update(extra)
    return ToolResult(success=success, reason=reason, can_retry=can_retry, metrics=metrics)


def _denied_navigation_summary(executed: list[ExecutedSegment]) -> dict[str, object]:
    governance_blockers: dict[str, int] = {}
    mutation_blockers: dict[str, int] = {}
    movement_counts: dict[str, int] = {}
    final_pos: object | None = None
    selected_goal: object | None = None
    capability_snapshot: object | None = None
    denial_count = 0

    for segment in executed:
        diagnostics = segment.diagnostics
        event_data = diagnostics.get("event_data")
        if isinstance(event_data, dict) and event_data.get("final_pos") is not None:
            final_pos = event_data["final_pos"]
        if diagnostics.get("selected_goal") is not None:
            selected_goal = diagnostics["selected_goal"]
        if diagnostics.get("capability_snapshot") is not None:
            capability_snapshot = diagnostics["capability_snapshot"]

        segment_counts = diagnostics.get("movement_counts")
        if isinstance(segment_counts, dict):
            for movement, value in segment_counts.items():
                if isinstance(value, (int, float)) and int(value) > 0:
                    key = str(movement)
                    movement_counts[key] = movement_counts.get(key, 0) + int(value)

        mutation_events = diagnostics.get("mutation_events")
        if not isinstance(mutation_events, list):
            continue
        for event in mutation_events:
            if not isinstance(event, dict) or event.get("event") != "navigateMutationDone":
                continue
            data = event.get("data")
            if not isinstance(data, dict) or data.get("success") is not False:
                continue
            denial_count += 1
            decision_reason = str(data.get("decision_reason") or data.get("reason") or "mutation_denied")
            governance_blockers[decision_reason] = governance_blockers.get(decision_reason, 0) + 1
            mutation_key = ":".join(
                (
                    str(data.get("kind") or "unknown"),
                    str(data.get("block_type") or "unknown"),
                    decision_reason,
                )
            )
            mutation_blockers[mutation_key] = mutation_blockers.get(mutation_key, 0) + 1

    summary: dict[str, object] = {
        "denied_mutation_count": denial_count,
        "governance_blockers": governance_blockers,
        "mutation_blockers": mutation_blockers,
    }
    if movement_counts:
        summary["movement_counts"] = movement_counts
    if final_pos is not None:
        summary["final_pos"] = final_pos
    if selected_goal is not None:
        summary["selected_goal"] = selected_goal
    if capability_snapshot is not None:
        summary["capability_snapshot"] = capability_snapshot
    return summary


def _navigation_capability_snapshot(body: Body, cfg: NavigationRunConfig) -> dict[str, object]:
    disabled = {
        "break_pickaxe": None,
        "break_axe": None,
        "break_shovel": None,
        "has_scaffold": False,
        "allow_place": False,
        "scaffold_item": None,
        "scaffold_count": 0,
        "inventory_complete": False,
    }
    needs_break_inventory = cfg.allow_break and cfg.max_break_steps > 0
    needs_place_inventory = cfg.allow_place and cfg.max_place_steps > 0
    needs_pillar_inventory = cfg.allow_pillar and cfg.max_pillar_steps > 0
    needs_downward_inventory = cfg.allow_downward and cfg.max_downward_steps > 0
    if not needs_break_inventory and not needs_place_inventory and not needs_pillar_inventory and not needs_downward_inventory:
        return disabled

    start: int | None = 0
    slots: list[InventorySlot] = []
    try:
        while start is not None:
            page = body.perceive("inventory", {"start": start, "limit": 12})
            if not page.ok:
                return disabled
            slots.extend(InventorySlot.from_payload(dict(raw)) for raw in page.data.get("slots") or [])
            cursor = perception_next_cursor(page)
            if not page.complete and cursor is None:
                return disabled
            start = None if cursor is None else int(cursor)
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError, AssertionError):
        return disabled

    counts: dict[str, int] = {}
    for slot in slots:
        if slot.empty or not slot.item:
            continue
        item = str(slot.item).removeprefix("minecraft:")
        counts[item] = counts.get(item, 0) + int(slot.count)
    scaffold_item = next(
        (str(item).removeprefix("minecraft:") for item in cfg.scaffold_blocks if counts.get(str(item).removeprefix("minecraft:"), 0) > 0),
        None,
    )
    break_tools = {
        "break_pickaxe": _best_navigation_tool(counts, "pickaxe"),
        "break_axe": _best_navigation_tool(counts, "axe"),
        "break_shovel": _best_navigation_tool(counts, "shovel"),
    }
    if scaffold_item is None:
        return {**disabled, **break_tools, "inventory_complete": True}
    return {
        **break_tools,
        "has_scaffold": True,
        "allow_place": needs_place_inventory,
        "scaffold_item": scaffold_item,
        "scaffold_count": counts[scaffold_item],
        "inventory_complete": True,
    }


def _best_navigation_tool(counts: dict[str, int], tool: str) -> str | None:
    return next(
        (
            f"{material}_{tool}"
            for material in ("netherite", "diamond", "iron", "stone", "golden", "wooden")
            if counts.get(f"{material}_{tool}", 0) > 0
        ),
        None,
    )


def _executed_payload(segment: ExecutedSegment) -> dict[str, object]:
    diagnostics = {key: value for key, value in segment.diagnostics.items() if key != "planned_segment"}
    return {
        "index": segment.index,
        "status": segment.status,
        "target": list(segment.target) if segment.target is not None else None,
        "terminal_reason": segment.terminal_reason,
        "success": segment.success,
        "action_id": segment.action_id,
        "diagnostics": diagnostics,
    }



def _wait_for_reflex_completion(
    body: Body,
    *,
    timeout_s: float,
    after_seq: int | None = None,
) -> Event | None:
    deadline = monotonic() + timeout_s
    buffered = _match_reflex_completion(getattr(body, "event_log", ()), after_seq=after_seq)
    if buffered is not None:
        return buffered
    while monotonic() < deadline:
        buffered = _match_reflex_completion(body.poll_events(), after_seq=after_seq)
        if buffered is not None:
            return buffered
        sleep(0.05)
    return None


def _match_reflex_completion(
    events: list[Event] | tuple[Event, ...],
    *,
    after_seq: int | None = None,
) -> Event | None:
    for event in events:
        if after_seq is not None and event.seq <= after_seq:
            continue
        if event.name == "reflexCompleted":
            return event
        if event.name in {"death", "bodyMissing", "respawned"}:
            return event
    return None


def _survival_reflex_settle_timeout(segment_timeout_s: float) -> float:
    return min(5.0, max(0.25, segment_timeout_s / 3.0))


def _survival_reflex_active(state: BodyState) -> bool:
    return state.body_owner in SURVIVAL_REFLEX_OWNERS


def _wait_for_survival_reflex_idle(body: Body, *, timeout_s: float) -> SurvivalReflexSettle:
    started = monotonic()
    deadline = started + timeout_s
    events: list[dict[str, object]] = []
    state = body.get_state()
    while _survival_reflex_active(state):
        if monotonic() >= deadline:
            break
        for event in body.poll_events():
            if event.name in {"reflexStarted", "reflexCompleted", "ownerPreempted", "death", "bodyMissing", "respawned"}:
                events.append({"event": event.name, "seq": event.seq, "tick": event.tick, "data": dict(event.data)})
        if monotonic() >= deadline:
            break
        sleep(min(0.05, max(0.0, deadline - monotonic())))
        state = body.get_state()
    return SurvivalReflexSettle(
        state=state,
        idle=not _survival_reflex_active(state),
        owner=state.body_owner,
        pending_action_count=state.pending_action_count,
        waited_s=monotonic() - started,
        events=tuple(events),
    )



def _xyz_pos(pos: Position | tuple[float, float, float]) -> tuple[float, float, float]:
    return (float(pos[0]), float(pos[1]), float(pos[2]))


def _danger_block_pos(pos: tuple[float, float, float]) -> Position:
    return (int(floor(pos[0])), int(floor(pos[1])), int(floor(pos[2])))


def _dodge_entities(facts):
    found = []
    for key in ("nearby_hostiles", "hostiles", "nearby_entities", "entities", "entities_near"):
        items = facts.get(key) or []
        if isinstance(items, dict):
            items = items.get("entities", items.get("items", []))
        for mob in items:
            if (isinstance(mob, dict) and (mob.get("hostile") or mob.get("boss") or mob.get("aggressive"))
                    and mob.get("health", 1) != 0):
                pos = mob.get("pos", mob.get("position"))
                if isinstance(pos, (list, tuple)) and len(pos) == 3:
                    kind = str(mob.get("type") or mob.get("name") or "").removeprefix("minecraft:")
                    if kind == "creeper" and (mob.get("charged") or mob.get("powered")):
                        kind = "charged_creeper"
                    found.append({**mob, "id": mob.get("id", mob.get("uuid")),
                                  "type": kind, "pos": _xyz_pos(pos)})
    return found


def _dodge_paths(origin, facts):
    paths = {}
    for step in facts.get("steps") or []:
        to = step.get("to") if isinstance(step, dict) else None
        if not isinstance(to, (list, tuple)) or len(to) != 3:
            continue
        x, y, z = (int(value) for value in to)
        dx, dz = x - origin[0], z - origin[2]
        # The existing native step ray proves traversal. Keep this dodge on
        # level ground; it must not turn a native three-block drop into escape.
        if y != origin[1] or not (dx or dz) or (dx and dz and abs(dx) != abs(dz)):
            continue
        sx, sz = (dx > 0) - (dx < 0), (dz > 0) - (dz < 0)
        path = []
        for index in range(1, max(abs(dx), abs(dz)) + 1):
            cell = (origin[0] + sx * index, origin[1], origin[2] + sz * index)
            path.append(cell)
            paths[cell] = tuple(path)
    return paths


def _dodge_place_clear(end, origin, facts, *, leaving=False):
    clouds = [facts["cloud"]] if isinstance(facts.get("cloud"), dict) else []
    clouds += [mob for mob in facts.get("entities") or []
               if isinstance(mob, dict) and str(mob.get("type", "")).removeprefix("minecraft:") == "area_effect_cloud"]
    for cloud in clouds:
        pos, radius = cloud.get("pos"), cloud.get("radius")
        if not isinstance(pos, (list, tuple)) or len(pos) != 3 or not isinstance(radius, (int, float)):
            continue
        # Same player half-width/vertical contact envelope as step_options.
        inside = abs(end[1] - pos[1]) <= 2 and hypot(end[0] - pos[0], end[2] - pos[2]) <= radius + 0.3
        if inside and (not leaving or dist(end, pos) < dist(origin, pos)):
            return False
    if facts.get("hurt_here") and not leaving and dist(end, origin) <= DODGE_MARGIN:
        return False
    return True


def _move_away_candidates(
    current: Position,
    danger: tuple[float, float, float] | list[tuple[float, float, float]],
    *,
    candidate_radii: tuple[int, ...],
    min_distance: float,
    max_candidates: int,
) -> tuple[Position, ...]:
    candidates: list[tuple[float, float, Position]] = []
    offsets = (
        (1, 0),
        (-1, 0),
        (0, 1),
        (0, -1),
        (1, 1),
        (1, -1),
        (-1, 1),
        (-1, -1),
    )
    hazards = danger if isinstance(danger, list) else [danger]
    for radius in candidate_radii:
        for dx, dz in offsets:
            target = (current[0] + dx * radius, current[1], current[2] + dz * radius)
            center = (target[0] + 0.5, float(target[1]), target[2] + 0.5)
            distances = [dist(center, hazard) for hazard in hazards]
            if min(distances) < min_distance:
                continue
            travel_distance = dist((current[0] + 0.5, float(current[1]), current[2] + 0.5), center)
            # Away from all hazards together, nearer ones weighing more
            # (AltoClef GoalRunAwayFromEntities); one hazard ranks by distance.
            pressure = sum(1.0 / max(value, 0.1) for value in distances)
            candidates.append((pressure, travel_distance, target))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    seen: list[Position] = []
    ordered: list[Position] = []
    for _pressure, _travel_distance, target in candidates:
        if target in seen:
            continue
        seen.append(target)
        ordered.append(target)
        if len(ordered) >= max_candidates:
            break
    return tuple(ordered)


def _recovery_goal_candidates(
    origin: Position,
    original_goal: Position,
    *,
    distances: tuple[int, ...],
    offsets: tuple[tuple[int, int], ...],
    y_offsets: tuple[int, ...],
) -> tuple[Position, ...]:
    if not distances or not offsets or not y_offsets:
        return ()
    ranked: list[tuple[int, int, int, Position]] = []
    for distance_value in distances:
        if distance_value < 1:
            continue
        for dx, dz in offsets:
            if dx == 0 and dz == 0:
                continue
            for y_offset in y_offsets:
                candidate = (
                    origin[0] + dx * distance_value,
                    origin[1] + y_offset,
                    origin[2] + dz * distance_value,
                )
                goal_distance = abs(candidate[0] - original_goal[0]) + abs(candidate[2] - original_goal[2])
                ranked.append((goal_distance, distance_value, abs(y_offset), candidate))
    ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    ordered: list[Position] = []
    for _goal_distance, _distance_value, _y_offset, candidate in ranked:
        if candidate in ordered:
            continue
        ordered.append(candidate)
        if len(ordered) >= SERVER_GOAL_SET_LIMIT:
            break
    return tuple(ordered)


def _selected_candidate(result: ToolResult, candidates: tuple[Position, ...]) -> Position:
    metrics = dict(result.metrics or {})
    raw = metrics.get("selected_goal", metrics.get("goal"))
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        selected = (int(raw[0]), int(raw[1]), int(raw[2]))
        if selected in candidates:
            return selected
    return candidates[0]
