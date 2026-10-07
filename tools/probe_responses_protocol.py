"""One native API experiment, without Minecraft or an Agents SDK Runner.

Example: python tools/probe_responses_protocol.py --env-file /private/provider.env
The optional --proxy selects a network route; credentials never enter reports.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
import sys
from urllib.parse import urlparse

import httpx
import openai
from openai import AsyncOpenAI

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def probe(args, *, environment=None):
    env = dict(os.environ if environment is None else environment)
    if args.env_file:
        for raw in Path(args.env_file).read_text().splitlines():
            line = raw.strip().removeprefix("export ")
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                words = shlex.split(value, comments=True)
                env[key.strip()] = words[0] if words else ""
    model = args.model or env.get("MINEBOT_LLM_MODEL") or env.get("OPENAI_MODEL")
    key = env.get(env.get("MINEBOT_LLM_API_KEY_ENV", "MINEBOT_LLM_API_KEY")) or env.get("OPENAI_API_KEY")
    if not model or not key:
        raise ValueError("A model and API key must be configured in the private environment")
    base_url = env.get("MINEBOT_LLM_BASE_URL") or env.get("OPENAI_BASE_URL")
    proxy = args.proxy or env.get("MINEBOT_LLM_PROXY")
    report = {"success": False, "scope": "native API protocol only; no Minecraft task",
              "model": model, "endpoint_host": urlparse(base_url or "https://api.openai.com").hostname,
              "sdk": openai.__version__, "phase": "connect"}
    tool = {"type": "function", "name": "slow_job", "description": "Start asynchronous work. Its real result will arrive later.",
            "async": True, "strict": True, "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}
    client = AsyncOpenAI(api_key=key, base_url=base_url, max_retries=0, timeout=25,
                         http_client=httpx.AsyncClient(proxy=proxy) if proxy else None)
    transport = getattr(args, "transport", None) or env.get("MINEBOT_RESPONSES_TRANSPORT", "http")
    report["transport"] = transport
    events = []
    try:
        options = {"open_timeout": 15}
        if proxy:
            options["proxy"] = proxy
        if transport == "http":
            from minebot.app.responses_kernel import _HttpResponsesConnection
            connection = _HttpResponsesConnection(client)
        else:
            connection = client.responses.connect(max_retries=0, websocket_connection_options=options)
        async with connection as ws:
            async def send(value):
                await ws.send_raw(json.dumps(value))

            async def receive(on_created=None):
                while True:
                    event = json.loads(await ws.recv_bytes())
                    events.append(event.get("type"))
                    if event.get("type") == "error":
                        raise RuntimeError(str(event.get("error", event)))
                    if event.get("type") == "response.created" and on_created:
                        await on_created(event["response"]["id"])
                    if event.get("type") in {"response.completed", "response.incomplete", "response.failed"}:
                        response = event["response"]
                        if event["type"] == "response.failed":
                            raise RuntimeError(str(response.get("error")))
                        return response

            def create(inputs, previous=None, **extra):
                value = {"type": "response.create", "model": model, "tools": [tool], "input": inputs,
                         "store": True, "max_output_tokens": 256, "reasoning": {"effort": "low"}, **extra}
                if previous:
                    value["previous_response_id"] = previous
                return value

            async with asyncio.timeout(55):
                report["phase"] = "async_dispatch"
                await send(create("Start slow_job using an asynchronous call. Do not invent its result.",
                                  tool_choice={"type": "function", "name": "slow_job"}))
                first = await receive()
                call = next(item for item in first["output"] if item["type"] == "function_call")
                if not call.get("async"):
                    raise RuntimeError("The returned function call is not marked async; native async execution is unproved")
                report["phase"] = "pending_user_turn"
                await send(create("While that job is still pending, reply ACK.", first["id"], tool_choice="none"))
                second = await receive()
                chat = "\n".join(part.get("text", "") for item in second.get("output", []) for part in item.get("content", []))
                if "ACK" not in chat:
                    raise RuntimeError("Conversation with a pending native async call did not return ACK")
                report["pending_chat"] = True
                report["phase"] = "late_result"
                await send(create([{"type": "function_call_output", "call_id": call["call_id"],
                                    "output": '{"count":3}'}], second["id"], tool_choice="none",
                                  instructions="Report the tool result exactly as COUNT=3."))
                third = await receive()
                text = "\n".join(part.get("text", "") for item in third.get("output", []) for part in item.get("content", []))
                if "COUNT=3" not in text:
                    raise RuntimeError("Late result was not reflected in the response: " + text[:200])
                if transport == "http":
                    report.update(success=True, phase="completed", native_async=True, late_result=True, steering=False)
                else:
                    report["phase"] = "steering"
                    async def steer(rid):
                        await send({"type": "response.steer", "previous_response_id": rid, "input": "Stop counting. Reply exactly STEERED."})
                    await send(create("Count slowly from 1 to 100 in words.", third["id"], tool_choice="none", max_output_tokens=1024))
                    before = await receive(steer)
                    after = await receive()
                    if before["id"] == after["id"] or "response.steer.accepted" not in events:
                        raise RuntimeError("Steering did not produce an accepted automatic successor")
                    text = "\n".join(part.get("text", "") for item in after.get("output", []) for part in item.get("content", []))
                    if "STEERED" not in text:
                        raise RuntimeError("The steering request was not reflected in the successor")
                    report.update(success=True, phase="completed", native_async=True, late_result=True, steering=True)
    except Exception as exc:
        report.update(error_type=type(exc).__name__, reason=str(exc).replace(key, "[redacted]")[:600] or type(exc).__name__)
        if exc.__cause__ is not None:
            report["cause"] = type(exc.__cause__).__name__ + ": " + str(exc.__cause__).replace(key, "[redacted]")[:300]
    finally:
        await client.close()
    report["events"] = events
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file")
    parser.add_argument("--model")
    parser.add_argument("--proxy")
    parser.add_argument("--transport", choices=("http", "websocket"), help="HTTP verifies native async; WebSocket also verifies mid-turn steering")
    args = parser.parse_args()
    report = asyncio.run(probe(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
