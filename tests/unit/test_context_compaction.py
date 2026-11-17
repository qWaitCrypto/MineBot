"""The model input stays a stable prefix and is summarized once at the threshold."""

import asyncio

from minebot.app.context_compaction import SUMMARY_HEADER, SUMMARY_INSTRUCTIONS, ConversationCompactor
from minebot.brain.context import skill_mounted_message, skill_unmounted_message


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


def test_harness_facts_are_summarized_not_carried_forward_as_player_messages():
    calls = []
    compactor = _compactor(20_000, calls, tail_chars=2_000)
    fact = "HARNESS_FACT: REFLEX_RESULT: " + "y" * 30_000
    items = [{"role": "user", "content": "build a house"}, *_turn(0),
             {"role": "user", "content": fact}, *_turn(1),
             {"role": "user", "content": "Alex: use oak please\n" + fact}]
    for index in range(2, 40):
        items.extend(_turn(index))

    view = asyncio.run(compactor.view(items))
    head = [item["content"] for item in view if item.get("role") == "user"]
    assert head[0] == "build a house" and head[1] == "Alex: use oak please"
    assert head[2].startswith(SUMMARY_HEADER)
    assert "y" * 100 not in "".join(head)
    assert "REFLEX_RESULT" in calls[0][1]  # the fact went to the summarizer instead


def test_no_compaction_when_what_is_large_is_the_part_that_stays():
    calls, events = [], []
    compactor = _compactor(20_000, calls, tail_chars=2_000)
    compactor.emit = lambda event, **payload: events.append((event, payload))
    huge = {"type": "function_call_output", "call_id": "call-0", "output": "z" * 90_000}
    last = [{"type": "function_call", "call_id": "call-0", "name": "read", "arguments": "{}"}, huge]

    async def scenario():
        views = []
        for turns in (1, 2, 3):
            small = [item for index in range(1, turns + 1) for item in _turn(index)]
            views.append(await compactor.view([{"role": "user", "content": "build a house"}, *small, *last]))
        return views

    views = asyncio.run(scenario())
    assert calls == []  # summarizing a few small items would not make the request fit
    assert all(view[-1] == huge and view[0]["content"] == "build a house" for view in views)
    assert {payload["reason"] for event, payload in events} == {"little_to_summarize"}


def test_summary_instructions_preserve_world_facts_without_copying_live_state():
    assert "PLACES" in SUMMARY_INSTRUCTIONS
    assert "STORED AND PLACED" in SUMMARY_INSTRUCTIONS
    assert "HOW THIS WORLD AND BODY BEHAVE" in SUMMARY_INSTRUCTIONS
    assert "DECISIONS" in SUMMARY_INSTRUCTIONS
    assert "WHERE THINGS STAND" in SUMMARY_INSTRUCTIONS
    assert "Do not copy those live values" in SUMMARY_INSTRUCTIONS
    assert "tool that broke" in SUMMARY_INSTRUCTIONS
    assert "Write in the language the player uses" in SUMMARY_INSTRUCTIONS


def test_the_live_context_appended_after_the_view_counts_toward_the_threshold():
    calls = []
    compactor = _compactor(40_000, calls, tail_chars=2_000)
    history = _history(20)

    async def scenario():
        await compactor.view(history)
        assert calls == []
        await compactor.view(history, live_chars=90_000)

    asyncio.run(scenario())
    assert len(calls) == 1


# -- Skills mounted on ladder items (agent-skills-v2-20261105.md §2.1) ----------

CRYSTALS = skill_mounted_message("end-crystals", "end-fight", "# End Crystals\n\n## Purpose\nClear the crystals.")
NETHER = skill_mounted_message("nether-expedition", "nether-trip", "# Nether Expedition\n\n## Purpose\nGo there.")


def _with_skills(turns, *, late=()):
    """History with a mount early on, a second mount and its unmount, then ``late`` items at the end."""
    items = _history(turns)
    items.insert(4, {"role": "user", "content": CRYSTALS})  # after turn 0
    items.insert(8, {"role": "user", "content": NETHER})  # after turn 1
    items.insert(9, {"role": "user", "content": skill_unmounted_message("nether-expedition")})
    return [*items, *late]


def test_mount_messages_are_not_summarized_and_still_mounted_ones_follow_the_summary():
    calls = []
    mounted = [CRYSTALS]
    compactor = _compactor(20_000, calls, tail_chars=2_000, mounted_skills=lambda: list(mounted))

    async def scenario():
        compactor.note_usage(0)
        compacted = await compactor.view(_with_skills(60))
        later = await compactor.view(_with_skills(62))
        # The Skill unmounts later: the line is appended, the prefix is kept.
        mounted.clear()
        unmounted = await compactor.view(
            _with_skills(63, late=[{"role": "user", "content": skill_unmounted_message("end-crystals")}]))
        return compacted, later, unmounted

    compacted, later, unmounted = asyncio.run(scenario())
    assert len(calls) == 1
    transcript = calls[0][1]
    assert "SKILL_MOUNTED" not in transcript and "SKILL_UNMOUNTED" not in transcript
    assert compacted[0] == {"role": "user", "content": "build a house"}
    assert compacted[1]["content"].startswith(SUMMARY_HEADER)
    # Same text as the original mount message, right after the summary.
    assert compacted[2] == {"role": "user", "content": CRYSTALS}
    contents = [item.get("content") for item in compacted]
    assert contents.count(CRYSTALS) == 1
    # An unmounted Skill is not carried forward, nor its lines as player messages.
    assert NETHER not in contents
    assert skill_unmounted_message("nether-expedition") not in contents
    assert _shared_prefix(compacted, later) == len(compacted)
    assert _shared_prefix(later, unmounted) == len(later)
    assert unmounted[-1]["content"] == skill_unmounted_message("end-crystals")


def test_a_mount_message_still_in_the_kept_tail_is_not_shown_twice():
    calls = []
    late_mount = skill_mounted_message("resource-progression", "iron", "# Resource Progression\n\nbody")
    compactor = _compactor(20_000, calls, tail_chars=2_000,
                           mounted_skills=lambda: [CRYSTALS, late_mount])

    async def scenario():
        compactor.note_usage(0)
        return await compactor.view(_with_skills(60, late=[{"role": "user", "content": late_mount}]))

    view = asyncio.run(scenario())
    contents = [item.get("content") for item in view]
    assert contents.count(late_mount) == 1 and contents[-1] == late_mount
    assert contents.count(CRYSTALS) == 1 and contents[2] == CRYSTALS


def test_mounts_the_session_window_dropped_are_shown_first_and_kept_stable():
    calls = []
    compactor = _compactor(1_000_000, calls, mounted_skills=lambda: [CRYSTALS])

    async def scenario():
        # The window starts after the mount message: it is no longer visible.
        first = await compactor.view(_history(5))
        longer = await compactor.view(_history(8))
        visible = ConversationCompactor(summarize=None, threshold_tokens=0,
                                        mounted_skills=lambda: [CRYSTALS])
        shown = await visible.view(_with_skills(5))
        return first, longer, shown

    first, longer, shown = asyncio.run(scenario())
    assert calls == []
    assert first[0] == {"role": "user", "content": CRYSTALS}
    assert first[1:] == _history(5)
    assert _shared_prefix(first, longer) == len(first)
    # When the history still holds the message, nothing is added.
    assert shown == _with_skills(5)
