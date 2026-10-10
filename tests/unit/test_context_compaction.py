"""The model input stays a stable prefix and is summarized once at the threshold."""

import asyncio

from minebot.app.context_compaction import SUMMARY_HEADER, ConversationCompactor


def _turn(index, size=400):
    call = f"call-{index}"
    return [
        {"type": "function_call", "call_id": call, "name": "move_to", "arguments": "{}"},
        {"type": "function_call_output", "call_id": call, "output": "x" * size},
        {"role": "assistant", "type": "message", "content": [{"type": "output_text", "text": f"step {index}"}]},
    ]


def _history(turns):
    items = [{"role": "user", "content": "build a house"}]
    for index in range(turns):
        items.extend(_turn(index))
    return items


def _compactor(threshold, calls, **kwargs):
    async def summarize(previous, transcript):
        calls.append((previous, transcript))
        return f"summary {len(calls)}"

    return ConversationCompactor(summarize=summarize, threshold_tokens=threshold, **kwargs)


def _shared_prefix(first, second):
    shared = 0
    for a, b in zip(first, second):
        if a != b:
            break
        shared += 1
    return shared


def test_under_the_threshold_every_request_extends_the_previous_one():
    calls = []
    compactor = _compactor(1_000_000, calls)

    async def scenario():
        views = []
        for turns in (5, 6, 7, 30):
            views.append(await compactor.view(_history(turns)))
        return views

    views = asyncio.run(scenario())
    assert calls == []
    for earlier, later in zip(views, views[1:]):
        assert _shared_prefix(earlier, later) == len(earlier)  # nothing slides


def test_threshold_summarizes_once_and_the_new_prefix_is_stable():
    calls = []
    compactor = _compactor(20_000, calls, tail_chars=2_000)

    async def scenario():
        compactor.note_usage(0)
        small = await compactor.view(_history(3))
        compacted = await compactor.view(_history(60))
        after = [await compactor.view(_history(turns)) for turns in (61, 62, 70)]
        return small, compacted, after

    small, compacted, after = asyncio.run(scenario())
    assert len(small) == 10 and len(calls) == 1
    assert compacted[0] == {"role": "user", "content": "build a house"}  # what the player said survives
    assert compacted[1]["role"] == "user" and compacted[1]["content"].startswith(SUMMARY_HEADER)
    assert "summary 1" in compacted[1]["content"]
    assert len(compacted) < 20
    previous = compacted
    for view in after:
        assert _shared_prefix(previous, view) == len(previous)
        previous = view
    assert len(calls) == 1  # no second summary while under the threshold again


def test_tail_never_starts_between_a_call_and_its_result():
    calls = []
    compactor = _compactor(5_000, calls, tail_chars=700)
    items = [{"role": "user", "content": "go"}]
    for index in range(30):
        items.extend(_turn(index))
    # Two parallel calls whose results follow both calls.
    items += [
        {"type": "function_call", "call_id": "a", "name": "read_state", "arguments": "{}"},
        {"type": "function_call", "call_id": "b", "name": "read_inventory", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "a", "output": "y" * 300},
        {"type": "function_call_output", "call_id": "b", "output": "z" * 300},
    ]
    view = asyncio.run(compactor.view(items))
    called = {item["call_id"] for item in view if item.get("type") == "function_call"}
    answered = {item["call_id"] for item in view if item.get("type") == "function_call_output"}
    assert answered <= called and {"a", "b"} <= called


def test_second_compaction_carries_the_first_summary_forward():
    calls = []
    compactor = _compactor(20_000, calls, tail_chars=2_000)

    async def scenario():
        await compactor.view(_history(60))
        return await compactor.view(_history(200))

    view = asyncio.run(scenario())
    assert len(calls) == 2 and calls[1][0] == "summary 1"
    assert sum(1 for item in view if str(item.get("content", "")).startswith(SUMMARY_HEADER)) == 1
    assert "summary 2" in view[1]["content"]


def test_failed_summary_still_shrinks_the_request_and_says_so():
    async def summarize(previous, transcript):
        raise RuntimeError("provider down")

    events = []
    compactor = ConversationCompactor(summarize=summarize, threshold_tokens=20_000, tail_chars=2_000,
                                      emit=lambda event, **fields: events.append(event))
    view = asyncio.run(compactor.view(_history(60)))
    assert len(view) < 20 and "could not be summarized" in view[1]["content"]
    assert "conversation_compaction_failed" in events


def test_real_usage_replaces_the_character_estimate():
    calls = []
    compactor = _compactor(50_000, calls, tail_chars=2_000)

    async def scenario():
        await compactor.view(_history(10))
        compactor.note_usage(49_900)  # tools and system made the real request far larger
        return await compactor.view(_history(12))

    asyncio.run(scenario())
    assert len(calls) == 1


def test_zero_threshold_never_summarizes():
    calls = []
    compactor = _compactor(0, calls)
    view = asyncio.run(compactor.view(_history(300)))
    assert calls == [] and len(view) == 901
