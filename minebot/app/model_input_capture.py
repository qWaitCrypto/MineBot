"""Record what the Agent's model is given each call, for replay on another model.

Off unless MINEBOT_CAPTURE_MODEL_INPUT names a file.  Each line holds one
call: the system prompt, the input items, and the tools (written in full
when they change, otherwise by hash).  Used to ask two models the same
turn (agent-model A/B, 2026-11-09).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any

_lock = threading.Lock()
_last_tools: dict[str, str] = {}


def _tools(agent: Any) -> list[dict[str, Any]]:
    rows = []
    for tool in getattr(agent, "tools", None) or []:
        rows.append({"name": getattr(tool, "name", None), "description": getattr(tool, "description", None),
                     "parameters": getattr(tool, "params_json_schema", None),
                     "strict": getattr(tool, "strict_json_schema", None)})
    return rows


def capture(system_prompt: str | None, input_items: list[Any], agent: Any) -> None:
    path = os.environ.get("MINEBOT_CAPTURE_MODEL_INPUT")
    if not path:
        return
    try:
        tools = _tools(agent)
        tools_text = json.dumps(tools, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(tools_text.encode("utf-8")).hexdigest()[:16]
        row: dict[str, Any] = {"ts": time.time(), "agent": getattr(agent, "name", None),
                               "model": str(getattr(agent, "model", None)), "system_prompt": system_prompt,
                               "input": input_items, "tools_hash": digest}
        with _lock:
            if _last_tools.get(path) != digest:
                row["tools"] = tools
                _last_tools[path] = digest
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:  # recording must never break a turn
        return
