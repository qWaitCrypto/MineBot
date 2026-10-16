#!/usr/bin/env python3
"""Does the provider extend a cached prefix as an agent loop grows it?

Runs a short agent loop of real, tiny requests against the configured Responses
endpoint and prints the input and cached tokens of every step. The same history
is used for each variant, so the only difference between rows is the layout.

    variant  what changes
    -------  ------------------------------------------------------------------
    repeat   the same request sent twice (a cache that works at all hits ~all)
    live     the layout the Agent sends: history + steps + a trailing user
             CURRENT_CONTEXT item that changes every step
    plain    no trailing item: the previous request is a pure prefix
    keyed    live layout with prompt_cache_key set

    $ python tools/cache_ab_probe.py --env-file ~/.config/minebot/provider-xxx.env \
          --history /path/to/conversation_items.json [--steps 5] [--variants repeat,live,plain,keyed]

The key is read from the env file and never printed. Cost: about 25k input
tokens per step (--chars sets the history size), a few output tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from openai import AsyncOpenAI

SYSTEM = "You are a Minecraft agent. Call the tool `step` once with a short note." + " Stay concise." * 40
TOOLS = [{
    "type": "function", "name": "step", "description": "Take one step.", "strict": False,
    "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"],
                   "additionalProperties": False},
}]


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(path).expanduser().read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("\"'")
    return env


def history_items(path: str, chars: int) -> list[dict[str, Any]]:
    """Real conversation items (user / reasoning / message / calls), newest last, up to `chars`."""
    items = json.loads(Path(path).read_text())
    kept: list[dict[str, Any]] = []
    total = 0
    for item in reversed(items):
        size = min(len(json.dumps(item, ensure_ascii=False)), 6000)
        if total + size > chars:
            break
        kept.append(item)
        total += size
    kept.reverse()
    # start on a user item and cut any oversized text so the history is the same on every request
    while kept and kept[0].get("role") != "user":
        kept.pop(0)
    def cut(value: Any) -> Any:
        if isinstance(value, str) and len(value) > 6000:
            return value[:6000]
        if isinstance(value, dict):
            return {k: cut(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cut(v) for v in value]
        return value
    # reasoning items are bound to the account that produced them; leave them out of the seed
    return [cut(item) for item in kept if item.get("type") != "reasoning"]


def context_item(step: int) -> dict[str, str]:
    return {"role": "user", "content": f"CURRENT_CONTEXT:\nstep {step}; position (1, 2, {step}); time {time.time()}\n" + "state line\n" * 120}


async def one(client: AsyncOpenAI, model: str, items: list[Any], *, key: str | None, effort: str) -> tuple[list[dict], int, int]:
    extra: dict[str, Any] = {"prompt_cache_key": key} if key else {}
    response = await client.responses.create(
        model=model, instructions=SYSTEM, tools=TOOLS, input=items, tool_choice="required",
        reasoning={"effort": effort}, max_output_tokens=400, **extra)
    usage = response.usage
    out = [item.model_dump(exclude_none=True) for item in response.output]
    return out, usage.input_tokens, (usage.input_tokens_details.cached_tokens if usage.input_tokens_details else 0)


async def run_variant(client, model, variant, history, steps, effort) -> list[tuple[int, int]]:
    rows: list[tuple[int, int]] = []
    key = "minebot-cache-ab" if variant == "keyed" else None
    generated: list[dict] = []
    fixed_context = context_item(0)
    begin = {"role": "user", "content": "Begin."}
    for step in range(1, steps + 1):
        if variant == "repeat":
            items = [*history, fixed_context]
        elif variant == "plain":
            items = [*history, begin, *generated]
        else:  # live, keyed
            items = [*history, *generated, context_item(step)]
        out, tokens, cached = await one(client, model, items, key=key, effort=effort)
        rows.append((tokens, cached))
        if variant != "repeat":
            for item in out:
                generated.append(item)
                if item.get("type") == "function_call":
                    generated.append({"type": "function_call_output", "call_id": item["call_id"], "output": "ok"})
        await asyncio.sleep(2)
    return rows


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--history", required=True, help="JSON list of conversation items")
    parser.add_argument("--steps", type=int, default=5)
    parser.add_argument("--chars", type=int, default=90_000)
    parser.add_argument("--variants", default="repeat,live,plain,keyed")
    parser.add_argument("--effort", default="low")
    args = parser.parse_args()

    env = load_env(args.env_file)
    base_url, model = env["MINEBOT_LLM_BASE_URL"], env["MINEBOT_LLM_MODEL"]
    key = env.get("MINEBOT_LLM_API_KEY") or os.environ.get(env.get("MINEBOT_LLM_API_KEY_ENV", ""), "")
    proxy = env.get("MINEBOT_LLM_PROXY") or None
    http = httpx.AsyncClient(proxy=proxy, trust_env=not bool(proxy), timeout=httpx.Timeout(180.0))
    client = AsyncOpenAI(api_key=key, base_url=base_url, http_client=http, max_retries=0)
    history = history_items(args.history, args.chars)
    print(f"model {model}; history {len(history)} items; {args.steps} steps per variant", file=sys.stderr)
    try:
        for variant in args.variants.split(","):
            rows = await run_variant(client, model, variant.strip(), history, args.steps, args.effort)
            print(f"\n{variant}")
            for index, (tokens, cached) in enumerate(rows, 1):
                print(f"  step {index}: input {tokens:>7}  cached {cached:>7}  {100 * cached / max(tokens, 1):5.1f}%")
    finally:
        await http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
