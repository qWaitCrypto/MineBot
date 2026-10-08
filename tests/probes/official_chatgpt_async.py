"""Opt-in official OAuth experiment; uses local tools, never touches Minecraft.

python -m tests.probes.official_chatgpt_async --output logs/chatgpt-async.jsonl
Success requires native async, chat while work is pending, and the real late
receipt delivered on the original call. A model answer alone is not success.
"""
import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path

from minebot.app.config import provider_registry_from_env
from minebot.app.responses_kernel import ResponsesKernel


async def experiment(output: Path):
    provider = provider_registry_from_env({**os.environ, "MINEBOT_LLM_AUTH": "chatgpt",
        "MINEBOT_LLM_KIND": "openai_responses", "MINEBOT_AGENT_KERNEL": "responses",
        "MINEBOT_LLM_BASE_URL": "https://api.openai.com/v1", "MINEBOT_RESPONSES_TRANSPORT": "http",
        "MINEBOT_LLM_MODEL": "gpt-6.1-sol", "MINEBOT_LLM_REASONING_EFFORT": "low"})
    release = asyncio.Event()
    receipt = "RECEIPT_" + secrets.token_hex(6)
    events, calls, spoken = [], [], []
    chat_sent, chat_during_work = False, False

    def record(kind, **data):
        nonlocal chat_sent
        events.append({"event": kind, **data})
        if kind == "responses_terminal" and kernel.pending and not chat_sent:
            chat_sent = True
            kernel.steer("Status? Say PENDING while the job is running. Do not start another job.")

    async def invoke(name, arguments, call_id):
        calls.append(call_id)
        await asyncio.wait_for(release.wait(), 60)
        return {"success": True, "receipt": receipt}

    def speak(text):
        nonlocal chat_during_work
        spoken.append(text)
        record("assistant", text=text)
        if chat_sent and "PENDING" in text and not release.is_set():
            chat_during_work = True
            release.set()

    async def instructions():
        return ("Call wait_for_signal exactly once. While it is pending say STARTED. "
                "Answer status questions without waiting. When the result arrives, repeat its receipt verbatim. "
                "Never invent a receipt or call the job again.")

    client, model, settings, timeout = provider.responses_connection_config()
    kernel = ResponsesKernel(client=client, model=model, settings=settings, request_timeout_s=timeout,
        tools=lambda: [{"type": "function", "name": "wait_for_signal", "async": True, "strict": True,
                        "description": "Wait for an external signal and return a private receipt.",
                        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}],
        instructions=instructions, invoke=invoke, emit=record, speak=speak,
        transport="http", tool_namespace=provider.responses_tool_namespace)
    try:
        await asyncio.wait_for(kernel.run("Start the job now.", max_responses=6), 180)
        native = [e for e in events if e["event"] == "responses_tool_started"]
        success = (len(calls) == 1 and len(native) == 1 and native[0]["asynchronous"]
                   and chat_during_work and any(receipt in text for text in spoken)
                   and kernel.calls[calls[0]].delivered)
        record("result", success=bool(success), chat_during_work=chat_during_work, calls=len(calls))
    except Exception as exc:
        # Request data are omitted. Auth errors are already sanitized.
        record("result", success=False, error_type=type(exc).__name__, reason=str(exc)[:600])
    finally:
        release.set()
        for call in kernel.pending:
            call.task.cancel()
        await asyncio.gather(*(c.task for c in kernel.pending), return_exceptions=True)
        await kernel.close()
        await provider.aclose()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n")
    print(json.dumps(events[-1], ensure_ascii=False))
    return events[-1]["success"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("logs/chatgpt-native-async.jsonl"))
    raise SystemExit(0 if asyncio.run(experiment(parser.parse_args().output)) else 1)
