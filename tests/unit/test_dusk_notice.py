"""The Agent hears each Overworld sunset once, as a player sees it (easy-run-20261106.md)."""

import asyncio
from types import SimpleNamespace

from minebot.app.body_loop import BodyLoop
from minebot.app import reflex_question as rq


def loop_with(states):
    heard, traced = [], []

    async def read(callback, *args):
        return callback(*args)

    reads = iter(states)
    runtime = SimpleNamespace(
        decision_client=SimpleNamespace(), trace=SimpleNamespace(emit=lambda *a, **k: traced.append((a, k))),
        body=SimpleNamespace(get_reflex_facts=lambda: next(reads),
                             perceive=lambda *a: {"ok": True, "data": {"entities": []}}),
        _read_lane=SimpleNamespace(run=read), direct_call_handoffs={},
        notify_body_lifecycle_event=heard.append)
    loop = BodyLoop(runtime)
    loop._life_profile = None
    return loop, heard


def state(time, dimension="minecraft:overworld"):
    return {"position": [1, 64, 2], "time": time, "dimension": dimension, "health": 20}


def test_sunset_is_told_once_with_the_clock():
    loop, heard = loop_with([state(11900), state(12010), state(12500), state(13200)])
    for _ in range(4):
        asyncio.run(loop._read_body_facts())
    assert [event.name for event in heard] == ["dusk"]
    message = heard[0].data["message"]
    assert "The sun is setting (time 12010)" in message and "in about 50 seconds" in message


def test_next_day_sunset_is_told_again_and_seq_is_new():
    loop, heard = loop_with([state(11000), state(12100), state(23500), state(24000 + 11000),
                             state(24000 + 12050)])
    for _ in range(5):
        asyncio.run(loop._read_body_facts())
    dusks = [event for event in heard if event.name == "dusk"]
    assert [event.seq for event in dusks] == [1, 2]
    assert dusks[0].data["native_epoch"] != dusks[1].data["native_epoch"]


def test_joining_at_night_or_other_dimensions_say_nothing():
    loop, heard = loop_with([state(15000), state(16000), state(11000, "minecraft:the_nether"),
                             state(12500, "minecraft:the_nether")])
    for _ in range(4):
        asyncio.run(loop._read_body_facts())
    assert heard == []


def test_skipping_dusk_says_night_has_fallen():
    loop, heard = loop_with([state(11000), state(13500)])
    for _ in range(2):
        asyncio.run(loop._read_body_facts())
    assert heard[0].data["message"].startswith("Night has fallen (time 13500)")


def test_daylight_sentence_unchanged():
    assert rq.daylight_sentence(1000) == "It is day."
    assert rq.daylight_sentence(12500) == "It is dusk; night falls soon."
    assert rq.daylight_sentence(14000) == "It is night."
    assert rq.daylight_sentence(None) is None


def test_sunrise_is_told_once_after_a_night():
    loop, heard = loop_with([state(16000), state(22900), state(23050), state(23500), state(24000 + 1000)])
    for _ in range(5):
        asyncio.run(loop._read_body_facts())
    assert [event.name for event in heard] == ["dawn"]
    assert heard[0].data["message"] == "The sun is up (time 23050). Sunset is at 12000, in about 648 seconds."
    assert heard[0].data["native_epoch"].startswith("dawn-")


def test_joining_by_day_or_sunrise_elsewhere_says_nothing():
    loop, heard = loop_with([state(1000), state(2000), state(16000, "minecraft:the_nether"),
                             state(23500, "minecraft:the_nether")])
    for _ in range(4):
        asyncio.run(loop._read_body_facts())
    assert heard == []
