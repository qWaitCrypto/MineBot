"""Shared streaming readers and measurements.

The only MineBot import is trace_facts, a standard-library trace codec.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import re
import struct

from minebot.app.trace_facts import TraceFactsReader

EVENT = re.compile(r'"event"\s*:\s*"([^"\\]+)"')
TS = re.compile(r'"ts"\s*:\s*([0-9.]+)')
FACT_EVENTS = {"call_supervision_request", "reflex_death"}


def trace_events(path: Path, names: set[str]):
    """Inspect event-name substrings before decoding; never load a whole trace."""
    # Newer traces keep these events' raw facts in earlier frame_facts lines.
    wanted = set(names) | ({"frame_facts"} if names & FACT_EVENTS else set())
    facts = TraceFactsReader()
    needles = tuple(f'"{name}"' for name in wanted)
    with path.open(encoding="utf-8", errors="replace") as stream:
        for number, line in enumerate(stream, 1):
            if not any(needle in line for needle in needles):
                continue
            match = EVENT.search(line)
            if match and match[1] in wanted:
                try:
                    row = facts.decode(json.loads(line))
                except ValueError as exc:
                    raise ValueError(f"invalid trace JSON at line {number}") from exc
                if row is not None:
                    yield row


def trace_bounds(path: Path) -> tuple[float, float]:
    # Timestamps inside payloads are not run boundaries. Decode only the first
    # and last outer event, retaining one line at a time even for huge traces.
    first, last_line = None, None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            # frame_facts lines carry no ts of their own.
            if '\"event\"' not in line or '"frame_facts"' in line[:40]:
                continue
            if first is None:
                first = json.loads(line)["ts"]
            last_line = line
    if first is None:
        raise ValueError("trace has no events")
    return first, json.loads(last_line)["ts"]


def json_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def timestamp(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    # Minecraft advancement times include offsets, e.g. +0800.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone offset")
    return parsed.timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def quantiles(values: list[float]) -> dict:
    """Use the old eval-final.py sorted-index convention, including upper median."""
    ordered = sorted(values)
    return {"p50": ordered[len(ordered) // 2] if ordered else None,
            "p90": ordered[min(len(ordered) - 1, int(len(ordered) * .9))] if ordered else None}


def compact(value, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text[:limit]


def union(intervals: list[tuple[float, float]]) -> list[list[float]]:
    out = []
    for start, end in sorted(intervals):
        if end < start:
            continue
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out


BUSY_EVENTS = {"reflex_state_changed", "llm_start", "llm_end", "agent_start", "agent_end",
               "turn_cancelled", "tool_start", "tool_end", "tool_result",
               "reflex_plan_started_from_queue", "reflex_activity_finished", "reflex_activity_stopped"}


class BusyTime:
    """busy.py's six priority categories, integrated at exact event boundaries.

    Open intervals are clipped to the trace end; cancelled turns close at the
    cancellation. Body work is the explicit reflex state 'work', not motion
    inferred from positions. 'idle' means the complement of that work state.
    """

    labels = ("body working", "plan running, between calls", "Agent thinking (LLM), no plan",
              "Agent tool (read/write), no plan", "Agent turn other", "no turn, no plan (asleep/waiting)")

    def __init__(self):
        self.open = {}
        self.intervals = defaultdict(list)
        self.activities = {}
        self.tools = defaultdict(list)

    def consume(self, row: dict) -> None:
        event, ts = row["event"], row["ts"]
        if event == "reflex_state_changed":
            if row.get("to") == "work":
                self.open.setdefault(0, ts)
            elif row.get("from") == "work":
                self.close(0, ts)
        elif event == "llm_start":
            self.open.setdefault(2, ts)
        elif event == "llm_end":
            self.close(2, ts)
        elif event == "agent_start":
            self.open.setdefault(4, ts)
        elif event in {"agent_end", "turn_cancelled"}:
            self.close(4, ts)
            if event == "turn_cancelled":
                self.close(2, ts)
        elif event == "tool_start":
            self.tools[row.get("tool")].append(ts)
        elif event == "tool_end":
            stack = self.tools[row.get("tool")]
            if stack:
                self.intervals[3].append((stack.pop(), ts))
        elif event == "tool_result" and row.get("tool") == "delegate_work" and row.get("reason") == "accepted":
            aid = (row.get("full_result") or {}).get("metrics", {}).get("activity_id")
            if aid:
                self.activities[aid] = ts
        elif event == "reflex_plan_started_from_queue":
            self.activities[row["activity_id"]] = ts
        elif event in {"reflex_activity_finished", "reflex_activity_stopped"}:
            start = self.activities.pop(row.get("activity_id"), None)
            if start is not None:
                self.intervals[1].append((start, ts))

    def close(self, category: int, ts: float) -> None:
        start = self.open.pop(category, None)
        if start is not None:
            self.intervals[category].append((start, ts))

    def finish(self, start: float, end: float) -> dict:
        for category in list(self.open):
            self.close(category, end)
        self.intervals[1].extend((ts, end) for ts in self.activities.values())
        self.intervals[3].extend((ts, end) for stack in self.tools.values() for ts in stack)
        edges = defaultdict(list)
        for category, intervals in self.intervals.items():
            for a, b in union(intervals):
                a, b = max(start, a), min(end, b)
                if a < b:
                    edges[a].append((category, 1))
                    edges[b].append((category, -1))
        active, totals, previous = Counter(), Counter(), start
        for ts in sorted({start, end, *edges}):
            category = next((k for k in range(5) if active[k]), 5)
            totals[self.labels[category]] += ts - previous
            for key, delta in edges[ts]:
                active[key] += delta
            previous = ts
        busy = totals[self.labels[0]]
        return {"busy_s": busy, "idle_s": end - start - busy, "categories_s": dict(totals),
                "method": self.__doc__.strip()}


def read_level(path: Path) -> dict:
    """Read a gzip NBT level.dat with a small standard-library tag reader."""
    with gzip.open(path, "rb") as stream:
        def unpack(fmt):
            return struct.unpack(fmt, stream.read(struct.calcsize(fmt)))[0]

        def string():
            return stream.read(unpack(">H")).decode("utf-8", errors="replace")

        def payload(kind):
            if kind in range(1, 7):
                return unpack({1: ">b", 2: ">h", 3: ">i", 4: ">q", 5: ">f", 6: ">d"}[kind])
            if kind == 8:
                return string()
            if kind == 9:
                subtype, size = unpack(">B"), unpack(">i")
                return [payload(subtype) for _ in range(size)]
            if kind == 10:
                result = {}
                while (subtype := unpack(">B")):
                    name = string()
                    result[name] = payload(subtype)
                return result
            if kind in {7, 11, 12}:
                size = unpack(">i")
                width = {7: 1, 11: 4, 12: 8}[kind]
                stream.read(size * width)
                return None
            raise ValueError(f"unsupported NBT tag {kind}")

        kind = unpack(">B")
        string()
        root = payload(kind)
        return root.get("Data", root.get("data", root))


def player_files(server: Path, world: Path, bot: str) -> tuple[Path | None, Path | None]:
    """Select a player's UUID, never combine different players' statistics."""
    uuids = set()
    cache = server / "usercache.json"
    if cache.exists():
        for player in json.loads(cache.read_text()):
            if player.get("name", "").casefold() == bot.casefold():
                uuids.add(player["uuid"])
    # Offline servers have a deterministic Java UUID.nameUUIDFromBytes.
    import hashlib
    import uuid
    uuids.add(str(uuid.UUID(bytes=hashlib.md5(f"OfflinePlayer:{bot}".encode()).digest(), version=3)))
    result = []
    for kind in ("advancements", "stats"):
        files = sorted({f for parent in (world / kind, world / "players" / kind) for f in parent.glob("*.json")})
        matches = [f for f in files if f.stem in uuids]
        if len(matches) > 1:
            raise ValueError(f"ambiguous {kind} files for {bot}")
        if not matches and files:
            raise ValueError(f"no UUID match in {kind} for {bot}; refusing to mix players")
        result.append(matches[0] if matches else None)
    return tuple(result)


LOG_TIME = re.compile(r"^\[(\d\d:\d\d:\d\d)\].*?\]: (.*)$")
DEATH_TEXT = re.compile(r"(?:was |drowned|fell |hit the ground|burned |went up in |tried to |"
                        r"blew up|died|starved|suffocated|withered|froze |experienced kinetic|"
                        r"discovered the floor|walked into|didn't want to|left the confines|"
                        r"was poked|was impaled)")


def server_events(server: Path, world: str, bot: str, start: float, end: float, tz) -> list[dict]:
    """Use rotation filename dates (latest.log mtime) and handle midnight rollover.

    Logs must establish Preparing level WORLD before a death/difficulty line is
    accepted. Duplicate lines shared by rotated/latest logs are deduplicated.
    """
    results, seen = [], set()
    logs = sorted((server / "logs").glob("*.log.gz")) + [server / "logs" / "latest.log"]
    for path in logs:
        if not path.exists():
            continue
        match = re.match(r"(\d{4}-\d\d-\d\d)", path.name)
        day = (datetime.fromisoformat(match[1]).date() if match else
               datetime.fromtimestamp(path.stat().st_mtime, tz).date())
        # Rotated files can span days. Use the filename's starting date;
        # latest.log's ending date is corrected after collecting clocks.
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as stream:
            lines = [(m[1], m[2], number) for number, line in enumerate(stream, 1)
                     if (m := LOG_TIME.match(line))]
        rollovers = sum(a[0] > b[0] for a, b in zip(lines, lines[1:]))
        if not match:
            day -= timedelta(days=rollovers)
        current_world, previous = None, None
        for clock, text, number in lines:
            if previous is not None and clock < previous:
                day += timedelta(days=1)
            previous = clock
            level = re.search(r'Preparing level "([^"]+)"', text)
            if level:
                current_world = level[1]
            if current_world != world:
                continue
            ts = datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=tz).timestamp()
            if not start - 300 <= ts <= end + 5:
                continue
            kind = None
            if text.startswith(bot + " ") and DEATH_TEXT.match(text[len(bot) + 1:]):
                kind = "death"
            elif "difficulty has been set to " in text:
                kind = "difficulty"
            if kind and (ts, text) not in seen:
                seen.add((ts, text))
                results.append({"ts": ts, "kind": kind, "text": text,
                                "source": f"{path}:{number}"})
    return sorted(results, key=lambda r: r["ts"])
