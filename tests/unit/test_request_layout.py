"""The request-layout record says where a request stopped repeating the one before.

A provider's cache hit can only be as long as the repeated prefix. Without this
record a low hit rate could be our request changing early or the provider not
caching a prefix we repeated, and the villa run of 2026-10-16 (21.8% hits,
exactly the tools and instructions every time) could not tell which.
"""

from __future__ import annotations

import json

import httpx

from minebot.app.request_layout import RequestLayoutTracker


def _request(items, *, tools=None, instructions="sys", path="/v1/responses"):
    body = {"model": "m", "tools": tools or [{"name": "t"}], "instructions": instructions, "input": items}
    return httpx.Request("POST", f"http://provider.test{path}", content=json.dumps(body).encode())


def _msg(text, role="user"):
    return {"role": role, "content": text}


def test_a_request_that_extends_the_previous_one_loses_nothing():
    tracker = RequestLayoutTracker()
    history = [_msg("goal"), {"type": "function_call", "name": "a", "call_id": "1", "arguments": "{}"},
               {"type": "function_call_output", "call_id": "1", "output": "ok"}]
    tracker.observe(_request([*history, _msg("CURRENT_CONTEXT: 1")]))
    assert "items_shared_with_previous" not in tracker.last
    tracker.observe(_request([*history, {"type": "function_call", "name": "b", "call_id": "2", "arguments": "{}"},
                              {"type": "function_call_output", "call_id": "2", "output": "ok"},
                              _msg("CURRENT_CONTEXT: 2")]))
    assert tracker.last["items_shared_with_previous"] == 3
    assert tracker.last["lost_items"] == 0
    assert "first_difference" not in tracker.last


def test_the_first_item_that_changed_is_named():
    tracker = RequestLayoutTracker()
    tracker.observe(_request([_msg("a"), _msg("b"), {"type": "reasoning", "id": "r1"}, _msg("ctx")]))
    tracker.observe(_request([_msg("a"), _msg("CHANGED"), {"type": "reasoning", "id": "r1"}, _msg("ctx2")]))
    assert tracker.last["items_shared_with_previous"] == 1
    assert tracker.last["lost_items"] == 2
    assert tracker.last["first_difference"] == {"index": 1, "was": "user", "now": "user"}


def test_a_window_that_slides_shows_as_the_first_item_changing():
    tracker = RequestLayoutTracker()
    tracker.observe(_request([_msg("turn 1"), _msg("turn 2"), _msg("turn 3"), _msg("ctx")]))
    tracker.observe(_request([_msg("turn 2"), _msg("turn 3"), _msg("turn 4"), _msg("ctx")]))
    assert tracker.last["items_shared_with_previous"] == 0
    assert tracker.last["first_difference"]["index"] == 0


def test_other_tools_or_other_endpoints_are_not_compared():
    tracker = RequestLayoutTracker()
    tracker.observe(_request([_msg("a"), _msg("ctx")], tools=[{"name": "x"}]))
    tracker.observe(_request([_msg("a"), _msg("ctx")], tools=[]))  # a summary request has no tools
    assert "items_shared_with_previous" not in tracker.last
    before = dict(tracker.last)
    tracker.observe(_request([_msg("zzz")], path="/v1/chat/completions"))
    assert tracker.last == before


def test_the_record_holds_hashes_and_counts_only():
    tracker = RequestLayoutTracker()
    tracker.observe(_request([_msg("a secret goal"), _msg("ctx")]))
    assert "secret" not in json.dumps(tracker.last)
