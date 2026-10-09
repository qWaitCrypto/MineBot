"""Request timing and context sizes without recording credentials or stream text."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.request import urlopen
import time
from collections import Counter
from contextvars import ContextVar
from functools import lru_cache
from typing import Any, Callable
from uuid import uuid4

import httpx
import tiktoken


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


@lru_cache(maxsize=1)
def encoding(model: str):
    # A portable estimate, calibrated against each provider's actual usage.
    # Do not let tiktoken's unbounded first-use download silently stall a turn.
    asset = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
    digest = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
    cache_dir = os.environ.get("TIKTOKEN_CACHE_DIR", os.environ.get("DATA_GYM_CACHE_DIR",
        str(Path(tempfile.gettempdir()) / "data-gym-cache")))
    if not cache_dir:
        raise RuntimeError("Token counting needs an enabled TIKTOKEN_CACHE_DIR")
    cached = Path(cache_dir) / hashlib.sha1(asset.encode()).hexdigest()
    if not cached.exists() or hashlib.sha256(cached.read_bytes()).hexdigest() != digest:
        with urlopen(asset, timeout=10) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError("Tokenizer asset failed its SHA256 check")
        cached.parent.mkdir(parents=True, exist_ok=True)
        temporary = cached.with_suffix("." + uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(cached)
        finally:
            temporary.unlink(missing_ok=True)
    return tiktoken.get_encoding("o200k_base")


def context_sizes(payload: dict) -> dict:
    """Visible text estimate only; opaque items require server rendered usage."""
    enc = encoding(payload.get("model", ""))
    def count(value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return len(enc.encode(text, disallowed_special=()))
    opaque_chars = 0
    kinds = Counter()
    visible = []
    for item in payload.get("input", []):
        kinds[item.get("type", item.get("role", "message"))] += 1
        clean = dict(item)
        opaque_chars += len(clean.pop("encrypted_content", "") or "")
        visible.append(clean)
    return {"unit": "tokens", "encoding": enc.name, "estimate_only": True,
            "instructions": count(payload.get("instructions", "")),
            "tools": count(payload.get("tools", [])), "visible_input": count(visible),
            "opaque_chars": opaque_chars, "input_kinds": dict(kinds)}


current_observation: ContextVar[RequestObservation | None] = ContextVar("responses_observation", default=None)


class RequestObservation:
    def __init__(self, emit: Callable[..., None]):
        self.emit = emit
        self.request_key = uuid4().hex[:12]
        self.started = time.monotonic()
        self.attempt = 0
        self.bytes = self.chunks = self.comments = 0
        self.last_byte = self.started
        self.max_gap = 0.0
        self.last_report = self.started
        self.comment_line = True
        self.line_start = True
        self.events = Counter()
        self.first_events: dict[str, float] = {}

    def record(self, event: str, **facts):
        self.emit(event, request_key=self.request_key, attempt=self.attempt,
                  elapsed_s=round(time.monotonic() - self.started, 3), **facts)

    async def trace(self, name: str, info: dict):
        # httpcore info can contain headers, sockets and TLS objects. Log only
        # phase names and error classes, never the info dictionary.
        if any(phase in name for phase in ("connect_tcp.", "start_tls.", "send_request_headers.",
                                          "send_request_body.", "receive_response_headers.")):
            facts = {"phase": name}
            if "exception" in info:
                facts["error_type"] = type(info["exception"]).__name__
            self.record("responses_http_phase", **facts)

    def received(self, chunk: bytes):
        now = time.monotonic()
        gap = now - self.last_byte
        self.max_gap = max(self.max_gap, gap)
        self.last_byte = now
        if not self.chunks:
            self.record("responses_first_byte", gap_s=round(gap, 3))
        self.chunks += 1
        self.bytes += len(chunk)
        # Count SSE comment lines even when split across socket chunks. Do not
        # decode, retain or log their content (or any model delta content).
        for line in chunk.splitlines(keepends=True):
            if self.line_start:
                self.comment_line = line.startswith(b":")
                if self.comment_line:
                    self.comments += 1
            self.line_start = line.endswith((b"\n", b"\r"))
        if now - self.last_report >= 5:
            self.last_report = now
            self.record("responses_stream_activity", **self.byte_summary())

    def byte_summary(self):
        return {"received_bytes": self.bytes, "chunk_count": self.chunks,
                "comment_count": self.comments, "max_byte_gap_s": round(self.max_gap, 3),
                "byte_idle_s": round(time.monotonic() - self.last_byte, 3)}

    def event(self, event: dict):
        kind = event.get("type", "unknown")
        self.events[kind] += 1
        group = next((g for g in ("output_text.delta", "reasoning", "function_call_arguments.delta", "compaction")
                      if g in kind), None)
        if group is None and kind == "response.output_item.added":
            group = event.get("item", {}).get("type")
        if group and group not in self.first_events:
            self.first_events[group] = round(time.monotonic() - self.started, 3)
            self.record("responses_first_activity", activity=group)

    def finish(self, status: str):
        self.record("responses_stream_summary", status=status, event_counts=dict(self.events),
                    first_activity_s=self.first_events, **self.byte_summary())


class _ObservedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, observation: RequestObservation):
        self.stream, self.observation = stream, observation

    async def __aiter__(self):
        async for chunk in self.stream:
            self.observation.received(chunk)
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


async def observe_http_request(request: httpx.Request):
    obs = current_observation.get()
    if obs is None:
        return
    obs.attempt += 1
    obs.record("responses_http_attempt", method=request.method, host=request.url.host)
    prior_trace = request.extensions.get("trace")
    async def trace(name, info):
        await obs.trace(name, info)
        if prior_trace is not None:
            await prior_trace(name, info)
    request.extensions["trace"] = trace


async def observe_http_response(response: httpx.Response):
    obs = current_observation.get()
    if obs is None:
        return
    obs.record("responses_http_headers", status_code=response.status_code,
               request_id=response.headers.get("x-request-id"),
               processing_ms=response.headers.get("openai-processing-ms"))
    response.stream = _ObservedStream(response.stream, obs)
