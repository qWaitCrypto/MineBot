"""What the Agent reads of its plan ladder each model call (plan-ladder-20261028.md §7.4-§7.5).

The ladder is the life profile's ``projects``; the run states come from the
project log.  Plan ends that do not wake the Agent are kept here as notices
and handed to it at the start of its next turn.  This section is the one
place that lists the items: every live item always has a line, shortened
before any is left out, and finished items are named once as off the
ladder (s7-later-20261029.md §8 D4-D5).
"""
from __future__ import annotations

import calendar
import json
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from minebot.app.life_profile import off_ladder

LADDER_SECTION_CHARS = 4000
SHOWN_NOTICES = 8
KEPT_NOTICES = 32
_LINE_CHARS = 300
# Item lines get shorter in steps until the section fits: all full; items
# not walking now short (then the notices go); every item short; id and
# state only (s7-later-20261029.md §8 D4).
_INTENT_CHARS, _SHORT_INTENT_CHARS, _WHEN_CHARS = 160, 80, 120
_STATE_WORDS = {"waiting": "waiting", "running": "running", "set_aside": "set aside", "done": "done"}
_OUTCOME_WORDS = {"needs_agent": "handed back", "put_down": "put down", "yielded": "taken over by you"}
_OFF_WORDS = {"completed": "completed", "done_by_you": "done by you"}


class LadderNotices:
    """Plan ends that tell the Agent without waking it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[dict[str, Any]] = []
        self._delivered: list[dict[str, Any]] = []
        # When the previous and the current turn started, on the monotonic
        # clock the Body loop's shot log uses; None before those turns.
        self.last_turn_started: float | None = None
        self.turn_started: float | None = None

    def add(self, fact: Mapping[str, Any]) -> None:
        with self._lock:
            self._pending.append({"at": time.time(), **notice_fields(fact)})
            del self._pending[:-KEPT_NOTICES]

    def take(self) -> list[dict[str, Any]]:
        """The notices for the turn that is starting; they stay shown until the next one."""
        with self._lock:
            self._delivered, self._pending = self._pending, []
            self.last_turn_started, self.turn_started = self.turn_started, time.monotonic()
            return list(self._delivered)

    def since_last_turn(self) -> list[dict[str, Any]]:
        """Newest first: those handed over at this turn's start and any since."""
        with self._lock:
            return list(reversed(self._delivered + self._pending))


def notice_fields(fact: Mapping[str, Any]) -> dict[str, Any]:
    """The four things a notice says: which item, how it ended, why, the Body at the end."""
    ended = fact.get("ended") if isinstance(fact.get("ended"), Mapping) else {}
    protection = fact.get("kind") == "protection"
    tool = fact.get("tool")
    out: dict[str, Any] = {"item": (f"protection:{tool}" if protection and tool else
                                     fact.get("item") or fact.get("project") or fact.get("activity_id")),
                           "result": ended.get("outcome") or fact.get("status")}
    reason = ended.get("reason") or fact.get("reason") or fact.get("error")
    if reason:
        out["reason"] = str(reason)[:300]
    if fact.get("status") == "step_reported":
        out["result"] = "step_reported"
        out["step"] = {key: fact[key] for key in ("node", "tool", "result") if key in fact}
    # The Body at the end and what the run changed (s7-later-20261029.md §8 D4).
    for key in ("position", "dimension", "inventory_counts", "inventory_change", "stopped_at", "reported_steps",
                "duration_s"):
        if key in ended:
            out[key] = ended[key]
    if protection:
        if tool:
            out["tool"] = tool
        if fact.get("reason"):
            out["reason"] = str(fact["reason"])[:300]
        for key in ("position", "health_before", "health_after"):
            if key in fact:
                out[key] = fact[key]
    missing = fact.get("missing_facts")
    if isinstance(missing, list) and missing:
        out["missing_facts"] = missing
    if fact.get("off_ladder"):
        out["off_ladder"] = fact["off_ladder"]
    if fact.get("activity_id"):
        out["activity_id"] = fact["activity_id"]
    return out


