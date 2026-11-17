"""Chat Completions model that reports prompt-cache hits for every vendor.

OpenAI-compatible endpoints all cache by prefix, but they do not agree on where
the hit count goes: OpenAI, Qwen, GLM and xAI use
``usage.prompt_tokens_details.cached_tokens``; DeepSeek uses
``usage.prompt_cache_hit_tokens``; Moonshot uses a top-level
``usage.cached_tokens``. The Agents SDK reads only the first form, so on the
others a cache hit would be reported as zero. This model moves the vendor field
into the standard place before the SDK converts the usage, for both blocking
and streamed responses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from agents import OpenAIChatCompletionsModel
from openai.types.chat import ChatCompletionChunk
from openai.types.completion_usage import CompletionUsage, PromptTokensDetails

# Vendor spellings of "prompt tokens served from cache", in preference order.
_VENDOR_CACHED_FIELDS = ("prompt_cache_hit_tokens", "cached_tokens", "cache_read_input_tokens")


def normalize_chat_usage(usage: CompletionUsage | None) -> None:
    """Fill ``prompt_tokens_details.cached_tokens`` from a vendor field, in place."""
    if usage is None:
        return
    details = usage.prompt_tokens_details
    if details is not None and (details.cached_tokens or 0) > 0:
        return
    extras = usage.model_extra or {}
    cached = next(
        (
            value
            for name in _VENDOR_CACHED_FIELDS
            if type(value := extras.get(name)) is int and value > 0
        ),
        0,
    )
    if not cached:
        return
    if details is None:
        usage.prompt_tokens_details = PromptTokensDetails(cached_tokens=cached)
    else:
        details.cached_tokens = cached


async def _normalized_chunks(
    stream: AsyncIterator[ChatCompletionChunk],
) -> AsyncIterator[ChatCompletionChunk]:
    async for chunk in stream:
        normalize_chat_usage(chunk.usage)
        yield chunk


class CacheUsageChatCompletionsModel(OpenAIChatCompletionsModel):
    """``OpenAIChatCompletionsModel`` with vendor-neutral cache-hit reporting."""

    async def _fetch_response(self, *args: Any, **kwargs: Any) -> Any:
        result = await super()._fetch_response(*args, **kwargs)
        if isinstance(result, tuple):
            response, stream = result
            return response, _normalized_chunks(stream)
        normalize_chat_usage(result.usage)
        return result


__all__ = ["CacheUsageChatCompletionsModel", "normalize_chat_usage"]
