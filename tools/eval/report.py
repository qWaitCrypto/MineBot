"""Generate a from-zero evaluation report from immutable trace/server evidence.

Interventions are optional RUN_DIR/interventions.jsonl records, one per line:
{"ts": 1792401629.0, "kind": "difficulty_change", "note": "operator changed to easy"}
ts accepts Unix seconds or an ISO8601 timestamp with offset. Missing files mean
'unrecorded', not 'no interventions'. See wakes.py for legacy wake inference.

--out-dir writes all artifacts elsewhere without copying or changing inputs.
Server log clocks use --timezone (default Asia/Taipei); all output times are UTC.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.eval.common import (BUSY_EVENTS, BusyTime, compact, iso, json_rows, player_files,
                               quantiles, read_level, server_events, timestamp, trace_bounds, trace_events)
from tools.eval.wakes import WAKE_EVENTS, Wakes


REPORT_EVENTS = BUSY_EVENTS | WAKE_EVENTS | {
    "provider_manifest", "runtime_scope", "model_tool_call", "tool_invoke", "model_usage",
    "call_supervision_request", "call_supervision_result", "decision_request", "reflex_death",
    "event_loop_stalled", "body_events", "session_terminal", "reflex_loop_stopped",
    "work_decision_request", "work_decision_result",
    "life_project_started", "life_project_finished", "notice_queued", "ladder_low",
}


def death_records(trace_deaths: list[dict], logs: list[dict], samples: list[dict], start: float, end: float) -> list[dict]:
    pending = [row for row in logs if row["kind"] == "death" and start <= row["ts"] <= end]
    records = []
    for death in trace_deaths:
        matches = [r for r in pending if abs(r["ts"] - death["ts"]) <= 5]
        log = matches[0] if len(matches) == 1 else None
        if log:
            pending.remove(log)
        facts = death.get("facts") or {}
        pos = facts.get("position") or facts.get("pos")
        ts = log["ts"] if log else death["ts"]
        position_source = "trace:reflex_death" if pos is not None else "unknown"
        sample_ts = None
        if pos is None:
            near = [r for r in samples if r.get("pos") is not None and 0 <= ts - r["ts"] <= 90]
            if near:
                sample = max(near, key=lambda r: r["ts"])
                pos = sample["pos"]
                if isinstance(pos, str):
                    pos = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", pos)]
                position_source, sample_ts = "samples.jsonl:preceding_sample", sample["ts"]
        records.append({"ts": ts, "time": iso(ts), "time_source": log["source"] if log else "trace:reflex_death",
                        "cause": log["text"] if log else "unknown", "cause_source": log["source"] if log else "unknown",
                        "position": pos, "position_source": position_source, "position_sample_ts": sample_ts,
                        "trace_ts": death["ts"], "seq": death.get("seq"), "dimension": facts.get("dimension")})
    for log in pending:
        # No trace death edge: the same sample fallback still applies.
        records.extend(death_records([{"ts": log["ts"], "facts": {}}], [log], samples, start, end))
    return sorted(records, key=lambda r: r["ts"])


def build_report(run_dir: Path, server_dir: Path, world_name: str, bot: str | None = None,
                 timezone: str = "Asia/Taipei") -> dict:
    trace = run_dir / "session" / "trace.jsonl"
    start, end = trace_bounds(trace)
    duration = end - start
    busy, wakes = BusyTime(), Wakes()
    samples = json_rows(run_dir / "samples.jsonl")
    counts = Counter()
    requests, choices = Counter(), defaultdict(Counter)
    work_requests, work_choices = Counter(), defaultdict(Counter)
    decision_times, latencies, trace_deaths, difficulty = [], [], [], []
    tool_calls, invocations, tool_failures, terminal_failures = Counter(), Counter(), Counter(), Counter()
    usage = defaultdict(Counter)
    llm_start = None
    # Plan ladder (plan-ladder-20261028.md §7.11): who starts items, how they
    # end, time executing vs completed, the gap from one item to the next.
    ladder = {"starts": Counter(), "ends": defaultdict(Counter), "executing_s": Counter(),
              "completed_s": Counter(), "picks": Counter(), "gaps": [], "last_end": None}
    stalls = 0.0
    seen_terminal = set()
    bot_name = bot
    for row in trace_events(trace, REPORT_EVENTS):
        event, ts = row["event"], row["ts"]
        counts[event] += 1
        if bot_name is None:
            bot_name = row.get("bot") or row.get("session_id")
        if event == "provider_manifest":
            # SDK traces have agent_start hooks; Responses traces do not.
            wakes.native = row.get("agent_kernel") == "responses" or row.get("kernel") == "responses"
        if event == "ladder_low" and row.get("reason") == "none_started":
            counts["ladder_low_none_started"] += 1
        busy.consume(row)
        wakes.consume(row)
        if event == "life_project_started":
            ladder["starts"][row.get("who") or "decision"] += 1
            if ladder["last_end"] is not None:
                ladder["gaps"].append(ts - ladder["last_end"])
                ladder["last_end"] = None
        elif event == "life_project_finished":
            who, outcome = row.get("who") or "decision", row.get("outcome") or "unknown"
            ladder["ends"][who][outcome] += 1
            if isinstance(row.get("duration_s"), (int, float)):
                ladder["executing_s"][who] += row["duration_s"]
                if outcome == "completed":
                    ladder["completed_s"][who] += row["duration_s"]
            ladder["last_end"] = ts
        elif event == "call_supervision_result" and row.get("purpose") == "idle_frame" and str(
                row.get("choice") or "").startswith("project_"):
            ladder["picks"][str(row["choice"]).removeprefix("project_")] += 1
        if event == "llm_start":
            llm_start = ts
        elif event == "llm_end" and llm_start is not None:
            latencies.append(ts - llm_start)
            llm_start = None
        elif event == "turn_cancelled":
            llm_start = None
        elif event == "model_usage":
            value = row.get("usage") or {}
            group = usage[row.get("purpose") or "agent"]
            group["requests"] += 1
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                group[key] += value.get(key, 0)
            group["cached_tokens"] += (value.get("input_tokens_details") or {}).get("cached_tokens", 0)
            group["reasoning_tokens"] += (value.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
        elif event in {"call_supervision_request", "work_decision_request"}:
            (requests if event == "call_supervision_request" else work_requests)[row.get("purpose", "unknown")] += 1
            facts = ((row.get("frame_input") or {}).get("execution") or {}).get("facts") or {}
            value = facts.get("difficulty")
            if value is not None and (not difficulty or difficulty[-1]["difficulty"] != value):
                difficulty.append({"ts": ts, "difficulty": value, "source": "trace:call_supervision_request", "seq": row.get("seq")})
        elif event in {"call_supervision_result", "work_decision_result"}:
            purpose, choice = row.get("purpose", "unknown"), row.get("choice") or "unknown"
            (choices if event == "call_supervision_result" else work_choices)[purpose][choice] += 1
            if event == "call_supervision_result" and isinstance(row.get("decision_ms"), (int, float)):
                decision_times.append(row["decision_ms"])
            if row.get("unknown") or choice == "unknown":
                counts["unknown_answers"] += 1
        elif event == "model_tool_call":
            tool_calls[row.get("tool") or row.get("name") or "unknown"] += 1
        elif event == "tool_invoke":
            invocations[row.get("tool") or "unknown"] += 1
        elif event == "tool_result" and row.get("success") is False:
            tool_failures[(row.get("tool") or "unknown", row.get("reason") or "unknown")] += 1
        elif event == "body_events":
            for body in row.get("events") or []:
                data = body.get("data") or {}
                key = (body.get("bot"), body.get("seq"), data.get("action_id"))
                if body.get("name", "").endswith("Done") and (data.get("success") is False or data.get("ok") is False) and key not in seen_terminal:
                    seen_terminal.add(key)
                    terminal_failures[(data.get("action_kind") or body["name"], data.get("reason") or "unknown")] += 1
        elif event == "reflex_death":
            trace_deaths.append(row)
        elif event == "event_loop_stalled":
            stalls += row.get("stalled_s", 0)
    if not bot_name:
        raise ValueError("bot name unavailable; supply --bot")
    world = server_dir / world_name
    adv_path, stats_path = player_files(server_dir, world, bot_name)
    stats = json.loads(stats_path.read_text()).get("stats", {}) if stats_path else {}
    advancements = []
    if adv_path:
        for key, value in json.loads(adv_path.read_text()).items():
            if not isinstance(value, dict) or not value.get("done") or key.startswith("minecraft:recipes/"):
                continue
            times = [timestamp(t) for t in value.get("criteria", {}).values()]
            # All criteria must be satisfied; completion is the last criterion.
            ts = max(times) if times else None
            advancements.append({"advancement": key, "ts": ts, "time": iso(ts) if ts is not None else None,
                                 "minutes": (ts - start) / 60 if ts is not None else None,
                                 "source": str(adv_path)})
    advancements.sort(key=lambda r: r["ts"] if r["ts"] is not None else float("inf"))
    logs = server_events(server_dir, world_name, bot_name, start, end, ZoneInfo(timezone))
    level = read_level(world / "level.dat") if (world / "level.dat").exists() else {}
    seed = (level.get("WorldGenSettings") or {}).get("seed", level.get("RandomSeed"))
    seed_source = world / "level.dat"
    generation_file = world / "data" / "minecraft" / "world_gen_settings.dat"
    if seed is None and generation_file.exists():
        seed = read_level(generation_file).get("seed")
        seed_source = generation_file
    saved_difficulty = level.get("Difficulty", (level.get("difficulty_settings") or {}).get("difficulty"))
    interventions_path = run_dir / "interventions.jsonl"
    interventions = json_rows(interventions_path)
    for row in interventions:
        if not {"ts", "kind", "note"} <= row.keys() or not isinstance(row["kind"], str) or not isinstance(row["note"], str):
            raise ValueError("interventions require ts, kind and note")
        timestamp(row["ts"])
    # A zero baseline is explicit protocol context, not proof of a fresh world.
    baseline_path = run_dir / "stats-before.json"
    baseline = None
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text()).get("stats", {}).get("minecraft:killed", {}).get("minecraft:ender_dragon", 0)
    dragon = stats.get("minecraft:killed", {}).get("minecraft:ender_dragon", 0) if stats_path else None
    delta = dragon - (baseline or 0) if dragon is not None else None
    limitations = ["No interventions file means interventions were not recorded; it does not certify no help.",
                   "Fresh world, empty DB/life and seed preregistration cannot be verified retrospectively from these files.",
                   "Old samples before the 2026-10-19 19:21 local sampler fix can omit items with components.",
                   "Busy/idle uses explicit reflex work state; body idle can include planning or protection between calls.",
                   "Death causes require an unambiguous server message within 5 seconds; nearby mobs are not a cause."]
    if baseline is None:
        limitations.append("Dragon baseline assumed zero under the from-zero protocol; supply stats-before.json for a measured delta.")
    deaths = death_records(trace_deaths, logs, samples, start, end)
    custom = stats.get("minecraft:custom", {})
    if custom.get("minecraft:deaths") != len(deaths):
        limitations.append(f"Death evidence count {len(deaths)} differs from server stats {custom.get('minecraft:deaths')}.")
    usage_rows = {}
    total = Counter()
    for purpose, counters in usage.items():
        total.update(counters)
        usage_rows[purpose] = dict(counters) | {"cache_read_ratio": counters["cached_tokens"] / counters["input_tokens"] if counters["input_tokens"] else None}
    total_usage = dict(total) | {"cache_read_ratio": total["cached_tokens"] / total["input_tokens"] if total["input_tokens"] else None}
    trace_size = trace.stat().st_size
    return {
        "schema_version": 1,
        "run": {"input_dir": str(run_dir.resolve()), "trace": str(trace.resolve()), "world": world_name, "bot": bot_name,
                "start_ts": start, "end_ts": end, "start": iso(start), "end": iso(end), "duration_s": duration,
                "time_source": "first/last trace timestamps", "seed": seed, "seed_source": str(seed_source),
                "difficulty_observations": difficulty, "difficulty_changes": [r for r in logs if r["kind"] == "difficulty"],
                "final_saved_difficulty": saved_difficulty, "log_timezone": timezone,
                "interventions_recorded": interventions_path.exists(), "interventions": interventions,
                "samples": {"count": len(samples), "errors": sum("error" in r for r in samples),
                            "start_ts": samples[0]["ts"] if samples else None, "end_ts": samples[-1]["ts"] if samples else None}},
        "progress": {"advancement_count": len(advancements), "advancements": advancements, "ender_dragon_kills": dragon,
                     "ender_dragon_baseline": baseline, "baseline_source": str(baseline_path) if baseline is not None else "assumed_zero_from_zero_protocol",
                     "ender_dragon_delta": delta, "passed": delta >= 1 if delta is not None else None,
                     "stats_source": str(stats_path) if stats_path else None, "server_deaths": custom.get("minecraft:deaths"),
                     "killed_by": stats.get("minecraft:killed_by", {})},
        "deaths": deaths,
        "agent": {"model_requests": {"count": counts["llm_end"], "started": counts["llm_start"],
                                     "incomplete_or_cancelled": counts["llm_start"] - counts["llm_end"],
                                     "paired_latency_count": len(latencies), "latency_s": quantiles(latencies)},
                  "usage": total_usage, "usage_by_purpose": usage_rows, "wake_ups": wakes.finish()},
        "decision": {"requests_by_purpose": dict(requests), "choices_by_purpose": {k: dict(v) for k, v in choices.items()},
                     "decision_request_count": counts["decision_request"], "decision_ms": quantiles(decision_times),
                     "decision_ms_count": len(decision_times), "unknown_answers": counts["unknown_answers"],
                     "work_requests_by_purpose": dict(work_requests), "work_choices_by_purpose": {k: dict(v) for k, v in work_choices.items()}},
        "ladder": {"starts_by_who": dict(ladder["starts"]),
                   "ends_by_who": {who: dict(outcomes) for who, outcomes in ladder["ends"].items()},
                   "executing_s_by_who": dict(ladder["executing_s"]), "completed_s_by_who": dict(ladder["completed_s"]),
                   "idle_picks_by_item": dict(ladder["picks"]), "end_to_next_start_s": quantiles(ladder["gaps"]),
                   "end_to_next_start_count": len(ladder["gaps"]),
                   "notices": counts["notice_queued"], "ladder_low": counts["ladder_low"],
                   "ladder_low_none_started": counts["ladder_low_none_started"]},
        "body": {**busy.finish(start, end), "model_tool_calls_by_tool": dict(tool_calls),
                 "tool_invocations_by_tool": dict(invocations), "tool_failures": [{"tool": tool, "reason": reason, "count": n} for (tool, reason), n in tool_failures.most_common()],
                 "body_terminal_failures": [{"tool": tool, "reason": reason, "count": n} for (tool, reason), n in terminal_failures.most_common()]},
        "engineering": {"event_loop_stalled_count": counts["event_loop_stalled"], "stalled_s": stalls,
                        "trace_bytes": trace_size, "trace_mb": trace_size / 1_000_000,
                        "trace_mb_per_hour": trace_size / 1_000_000 / (duration / 3600) if duration else None},
        "limitations": limitations,
    }


def cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def table(headers: list[str], rows) -> list[str]:
    return ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(cell(v) for v in row) + " |" for row in rows), ""]


def render_report(report: dict) -> str:
    run, agent, decision, body, progress = (report[k] for k in ("run", "agent", "decision", "body", "progress"))
    model = agent["model_requests"]
    lines = [f"# Evaluation: {run['world']} / {run['bot']}", "", "## Run", "",
             f"Input: `{run['input_dir']}`. All times UTC; server log clocks: {run['log_timezone']}.", "",
             f"Start {run['start']}; end {run['end']}; duration {run['duration_s']/60:.2f} min (trace bounds).",
             f"Seed: {run['seed']} ({run['seed_source']}). Saved difficulty: {run['final_saved_difficulty']}.", "",
             "Difficulty observations and changes:", ""]
    lines += table(["Time", "Difficulty / message", "Source"],
                   [(iso(r["ts"]), r.get("difficulty", r.get("text")), r["source"]) for r in run["difficulty_observations"] + run["difficulty_changes"]])
    lines += [f"Interventions file present: {run['interventions_recorded']}.", ""]
    lines += table(["Time", "Kind", "Note"], [(r["ts"], r["kind"], r["note"]) for r in run["interventions"]])
    lines += ["Intervention format: one JSON object per line with `ts` (Unix seconds or ISO8601 with offset), `kind`, `note`.", "",
              "## Progress", "", f"Advancements: **{progress['advancement_count']}** (non-recipe, completed).",
              f"Ender dragon kills: **{progress['ender_dragon_kills']}**; delta: {progress['ender_dragon_delta']}; pass: **{progress['passed']}**.",
              f"Baseline: {progress['baseline_source']}. Stats: {progress['stats_source']}.", ""]
    lines += table(["Minute", "Time", "Advancement"], [(f"{r['minutes']:.2f}" if r['minutes'] is not None else "unknown", r["time"], r["advancement"]) for r in progress["advancements"]])
    lines += ["## Deaths", "", f"Server stats: **{progress['server_deaths']}**; evidence records: {len(report['deaths'])}. Killed by: {progress['killed_by']}.", ""]
    lines += table(["Time (source)", "Cause (source)", "Position (source)", "Trace seq"],
                   [(f"{r['time']} ({r['time_source']})", f"{r['cause']} ({r['cause_source']})", f"{r['position']} ({r['position_source']})", r["seq"]) for r in report["deaths"]])
    lines += ["## Agent", "", f"Model requests completed: **{model['count']}**; started: {model['started']}; incomplete/cancelled: {model['incomplete_or_cancelled']}.",
              f"Paired latency ({model['paired_latency_count']}): {model['latency_s']} seconds (sorted-index quantiles, matching eval-final.py).", ""]
    lines += table(["Usage purpose", "Requests", "Input tokens", "Reasoning tokens", "Cache-read ratio"],
                   [(k, v["requests"], v["input_tokens"], v["reasoning_tokens"], v["cache_read_ratio"]) for k,v in agent["usage_by_purpose"].items()])
    lines += [f"Wake-ups / outer turns: **{agent['wake_ups']['count']}**. SDK tool continuations are model requests inside a turn.", ""]
    lines += table(["Wake reason", "Count"], sorted(agent["wake_ups"]["by_reason"].items()))
    lines += ["Per-turn reason, detail and evidence seq are in report.json. Legacy mapping is documented in tools/eval/wakes.py; unmatched turns stay unknown.", "", "## Decision", "",
              f"Total decision_request: **{decision['decision_request_count']}**; unknown answers: **{decision['unknown_answers']}**.",
              f"Supervision decision_ms ({decision['decision_ms_count']} results): {decision['decision_ms']}.", ""]
    lines += table(["Purpose", "Requests", "Choice distribution"], [(k,n,decision["choices_by_purpose"].get(k,{})) for k,n in sorted(decision["requests_by_purpose"].items())])
    if decision["work_requests_by_purpose"]:
        lines += ["Additional work_decision requests (not call_supervision_request):", ""]
        lines += table(["Purpose", "Requests", "Choices"], [(k,n,decision["work_choices_by_purpose"].get(k,{})) for k,n in sorted(decision["work_requests_by_purpose"].items())])
    ladder = report.get("ladder") or {}
    lines += ["## Plan ladder", "",
              f"Notices (plan ends that did not wake the Agent): **{ladder.get('notices', 0)}**; "
              f"low-water wakes: **{ladder.get('ladder_low', 0)}** "
              f"(items offered but none started: {ladder.get('ladder_low_none_started', 0)}).",
              f"Item end to next item start ({ladder.get('end_to_next_start_count', 0)} gaps): "
              f"{ladder.get('end_to_next_start_s')} seconds.", ""]
    lines += table(["Started by", "Starts", "Ends by outcome", "Executing min", "Completed min"],
                   [(who, n, (ladder.get("ends_by_who") or {}).get(who, {}),
                     f"{(ladder.get('executing_s_by_who') or {}).get(who, 0)/60:.2f}",
                     f"{(ladder.get('completed_s_by_who') or {}).get(who, 0)/60:.2f}")
                    for who, n in sorted((ladder.get("starts_by_who") or {}).items())])
    lines += table(["Item picked in idle frames", "Picks"], sorted((ladder.get("idle_picks_by_item") or {}).items()))
    lines += ["## Body", "", f"Busy {body['busy_s']/60:.2f} min; idle {body['idle_s']/60:.2f} min (reflex work-state complement).", ""]
    lines += table(["Category", "Minutes"], [(k, f"{v/60:.2f}") for k,v in body["categories_s"].items()])
    lines += [body["method"], ""]
    lines += table(["Tool", "Model calls", "Invocations"], [(k,body["model_tool_calls_by_tool"].get(k,0),body["tool_invocations_by_tool"].get(k,0)) for k in sorted(body["model_tool_calls_by_tool"].keys() | body["tool_invocations_by_tool"].keys())])
    for label, key in (("Tool result failures", "tool_failures"), ("Body action terminal failures", "body_terminal_failures")):
        lines += [label + ":", ""]
        lines += table(["Tool / action", "Reason", "Count"], [(r["tool"],r["reason"],r["count"]) for r in body[key]])
    lines += ["## Engineering", "", *[f"- {k}: {v}" for k,v in report["engineering"].items()], "",
              "## Evidence limits", "", *[f"- {text}" for text in report["limitations"]], ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--server-dir", type=Path, required=True)
    parser.add_argument("--world", required=True)
    parser.add_argument("--bot")
    parser.add_argument("--timezone", default="Asia/Taipei")
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    report = build_report(args.run_dir, args.server_dir, args.world, args.bot, args.timezone)
    out = args.out_dir or args.run_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (out / "report.md").write_text(render_report(report))
    print(f"{out}/report.json\n{out}/report.md")


if __name__ == "__main__":
    main()