def notices_message(notices: list[Mapping[str, Any]]) -> str:
    """One message at the start of a turn, newest first."""
    rows = [{key: value for key, value in notice.items() if key != "at"} for notice in reversed(notices)]
    return (f"NOTICES ({len(rows)} plan ends since your last turn, newest first; they did not wake you): "
            + json.dumps(rows, ensure_ascii=False, sort_keys=True))


def ladder_section(record: Mapping[str, Any], state_of: Callable[[Mapping[str, Any]], Mapping[str, Any]],
                   *, timeline: Mapping[str, Any] | None = None, notices: list[Mapping[str, Any]] = (),
                   held: bool = False, now: float | None = None,
                   account_of: Callable[[str], Mapping[str, Any]] | None = None) -> str:
    now = time.time() if now is None else now
    profile = record.get("profile") or {}
    head = "LADDER (your plan, in order; states are the Body's own record):"
    if held:
        # Held is not hands off: run-3's Agent read HELD as the reflex layer
        # standing down for the night (closed-loop-architecture-20261107.md §1).
        head += (" HELD by take_control: no item starts until you resume or delegate; "
                 "the reflex layer still protects the Body: it may fight, flee or surface when it reads danger; your stance tells it where the Body should stay.")
    items: list[tuple[Mapping[str, Any], Mapping[str, Any] | None]] = []
    finished: list[str] = []
    for item in profile.get("projects") or []:
        run = state_of(item) if isinstance(item.get("work"), Mapping) else None
        why = off_ladder(item, run or {})
        if why is None:
            if run is not None and account_of is not None:
                run = {**run, "account": account_of(str(item.get("id")))}
            items.append((item, run))
        else:
            finished.append(f"{item.get('id')} ({_OFF_WORDS.get(why, why)})")
    tail = []
    if not items:
        tail.append("- empty: write items with update_life_project or delegate_work.")
    if finished:
        # Until the Agent's next write drops them; every one is named.
        tail.append(f"- off the ladder now: {', '.join(finished)}. read_life lists finished items; "
                    "writing one's id again (standing, if it should repeat) brings it back.")
    rules = profile.get("rules") or []
    if rules:
        tail.append(f"- rules: {len(rules)} standing rules (see read_life).")
    if timeline:
        tail.append("BODY TIMELINE: " + _timeline(timeline, now))
    notice_lines = [_notice_line(notice, now) for notice in notices[:SHOWN_NOTICES]]
    omitted = max(0, len(notices) - SHOWN_NOTICES)

    def render(detail: int) -> str:
        lines = [head, *(_item_line(item, run, now, detail) for item, run in items), *tail]
        return "\n".join(lines + _notice_block(notices, notice_lines, omitted))

    # No item is ever left out: lines of items not walking now get short,
    # then the notices go (the turn's NOTICES message has them all), then
    # every line gets short, then only ids and states are left.
    for detail in (0, 1):
        text = render(detail)
        if len(text) <= LADDER_SECTION_CHARS:
            return text
    while notice_lines:
        notice_lines.pop()
        omitted += 1
        text = render(1)
        if len(text) <= LADDER_SECTION_CHARS:
            return text
    for detail in (2, 3):
        text = render(detail)
        if len(text) <= LADDER_SECTION_CHARS:
            return text
    return text


