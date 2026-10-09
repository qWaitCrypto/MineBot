"""Inference-only replay: saved Agent tools/history, no Minecraft tool execution."""

import argparse
import asyncio
import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from minebot.app.config import provider_registry_from_env
from minebot.app.responses_kernel import _HttpResponsesConnection
from minebot.app.observability import sanitize_observation


async def main(args):
    env = {}
    for line in args.profile.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.removeprefix("export ").split("=", 1)
            parts = shlex.split(value)
            env[key.strip()] = parts[0] if parts else ""
    env.update(MINEBOT_LLM_MODEL=args.model, MINEBOT_LLM_PROXY=args.proxy,
               MINEBOT_LLM_REQUEST_TIMEOUT_S="120")
    provider = provider_registry_from_env(env)
    client, model, settings, _ = provider.responses_connection_config()
    mode, threshold = settings.pop("_context_mode"), settings.pop("_compact_threshold")
    if args.threshold is not None:
        threshold = args.threshold
    if args.cache_options:
        settings["extra_body"] = {"prompt_cache_options": {"mode": args.cache_mode, "ttl": "30m"}}
    if args.cache_key:
        settings["prompt_cache_key"] = args.cache_key
    payload = json.loads(args.payload.read_text())
    tools = payload["tools"]
    if tools and tools[0].get("type") == "namespace":
        tools = tools[0]["tools"]
    instructions = payload["instructions"].split("GOAL:", 1)[0].strip()
    snapshot = payload["instructions"].split("GOAL:", 1)[-1].split("RECENT_TOOL_RESULTS:", 1)[0]
    snapshot = "FROZEN_REPLAY_STATE (historical, no live game execution):\nGOAL:" + snapshot
    records, results = [], []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sink = args.output.with_suffix(".jsonl").open("w")
    def emit(event, **facts):
        row = sanitize_observation({"event": event, **facts})
        records.append(row)
        sink.write(json.dumps(row, ensure_ascii=False) + "\n")
        sink.flush()
    conn = _HttpResponsesConnection(client, emit, context_mode=mode, compact_threshold=threshold)
    conn.history = payload["input"][:1]
    if args.cache_options and args.cache_mode == "explicit":
        conn.history = [{**i, "content": [{"type": "input_text", "text": i["content"],
            "prompt_cache_breakpoint": {"mode": "explicit"}}]} for i in conn.history]
    inputs = payload["input"][1:]
    try:
        async with conn:
            for sample in range(args.samples):
                params = {"type": "response.create", "model": model, "instructions": instructions,
                          "tools": tools, "input": inputs, "_current_context": snapshot, **settings}
                if not args.auto:
                    params["tool_choice"] = {"type": "function", "name": "read_state"}
                begin = len(records)
                calls = []
                async with asyncio.timeout(150):
                    await conn.send_raw(json.dumps(params, ensure_ascii=False))
                    while True:
                        event = json.loads(await conn.recv_bytes())
                        if event["type"] == "response.output_item.done" and event["item"]["type"] == "function_call":
                            calls.append(event["item"])
                        if event["type"] in {"response.completed", "response.failed", "response.incomplete"}:
                            result = {"sample": sample, "terminal": event["type"], "usage": event["response"].get("usage"),
                                      "response_model": event["response"].get("model"), "choices": [c["name"] for c in calls],
                                      "incomplete_details": event["response"].get("incomplete_details"),
                                      "context": conn.context_usage, "observations": records[begin:]}
                            results.append(result)
                            brief = {k: v for k, v in result.items() if k not in {"observations", "context", "usage"}}
                            brief["usage"] = {k: v for k, v in (result["usage"] or {}).items() if k != "attribution"}
                            print(json.dumps(brief, ensure_ascii=False), flush=True)
                            break
                # Cache probes force read_state and replay the saved real
                # state, rather than executing any generated tool.
                if args.auto or any(c["name"] != "read_state" for c in calls):
                    break
                inputs = [{"type": "function_call_output", "call_id": c["call_id"],
                           "output": json.dumps({"success": True, "reason": "frozen_state_replay", "snapshot": snapshot})}
                          for c in calls]
    except Exception as exc:
        results.append({"error_type": type(exc).__name__, "message": str(exc)[:300]})
        print(json.dumps(results[-1]), flush=True)
    finally:
        await provider.aclose()
        sink.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"scope": "Saved real Agent schemas and reconstructed task history; frozen state; no generated tools executed",
            "context_mode": mode, "threshold": threshold, "results": results, "observations": records}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--threshold", type=int)
    parser.add_argument("--cache-options", action="store_true", help="Explicitly test these options on this endpoint")
    parser.add_argument("--cache-mode", choices=["implicit", "explicit"], default="implicit")
    parser.add_argument("--cache-key")
    parser.add_argument("--auto", action="store_true", help="Let the model select a tool; record it and stop without executing")
    asyncio.run(main(parser.parse_args()))
