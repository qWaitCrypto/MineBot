package dev.minebot.body.nav;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;
import java.util.Set;

/**
 * Bounded local planner for reaching a buried interaction target. It plans a
 * dry, supported player volume, not a ray to one block: every horizontal or
 * stair step clears both feet and head cells, while a same-column descent
 * clears the floor and lets gravity settle the player onto verified support.
 *
 * <p>The plan is optimistic about solid blocks only. Execution must obtain a
 * fresh governance verdict for every blocker before changing the world.</p>
 */
public final class ExcavationPlanner {
    public static final int DEFAULT_NODE_CAP = 12_000;
    public static final int DEFAULT_STEP_CAP = 24;
    private static final double MOVE_COST = 4.6;
    private static final double VERTICAL_COST = 1.5;
    private static final double BREAK_COST = 20.0;
    private static final int[][] CARDINALS = {{1, 0}, {-1, 0}, {0, 1}, {0, -1}};

    public record Cell(int x, int y, int z) {
    }

    public record Target(int x, int y, int z) {
        public Cell cell() {
            return new Cell(x, y, z);
        }
    }

    public enum StepMode {
        WALK,
        DESCEND
    }

    public record Step(Cell stand, StepMode mode, List<Cell> blockers) {
        public Step {
            blockers = List.copyOf(blockers);
        }
    }

    public record Plan(Target target, List<Step> steps, int breakCount, int expandedNodes) {
        public Plan {
            steps = List.copyOf(steps);
        }
    }

    public record Result(Plan plan, String reason, int expandedNodes) {
        public boolean success() {
            return plan != null;
        }
    }

    private record State(Cell cell, int breaks, int steps) {
    }

    private record Parent(State previous, Step step) {
    }

    private record Open(State state, double g, double f) {
    }

    private record Transition(Step step, int breaks) {
    }

    private ExcavationPlanner() {
    }

    public static Result plan(
        WorldView world,
        Cell start,
        List<Target> targets,
        Set<Cell> deniedBlocks,
        int maxBreaks
    ) {
        return plan(
            world,
            start,
            targets,
            deniedBlocks,
            maxBreaks,
            DEFAULT_NODE_CAP,
            DEFAULT_STEP_CAP
        );
    }

    static Result plan(
        WorldView world,
        Cell start,
        List<Target> targets,
        Set<Cell> deniedBlocks,
        int maxBreaks,
        int nodeCap,
        int stepCap
    ) {
        if (targets.isEmpty()) {
            return new Result(null, "no_targets", 0);
        }
        if (maxBreaks <= 0) {
            return new Result(null, "break_budget_exhausted", 0);
        }

        Map<Cell, Target> terminals = terminalStands(world, targets);
        if (terminals.isEmpty()) {
            return new Result(null, "no_terminal_stands", 0);
        }
        Set<Cell> targetCells = new HashSet<>();
        for (Target target : targets) {
            targetCells.add(target.cell());
        }

        PriorityQueue<Open> open = new PriorityQueue<>((left, right) -> Double.compare(left.f(), right.f()));
        Map<State, Double> best = new HashMap<>();
        Map<State, Parent> parents = new HashMap<>();
        State initial = new State(start, 0, 0);
        best.put(initial, 0.0);
        open.add(new Open(initial, 0.0, heuristic(start, terminals.keySet())));
        int expanded = 0;

        while (!open.isEmpty() && expanded < nodeCap) {
            Open current = open.poll();
            Double known = best.get(current.state());
            if (known == null || current.g() > known + 1.0e-9) {
                continue;
            }
            Target terminal = terminals.get(current.state().cell());
            if (terminal != null) {
                List<Step> steps = reconstruct(current.state(), parents);
                return new Result(
                    new Plan(terminal, steps, current.state().breaks(), expanded),
                    "planned",
                    expanded
                );
            }
            if (current.state().steps() >= stepCap) {
                continue;
            }
            expanded++;

            for (Transition transition : transitions(
                world,
                current.state().cell(),
                terminals,
                targetCells,
                deniedBlocks
            )) {
                int nextBreaks = current.state().breaks() + transition.breaks();
                if (nextBreaks > maxBreaks) {
                    continue;
                }
                State next = new State(
                    transition.step().stand(),
                    nextBreaks,
                    current.state().steps() + 1
                );
                double nextG = current.g()
                    + MOVE_COST
                    + Math.abs(next.cell().y() - current.state().cell().y()) * VERTICAL_COST
                    + transition.breaks() * BREAK_COST;
                Double prior = best.get(next);
                if (prior != null && prior <= nextG) {
                    continue;
                }
                best.put(next, nextG);
                parents.put(next, new Parent(current.state(), transition.step()));
                open.add(new Open(next, nextG, nextG + heuristic(next.cell(), terminals.keySet())));
            }
        }
        return new Result(
            null,
            expanded >= nodeCap ? "node_budget_exhausted" : "no_excavation_route",
            expanded
        );
    }

    private static Map<Cell, Target> terminalStands(WorldView world, List<Target> targets) {
        Map<Cell, Target> terminals = new LinkedHashMap<>();
        for (Target target : targets) {
            for (int dy = -1; dy <= 1; dy++) {
                for (int[] direction : CARDINALS) {
                    addTerminal(
                        world,
                        terminals,
                        new Cell(target.x() + direction[0], target.y() + dy, target.z() + direction[1]),
                        target
                    );
                }
            }
            // Mine a floor target from above or a ceiling target from below.
            addTerminal(world, terminals, new Cell(target.x(), target.y() + 1, target.z()), target);
            addTerminal(world, terminals, new Cell(target.x(), target.y() - 2, target.z()), target);
        }
        return terminals;
    }

