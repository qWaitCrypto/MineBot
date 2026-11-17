#!/usr/bin/env python3
"""Replay gate for reflex frame changes (ladder-item-consolidation-20261108 §4).

Recorded frames are recompiled from the trace's own compiler inputs with a
candidate tree; only frames whose request text changed are asked again, old
and new, and the answers compared with the live one.

    gate.py harvest --run run-6 TRACE --out corpus/run-6.jsonl
    gate.py recompile --tree TREE corpus/*.jsonl --out out/X.jsonl [--annotate]
    gate.py ask out/X.jsonl --out out/X-answers.jsonl [--concurrency 3] [--sample-identical 200]
    gate.py report out/X.jsonl [out/X-answers.jsonl] [--at run-6:13:30:4]

`--annotate` (baseline pass, same tree as the run) writes back per-frame
facts the loop had and the compiler input lacks: idle options the loop
withheld after a failed repeat, and which recorded life profile (the one
before or the one after the frame) the frame was compiled with. Later
passes read them from the corpus. One corpus file holds one run in order.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as _dt
import hashlib
import json
import random
import sys
import time
import os
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

URL = os.environ.get("MINEBOT_DECISION_URL", "http://127.0.0.1:10004/v1/systemone")
RECOMPILED = ("idle_frame", "running_frame", "result_frame", "default_result_frame")
# Facts BodyLoop._frame adds from the life store and runtime flags, carried
# from the nearest frame that recorded final facts (idle, result frames).
CARRY = ("life_profile", "life_intent", "agent_stance", "agent_thinking", "ladder_held", "ladder_states",
         "ladder_same_run", "known_portals", "own_beds")
# Loop memories that advance frame by frame (older trees lack some of them).
MEMORIES = ("_creeper_memory", "_shot_record", "_shot_log", "_placed_beds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def payload_key(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def hms(ts: float | None) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""


def read_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(dumps(row) + "\n")


def final_facts(row: dict[str, Any]) -> dict[str, Any] | None:
    """The complete facts the frame was compiled from, when the trace has them."""
    compiler_input = row.get("compiler_input") or {}
    frame_input = row.get("frame_input") or {}
    if isinstance(compiler_input.get("facts"), dict):
        return compiler_input["facts"]
    purpose = row.get("purpose")
    if purpose == "result_frame":
        facts = (compiler_input.get("decision_context") or {}).get("body_state")
    elif purpose == "default_result_frame":
        facts = (frame_input.get("execution") or {}).get("facts")
    else:
        facts = None
    return facts if isinstance(facts, dict) else None


def carry_of(final: dict[str, Any]) -> dict[str, Any]:
    carry = {key: copy.deepcopy(final[key]) for key in CARRY if key in final}
    carry["_present"] = [key for key in CARRY if key in final]
    return carry


# ----------------------------------------------------------------- harvest

def harvest(trace: Path, run: str, tree: str) -> list[dict[str, Any]]:
    sys.path.insert(0, tree)
    from minebot.app.trace_facts import read_trace
    rows: list[dict[str, Any]] = []
    pending: dict[tuple[str, str], dict[str, Any]] = {}
    requests: dict[Any, Any] = {}
    answers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with trace.open("rb") as stream:
        for event in read_trace(stream):
            kind = event.get("event")
            if kind == "decision_request":
                requests[event.get("request_id")] = event.get("payload")
            elif kind == "decision_response":
                payload = requests.pop(event.get("request_id"), None)
                response = event.get("response") or {}
                if payload is not None and isinstance(response.get("answers"), dict):
                    answers[payload_key(payload)].append(
                        {key: value.get("choice") for key, value in response["answers"].items()
                         if isinstance(value, dict)})
            elif kind == "call_supervision_request":
                purpose = event.get("purpose")
                if purpose == "done_frame":
                    continue
                row = {"id": f"{run}:{event.get('seq')}", "run": run, "ts": event.get("ts"), "t": hms(event.get("ts")),
                       "seq": event.get("seq"), "purpose": purpose, "tool": event.get("tool"),
                       "tool_call_id": event.get("tool_call_id"), "idle_s": event.get("idle_s"),
                       "loop_state": event.get("loop_state"), "request": event.get("request"),
                       "done_request": event.get("done_request"),
                       "compiler_input": event.get("compiler_input") or None,
                       "frame_input": event.get("frame_input") or None,
                       "carry": None, "carry_next": None, "live_choice": None, "live_unknown": None,
                       "live_answers": None}
                rows.append(row)
                pending[(str(event.get("tool_call_id")), str(purpose))] = row
            elif kind == "call_supervision_result":
                row = pending.pop((str(event.get("tool_call_id")), str(event.get("purpose"))), None)
                if row is not None:
                    row["live_choice"] = event.get("choice")
                    row["live_unknown"] = event.get("unknown")
    # Store-derived facts for frames without final facts: the nearest recorded
    # ones before (carry) and after (carry_next); the baseline pass picks.
    carry: dict[str, Any] | None = None
    for row in rows:
        final = final_facts(row)
        if final is not None:
            carry = carry_of(final)
        elif row["purpose"] in RECOMPILED:
            row["carry"] = copy.deepcopy(carry) if carry else None
    carry = None
    for row in reversed(rows):
        final = final_facts(row)
        if final is not None:
            carry = carry_of(final)
        elif row["purpose"] in RECOMPILED:
            row["carry_next"] = copy.deepcopy(carry) if carry else None
    for row in rows:
        if row["request"] is not None:
            found = answers.get(payload_key(row["request"]))
            if found:
                row["live_answers"] = found.pop(0)
    return rows


# ------------------------------------------------------------ simulations
# Some steps change how a fact is derived before it is recorded, not how the
# frame is compiled from it. The trace's events let the new derivation be
# replayed; the result is written on each frame and overlaid at recompile.

MOVING = {"leave_lava", "surface", "cushion_fall", "engage", "move_away"}
STAND_STILL_TOOLS = {"ranged_attack", "consume_item", "use_item", "equip_item", "use_on_entity", "use_on_block"}


def _stance_time(value: Any) -> float | None:
    try:
        return _dt.datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return None


def simulate_stance(trace: Path, run: str, rows: list[dict[str, Any]], tree: str,
                    moves_end: bool = False) -> Counter:
    """ladder-item-consolidation §3: the stance in a frame is the one a ladder
    item set by its first successful Body result, or the Agent's update_life
    stance when written later (an item without a stance leaves it as it is); a
    broken stance check, a death or the Agent clearing it ends whichever is
    current (v6; moves_end=True replays v5, where a successful protective
    move ended it too). Copies the old mechanism made of item stances (ladder_item_stance
    events) do not count as the Agent's writes."""
    sys.path.insert(0, tree)
    from minebot.app.trace_facts import read_trace
    by_id = {row["id"]: row for row in rows}
    profile: dict[str, Any] = {}
    pending: dict[str, Any] | None = None    # {"id", "text"} the running item, before its first success
    item_set: dict[str, Any] | None = None   # {"id", "text"|None, "ts"} the latest item result that set it
    copied: set[tuple[str, str]] = set()     # (text, at) the old mechanism wrote into the profile
    ended_ts: float | None = None            # the latest death / clearing (/ move with moves_end)
    checked: str | None = None               # the stance text the latest stance check read
    left: str | None = None                  # the stance Decision read as broken: ends it by identity, as live
    counts: Counter = Counter()
    with trace.open("rb") as stream:
        for event in read_trace(stream):
            kind = event.get("event")
            ts = event.get("ts")
            if kind == "life_project_started":
                project = str(event.get("project"))
                stance = next((p.get("stance") for p in profile.get("projects") or []
                               if isinstance(p, dict) and p.get("id") == project), None)
                text = stance.get("text") if isinstance(stance, dict) else stance
                pending = {"id": project, "text": text.strip() if isinstance(text, str) and text.strip() else None}
                counts["item_runs"] += 1
            elif kind == "ladder_item_stance" and isinstance(event.get("stance"), dict):
                stance = event["stance"]
                copied.add((str(stance.get("text", "")).strip(), str(stance.get("at", ""))))
                if pending is not None and pending.get("text") is None and str(event.get("project")) == pending["id"]:
                    pending["text"] = str(stance.get("text", "")).strip() or None
            elif kind == "life_project_finished":
                if pending is not None and str(event.get("project")) == pending["id"]:
                    pending = None
            elif kind == "tool_raw_result" and str(event.get("tool_call_id") or "").startswith("plan-"):
                raw = event.get("raw_result")
                if pending is not None and isinstance(raw, dict) and raw.get("success") is True:
                    if pending["text"]:   # an item without a stance leaves the stance as it is
                        item_set = {"id": pending["id"], "text": pending["text"], "ts": float(ts)}
                        counts["set_stance"] += 1
                    pending = None
            elif kind == "call_supervision_default_finished":
                # The stance ends when a protective move SUCCEEDED: the Body's own result says it moved.
                choice = str(event.get("choice") or "")
                result = event.get("result")
                if not isinstance(result, dict):
                    result = ((event.get("facts") or {}).get("default_action") or {}).get("result")
                tool = str(event.get("tool") or ((event.get("facts") or {}).get("default_action") or {}).get("tool"))
                moving = choice in MOVING or choice.startswith("step_") or (
                    choice.startswith("rule_") and tool not in STAND_STILL_TOOLS)
                if moving and isinstance(result, dict) and result.get("success") is True:
                    counts["moves_done"] += 1
                    if moves_end:
                        ended_ts = float(ts)
            elif kind == "call_supervision_result" and event.get("purpose") == "stance_frame":
                if event.get("choice") == "broken":
                    left = checked
                    counts["broken"] += 1
            elif kind == "reflex_death":
                ended_ts = float(ts)
                counts["deaths"] += 1
            elif kind == "tool_invoke" and event.get("tool") == "update_life" \
                    and '"stance": null' in str(event.get("arguments_summary") or ""):
                ended_ts = float(ts)          # the Agent cleared its stance
                counts["cleared"] += 1
            elif kind == "call_supervision_request":
                row = by_id.get(f"{run}:{event.get('seq')}")
                if row is None:
                    continue
                if event.get("purpose") == "stance_frame" or row.get("purpose") == "stance_frame":
                    said = str(((row.get("request") or {}).get("state") or {}).get("agent_stance") or "")
                    if "\u201c" in said and "\u201d" in said:
                        checked = said[said.index("\u201c") + 1:said.rindex("\u201d")].strip()
                final = final_facts(row)
                if isinstance(final, dict) and isinstance(final.get("life_profile"), dict):
                    profile = final["life_profile"]
                elif isinstance((row.get("carry") or {}).get("life_profile"), dict):
                    profile = row["carry"]["life_profile"]
                recorded = (final or {}).get("agent_stance") if isinstance(final, dict) else None
                if recorded is None and isinstance(row.get("carry"), dict):
                    recorded = row["carry"].get("agent_stance")
                # The Agent's own write: the profile's stance, unless it is an old-mechanism copy.
                written = profile.get("stance") if isinstance(profile.get("stance"), dict) else None
                agent = None
                if written and (str(written.get("text", "")).strip(), str(written.get("at", ""))) not in copied:
                    at = _stance_time(written.get("at"))
                    if at is not None:
                        agent = {"text": str(written["text"]).strip(), "ts": at}
                chosen = agent
                if item_set is not None and (chosen is None or item_set["ts"] > chosen["ts"]):
                    chosen = {"text": item_set["text"], "ts": item_set["ts"]}
                if chosen is not None and ended_ts is not None and chosen["ts"] < float(int(ended_ts)):
                    chosen = None
                if chosen is not None and left is not None and chosen["text"] == left:
                    chosen = None
                fact = None
                if chosen is not None:
                    at = _dt.datetime.fromtimestamp(chosen["ts"], _dt.timezone.utc)
                    fact = {"text": chosen["text"], "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "age_s": max(0.0, float(ts) - chosen["ts"])}
                same = (isinstance(recorded, dict) and fact is not None
                        and str(recorded.get("text", "")).strip() == fact["text"]
                        and abs((_stance_time(recorded.get("at")) or 0) - chosen["ts"]) < 2.0) \
                    or (recorded is None and fact is None)
                row["stance_sim"] = None if same else {"item": item_set["id"] if item_set else None, "fact": fact}
                counts["frames_same" if same else ("frames_removed" if fact is None else
                                                   "frames_added" if recorded is None else "frames_replaced")] += 1
    return counts


def overlay(facts: dict[str, Any], row: dict[str, Any], name: str | None) -> dict[str, Any]:
    """Apply a simulated derivation to a frame's facts (a copy)."""
    if name != "stance" or row.get("stance_sim") is None:
        return facts
    facts = dict(facts)
    fact = row["stance_sim"].get("fact")
    if fact is None:
        facts.pop("agent_stance", None)
    else:
        facts["agent_stance"] = dict(fact)
    return facts


# --------------------------------------------------------------- recompile

class Modules:
    def __init__(self, tree: str) -> None:
        for name in [name for name in sys.modules if name == "minebot" or name.startswith("minebot.")]:
            del sys.modules[name]
        sys.path.insert(0, tree)
        import minebot.app.body_loop as body_loop
        import minebot.app.reflex_question as rq
        import minebot.app.direct_call_supervision as dcs
        self.body_loop, self.rq, self.dcs = body_loop, rq, dcs


@contextmanager
def frozen_clock(mods: Modules, monotonic_s: float | None, ts: float | None):
    """time.monotonic as the frame saw it; datetime.now as the frame's wall clock."""
    real = _dt.datetime

    class Frozen(real):  # type: ignore[misc]
        @classmethod
        def now(cls, tz=None):
            if ts is None:
                return real.now(tz)
            return real.fromtimestamp(ts, tz) if tz else real.fromtimestamp(ts)

        @classmethod
        def utcnow(cls):
            return real.utcnow() if ts is None else real.utcfromtimestamp(ts)

    patches = []
    for module in (mods.body_loop, mods.rq, mods.dcs):
        if getattr(module, "datetime", None) is real:
            patches.append(patch.object(module, "datetime", Frozen))
    with patch("time.monotonic", return_value=monotonic_s if monotonic_s is not None else time.monotonic()), \
            patch("time.time", return_value=ts if ts is not None else time.time()):
        for item in patches:
            item.start()
        try:
            yield
        finally:
            for item in patches:
                item.stop()


class RunLoop:
    """One BodyLoop per run, as live: creeper memory, shot record and placed
    beds advance frame by frame; what each frame recorded is set per frame."""

    def __init__(self, mods: Modules) -> None:
        self.mods = mods
        self.runtime = SimpleNamespace(
            trace=SimpleNamespace(emit=lambda *args, **kwargs: None),
            decision_client=SimpleNamespace(ask=None, close=None),
            agent_context=SimpleNamespace(goal_text=""),
            agent_thinking=False,
        )
        self.loop = mods.body_loop.BodyLoop(self.runtime)
        self.loop._life_store = None
        self.loop._life_profile = {}
        self.loop._life_version = None

    def seed(self, row: dict[str, Any], carry: dict[str, Any] | None) -> None:
        loop = self.loop
        carry = carry or {}
        context = (row.get("compiler_input") or {}).get("supervision_context") or {}
        recorded = (row.get("request") or {}).get("state") or {}
        # The Agent goal is runtime state the frame did not record; the live
        # frame's own task line is the only trace of it.
        self.runtime.agent_context.goal_text = "" if context.get("goal") else str(recorded.get("task") or "")
        self.runtime.agent_thinking = bool(carry.get("agent_thinking", False))
        loop._life_profile = copy.deepcopy(carry.get("life_profile") or {})
        saved = row.get("frame_input") or {}
        if isinstance(saved.get("vital_samples"), list):
            loop._vitals._samples = copy.deepcopy(saved["vital_samples"])
        if isinstance(saved.get("protections"), list) and hasattr(loop._vitals, "_protections"):
            loop._vitals._protections = copy.deepcopy(saved["protections"])
        loop._ambient_facts = copy.deepcopy(saved["ambient_facts"]) if isinstance(saved.get("ambient_facts"), dict) else {}
        loop._latest_finished_call = recorded.get("finished_call") or None

    def advance(self, row: dict[str, Any], final: dict[str, Any]) -> None:
        """Feed a frame with recorded final facts through _frame, for the memories."""
        monotonic_s = (row.get("frame_input") or {}).get("monotonic_s")
        if monotonic_s is None:
            return
        self.loop._ambient_facts = {}
        with frozen_clock(self.mods, monotonic_s, row["ts"]):
            self.loop._frame({"facts": copy.deepcopy(final)})


def carry_into(facts: dict[str, Any], row: dict[str, Any], carry: dict[str, Any] | None) -> None:
    carry = carry or {}
    present = set(carry.get("_present") or ())
    for key in ("own_beds", "ladder_held", "ladder_states", "ladder_same_run", "known_portals"):
        if key in carry and key not in facts:
            facts[key] = copy.deepcopy(carry[key])
    if "agent_stance" in present and isinstance(carry.get("agent_stance"), dict):
        stance = dict(carry["agent_stance"])
        try:
            at = _dt.datetime.strptime(stance["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
            stance["age_s"] = max(0.0, float(row["ts"]) - at.timestamp())
        except (KeyError, ValueError, TypeError):
            pass
        facts["agent_stance"] = stance
    elif "agent_stance" in facts and carry and "agent_stance" not in present:
        facts.pop("agent_stance", None)


def strip(request: dict[str, Any] | None) -> tuple[dict[str, Any] | None, Any]:
    if request is None:
        return None, None
    request = copy.deepcopy(request)
    for key in ("_request_chars", "_request_chars_before"):
        request.pop(key, None)
    return request, request.pop("_done", None)


def compile_once(run: RunLoop, row: dict[str, Any], carry: dict[str, Any] | None,
                 annotate: bool, lay: str | None = None) -> tuple[dict[str, Any], Any, str]:
    """The frame's request under the loaded tree; returns (request, done, options_source)."""
    rq = run.mods.rq
    purpose = row["purpose"]
    compiler_input = row.get("compiler_input") or {}
    frame_input = row.get("frame_input") or {}
    if purpose == "idle_frame":
        facts = overlay(compiler_input["facts"], row, lay)
        with frozen_clock(run.mods, frame_input.get("monotonic_s"), row["ts"]):
            fresh = rq.idle_options(facts)
            withheld = row.get("withheld")
            if withheld is None:
                withheld = [key for key in fresh if key not in (compiler_input.get("options") or {})]
                if annotate:
                    row["withheld"] = withheld
            options = {key: text for key, text in fresh.items() if key not in withheld}
            request = rq.idle_question(facts, options)
        request, done = strip(request)
        return request, done, "recomputed"
    loop = run.loop
    run.seed(row, carry)
    tool = SimpleNamespace(name=row["tool"])
    context = compiler_input.get("supervision_context") or {}
    params = compiler_input.get("params") or {}
    decision_context = compiler_input.get("decision_context") or None
    record = {"tool": row["tool"], "params": params, "supervision_context": context}
    with frozen_clock(run.mods, frame_input.get("monotonic_s"), row["ts"]):
        if purpose == "running_frame" and isinstance(compiler_input.get("facts"), dict):
            # Traces written after step 0 record the frame's final facts: no reconstruction.
            restored = {**frame_input["execution"], "facts": overlay(copy.deepcopy(compiler_input["facts"]), row, lay)}
            options = loop._options_for(tool, record, supplied=loop._tree_options(context), execution=restored)
            source = "recomputed"
        elif purpose == "running_frame":
            memory = {name: copy.deepcopy(getattr(loop, name)) for name in MEMORIES if hasattr(loop, name)}
            restored = loop._frame(frame_input["execution"])
            if annotate:
                # The pass may compile this frame twice (carry before / after); only the last advances.
                run._memory_before = memory
            carry_into(restored["facts"], row, carry)
            restored["facts"] = overlay(restored["facts"], row, lay)
            options = loop._options_for(tool, record, supplied=loop._tree_options(context), execution=restored)
            source = "recomputed"
        elif purpose == "result_frame":
            body = overlay((decision_context or {}).get("body_state") or {}, row, lay)
            restored = {**frame_input["execution"], "facts": body}
            if decision_context is not None and body is not decision_context.get("body_state"):
                decision_context = {**decision_context, "body_state": body}
            options = loop._options_for(tool, record, execution=restored)
            source = "recomputed"
        else:
            execution = frame_input["execution"]
            body = overlay(execution.get("facts") or {}, row, lay)
            restored = {**execution, "facts": body}
            if decision_context is not None and body is not execution.get("facts"):
                decision_context = {**decision_context, "body_state": body}
            options = compiler_input.get("options") or {}
            source = "recorded"
        request = loop._question(str(row.get("tool_call_id")), tool, params, restored,
                                 idle_s=float(row.get("idle_s") or 0.0), purpose=purpose,
                                 supervision_context=context, options=options, decision_context=decision_context)
    request, done = strip(request)
    return request, done, source


def recompile_row(run: RunLoop, row: dict[str, Any], annotate: bool,
                  base: dict[str, Any] | None = None, lay: str | None = None) -> dict[str, Any]:
    """With ``base`` (the base tree's recompile of the same frame) the comparison
    is candidate vs base on the same reconstruction, so reconstruction noise
    (creeper-memory ages from frames the trace did not ask) cancels out."""
    purpose = row["purpose"]
    out = {"id": row["id"], "run": row["run"], "t": row["t"], "ts": row["ts"], "purpose": purpose, "tool": row["tool"],
           "live_choice": row.get("live_choice"), "live_answers": row.get("live_answers"), "status": None,
           "options_source": None, "request": row["request"], "request_new": None, "done_identical": None, "note": ""}
    compiler_input = row.get("compiler_input") or {}
    if purpose not in RECOMPILED or not compiler_input:
        out["status"] = "skipped"
        return out
    committed = getattr(run.mods.body_loop, "COMMITTED_ACTIONS", ())
    if purpose == "running_frame" and row["tool"] in committed:
        out["status"] = "dropped"
        out["note"] = "committed action: the loop no longer asks running frames"
        return out
    recorded, _ = strip(row["request"])
    reference = recorded if base is None else (base.get("request_new") if base.get("status") in ("identical", "changed") else None)
    try:
        request, done, source = compile_once(run, row, row.get("carry"), annotate, lay)
        if annotate and purpose == "running_frame" and row.get("carry_next") is not None \
                and profile_mismatch(recorded, request):
            # The Agent may have changed its profile during the call: try the
            # profile recorded after the frame, keep whichever reproduces its words.
            for name, value in run._memory_before.items():
                setattr(run.loop, name, copy.deepcopy(value))
            second, done2, source2 = compile_once(run, row, row["carry_next"], annotate)
            if not profile_mismatch(recorded, second):
                request, done, source = second, done2, source2
                row["carry"] = row["carry_next"]
    except Exception as exc:  # a frame the candidate tree cannot compile is reported, not hidden
        out["status"] = "error"
        out["note"] = f"{type(exc).__name__}: {exc}"[:300]
        return out
    out["options_source"] = source
    out["request_new"] = request
    out["overlay"] = row.get("stance_sim") if lay == "stance" else None
    out["live_identical"] = dumps(request) == dumps(recorded)
    if reference is None:
        out["status"] = "unreferenced" if base is not None else ("identical" if out["live_identical"] else "changed")
        out["note"] = "" if out["status"] == "identical" else describe_change(recorded, request)
    else:
        out["request"] = reference  # the old side of the comparison: the base tree's own recompile
        out["status"] = "identical" if dumps(request) == dumps(reference) else "changed"
        out["note"] = "" if out["status"] == "identical" else describe_change(reference, request)
    if row.get("done_request") is not None or done is not None:
        out["done_identical"] = dumps(done) == dumps(row.get("done_request"))
    return out


def profile_mismatch(recorded: dict[str, Any] | None, request: dict[str, Any]) -> bool:
    """Whether the Agent's own words (rules, stance) differ from the live frame."""
    old = (recorded or {}).get("state") or {}
    new = request.get("state") or {}
    return any(old.get(key) != new.get(key) for key in ("agent_rules", "agent_stance"))


def describe_change(old: dict[str, Any] | None, new: dict[str, Any]) -> str:
    old = old or {}
    parts = []
    old_state, new_state = old.get("state") or {}, new.get("state") or {}
    for key in sorted(set(old_state) | set(new_state)):
        if key not in old_state:
            parts.append(f"+state.{key}")
        elif key not in new_state:
            parts.append(f"-state.{key}")
        elif old_state[key] != new_state[key]:
            parts.append(f"~state.{key}")
    if list(old_state) != list(new_state) and not parts:
        parts.append("state order")
    old_q, new_q = old.get("questions") or {}, new.get("questions") or {}
    for key in sorted(set(old_q) | set(new_q)):
        if key not in old_q:
            parts.append(f"+q.{key}")
        elif key not in new_q:
            parts.append(f"-q.{key}")
        else:
            oc, nc = old_q[key].get("criteria") or {}, new_q[key].get("criteria") or {}
            for option in sorted(set(oc) | set(nc)):
                if option not in oc:
                    parts.append(f"+{key}.{option}")
                elif option not in nc:
                    parts.append(f"-{key}.{option}")
                elif oc[option] != nc[option]:
                    parts.append(f"~{key}.{option}")
            if list(oc) != list(nc) and set(oc) == set(nc):
                parts.append(f"{key} order")
            for field in ("instructions", "type"):
                if old_q[key].get(field) != new_q[key].get(field):
                    parts.append(f"~q.{key}.{field}")
    return " ".join(parts)[:400]


def recompile(tree: str, corpus: list[Path], out: Path, annotate: bool, base_path: Path | None,
              lay: str | None = None) -> None:
    mods = Modules(tree)
    base = {row["id"]: row for row in read_rows(base_path)} if base_path is not None else None
    results: list[dict[str, Any]] = []
    for path in corpus:
        rows = read_rows(path)
        run = RunLoop(mods)
        for row in rows:
            final = final_facts(row)
            if row["purpose"] in RECOMPILED and final is not None and row["purpose"] != "running_frame":
                run.advance(row, final)
            results.append(recompile_row(run, row, annotate, None if base is None else base.get(row["id"], {}), lay))
        if annotate:
            for row in rows:
                row.pop("carry_next", None)
            write_rows(path, rows)
    write_rows(out, results)
    counts = Counter((row["purpose"], row["status"]) for row in results)
    for key in sorted(counts):
        print(f"{counts[key]:6d}  {key[0]:22s} {key[1]}")
    live = sum(1 for row in results if row.get("live_identical"))
    print(f"{live} frames recompile to the live request text exactly")
    errors = Counter(row["note"] for row in results if row["status"] == "error")
    for note, count in errors.most_common(10):
        print(f"error x{count}: {note}")


# --------------------------------------------------------------------- ask

async def ask_rows(rows: list[dict[str, Any]], out: Path, concurrency: int, sample_identical: int,
                   tree: str, seed: int) -> None:
    sys.path.insert(0, tree)
    import httpx
    from minebot.app import reflex_question as rq
    from minebot.app.decision_client import DecisionClient
    done: dict[str, dict[str, Any]] = {}
    if out.exists():
        for row in read_rows(out):
            if row.get("error") is None:
                done[row["id"]] = row
    todo = [row for row in rows if row["status"] == "changed" and row["id"] not in done]
    identical = [row for row in rows if row["status"] == "identical" and row["id"] not in done
                 and row.get("live_choice") not in (None, "unknown")]
    rng = random.Random(seed)
    rng.shuffle(identical)
    todo += identical[:sample_identical]
    print(f"asking {len(todo)} frames ({sum(1 for r in todo if r['status']=='changed')} changed, "
          f"{sum(1 for r in todo if r['status']=='identical')} identical samples); {len(done)} already answered")
    semaphore = asyncio.Semaphore(max(1, min(3, concurrency)))
    out.parent.mkdir(parents=True, exist_ok=True)
    stream = out.open("a", encoding="utf-8")

    async def one(client: Any, row: dict[str, Any]) -> None:
        result = {"id": row["id"], "status": row["status"], "live_choice": row.get("live_choice"),
                  "old_fresh": None, "new": None, "old_raw": None, "new_raw": None, "error": None}
        async with semaphore:
            try:
                answer = await client.ask(row["request"])
                result["old_raw"] = answer.raw
                result["old_fresh"] = rq.frame_choice(answer.raw, row["request"]["questions"])
                if row["status"] == "changed":
                    answer = await client.ask(row["request_new"])
                    result["new_raw"] = answer.raw
                    result["new"] = rq.frame_choice(answer.raw, row["request_new"]["questions"])
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        stream.write(dumps(result) + "\n")
        stream.flush()

    async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
        client = DecisionClient(url=URL, timeout_s=10, client=http)
        await asyncio.gather(*(one(client, row) for row in todo))
    stream.close()


# ------------------------------------------------------------------ report

def report(recompiled: Path, answers: Path | None, at: list[str]) -> None:
    rows = read_rows(recompiled)
    answered = {}
    if answers is not None and answers.exists():
        for row in read_rows(answers):
            if row.get("error") is None:
                answered[row["id"]] = row
    print(f"# Gate report: {recompiled.name}\n")
    counts = Counter((row["purpose"], row["status"]) for row in rows)
    purposes = sorted({row["purpose"] for row in rows})
    statuses = ["identical", "changed", "dropped", "error", "skipped"]
    print("| purpose | " + " | ".join(statuses) + " |")
    print("|---|" + "---|" * len(statuses))
    for purpose in purposes:
        print(f"| {purpose} | " + " | ".join(str(counts.get((purpose, status), 0)) for status in statuses) + " |")
    print("| all | " + " | ".join(str(sum(counts.get((p, s), 0) for p in purposes)) for s in statuses) + " |\n")
    sample = [answered[r["id"]] for r in rows if r["status"] == "identical" and r["id"] in answered]
    if sample:
        same = sum(1 for r in sample if r["old_fresh"] == r["live_choice"])
        print(f"Reproduction on identical frames: {same}/{len(sample)} fresh answers equal the live choice.\n")
    changed = [r for r in rows if r["status"] == "changed"]
    if changed:
        print("## Changed frames\n")
        kinds = Counter(r["note"] for r in changed)
        print("| change | frames |\n|---|---|")
        for note, count in kinds.most_common(40):
            print(f"| {note or '(text only)'} | {count} |")
        print()
        judged = [r for r in changed if r["id"] in answered]
        if judged:
            agree = sum(1 for r in judged if answered[r["id"]]["new"] == answered[r["id"]]["old_fresh"])
            live_new = sum(1 for r in judged if answered[r["id"]]["new"] == r["live_choice"])
            live_old = sum(1 for r in judged if answered[r["id"]]["old_fresh"] == r["live_choice"])
            print(f"Answers on changed frames: {len(judged)} asked; new == old-fresh {agree}; "
                  f"new == live {live_new}; old-fresh == live {live_old}.\n")
            print("| id | t | purpose | tool | live | old fresh | new | change |\n|---|---|---|---|---|---|---|---|")
            for r in judged:
                a = answered[r["id"]]
                if a["new"] != a["old_fresh"]:
                    print(f"| {r['id']} | {r['t']} | {r['purpose']} | {r['tool']} | {r['live_choice']} | "
                          f"{a['old_fresh']} | {a['new']} | {r['note']} |")
            print()
    dropped = [r for r in rows if r["status"] == "dropped"]
    if dropped:
        print(f"## Dropped frames ({len(dropped)})\n")
        for r in dropped[:50]:
            print(f"- {r['id']} {r['t']} {r['purpose']} {r['tool']}: {r['note']}")
        print()
    errors = [r for r in rows if r["status"] == "error"]
    if errors:
        print(f"## Errors ({len(errors)})\n")
        for note, count in Counter(r["note"] for r in errors).most_common(20):
            print(f"- x{count}: {note}")
        print()
    for mark in at:
        run, _, prefix = mark.partition(":")
        print(f"## Frames at {mark}\n")
        for r in rows:
            if r["run"] == run and str(r["t"]).startswith(prefix):
                a = answered.get(r["id"]) or {}
                print(f"- {r['id']} {r['t']} {r['purpose']} {r['tool']} {r['status']} live={r['live_choice']} "
                      f"old_fresh={a.get('old_fresh')} new={a.get('new')} {r['note']}")
                if r["status"] == "changed":
                    print("  old: " + dumps(r["request"])[:1500])
                    print("  new: " + dumps(r["request_new"])[:1500])
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    h = sub.add_parser("harvest")
    h.add_argument("trace", type=Path)
    h.add_argument("--run", required=True)
    h.add_argument("--out", type=Path, required=True)
    h.add_argument("--tree", default=str(Path(__file__).resolve().parents[2]))
    r = sub.add_parser("recompile")
    r.add_argument("corpus", type=Path, nargs="+")
    r.add_argument("--tree", required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--annotate", action="store_true")
    r.add_argument("--base", type=Path, default=None, help="the base tree's recompile output to compare with")
    r.add_argument("--overlay", choices=["stance"], default=None, help="apply a simulated derivation written by simulate")
    s = sub.add_parser("simulate", help="replay a new fact derivation over the trace and write it on the corpus")
    s.add_argument("what", choices=["stance"])
    s.add_argument("trace", type=Path)
    s.add_argument("corpus", type=Path)
    s.add_argument("--run", required=True)
    s.add_argument("--tree", default=str(Path(__file__).resolve().parents[2]))
    s.add_argument("--moves-end", action="store_true", help="v5: a successful protective move ends the stance")
    a = sub.add_parser("ask")
    a.add_argument("recompiled", type=Path)
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--concurrency", type=int, default=3)
    a.add_argument("--sample-identical", type=int, default=0)
    a.add_argument("--seed", type=int, default=7)
    a.add_argument("--tree", default=str(Path(__file__).resolve().parents[2]))
    p = sub.add_parser("report")
    p.add_argument("recompiled", type=Path)
    p.add_argument("answers", type=Path, nargs="?")
    p.add_argument("--at", action="append", default=[])
    args = parser.parse_args()
    if args.command == "harvest":
        rows = harvest(args.trace, args.run, args.tree)
        write_rows(args.out, rows)
        counts = Counter(row["purpose"] for row in rows)
        answered = sum(1 for row in rows if row["live_answers"])
        print(f"{len(rows)} frames, {answered} with per-question live answers; "
              + ", ".join(f"{k} {v}" for k, v in counts.most_common()))
    elif args.command == "recompile":
        recompile(args.tree, args.corpus, args.out, args.annotate, args.base, args.overlay)
    elif args.command == "simulate":
        rows = read_rows(args.corpus)
        counts = simulate_stance(args.trace, args.run, rows, args.tree, moves_end=args.moves_end)
        write_rows(args.corpus, rows)
        print(f"{args.run}: " + ", ".join(f"{key} {value}" for key, value in sorted(counts.items())))
    elif args.command == "ask":
        asyncio.run(ask_rows(read_rows(args.recompiled), args.out, args.concurrency, args.sample_identical,
                             args.tree, args.seed))
    else:
        report(args.recompiled, args.answers, args.at)


if __name__ == "__main__":
    main()