def _item_line(item: Mapping[str, Any], run: Mapping[str, Any] | None, now: float, detail: int = 0) -> str:
    """One item: its state, what it is, when it fits, and the Body's record of its last run.

    The intent and when are the Agent's own words and are cut first; the
    Body's record stays whole while the item walks.
    """
    maturity = "walk" if isinstance(item.get("work"), Mapping) else "look" if item.get("when") or item.get(
        "area") else "think"
    status = str(item.get("status") or "active")
    walking = status == "active" and run is not None
    short = detail >= 2 or (detail == 1 and not walking)
    words = [f"{item.get('id')} [{maturity}]"]
    if detail < 3 and item.get("intent"):
        words[0] += " " + _cut(str(item["intent"]), _SHORT_INTENT_CHARS if short else _INTENT_CHARS)
    if item.get("when") and not short:
        when = _cut(str(item["when"]).rstrip("."), _WHEN_CHARS)
        words.append(f"Fits when {when}" + ("" if when.endswith("\u2026") else "."))
    prefix = "- "
    if status != "active":
        prefix += f"{status} by you: "
    elif run is not None:
        state = str(run.get("state") or "waiting")
        since = _at(run.get("since"))
        if state == "running" and since is not None:
            prefix += f"running {_duration(now - since)}: "
        elif state == "set_aside" and since is not None:
            prefix += f"set aside {_duration(now - since)} ago: "
        else:
            prefix += f"{_STATE_WORDS.get(state, state)}: "
        last = run.get("last_result")
        if isinstance(last, Mapping) and state in {"set_aside", "done", "waiting"} and detail < 3:
            text = "Last: " + _outcome_words(last, brief=short)
            if state == "set_aside":
                text += "; not changed since"
            elif state == "done":
                text += "; it stays until you mark it done, change it or remove it"
            words.append(text + ".")
        account = _account_words(run.get("account"), brief=short) if detail < 3 else None
        if account:
            words.append(account)
    skills = [str(name) for name in item.get("skills") or ()]
    if skills and detail < 3:
        # The Skills mounted on this item (agent-skills-v2-20261105.md §2.1).
        words.append("skills: " + ", ".join(skills))
    if item.get("standing"):
        words.append("(standing)")
    return prefix + " ".join(words)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"


def _outcome_words(row: Mapping[str, Any], *, brief: bool = False) -> str:
    """How a run ended in the row's own facts: the outcome, the step it stopped on or
    the steps it reported, and what changed in the inventory."""
    text = str(row.get("outcome") or row.get("result"))
    stopped = row.get("stopped_at") if isinstance(row.get("stopped_at"), Mapping) else row.get("step")
    if isinstance(stopped, Mapping):
        text += " at " + _step_text(stopped)
    elif row.get("reason"):
        text += f" ({str(row['reason'])[:120]})"
    steps = [step for step in row.get("reported_steps") or [] if isinstance(step, Mapping)]
    if steps:
        text += f" with {len(steps)} reported step{'' if len(steps) == 1 else 's'}"
        if not brief:
            text += " (" + "; ".join(_step_text(step) for step in steps[:3]) + ")"
    change = row.get("inventory_change")
    if isinstance(change, Mapping) and not brief:
        text += "; inventory " + (_change_text(change) if change else "unchanged")
    return text


def _account_words(account: Any, *, brief: bool = False) -> str | None:
    """The item's account over all its runs, once it has run twice (s7-later-20261029.md §8 D6)."""
    if not isinstance(account, Mapping) or int(account.get("runs") or 0) < 2:
        return None
    outcomes = sorted((account.get("outcomes") or {}).items(), key=lambda pair: (-pair[1], pair[0]))
    text = (f"{account['runs']} runs, {_duration(float(account.get('seconds') or 0))}: "
            + ", ".join(f"{count} {_OUTCOME_WORDS.get(name, name)}" for name, count in outcomes))
    if not brief:
        same = account.get("same_end_twice")
        if isinstance(same, Mapping):
            cause = _step_text(same) if same.get("node") is not None else str(same.get("reason"))[:120]
            text += f"; the last two ended at the same step for the same reason ({cause})"
        rewrites = int(account.get("rewrites") or 0)
        if rewrites:
            text += f"; rewritten {rewrites} time{'' if rewrites == 1 else 's'}"
    return text + "."


def _step_text(step: Mapping[str, Any]) -> str:
    text = str(step.get("node"))
    if step.get("tool"):
        text += f" {step['tool']}"
    cause = step.get("result")
    if isinstance(cause, Mapping):  # a step_reported notice carries the call's whole result
        cause = cause.get("reason")
    cause = cause or step.get("why")
    return text + (f": {str(cause)[:120]}" if cause else "")