    private static void addTerminal(
        WorldView world,
        Map<Cell, Target> terminals,
        Cell stand,
        Target target
    ) {
        if (world.isBodyPositionHazardous(stand.x(), stand.y(), stand.z())) {
            return;
        }
        WorldView.NodeKind support = world.kindAt(stand.x(), stand.y() - 1, stand.z());
        if (support != WorldView.NodeKind.SOLID) {
            return;
        }
        if (!canClearBodyCell(world.kindAt(stand.x(), stand.y(), stand.z()))
            || !canClearBodyCell(world.kindAt(stand.x(), stand.y() + 1, stand.z()))) {
            return;
        }
        // Standing on the target is safe only when the post-mine landing is solid.
        if (stand.x() == target.x() && stand.y() == target.y() + 1 && stand.z() == target.z()) {
            if (world.kindAt(target.x(), target.y() - 1, target.z()) != WorldView.NodeKind.SOLID
                || world.isBodyPositionHazardous(target.x(), target.y(), target.z())) {
                return;
            }
        }
        terminals.putIfAbsent(stand, target);
    }

    private static List<Transition> transitions(
        WorldView world,
        Cell current,
        Map<Cell, Target> terminals,
        Set<Cell> targetCells,
        Set<Cell> deniedBlocks
    ) {
        List<Transition> transitions = new ArrayList<>();

        // A straight, supported shaft down costs one governed floor break.
        Cell below = new Cell(current.x(), current.y() - 1, current.z());
        Cell belowSupport = new Cell(current.x(), current.y() - 2, current.z());
        if (world.kindAt(belowSupport.x(), belowSupport.y(), belowSupport.z()) == WorldView.NodeKind.SOLID
            && world.kindAt(below.x(), below.y(), below.z()) == WorldView.NodeKind.SOLID
            && !targetCells.contains(below)
            && !deniedBlocks.contains(below)
            && !world.isBodyPositionHazardous(below.x(), below.y(), below.z())) {
            transitions.add(new Transition(
                new Step(below, StepMode.DESCEND, List.of(below)),
                1
            ));
        }

        for (int[] direction : CARDINALS) {
            for (int dy = -1; dy <= 1; dy++) {
                Cell destination = new Cell(
                    current.x() + direction[0],
                    current.y() + dy,
                    current.z() + direction[1]
                );
                Transition transition = walkTransition(
                    world,
                    current,
                    destination,
                    terminals,
                    targetCells,
                    deniedBlocks
                );
                if (transition != null) {
                    transitions.add(transition);
                }
            }
        }
        return transitions;
    }

    private static Transition walkTransition(
        WorldView world,
        Cell current,
        Cell destination,
        Map<Cell, Target> terminals,
        Set<Cell> targetCells,
        Set<Cell> deniedBlocks
    ) {
        if (world.isBodyPositionHazardous(destination.x(), destination.y(), destination.z())) {
            return null;
        }
        Cell support = new Cell(destination.x(), destination.y() - 1, destination.z());
        if (world.kindAt(support.x(), support.y(), support.z()) != WorldView.NodeKind.SOLID) {
            return null;
        }
        Target terminalTarget = terminals.get(destination);
        if (targetCells.contains(support)
            && (terminalTarget == null || !support.equals(terminalTarget.cell()))) {
            return null;
        }

        List<Cell> blockers = new ArrayList<>();
        if (destination.y() > current.y()) {
            Cell currentCap = new Cell(current.x(), current.y() + 2, current.z());
            if (!addBodyCell(world, currentCap, blockers, targetCells, deniedBlocks)) {
                return null;
            }
        }
        Cell head = new Cell(destination.x(), destination.y() + 1, destination.z());
        Cell feet = destination;
        // Clear high to low so opening a lower cell cannot drop a blocker onto the player.
        if (!addBodyCell(world, head, blockers, targetCells, deniedBlocks)
            || !addBodyCell(world, feet, blockers, targetCells, deniedBlocks)) {
            return null;
        }
        return new Transition(new Step(destination, StepMode.WALK, blockers), blockers.size());
    }

    private static boolean addBodyCell(
        WorldView world,
        Cell cell,
        List<Cell> blockers,
        Set<Cell> targetCells,
        Set<Cell> deniedBlocks
    ) {
        WorldView.NodeKind kind = world.kindAt(cell.x(), cell.y(), cell.z());
        if (kind == WorldView.NodeKind.PASSABLE || kind == WorldView.NodeKind.CLIMBABLE) {
            return true;
        }
        if (kind != WorldView.NodeKind.SOLID
            || targetCells.contains(cell)
            || deniedBlocks.contains(cell)) {
            return false;
        }
        if (!blockers.contains(cell)) {
            blockers.add(cell);
        }
        return true;
    }

    private static boolean canClearBodyCell(WorldView.NodeKind kind) {
        return kind == WorldView.NodeKind.PASSABLE
            || kind == WorldView.NodeKind.CLIMBABLE
            || kind == WorldView.NodeKind.SOLID;
    }

    private static double heuristic(Cell cell, Set<Cell> terminals) {
        double best = Double.MAX_VALUE;
        for (Cell terminal : terminals) {
            double dx = terminal.x() - cell.x();
            double dy = terminal.y() - cell.y();
            double dz = terminal.z() - cell.z();
            best = Math.min(best, Math.sqrt(dx * dx + dy * dy + dz * dz) * MOVE_COST);
        }
        return best;
    }

    private static List<Step> reconstruct(State state, Map<State, Parent> parents) {
        List<Step> reversed = new ArrayList<>();
        Parent parent = parents.get(state);
        while (parent != null) {
            reversed.add(parent.step());
            state = parent.previous();
            parent = parents.get(state);
        }
        Collections.reverse(reversed);
        return List.copyOf(reversed);
    }
}