def _change_text(change: Mapping[str, Any], shown: int = 6) -> str:
    parts = [f"{count:+d} {name}" for name, count in change.items() if isinstance(count, int)]
    return ", ".join(parts[:shown]) + (f" and {len(parts) - shown} more" if len(parts) > shown else "")


def _timeline(timeline: Mapping[str, Any], now: float) -> str:
    busy = timeline.get("busy_since")
    if isinstance(busy, (int, float)):
        return (f"busy since {time.strftime('%H:%M:%S', time.localtime(busy))} "
                f"({_duration(now - busy)}, {int(timeline.get('runs') or 0)} runs); idle 0s.")
    idle = timeline.get("idle_since")
    if isinstance(idle, (int, float)):
        return f"idle since {time.strftime('%H:%M:%S', time.localtime(idle))} ({_duration(now - idle)})."
    return "unknown."


def _notice_block(notices: list[Mapping[str, Any]], lines: list[str], omitted: int) -> list[str]:
    if not notices:
        return []
    block = [f"NOTICES since your last turn: {len(notices)} (newest first):", *lines]
    if omitted:
        block.append(f"- ... {omitted} earlier notices not shown.")
    return block


def _notice_line(notice: Mapping[str, Any], now: float) -> str:
    at = notice.get("at")
    ago = f"{_duration(now - at)} ago" if isinstance(at, (int, float)) else ""
    text = f"- {ago}: {notice.get('item')} {_outcome_words(notice)}"
    if notice.get("off_ladder"):
        text += "; off your ladder now"
    return text[:_LINE_CHARS]


def _at(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
        except ValueError:
            return None
    return None


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


_SHOOTERS = (("decision", "by Decision on its own (protective shots; no call, plan or rule of yours asked for them)"),
             ("rule", "by Decision carrying out your rules"),
             ("you", "by your own calls and plans"))


def _by_target(shots: list[Mapping[str, Any]]) -> str:
    counts: dict[str, list[int]] = {}
    for shot in shots:
        name = str(shot.get("target_type") or "unknown").removeprefix("minecraft:").replace("_", " ")
        pair = counts.setdefault(name, [0, 0])
        pair[0] += 1
        pair[1] += 1 if shot.get("hit") else 0
    ordered = sorted(counts.items(), key=lambda item: (-item[1][0], item[0]))
    return ", ".join(f"{name} {shots} ({hits} hit)" for name, (shots, hits) in ordered)


def shots_section(log: list[Mapping[str, Any]], *, since: float | None = None) -> str | None:
    """Who loosed the Body's arrows and at what, this session and since the
    Agent's last turn.  The Agent saw only the latest shot, so 78 arrows
    Decision put into a flying dragon reached it as a falling arrow count
    (End run-31, dragon-unit-tests-20261030 §6.y.41).  Facts only."""
    if not log:
        return None
    lines = [f"SHOTS (arrows the Body loosed this session, kept across deaths): {len(log)}."]
    for key, words in _SHOOTERS:
        mine = [shot for shot in log if shot.get("by", "you") == key]
        if mine:
            lines.append(f"- {words}: {len(mine)}: {_by_target(mine)}.")
    if since is not None:
        recent = [shot for shot in log if isinstance(shot.get("released"), (int, float)) and shot["released"] >= since]
        if not recent:
            lines.append("- since your last turn: none.")
        for key, _ in _SHOOTERS:
            mine = [shot for shot in recent if shot.get("by", "you") == key]
            if mine:
                who = {"decision": "Decision on its own", "rule": "Decision for your rules", "you": "you"}[key]
                lines.append(f"- since your last turn, {who}: {len(mine)}: {_by_target(mine)}.")
    return "\n".join(lines)


__all__ = ["LADDER_SECTION_CHARS", "LadderNotices", "ladder_section", "notice_fields", "notices_message",
           "shots_section"]
